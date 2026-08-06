import datetime

import discord
from discord import app_commands

from . import gating, leveling, moderation, movie_night, release_digest, theming, xp_store
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
client.add_view(gating.RulesGateView())  # persistent — keeps working on old messages across restarts

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
    if message.author.bot or message.guild is None:
        return

    # Belt-and-suspenders — the Start Here channels already deny
    # send_messages for @everyone via gating.lock_down_server, so this
    # should rarely fire. But permission overwrites can be missed (a
    # channel created before setup ran, a role misconfigured, etc.), and
    # silently leaving a stray message there is worse than a redundant check.
    if isinstance(message.channel, discord.TextChannel) and message.channel.name in gating.READ_ONLY_CHANNEL_NAMES:
        source_channel = message.channel.name
        author = message.author
        await _relocate_stray_message(message)
        await _mod_log(message.guild, f"↪️ Moved a message from **{author}** in #{source_channel} to #general (that channel's read-only).")

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


@tree.command(name="refresh-calendar", description="Mod-only: post the release calendar to #movie-releases/#tv-releases/#anime-releases right now (testing)")
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
    await interaction.followup.send("✅ Vote posted in #general." if started else "⚠️ Couldn't start it — check the log (need 2+ downloaded movies and a #general channel).", ephemeral=True)


@tree.command(name="movie-night-play", description="Mod-only: immediately tally (or pick) and start playing tonight's movie right now (testing)")
@app_commands.checks.has_permissions(manage_guild=True)
async def movie_night_play_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    started = await movie_night.force_winner_now(interaction.guild)
    await interaction.followup.send("✅ Announced in #general and sent to Neko." if started else "⚠️ Couldn't start it — check the log (need at least 1 downloaded movie).", ephemeral=True)


@tree.command(name="help", description="List everything this server's bots can do")
async def help_command(interaction: discord.Interaction) -> None:
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
        name="🛡️ Moderator-only (Servarr)",
        value="`/kick` `/ban` `/timeout` `/warn` `/setup-server` — each requires the matching real Discord permission on your account.",
        inline=False,
    )
    embed.add_field(name="📺 Live status", value="Check #now-playing for what's currently being watched.", inline=False)
    embed.add_field(name="🍿 Movie Night", value="A vote posts in #general every evening — winner's announced at showtime, everyone watches on their own device.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
        return
    await log(f"bot: command error: {error}")
    if interaction.response.is_done():
        await interaction.followup.send("Something went wrong running that command.", ephemeral=True)
    else:
        await interaction.response.send_message("Something went wrong running that command.", ephemeral=True)


@client.event
async def on_ready() -> None:
    await log(f"servarr: logged in as {client.user}")
    if settings.discord_guild_id:
        guild = discord.Object(id=int(settings.discord_guild_id))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        tree.clear_commands(guild=None)
        await tree.sync()
        await log(f"servarr: synced commands to guild {settings.discord_guild_id} (instant); cleared global commands")
    else:
        await tree.sync()
        await log("servarr: synced commands globally (can take up to an hour to propagate on first use)")


async def start() -> None:
    if not settings.discord_bot_token:
        await log("servarr: DISCORD_BOT_TOKEN not set — bot will not connect. Set it in .env and restart once you've created a bot application (see README).")
        return
    try:
        await client.start(settings.discord_bot_token)
    except discord.PrivilegedIntentsRequired:
        await log(
            "servarr: login rejected — one or both privileged intents aren't enabled for this bot application. "
            "Go to https://discord.com/developers/applications -> your app -> Bot -> Privileged Gateway Intents, "
            "enable BOTH 'Server Members Intent' (for /leaderboard names) and 'Message Content Intent' "
            "(for relocating stray messages out of read-only channels), then restart."
        )
