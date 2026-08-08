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
    """A relative URL (proxied through Servarr's own /api/mpv/poster
    endpoint, see get_poster_bytes) rather than a direct Plex URL —
    confirmed live that a direct link (Plex's LAN address, with the token
    embedded in it) breaks entirely from outside the house, and even on
    LAN just needlessly exposes the token in the page's own HTML/network
    tab. Being relative also means it automatically resolves against
    whichever origin the page itself was loaded from (LAN or public),
    with no separate reachability logic needed the way the Playback tab's
    iframe URL requires.
    """
    if not item.get("thumb") or not item.get("ratingKey"):
        return None
    return f"/api/mpv/poster/{item['ratingKey']}"


async def get_poster_bytes(rating_key: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(base_url=settings.plex_url, timeout=15) as client:
        resp = await client.get(
            f"/library/metadata/{rating_key}/thumb",
            params={"X-Plex-Token": settings.plex_token},
        )
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "image/jpeg")


async def get_machine_id() -> str:
    async with _client() as client:
        identity = await client.get("/", params={"X-Plex-Token": settings.plex_token})
        return identity.json()["MediaContainer"]["machineIdentifier"]


async def list_sections() -> list[dict]:
    """Movie/show library sections — kept separate rather than collapsed by
    type, since e.g. "TV Shows" and "Anime" are both type=show but are
    real distinct sections a user would want to browse independently.
    """
    async with _client() as client:
        resp = await client.get("/library/sections", params={"X-Plex-Token": settings.plex_token})
    return [
        {"key": s["key"], "title": s.get("title", ""), "type": s.get("type")}
        for s in resp.json()["MediaContainer"].get("Directory", [])
        if s.get("type") in ("movie", "show")
    ]


async def browse_section(section_key: str, limit: int = 30, offset: int = 0) -> list[dict]:
    """Recently-added browse view for one library section — reuses the
    same cached full-section fetch search_library already relies on
    (see _section_items), so paging through this stays fast/light after
    the first request for a given section instead of hitting Plex again
    on every page.
    """
    async with _client() as client:
        items = await _section_items(client, section_key)
    items = sorted(items, key=lambda c: c.get("addedAt", 0), reverse=True)
    page = items[offset : offset + limit]
    return [
        {
            "rating_key": c["ratingKey"],
            "title": c.get("title", ""),
            "year": c.get("year"),
            "type": c.get("type"),
            "poster_url": _poster_url(c),
        }
        for c in page
    ]


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
