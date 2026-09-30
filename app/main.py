import json
import os
import re
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse
from xml.etree.ElementTree import ParseError as ET_ParseError

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import (
    auth_store,
    features,
    friendly,
    bot,
    connections_store,
    invite,
    movie_night,
    mpv_control,
    player_library,
    plex,
    plex_monitor,
    radarr,
    reclaimarr_client,
    release_digest,
    settings_store,
    sonarr,
    tautulli,
    xp_store,
)
from .auth import MPV_REMOTE_COOKIE, check_credentials, check_remote_key, require_login, require_login_or_remote_key, security
from .config import APP_VERSION, settings
from .logger import log

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

# Explicit no-cache on every static asset — this app changes during setup/
# tuning, and a browser silently serving a stale cached page is a much worse
# failure mode than the tiny cost of re-fetching these small files on every load.
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.makedirs(settings.data_dir, exist_ok=True)
    connections_store.load_overrides()
    settings_store.load_overrides()
    if connections_store.ensure_remote_key():
        await log("servarr: generated a random web-remote key (MPV_REMOTE_KEY) — no action needed")
    if connections_store.auto_pair_from_file():
        await log(f"servarr: paired with the Movie Night player at {settings.neko_mpv_shim_url} (from its pairing file)")
    elif settings.player_pair_file and not settings.neko_mpv_shim_url:
        await log(f"servarr: waiting for the Movie Night player's pairing file ({settings.player_pair_file}) — it pairs by itself once the player writes it")
    await mpv_control.refresh_links(force=True)
    await xp_store.store.load()

    if features.unknown_env_value():
        await log(f"servarr: SERVARR_MODE='{features.unknown_env_value()}' isn't a mode (use full or movienight) — ignoring it")
    if features.is_movienight():
        how = "set by SERVARR_MODE" if features.locked() else "picked on the setup page"
        await log(f"servarr: starting in Movie Night only mode ({how}) — calendars, leveling, moderation and the Sonarr/Radarr/Tautulli/Plex integrations are off")
    else:
        await log("servarr: starting")
    plex_monitor.set_client(bot.client)
    movie_night.set_client(bot.client)
    release_digest.set_client(bot.client)
    bot.launch()
    features.reconcile_tasks()

    yield
    features.stop_tasks()
    await bot.shutdown()


app = FastAPI(title="Servarr", version=APP_VERSION, lifespan=lifespan)


def _origin_of(url: str) -> str:
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _allowed_origins() -> set[str]:
    """Browser origins allowed to call Servarr cross-origin: the player's
    viewer/public pages (which may embed /mpv-remote in an iframe), the
    configured public URL of Servarr itself, and CORS_ALLOWED_ORIGINS."""
    candidates = [
        settings.neko_mpv_public_url,
        settings.neko_mpv_viewer_url,
        settings.servarr_public_url,
        *mpv_control.link_origins(),
        *settings.cors_allowed_origins,
    ]
    return {o for o in (_origin_of(c) for c in candidates) if o}


_CORS_ALLOW_HEADERS = "Content-Type, Authorization, X-Remote-Key"


@app.middleware("http")
async def cross_origin_guard(request: Request, call_next):
    """CORS + Private Network Access, restricted to an allow-list.

    A player page opened via a public address that embeds /mpv-remote in an
    iframe needs Chromium's Private Network Access preflight answered, or
    the request is silently blocked. Only allow-listed origins get that
    (and CORS) — reflecting any Origin with credentials would let any
    website a logged-in admin visits read Servarr's API. Cross-origin
    state-changing requests from other origins are refused outright (CSRF
    protection for the cookie/Basic-auth routes).
    """
    origin = (request.headers.get("origin") or "").lower()
    host = (request.headers.get("host") or "").lower()
    same_origin = bool(origin) and urlparse(origin).netloc == host
    allowed = bool(origin) and (same_origin or origin in _allowed_origins())

    if request.method == "OPTIONS" and origin:
        if not allowed:
            return Response(status_code=403)
        response = Response(status_code=204)
    else:
        if origin and not allowed and request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
        response = await call_next(request)

    if origin and allowed and not same_origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = _CORS_ALLOW_HEADERS
        if request.headers.get("access-control-request-private-network"):
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        response.headers["Vary"] = "Origin"
    return response


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": APP_VERSION}


