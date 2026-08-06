import re
from typing import Optional

import httpx

from .config import settings
from .logger import with_retry

_JSON_HEADERS = {"Accept": "application/json"}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.plex_url, timeout=15, headers=_JSON_HEADERS)


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


@with_retry(label="Plex: get machine identifier")
async def get_machine_identifier() -> Optional[str]:
    async with _client() as client:
        resp = await client.get("/", params={"X-Plex-Token": settings.plex_token})
        resp.raise_for_status()
        data = resp.json()
    return data.get("MediaContainer", {}).get("machineIdentifier")


async def _tmdb_id_of(client: httpx.AsyncClient, rating_key: str) -> Optional[int]:
    resp = await client.get(f"/library/metadata/{rating_key}", params={"X-Plex-Token": settings.plex_token})
    resp.raise_for_status()
    metadata = resp.json().get("MediaContainer", {}).get("Metadata", [])
    if not metadata:
        return None
    for guid_entry in metadata[0].get("Guid", []) or []:
        match = re.search(r"tmdb://(\d+)", guid_entry.get("id", ""))
        if match:
            return int(match.group(1))
    return None


@with_retry(label="Plex: find rating key by tmdb id")
async def find_rating_key_by_tmdb_id(title: str, tmdb_id: int) -> Optional[str]:
    """Resolve a Movie Night pick to its Plex ratingKey so Neko can be pointed
    at the right details page. Matches by title search + tmdb id confirmation
    (same pattern already proven in reclaimarr/app/plex.py) since Radarr and
    Plex don't share a common id directly.
    """
    async with _client() as client:
        resp = await client.get("/search", params={"query": title, "X-Plex-Token": settings.plex_token})
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("MediaContainer", {}).get("Metadata", []) or []:
            if item.get("type") != "movie":
                continue
            rating_key = str(item.get("ratingKey", ""))
            if not rating_key:
                continue
            if await _tmdb_id_of(client, rating_key) == tmdb_id:
                return rating_key
    return None
