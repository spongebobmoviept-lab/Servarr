"""Rules gate. Explicit design constraint: everyone can always SEE
everything — releases, their own requests, live playback — without
agreeing to anything first. The only thing gated behind clicking "I Agree"
in #rules is chat access in #general. Everywhere else stays read-only
(bot/webhook posts only) rather than hidden, so notifications are visible
to everyone regardless of Member status.
"""

import discord

from .logger import log

MEMBER_ROLE_NAME = "Member"
GATE_CUSTOM_ID = "servarr:agree_rules"

# Visible to everyone, but only the bot (and webhooks, for the release
# feeds) can post here — never hidden, since these are exactly what people
# need to see without doing anything first: releases, requests, live status.
READ_ONLY_CHANNEL_NAMES = [
    "start-here", "rules", "faq", "how-to-request", "playback-help",
    "requests", "movie-releases", "tv-releases", "anime-releases",
    "now-playing", "leaderboard", "level-ups", "mod-log",
]
# Other bots that also need to post in these read-only channels — Requestarr
# posts request confirmations to #requests, but has no channel-level
# overwrite of its own unless explicitly granted here. Confirmed live: this
# was missing and silently broke every request after the channels were
# locked down (Requestarr could add to Sonarr/Radarr, then hit Forbidden
# trying to post the confirmation, leaving the job untracked).
OTHER_BOT_ROLE_NAMES = ["Requestarr"]
# The one channel actually gated behind agreeing to the rules.
GATED_CHAT_CHANNEL_NAME = "general"


class RulesGateView(discord.ui.View):
    """timeout=None + a fixed custom_id makes this a persistent view — the
    button keeps working on old messages even after the bot restarts, as
    long as this view is registered once via client.add_view() at startup.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="I Agree to the Rules", emoji="✅", style=discord.ButtonStyle.success, custom_id=GATE_CUSTOM_ID)
    async def agree(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        guild = interaction.guild
        role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)
        if role is None:
            await interaction.response.send_message("Something's misconfigured — tell a mod the Member role is missing.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message("You're already in!", ephemeral=True)
            return
        await interaction.user.add_roles(role, reason="Agreed to server rules")
        await interaction.response.send_message(f"✅ You're in! #{GATED_CHAT_CHANNEL_NAME} is unlocked — everything else was already visible.", ephemeral=True)


def gate_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🔓 One click to unlock chat",
        description=(
            f"You can already see and use everything — browse releases, run `/movie` `/tv` `/anime`, check `/rank` — no click required for any of that. "
            f"Agreeing to the rules just unlocks **#{GATED_CHAT_CHANNEL_NAME}** so you can actually chat."
        ),
        color=0xD4AF37,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"{guild.name} · Welcome aboard")
    return embed


AUTOMOD_RULE_NAME = "Servarr — blocked words"


async def setup_automod(guild: discord.Guild) -> bool:
    """Uses Discord's own native AutoMod (server-side, instant, no bot
    downtime/latency, and doesn't need the privileged Message Content
    intent) rather than a hand-rolled word filter. Blocks the message —
    never bans or kicks anyone over it, per explicit instruction.
    """
    existing = discord.utils.get(await guild.fetch_automod_rules(), name=AUTOMOD_RULE_NAME)
    if existing is not None:
        return True
    try:
        await guild.create_automod_rule(
            name=AUTOMOD_RULE_NAME,
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType.keyword_preset,
                presets=discord.AutoModPresets.all(),
            ),
            actions=[
                discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType.block_message,
                    custom_message="That message was blocked by the word filter — no bans, just keeping things clean.",
                )
            ],
            enabled=True,
            reason="Servarr initial setup — blocked-word filter, block only, never punitive",
        )
        return True
    except discord.Forbidden as exc:
        await log(f"gating: couldn't set up AutoMod word filter (needs 'Manage Server' permission): {exc}")
        return False


async def lock_down_server(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=MEMBER_ROLE_NAME, colour=discord.Colour(0xD4AF37), reason="Rules-gate role")

    # Nothing is hidden — every category/channel stays visible to everyone.
    # Read-only channels (info pages + all the release/status feeds) just
    # deny @everyone send_messages so they can't turn into chat; the bot's
    # own top role is explicitly allowed to send so its future content
    # updates and auto-posted feeds keep working.
    bot_role = guild.me.top_role
    other_bot_roles = [r for r in (discord.utils.get(guild.roles, name=n) for n in OTHER_BOT_ROLE_NAMES) if r is not None]
    for name in READ_ONLY_CHANNEL_NAMES:
        channel = discord.utils.get(guild.text_channels, name=name)
        if channel is None:
            continue
        await channel.set_permissions(guild.default_role, send_messages=False, add_reactions=True, read_message_history=True)
        await channel.set_permissions(bot_role, send_messages=True)
        for other_role in other_bot_roles:
            await channel.set_permissions(other_role, send_messages=True, create_public_threads=True, send_messages_in_threads=True)

    # #general is the one channel actually gated behind the rules-gate button.
    general = discord.utils.get(guild.text_channels, name=GATED_CHAT_CHANNEL_NAME)
    if general is not None:
        await general.set_permissions(guild.default_role, send_messages=False, view_channel=True, read_message_history=True)
        await general.set_permissions(role, send_messages=True)
        await general.set_permissions(bot_role, send_messages=True)

    return role