def _full_only(feature: str) -> None:
    """Refuses a setup/API call for something Movie Night only mode turns off."""
    if features.is_movienight():
        raise HTTPException(status_code=409, detail=features.refusal(feature))


def _mode_info() -> dict:
    return {
        "mode": features.current(),
        "mode_label": features.LABELS[features.current()],
        "mode_locked": features.locked(),
        "mode_chosen": features.chosen(),
    }


# setup.html wraps each mode's sections in <!-- only:MODE --> ... <!-- /only:MODE -->.
_OTHER_MODE_BLOCKS = {
    mode: re.compile(rf"[ \t]*<!-- only:{other} -->.*?<!-- /only:{other} -->\n?", re.S)
    for mode, other in ((features.FULL, features.MOVIENIGHT), (features.MOVIENIGHT, features.FULL))
}


def _setup_page() -> HTMLResponse:
    """The setup page without the other mode's sections: in Movie Night only
    mode the Radarr/Sonarr/Plex/Tautulli/leveling/moderation sections aren't
    sent at all. Until a mode is picked (fresh install) it carries both, and
    the page drops one set when the choice is made."""
    with open(os.path.join(STATIC_DIR, "setup.html"), "r", encoding="utf-8") as f:
        html = f.read()
    if features.chosen():
        html = _OTHER_MODE_BLOCKS[features.current()].sub("", html)
    return HTMLResponse(html, headers=_NO_CACHE_HEADERS)


@app.get("/favicon.svg")
async def favicon() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "favicon.svg"), media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    # Checked before auth on purpose: a fresh, unconfigured instance has no
    # working login yet, so gating "/" behind require_login here would just
    # show the browser's native Basic Auth popup with no credentials that
    # could possibly work — a dead end for a first-time visitor. Route them
    # to the wizard instead, which is what actually creates that login.
    if not auth_store.is_setup_complete():
        return RedirectResponse(url="/setup")
    credentials = await security(request)
    check_credentials(request, credentials)
    return _setup_page()


@app.get("/setup", response_class=HTMLResponse)
async def setup_page() -> HTMLResponse:
    # Same page serves both first-run setup AND later editing — once setup is
    # complete, the page's own JS asks for login (via a 401 from /api/connections)
    # and switches into an unlocked "edit any step" mode instead of the
    # sequential first-run flow. No separate dashboard to build for that.
    return _setup_page()


@app.get("/api/setup/status")
async def api_setup_status() -> JSONResponse:
    """Public on purpose — this is the very first call the page makes,
    before any login exists, to decide whether to show the wizard at all.
    """
    return JSONResponse(
        {
            "setup_complete": auth_store.is_setup_complete(),
            "admin_configured": auth_store.is_admin_configured(),
            "discord_configured": bool(settings.discord_bot_token),
            **_mode_info(),
        }
    )


async def _apply_mode_change() -> None:
    """Makes a mode switch take effect now: background loops start/stop and
    the bot reconnects with that mode's commands, listeners and intents."""
    features.reconcile_tasks()
    bot.request_restart()


async def _save_mode(wanted: str) -> None:
    changed = wanted != features.current()
    settings_store.save_mode(wanted)
    if changed:
        await log(f"setup: mode set to {features.LABELS[wanted]}")
        await _apply_mode_change()


@app.post("/api/setup/admin")
async def api_setup_admin(body: dict) -> JSONResponse:
    """Creates the one and only admin login. Deliberately not behind
    require_login — there's no login yet to require. Guarded instead by
    "only works once": the moment an admin exists, this refuses, so nobody
    else on the LAN can hijack an instance after the fact.
    """
    if auth_store.is_admin_configured():
        raise HTTPException(status_code=403, detail="An admin login already exists for this instance")
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or len(password) < 8:
        raise HTTPException(status_code=400, detail="Username is required and password must be at least 8 characters")
    # The wizard's first choice (Everything / Movie Night only) rides along,
    # so it's saved by the same one-time call. SERVARR_MODE still wins.
    wanted = features.normalize(body.get("mode"))
    if body.get("mode") not in (None, "") and not wanted:
        raise HTTPException(status_code=400, detail='mode must be "full" or "movienight"')
    auth_store.set_admin(username, password)
    await log(f"setup: admin login created for '{username}'")
    if wanted and not features.locked():
        await _save_mode(wanted)
    return JSONResponse({"ok": True, **_mode_info()})


