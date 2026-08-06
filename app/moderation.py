"""Phase 4 — kick/ban/timeout/warn. Each command in bot.py is gated by
app_commands.checks.has_permissions against the INVOKING user's real Discord
permissions (Discord's own permission system, not a role name we'd have to
maintain ourselves). This module just does the action + logs it.
"""

import datetime

import discord

from .config import settings
from .logger import log

RED = 0xE05555


async def _post_log(client: discord.Client, embed: discord.Embed) -> None:
    if not settings.mod_log_channel_id:
        return
    try:
        channel = client.get_channel(int(settings.mod_log_channel_id)) or await client.fetch_channel(int(settings.mod_log_channel_id))
        await channel.send(embed=embed)
    except Exception as exc:  # noqa: BLE001
        await log(f"moderation: failed to post to mod-log channel: {exc}")


def _action_embed(action: str, target: discord.Member, moderator: discord.Member, reason: str) -> discord.Embed:
    embed = discord.Embed(title=f"🛡️ {action}", color=RED)
    embed.add_field(name="User", value=f"{target.mention} ({target})", inline=True)
    embed.add_field(name="Moderator", value=moderator.mention, inline=True)
    embed.add_field(name="Reason", value=reason or "No reason given", inline=False)
    return embed


async def do_kick(client: discord.Client, target: discord.Member, moderator: discord.Member, reason: str) -> None:
    await target.kick(reason=f"{moderator}: {reason}" if reason else str(moderator))
    await _post_log(client, _action_embed("Kicked", target, moderator, reason))
    await log(f"moderation: {moderator} kicked {target} — {reason}")


async def do_ban(client: discord.Client, target: discord.Member, moderator: discord.Member, reason: str) -> None:
    await target.ban(reason=f"{moderator}: {reason}" if reason else str(moderator))
    await _post_log(client, _action_embed("Banned", target, moderator, reason))
    await log(f"moderation: {moderator} banned {target} — {reason}")


async def do_timeout(client: discord.Client, target: discord.Member, moderator: discord.Member, minutes: int, reason: str) -> None:
    until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
    await target.timeout(until, reason=f"{moderator}: {reason}" if reason else str(moderator))
    embed = _action_embed(f"Timed out ({minutes}m)", target, moderator, reason)
    await _post_log(client, embed)
    await log(f"moderation: {moderator} timed out {target} for {minutes}m — {reason}")


async def do_warn(client: discord.Client, target: discord.Member, moderator: discord.Member, reason: str) -> bool:
    """Discord has no native 'warning' object — this DMs the user (best
    effort, they may have DMs closed) and always logs it regardless.
    Returns whether the DM actually went through.
    """
    dm_sent = True
    try:
        await target.send(f"You've received a warning in **{target.guild.name}**: {reason or 'No reason given'}")
    except Exception:  # noqa: BLE001
        dm_sent = False
    await _post_log(client, _action_embed("Warned", target, moderator, reason))
    await log(f"moderation: {moderator} warned {target} — {reason} (DM {'sent' if dm_sent else 'failed/closed'})")
    return dm_sent
