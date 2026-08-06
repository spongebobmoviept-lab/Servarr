"""Daily auto-post of what's coming up — so #movie-releases/#tv-releases/
#anime-releases show something without anyone having to remember to type
/upcoming. Posted once a day; /upcoming still works any time for an
on-demand check.
"""

import asyncio
import datetime
from typing import Optional

import discord

from . import calendar_embeds
from .config import settings
from .logger import log
from .radarr import get_calendar as radarr_calendar
from .sonarr import get_calendar as sonarr_calendar

_client: Optional[discord.Client] = None
_DAYS_AHEAD = 14
_POST_HOUR_UTC = 13  # ~9am US Eastern


def set_client(client: discord.Client) -> None:
    global _client
    _client = client


async def post_once(guild: discord.Guild) -> None:
    start = datetime.date.today().isoformat()
    end = (datetime.date.today() + datetime.timedelta(days=_DAYS_AHEAD)).isoformat()
    episodes = await sonarr_calendar(start, end)
    movies = await radarr_calendar(start, end)

    targets = [
        ("movie-releases", calendar_embeds.build_movie_digest(movies, _DAYS_AHEAD)),
        ("tv-releases", calendar_embeds.build_tv_digest(episodes, _DAYS_AHEAD)),
        ("anime-releases", calendar_embeds.build_anime_digest(episodes, _DAYS_AHEAD)),
    ]
    for channel_name, embed in targets:
        if embed is None:
            continue
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if channel is None:
            continue
        try:
            await channel.send(embed=embed)
        except Exception as exc:  # noqa: BLE001
            await log(f"release_digest: couldn't post to #{channel_name}: {exc}")


async def _sleep_until_hour_utc(hour: int) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())


async def daily_loop() -> None:
    while _client is None or not _client.guilds:
        await asyncio.sleep(2)
    while True:
        await _sleep_until_hour_utc(_POST_HOUR_UTC)
        for guild in _client.guilds:
            try:
                await post_once(guild)
            except Exception as exc:  # noqa: BLE001
                await log(f"release_digest: daily post failed: {exc}")
