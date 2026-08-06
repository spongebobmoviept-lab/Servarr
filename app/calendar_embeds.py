"""Formats combined Sonarr+Radarr calendar results into a themed embed for
the /upcoming command — grouped by day, chronological.
"""

import datetime
from collections import defaultdict
from typing import Optional

import discord

from .radarr import CalendarMovie
from .sonarr import CalendarEpisode

GOLD = 0xD4AF37


def _day_key(iso_date: str) -> str:
    return iso_date[:10] if iso_date else "Unknown date"


def _day_label(day_key: str) -> str:
    try:
        d = datetime.date.fromisoformat(day_key)
    except ValueError:
        return day_key
    return d.strftime("%a, %b %d")


def _episode_label(ep: CalendarEpisode) -> str:
    label = f"📺 **{ep.series_title}** S{ep.season_number:02d}E{ep.episode_number:02d}"
    if ep.episode_title:
        label += f" — {ep.episode_title}"
    return label


def build_upcoming_embed(episodes: list[CalendarEpisode], movies: list[CalendarMovie], days: int) -> discord.Embed:
    by_day: dict[str, list[str]] = defaultdict(list)

    for ep in episodes:
        by_day[_day_key(ep.air_date_utc)].append(_episode_label(ep))
    for m in movies:
        by_day[_day_key(m.release_date)].append(f"🎬 **{m.title}**")

    embed = discord.Embed(title=f"📅 Coming up in the next {days} days", color=GOLD)
    if not by_day:
        embed.description = "Nothing scheduled in this window."
        return embed

    for day_key in sorted(by_day.keys()):
        value = "\n".join(by_day[day_key][:15])
        if len(by_day[day_key]) > 15:
            value += f"\n…and {len(by_day[day_key]) - 15} more"
        embed.add_field(name=_day_label(day_key), value=value[:1024], inline=False)

    return embed


def _digest_embed(title: str, lines_by_day: dict[str, list[str]], empty_message: str) -> Optional[discord.Embed]:
    """Used for the daily auto-post per themed channel — returns None when
    there's nothing at all so the digest loop can skip posting an empty
    "nothing happening" message every single day.
    """
    if not lines_by_day:
        return None
    embed = discord.Embed(title=title, color=GOLD)
    for day_key in sorted(lines_by_day.keys()):
        value = "\n".join(lines_by_day[day_key][:15])
        if len(lines_by_day[day_key]) > 15:
            value += f"\n…and {len(lines_by_day[day_key]) - 15} more"
        embed.add_field(name=_day_label(day_key), value=value[:1024], inline=False)
    return embed


def build_movie_digest(movies: list[CalendarMovie], days: int) -> Optional[discord.Embed]:
    by_day: dict[str, list[str]] = defaultdict(list)
    for m in movies:
        by_day[_day_key(m.release_date)].append(f"🎬 **{m.title}**")
    return _digest_embed(f"🎬 Movies coming up in the next {days} days", by_day, "Nothing scheduled.")


def build_tv_digest(episodes: list[CalendarEpisode], days: int) -> Optional[discord.Embed]:
    by_day: dict[str, list[str]] = defaultdict(list)
    for ep in (e for e in episodes if not e.is_anime):
        by_day[_day_key(ep.air_date_utc)].append(_episode_label(ep))
    return _digest_embed(f"📺 TV episodes coming up in the next {days} days", by_day, "Nothing scheduled.")


def build_anime_digest(episodes: list[CalendarEpisode], days: int) -> Optional[discord.Embed]:
    by_day: dict[str, list[str]] = defaultdict(list)
    for ep in (e for e in episodes if e.is_anime):
        by_day[_day_key(ep.air_date_utc)].append(_episode_label(ep))
    return _digest_embed(f"🈴 Anime episodes coming up in the next {days} days", by_day, "Nothing scheduled.")
