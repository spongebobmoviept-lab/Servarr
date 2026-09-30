"""Thin client for the one Reclaimarr endpoint Movie Night needs — pausing
the upgrade-on-play workflow for tonight's pick, so nobody's shared viewing
gets interrupted by a surprise 4K swap. Uses Reclaimarr's own admin login
(HTTP Basic) since it has no separate API-key scheme.
"""

import httpx

from . import features
from .config import settings
from .logger import log


async def test_connection(url: str, auth_user: str, auth_pass: str) -> dict:
    """Ad-hoc connectivity + credentials check for the setup wizard — hits an
    authenticated endpoint (not /health, which is public) since the whole
    point is confirming these exact credentials actually work.
    """
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=10) as client:
        resp = await client.get("/api/status", auth=(auth_user, auth_pass))
        resp.raise_for_status()
    return {"ok": True}


async def pause_upgrade(movie_id: int, minutes: int) -> bool:
    if not settings.reclaimarr_url or features.is_movienight():
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.reclaimarr_url}/api/movies/{movie_id}/pause-upgrade",
                json={"minutes": minutes},
                auth=(settings.reclaimarr_auth_user, settings.reclaimarr_auth_pass),
            )
            resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — movie night still proceeds even if this fails
        await log(f"reclaimarr_client: couldn't pause upgrade for movie {movie_id}: {exc}")
        return False
