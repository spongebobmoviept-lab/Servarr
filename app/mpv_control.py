"""Movie Night player client.

Two kinds of player are supported, picked automatically when you pair one
in the setup wizard (or set PLAYER_MODE):

* ``movienight`` — the Movie Night player (spongebobmoviept-lab/
  movienight-player). Servarr calls its control API under
  ``/movienight/api/v1`` with the pairing key in an
  ``Authorization: Bearer`` header. The player talks to Plex itself and
  hands back ready-made watch links, so Servarr needs nothing else.
* ``companion`` — any bare Plex Companion HTTP player such as plex-mpv-shim.
  Servarr resolves items in its own Plex connection and sends playMedia.
  The watch page and its passwords are configured separately.

Every function degrades to "nothing playing" / False when no player is
configured, so callers never need to special-case it.
"""

import json
import time
from typing import Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from . import plex
from .config import settings

# Companion API poll calls need a stable X-Plex-Client-Identifier — the shim
# re-registers the same subscriber entry on every call rather than
# accumulating one per request.
_CLIENT_ID = "servarr-discord-bot"
_command_id = 0

MN_PREFIX = "/movienight/api/v1"
PAIR_PREFIX = "MNP1-"

# Watch links reported by a movienight player (GET /links), cached so the
# synchronous viewer_link()/admin_link() helpers can use them.
_links: dict = {}
_links_at = 0.0
_LINKS_TTL = 300


class PlayerError(Exception):
    """The player answered with an error; str() is its own message."""


def is_configured() -> bool:
    """Movie Night's player is optional — every function below degrades to
    "nothing playing"/False when no player URL is set."""
    return bool(settings.neko_mpv_shim_url)


def mode() -> str:
    m = (settings.player_mode or "").strip().lower()
    return m if m in ("movienight", "companion") else "companion"


def _auth_headers(key: Optional[str] = None) -> dict:
    """Server-to-server auth: the pairing key goes in a header, never in a
    URL (URLs end up in logs)."""
    key = settings.player_key if key is None else key
    return {"Authorization": f"Bearer {key}"} if key else {}


# Kept for callers/tests that only need the header dict.
def _headers() -> dict:
    return _auth_headers()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.neko_mpv_shim_url, timeout=10, headers=_auth_headers())


