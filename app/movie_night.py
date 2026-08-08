"""Movie Night — a daily vote among already-downloaded movies, winner
announced at 9pm. Can't literally stream video through the bot (Discord
bot accounts can't "Go Live" the way a real user client can — not reliably
buildable), so this gets everyone watching at the same time on their own
device instead: vote, announce, and make sure Reclaimarr doesn't interrupt
the pick with a surprise 4K swap mid-movie.
"""

import asyncio
import datetime
import random
import urllib.parse
from typing import Optional

import discord

from . import mpv_control, radarr, reclaimarr_client
from .config import settings
from .logger import log

GOLD = 0xD4AF37

_client: Optional[discord.Client] = None
_current_vote: dict = {}


def set_client(client: discord.Client) -> None:
    global _client
    _client = client


def _vote_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    return discord.utils.get(guild.text_channels, name="general")


def _is_dj_member(member: discord.Member) -> bool:
    """Same rule as bot.py's _is_movie_night_dj, applied to a plain
    discord.Member instead of an Interaction — needed here to scan every
    guild member for the nightly DM (see _dm_admin_links), not just check
    whoever ran a command. Kept as its own copy rather than importing from
    bot.py to avoid a circular import (bot.py already imports this module).
    """
    if member.guild_permissions.manage_guild:
        return True
    configured = settings.movie_night_dj_role_id.strip()
    if not configured:
        return False
    if configured.isdigit():
        return any(r.id == int(configured) for r in member.roles)
    return any(r.name.lower() == configured.lower() for r in member.roles)


async def _dm_admin_links(guild: discord.Guild) -> None:
    """Automatically hands every DJ/mod their personal admin control link
    the moment tonight's movie starts — a single Discord message can't show
    different button URLs to different viewers, so this is the only way
    for the nightly automated announcement (as opposed to a manual
    /play-movie run, which already gets this via _send_admin_link) to give
    Commanders control without them having to ask for it separately.
    Best-effort per member — a closed-DMs error shouldn't stop the others.
    """
    link = mpv_control.admin_link()
    if not link:
        return
    for member in guild.members:
        if member.bot or not _is_dj_member(member):
            continue
        try:
            await member.send(
                f"🔑 Tonight's Movie Night admin link (pause/seek/browse) — keep it to yourself: {link}"
            )
        except Exception as exc:  # noqa: BLE001
            await log(f"movie_night: couldn't DM admin link to {member}: {exc}")


class VoteView(discord.ui.View):
    """Not persistent across restarts on purpose — each night's candidates
    are different, so there's nothing meaningful to resume after a restart
    mid-vote. The vote window is a few hours; a redeploy in that window is
    rare enough to accept the tradeoff.
    """

    def __init__(self, candidates: list) -> None:
        super().__init__(timeout=None)
        for movie in candidates:
            btn = discord.ui.Button(label=movie.title[:75], style=discord.ButtonStyle.primary)
            btn.callback = self._make_vote(movie.id, movie.title)
            self.add_item(btn)

    def _make_vote(self, movie_id: int, title: str):
        async def _cb(interaction: discord.Interaction) -> None:
            _current_vote.setdefault("votes", {})[interaction.user.id] = movie_id
            await interaction.response.send_message(f"🗳️ Voted for **{title}**!", ephemeral=True)

        return _cb


async def post_vote(guild: discord.Guild) -> bool:
    candidates = await radarr.list_downloaded_movies()
    if len(candidates) < 2:
        await log("movie_night: fewer than 2 downloaded movies available — skipping tonight's vote")
        return False
    channel = _vote_channel(guild)
    if channel is None:
        await log("movie_night: no #general channel found — skipping vote")
        return False

    picks = random.sample(candidates, min(settings.movie_night_candidate_count, len(candidates)))
    embed = discord.Embed(
        title="🍿 Vote for tonight's Movie Night!",
        description="Pick one below — whichever gets the most votes plays tonight. No votes in by showtime? I'll just pick one at random.",
        color=GOLD,
    )
    for m in picks:
        embed.add_field(name=m.title, value=str(m.year) if m.year else "​", inline=True)

    _current_vote.clear()
    _current_vote["candidates"] = picks
    _current_vote["votes"] = {}
    await channel.send(content="@everyone", embed=embed, view=VoteView(picks))
    return True


