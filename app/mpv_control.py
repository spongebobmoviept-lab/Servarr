from typing import Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from . import plex
from .config import settings

# Companion API poll calls need a stable X-Plex-Client-Identifier — the shim
# just re-registers/updates the same subscriber entry on every call rather
# than accumulating one per request (see plex_mpv_shim/client.py:poll), but
# reusing one fixed id keeps that dict from churning pointlessly.
_CLIENT_ID = "servarr-discord-bot"
_command_id = 0


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.neko_mpv_shim_url, timeout=10)


def viewer_link() -> str:
    """Public watch-along link for the WATCH LIVE button everyone gets —
    auto-logs in as a plain viewer (?pwd=...&usr=...), confirmed live to
    be Neko's own real support for this. Deliberately the non-admin
    password, so the native admin-only Playback tab (see
    neko-mpv/neko-client-patch/side.vue) never renders for people who
    just get this link.
    """
    if not settings.neko_mpv_public_url or not settings.neko_mpv_password:
        return settings.neko_mpv_public_url
    return f"{settings.neko_mpv_public_url}/?pwd={settings.neko_mpv_password}&usr=Guest"


def admin_link() -> str:
    """Private control link for whoever started the movie — auto-logs in
    as Neko admin, which is what actually makes the native Playback tab
    appear (gated by real Neko admin status, not a URL param). Replaces
    the old system's admin_viewer_link/CDP approach entirely.
    """
    if not settings.neko_mpv_public_url or not settings.neko_mpv_admin_password:
        return settings.neko_mpv_public_url
    return f"{settings.neko_mpv_public_url}/?pwd={settings.neko_mpv_admin_password}&usr=Admin"


def _next_command_id() -> int:
    global _command_id
    _command_id += 1
    return _command_id


async def _get_timeline() -> Optional[dict]:
    """Polls plex-mpv-shim's own Companion API for the live timeline
    (position/duration/state) — this is a real synchronous GET (no `wait`
    param), not a subscription. Returns None if nothing's loaded at all.
    """
    async with _client() as client:
        resp = await client.get(
            "/player/timeline/poll",
            params={"commandID": _next_command_id()},
            headers={"X-Plex-Client-Identifier": _CLIENT_ID},
        )
        resp.raise_for_status()
    root = ET.fromstring(resp.text)
    timeline = root.find("Timeline")
    if timeline is None or "time" not in timeline.attrib:
        return None
    return {
        "time_ms": int(timeline.get("time", 0)),
        "duration_ms": int(timeline.get("duration", 0)),
        "state": timeline.get("state", "stopped"),
    }


async def get_status() -> dict:
    """Public wrapper around _get_timeline for callers outside this module
    (the /mpv-remote page's status poll, /mpv-pause's own state check
    reuses _get_timeline directly since it's already in this file).
    Returns {"state": None, ...} rather than None when nothing's loaded, so
    callers can serialize it directly.
    """
    timeline = await _get_timeline()
    return timeline or {"state": None, "time_ms": None, "duration_ms": None}


async def _play_media(rating_key: str, machine_id: str, offset_ms: int = 0) -> bool:
    parsed = urlparse(settings.plex_url)
    key = f"/library/metadata/{rating_key}"
    async with _client() as client:
        resp = await client.get(
            "/player/playback/playMedia",
            params={
                "key": key,
                "containerKey": key,
                "offset": offset_ms,
                "machineIdentifier": machine_id,
                "address": parsed.hostname,
                "port": parsed.port or 32400,
                "protocol": parsed.scheme or "http",
                "token": settings.plex_token,
            },
        )
        resp.raise_for_status()
    return True


async def play(title: str, year: Optional[int] = None) -> bool:
    """Resolves the title against our own Plex library (same server-scoped
    resolver neko_control.py uses — never Discover) and hands it straight to
    plex-mpv-shim's Companion API. No mouse, no debug port, no restart —
    unlike the Plex-Desktop/CDP path, this is a real Companion client that
    was built to be driven exactly this way.
    """
    item = await plex.resolve_library_item(title, year)
    if not item:
        return False
    return await _play_media(item["rating_key"], item["machine_id"])


async def play_by_rating_key(rating_key: str, offset_ms: int = 0) -> bool:
    """Plays an exact item the caller already picked from a search/browse
    result (see plex.search_library / plex.get_show_children / plex.get_on_deck)
    — skips title resolution entirely, unlike play(). Works the same for a
    movie or a TV episode: plex-mpv-shim's playMedia handler only ever
    looks at a plain type=video regardless (confirmed in its source), so no
    shim-side changes were needed to support episodes. offset_ms lets the
    on-deck ("continue watching") list actually resume where a title was
    left off, instead of always restarting from 0.
    """
    machine_id = await plex.get_machine_id()
    return await _play_media(rating_key, machine_id, offset_ms)


async def pause() -> bool:
    """Deterministic pause. The shim's own /pause and /play routes both just
    toggle the same internal flag (confirmed in its source — pausePlay()
    calls playerManager.toggle_pause() either way), so this checks the
    actual state first instead of blindly toggling, which would resume
    playback if it was already paused.
    """
    timeline = await _get_timeline()
    if not timeline or timeline["state"] != "playing":
        return False
    async with _client() as client:
        resp = await client.get("/player/playback/pause")
        resp.raise_for_status()
    return True


async def resume() -> bool:
    timeline = await _get_timeline()
    if not timeline or timeline["state"] != "paused":
        return False
    async with _client() as client:
        resp = await client.get("/player/playback/play")
        resp.raise_for_status()
    return True


async def stop() -> bool:
    async with _client() as client:
        resp = await client.get("/player/playback/stop")
        resp.raise_for_status()
    return True


async def seek(delta_seconds: int) -> Optional[int]:
    """Relative seek (e.g. -10 or +10 seconds). The Companion API's seekTo
    only takes an absolute ms offset — stepForward/stepBack exist as routes
    but are literally unimplemented in the shim ('not implemented yet' in
    its source) — so this reads the current position first and computes the
    new one itself. Returns the new position in seconds, or None if nothing
    is currently loaded.
    """
    timeline = await _get_timeline()
    if not timeline:
        return None
    new_ms = timeline["time_ms"] + delta_seconds * 1000
    ceiling = timeline["duration_ms"] or None
    new_ms = max(0, new_ms if ceiling is None else min(new_ms, ceiling))
    async with _client() as client:
        resp = await client.get("/player/playback/seekTo", params={"offset": new_ms})
        resp.raise_for_status()
    return new_ms // 1000
