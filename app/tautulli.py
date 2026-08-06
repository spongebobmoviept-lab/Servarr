from dataclasses import dataclass

import httpx

from .config import settings
from .logger import with_retry


async def test_connection(url: str, api_key: str) -> dict:
    """Ad-hoc connectivity check for the setup wizard, using credentials the
    caller just typed in rather than whatever's already saved."""
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=10) as client:
        resp = await client.get("/api/v2", params={"apikey": api_key, "cmd": "status"})
        resp.raise_for_status()
        data = resp.json()
    if data.get("response", {}).get("result") != "success":
        raise ValueError(data.get("response", {}).get("message") or "Tautulli reported an error")
    return {"ok": True}


@dataclass
class PlexSession:
    user: str
    title: str
    year: str
    state: str  # playing / paused / buffering
    progress_percent: int
    player: str
    decision: str  # "Direct Play" or "Transcoding"
    quality: str
    poster_url: str


@with_retry(label="Tautulli: get activity")
async def get_activity() -> list[PlexSession]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.tautulli_url}/api/v2",
            params={"apikey": settings.tautulli_api_key, "cmd": "get_activity"},
        )
        resp.raise_for_status()
        data = resp.json()["response"]["data"]
    sessions = []
    for s in data.get("sessions", []):
        thumb = s.get("thumb") or s.get("grandparent_thumb") or ""
        poster_url = f"{settings.tautulli_url}/pms_image_proxy?img={thumb}&width=300&height=450" if thumb else ""
        sessions.append(
            PlexSession(
                user=s.get("friendly_name") or s.get("user", "Someone"),
                title=s.get("full_title", "something"),
                year=str(s.get("year") or ""),
                state=s.get("state", "playing"),
                progress_percent=int(float(s.get("progress_percent") or 0)),
                player=s.get("player", "a device"),
                decision="Direct Play" if s.get("transcode_decision") == "direct play" else "Transcoding",
                quality=s.get("video_resolution") or s.get("stream_video_resolution") or "",
                poster_url=poster_url,
            )
        )
    return sessions