@app.post("/api/setup/mode")
async def api_setup_mode(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    """Switches between Everything ("full") and Movie Night only
    ("movienight"). Refused while SERVARR_MODE sets the mode."""
    wanted = features.normalize(body.get("mode"))
    if not wanted:
        raise HTTPException(status_code=400, detail='mode must be "full" or "movienight"')
    if features.locked():
        if wanted != features.current():
            raise HTTPException(
                status_code=409,
                detail=f"The mode is set by SERVARR_MODE={features.current()} in the container's settings. Change it there, then restart Servarr.",
            )
        return JSONResponse(_mode_info())
    await _save_mode(wanted)
    return JSONResponse(_mode_info())


@app.post("/api/setup/test-discord")
async def api_setup_test_discord(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    token = body.get("token", "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise HTTPException(status_code=400, detail="Discord rejected that bot token. In the Developer Portal, open your app → Bot → Reset Token, and paste the new one.")
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Discord"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Discord"))
    return JSONResponse({"username": f"{data.get('username', 'unknown')}", "application_id": data.get("id", "")})


async def _discord_get(path: str, token: str):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"https://discord.com/api/v10{path}", headers={"Authorization": f"Bot {token}"})
        resp.raise_for_status()
        return resp.json()


_BAD_TOKEN = "Discord rejected that bot token. In the Developer Portal, open your app → Bot → Reset Token, and paste the new one."


async def _discord_me(token: str) -> dict:
    """Who a bot token belongs to (its application id is the bot's user id).
    Raises a 400 with a plain-English reason; never echoes the token."""
    try:
        me = await _discord_get("/users/@me", token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise HTTPException(status_code=400, detail=_BAD_TOKEN)
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Discord"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Discord"))
    return me if isinstance(me, dict) else {}


def _invite_info(application_id: str) -> dict:
    mode = features.current()
    return {
        "invite_url": invite.invite_url(application_id, mode),
        "permissions": invite.permission_names(mode),
    }


