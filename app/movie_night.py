"""Movie Night — a daily vote among already-downloaded movies, winner
announced at 9pm. Can't literally stream video through the bot (Discord
bot accounts can't "Go Live" the way a real user client can — not reliably
buildable), so this gets everyone watching at the same time on their own
device instead: vote, announce, and make sure Reclaimarr doesn't interrupt
the pick with a surprise 4K swap mid-movie.
"""

import asyncio
import datetime
import json
import random
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import discord

from . import features, mpv_control, player_library, radarr, reclaimarr_client
from .config import settings
from .logger import log

GOLD = 0xD4AF37

_client: Optional[discord.Client] = None
_current_vote: dict = {}

# Now-playing messages whose "too many viewers" line status_loop keeps
# current while the movie plays: {"public"|"admin": [message, embed without
# the line, the line currently shown]}.
_live: dict[str, list] = {}
_STATUS_POLL_SECONDS = 60


def _vote_state_path() -> Path:
    return Path(settings.data_dir) / "movie_night_vote.json"


def _save_vote_state() -> None:
    """Persists the in-memory vote across restarts — otherwise a
    redeploy between the vote posting (post_vote) and showtime
    (announce_winner) silently wiped _current_vote, and announce_winner's
    "no candidates on record" case returns False with no error at all, so
    showtime just quietly did nothing and nobody found out until the movie
    never started. Best-effort: a write failure here shouldn't crash the
    bot over what's ultimately just a convenience cache.
    """
    try:
        path = _vote_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "candidates": [asdict(m) for m in _current_vote.get("candidates", [])],
            "votes": {str(k): v for k, v in _current_vote.get("votes", {}).items()},
        }
        path.write_text(json.dumps(data))
    except Exception as exc:  # noqa: BLE001
        pass


def _load_vote_state() -> None:
    path = _vote_state_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
        _current_vote["candidates"] = [radarr.LibraryMovie(**c) for c in data.get("candidates", [])]
        _current_vote["votes"] = {int(k): v for k, v in data.get("votes", {}).items()}
    except Exception as exc:  # noqa: BLE001
        pass


def _clear_vote_state() -> None:
    _current_vote.clear()
    try:
        _vote_state_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def set_client(client: discord.Client) -> None:
    global _client
    _client = client
    _load_vote_state()


def _vote_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """The Movie Night channel picked in the setup page, or #general when
    none is picked. With a channel picked, other servers get nothing."""
    configured = (settings.movie_night_channel_id or "").strip()
    if configured.isdigit():
        channel = guild.get_channel(int(configured))
        return channel if isinstance(channel, discord.TextChannel) else None
    return discord.utils.get(guild.text_channels, name="general")


def _no_channel_reason() -> str:
    if (settings.movie_night_channel_id or "").strip():
        return "the Movie Night channel picked in the setup page isn't in this server"
    return "no #general channel found"


def vote_channel_label(guild: discord.Guild) -> str:
    channel = _vote_channel(guild)
    return f"#{channel.name}" if channel else "the Movie Night channel"


def _everyone() -> Optional[str]:
    """Movie Night only mode never pings @everyone (its invite doesn't ask
    for Mention Everyone, and it's usually someone else's server)."""
    return None if features.is_movienight() else "@everyone"


# ---------------------------------------------------------------------------
# "too many viewers" line (Movie Night player 1.1+)
# ---------------------------------------------------------------------------


def _with_warning(embed: discord.Embed, warning: str) -> discord.Embed:
    if not warning:
        return embed
    shown = embed.copy()
    shown.description = f"{embed.description}\n{warning}" if embed.description else warning
    return shown


async def _current_warning() -> str:
    return mpv_control.capacity_warning(await mpv_control.player_status())


def _track(kind: str, message: Optional[discord.Message], embed: discord.Embed, warning: str) -> None:
    if message is not None and mpv_control.mode() == "movienight":
        _live[kind] = [message, embed, warning]


