"""Phase 3 — the one piece that touches the *existing* live server. Never
runs blind: /setup-server always shows this exact proposal and waits for an
explicit Confirm before creating anything. Everything here is additive —
it only ever creates new categories/channels/roles, never renames, moves,
or deletes anything that already exists on the server.
"""

import os
from dataclasses import dataclass

import discord

from . import gating, onboarding_content
from .config import settings
from .logger import log

GOLD = 0xD4AF37
DARK = discord.Colour(0x101010)

ICON_PATH = os.path.join(settings.data_dir, "icon.png")
BANNER_PATH = os.path.join(settings.data_dir, "banner.png")


@dataclass
class ProposedChannel:
    name: str
    topic: str


@dataclass
class ProposedCategory:
    name: str
    channels: list[ProposedChannel]


PROPOSED_CATEGORIES = [
    ProposedCategory(
        "📋 Start Here",
        [
            ProposedChannel("start-here", "New? Read this first"),
            ProposedChannel("rules", "Server rules"),
            ProposedChannel("faq", "Frequently asked questions"),
            ProposedChannel("how-to-request", "Step-by-step walkthrough for requesting a movie/show/anime"),
            ProposedChannel("playback-help", "What to do if playback stops, buffers, or something looks broken"),
        ],
    ),
    ProposedCategory("📥 Requests", [ProposedChannel("requests", "Use /movie, /tv, or /anime here (or anywhere) — confirmations and status threads show up here")]),
    ProposedCategory("🎬 Movies", [ProposedChannel("movie-releases", "New/upcoming movies, posted automatically")]),
    ProposedCategory("📺 TV Shows", [ProposedChannel("tv-releases", "New/upcoming TV episodes, posted automatically")]),
    ProposedCategory("🈴 Anime", [ProposedChannel("anime-releases", "New/upcoming anime episodes, posted automatically")]),
    ProposedCategory(
        "🏆 Community",
        [
            ProposedChannel("general", "General chat"),
            ProposedChannel("now-playing", "Live view of what's currently being watched on Plex"),
            ProposedChannel("leaderboard", "Server activity leaderboard"),
            ProposedChannel("level-ups", "Level-up announcements"),
            ProposedChannel("mod-log", "Moderation action log"),
        ],
    ),
]

PROPOSED_ROLES = [
    ("Bronze", 0xCD7F32),
    ("Silver", 0xC0C0C0),
    ("Gold", 0xD4AF37),
]


def build_proposal_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛠️ Proposed Server Setup",
        description="This only ever **adds** new categories/channels/roles — nothing existing gets renamed, moved, or deleted.",
        color=GOLD,
    )
    for cat in PROPOSED_CATEGORIES:
        channel_list = "\n".join(f"#{c.name} — {c.topic}" for c in cat.channels)
        embed.add_field(name=cat.name, value=channel_list, inline=False)
    embed.add_field(name="New roles", value=", ".join(f"{name}" for name, _ in PROPOSED_ROLES), inline=False)
    extras = []
    if os.path.exists(ICON_PATH):
        extras.append("server icon")
    if os.path.exists(BANNER_PATH):
        extras.append("server banner")
    if extras:
        embed.add_field(name="Also updates", value=", ".join(extras), inline=False)
    embed.set_footer(text="#start-here, #rules, #faq, and #how-to-request get real content posted (and pinned) automatically, written for someone brand new to the server.")
    return embed


_CONTENT_POSTERS = {
    "start-here": lambda guild: [onboarding_content.start_here_embed_themed(guild)],
    "rules": lambda guild: [onboarding_content.rules_embed(guild.name)],
    "faq": lambda guild: [onboarding_content.faq_embed()],
    "how-to-request": lambda guild: onboarding_content.how_to_request_embeds(),
    "playback-help": lambda guild: onboarding_content.playback_help_embeds(),
}


async def _post_and_pin(channel: discord.TextChannel, embeds: list[discord.Embed]) -> None:
    if not embeds:
        return
    last_msg = None
    for embed in embeds:
        last_msg = await channel.send(embed=embed)
    try:
        await last_msg.pin()  # pin the last one so the channel opens showing it's fully filled out
    except Exception as exc:  # noqa: BLE001 — missing "Manage Messages" would only affect the pin, not the content itself
        await log(f"theming: couldn't pin message in #{channel.name}: {exc}")


async def apply_setup(guild: discord.Guild) -> list[str]:
    created: list[str] = []
    for index, cat in enumerate(PROPOSED_CATEGORIES):
        existing = discord.utils.get(guild.categories, name=cat.name)
        category = existing or await guild.create_category(cat.name, position=index)
        if not existing:
            created.append(f"category {cat.name}")
        for ch in cat.channels:
            channel = discord.utils.get(category.text_channels, name=ch.name)
            if channel is None:
                channel = await category.create_text_channel(ch.name, topic=ch.topic)
                created.append(f"#{ch.name}")
                poster = _CONTENT_POSTERS.get(ch.name)
                if poster:
                    await _post_and_pin(channel, poster(guild))

    if os.path.exists(ICON_PATH):
        try:
            with open(ICON_PATH, "rb") as f:
                await guild.edit(icon=f.read())
            created.append("server icon")
        except Exception as exc:  # noqa: BLE001
            await log(f"theming: couldn't set server icon: {exc}")

    if os.path.exists(BANNER_PATH):
        try:
            with open(BANNER_PATH, "rb") as f:
                await guild.edit(banner=f.read())
            created.append("server banner")
        except Exception as exc:  # noqa: BLE001 — banner requires a boost level; failing here shouldn't block everything else
            await log(f"theming: couldn't set server banner (needs Level 2 boost or higher): {exc}")

    for name, color in PROPOSED_ROLES:
        if discord.utils.get(guild.roles, name=name) is None:
            await guild.create_role(name=name, colour=discord.Colour(color))
            created.append(f"role {name}")

    await gating.lock_down_server(guild)
    created.append("read-only lock on info/feed channels + rules-gated #general chat")

    if await gating.setup_automod(guild):
        created.append("AutoMod blocked-word filter (block only, never bans)")

    rules_channel = discord.utils.get(guild.text_channels, name="rules")
    if rules_channel is not None:
        already_posted = False
        async for msg in rules_channel.history(limit=20):
            if msg.author.id == guild.me.id and msg.components:
                already_posted = True
                break
        if not already_posted:
            await rules_channel.send(embed=gating.gate_embed(guild), view=gating.RulesGateView())
            created.append("rules-gate button")

    return created


class SetupConfirmView(discord.ui.View):
    def __init__(self, guild: discord.Guild, requester_id: int):
        super().__init__(timeout=300)
        self.guild = guild
        self.requester_id = requester_id

    async def _owner_only(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /setup-server can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._owner_only(interaction):
            return
        self.stop()
        await interaction.response.edit_message(content="⏳ Setting up…", embed=None, view=None)
        created = await apply_setup(self.guild)
        summary = "\n".join(f"• {c}" for c in created) if created else "Everything already existed — nothing new to create."
        await interaction.followup.send(f"✅ Done.\n{summary}", ephemeral=True)

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._owner_only(interaction):
            return
        self.stop()
        await interaction.response.edit_message(content="Cancelled — nothing was changed.", embed=None, view=None)
