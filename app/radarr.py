from dataclasses import dataclass
from typing import Optional

import httpx

from .config import settings
from .logger import with_retry


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.radarr_url}/api/v3",
        timeout=30,
        headers={"X-Api-Key": settings.radarr_api_key},
    )


def _poster_from_raw(raw: dict) -> Optional[str]:
    for image in raw.get("images", []):
        if image.get("coverType") == "poster":
            return image.get("remoteUrl") or image.get("url")
    return None


async def test_connection(url: str, api_key: str) -> dict:
    """Ad-hoc connectivity check for the setup wizard, using credentials the
    caller just typed in rather than whatever's already saved — this runs
    BEFORE anything is saved, so it can't reuse _client().
    """
    async with httpx.AsyncClient(base_url=f"{url.rstrip('/')}/api/v3", timeout=10, headers={"X-Api-Key": api_key}) as client:
        status_resp = await client.get("/system/status")
        status_resp.raise_for_status()
        version = status_resp.json().get("version")
    return {"version": version}


@dataclass
class CalendarMovie:
    title: str
    release_date: str  # best available of digital/physical/cinema release
    poster_url: Optional[str]
    has_file: bool


def _best_release_date(raw: dict) -> str:
    for key in ("digitalRelease", "physicalRelease", "inCinemas"):
        if raw.get(key):
            return raw[key]
    return ""


@dataclass
class LibraryMovie:
    id: int
    title: str
    year: int
    poster_url: Optional[str]
    tmdb_id: Optional[int] = None
    # Set when the movie came from the Movie Night player's library (Movie
    # Night only mode): its Plex ratingKey, so it plays that exact item.
    rating_key: Optional[str] = None


@with_retry(label="Radarr: list downloaded movies")
async def list_downloaded_movies() -> list[LibraryMovie]:
    """The candidate pool for Movie Night — only ever picks from what's
    already downloaded, never something that would need to be grabbed first.
    """
    async with _client() as client:
        resp = await client.get("/movie")
        resp.raise_for_status()
        data = resp.json()
    return [
        LibraryMovie(
            id=m["id"],
            title=m.get("title", ""),
            year=m.get("year", 0),
            poster_url=_poster_from_raw(m),
            tmdb_id=m.get("tmdbId"),
        )
        for m in data
        if m.get("hasFile")
    ]


@with_retry(label="Radarr: get calendar")
async def get_calendar(start_date: str, end_date: str) -> list[CalendarMovie]:
    async with _client() as client:
        resp = await client.get("/calendar", params={"start": start_date, "end": end_date})
        resp.raise_for_status()
        data = resp.json()
    return [
        CalendarMovie(
            title=m.get("title", ""),
            release_date=_best_release_date(m),
            poster_url=_poster_from_raw(m),
            has_file=bool(m.get("hasFile")),
        )
        for m in data
    ]
