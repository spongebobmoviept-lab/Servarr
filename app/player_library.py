"""The library, read through the paired Movie Night player.

In Movie Night only mode Servarr has no Plex or Radarr connection of its
own: the player is the one signed in to Plex (possibly a friend's server),
so the vote's candidates, /play-movie's search and the web remote's
browser all come from the player's read-only library API (see the player's
PAIRING.md). Items come back in the same shapes as Servarr's own Plex
client (plex.py), so the web remote works unchanged.
"""

import random
from typing import Optional

from . import mpv_control
from .radarr import LibraryMovie

# The player's limits: search terms up to 100 characters, pages up to 100 items.
_MAX_QUERY = 100
_MAX_PAGE = 100


def _poster_url(item: dict) -> Optional[str]:
    # Proxied through Servarr's /api/mpv/poster (see main.py), like plex.py does.
    return f"/api/mpv/poster/{item['rating_key']}" if item.get("has_poster") else None


def _valid(item) -> bool:
    return isinstance(item, dict) and str(item.get("rating_key", "")).isdigit()


def _remote_item(item: dict) -> dict:
    kind = item.get("type")
    index = item.get("season") if kind == "season" else item.get("episode") if kind == "episode" else None
    return {
        "rating_key": str(item["rating_key"]),
        "title": item.get("title") or "",
        "year": item.get("year"),
        "type": kind,
        "index": index,
        "poster_url": _poster_url(item),
    }


def _library_movie(item: dict) -> LibraryMovie:
    rating_key = str(item["rating_key"])
    year = item.get("year")
    return LibraryMovie(
        id=int(rating_key),
        title=item.get("title") or "",
        year=year if isinstance(year, int) else 0,
        poster_url=None,  # the player's posters need its key; Discord can't fetch them
        rating_key=rating_key,
    )


def _is_movie(item: dict) -> bool:
    return item.get("type") == "movie" and item.get("playable", True) is not False


async def search(query: str, limit: int = 15) -> list[dict]:
    query = query.strip()[:_MAX_QUERY]
    if not query:
        return []
    data = await mpv_control.api_get("/search", {"q": query, "limit": max(1, min(limit, _MAX_PAGE))})
    return [_remote_item(i) for i in data.get("items") or [] if _valid(i)]


async def sections() -> list[dict]:
    data = await mpv_control.api_get("/sections")
    return [
        {"key": str(s.get("key")), "title": s.get("title") or "", "type": s.get("type")}
        for s in data.get("sections") or []
        if isinstance(s, dict) and str(s.get("key", "")).isdigit() and s.get("type") in ("movie", "show")
    ]


async def browse(section_key: str, offset: int = 0, limit: int = 30) -> list[dict]:
    data = await mpv_control.api_get(
        f"/browse/{section_key}", {"offset": max(0, offset), "limit": max(1, min(limit, _MAX_PAGE))}
    )
    return [_remote_item(i) for i in data.get("items") or [] if _valid(i)]


async def children(rating_key: str) -> list[dict]:
    data = await mpv_control.api_get(f"/children/{rating_key}")
    return [_remote_item(i) for i in data.get("items") or [] if _valid(i)]


async def on_deck(limit: int = 10) -> list[dict]:
    data = await mpv_control.api_get("/on-deck")
    results = []
    for item in [i for i in data.get("items") or [] if _valid(i)][:limit]:
        row = _remote_item(item)
        if item.get("type") == "episode" and item.get("show_title"):
            row["title"] = f"{item['show_title']} - S{item.get('season', '?')}E{item.get('episode', '?')}: {item.get('title') or ''}"
        row["view_offset_ms"] = item.get("view_offset_ms") or 0
        row["duration_ms"] = item.get("duration_ms") or 0
        results.append(row)
    return results


async def poster(rating_key: str) -> tuple[bytes, str]:
    return await mpv_control.api_get_bytes(f"/poster/{rating_key}")


async def find_movies(query: str, limit: int = 25) -> list[LibraryMovie]:
    """Movies matching a title, best match first (for /play-movie)."""
    query = query.strip()[:_MAX_QUERY]
    if not query:
        return []
    data = await mpv_control.api_get("/search", {"q": query, "limit": max(1, min(limit, _MAX_PAGE))})
    return [_library_movie(i) for i in data.get("items") or [] if _valid(i) and _is_movie(i)]


async def random_movies(count: int) -> list[LibraryMovie]:
    """Up to `count` different movies picked at random across the movie
    libraries (for the vote). Asks the player for one item at a time at
    random positions, instead of downloading the whole library."""
    if count <= 0:
        return []
    totals: list[tuple[str, int]] = []
    for section in await sections():
        if section["type"] != "movie":
            continue
        page = await mpv_control.api_get(f"/browse/{section['key']}", {"offset": 0, "limit": 1})
        total = page.get("total")
        if isinstance(total, int) and total > 0:
            totals.append((section["key"], total))
    grand_total = sum(total for _, total in totals)
    picks: list[LibraryMovie] = []
    seen: set[str] = set()
    for position in random.sample(range(grand_total), min(count, grand_total)):
        for key, total in totals:
            if position < total:
                page = await mpv_control.api_get(f"/browse/{key}", {"offset": position, "limit": 1})
                for item in page.get("items") or []:
                    if _valid(item) and _is_movie(item) and str(item["rating_key"]) not in seen:
                        seen.add(str(item["rating_key"]))
                        picks.append(_library_movie(item))
                break
            position -= total
    return picks