@app.post("/api/setup/discord-token")
async def api_setup_discord_token(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    """Checks a bot token with Discord, saves it, and (re)connects the bot.
    The token is kept in the data folder (connections_overrides.json, mode
    0600), is only ever shown back masked, and isn't in this response."""
    token = body.get("token")
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        raise HTTPException(status_code=400, detail="Paste the bot token first.")
    if len(token) > 200 or any(ch.isspace() for ch in token):
        raise HTTPException(status_code=400, detail="That doesn't look like a bot token. Copy it again from the Developer Portal (your app → Bot → Reset Token).")
    me = await _discord_me(token)
    if me.get("bot") is False:
        raise HTTPException(status_code=400, detail="That token belongs to a person, not a bot. Use the token from your app's Bot tab in the Developer Portal.")
    changed = token != settings.discord_bot_token
    connections_store.save_overrides({"discord_bot_token": token})
    await log(f"setup: Discord bot token saved (bot: {me.get('username', 'unknown')})")
    if changed or not bot.status()["connected"]:
        bot.request_restart()
    application_id = str(me.get("id", ""))
    return JSONResponse({"ok": True, "username": me.get("username", "unknown"), "application_id": application_id, **_invite_info(application_id)})


@app.get("/api/setup/invite")
async def api_setup_invite(_: str = Depends(require_login)) -> JSONResponse:
    """The link that adds the bot to a server, asking only for the
    permissions the current mode uses (see invite.py)."""
    if not settings.discord_bot_token:
        raise HTTPException(status_code=400, detail="Save the bot token first.")
    me = await _discord_me(settings.discord_bot_token)
    application_id = str(me.get("id", ""))
    return JSONResponse({"username": me.get("username", "unknown"), "application_id": application_id, **_invite_info(application_id)})


@app.get("/api/setup/bot")
async def api_setup_bot(_: str = Depends(require_login)) -> JSONResponse:
    """Whether the bot is online, for the setup page."""
    return JSONResponse(bot.status())


@app.post("/api/setup/discord-guilds")
async def api_setup_discord_guilds(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    """The servers the bot is in, for the wizard's server dropdown, plus an
    invite link for adding it to one. Uses the token from the body (not yet
    saved) or the saved one."""
    token = (body.get("token") or "").strip() or settings.discord_bot_token
    if not token:
        raise HTTPException(status_code=400, detail="Enter the bot token first.")
    try:
        me = await _discord_get("/users/@me", token)
        guilds = await _discord_get("/users/@me/guilds", token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise HTTPException(status_code=400, detail="Discord rejected that bot token.")
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Discord"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Discord"))
    return JSONResponse({
        "guilds": [{"id": g["id"], "name": g.get("name", g["id"])} for g in guilds],
        **_invite_info(str(me.get("id", ""))),
    })


@app.get("/api/setup/discord-layout")
async def api_setup_discord_layout(guild_id: str = "", _: str = Depends(require_login)) -> JSONResponse:
    """Text channels and roles of the configured server (or of `guild_id`,
    before it's saved), for the wizard's channel/role dropdowns. Read live
    from Discord's API, so it works even before the bot's own connection
    is up."""
    token = settings.discord_bot_token
    guild_id = guild_id.strip() or settings.discord_guild_id
    if not token:
        raise HTTPException(status_code=400, detail="Connect Discord first (step 2).")
    try:
        if not guild_id:
            guilds = await _discord_get("/users/@me/guilds", token)
            if len(guilds) != 1:
                raise HTTPException(status_code=400, detail="Pick your server in the Discord step first.")
            guild_id = guilds[0]["id"]
        if not str(guild_id).isdigit():
            raise HTTPException(status_code=400, detail="The saved server ID isn't valid — pick your server again in the Discord step.")
        channels = await _discord_get(f"/guilds/{guild_id}/channels", token)
        roles = await _discord_get(f"/guilds/{guild_id}/roles", token)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            raise HTTPException(status_code=400, detail="The bot isn't in that server (or can't see it). Invite it using the link in the Discord step.")
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Discord"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Discord"))
    text = sorted((c for c in channels if c.get("type") in (0, 5)), key=lambda c: c.get("position", 0))
    return JSONResponse({
        "channels": [{"id": c["id"], "name": c["name"]} for c in text],
        "roles": [
            {"id": r["id"], "name": r["name"]}
            for r in sorted(roles, key=lambda r: -r.get("position", 0))
            if r["name"] != "@everyone" and not r.get("managed")
        ],
    })


@app.post("/api/setup/test-player")
async def api_setup_test_player(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    """Accepts a movienight pair code (MNP1-...) or a URL (+ optional key).
    Tests it and reports what was found; the wizard saves the result."""
    url = (body.get("url") or "").strip()
    key = (body.get("key") or "").strip()
    public_url = ""
    if url.startswith(mpv_control.PAIR_PREFIX):
        try:
            pair = mpv_control.decode_pair_code(url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        url, key, public_url = pair["url"], pair["key"], pair["public_url"]
    if not url:
        raise HTTPException(status_code=400, detail="Paste the player's pair code, or its address.")
    if not urlparse(url).scheme.startswith("http"):
        raise HTTPException(status_code=400, detail="The player address must start with http:// or https://.")
    key = key or (settings.player_key if url.rstrip("/") == settings.neko_mpv_shim_url else "")
    try:
        found = await mpv_control.test_connection(url, key)
    except mpv_control.PlayerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ET_ParseError:
        raise HTTPException(status_code=400, detail="Something answered at that address, but it isn't a Movie Night player. Check the port.")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "the player"))
    return JSONResponse({**found, "url": url.rstrip("/"), "public_url": public_url})


_COMPANION_NEEDS_FULL = (
    "That's a plain Plex Companion player. Movie Night only mode works with the Movie Night player, "
    "which signs in to Plex itself: paste the pair code from its setup page."
)