async def announce_winner(guild: discord.Guild, is_test: bool = False) -> bool:
    candidates = _current_vote.get("candidates")
    if not candidates:
        return False
    channel = _vote_channel(guild)
    if channel is None:
        return False

    votes: dict = _current_vote.get("votes", {})
    tally: dict[int, int] = {}
    for movie_id in votes.values():
        tally[movie_id] = tally.get(movie_id, 0) + 1

    if tally:
        top_count = max(tally.values())
        winner_id = random.choice([mid for mid, count in tally.items() if count == top_count])
        note = f"{tally[winner_id]} vote(s)"
    else:
        winner_id = random.choice(candidates).id
        note = "no votes came in, so I picked one at random"

    winner = next(m for m in candidates if m.id == winner_id)
    await reclaimarr_client.pause_upgrade(winner.id, settings.movie_night_pause_upgrade_minutes)

    started = await start_in_neko(winner)

    embed = discord.Embed(
        title="🧪 [TEST] Movie Night pick" if is_test else "🎬 Tonight's Movie Night pick",
        color=GOLD,
    )
    if is_test:
        note = f"manual test trigger, not tonight's real pick — {note}"
    view = None
    if started:
        embed.description = (
            f"**{winner.title}**" + (f" ({winner.year})" if winner.year else "") + f"\n{note} — it's already loading up, click below to watch together!"
        )
        # Viewer-level link — this posts to the whole channel, so it must
        # never be the admin one (that would hand everyone the admin
        # password and show them the control tab).
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="▶️  WATCH LIVE", style=discord.ButtonStyle.link, url=mpv_control.viewer_link()))
    else:
        search_url = "https://app.plex.tv/desktop#!/search?query=" + urllib.parse.quote(winner.title)
        embed.description = (
            f"**{winner.title}**" + (f" ({winner.year})" if winner.year else "") + f"\n{note} — grab your snacks and hit play on your own device!"
        )
        embed.add_field(name="Find it in Plex", value=f"[Search for it]({search_url})", inline=False)
    if winner.poster_url:
        embed.set_image(url=winner.poster_url)
    await channel.send(content=None if is_test else "@everyone", embed=embed, view=view)
    if started and not is_test:
        await _dm_admin_links(guild)
    _current_vote.clear()
    return True


async def start_in_neko(movie: "radarr.LibraryMovie") -> bool:
    """Best-effort: start this movie on the shared neko-mpv player. Returns
    False (never raises) on anything short of full success so a
    Neko/Plex hiccup can't take down whatever's calling this (a Movie
    Night announcement, or a direct /play-movie trigger). Runs through
    plex-mpv-shim's real Companion API (see mpv_control.py) — the old
    Plex-Desktop/CDP-driven browser this used to drive has been retired.
    """
    if not settings.neko_mpv_shim_url:
        return False
    try:
        return await mpv_control.play(movie.title, movie.year)
    except Exception as exc:  # noqa: BLE001
        await log(f"movie_night: neko-mpv automation failed: {exc}")
        return False


async def stop_movie() -> bool:
    """/stop-movie's actual logic. Best-effort, same reasoning as
    start_in_neko above."""
    if not settings.neko_mpv_shim_url:
        return False
    try:
        return await mpv_control.stop()
    except Exception as exc:  # noqa: BLE001
        await log(f"movie_night: stopping playback failed: {exc}")
        return False


async def play_now(channel: discord.abc.Messageable, movie: "radarr.LibraryMovie") -> bool:
    """Shared by both Movie Night's own announcement and /play-movie's direct
    trigger: pause upgrades so nothing swaps mid-movie, start it in Neko, and
    post a watch-live announcement in the given channel.
    """
    await reclaimarr_client.pause_upgrade(movie.id, settings.movie_night_pause_upgrade_minutes)
    started = await start_in_neko(movie)

    embed = discord.Embed(title="🎬 Now playing", color=GOLD)
    view = None
    if started:
        embed.description = f"**{movie.title}**" + (f" ({movie.year})" if movie.year else "") + "\nIt's already loading up, click below to watch together!"
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="▶️  WATCH LIVE", style=discord.ButtonStyle.link, url=mpv_control.viewer_link()))
    else:
        search_url = "https://app.plex.tv/desktop#!/search?query=" + urllib.parse.quote(movie.title)
        embed.description = f"**{movie.title}**" + (f" ({movie.year})" if movie.year else "") + "\nCouldn't start it automatically — grab it yourself in Plex instead."
        embed.add_field(name="Find it in Plex", value=f"[Search for it]({search_url})", inline=False)
    if movie.poster_url:
        embed.set_image(url=movie.poster_url)
    await channel.send(embed=embed, view=view)
    return started


