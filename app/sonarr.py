from dataclasses import dataclass
from typing import Optional

import httpx

from .config import settings
from .logger import with_retry


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.sonarr_url}/api/v3",
        timeout=30,
        headers={"X-Api-Key": settings.sonarr_api_key},
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
class CalendarEpisode:
    series_title: str
    season_number: int
    episode_number: int
    episode_title: str
    air_date_utc: str
    network: str
    poster_url: Optional[str]
    has_file: bool
    is_anime: bool


@with_retry(label="Sonarr: get calendar")
async def get_calendar(start_date: str, end_date: str) -> list[CalendarEpisode]:
    async with _client() as client:
        resp = await client.get(
            "/calendar",
            params={"start": start_date, "end": end_date, "includeSeries": "true"},
        )
        resp.raise_for_status()
        data = resp.json()
    results = []
    for e in data:
        series = e.get("series") or {}
        results.append(
            CalendarEpisode(
                series_title=series.get("title", "Unknown series"),
                season_number=e.get("seasonNumber", 0),
                episode_number=e.get("episodeNumber", 0),
                episode_title=e.get("title", ""),
                air_date_utc=e.get("airDateUtc", ""),
                network=series.get("network", "") or "",
                poster_url=_poster_from_raw(series),
                has_file=bool(e.get("hasFile")),
                is_anime=(series.get("seriesType", "") == "anime"),
            )
        )
    return results