def decode_pair_code(code: str) -> dict:
    """Decodes a movienight pair code ("MNP1-" + base64url JSON with u/k/p)
    into {"url", "key", "public_url"}. Raises ValueError if it isn't one."""
    import base64

    code = (code or "").strip()
    if not code.startswith(PAIR_PREFIX):
        raise ValueError("That isn't a Movie Night pair code (it should start with MNP1-).")
    body = code[len(PAIR_PREFIX):]
    body += "=" * (-len(body) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except Exception:  # noqa: BLE001
        raise ValueError("That pair code is damaged — copy it again from the player's setup page.") from None
    if not isinstance(data, dict) or data.get("v") != 1 or not data.get("u") or not data.get("k"):
        raise ValueError("That pair code is incomplete — copy it again from the player's setup page.")
    return {"url": str(data["u"]).rstrip("/"), "key": str(data["k"]), "public_url": (data.get("p") or "").rstrip("/")}


def load_pair_file(path: str) -> Optional[dict]:
    """Reads the pair.json a movienight player can export into a volume
    shared with Servarr (the bundle's zero-click pairing). None if absent."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("url") or not data.get("api_key"):
        return None
    return {"url": str(data["url"]).rstrip("/"), "key": str(data["api_key"]), "public_url": (data.get("public_url") or "").rstrip("/")}


_DEFAULT_PAIR_FILES = ("/pairing/pair.json", "/pair/pair.json")


def find_pair_file() -> Optional[dict]:
    paths = (settings.player_pair_file,) if settings.player_pair_file else _DEFAULT_PAIR_FILES
    for path in paths:
        data = load_pair_file(path)
        if data:
            return data
    return None


# ---------------------------------------------------------------------------
# movienight player (v1 API)
# ---------------------------------------------------------------------------


async def _mn(method: str, path: str, payload: Optional[dict] = None, params: Optional[dict] = None) -> tuple[int, dict]:
    async with _client() as client:
        resp = await client.request(method, MN_PREFIX + path, json=payload, params=params)
    if resp.status_code in (401, 403):
        # The player may have rotated its key; a bundled player re-exports
        # pair.json, so pick that up once and retry.
        from . import connections_store

        if connections_store.auto_pair_from_file():
            async with _client() as client:
                resp = await client.request(method, MN_PREFIX + path, json=payload, params=params)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code in (401, 403):
        raise PlayerError("the player rejected Servarr's pairing key — pair it again in /setup")
    return resp.status_code, data if isinstance(data, dict) else {}


async def refresh_links(force: bool = False) -> dict:
    global _links, _links_at
    if mode() != "movienight" or not is_configured():
        return {}
    if not force and _links and time.time() - _links_at < _LINKS_TTL:
        return _links
    try:
        status, data = await _mn("GET", "/links")
        if status == 200 and data.get("ok"):
            _links = {k: data.get(k) for k in ("watch_url", "viewer_url", "admin_url")}
            _links_at = time.time()
    except Exception:  # noqa: BLE001 — links are cosmetic; keep the last good ones
        pass
    return _links


def link_origins() -> list[str]:
    out = []
    for url in (_links.get("watch_url"), settings.neko_mpv_public_url, settings.neko_mpv_viewer_url):
        parsed = urlparse(url or "")
        if parsed.scheme and parsed.netloc:
            out.append(f"{parsed.scheme}://{parsed.netloc}")
    return out


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------


def viewer_link() -> str:
    """Public watch-along link for the WATCH LIVE button everyone gets —
    auto-fills the viewer password (?pwd=..., which Neko supports natively)
    so nobody needs to type it. Never the admin password."""
    if mode() == "movienight" and _links:
        return _links.get("viewer_url") or _links.get("watch_url") or ""
    if not settings.neko_mpv_public_url or not settings.neko_mpv_password:
        return settings.neko_mpv_public_url
    return f"{settings.neko_mpv_public_url}/?pwd={settings.neko_mpv_password}"


def admin_link() -> str:
    """Private control link for DJs/mods — auto-fills the watch page's admin
    password. Only ever sent ephemerally, by DM, or in the private control
    channel."""
    if mode() == "movienight" and _links:
        return _links.get("admin_url") or ""
    if not settings.neko_mpv_public_url or not settings.neko_mpv_admin_password:
        return settings.neko_mpv_public_url
    return f"{settings.neko_mpv_public_url}/?pwd={settings.neko_mpv_admin_password}"


# ---------------------------------------------------------------------------
# companion (plex-mpv-shim style) helpers
# ---------------------------------------------------------------------------


def _next_command_id() -> int:
    global _command_id
    _command_id += 1
    return _command_id


async def _get_timeline() -> Optional[dict]:
    """Normalised player state: {"time_ms", "duration_ms", "state"} with
    state "playing"/"paused"/"stopped", or None if nothing's loaded."""
    if not is_configured():
        return None
    if mode() == "movienight":
        status, data = await _mn("GET", "/status")
        if status != 200:
            raise PlayerError(data.get("error") or f"player status failed (HTTP {status})")
        state = data.get("state")
        if state not in ("playing", "paused"):
            return None
        return {"time_ms": int(data.get("position_ms") or 0), "duration_ms": int(data.get("duration_ms") or 0), "state": state}
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
    """Serialisable status for the /mpv-remote page and the buttons.
    {"state": None, ...} when nothing's loaded."""
    timeline = await _get_timeline()
    return timeline or {"state": None, "time_ms": None, "duration_ms": None}


async def _play_media(rating_key: str, machine_id: str, offset_ms: int = 0) -> bool:
    if not is_configured() or not str(rating_key).isdigit():
        return False
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


# ---------------------------------------------------------------------------
# public API used by the bot, Movie Night and the web remote
# ---------------------------------------------------------------------------


async def play(title: str, year: Optional[int] = None) -> bool:
    """Plays a movie by title. The movienight player resolves it in its own
    Plex login; companion mode resolves it in Servarr's Plex connection
    (server-scoped, never an ambiguous cross-source result)."""
    if not is_configured():
        return False
    if mode() == "movienight":
        payload: dict = {"title": title[:200]}
        if year:
            payload["year"] = int(year)
        status, data = await _mn("POST", "/play", payload)
        if status == 404:
            return False
        if status != 200:
            raise PlayerError(data.get("error") or f"play failed (HTTP {status})")
        await refresh_links()
        return True
    item = await plex.resolve_library_item(title, year)
    if not item:
        return False
    return await _play_media(item["rating_key"], item["machine_id"])


async def play_by_rating_key(rating_key: str, offset_ms: int = 0) -> bool:
    """Plays an exact item the caller already picked (search/browse/on-deck
    in the web remote). offset_ms resumes where it was left off."""
    if not is_configured() or not str(rating_key).isdigit():
        return False
    if mode() == "movienight":
        status, data = await _mn("POST", "/play", {"rating_key": str(rating_key), "offset_ms": max(0, int(offset_ms))})
        if status == 404:
            return False
        if status != 200:
            raise PlayerError(data.get("error") or f"play failed (HTTP {status})")
        return True
    machine_id = await plex.get_machine_id()
    return await _play_media(rating_key, machine_id, offset_ms)


async def pause() -> bool:
    """Deterministic pause: checks the real state first instead of toggling
    (the Companion pause/play routes both toggle)."""
    if not is_configured():
        return False
    if mode() == "movienight":
        _, data = await _mn("POST", "/pause")
        return bool(data.get("changed"))
    timeline = await _get_timeline()
    if not timeline or timeline["state"] != "playing":
        return False
    async with _client() as client:
        resp = await client.get("/player/playback/pause")
        resp.raise_for_status()
    return True


async def resume() -> bool:
    if not is_configured():
        return False
    if mode() == "movienight":
        _, data = await _mn("POST", "/resume")
        return bool(data.get("changed"))
    timeline = await _get_timeline()
    if not timeline or timeline["state"] != "paused":
        return False
    async with _client() as client:
        resp = await client.get("/player/playback/play")
        resp.raise_for_status()
    return True


async def stop() -> bool:
    if not is_configured():
        return False
    if mode() == "movienight":
        status, _ = await _mn("POST", "/stop")
        return status == 200
    async with _client() as client:
        resp = await client.get("/player/playback/stop")
        resp.raise_for_status()
    return True


async def seek(delta_seconds: int) -> Optional[int]:
    """Relative seek (e.g. -10 or +10 seconds). Returns the new position in
    seconds, or None if nothing is loaded."""
    if not is_configured():
        return None
    if mode() == "movienight":
        status, data = await _mn("POST", "/seek", {"delta_s": int(delta_seconds)})
        if status == 409:
            return None
        if status != 200:
            raise PlayerError(data.get("error") or f"seek failed (HTTP {status})")
        return int(data.get("position_ms") or 0) // 1000
    # The Companion API's seekTo only takes an absolute offset, so read the
    # current position first.
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


async def test_connection(url: str, key: str) -> dict:
    """Setup-wizard check with credentials that aren't saved yet. Tries the
    movienight API first, then a bare Companion player. Returns
    {"mode": ..., "name": ..., "ready": bool}. Raises on failure (see
    friendly.explain); raises PlayerError for a rejected key."""
    base = url.rstrip("/")
    async with httpx.AsyncClient(base_url=base, timeout=10) as client:
        resp = await client.get(MN_PREFIX + "/pair", headers=_auth_headers(key))
        if resp.status_code in (401, 403):
            raise PlayerError("The player rejected that key. Copy the pair code (or key) again from the player's setup page.")
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if isinstance(data, dict) and data.get("ok"):
                return {"mode": "movienight", "name": data.get("name") or "Movie Night player", "ready": bool(data.get("ready"))}
        resp = await client.get(
            "/player/timeline/poll",
            params={"commandID": _next_command_id()},
            headers={"X-Plex-Client-Identifier": _CLIENT_ID, **_auth_headers(key)},
        )
        resp.raise_for_status()
    ET.fromstring(resp.text)  # must be the Companion XML, not some other web page
    return {"mode": "companion", "name": "Plex Companion player", "ready": True}
