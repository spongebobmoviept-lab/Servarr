import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import (
    auth_store,
    bot,
    connections_store,
    movie_night,
    mpv_control,
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
from .auth import MPV_REMOTE_COOKIE, check_credentials, require_login, require_login_or_remote_key, security
from .config import settings
from .logger import log

_background_tasks: list[asyncio.Task] = []

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
    await xp_store.store.load()

    await log("servarr: starting")
    plex_monitor.set_client(bot.client)
    movie_night.set_client(bot.client)
    release_digest.set_client(bot.client)
    _background_tasks.append(asyncio.create_task(bot.start()))
    _background_tasks.append(asyncio.create_task(plex_monitor.poll_loop()))
    _background_tasks.append(asyncio.create_task(movie_night.vote_loop()))
    _background_tasks.append(asyncio.create_task(movie_night.announce_loop()))
    _background_tasks.append(asyncio.create_task(release_digest.daily_loop()))

    yield
    for task in _background_tasks:
        task.cancel()


app = FastAPI(title="Servarr", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


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
    return FileResponse(os.path.join(STATIC_DIR, "setup.html"), headers=_NO_CACHE_HEADERS)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page() -> FileResponse:
    # Same page serves both first-run setup AND later editing — once setup is
    # complete, the page's own JS asks for login (via a 401 from /api/connections)
    # and switches into an unlocked "edit any step" mode instead of the
    # sequential first-run flow. No separate dashboard to build for that.
    return FileResponse(os.path.join(STATIC_DIR, "setup.html"), headers=_NO_CACHE_HEADERS)


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
        }
    )


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
    auth_store.set_admin(username, password)
    await log(f"setup: admin login created for '{username}'")
    return JSONResponse({"ok": True})


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
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Couldn't connect: {exc}")
    return JSONResponse({"username": f"{data.get('username', 'unknown')}"})