async def refresh_live_messages() -> None:
    """Adds, updates or removes the warning line on the tracked now-playing
    messages (only edits when the line changes). Stops tracking once
    nothing is playing any more."""
    status = await mpv_control.player_status()
    if status is None:
        return  # the player didn't answer; try again next round
    if status.get("state") not in ("playing", "paused", "buffering"):
        _live.clear()
        return
    warning = mpv_control.capacity_warning(status)
    for kind, entry in list(_live.items()):
        message, embed, shown = entry
        if warning == shown:
            continue
        try:
            await message.edit(embed=_with_warning(embed, warning))
            entry[2] = warning
        except discord.NotFound:
            _live.pop(kind, None)
        except discord.HTTPException as exc:
            await log(f"movie_night: couldn't update the now-playing message: {exc}")


async def status_loop() -> None:
    while True:
        await asyncio.sleep(_STATUS_POLL_SECONDS)
        if not _live:
            continue
        try:
            await refresh_live_messages()
        except Exception as exc:  # noqa: BLE001
            await log(f"movie_night: checking the player's viewers failed: {exc}")


def _commander_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Admin-only player controls, for DJs/mods — a more reliable delivery
    of the admin link than an ephemeral reply (easy to miss) or a DM (fails
    silently when the recipient has DMs closed). Name comes from
    MOVIE_NIGHT_CONTROL_CHANNEL.

    Returns None (and logs) if the channel is visible to @everyone or the
    rules-gate Member role: the admin link carries the player's admin
    password, so it's only ever posted in a channel that is actually private.
    """
    channel = discord.utils.get(guild.text_channels, name=settings.movie_night_control_channel)
    if channel is None:
        return None
    public_roles = [guild.default_role]
    member_role = discord.utils.get(guild.roles, name="Member")
    if member_role is not None:
        public_roles.append(member_role)
    for role in public_roles:
        if channel.permissions_for(role).view_channel:
            _warn_public_control_channel(channel.name, role.name)
            return None
    return channel


_warned_public_channels: set[str] = set()


def _warn_public_control_channel(channel_name: str, role_name: str) -> None:
    if channel_name in _warned_public_channels:
        return
    _warned_public_channels.add(channel_name)
    print(
        f"movie_night: #{channel_name} is visible to {role_name} — not posting admin controls there. "
        "Make it visible to DJs/mods only.",
        flush=True,
    )


async def _check_dj(interaction: discord.Interaction) -> bool:
    if isinstance(interaction.user, discord.Member) and _is_dj_member(interaction.user):
        return True
    await interaction.response.send_message("Only a Movie Night DJ/mod can control playback.", ephemeral=True)
    return False


class MovieSearchModal(discord.ui.Modal, title="Search your Plex library"):
    """Lets a DJ search-and-play without ever leaving Discord or
    typing a slash command — the control-channel panel button that opens
    this is otherwise just a link + a few transport buttons, which isn't
    "complete" on its own if starting something new still requires
    /play-movie. Reuses play_specific's exact search/pick/play logic
    (single match plays directly, multiple shows a picker) so there's only
    one implementation of "search the downloaded library" to maintain.
    """

    query = discord.ui.TextInput(label="Movie title", placeholder="e.g. Inception")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await play_specific(interaction, str(self.query))


class CommanderPanelView(discord.ui.View):
    """The always-on control panel in the Movie Night control channel — persistent
    (fixed custom_id per button, registered via client.add_view in
    bot.py's on_ready) so it keeps working across every redeploy, unlike
    add_transport_buttons' one-off buttons on each "Now playing" message
    (those are fine to lose on restart — a fresh one posts next movie).
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="⏯", style=discord.ButtonStyle.secondary, custom_id="commander_panel:pause_resume", row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _check_dj(interaction):
            return
        status = await mpv_control.get_status()
        if status["state"] == "playing":
            await mpv_control.pause()
            await interaction.response.send_message("⏸️ Paused.", ephemeral=True)
        elif status["state"] == "paused":
            await mpv_control.resume()
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing's playing right now.", ephemeral=True)

    @discord.ui.button(label="⏪ 10s", style=discord.ButtonStyle.secondary, custom_id="commander_panel:seek_back", row=0)
    async def seek_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _check_dj(interaction):
            return
        position = await mpv_control.seek(-10)
        if position is None:
            await interaction.response.send_message("Nothing's loaded right now.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⏪ Now at {position // 60}:{position % 60:02d}.", ephemeral=True)

    @discord.ui.button(label="10s ⏩", style=discord.ButtonStyle.secondary, custom_id="commander_panel:seek_fwd", row=0)
    async def seek_fwd(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _check_dj(interaction):
            return
        position = await mpv_control.seek(10)
        if position is None:
            await interaction.response.send_message("Nothing's loaded right now.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⏩ Now at {position // 60}:{position % 60:02d}.", ephemeral=True)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id="commander_panel:stop", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _check_dj(interaction):
            return
        ok = await mpv_control.stop()
        await interaction.response.send_message("⏹️ Stopped." if ok else "⚠️ Couldn't stop it.", ephemeral=True)

    @discord.ui.button(label="🔍 Search & Play", style=discord.ButtonStyle.primary, custom_id="commander_panel:search", row=1)
    async def search_play(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _check_dj(interaction):
            return
        await interaction.response.send_modal(MovieSearchModal())


_PANEL_MARKER = "commander-panel-v1"


async def refresh_commander_panel(guild: discord.Guild) -> None:
    """Idempotent: checks for an already-posted panel (by its footer
    marker) before posting a new one, so a redeploy/restart doesn't spam a
    duplicate panel into the channel every time. Called from bot.py's
    on_ready, right after the persistent view itself gets registered.
    """
    channel = _commander_channel(guild)
    if channel is None:
        return
    try:
        async for old in channel.history(limit=20):
            if old.embeds and old.embeds[0].footer and old.embeds[0].footer.text == _PANEL_MARKER:
                return
    except Exception as exc:  # noqa: BLE001
        await log(f"movie_night: couldn't check #{channel.name} history: {exc}")
        return
    link = mpv_control.admin_link()
    if not link:
        return
    embed = discord.Embed(
        title="🔑 Movie Night DJ Controls",
        description=(
            f"[Open admin Neko link]({link}) — pick your own name when Neko asks.\n\n"
            "The buttons below work anytime, whether or not a movie is currently loaded — "
            "**Search & Play** finds anything in the downloaded library without needing a slash command."
        ),
        color=GOLD,
    )
    embed.set_footer(text=_PANEL_MARKER)
    try:
        await channel.send(embed=embed, view=CommanderPanelView())
    except Exception as exc:  # noqa: BLE001
        await log(f"movie_night: couldn't post commander panel: {exc}")


_NOW_PLAYING_MARKER = "commander-nowplaying-v1"


async def _post_admin_now_playing(guild: discord.Guild, title: str, year: Optional[int]) -> None:
    """Edits one persistent 'now playing' message in place (by footer
    marker, same idempotent pattern as refresh_commander_panel) instead of
    sending a new one every time a movie starts — this used to post a fresh
    message daily, leaving the admin link (with the real Neko admin
    password baked into the URL) sitting in an ever-growing pile of old
    messages instead of one current, live one.
    """
    channel = _commander_channel(guild)
    if channel is None:
        return
    link = mpv_control.admin_link()
    if not link:
        return
    embed = discord.Embed(title="🎬 Now playing (admin)", color=GOLD)
    embed.description = f"**{title}**" + (f" ({year})" if year else "") + f"\n[Open admin controls]({link}) — pick your own name when Neko asks."
    embed.set_footer(text=_NOW_PLAYING_MARKER)
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🔑  ADMIN CONTROLS", style=discord.ButtonStyle.link, url=link))
    add_transport_buttons(view)
    warning = await _current_warning()
    try:
        existing = None
        async for old in channel.history(limit=20):
            if old.author == guild.me and old.embeds and old.embeds[0].footer and old.embeds[0].footer.text == _NOW_PLAYING_MARKER:
                existing = old
                break
        if existing:
            await existing.edit(embed=_with_warning(embed, warning), view=view)
            message = existing
        else:
            message = await channel.send(embed=_with_warning(embed, warning), view=view)
        _track("admin", message, embed, warning)
    except Exception as exc:  # noqa: BLE001
        await log(f"movie_night: couldn't post to #{channel.name}: {exc}")


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


def _require_dj(handler):
    """Wraps a button callback so only a DJ/mod can actually trigger it —
    same permission as /mpv-pause etc. (which still work fine on their
    own; these buttons are an additional, lower-friction way to reach the
    same mpv_control functions from the Now Playing message itself).
    Anyone else gets a quiet ephemeral no instead of the action running.
    """

    async def wrapped(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not _is_dj_member(interaction.user):
            await interaction.response.send_message("Only a Movie Night DJ/mod can control playback.", ephemeral=True)
            return
        await handler(interaction)

    return wrapped


def add_transport_buttons(view: discord.ui.View) -> None:
    """Real clickable pause/seek/stop controls on the Now Playing message
    itself, next to the WATCH LIVE link button — not just slash commands.
    """

    @_require_dj
    async def _pause_resume(interaction: discord.Interaction) -> None:
        status = await mpv_control.get_status()
        if status["state"] == "playing":
            await mpv_control.pause()
            await interaction.response.send_message("⏸️ Paused.", ephemeral=True)
        elif status["state"] == "paused":
            await mpv_control.resume()
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing's playing right now.", ephemeral=True)

    @_require_dj
    async def _seek_back(interaction: discord.Interaction) -> None:
        position = await mpv_control.seek(-10)
        if position is None:
            await interaction.response.send_message("Nothing's loaded right now.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⏪ Now at {position // 60}:{position % 60:02d}.", ephemeral=True)

    @_require_dj
    async def _seek_fwd(interaction: discord.Interaction) -> None:
        position = await mpv_control.seek(10)
        if position is None:
            await interaction.response.send_message("Nothing's loaded right now.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⏩ Now at {position // 60}:{position % 60:02d}.", ephemeral=True)

    @_require_dj
    async def _stop(interaction: discord.Interaction) -> None:
        ok = await mpv_control.stop()
        await interaction.response.send_message("⏹️ Stopped." if ok else "⚠️ Couldn't stop it.", ephemeral=True)

    pause_btn = discord.ui.Button(label="⏯", style=discord.ButtonStyle.secondary, row=1)
    pause_btn.callback = _pause_resume
    view.add_item(pause_btn)

    back_btn = discord.ui.Button(label="⏪ 10s", style=discord.ButtonStyle.secondary, row=1)
    back_btn.callback = _seek_back
    view.add_item(back_btn)

    fwd_btn = discord.ui.Button(label="10s ⏩", style=discord.ButtonStyle.secondary, row=1)
    fwd_btn.callback = _seek_fwd
    view.add_item(fwd_btn)

    stop_btn = discord.ui.Button(label="⏹ Stop", style=discord.ButtonStyle.danger, row=1)
    stop_btn.callback = _stop
    view.add_item(stop_btn)


async def _dm_admin_links(guild: discord.Guild) -> None:
    """Automatically hands every DJ/mod their personal admin control link
    the moment tonight's movie starts — a single Discord message can't show
    different button URLs to different viewers, so this is the only way
    for the nightly automated announcement (as opposed to a manual
    /play-movie run, which already gets this via _send_admin_link) to give
    DJs control without them having to ask for it separately.
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


def _build_vote_embed(candidates: list, votes: dict) -> discord.Embed:
    """Rebuilds the poll embed from scratch with a live tally + who voted
    for what — called both for the initial post and after every single
    vote, so the count is always visible in the poll message itself rather
    than hidden behind each voter's own private "you voted" receipt
    (a private receipt reads as "the whole vote is
    hidden" even though the candidate list itself was always public).
    """
    tally: dict[int, list[int]] = {m.id: [] for m in candidates}
    for voter_id, movie_id in votes.items():
        tally.setdefault(movie_id, []).append(voter_id)

    embed = discord.Embed(
        title="🍿 Vote for tonight's Movie Night!",
        description="Pick one below — whichever gets the most votes plays tonight. No votes in by showtime? I'll just pick one at random.",
        color=GOLD,
    )
    for m in candidates:
        voter_ids = tally.get(m.id, [])
        count_label = "1 vote" if len(voter_ids) == 1 else f"{len(voter_ids)} votes"
        voters = ", ".join(f"<@{uid}>" for uid in voter_ids) if voter_ids else "​"
        year = f" ({m.year})" if m.year else ""
        embed.add_field(name=f"{m.title}{year} — {count_label}", value=voters, inline=False)
    return embed


class VoteView(discord.ui.View):
    """Not persistent across restarts on purpose — each night's candidates
    are different, so there's nothing meaningful to resume after a restart
    mid-vote. The vote window is a few hours; a redeploy in that window is
    rare enough to accept the tradeoff.
    """

    def __init__(self, candidates: list) -> None:
        super().__init__(timeout=None)
        self.candidates = candidates
        for movie in candidates:
            btn = discord.ui.Button(label=movie.title[:75], style=discord.ButtonStyle.primary)
            btn.callback = self._make_vote(movie.id)
            self.add_item(btn)

    def _make_vote(self, movie_id: int):
        async def _cb(interaction: discord.Interaction) -> None:
            _current_vote.setdefault("votes", {})[interaction.user.id] = movie_id
            _save_vote_state()
            # Edits the poll message itself (visible to everyone in the
            # channel) instead of replying with a private ephemeral
            # confirmation — the updated tally IS the confirmation.
            embed = _build_vote_embed(self.candidates, _current_vote["votes"])
            await interaction.response.edit_message(embed=embed, view=self)

        return _cb


async def post_vote(guild: discord.Guild) -> bool:
    if features.is_movienight():
        if _vote_channel(guild) is None:  # not the server Movie Night is set up for: leave the player alone
            await log(f"movie_night: {_no_channel_reason()} — skipping vote")
            return False
        # No Radarr here: pick from the Plex library the player is signed in to.
        try:
            candidates = await player_library.random_movies(settings.movie_night_candidate_count)
        except Exception as exc:  # noqa: BLE001
            await log(f"movie_night: couldn't read the Movie Night player's library for the vote: {exc}")
            return False
        if len(candidates) < 2:
            await log("movie_night: fewer than 2 movies in the player's Plex library — skipping tonight's vote")
            return False
    else:
        candidates = await radarr.list_downloaded_movies()
        if len(candidates) < 2:
            await log("movie_night: fewer than 2 downloaded movies available — skipping tonight's vote")
            return False
    channel = _vote_channel(guild)
    if channel is None:
        await log(f"movie_night: {_no_channel_reason()} — skipping vote")
        return False

    picks = random.sample(candidates, min(settings.movie_night_candidate_count, len(candidates)))
    _current_vote.clear()
    _current_vote["candidates"] = picks
    _current_vote["votes"] = {}
    _save_vote_state()
    embed = _build_vote_embed(picks, {})
    await channel.send(content=_everyone(), embed=embed, view=VoteView(picks))
    return True


async def announce_winner(guild: discord.Guild, is_test: bool = False) -> bool:
    candidates = _current_vote.get("candidates")
    if not candidates:
        # This can happen for real: a restart between
        # post_vote and showtime used to silently wipe _current_vote (now
        # persisted to disk — see _save_vote_state/_load_vote_state — so
        # this should be rare going forward), and showtime would then just
        # quietly do nothing with zero error anywhere. At minimum, log it
        # so a miss is visible instead of invisible.
        await log("movie_night: showtime reached but no vote is on record — skipping (was a vote posted tonight?)")
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
    warning = ""
    if started:
        embed.description = (
            f"**{winner.title}**" + (f" ({winner.year})" if winner.year else "") + f"\n{note} — it's already loading up, click below to watch together!"
        )
        # Viewer-level link — this posts to the whole channel, so it must
        # never be the admin one (that would hand everyone the admin
        # password and show them the control tab).
        view = discord.ui.View()
        watch_url = mpv_control.viewer_link()
        if watch_url:
            view.add_item(discord.ui.Button(label="▶️  WATCH LIVE", style=discord.ButtonStyle.link, url=watch_url))
        add_transport_buttons(view)
        warning = await _current_warning()
    else:
        search_url = "https://app.plex.tv/desktop#!/search?query=" + urllib.parse.quote(winner.title)
        embed.description = (
            f"**{winner.title}**" + (f" ({winner.year})" if winner.year else "") + f"\n{note} — grab your snacks and hit play on your own device!"
        )
        embed.add_field(name="Find it in Plex", value=f"[Search for it]({search_url})", inline=False)
    if winner.poster_url:
        embed.set_image(url=winner.poster_url)
    message = await channel.send(content=None if is_test else _everyone(), embed=_with_warning(embed, warning), view=view)
    if started:
        _track("public", message, embed, warning)
        await _post_admin_now_playing(guild, winner.title, winner.year)
        if not is_test:
            await _dm_admin_links(guild)
    _clear_vote_state()
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
        if movie.rating_key:
            # From the player's own library (Movie Night only mode): play
            # exactly that item rather than searching by title again.
            return await mpv_control.play_by_rating_key(movie.rating_key)
        return await mpv_control.play(movie.title, movie.year)
    except Exception as exc:  # noqa: BLE001
        await log(f"movie_night: neko-mpv automation failed: {exc!r}")
        return False


async def stop_movie() -> bool:
    """/stop-movie's actual logic. Best-effort, same reasoning as
    start_in_neko above."""
    if not settings.neko_mpv_shim_url:
        return False
    try:
        return await mpv_control.stop()
    except Exception as exc:  # noqa: BLE001
        await log(f"movie_night: stopping playback failed: {exc!r}")
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
    warning = ""
    if started:
        embed.description = f"**{movie.title}**" + (f" ({movie.year})" if movie.year else "") + "\nIt's already loading up, click below to watch together!"
        view = discord.ui.View()
        watch_url = mpv_control.viewer_link()
        if watch_url:
            view.add_item(discord.ui.Button(label="▶️  WATCH LIVE", style=discord.ButtonStyle.link, url=watch_url))
        add_transport_buttons(view)
        warning = await _current_warning()
    else:
        search_url = "https://app.plex.tv/desktop#!/search?query=" + urllib.parse.quote(movie.title)
        embed.description = f"**{movie.title}**" + (f" ({movie.year})" if movie.year else "") + "\nCouldn't start it automatically — grab it yourself in Plex instead."
        embed.add_field(name="Find it in Plex", value=f"[Search for it]({search_url})", inline=False)
    if movie.poster_url:
        embed.set_image(url=movie.poster_url)
    message = await channel.send(embed=_with_warning(embed, warning), view=view)
    if started:
        _track("public", message, embed, warning)
    if started and getattr(channel, "guild", None) is not None:
        await _post_admin_now_playing(channel.guild, movie.title, movie.year)
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
    if features.is_movienight():
        try:
            matches = await player_library.find_movies(query)
        except Exception as exc:  # noqa: BLE001
            await log(f"movie_night: searching the player's library failed: {exc}")
            await interaction.followup.send("⚠️ Couldn't search the Movie Night player's library right now. Is the player running and paired?", ephemeral=True)
            return
        not_found = f"Couldn't find **{query}** in the Plex library."
    else:
        candidates = await radarr.list_downloaded_movies()
        normalized = query.strip().lower()
        matches = [m for m in candidates if normalized in m.title.lower()]
        not_found = f"Couldn't find a downloaded movie matching **{query}**."
    if not matches:
        await interaction.followup.send(not_found, ephemeral=True)
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
        if features.is_movienight():
            try:
                candidates = await player_library.random_movies(1)
            except Exception as exc:  # noqa: BLE001
                await log(f"movie_night: couldn't read the Movie Night player's library: {exc}")
                return False
        else:
            candidates = await radarr.list_downloaded_movies()
        if not candidates:
            return False
        _current_vote.clear()
        _current_vote["candidates"] = [random.choice(candidates)]
        _current_vote["votes"] = {}
        _save_vote_state()
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
        if not settings.movie_night_daily_vote:
            continue  # automatic votes are switched off; /movie-night still posts one
        for guild in _client.guilds:
            try:
                await post_vote(guild)
            except Exception as exc:  # noqa: BLE001
                await log(f"movie_night: posting the vote failed: {exc}")


async def announce_loop() -> None:
    await _wait_for_client()
    while True:
        await _sleep_until_hour_utc(settings.movie_night_hour_utc)
        if not settings.movie_night_daily_vote and not _current_vote.get("candidates"):
            continue  # nothing automatic, and no vote was started by hand
        for guild in _client.guilds:
            try:
                await announce_winner(guild)
            except Exception as exc:  # noqa: BLE001
                await log(f"movie_night: announcing the winner failed: {exc}")
