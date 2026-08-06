"""Live 'now playing' dashboard — one message in #now-playing that gets
edited in place every poll interval, rather than a stream of separate
event messages. Tautulli already has its own Discord webhook for the
play/pause/stop event log (that's a different, existing thing); this is a
persistent status view instead — "what's happening right now," at a glance.
"""

import asyncio
from typing import Optional

import discord

from .config import settings
from .logger import log
from .tautulli import get_activity

GOLD = 0xD4AF37
PAUSED_GREY = 0x6B6B6B

_client: Optional[discord.Client] = None
_message: Optional[discord.Message] = None

_STATE_EMOJI = {"playing": "▶️", "paused": "⏸️", "buffering": "⏳"}


def _progress_bar(percent: int, length: int = 14) -> str:
    filled = min(length, round((percent / 100) * length))
    return "█" * filled + "░" * (length - filled)


def set_client(client: discord.Client) -> None:
    global _client
    _client = client


def _build_embed(sessions: list) -> discord.Embed:
    if not sessions:
        embed = discord.Embed(title="📺 Now Playing", description="*Nothing playing right now — start something and it'll show up here.*", color=PAUSED_GREY)
        return embed

    any_playing = any(s.state == "playing" for s in sessions)
    embed = discord.Embed(title="📺 Now Playing", color=GOLD if any_playing else PAUSED_GREY)

    for s in sessions:
        emoji = _STATE_EMOJI.get(s.state, "▶️")
        title = f"{s.title} ({s.year})" if s.year else s.title
        quality_bits = " • ".join(b for b in [s.quality, s.decision] if b)
        value = (
            f"{emoji} **{title}**\n"
            f"`{_progress_bar(s.progress_percent)}` {s.progress_percent}%\n"
            f"🖥️ {s.player}" + (f" • {quality_bits}" if quality_bits else "")
        )
        embed.add_field(name=f"👤 {s.user}", value=value, inline=False)

    poster = next((s.poster_url for s in sessions if s.poster_url), None)
    if poster:
        embed.set_thumbnail(url=poster)
    embed.set_footer(text=f"{len(sessions)} active stream(s) • updates every 20s")
    return embed


async def _find_or_create_message(guild: discord.Guild) -> Optional[discord.Message]:
    channel = discord.utils.get(guild.text_channels, name="now-playing")
    if channel is None:
        return None
    async for msg in channel.history(limit=20):
        if msg.author.id == _client.user.id:
            return msg
    return await channel.send(embed=_build_embed([]))


async def poll_loop() -> None:
    global _message
    if not (settings.tautulli_url and settings.tautulli_api_key):
        await log("plex_monitor: TAUTULLI_URL/TAUTULLI_API_KEY not set — live now-playing view disabled")
        return

    while _client is None or not _client.guilds:
        await asyncio.sleep(2)

    while True:
        try:
            for guild in _client.guilds:
                if _message is None:
                    _message = await _find_or_create_message(guild)
                if _message is not None:
                    sessions = await get_activity()
                    await _message.edit(embed=_build_embed(sessions))
        except Exception as exc:  # noqa: BLE001
            await log(f"plex_monitor: poll failed: {exc} — will retry next cycle")
        await asyncio.sleep(settings.plex_monitor_interval_seconds)
