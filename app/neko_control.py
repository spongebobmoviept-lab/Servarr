"""Drives the shared Neko browser session directly to start a movie playing
in Plex Web, for Movie Night.

Plex's own remote-control API (/player/playback/*) does NOT work against a
Plex Web browser session — confirmed live, it 404s even though the session
shows up correctly in /status/sessions. Only native apps (Roku, mobile,
desktop) support that. So instead of asking Plex to control the browser,
this controls the browser itself, exactly like a person would: paste the
movie's Plex Web URL into the address bar, navigate, and click the on-screen
Play button.

Neko's real WebSocket control protocol (confirmed against the server source,
github.com/m1k1o/neko) uses separate control/keydown and control/keyup
events, each taking only {"keysym": uint32} — NOT a single control/keypress
with a "pressed" flag, which silently does nothing.
"""

import asyncio
import json
import urllib.parse
from typing import Optional

import httpx
import websockets

from .config import settings
from .logger import log

_CTRL_L = 0xFFE3
_KEY_L = ord("l")
_KEY_A = ord("a")
_KEY_V = ord("v")
_RETURN = 0xFF0D
_ESCAPE = 0xFF1B
_LEFT_BUTTON = 1

# Where the "Play" button sits on a movie's Plex Web details page, at Neko's
# fixed 1920x1080 screen (NEKO_SCREEN in neko/.env) — confirmed live for a
# fresh/completed item. A partially-watched movie shows "Resume" instead,
# which may not land in exactly the same spot; a missed click there just
# leaves everyone looking at the right movie's page instead of it playing,
# which is an accepted fallback rather than something worth pixel-perfect
# detection for every watch state.
_PLAY_BUTTON_X = 657
_PLAY_BUTTON_Y = 417

# The Plex Home profile tile Neko logs in as, on Plex's "Select User" picker
# — confirmed live. That picker (and its PIN prompt) resets after any Neko/
# Chromium restart, so this is sent defensively before every navigation
# rather than only when the picker is actually showing: if it's not showing,
# these clicks/keys land harmlessly on whatever page is already up, since
# the real navigation right after this overwrites it regardless.
_PROFILE_TILE_X = 742
_PROFILE_TILE_Y = 300

# For a partially-watched item, clicking Play/Resume opens Plex Web's small
# docked mini-player instead of the full player — confirmed live (Mud, mid-
# watch). Clicking its thumbnail expands to the full inline player; for a
# fresh item that's already in the full player, this same click just lands
# on inert label text there, so it's safe to send unconditionally either way.
_MINI_PLAYER_X = 78
_MINI_PLAYER_Y = 1032

# Plex Web's own in-page fullscreen toggle, top-right of the inline player's
# overlay bar (separate from Chromium's browser chrome) — confirmed live,
# same position whether the player was reached directly or via the mini-
# player expand above.
_FULLSCREEN_BUTTON_X = 1897
_FULLSCREEN_BUTTON_Y = 117

# A neutral point over the video itself (not over any control bar, which
# would pin the on-screen controls open) — parking the cursor here lets
# Plex Web's own inactivity timer hide both the OSD and the cursor, so the
# shared view is just the movie with nothing overlaid.
_NEUTRAL_X = 960
_NEUTRAL_Y = 500


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


async def _set_clipboard(token: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.neko_url}/api/room/clipboard",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": text},
        )
        resp.raise_for_status()


def _ws_url(token: str) -> str:
    base = settings.neko_url.replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/api/ws?token={token}"


async def _down(ws, keysym: int) -> None:
    await ws.send(json.dumps({"event": "control/keydown", "payload": {"keysym": keysym}}))


async def _up(ws, keysym: int) -> None:
    await ws.send(json.dumps({"event": "control/keyup", "payload": {"keysym": keysym}}))


async def _tap(ws, keysym: int) -> None:
    await _down(ws, keysym)
    await asyncio.sleep(0.05)
    await _up(ws, keysym)
    await asyncio.sleep(0.05)


async def _combo(ws, modifier: int, key: int) -> None:
    await _down(ws, modifier)
    await asyncio.sleep(0.05)
    await _tap(ws, key)
    await _up(ws, modifier)
    await asyncio.sleep(0.2)


async def _click(ws, x: int, y: int) -> None:
    await ws.send(json.dumps({"event": "control/move", "payload": {"x": x, "y": y}}))
    await asyncio.sleep(0.1)
    await ws.send(json.dumps({"event": "control/buttondown", "payload": {"x": x, "y": y, "code": _LEFT_BUTTON}}))
    await asyncio.sleep(0.08)
    await ws.send(json.dumps({"event": "control/buttonup", "payload": {"x": x, "y": y, "code": _LEFT_BUTTON}}))


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
    password = None
    try:
        with open(settings.neko_shared_env_file, encoding="utf-8") as f:
            for line in f:
                if line.startswith("NEKO_PASSWORD="):
                    password = line.split("=", 1)[1].strip()
                    break
    except OSError:
        pass
    if not password:
        return settings.neko_public_url
    return f"{settings.neko_public_url}/?pwd={urllib.parse.quote(password)}"


async def play_movie(plex_web_url: str) -> bool:
    """Navigate the shared Neko browser to a movie's Plex Web page and hit
    play. Best-effort: logs and returns False on any failure rather than
    raising, since a failed playback automation shouldn't crash Movie Night —
    the announcement with the Neko link still goes out either way.
    """
    if not settings.neko_url or not settings.neko_admin_password:
        await log("neko_control: NEKO_URL/NEKO_ADMIN_PASSWORD not configured — skipping playback automation")
        return False
    try:
        token = await _login()
        if not token:
            return False
        await _take_control(token)
        await _set_clipboard(token, plex_web_url)
        async with websockets.connect(_ws_url(token)) as ws:
            await asyncio.sleep(0.5)
            if settings.plex_home_pin:
                await _click(ws, _PROFILE_TILE_X, _PROFILE_TILE_Y)
                await asyncio.sleep(1)
                for digit in settings.plex_home_pin:
                    await _tap(ws, ord(digit))
                await asyncio.sleep(1)
            await _tap(ws, _ESCAPE)  # exit fullscreen if something's already playing — hides the address bar otherwise
            await asyncio.sleep(0.3)
            await _combo(ws, _CTRL_L, _KEY_L)  # focus address bar
            await _combo(ws, _CTRL_L, _KEY_A)  # select existing text
            await _combo(ws, _CTRL_L, _KEY_V)  # paste the new URL
            await asyncio.sleep(0.3)
            await _tap(ws, _RETURN)  # navigate
            await asyncio.sleep(4)  # let the details page finish loading before clicking play
            await _click(ws, _PLAY_BUTTON_X, _PLAY_BUTTON_Y)
            await asyncio.sleep(2.5)  # let the player (or mini-player) come up
            await _click(ws, _MINI_PLAYER_X, _MINI_PLAYER_Y)  # expand if it landed in the mini-player
            await asyncio.sleep(1.5)
            await _click(ws, _FULLSCREEN_BUTTON_X, _FULLSCREEN_BUTTON_Y)  # true fullscreen, hides all browser chrome
            await asyncio.sleep(1)
            await ws.send(json.dumps({"event": "control/move", "payload": {"x": _NEUTRAL_X, "y": _NEUTRAL_Y}}))  # off the controls, so they and the cursor auto-hide
        await log(f"neko_control: sent play for {plex_web_url}")
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort automation, never crash Movie Night over it
        await log(f"neko_control: playback automation failed: {exc}")
        return False