class PlayPickView(discord.ui.View):
    """Only the person who ran /play-movie can pick — otherwise anyone
    seeing the ephemeral picker in a shared channel context could hijack
    someone else's search. Times out after a couple minutes since it's a
    one-shot pick, not something meant to be revisited later.
    """

    def __init__(self, candidates: list, requester_id: int) -> None:
        super().__init__(timeout=120)
        self.requester_id = requester_id
        for movie in candidates[:10]:
            label = (movie.title + (f" ({movie.year})" if movie.year else ""))[:80]
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            btn.callback = self._make_pick(movie)
            self.add_item(btn)

    def _make_pick(self, movie: "radarr.LibraryMovie"):
        async def _cb(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.requester_id:
                await interaction.response.send_message("Only whoever ran the command can pick.", ephemeral=True)
                return
            await interaction.response.edit_message(content=f"Starting **{movie.title}**…", view=None)
            started = await play_now(interaction.channel, movie)
            if started:
                await _send_admin_link(interaction)

        return _cb


async def _send_admin_link(interaction: discord.Interaction) -> None:
    """Sent privately only to whoever explicitly asked for this movie —
    never posted to the channel. Lets them actually control the shared
    session themselves (pause, seek), which the regular WATCH LIVE link
    everyone else gets intentionally can't do.
    """
    link = mpv_control.admin_link()
    if not link:
        return
    await interaction.followup.send(
        f"🔑 Since you started it, here's the control link (pause/seek yourself) — "
        f"keep this one to yourself, don't share it in chat: {link}",
        ephemeral=True,
    )


async def play_specific(interaction: discord.Interaction, query: str) -> None:
    """/play-movie's actual logic — search the downloaded library by title
    and either play the single match directly, show a picker for multiple
    matches, or report nothing found. Assumes the interaction has already
    been deferred by the caller.
    """
    candidates = await radarr.list_downloaded_movies()
    normalized = query.strip().lower()
    matches = [m for m in candidates if normalized in m.title.lower()]
    if not matches:
        await interaction.followup.send(f"Couldn't find a downloaded movie matching **{query}**.", ephemeral=True)
        return
    if len(matches) == 1:
        started = await play_now(interaction.channel, matches[0])
        await interaction.followup.send(f"Starting **{matches[0].title}**…", ephemeral=True)
        if started:
            await _send_admin_link(interaction)
        return
    view = PlayPickView(matches, interaction.user.id)
    await interaction.followup.send(f"Found {len(matches)} matches — pick one:", view=view, ephemeral=True)


async def force_winner_now(guild: discord.Guild) -> bool:
    """Skip the normal vote-then-wait cycle — for manual/testing triggers.
    If a vote is currently open, tallies and announces it right now;
    otherwise just picks one random downloaded movie and announces+plays it.
    """
    if not _current_vote.get("candidates"):
        candidates = await radarr.list_downloaded_movies()
        if not candidates:
            return False
        _current_vote.clear()
        _current_vote["candidates"] = [random.choice(candidates)]
        _current_vote["votes"] = {}
    return await announce_winner(guild, is_test=True)


async def _sleep_until_hour_utc(hour: int) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())


async def _wait_for_client() -> None:
    while _client is None or not _client.guilds:
        await asyncio.sleep(2)


async def vote_loop() -> None:
    await _wait_for_client()
    while True:
        await _sleep_until_hour_utc(settings.movie_night_vote_hour_utc)
        for guild in _client.guilds:
            try:
                await post_vote(guild)
            except Exception as exc:  # noqa: BLE001
                await log(f"movie_night: posting the vote failed: {exc}")


async def announce_loop() -> None:
    await _wait_for_client()
    while True:
        await _sleep_until_hour_utc(settings.movie_night_hour_utc)
        for guild in _client.guilds:
            try:
                await announce_winner(guild)
            except Exception as exc:  # noqa: BLE001
                await log(f"movie_night: announcing the winner failed: {exc}")
