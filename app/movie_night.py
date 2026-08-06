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

from . import neko_control, plex, radarr, reclaimarr_client
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

    started = await _start_in_neko(winner)

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
        viewer_link = neko_control.current_viewer_link() or settings.neko_public_url
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="▶️  WATCH LIVE", style=discord.ButtonStyle.link, url=viewer_link))
    else:
        search_url = "https://app.plex.tv/desktop#!/search?query=" + urllib.parse.quote(winner.title)
        embed.description = (
            f"**{winner.title}**" + (f" ({winner.year})" if winner.year else "") + f"\n{note} — grab your snacks and hit play on your own device!"
        )
        embed.add_field(name="Find it in Plex", value=f"[Search for it]({search_url})", inline=False)
    if winner.poster_url:
        embed.set_image(url=winner.poster_url)
    await channel.send(content=None if is_test else "@everyone", embed=embed, view=view)
    _current_vote.clear()
    return True


async def _start_in_neko(winner: "radarr.LibraryMovie") -> bool:
    """Best-effort: navigate the shared Neko browser to the winning movie and
    hit play. Returns False (never raises) on anything short of full success
    so a Neko/Plex hiccup can't take down the rest of the announcement.
    """
    if not settings.neko_url or not settings.plex_url or winner.tmdb_id is None:
        return False
    try:
        rating_key = await plex.find_rating_key_by_tmdb_id(winner.title, winner.tmdb_id)
        if not rating_key:
            await log(f"movie_night: couldn't resolve a Plex ratingKey for {winner.title!r} — skipping Neko automation")
            return False
        machine_id = await plex.get_machine_identifier()
        if not machine_id:
            return False
        plex_web_url = f"https://app.plex.tv/desktop/#!/server/{machine_id}/details?key=%2Flibrary%2Fmetadata%2F{rating_key}"
        return await neko_control.play_movie(plex_web_url)
    except Exception as exc:  # noqa: BLE001
        await log(f"movie_night: Neko automation failed: {exc}")
        return False


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