@app.post("/api/setup/test-radarr")
async def api_setup_test_radarr(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await radarr.test_connection(body.get("url", ""), body.get("api_key", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Couldn't connect: {exc}")
    return JSONResponse(result)


@app.post("/api/setup/test-sonarr")
async def api_setup_test_sonarr(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await sonarr.test_connection(body.get("url", ""), body.get("api_key", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Couldn't connect: {exc}")
    return JSONResponse(result)


@app.post("/api/setup/test-tautulli")
async def api_setup_test_tautulli(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await tautulli.test_connection(body.get("url", ""), body.get("api_key", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Couldn't connect: {exc}")
    return JSONResponse(result)


@app.post("/api/setup/test-plex")
async def api_setup_test_plex(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await plex.test_connection(body.get("url", ""), body.get("token", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Couldn't connect: {exc}")
    return JSONResponse(result)


@app.post("/api/setup/test-reclaimarr")
async def api_setup_test_reclaimarr(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await reclaimarr_client.test_connection(body.get("url", ""), body.get("auth_user", ""), body.get("auth_pass", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Couldn't connect: {exc}")
    return JSONResponse(result)


@app.get("/api/connections")
async def api_get_connections(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(connections_store.current_display())


@app.post("/api/connections")
async def api_save_connections(update: dict, _: str = Depends(require_login)) -> JSONResponse:
    unknown = [k for k in update if k not in connections_store.ALL_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown connection field(s): {unknown}")
    result = connections_store.save_overrides(update)
    await log("connections: settings updated by user")
    return JSONResponse(result)


@app.get("/api/settings")
async def api_get_settings(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(settings_store.current_editable())


@app.post("/api/settings")
async def api_update_settings(update: dict, _: str = Depends(require_login)) -> JSONResponse:
    unknown = [k for k in update if k not in settings_store.EDITABLE_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown/non-editable setting(s): {unknown}")
    return JSONResponse(settings_store.save_overrides(update))


@app.get("/mpv-remote", response_class=HTMLResponse)
async def mpv_remote_page(request: Request, key: Optional[str] = None) -> Response:
    """A minimal always-available transport-control page for the neko-mpv
    player — unlike the old Plex-Desktop/Neko browser, plex-mpv-shim has no
    on-screen UI to click at all once NEKO_DESKTOP_INPUT_ENABLED=false, so
    there's otherwise no way to pause/seek/stop once it's playing. Meant to
    be kept open in a small second window/tab alongside the movie, not a
    replacement for the Discord commands (/mpv-*) — both call the same
    mpv_control.py functions.

    Opened via a plain link with ?key=<mpv_remote_key> (see bot.py's
    /mpv-play command) instead of a login popup — confirmed live this was
    real friction (nobody has the Servarr admin password memorized for a
    quick "pause the movie" click). The key gets stashed in a cookie here
    so the page's own fetch() calls Just Work afterwards with no changes
    needed in mpv-remote.html. Visiting without a valid key still falls
    back to the normal admin login, for anyone who bookmarks this directly.
    """
    if key and settings.mpv_remote_key and key == settings.mpv_remote_key:
        response = FileResponse(os.path.join(STATIC_DIR, "mpv-remote.html"), headers=_NO_CACHE_HEADERS)
        response.set_cookie(MPV_REMOTE_COOKIE, key, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
        return response
    if request.cookies.get(MPV_REMOTE_COOKIE) == settings.mpv_remote_key and settings.mpv_remote_key:
        return FileResponse(os.path.join(STATIC_DIR, "mpv-remote.html"), headers=_NO_CACHE_HEADERS)
    credentials = await security(request)
    check_credentials(request, credentials)
    return FileResponse(os.path.join(STATIC_DIR, "mpv-remote.html"), headers=_NO_CACHE_HEADERS)


@app.get("/api/mpv/status")
async def api_mpv_status(_: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    return JSONResponse(await mpv_control.get_status())


@app.post("/api/mpv/play")
async def api_mpv_play(body: dict, _: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    started = await mpv_control.play(title, body.get("year"))
    if not started:
        raise HTTPException(status_code=404, detail=f"Couldn't find '{title}' in the library")
    return JSONResponse({"started": True})


@app.get("/api/mpv/search")
async def api_mpv_search(q: str, _: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    """Real search results to manually pick from (movies AND shows) — used
    by the /mpv-remote browse UI. Unlike /api/mpv/play, this never guesses;
    the caller always picks an exact item and hits /api/mpv/play-item.
    """
    if not q.strip():
        return JSONResponse([])
    return JSONResponse(await plex.search_library(q))


@app.get("/api/mpv/children/{rating_key}")
async def api_mpv_children(rating_key: str, _: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    """A show's seasons, or a season's episodes — same Plex /children shape
    either way, so the browse UI calls this twice to drill all the way down
    to a specific episode.
    """
    return JSONResponse(await plex.get_show_children(rating_key))


@app.get("/api/mpv/sections")
async def api_mpv_sections(_: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    return JSONResponse(await plex.list_sections())


@app.get("/api/mpv/browse/{section_key}")
async def api_mpv_browse(section_key: str, offset: int = 0, _: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    return JSONResponse(await plex.browse_section(section_key, offset=offset))


@app.post("/api/mpv/play-item")
async def api_mpv_play_item(body: dict, _: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    rating_key = str(body.get("rating_key") or "").strip()
    if not rating_key:
        raise HTTPException(status_code=400, detail="rating_key is required")
    try:
        offset_ms = int(body.get("offset_ms", 0))
    except (TypeError, ValueError):
        offset_ms = 0
    started = await mpv_control.play_by_rating_key(rating_key, offset_ms)
    return JSONResponse({"started": started})


@app.get("/api/mpv/on-deck")
async def api_mpv_on_deck(_: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    return JSONResponse(await plex.get_on_deck())


@app.post("/api/mpv/pause")
async def api_mpv_pause(_: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    ok = await mpv_control.pause()
    return JSONResponse({"ok": ok})


@app.post("/api/mpv/resume")
async def api_mpv_resume(_: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    ok = await mpv_control.resume()
    return JSONResponse({"ok": ok})


@app.post("/api/mpv/stop")
async def api_mpv_stop(_: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    ok = await mpv_control.stop()
    return JSONResponse({"ok": ok})


@app.post("/api/mpv/seek")
async def api_mpv_seek(body: dict, _: str = Depends(require_login_or_remote_key)) -> JSONResponse:
    try:
        delta = int(body.get("delta_seconds", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="delta_seconds must be an integer")
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
    if not bot.client.guilds:
        raise HTTPException(status_code=503, detail="bot not connected to any guild yet")
    guild = bot.client.guilds[0]
    started = await movie_night.force_winner_now(guild)
    return {"started": started}
