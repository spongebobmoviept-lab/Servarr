import asyncio
import datetime
from typing import Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import tasks
from discord.utils import MISSING

from . import features, gating, invite, leveling, moderation, movie_night, mpv_control, release_digest, theming, xp_store
from .calendar_embeds import build_upcoming_embed
from .config import settings
from .logger import log
from .radarr import get_calendar as radarr_calendar
from .sonarr import get_calendar as sonarr_calendar

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True  # needed to actually preserve/relocate what someone typed in a read-only channel
intents.members = True  # needed to resolve display names for the leaderboard

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
# The persistent views (rules gate, Movie Night control panel) are
# registered in _register_views() each time the bot connects.

# In-memory, resets on restart — deliberately not persisted. This is just to
# avoid DM-spamming someone who fires off several stray messages in a row;
# it doesn't need to survive a redeploy to do its job.
_already_warned: set[int] = set()


def _level_up_channel(guild: discord.Guild, fallback: discord.abc.Messageable) -> discord.abc.Messageable:
    return discord.utils.get(guild.text_channels, name="level-ups") or fallback


async def _mod_log(guild: discord.Guild, text: str) -> None:
    channel = discord.utils.get(guild.text_channels, name="mod-log")
    if channel is not None:
        try:
            await channel.send(text)
        except Exception as exc:  # noqa: BLE001
            await log(f"bot: couldn't post to mod-log: {exc}")


_LEADERBOARD_MARKER = "live-leaderboard-v1"


async def refresh_leaderboard_board(guild: discord.Guild) -> None:
    """Self-updating top-10 board in the leaderboard channel, so the channel shows
    current standings at a glance instead of sitting empty between people
    manually running /leaderboard. Idempotent by footer marker, same
    pattern as movie_night's commander panel — edits the existing post
    in place rather than spamming a new one each tick/restart.
    """
    channel = discord.utils.get(guild.text_channels, name=settings.leaderboard_channel)
    if channel is None:
        return
    entries = await xp_store.store.get_leaderboard(10)

    def resolve_name(user_id: int) -> str:
        member = guild.get_member(user_id)
        return member.display_name if member else f"User {user_id}"

    embed = leveling.leaderboard_embed(entries, resolve_name)
    embed.set_footer(text=_LEADERBOARD_MARKER)
    embed.timestamp = discord.utils.utcnow()
    try:
        async for old in channel.history(limit=20):
            if old.author == client.user and old.embeds and old.embeds[0].footer and old.embeds[0].footer.text == _LEADERBOARD_MARKER:
                await old.edit(embed=embed)
                return
        await channel.send(embed=embed)
    except Exception as exc:  # noqa: BLE001
        await log(f"bot: couldn't refresh #{settings.leaderboard_channel} board: {exc}")


@tasks.loop(minutes=10)
async def _leaderboard_tick() -> None:
    if features.is_movienight():
        return
    for guild in client.guilds:
        await refresh_leaderboard_board(guild)


async def _relocate_stray_message(message: discord.Message) -> None:
    """A message landed in a read-only info channel — move it to #general
    (reposted via webhook under the original author's name/avatar) instead
    of just deleting it outright, then remove the original.
    """
    general = discord.utils.get(message.guild.text_channels, name="general")
    if general is not None:
        try:
            webhooks = await general.webhooks()
            webhook = discord.utils.get(webhooks, name="Servarr Relay") or await general.create_webhook(name="Servarr Relay")
            content = message.content or "*(no text)*"
            if message.attachments:
                content += "\n" + "\n".join(a.url for a in message.attachments)
            await webhook.send(content=content, username=message.author.display_name, avatar_url=message.author.display_avatar.url)
        except Exception as exc:  # noqa: BLE001
            await log(f"bot: couldn't relocate message to #general: {exc}")
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001
        await log(f"bot: couldn't remove message in read-only #{message.channel.name}: {exc}")


