"""Drives the shared Neko session directly to start a movie playing, for
Movie Night.

Plex's own remote-control API (/player/playback/*) does NOT work against
this setup — confirmed live, even the native Plex Desktop client never
registers itself as a controllable player (no GDM broadcast, no plex.tv
resource registration, no "advertise as player" setting anywhere in its
own UI). So instead of asking Plex to control the client, this controls
the client itself — but via Chromium's DevTools Protocol (the same
mechanism Puppeteer/Playwright use), not by guessing at pixel positions in
Plex's own blended search UI, which mixes in ambiguous cross-source
"Discover" results for popular titles (confirmed live: clicking the top
search result for "The Matrix" landed on a $3.99 rental page instead of
the library copy). Instead, the exact library item is resolved server-side
first (plex.resolve_library_item — always your own library, never
Discover), then Plex Desktop is told to navigate straight to it by setting
its internal route hash directly.

The one real complication: Plex Desktop disables its hardware video
overlay entirely while its own remote-debugging port is active (confirmed
live — tried the standard Qt debugging env var and an explicit
hardware-overlay-preserving Chromium flag, neither fixed it; this looks
like deliberate behavior in Plex's closed-source client, not a bug we can
flag our way around). So debugging is only ever turned on for the few
seconds it takes to resolve and launch a movie, then off again before
anyone actually watches — Plex Desktop's own "resume where you left off"
feature (restorePlayQueue, in plex.ini) picks the exact position back up
automatically across that restart.
"""

import asyncio
import json
import re
import urllib.parse
import xmlrpc.client
from typing import Optional

import httpx
import websockets

from . import plex
from .config import settings
from .logger import log

_RETURN = 0xFF0D
_SPACE = 0x20
_LEFT_BUTTON = 1

# All pixel coordinates below are calibrated for Neko's configured screen
# size (NEKO_SCREEN in neko/.env) and MUST be re-confirmed live any time
# that resolution changes. Current calibration: 2560x1440.
#
# Unlike the old search-dropdown coordinates this replaces, these two are
# NOT content-dependent — they're Plex Desktop's own persistent mini-player
# bar, which sits in the same spot regardless of what's playing. Confirmed
# live across multiple different titles.
_MINI_PLAYER_RESUME_X = 1270
_MINI_PLAYER_RESUME_Y = 1391
_MINI_PLAYER_EXPAND_X = 77
_MINI_PLAYER_EXPAND_Y = 1389
# Same fixed transport overlay bar as above — present at this same spot
# whether the player is in its compact or fullscreen state, confirmed live.
_CLOSE_PLAYER_X = 1375
_CLOSE_PLAYER_Y = 1392

_CDP_POLL_INTERVAL_SECONDS = 0.5
_CDP_POLL_TIMEOUT_SECONDS = 20
_RESTART_SETTLE_SECONDS = 12  # plex-desktop's own cold-boot time, confirmed live