@app.post("/api/setup/pair-player")
async def api_setup_pair_player(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    """Tests and saves a player in one go (pair code, or URL + key)."""
    url = (body.get("url") or "").strip()
    key = (body.get("key") or "").strip()
    if url.startswith(mpv_control.PAIR_PREFIX):
        try:
            pair = mpv_control.decode_pair_code(url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        url, key = pair["url"], pair["key"]
    result = await api_setup_test_player({"url": url, "key": key})
    found = json.loads(result.body)
    if features.is_movienight() and found["mode"] != "movienight":
        raise HTTPException(status_code=400, detail=_COMPANION_NEEDS_FULL)
    update = {"neko_mpv_shim_url": found["url"], "player_mode": found["mode"]}
    if key:
        update["player_key"] = key
    connections_store.save_overrides(update)
    connections_store.mark_paired_by_hand()
    await mpv_control.refresh_links(force=True)
    bot.request_panel_refresh()
    return JSONResponse({
        "ok": True, "mode": found["mode"], "name": found["name"], "ready": found["ready"],
        "url": found["url"], "edition": found.get("edition"),
    })


@app.get("/api/setup/player")
async def api_setup_player(_: str = Depends(require_login)) -> JSONResponse:
    """How the Movie Night player is paired. `auto` is true when the pairing
    came from the pairing file a bundled player shares (MOVIENIGHT_PAIRING_FILE),
    so the wizard can say "Paired automatically" instead of asking for a code.
    Never returns the key."""
    if connections_store.auto_pair_from_file():
        await mpv_control.refresh_links(force=True)
        await log(f"servarr: paired with the Movie Night player at {settings.neko_mpv_shim_url} (from its pairing file)")
        bot.request_panel_refresh()
    shared = mpv_control.find_pair_file()
    paired = mpv_control.is_configured()
    info = {
        "paired": paired,
        "auto": bool(paired and shared and connections_store.paired_from_file(shared["key"])),
        "pairing_file_found": bool(shared),
        "url": settings.neko_mpv_shim_url if paired else "",
        "mode": mpv_control.mode() if paired else None,
        "name": None,
        "ready": None,
        "edition": None,
        "error": None,
    }
    if settings.neko_mpv_shim_url and not paired:
        info["error"] = _COMPANION_NEEDS_FULL
    if paired:
        try:
            found = await mpv_control.test_connection(settings.neko_mpv_shim_url, settings.player_key)
            info.update(name=found["name"], ready=found["ready"], edition=found.get("edition"))
        except mpv_control.PlayerError as exc:
            info["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            info["error"] = friendly.explain(exc, "the player")
    return JSONResponse(info)


@app.post("/api/setup/movienight-server")
async def api_setup_movienight_server(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    """The short wizard's "server and channel" step: saves the Discord
    server, the Movie Night channel, and optionally the DJ role and the
    private DJ control channel, after checking them against Discord."""
    guild_id = str(body.get("guild_id") or "").strip()
    channel_id = str(body.get("channel_id") or "").strip()
    dj_role_id = str(body.get("dj_role_id") or "").strip()
    control_channel = str(body.get("control_channel") or "").strip()
    token = settings.discord_bot_token
    if not token:
        raise HTTPException(status_code=400, detail="Save the bot token first.")
    if not guild_id.isdigit():
        raise HTTPException(status_code=400, detail="Pick your server.")
    if not channel_id.isdigit():
        raise HTTPException(status_code=400, detail="Pick the Movie Night channel.")
    if dj_role_id and not dj_role_id.isdigit():
        raise HTTPException(status_code=400, detail="Pick the DJ role from the list.")
    try:
        channels = await _discord_get(f"/guilds/{guild_id}/channels", token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            raise HTTPException(status_code=400, detail="The bot isn't in that server yet. Go back a step and use the invite link.")
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Discord"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Discord"))
    text_channels = {str(c["id"]): c.get("name", "") for c in channels if c.get("type") in (0, 5)}
    if channel_id not in text_channels:
        raise HTTPException(status_code=400, detail="That isn't a text channel in the server you picked. Pick one from the list.")
    if control_channel and control_channel not in text_channels.values():
        raise HTTPException(status_code=400, detail="The DJ control channel isn't a text channel in that server. Pick one from the list, or None.")
    guild_changed = guild_id != settings.discord_guild_id
    connections_store.save_overrides({"discord_guild_id": guild_id})
    settings_store.save_overrides({
        "movie_night_channel_id": channel_id,
        "movie_night_dj_role_id": dj_role_id,
        "movie_night_control_channel": control_channel,
    })
    await log(f"setup: Movie Night channel set to #{text_channels[channel_id]}")
    if guild_changed:
        bot.request_resync()
    bot.request_panel_refresh()
    return JSONResponse({
        "ok": True,
        "channel": text_channels[channel_id],
        # Permissions the bot lacks in that channel; null while it's still connecting.
        "missing_permissions": bot.channel_problems(channel_id),
    })


@app.post("/api/setup/test-radarr")
async def api_setup_test_radarr(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    _full_only("Radarr")
    try:
        result = await radarr.test_connection(body.get("url", ""), body.get("api_key", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Radarr"))
    return JSONResponse(result)


@app.post("/api/setup/test-sonarr")
async def api_setup_test_sonarr(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    _full_only("Sonarr")
    try:
        result = await sonarr.test_connection(body.get("url", ""), body.get("api_key", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Sonarr"))
    return JSONResponse(result)


@app.post("/api/setup/test-tautulli")
async def api_setup_test_tautulli(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    _full_only("Tautulli")
    try:
        result = await tautulli.test_connection(body.get("url", ""), body.get("api_key", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Tautulli"))
    return JSONResponse(result)


@app.post("/api/setup/test-plex")
async def api_setup_test_plex(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    _full_only("Plex")
    try:
        result = await plex.test_connection(body.get("url", ""), body.get("token", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Plex"))
    return JSONResponse(result)


@app.post("/api/setup/test-reclaimarr")
async def api_setup_test_reclaimarr(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    _full_only("Reclaimarr")
    try:
        result = await reclaimarr_client.test_connection(body.get("url", ""), body.get("auth_user", ""), body.get("auth_pass", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Reclaimarr"))
    return JSONResponse(result)


def _connection_keys():
    """None (everything) in full mode; Movie Night only mode shows and
    accepts just the Discord and player fields."""
    return features.MOVIE_NIGHT_CONNECTIONS if features.is_movienight() else None


def _setting_keys():
    return features.MOVIE_NIGHT_SETTINGS if features.is_movienight() else None


@app.get("/api/connections")
async def api_get_connections(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(connections_store.current_display(_connection_keys()))


@app.post("/api/connections")
async def api_save_connections(update: dict, _: str = Depends(require_login)) -> JSONResponse:
    unknown = [k for k in update if k not in connections_store.ALL_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown connection field(s): {unknown}")
    refused = features.refused_keys(update, features.MOVIE_NIGHT_CONNECTIONS)
    if refused:
        raise HTTPException(status_code=409, detail=features.refusal(f"Saving {', '.join(refused)}"))
    if features.is_movienight() and str(update.get("player_mode") or "").strip().lower() == "companion":
        raise HTTPException(status_code=409, detail=_COMPANION_NEEDS_FULL)
    new_token = update.get("discord_bot_token")
    token_changed = bool(new_token) and new_token != settings.discord_bot_token
    guild_changed = "discord_guild_id" in update and update["discord_guild_id"] != settings.discord_guild_id
    connections_store.save_overrides(update)
    await mpv_control.refresh_links(force=True)
    await log("connections: settings updated by user")
    if token_changed:
        bot.request_restart()  # connect (or reconnect) with the new token right away
    elif guild_changed:
        bot.request_resync()  # slash commands move to the newly picked server
    return JSONResponse(connections_store.current_display(_connection_keys()))


@app.get("/api/settings")
async def api_get_settings(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(settings_store.current_editable(_setting_keys()))


@app.post("/api/settings")
async def api_update_settings(update: dict, _: str = Depends(require_login)) -> JSONResponse:
    unknown = [k for k in update if k not in settings_store.EDITABLE_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown/non-editable setting(s): {unknown}")
    refused = features.refused_keys(update, features.MOVIE_NIGHT_SETTINGS)
    if refused:
        raise HTTPException(status_code=409, detail=features.refusal(f"Saving {', '.join(refused)}"))
    try:
        settings_store.validate(update)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    panel_moved = "movie_night_control_channel" in update and update["movie_night_control_channel"] != settings.movie_night_control_channel
    settings_store.save_overrides(update)
    if panel_moved:
        bot.request_panel_refresh()
    return JSONResponse(settings_store.current_editable(_setting_keys()))


@app.get("/mpv-remote", response_class=HTMLResponse)
async def mpv_remote_page(request: Request) -> Response:
    """A minimal always-available transport-control page for the Movie
    Night player, which has no on-screen UI of its own. Meant to be kept
    open in a small second window/tab alongside the movie; both it and the
    Discord commands (/mpv-*) call the same mpv_control.py functions.

    DJs open it from a link carrying the key in the URL fragment
    (/mpv-remote#key=...). Fragments are never sent to the server, so the
    page itself holds no secret and is served without auth when a remote
    key is configured; its JS moves the key into an X-Remote-Key header,
    calls POST /api/mpv/login to set an HttpOnly cookie for later visits,
    and strips the key from the address bar. Every API call it makes is
    still authenticated. With no MPV_REMOTE_KEY set, the page needs the
    admin login like the rest of the UI.
    """
    if not settings.mpv_remote_key:
        credentials = await security(request)
        check_credentials(request, credentials)
    return FileResponse(os.path.join(STATIC_DIR, "mpv-remote.html"), headers=_NO_CACHE_HEADERS)


@app.post("/api/mpv/login")
async def api_mpv_login(request: Request, body: dict) -> JSONResponse:
    """Exchanges the remote key for an HttpOnly cookie (30 days), so a
    bookmarked /mpv-remote keeps working without the key in the URL."""
    key = body.get("key") if isinstance(body.get("key"), str) else ""
    if not settings.mpv_remote_key or not check_remote_key(request, key):
        raise HTTPException(status_code=401, detail="Invalid remote key")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        MPV_REMOTE_COOKIE,
        key,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


def _require_player() -> None:
    if not mpv_control.is_configured():
        raise HTTPException(status_code=503, detail="No Movie Night player is paired yet — pair one in /setup (Movie Night step).")


async def _from_player(func, *args, **kwargs):
    """Runs a player_library call for the web remote (Movie Night only mode),
    turning the player's errors into HTTP ones."""
    _require_player()
    try:
        return await func(*args, **kwargs)
    except mpv_control.PlayerError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=friendly.explain(exc, "the player"))


def _plex_key(value: str, name: str = "rating_key") -> str:
    """Plex rating/section keys are numeric; rejecting anything else keeps
    caller input from steering the proxied Plex/player request paths."""
    value = str(value or "").strip()
    if not value.isdigit():
        raise HTTPException(status_code=400, detail=f"{name} must be numeric")
    return value


@app.get("/api/mpv/status")
async def api_mpv_status(_: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    return JSONResponse(await mpv_control.get_status())


@app.post("/api/mpv/play")
async def api_mpv_play(body: dict, _: str = Depends(require_login_or_remote_key), __: None = Depends(_require_player)) -> JSONResponse:
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    year = body.get("year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise HTTPException(status_code=400, detail="year must be an integer")
    started = await mpv_control.play(title[:200], year)
    if not started:
        raise HTTPException(status_code=404, detail=f"Couldn't find '{title}' in the library")
    return JSONResponse({"started": True})


@app.get("/api/mpv/search")
async def api_mpv_search(q: str, _: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    q = q[:200]
    """Real search results to manually pick from (movies AND shows) — used
    by the /mpv-remote browse UI. Unlike /api/mpv/play, this never guesses;
    the caller always picks an exact item and hits /api/mpv/play-item.
    """
    if not q.strip():
        return JSONResponse([])
    if features.is_movienight():
        return JSONResponse(await _from_player(player_library.search, q))
    return JSONResponse(await plex.search_library(q))


@app.get("/api/mpv/children/{rating_key}")
async def api_mpv_children(rating_key: str, _: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    """A show's seasons, or a season's episodes — same Plex /children shape
    either way, so the browse UI calls this twice to drill all the way down
    to a specific episode.
    """
    if features.is_movienight():
        return JSONResponse(await _from_player(player_library.children, _plex_key(rating_key)))
    return JSONResponse(await plex.get_show_children(_plex_key(rating_key)))


@app.get("/api/mpv/poster/{rating_key}")
async def api_mpv_poster(rating_key: str, _: str = Depends(require_login_or_remote_key)) -> Response:
    """Proxies a poster/thumbnail through Servarr instead of linking to
    Plex directly — see plex._poster_url for why (breaks from outside the
    house, exposes the Plex token in page HTML otherwise). A plain <img>
    tag sends the cookie automatically; the page also fetches posters with
    the X-Remote-Key header where cookies are blocked (iframes on Safari).
    """
    if features.is_movienight():
        image_bytes, content_type = await _from_player(player_library.poster, _plex_key(rating_key))
    else:
        image_bytes, content_type = await plex.get_poster_bytes(_plex_key(rating_key))
    return Response(content=image_bytes, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/mpv/sections")
async def api_mpv_sections(_: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    if features.is_movienight():
        return JSONResponse(await _from_player(player_library.sections))
    return JSONResponse(await plex.list_sections())


@app.get("/api/mpv/browse/{section_key}")
async def api_mpv_browse(section_key: str, offset: int = 0, _: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    if features.is_movienight():
        return JSONResponse(await _from_player(player_library.browse, _plex_key(section_key, "section_key"), offset=max(0, offset)))
    return JSONResponse(await plex.browse_section(_plex_key(section_key, "section_key"), offset=max(0, offset)))


@app.post("/api/mpv/play-item")
async def api_mpv_play_item(body: dict, _: str = Depends(require_login_or_remote_key), __: None = Depends(_require_player)) -> JSONResponse:
    rating_key = _plex_key(body.get("rating_key"))
    try:
        offset_ms = max(0, int(body.get("offset_ms", 0)))
    except (TypeError, ValueError):
        offset_ms = 0
    started = await mpv_control.play_by_rating_key(rating_key, offset_ms)
    return JSONResponse({"started": started})


@app.get("/api/mpv/on-deck")
async def api_mpv_on_deck(_: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    if features.is_movienight():
        return JSONResponse(await _from_player(player_library.on_deck))
    return JSONResponse(await plex.get_on_deck())


@app.post("/api/mpv/pause")
async def api_mpv_pause(_: str = Depends(require_login_or_remote_key), __: None = Depends(_require_player)) -> JSONResponse:
    ok = await mpv_control.pause()
    return JSONResponse({"ok": ok})


@app.post("/api/mpv/resume")
async def api_mpv_resume(_: str = Depends(require_login_or_remote_key), __: None = Depends(_require_player)) -> JSONResponse:
    ok = await mpv_control.resume()
    return JSONResponse({"ok": ok})


@app.post("/api/mpv/stop")
async def api_mpv_stop(_: str = Depends(require_login_or_remote_key), __: None = Depends(_require_player)) -> JSONResponse:
    ok = await mpv_control.stop()
    return JSONResponse({"ok": ok})


@app.post("/api/mpv/seek")
async def api_mpv_seek(body: dict, _: str = Depends(require_login_or_remote_key), __: None = Depends(_require_player)) -> JSONResponse:
    try:
        delta = int(body.get("delta_seconds", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="delta_seconds must be an integer")
    if abs(delta) > 24 * 3600:
        raise HTTPException(status_code=400, detail="delta_seconds is out of range")
    position = await mpv_control.seek(delta)
    if position is None:
        raise HTTPException(status_code=409, detail="Nothing is currently loaded")
    return JSONResponse({"position_seconds": position})


@app.post("/api/movie-night/force")
async def api_force_movie_night(_: str = Depends(require_login)) -> dict:
    """Manual trigger for testing — tallies the current vote (or picks one
    random downloaded movie if none is open) and runs the full announce +
    Neko playback flow immediately, in whichever guild the bot is in.
    """
    guild = bot.target_guild()
    if guild is None:
        raise HTTPException(status_code=503, detail="bot not connected to any guild yet")
    started = await movie_night.force_winner_now(guild)
    return {"started": started}


@app.post("/api/release-digest/force")
async def api_force_release_digest(_: str = Depends(require_login)) -> dict:
    """Manual trigger (admin login required) — posts the calendar digest to
    #movie-releases, #tv-releases and #anime-releases immediately, same
    pattern as /api/movie-night/force above. Independent of daily_loop's own
    once-a-day schedule.
    """
    _full_only("The release calendar")
    guild = bot.target_guild()
    if guild is None:
        raise HTTPException(status_code=503, detail="bot not connected to any guild yet")
    await release_digest.post_once(guild)
    return {"posted": True}