@client.event
async def on_message(message: discord.Message) -> None:
    # Movie Night only mode: no leveling, read-only channels or calendar
    # boards, so there's nothing to do with ordinary messages.
    if features.is_movienight():
        return
    # Checked before the bot-author return below — the thing this needs to
    # react to (other bots' webhook posts landing in the release channels)
    # IS a bot/webhook author. Without this, the calendar digest board
    # would scroll out of view under their activity within minutes.
    release_digest.on_channel_message(message)

    if message.author.bot or message.guild is None:
        return

    # Belt-and-suspenders — the Start Here channels already deny
    # send_messages for @everyone via gating.lock_down_server, so this
    # should rarely fire. But permission overwrites can be missed (a
    # channel created before setup ran, a role misconfigured, etc.), and
    # silently leaving a stray message there is worse than a redundant check.
    if isinstance(message.channel, discord.TextChannel) and message.channel.name in gating.read_only_channel_names():
        source_channel = message.channel.name
        author = message.author
        await _relocate_stray_message(message)
        quoted = (message.content or "*(no text)*")[:500]
        if message.attachments:
            quoted += "\n" + "\n".join(a.url for a in message.attachments)
        await _mod_log(
            message.guild,
            f"↪️ Moved a message from **{author}** in #{source_channel} to #general (that channel's read-only).\n> {quoted}",
        )

        if author.id not in _already_warned:
            _already_warned.add(author.id)
            member_role = discord.utils.get(message.guild.roles, name=gating.MEMBER_ROLE_NAME)
            if member_role and member_role in author.roles:
                where = "#general is open for chat"
            else:
                where = "check #rules and click 'I Agree' to unlock #general for chat"
            try:
                await author.send(
                    f"#{source_channel} is read-only — that one's for information/notifications, not chat. I moved your message to #general. "
                    f"You can still browse and use commands (`/movie`, `/tv`, `/anime`, `/rank`, etc.) anywhere — {where} for actual conversation."
                )
            except Exception:  # noqa: BLE001 — DMs closed is common and not worth logging as an error
                pass
        return

    result = await xp_store.store.try_add_xp(message.author.id)
    if result is None:
        return
    old_level, new_level, _total_xp = result
    if new_level > old_level:
        channel = _level_up_channel(message.guild, message.channel)
        try:
            await channel.send(embed=leveling.level_up_embed(message.author, new_level))
        except Exception as exc:  # noqa: BLE001
            await log(f"bot: failed to post level-up for {message.author}: {exc}")


# ---------------------------------------------------------------------------
# Phase 1 — release calendar
# ---------------------------------------------------------------------------


@tree.command(name="upcoming", description="Show upcoming movie/TV/anime releases")
@app_commands.describe(days="How many days ahead to look (default 14)")
async def upcoming_command(interaction: discord.Interaction, days: int = 14) -> None:
    await interaction.response.defer()
    start = datetime.date.today().isoformat()
    end = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    episodes = await sonarr_calendar(start, end)
    movies = await radarr_calendar(start, end)
    embed = build_upcoming_embed(episodes, movies, days)
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Phase 2 — leveling
# ---------------------------------------------------------------------------


