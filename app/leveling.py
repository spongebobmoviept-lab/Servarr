import discord

from .xp_store import xp_for_level

GOLD = 0xD4AF37


def _progress_bar(current: int, needed: int, length: int = 12) -> str:
    filled = min(length, round((current / needed) * length)) if needed else 0
    return "█" * filled + "░" * (length - filled)


def rank_embed(member: discord.Member, xp: int, level: int, position: int, total_members: int) -> discord.Embed:
    xp_into_level = xp - sum(xp_for_level(n) for n in range(1, level + 1))
    xp_needed = xp_for_level(level + 1)
    embed = discord.Embed(title=f"{member.display_name}'s Rank", color=GOLD)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Server Rank", value=f"#{position} / {total_members}", inline=True)
    embed.add_field(name="XP", value=f"{xp_into_level} / {xp_needed}  {_progress_bar(xp_into_level, xp_needed)}", inline=False)
    return embed


def leaderboard_embed(entries: list[dict], resolve_name) -> discord.Embed:
    """entries: [{"user_id", "xp", "level"}, ...]. resolve_name(user_id) ->
    display name string, supplied by the caller since only bot.py has
    access to the guild's member cache.
    """
    embed = discord.Embed(title="🏆 Leaderboard", color=GOLD)
    if not entries:
        embed.description = "No activity yet — send some messages!"
        return embed
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, entry in enumerate(entries):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} **{resolve_name(entry['user_id'])}** — Level {entry['level']} ({entry['xp']} XP)")
    embed.description = "\n".join(lines)
    return embed


def level_up_embed(member: discord.Member, new_level: int) -> discord.Embed:
    embed = discord.Embed(title="🎉 Level Up!", description=f"{member.mention} just reached **Level {new_level}**!", color=GOLD)
    embed.set_thumbnail(url=member.display_avatar.url)
    return embed
