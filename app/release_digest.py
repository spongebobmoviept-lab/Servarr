"""Daily auto-post of what's coming up — so #movie-releases/#tv-releases/
#anime-releases show something without anyone having to remember to type
/upcoming. Posted once a day; /upcoming still works any time for an
on-demand check.

These three channels often also get other bots' webhook posts (e.g. grab
notifications from a downloader) interleaved constantly. A digest message
sent once and left alone would scroll out of view within minutes, so
_last_message/on_channel_message below keep it pinned at the bottom by
deleting and re-sending it whenever something else posts after it.

Optional "pinned tile" support (PINNED_TILE_CHANNEL + PINNED_TILE_TITLE_MATCH,
off by default) handles a different shape of the same problem: another
bot's single webhook message that it edits in place (a status board, say).
An edit never moves a message, so once anything else posts after it the
tile sits stranded above the new posts. Servarr doesn't own that tile's
content and can't regenerate it, so for that channel it only deletes: the
stale tile is removed and the tile's owner is expected to post a fresh one
(most self-editing webhook boards re-post when their message 404s). While
no tile exists, stray posts in that channel are removed too, so only use
this on a channel dedicated to the tile.
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

_SELF_MANAGED_CHANNEL_NAMES = {"movie-releases", "tv-releases", "anime-releases"}
# Debounce so a burst of other bots' posts (a single scan pass can post a
# dozen+ grabs back to back) triggers exactly one reclaim at the end of the
# burst, not one delete+repost per message.
_RECLAIM_DEBOUNCE_SECONDS = 8

_last_message: dict[int, discord.Message] = {}
_reclaim_tasks: dict[int, asyncio.Task] = {}


def set_client(client: discord.Client) -> None:
    global _client
    _client = client


def _tile_enabled() -> bool:
    return bool(settings.pinned_tile_channel and settings.pinned_tile_title_match)


def _is_tile_channel(channel: discord.abc.GuildChannel) -> bool:
    return _tile_enabled() and channel.name == settings.pinned_tile_channel


def _is_pinned_tile(message: discord.Message) -> bool:
    """The tile is a webhook message (not a bot user) whose first embed's
    title contains PINNED_TILE_TITLE_MATCH."""
    if not _tile_enabled() or message.webhook_id is None or not message.embeds:
        return False
    title = message.embeds[0].title or ""
    return settings.pinned_tile_title_match in title


async def _replace_tracked_message(channel: discord.TextChannel, embed: discord.Embed) -> None:
    """Delete whatever copy of the board is currently tracked for this
    channel (if any) and send a fresh one, becoming the new bottom message.
    Used both by the daily refresh and by the bottom-reclaim below. Only
    for the self-managed digest channels — the pinned tile uses the
    delete-only path in _reclaim_bottom instead.
    """
    old = _last_message.get(channel.id)
    if old is not None:
        try:
            await old.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            await log(f"release_digest: couldn't delete old board in #{channel.name}: {exc}")
    try:
        _last_message[channel.id] = await channel.send(embed=embed)
    except discord.HTTPException as exc:
        await log(f"release_digest: couldn't post to #{channel.name}: {exc}")


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
        # A fresh day's content is about to replace whatever's tracked —
        # a reclaim mid-flight from stale content would just be deleted
        # again immediately after, so drop it rather than let it race.
        pending = _reclaim_tasks.pop(channel.id, None)
        if pending and not pending.done():
            pending.cancel()
        await _replace_tracked_message(channel, embed)


async def seed_pinned_tile(guild: discord.Guild) -> None:
    """Call once from on_ready. Finds the tile currently in the pinned-tile
    channel (if the feature is on) and adopts it into _last_message, so a
    stray post that already landed after it before this bot started gets
    reclaimed right away instead of waiting for the next message.
    """
    if not _tile_enabled():
        return
    channel = discord.utils.get(guild.text_channels, name=settings.pinned_tile_channel)
    if channel is None:
        return
    try:
        async for msg in channel.history(limit=20):
            if _is_pinned_tile(msg):
                _last_message[channel.id] = msg
                if msg.id != channel.last_message_id:
                    _schedule_reclaim(channel)
                return
    except discord.HTTPException as exc:
        await log(f"release_digest: couldn't seed the pinned tile in #{channel.name}: {exc}")


def _schedule_reclaim(channel: discord.TextChannel) -> None:
    pending = _reclaim_tasks.get(channel.id)
    if pending and not pending.done():
        pending.cancel()
    _reclaim_tasks[channel.id] = asyncio.create_task(_reclaim_bottom(channel))


def on_channel_message(message: discord.Message) -> None:
    """Call from bot.py's on_message for every message, including bot/
    webhook ones — other bots' posts are exactly what this needs to react
    to. No-op for anything outside the tracked channels or when nothing's
    tracked there yet.
    """
    if _client is None or _client.user is None or message.author.id == _client.user.id:
        return
    channel = message.channel
    if not isinstance(channel, discord.TextChannel):
        return
    is_tile_channel = _is_tile_channel(channel)
    if channel.name not in _SELF_MANAGED_CHANNEL_NAMES and not is_tile_channel:
        return

    if is_tile_channel and _is_pinned_tile(message):
        # The tile's owner posting/recreating its own tile — adopt it as
        # the new tracked message, don't reclaim.
        pending = _reclaim_tasks.get(channel.id)
        if pending and not pending.done():
            pending.cancel()
            _reclaim_tasks.pop(channel.id, None)
        _last_message[channel.id] = message
        return

    if channel.id not in _last_message:
        if is_tile_channel:
            # Nothing tracked usually means we're mid self-heal (our delete
            # already went out; the tile's owner hasn't re-posted yet). The
            # channel is tile-only by configuration, so anything landing in
            # that gap is clutter — remove it rather than let it strand
            # there with nothing left to trigger a reclaim off of.
            asyncio.create_task(_delete_stray_tile_channel_message(message))
        return

    _schedule_reclaim(channel)


async def _delete_stray_tile_channel_message(message: discord.Message) -> None:
    try:
        await message.delete()
    except discord.NotFound:
        pass
    except discord.HTTPException as exc:
        await log(f"release_digest: couldn't delete stray message in #{message.channel}: {exc}")


async def _reclaim_bottom(channel: discord.TextChannel) -> None:
    try:
        await asyncio.sleep(_RECLAIM_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    tracked = _last_message.get(channel.id)
    if tracked is None:
        return

    if _is_tile_channel(channel):
        # Delete-only — see module docstring for why we don't regenerate
        # or repost someone else's tile ourselves.
        try:
            await tracked.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            await log(f"release_digest: couldn't delete stale tile in #{channel.name}: {exc}")
        _last_message.pop(channel.id, None)
        _reclaim_tasks.pop(channel.id, None)
        return

    if not tracked.embeds:
        return
    await _replace_tracked_message(channel, tracked.embeds[0])
    _reclaim_tasks.pop(channel.id, None)


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
