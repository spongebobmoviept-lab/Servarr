import difflib
import re
import time
from typing import Optional

import httpx

from .config import settings

_JSON_HEADERS = {"Accept": "application/json"}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.plex_url, timeout=15, headers=_JSON_HEADERS)


# Plex's own server-side `?title=` filter on /library/sections/{key}/all is
# a strict substring match — confirmed live it returns ZERO results for
# "wall-e" against the real title "WALL·E" (interpunct character), which
# means fuzzy re-ranking alone can't fix anything: there's nothing to rank
# if the candidate list is already empty. Fetching a whole section (no
# title param at all) and ranking every item ourselves fixes that for good,
# at the cost of a real payload (~3MB for a 1400-movie library, ~0.5s) —
# cached briefly per section so a run of searches (typing a few different
# queries while browsing) only pays that cost once.
_SECTION_CACHE_TTL_SECONDS = 60
_section_cache: dict[str, tuple[float, list[dict]]] = {}


async def _section_items(client: httpx.AsyncClient, section_key: str) -> list[dict]:
    cached = _section_cache.get(section_key)
    now = time.monotonic()
    if cached and now - cached[0] < _SECTION_CACHE_TTL_SECONDS:
        return cached[1]
    resp = await client.get(
        f"/library/sections/{section_key}/all",
        params={"X-Plex-Token": settings.plex_token},
    )
    items = resp.json().get("MediaContainer", {}).get("Metadata", []) or []
    _section_cache[section_key] = (now, items)
    return items