async def test_connection(url: str, admin_password: str) -> dict:
    """Ad-hoc connectivity check for the setup wizard — confirms the admin
    password actually logs in, using credentials the caller just typed in
    rather than whatever's already saved.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{url.rstrip('/')}/api/login",
            json={"username": "admin", "password": admin_password},
        )
        resp.raise_for_status()
        data = resp.json()
    if not data.get("profile", {}).get("is_admin"):
        raise ValueError("That password logged in, but not as an admin")
    return {"ok": True}


async def _login() -> Optional[str]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.neko_url}/api/login",
            json={"username": "admin", "password": settings.neko_admin_password},
        )
        resp.raise_for_status()
        return resp.json().get("token")


async def _take_control(token: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.neko_url}/api/room/control/take",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()


def _ws_url(token: str) -> str:
    base = settings.neko_url.replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/api/ws?token={token}"


async def _tap(ws, keysym: int) -> None:
    await ws.send(json.dumps({"event": "control/keydown", "payload": {"keysym": keysym}}))
    await asyncio.sleep(0.05)
    await ws.send(json.dumps({"event": "control/keyup", "payload": {"keysym": keysym}}))
    await asyncio.sleep(0.05)


async def _click(ws, x: int, y: int) -> None:
    await ws.send(json.dumps({"event": "control/move", "payload": {"x": x, "y": y}}))
    await asyncio.sleep(0.1)
    await ws.send(json.dumps({"event": "control/buttondown", "payload": {"x": x, "y": y, "code": _LEFT_BUTTON}}))
    await asyncio.sleep(0.08)
    await ws.send(json.dumps({"event": "control/buttonup", "payload": {"x": x, "y": y, "code": _LEFT_BUTTON}}))


def _read_shared_env_value(key: str) -> Optional[str]:
    """Generic reader for neko/.env, shared read-only into this container
    (see docker-compose.yml) — used for the viewer-link password below and
    for the Supervisor RPC credentials, so neither needs duplicating into
    Servarr's own .env.
    """
    try:
        with open(settings.neko_shared_env_file, encoding="utf-8") as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def current_viewer_link() -> Optional[str]:
    """The link to post in Discord — auto-fills the (daily-rotating, see
    neko/rotate_password.sh) member password so nobody has to type or share
    it, but deliberately does NOT auto-fill a display name: Neko's frontend
    only auto-logs in when both are pre-filled (client/src/components/
    connect.vue), so leaving usr out means each viewer is prompted for their
    own name on first click — the only way for people to actually show up as
    themselves in the shared session instead of everyone looking like the
    same generic "Guest". Their browser remembers it after that first time.
    Falls back to the plain URL (asking whoever clicks it to type the
    password too) if the shared env file isn't mounted or has no password yet.
    """
    if not settings.neko_public_url:
        return None
    password = _read_shared_env_value("NEKO_PASSWORD")
    if not password:
        return settings.neko_public_url
    return f"{settings.neko_public_url}/?pwd={urllib.parse.quote(password)}"


def admin_viewer_link() -> Optional[str]:
    """Only ever sent as a private (ephemeral) reply to whoever explicitly
    asked for a specific movie via /play-movie — never posted anywhere the
    rest of the channel can see it. Unlike the regular viewer link, this
    lets them actually control the shared session themselves (pause, seek).

    This is the SAME fixed admin password Servarr's own automation uses
    (neko_admin_password), not a per-request or rotating one — handing it
    out any more broadly than one ephemeral reply per request would defeat
    the point of it being admin-only.
    """
    if not settings.neko_public_url or not settings.neko_admin_password:
        return None
    return f"{settings.neko_public_url}/?pwd={urllib.parse.quote(settings.neko_admin_password)}"


def _neko_host() -> str:
    return urllib.parse.urlparse(settings.neko_url).hostname


def _supervisor_proxy() -> xmlrpc.client.ServerProxy:
    user = _read_shared_env_value("SUPERVISOR_RPC_USER") or "admin"
    password = _read_shared_env_value("SUPERVISOR_RPC_PASSWORD") or ""
    auth = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}"
    url = f"http://{auth}@{_neko_host()}:{settings.neko_supervisor_rpc_port}/RPC2"
    return xmlrpc.client.ServerProxy(url)


def _restart_plex_desktop_sync() -> None:
    proxy = _supervisor_proxy()
    try:
        proxy.supervisor.stopProcess("plex-desktop", True)
    except xmlrpc.client.Fault:
        pass  # already stopped
    proxy.supervisor.startProcess("plex-desktop", True)


async def _restart_plex_desktop() -> None:
    await asyncio.to_thread(_restart_plex_desktop_sync)


def _set_web_inspector_port_sync(enabled: bool) -> None:
    """Flips Plex Desktop's own remote-debugging setting by editing the
    persisted plex.ini directly (mounted read-write — see docker-compose.yml).
    Plex Desktop must be fully restarted for a change here to take effect;
    it does not hot-reload this file.
    """
    value = "9222" if enabled else "0"
    with open(settings.neko_plex_ini_path, encoding="utf-8") as f:
        content = f.read()
    content = re.sub(r"webInspectorPort=\d+", f"webInspectorPort={value}", content)
    # Plex's own web layer also caches this inside its persisted
    # deviceSettings JSON blob — confirmed live it can otherwise silently
    # re-apply the old value from there on next boot.
    content = re.sub(
        r'\\"settings\.webInspectorPort\\":\d+',
        rf'\\"settings.webInspectorPort\\":{value}',
        content,
    )
    with open(settings.neko_plex_ini_path, "w", encoding="utf-8") as f:
        f.write(content)


async def _set_web_inspector_port(enabled: bool) -> None:
    await asyncio.to_thread(_set_web_inspector_port_sync, enabled)


def _our_client_id_sync() -> Optional[str]:
    """Plex Desktop's own persisted client identifier (plex.ini's clientID=
    line) — used to clean up only OUR sessions after a forced restart,
    never anyone else's real device."""
    with open(settings.neko_plex_ini_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("clientID="):
                return line.split("=", 1)[1].strip()
    return None


async def _cleanup_stale_sessions() -> None:
    """Forcibly restarting plex-desktop (see _restart_plex_desktop) kills
    the player process without ever telling Plex the session ended —
    confirmed live this can leave a duplicate/orphaned entry sitting in
    /status/sessions. Called before and after every automated playback so
    nothing lingers on the server regardless of how the previous run ended.
    """
    client_id = await asyncio.to_thread(_our_client_id_sync)
    if not client_id:
        return
    try:
        await plex.terminate_client_sessions(client_id)
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup, never block playback over it
        await log(f"neko_control: session cleanup failed: {exc}")


def _cdp_base_url() -> str:
    return f"http://{_neko_host()}:{settings.neko_cdp_port}"


async def _wait_for_cdp_page() -> str:
    """Polls the always-on cdp-bridge (see rpc-supervisord.conf) until Plex
    Desktop's debug port is actually reachable through it — it only starts
    listening once the freshly-restarted app has finished booting.
    """
    deadline = asyncio.get_event_loop().time() + _CDP_POLL_TIMEOUT_SECONDS
    async with httpx.AsyncClient(timeout=5) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await client.get(f"{_cdp_base_url()}/json")
                pages = resp.json()
                if pages:
                    return pages[0]["webSocketDebuggerUrl"]
            except (httpx.HTTPError, ValueError, KeyError, IndexError):
                pass
            await asyncio.sleep(_CDP_POLL_INTERVAL_SECONDS)
    raise TimeoutError("Plex Desktop's debug port never came up after restart")


class _CDPSession:
    """Thin wrapper over a raw DevTools Protocol websocket connection.
    Clicks go through Input.dispatchMouseEvent (a genuinely trusted,
    synthesized input event) rather than calling .click() via JS — Chromium
    treats the latter as untrusted and silently refuses user-activation-
    gated behavior for it, confirmed live (the player never entered its own
    fullscreen video mode when driven that way).
    """

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next_id = 1

    async def _send(self, method: str, params: Optional[dict] = None, timeout: float = 10) -> dict:
        msg_id = self._next_id
        self._next_id += 1
        await self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            resp = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=timeout))
            if resp.get("id") == msg_id:
                return resp

    async def eval_js(self, expression: str, timeout: float = 10):
        resp = await self._send("Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout)
        result = resp.get("result", {}).get("result", {})
        return result.get("value")

    async def click_xy(self, x: float, y: float) -> None:
        await self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await self._send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        await asyncio.sleep(0.05)
        await self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

    async def click_selector(self, selector: str) -> bool:
        rect = await self.eval_js(f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {{x: r.x + r.width / 2, y: r.y + r.height / 2}};
            }})()
        """)
        if not rect:
            return False
        await self.click_xy(rect["x"], rect["y"])
        return True

    async def find_button_by_text(self, pattern: str) -> Optional[dict]:
        """Buttons in Plex's resume-choice dialog ("Resume from ..." /
        "Start from the beginning") have no data-testid, confirmed live —
        only a real DOM query by visible text finds them reliably."""
        result = await self.eval_js(f"""
            JSON.stringify(Array.from(document.querySelectorAll('button')).map(b => {{
                const r = b.getBoundingClientRect();
                return {{text: (b.textContent || '').trim(), x: r.x + r.width / 2, y: r.y + r.height / 2}};
            }}).filter(b => new RegExp({json.dumps(pattern)}, 'i').test(b.text)))
        """)
        matches = json.loads(result) if result else []
        return matches[0] if matches else None


async def _drive_playback_via_cdp(machine_id: str, rating_key: str, expected_title: str) -> bool:
    ws_url = await _wait_for_cdp_page()
    await log(f"neko_control: CDP reachable, driving {expected_title!r}")
    key_enc = urllib.parse.quote(f"/library/metadata/{rating_key}", safe="")
    target_hash = f"#!/server/{machine_id}/details?key={key_enc}"

    async with websockets.connect(ws_url, max_size=None) as ws:
        session = _CDPSession(ws)
        await session.eval_js(f"location.hash = {json.dumps(target_hash)};")
        await asyncio.sleep(2)

        heading = await session.eval_js("document.querySelector('h1')?.textContent || ''")
        await log(f"neko_control: landed on details page heading={heading!r}")

        if not await session.click_selector('[data-testid="preplay-play"]'):
            await log(f"neko_control: no Play button found for {expected_title!r} — details page may not have loaded")
            return False

        await asyncio.sleep(1.5)
        # If a saved position exists, clicking the preplay button either
        # resumes directly (button already said "Resume", no dialog at
        # all — confirmed live) or opens a choice dialog first ("Resume
        # from X" / "Start from the beginning", when the button said
        # "Play" instead). Either way is fine to dismiss with whichever
        # option, since the Previous-button restart below forces position
        # 0 regardless — so this only needs to get playback moving, not
        # pick the right choice itself.
        dialog_button = await session.find_button_by_text(r"beginning|resume from")
        if dialog_button:
            await session.click_xy(dialog_button["x"], dialog_button["y"])
            await asyncio.sleep(1)

        started = False
        for attempt in range(6):
            await asyncio.sleep(1.5)
            playing = await plex.is_title_playing(expected_title)
            if playing:
                started = True
                break
        if not started:
            await log(f"neko_control: {expected_title!r} never showed up as playing in Plex's own session list")
            return False

        # Force position 0:00 regardless of anyone's individual watch
        # history — clicking Previous with nothing earlier in the queue
        # restarts the current item instead (confirmed live: 9:21 -> 0:01),
        # which is a real, deterministic player control rather than a guess
        # at which dialog option does what.
        await session.click_selector('[data-testid="previousButton"]')
        await asyncio.sleep(0.5)

        # Pause immediately, right here, while still just a few seconds in —
        # otherwise the movie keeps advancing invisibly (screen is white the
        # whole time debugging is on) through both restarts and the expand
        # step below, and everyone would join partway in by the time it's
        # actually visible. The later click that resumes it out of the
        # mini-player (_resume_and_expand_player) is what really starts it
        # for the room, so nothing is lost.
        await session.click_selector('[data-testid="pauseButton"]')

    # NOTE: deliberately does NOT click "Close Player" here — tried that to
    # avoid the orphaned-session issue below, but confirmed live it clears
    # plex.ini's own restorePlayQueue entirely (goes to "{}"), which is the
    # exact state the debug-off restart below depends on to resume at all.
    # _cleanup_stale_sessions (API-based, doesn't touch client-local state)
    # is the safe way to handle that instead.
    return True


async def _resume_and_expand_player() -> None:
    """After the debug-off restart, Plex Desktop lands on Home with the
    just-started movie sitting paused in its persistent mini-player bar
    (restorePlayQueue) — confirmed live this is always the same fixed
    layout regardless of what's playing, unlike the search results this
    replaces, so plain coordinate clicks are reliable here. Retries the
    actual clicks (not just a spacebar fallback) because the fixed
    post-restart settle time isn't always long enough for the app to be
    fully interactive yet — confirmed live the exact same click works fine
    once the app's had more time to settle.
    """
    client_id = await asyncio.to_thread(_our_client_id_sync)
    token = await _login()
    if not token:
        return
    await _take_control(token)
    async with websockets.connect(_ws_url(token)) as ws:
        await asyncio.sleep(0.5)
        for attempt in range(5):
            await _click(ws, _MINI_PLAYER_RESUME_X, _MINI_PLAYER_RESUME_Y)
            await asyncio.sleep(1)
            await _click(ws, _MINI_PLAYER_EXPAND_X, _MINI_PLAYER_EXPAND_Y)
            await asyncio.sleep(1.5)
            if not client_id or await plex.is_client_playing(client_id):
                return
            await log(f"neko_control: still not playing after resume/expand click attempt {attempt + 1}, retrying")
        await log("neko_control: could not get playback out of a paused state after expanding to fullscreen")


async def play_movie(title: str, year: Optional[int] = None) -> bool:
    """Resolves the exact library item on our own server (never a Discover/
    rental result — see plex.resolve_library_item), tells Plex Desktop to
    navigate straight to it and hit play, then restores normal (debugging
    off) video rendering before returning. Best-effort: logs and returns
    False on any failure rather than raising, since a failed playback
    automation shouldn't crash Movie Night — the announcement with the Neko
    link still goes out either way.
    """
    if not settings.neko_url or not settings.neko_admin_password:
        await log("neko_control: NEKO_URL/NEKO_ADMIN_PASSWORD not configured — skipping playback automation")
        return False

    item = await plex.resolve_library_item(title, year)
    if item is None:
        await log(f"neko_control: {title!r} not found in the library — skipping playback automation")
        return False

    # Clean up anything left over from a previous run before touching
    # anything else — a crash or an aborted automation earlier could have
    # left a stale session sitting active on the server.
    await _cleanup_stale_sessions()

    played = False
    try:
        await _set_web_inspector_port(True)
        await _restart_plex_desktop()
        played = await _drive_playback_via_cdp(item["machine_id"], item["rating_key"], item["title"])
    except Exception as exc:  # noqa: BLE001 - best-effort automation, never crash Movie Night over it
        await log(f"neko_control: playback automation failed: {exc}")
    finally:
        # Explicitly end the phase-1 session through Plex's own API before
        # killing the process that was driving it — confirmed live that
        # restarting first (process just vanishes) triggers a "device asked
        # to stop your playback" error toast client-side, since the server
        # never got a clean stop. Terminating it first avoids that.
        await _cleanup_stale_sessions()
        # Always turn debugging back off, even on failure — leaving it on
        # would leave video broken for anyone using the shared session,
        # confirmed live, regardless of whether this specific attempt worked.
        try:
            await _set_web_inspector_port(False)
            await _restart_plex_desktop()
            await asyncio.sleep(_RESTART_SETTLE_SECONDS)
        except Exception as exc:  # noqa: BLE001
            await log(f"neko_control: failed to restore normal video rendering: {exc}")

    if played:
        try:
            await _resume_and_expand_player()
        except Exception as exc:  # noqa: BLE001
            await log(f"neko_control: playback started but expanding to fullscreen failed: {exc}")
        await log(f"neko_control: sent play for {item['title']!r}")

    return played


async def stop_movie() -> bool:
    """/stop-movie's actual logic — ends whatever's currently playing in the
    shared session. Clicks Plex's own Close Player control (the same fixed
    overlay bar the mini-player buttons live on, confirmed live it's in the
    same spot whether the player is compact or fullscreen) and explicitly
    terminates the session server-side too, so nothing lingers in Plex's
    own session list afterward the way a bare process restart would (see
    _cleanup_stale_sessions).
    """
    if not settings.neko_url or not settings.neko_admin_password:
        await log("neko_control: NEKO_URL/NEKO_ADMIN_PASSWORD not configured — skipping stop")
        return False
    try:
        token = await _login()
        if not token:
            return False
        await _take_control(token)
        async with websockets.connect(_ws_url(token)) as ws:
            await asyncio.sleep(0.3)
            await _click(ws, _CLOSE_PLAYER_X, _CLOSE_PLAYER_Y)
            await asyncio.sleep(1)
    except Exception as exc:  # noqa: BLE001 - best-effort, still try the server-side cleanup below
        await log(f"neko_control: stop click failed: {exc}")

    await _cleanup_stale_sessions()
    await log("neko_control: stopped playback")
    return True