@tree.command(name="rank", description="Show your (or someone else's) activity rank")
@app_commands.describe(member="Whose rank to show (defaults to you)")
async def rank_command(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
    target = member or interaction.user
    info = await xp_store.store.get_rank(target.id)
    if info is None:
        await interaction.response.send_message(f"{target.display_name} hasn't sent any messages yet.", ephemeral=True)
        return
    embed = leveling.rank_embed(target, info["xp"], info["level"], info["position"], info["total_members"])
    await interaction.response.send_message(embed=embed)


@tree.command(name="leaderboard", description="Show the server activity leaderboard")
async def leaderboard_command(interaction: discord.Interaction) -> None:
    entries = await xp_store.store.get_leaderboard(10)

    def resolve_name(user_id: int) -> str:
        member = interaction.guild.get_member(user_id) if interaction.guild else None
        return member.display_name if member else f"User {user_id}"

    embed = leveling.leaderboard_embed(entries, resolve_name)
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Phase 3 — theming/setup (never runs blind — preview, then explicit confirm)
# ---------------------------------------------------------------------------


@tree.command(name="setup-server", description="Preview and optionally apply the suggested server layout (Movies/TV/Anime/Community)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_server_command(interaction: discord.Interaction) -> None:
    embed = theming.build_proposal_embed()
    view = theming.SetupConfirmView(interaction.guild, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# Phase 4 — moderation
# ---------------------------------------------------------------------------


@tree.command(name="kick", description="Kick a member")
@app_commands.describe(member="Who to kick", reason="Why")
@app_commands.checks.has_permissions(kick_members=True)
async def kick_command(interaction: discord.Interaction, member: discord.Member, reason: str = "") -> None:
    await moderation.do_kick(client, member, interaction.user, reason)
    await interaction.response.send_message(f"👢 Kicked {member.mention}.", ephemeral=True)


@tree.command(name="ban", description="Ban a member")
@app_commands.describe(member="Who to ban", reason="Why")
@app_commands.checks.has_permissions(ban_members=True)
async def ban_command(interaction: discord.Interaction, member: discord.Member, reason: str = "") -> None:
    await moderation.do_ban(client, member, interaction.user, reason)
    await interaction.response.send_message(f"🔨 Banned {member.mention}.", ephemeral=True)


@tree.command(name="timeout", description="Timeout (mute) a member")
@app_commands.describe(member="Who to time out", minutes="How many minutes", reason="Why")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout_command(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "") -> None:
    await moderation.do_timeout(client, member, interaction.user, minutes, reason)
    await interaction.response.send_message(f"🔇 Timed out {member.mention} for {minutes}m.", ephemeral=True)


@tree.command(name="warn", description="Warn a member (DMs them and logs it)")
@app_commands.describe(member="Who to warn", reason="Why")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn_command(interaction: discord.Interaction, member: discord.Member, reason: str = "") -> None:
    dm_sent = await moderation.do_warn(client, member, interaction.user, reason)
    note = "" if dm_sent else " (couldn't DM them — they may have DMs closed)"
    await interaction.response.send_message(f"⚠️ Warned {member.mention}.{note}", ephemeral=True)


@tree.command(name="refresh-calendar", description="Mod-only: post the release calendar to its channels right now (testing)")
@app_commands.checks.has_permissions(manage_guild=True)
async def refresh_calendar_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await release_digest.post_once(interaction.guild)
    await interaction.followup.send("✅ Posted (channels with nothing scheduled are skipped).", ephemeral=True)


@tree.command(name="movie-night", description="Mod-only: manually start tonight's Movie Night vote right now (testing)")
@app_commands.checks.has_permissions(manage_guild=True)
async def movie_night_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    started = await movie_night.post_vote(interaction.guild)
    where = movie_night.vote_channel_label(interaction.guild)
    if features.is_movienight():
        failed = f"⚠️ Couldn't start it — check the log (need 2+ movies in the player's Plex library and {where})."
    else:
        failed = "⚠️ Couldn't start it — check the log (need 2+ downloaded movies and a #general channel)."
    await interaction.followup.send(f"✅ Vote posted in {where}." if started else failed, ephemeral=True)


@tree.command(name="movie-night-play", description="Mod-only: immediately tally (or pick) and start playing tonight's movie right now (testing)")
@app_commands.checks.has_permissions(manage_guild=True)
async def movie_night_play_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    started = await movie_night.force_winner_now(interaction.guild)
    where = movie_night.vote_channel_label(interaction.guild)
    need = "at least 1 movie in the player's Plex library" if features.is_movienight() else "at least 1 downloaded movie"
    await interaction.followup.send(f"✅ Announced in {where} and sent to Neko." if started else f"⚠️ Couldn't start it — check the log (need {need}).", ephemeral=True)


def _is_movie_night_dj(interaction: discord.Interaction) -> bool:
    """Manage Server always works; the configurable DJ role (see /setup's
    Movie Night section) extends the same privilege to specific people
    without handing them full mod permissions.

    Accepts either the role's numeric ID or its plain name (case-insensitive)
    — people naturally type the name (e.g. "Movie Night DJ").
    """
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.manage_guild:
        return True
    configured = settings.movie_night_dj_role_id.strip()
    if not configured:
        return False
    if configured.isdigit():
        return any(r.id == int(configured) for r in interaction.user.roles)
    return any(r.name.lower() == configured.lower() for r in interaction.user.roles)


@tree.command(name="play-movie", description="Start playing a specific downloaded movie on the shared Neko browser right now")
@app_commands.describe(title="Which movie — searches your downloaded library")
@app_commands.check(_is_movie_night_dj)
async def play_movie_command(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer(ephemeral=True)
    await movie_night.play_specific(interaction, title)


@tree.command(name="stop-movie", description="Stop whatever's currently playing on the shared Neko browser")
@app_commands.check(_is_movie_night_dj)
async def stop_movie_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    stopped = await movie_night.stop_movie()
    await interaction.followup.send("⏹️ Stopped." if stopped else "⚠️ Couldn't stop it — check the log.", ephemeral=True)


def _fmt_time(ms: int) -> str:
    total_seconds = ms // 1000
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _remote_link_works() -> bool:
    """Movie Night only mode: no web-remote button while Servarr's address
    is still a localhost one (a link nobody but the host could open)."""
    if not features.is_movienight():
        return True
    return (urlparse(settings.servarr_public_url).hostname or "localhost") not in ("localhost", "127.0.0.1", "::1")


@tree.command(name="mpv-play", description="Start playing a movie on the neko-mpv player (hardware-accelerated, no on-screen controls)")
@app_commands.describe(title="Which movie — searches your downloaded library")
@app_commands.check(_is_movie_night_dj)
async def mpv_play_command(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer(ephemeral=True)
    started = await mpv_control.play(title)
    if not started:
        await interaction.followup.send(f"Couldn't find **{title}** in the library.", ephemeral=True)
        return
    view = discord.ui.View()
    # This reply is ephemeral — only the DJ who ran the command sees
    # it — so they get the admin-level auto-login link (from the Movie
    # Night player, or built from the Companion player's watch page).
    watch_url = mpv_control.admin_link()
    if watch_url:
        view.add_item(discord.ui.Button(label="▶️  WATCH LIVE", style=discord.ButtonStyle.link, url=watch_url))
    if settings.mpv_remote_key and _remote_link_works():
        # This reply is ephemeral (only the person who ran the command sees
        # it). The key rides in the URL fragment (#key=...), which browsers
        # never send to the server, so it can't land in access logs or
        # proxies; the page moves it into a header and strips it from the
        # address bar (see static/mpv-remote.html and auth.py).
        remote_url = f"{settings.servarr_public_url}/mpv-remote#key={settings.mpv_remote_key}"
        view.add_item(discord.ui.Button(label="🎮  REMOTE CONTROLS", style=discord.ButtonStyle.link, url=remote_url))
    await interaction.followup.send(
        f"Starting **{title}**… use `/mpv-pause`, `/mpv-seek`, `/mpv-stop`, or the remote link above to control it.",
        view=view if view.children else None,
        ephemeral=True,
    )


@tree.command(name="mpv-pause", description="Pause the neko-mpv player")
@app_commands.check(_is_movie_night_dj)
async def mpv_pause_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    ok = await mpv_control.pause()
    await interaction.followup.send("⏸️ Paused." if ok else "⚠️ Nothing's playing right now.", ephemeral=True)


@tree.command(name="mpv-resume", description="Resume the neko-mpv player")
@app_commands.check(_is_movie_night_dj)
async def mpv_resume_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    ok = await mpv_control.resume()
    await interaction.followup.send("▶️ Resumed." if ok else "⚠️ Nothing's paused right now.", ephemeral=True)


@tree.command(name="mpv-seek", description="Jump forward or back in the neko-mpv player")
@app_commands.describe(direction="Which way", seconds="How many seconds (default 10)")
@app_commands.choices(direction=[
    app_commands.Choice(name="back", value="back"),
    app_commands.Choice(name="forward", value="forward"),
])
@app_commands.check(_is_movie_night_dj)
async def mpv_seek_command(interaction: discord.Interaction, direction: app_commands.Choice[str], seconds: int = 10) -> None:
    await interaction.response.defer(ephemeral=True)
    delta = seconds if direction.value == "forward" else -seconds
    position_seconds = await mpv_control.seek(delta)
    if position_seconds is None:
        await interaction.followup.send("⚠️ Nothing's loaded right now.", ephemeral=True)
        return
    arrow = "⏩" if delta > 0 else "⏪"
    await interaction.followup.send(f"{arrow} Now at {_fmt_time(position_seconds * 1000)}.", ephemeral=True)


@tree.command(name="mpv-stop", description="Stop the neko-mpv player")
@app_commands.check(_is_movie_night_dj)
async def mpv_stop_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    ok = await mpv_control.stop()
    await interaction.followup.send("⏹️ Stopped." if ok else "⚠️ Couldn't stop it — check the log.", ephemeral=True)


def _movie_night_help_embed(guild: Optional[discord.Guild]) -> discord.Embed:
    where = movie_night.vote_channel_label(guild) if guild is not None else "the Movie Night channel"
    embed = discord.Embed(
        title="🍿 Movie Night",
        description=f"Watch movies together in your browser. Everything is posted in {where}.",
        color=0xD4AF37,
    )
    embed.add_field(
        name="Start and control a movie",
        value=(
            "`/play-movie <title>` — search the Plex library and start a movie for everyone (you also get a private control link)\n"
            "`/stop-movie` — stop it\n"
            "`/mpv-play <title>` `/mpv-pause` `/mpv-resume` `/mpv-seek <back/forward> [seconds]` `/mpv-stop` — the same controls as commands\n"
            "Need Manage Server or the Movie Night DJ role."
        ),
        inline=False,
    )
    if settings.movie_night_daily_vote:
        vote = (
            f"A vote posts every day at {settings.movie_night_vote_hour_utc}:00 UTC and the winner starts at "
            f"{settings.movie_night_hour_utc}:00 UTC. `/movie-night` posts one now and `/movie-night-play` starts the pick now (Manage Server)."
        )
    else:
        vote = "`/movie-night` posts a vote and `/movie-night-play` tallies it and starts the winner (Manage Server)."
    embed.add_field(name="The vote", value=vote, inline=False)
    embed.add_field(name="Watching", value="Click **WATCH LIVE** on the Now playing message, pick a name, and you're in.", inline=False)
    return embed


@tree.command(name="help", description="List everything this server's bots can do")
async def help_command(interaction: discord.Interaction) -> None:
    if features.is_movienight():
        await interaction.response.send_message(embed=_movie_night_help_embed(interaction.guild), ephemeral=True)
        return
    embed = discord.Embed(
        title="🤖 What can I do here?",
        description="New? Check #start-here first — this is just a quick command reference.",
        color=0xD4AF37,
    )
    embed.add_field(
        name="🎬 Requesting something (Requestarr)",
        value="`/movie <title>` · `/tv <title>` · `/anime <title>`\nEach shows you a picker to confirm the exact one before anything's added — see #how-to-request.",
        inline=False,
    )
    embed.add_field(
        name="📅 What's coming up (Servarr)",
        value="`/upcoming [days]` — upcoming movie/TV/anime releases already tracked.",
        inline=False,
    )
    embed.add_field(
        name="🏆 Activity (Servarr)",
        value="`/rank [member]` — your level/XP\n`/leaderboard` — top 10 most active members",
        inline=False,
    )
    embed.add_field(
        name="🍿 Movie Night (Servarr)",
        value="`/play-movie <title>` — pick a downloaded movie and start it on the shared watch-party browser right now (whoever runs it also gets a private control link). `/stop-movie` — stop whatever's currently playing. Both need Manage Server or the Movie Night DJ role.",
        inline=False,
    )
    embed.add_field(
        name="🎮 Movie Night (hardware player, experimental)",
        value="`/mpv-play <title>` `/mpv-pause` `/mpv-resume` `/mpv-seek <back/forward> [seconds]` `/mpv-stop` — same permissions as above. This player has no on-screen controls at all, so these (or the remote page) are the only way to control it.",
        inline=False,
    )
    embed.add_field(
        name="🛡️ Moderator-only (Servarr)",
        value="`/kick` `/ban` `/timeout` `/warn` `/setup-server` — each requires the matching real Discord permission on your account.",
        inline=False,
    )
    embed.add_field(name="📺 Live status", value="Check #now-playing for what's currently being watched.", inline=False)
    where = movie_night.vote_channel_label(interaction.guild) if interaction.guild is not None else "#general"
    embed.add_field(name="🍿 Movie Night", value=f"A vote posts in {where} every evening — winner's announced at showtime, everyone watches on their own device.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CommandNotFound) and features.is_movienight():
        # A leftover command from Everything mode that Discord still shows.
        await interaction.response.send_message("That command is turned off here (Servarr runs Movie Night only).", ephemeral=True)
        return
    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
        # The user only ever sees the line above (ephemeral) — mods
        # otherwise have zero visibility into who's trying to run
        # gated commands (mpv-play, kick, etc.) without permission.
        # Movie Night only mode has no moderation, so no mod-log posts.
        if interaction.guild is not None and interaction.command is not None and not features.is_movienight():
            await _mod_log(interaction.guild, f"🚫 {interaction.user.mention} tried `/{interaction.command.name}` — blocked (no permission)")
        return
    await log(f"bot: command error: {error}")
    if interaction.response.is_done():
        await interaction.followup.send("Something went wrong running that command.", ephemeral=True)
    else:
        await interaction.response.send_message("Something went wrong running that command.", ephemeral=True)


# Every slash command defined above, captured once. sync_commands() rebuilds
# the tree from this list, so switching modes can add commands back.
_ALL_COMMANDS = list(tree.get_commands())


def command_names(mode: Optional[str] = None) -> list[str]:
    """The slash commands synced in a mode (Movie Night only drops the
    calendar, leveling, theming and moderation commands)."""
    mode = features.normalize(mode) or features.current()
    return [c.name for c in _ALL_COMMANDS if mode != features.MOVIENIGHT or c.name not in features.FULL_ONLY_COMMANDS]


_synced_guild_id = ""


def _picked_guild_id() -> str:
    guild_id = str(settings.discord_guild_id or "").strip()
    return guild_id if guild_id.isdigit() else ""


async def sync_commands() -> None:
    """Registers this mode's commands with Discord: in the picked server
    (instant, and any old global commands are cleared) or globally."""
    global _synced_guild_id
    wanted = set(command_names())
    commands = [c for c in _ALL_COMMANDS if c.name in wanted]
    guild_id = _picked_guild_id()
    stale = _synced_guild_id if _synced_guild_id != guild_id else ""

    # Rebuild the tree in one go (no awaits), so a command used mid-sync is still found.
    tree.clear_commands(guild=None)
    if stale:
        tree.clear_commands(guild=discord.Object(id=int(stale)))
    if guild_id:
        guild = discord.Object(id=int(guild_id))
        tree.clear_commands(guild=guild)
        for command in commands:
            tree.add_command(command, guild=guild)
    else:
        for command in commands:
            tree.add_command(command)

    if guild_id:
        await tree.sync(guild=guild)
        await tree.sync()
        await log(f"servarr: synced commands to guild {guild_id} (instant); cleared global commands")
    else:
        await tree.sync()
        await log("servarr: synced commands globally (can take up to an hour to propagate on first use)")
    _synced_guild_id = guild_id
    if stale:
        # Another server was picked: take the commands out of the old one.
        try:
            await tree.sync(guild=discord.Object(id=int(stale)))
        except discord.HTTPException:
            pass  # the bot may not be in that server any more


@client.event
async def on_ready() -> None:
    await log(f"servarr: logged in as {client.user}")
    try:
        await sync_commands()
    except Exception as exc:  # noqa: BLE001
        await log(f"servarr: couldn't sync slash commands: {exc} (was the bot invited with the applications.commands scope, and is it in the picked server?)")
    for guild in client.guilds:
        try:
            await movie_night.refresh_commander_panel(guild)
        except Exception as exc:  # noqa: BLE001
            await log(f"servarr: couldn't refresh commander panel for {guild}: {exc}")
        if features.is_movienight():
            continue
        try:
            await release_digest.seed_pinned_tile(guild)
        except Exception as exc:  # noqa: BLE001
            await log(f"servarr: couldn't seed the pinned tile: {exc}")
    if features.is_movienight():
        if _leaderboard_tick.is_running():
            _leaderboard_tick.cancel()
    elif not _leaderboard_tick.is_running():
        _leaderboard_tick.start()


# ---------------------------------------------------------------------------
# connecting, and reconnecting when the setup page saves a token or mode
# ---------------------------------------------------------------------------

_runner: Optional[asyncio.Task] = None
_restart_lock = asyncio.Lock()
_pending: set[asyncio.Task] = set()
# Movie Night only mode: Discord refused the Server Members intent, so
# connect without it (DJs then don't get their player link by DM).
_members_fallback = False

_FULL_INTENTS_HELP = (
    "servarr: login rejected — one or both privileged intents aren't enabled for this bot application. "
    "Go to https://discord.com/developers/applications -> your app -> Bot -> Privileged Gateway Intents, "
    "enable BOTH 'Server Members Intent' (for /leaderboard names) and 'Message Content Intent' "
    "(for relocating stray messages out of read-only channels), then restart."
)
_TOKEN_REJECTED = (
    "servarr: Discord rejected the bot token. Paste a new one on the setup page "
    "(Developer Portal -> your app -> Bot -> Reset Token)."
)
_RETRY_SECONDS = (15, 30, 60, 120, 300)


def _configure_intents() -> None:
    """Everything mode asks for both privileged intents, as before. Movie
    Night only mode never reads message text, so it drops Message Content,
    and asks for Server Members only to DM DJs their player link."""
    if features.is_movienight():
        intents.message_content = False
        intents.members = not _members_fallback
    else:
        intents.message_content = True
        intents.members = True


def _register_views() -> None:
    """Persistent views: their buttons keep working on old messages after a
    restart. Called before every connect (reconnecting clears them)."""
    if not features.is_movienight():
        client.add_view(gating.RulesGateView())
    client.add_view(movie_night.CommanderPanelView())


def _reopen() -> None:
    """Lets a closed client log in again. clear() re-opens it; its HTTP
    connector was closed along with the old session, so drop it and the
    next login makes a new one."""
    connector = client.http.connector
    if connector is not MISSING and connector.closed:
        client.http.connector = MISSING
    client.clear()


async def _connect(token: str) -> None:
    if client.is_closed():
        _reopen()
    _configure_intents()
    _register_views()
    async with client:  # (re)initialises the client's event-loop state; closes it on the way out
        await client.start(token)


async def start() -> None:
    """Runs the bot until it's stopped (restart/shutdown) or Discord refuses
    the login. If Discord can't be reached (network not up yet, an outage)
    it keeps trying."""
    global _members_fallback
    token = settings.discord_bot_token
    if not token:
        await log("servarr: no Discord bot token yet — the bot connects as soon as you save one on the setup page (or set DISCORD_TOKEN).")
        return
    attempt = 0
    while True:
        try:
            await _connect(token)
            return
        except discord.PrivilegedIntentsRequired:
            if not features.is_movienight() or _members_fallback:
                await log(_FULL_INTENTS_HELP)
                return
            _members_fallback = True
            await log(
                "servarr: the bot's Server Members Intent is off, so it connects without it. Movie Night works; "
                "DJs just don't get their player link by DM at showtime. To get that back, turn on Server Members Intent "
                "(Developer Portal -> your app -> Bot -> Privileged Gateway Intents)."
            )
        except discord.LoginFailure:
            await log(_TOKEN_REJECTED)
            return
        except Exception as exc:  # noqa: BLE001 — Discord unreachable; discord.py already retried a few times
            delay = _RETRY_SECONDS[min(attempt, len(_RETRY_SECONDS) - 1)]
            attempt += 1
            await log(f"servarr: couldn't connect to Discord ({exc.__class__.__name__}: {exc}) — trying again in {delay}s")
            await asyncio.sleep(delay)


def launch() -> asyncio.Task:
    """Starts the bot in the background unless it's already running."""
    global _runner
    if _runner is None or _runner.done():
        _runner = asyncio.create_task(start(), name="servarr:bot")
    return _runner


async def _stop_runner() -> None:
    """Disconnects cleanly: cancelling the task unwinds through `async with
    client`, which closes the gateway and the HTTP session."""
    task = _runner
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.wait({task}, timeout=20)


async def restart() -> None:
    """Connects with the saved token and the current mode, disconnecting
    first if the bot was already running."""
    global _members_fallback
    async with _restart_lock:
        await _stop_runner()
        _members_fallback = False  # new token or mode: try the Server Members intent again
        if _runner is not None and not _runner.done():
            # Still disconnecting (a slow network close): connect once that's finished.
            _runner.add_done_callback(lambda _task: launch())
            return
        launch()


def request_restart() -> None:
    """Schedules restart() without making the caller (the setup API) wait
    for Discord."""
    task = asyncio.create_task(restart(), name="servarr:bot-restart")
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _resync() -> None:
    try:
        await sync_commands()
    except Exception as exc:  # noqa: BLE001
        await log(f"servarr: couldn't sync slash commands: {exc}")


def request_resync() -> None:
    """Re-registers the slash commands (e.g. after picking another server)."""
    if not client.is_ready():
        return  # on_ready syncs them when the bot connects
    task = asyncio.create_task(_resync(), name="servarr:command-sync")
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _refresh_panels() -> None:
    for guild in client.guilds:
        try:
            await movie_night.refresh_commander_panel(guild)
        except Exception as exc:  # noqa: BLE001
            await log(f"servarr: couldn't refresh commander panel for {guild}: {exc}")


def request_panel_refresh() -> None:
    """Posts the DJ control panel where it's missing: after a player gets
    paired or the control channel is picked, instead of at the next restart."""
    if not client.is_ready():
        return  # on_ready posts it when the bot connects
    task = asyncio.create_task(_refresh_panels(), name="servarr:panel-refresh")
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def shutdown() -> None:
    await _stop_runner()


def status() -> dict:
    """For the setup page. Never includes the token."""
    ready = client.is_ready()
    return {
        "token_set": bool(settings.discord_bot_token),
        "connecting": bool(_runner is not None and not _runner.done() and not ready),
        "connected": ready,
        "user": str(client.user) if ready and client.user else None,
        "servers": len(client.guilds) if ready else 0,
    }


def target_guild() -> Optional[discord.Guild]:
    """The server picked in the setup page if the bot is in it, else the first one."""
    if not client.guilds:
        return None
    if _picked_guild_id():
        picked = client.get_guild(int(_picked_guild_id()))
        if picked is not None:
            return picked
    return client.guilds[0]


def channel_problems(channel_id: str) -> Optional[list[str]]:
    """What the bot can't do in the Movie Night channel (permission names),
    or None when it can't tell (bot not connected yet)."""
    if not client.is_ready() or not str(channel_id).isdigit():
        return None
    channel = client.get_channel(int(channel_id))
    if channel is None:
        return ["View Channels"] if client.guilds else None
    perms = channel.permissions_for(channel.guild.me)
    return [name for name, attr, _ in invite.MOVIE_NIGHT_PERMISSIONS if not getattr(perms, attr, False)]