def _normalize(s: str) -> str:
    """Strips everything but letters/digits — makes "wall-e" and "WALL·E"
    (interpunct character) compare identically, along with the usual
    "The "/colon/apostrophe noise in titles generally.
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _similarity(query: str, title: str) -> float:
    """Ranks candidates by closeness to what was actually typed. A
    punctuation-normalized substring match (handles "wall-e" -> "WALL·E",
    and any partial-title search) always wins outright; difflib's
    SequenceMatcher (same approach already proven in
    requestarr/app/bot.py's _similarity(), stdlib only) is the fallback for
    genuine typos that aren't a clean substring either way.
    """
    normalized_query, normalized_title = _normalize(query), _normalize(title)
    if normalized_query and normalized_query in normalized_title:
        return 1.0
    return difflib.SequenceMatcher(None, query.strip().lower(), title.strip().lower()).ratio()


# Applies only to the difflib fallback above (a substring hit is always
# 1.0, well clear of this) — below this, a "best match" is more likely
# noise than a real typo. E.g. a transposed-letter typo still scores
# comfortably above this; two barely-related titles usually land well under.
_MIN_SIMILARITY = 0.55


def _poster_url(item: dict) -> Optional[str]:
    thumb = item.get("thumb")
    if not thumb:
        return None
    return f"{settings.plex_url}{thumb}?X-Plex-Token={settings.plex_token}"


async def get_machine_id() -> str:
    async with _client() as client:
        identity = await client.get("/", params={"X-Plex-Token": settings.plex_token})
        return identity.json()["MediaContainer"]["machineIdentifier"]


async def resolve_library_item(title: str, year: Optional[int] = None) -> Optional[dict]:
    """Resolves a title to its exact item in one of OUR OWN movie library
    sections — used so playback automation never has to guess between our
    own copy and an ambiguous cross-source "Discover" result (confirmed
    live: Plex's blended global search puts an unlabeled Discover entry on
    top for popular titles like "The Matrix", with no way to tell it apart
    from the library copy except by clicking through). Returns None if
    nothing matches. Movies-only, single best guess — this is what
    Discord's /mpv-play and /play-movie use, where there's no UI to pick
    from multiple candidates; see search_library for the browsable version.
    """
    async with _client() as client:
        params = {"X-Plex-Token": settings.plex_token}
        identity = await client.get("/", params=params)
        machine_id = identity.json()["MediaContainer"]["machineIdentifier"]

        sections = await client.get("/library/sections", params=params)
        movie_section_keys = [
            s["key"] for s in sections.json()["MediaContainer"].get("Directory", []) if s.get("type") == "movie"
        ]

        candidates: list[dict] = []
        for section_key in movie_section_keys:
            candidates.extend(await _section_items(client, section_key))

    if not candidates:
        return None
    pool = [c for c in candidates if c.get("year") == year] if year else candidates
    pool = pool or candidates
    item = max(pool, key=lambda c: _similarity(title, c.get("title", "")))
    if _similarity(title, item.get("title", "")) < _MIN_SIMILARITY:
        return None
    return {"machine_id": machine_id, "rating_key": item["ratingKey"], "title": item["title"]}


async def search_library(query: str, kinds: tuple[str, ...] = ("movie", "show"), limit: int = 15) -> list[dict]:
    """Real search results to manually pick from — used by the /mpv-remote
    browse UI, unlike resolve_library_item's single best-guess. Spans both
    movie and show library sections, ranked by the same similarity scoring
    so typos/near-matches still surface near the top instead of being lost
    below the truncation point.
    """
    async with _client() as client:
        params = {"X-Plex-Token": settings.plex_token}
        sections = await client.get("/library/sections", params=params)
        section_keys = [
            s["key"] for s in sections.json()["MediaContainer"].get("Directory", []) if s.get("type") in kinds
        ]

        candidates: list[dict] = []
        for section_key in section_keys:
            candidates.extend(await _section_items(client, section_key))

    scored = [(c, _similarity(query, c.get("title", ""))) for c in candidates]
    scored = [(c, score) for c, score in scored if score >= _MIN_SIMILARITY]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    candidates = [c for c, _ in scored]
    return [
        {
            "rating_key": c["ratingKey"],
            "title": c.get("title", ""),
            "year": c.get("year"),
            "type": c.get("type"),
            "poster_url": _poster_url(c),
        }
        for c in candidates[:limit]
    ]


async def get_show_children(rating_key: str) -> list[dict]:
    """Lists a show's seasons, or a season's episodes — Plex's own
    /children endpoint serves both the same way, so this one function
    covers both drill-down steps the browse UI needs.
    """
    async with _client() as client:
        resp = await client.get(
            f"/library/metadata/{rating_key}/children",
            params={"X-Plex-Token": settings.plex_token},
        )
        resp.raise_for_status()
        data = resp.json()
    return [
        {
            "rating_key": c["ratingKey"],
            "title": c.get("title", ""),
            "index": c.get("index"),
            "type": c.get("type"),
            "poster_url": _poster_url(c),
        }
        for c in data.get("MediaContainer", {}).get("Metadata", []) or []
    ]


async def get_on_deck(limit: int = 10) -> list[dict]:
    """Plex's own "Continue Watching" list — genuinely in-progress movies,
    or the next unwatched episode for an in-progress show, server-wide
    across every device (not just neko-mpv), which is exactly the point:
    resume something you started watching anywhere. Returns viewOffset so
    the caller can actually resume at the right spot rather than
    restarting from 0.
    """
    async with _client() as client:
        resp = await client.get("/library/onDeck", params={"X-Plex-Token": settings.plex_token})
        resp.raise_for_status()
        data = resp.json()
    items = data.get("MediaContainer", {}).get("Metadata", []) or []
    results = []
    for c in items[:limit]:
        if c.get("type") == "episode":
            # Plain ASCII separator on purpose — a non-ASCII dash here got
            # corrupted somewhere in the Windows-authoring/scp-to-Linux
            # pipeline (confirmed live: server sent invalid UTF-8 bytes for
            # it), so this avoids that whole class of bug rather than
            # chasing the exact encoding step that mangled it.
            title = f"{c.get('grandparentTitle', '')} - S{c.get('parentIndex', '?')}E{c.get('index', '?')}: {c.get('title', '')}"
        else:
            title = c.get("title", "")
        results.append(
            {
                "rating_key": c["ratingKey"],
                "title": title,
                "year": c.get("year"),
                "type": c.get("type"),
                "poster_url": _poster_url(c),
                "view_offset_ms": c.get("viewOffset", 0),
                "duration_ms": c.get("duration", 0),
            }
        )
    return results


async def is_title_playing(title: str) -> bool:
    """Ground truth for whether a specific title actually started playing —
    used right after the automation clicks Play, since the click itself
    could silently land wrong."""
    async with _client() as client:
        resp = await client.get("/status/sessions", params={"X-Plex-Token": settings.plex_token})
        resp.raise_for_status()
        data = resp.json()
    for item in data.get("MediaContainer", {}).get("Metadata", []) or []:
        if item.get("title") == title:
            return True
    return False


async def terminate_client_sessions(machine_identifier: str) -> None:
    """Explicitly ends any session belonging to our own Plex Desktop client
    (identified by its own persisted clientID, read from plex.ini) rather
    than trusting it to time out on its own — forcibly restarting that
    client (see neko_control's debug-port toggle) kills the player process
    abruptly without ever telling Plex the session ended, and confirmed
    live this can leave a duplicate/orphaned entry in /status/sessions.
    Called before and after every automated playback so nothing lingers
    regardless of how the previous run ended. Never touches other people's
    real sessions (Apple TV, phones, etc.) — only ones matching our own
    client's machine identifier.
    """
    async with _client() as client:
        resp = await client.get("/status/sessions", params={"X-Plex-Token": settings.plex_token})
        resp.raise_for_status()
        sessions = resp.json().get("MediaContainer", {}).get("Metadata", []) or []
        for item in sessions:
            player = item.get("Player") or {}
            if player.get("machineIdentifier") != machine_identifier:
                continue
            session_id = (item.get("Session") or {}).get("id")
            if not session_id:
                continue
            await client.get(
                "/status/sessions/terminate",
                params={
                    "sessionId": session_id,
                    "reason": "Movie Night automation restarting the player",
                    "X-Plex-Token": settings.plex_token,
                },
            )


async def test_connection(url: str, token: str) -> dict:
    """Ad-hoc connectivity check for the setup wizard, using credentials the
    caller just typed in rather than whatever's already saved."""
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=10, headers=_JSON_HEADERS) as client:
        resp = await client.get("/library/sections", params={"X-Plex-Token": token})
        resp.raise_for_status()
        data = resp.json()
    sections = data.get("MediaContainer", {}).get("Directory", [])
    movie_libraries = [s.get("title") for s in sections if s.get("type") == "movie"]
    return {"library_count": len(sections), "movie_libraries": movie_libraries}


async def is_client_playing(machine_identifier: str) -> bool:
    """Checked right after neko_control clicks resume — ground truth from
    Plex's own session list is the only reliable way to know the click
    actually worked, since a blind retry could just as easily toggle a
    genuinely-already-playing session back into paused.

    Checks for a positive "at least one session is playing" signal rather
    than "is any session paused", and deliberately doesn't require there to
    be exactly one session for our client — forcibly restarting the player
    process (see _restart_plex_desktop) can leave duplicate/zombie entries
    behind that _cleanup_stale_sessions doesn't always catch (some have no
    Session.id at all, which Plex's own /status/sessions/terminate has no
    way to target), confirmed live. Checking for "paused" instead used to
    false-negative forever whenever one of those zombies happened to still
    say paused, even though the real session had already started fine.
    """
    async with _client() as client:
        resp = await client.get("/status/sessions", params={"X-Plex-Token": settings.plex_token})
        resp.raise_for_status()
        data = resp.json()
    for item in data.get("MediaContainer", {}).get("Metadata", []) or []:
        player = item.get("Player") or {}
        if player.get("machineIdentifier") == machine_identifier and player.get("state") == "playing":
            return True
    return False
