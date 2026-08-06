"""The actual words shown in the Start Here section — written for someone
with zero context (explicitly: "if I invited my mom"). Plain language, no
jargon, concrete worked example. Posted (and pinned) into their channels
once by theming.apply_setup(); editing these later just needs a redeploy.
"""

import discord

GOLD = 0xD4AF37


def start_here_embed(guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"👋 Welcome to {guild_name}!",
        description=(
            "This server is a shared media library — think of it like a private Netflix your friends run for each other. "
            "Here's everything you need to know, in order:"
        ),
        color=GOLD,
    )
    embed.add_field(
        name="1️⃣ Read the rules",
        value="Quick, not scary — check #rules.",
        inline=False,
    )
    embed.add_field(
        name="2️⃣ Want to watch something that isn't here yet?",
        value="Go to #how-to-request — it walks you through asking for a movie, TV show, or anime, step by step, with pictures of what you'll actually see.",
        inline=False,
    )
    embed.add_field(
        name="3️⃣ Got a question?",
        value="Check #faq first — it probably already answers it.",
        inline=False,
    )
    embed.add_field(
        name="4️⃣ Just here to hang out?",
        value="Chatting in the server earns you XP — check your progress any time with `/rank`, and see who's most active with `/leaderboard`.",
        inline=False,
    )
    embed.add_field(
        name="5️⃣ Curious what's coming soon?",
        value="Type `/upcoming` anywhere to see what movies/shows/anime are about to be added.",
        inline=False,
    )
    embed.set_footer(text="That's it — you don't need to know anything technical. Just type the commands like normal messages.")
    return embed


def start_here_embed_themed(guild: discord.Guild) -> discord.Embed:
    embed = start_here_embed(guild.name)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def rules_embed(guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="📖 Server Rules",
        description=f"Nothing complicated — just the basics for keeping {guild_name} a good place to hang out.",
        color=GOLD,
    )
    embed.add_field(name="Be decent", value="Treat people how you'd want to be treated. No harassment, hate speech, or being a jerk.", inline=False)
    embed.add_field(name="No spam", value="Don't flood channels with repeated messages just to farm XP — it doesn't work anyway (there's a cooldown).", inline=False)
    embed.add_field(
        name="Where commands work",
        value=(
            "`/movie`, `/tv`, `/anime`, `/upcoming`, `/rank`, `/leaderboard` all work in **any** channel you have access to. "
            "Requests always post their confirmation/status thread in #requests, no matter where you typed the command."
        ),
        inline=False,
    )
    embed.add_field(
        name="Don't want your request public?",
        value="Add `private: True` as an option on `/movie`, `/tv`, or `/anime` — the whole thing (picker, confirmation, and updates) happens over DM instead of posting in a channel. No judgment here.",
        inline=False,
    )
    embed.add_field(name="This channel and the rest of Start Here are read-only", value="Everything here is visible right away — questions go in #general once you've agreed to the rules, not here.", inline=False)
    embed.add_field(name="Don't share invite links or account info publicly", value="Keep access to this server and its media between people who are actually invited.", inline=False)
    embed.add_field(name="Follow Discord's own rules too", value="The usual — no illegal content, no doxxing, etc. (discord.com/guidelines)", inline=False)
    return embed


def faq_embed() -> discord.Embed:
    embed = discord.Embed(title="❓ Frequently Asked Questions", color=GOLD)
    embed.add_field(
        name="How do I request a movie or show?",
        value="Type `/movie`, `/tv`, or `/anime` in any channel, followed by the title. Full walkthrough in #how-to-request.",
        inline=False,
    )
    embed.add_field(
        name="I requested something — how long until it's ready?",
        value="Varies by how popular/available it is — could be minutes, could be a day or two. You'll get a notification in a thread as it progresses, so you don't need to keep checking.",
        inline=False,
    )
    embed.add_field(
        name="What's the difference between /tv and /anime?",
        value="Anime gets special handling behind the scenes to make sure it picks the right subtitles/dub — always use `/anime` for anime rather than `/tv`, even though they look similar.",
        inline=False,
    )
    embed.add_field(
        name="What is /rank and /leaderboard?",
        value="Just for fun — you earn a small amount of XP for chatting (not spamming) and level up over time. `/rank` shows your own progress, `/leaderboard` shows the top members.",
        inline=False,
    )
    embed.add_field(
        name="Something's broken / my request never showed up",
        value="Let a mod know in the server — don't worry about figuring out why yourself.",
        inline=False,
    )
    embed.add_field(
        name="I don't want everyone seeing what I request",
        value="Add `private: True` when you type `/movie`, `/tv`, or `/anime` — everything happens over DM instead.",
        inline=False,
    )
    embed.add_field(
        name="What's Movie Night?",
        value=(
            "A vote posts in #general most evenings — pick from a few options, most votes wins (or a random pick if nobody votes). "
            "The winner gets announced with a link at showtime. There's no shared stream — everyone just hits play on their own Plex "
            "at the same time, like a regular watch party."
        ),
        inline=False,
    )
    embed.add_field(
        name="What's #now-playing / #requests / #movie-releases / #tv-releases / #anime-releases?",
        value="Live/automatic status feeds — no need to check them, but #now-playing shows what's currently being watched, #requests is where every request's confirmation and progress thread shows up, and the `-releases` channels post daily updates on what's newly available or coming soon.",
        inline=False,
    )
    return embed


def playback_help_embeds() -> list[discord.Embed]:
    upgrade = discord.Embed(
        title="⏸️ \"My movie just stopped and showed a weird message!\"",
        description=(
            "That's not a bug — that's **Reclaimarr**, the system that watches for a chance to quietly upgrade "
            "whatever you're watching to a better (4K) version. It noticed you were watching a lower-quality copy, "
            "already grabbed a better one in the background, and swapped it in."
        ),
        color=GOLD,
    )
    upgrade.add_field(
        name="What the on-screen message actually means",
        value="Something like *\"Hey! We upgraded [Movie] to 4K while you weren't looking. Restart to watch it — your original's untouched for 7 days.\"* — that message **is** the instructions. Just press play again.",
        inline=False,
    )
    upgrade.add_field(
        name="Is anything lost?",
        value="No. The original file is kept safe for a week (in case anything looks wrong with the new one), then it's cleaned up automatically. Nothing you were watching is ever deleted on the spot.",
        inline=False,
    )
    upgrade.add_field(
        name="Does this happen every time?",
        value="No — only the first time you happen to be watching something that's eligible for an upgrade. Most of the time, playback just works normally.",
        inline=False,
    )

    quality = discord.Embed(title="📶 Playback stuttering or buffering?", color=GOLD)
    quality.add_field(
        name="The fix is almost always \"lower the streaming quality\"",
        value="Every Plex app has a quality setting for the *stream*, separate from the file's actual quality. Lowering it re-encodes on the fly so it fits your connection — the file itself isn't changed.",
        inline=False,
    )
    quality.add_field(
        name="On your phone/tablet (iOS or Android app)",
        value="While something's playing, tap the screen once, then tap the **quality/gear icon** (bottom right of the player). Pick \"Original\" for the best quality if your connection is strong, or a lower number (e.g. 4 Mbps, 2 Mbps) if it's stuttering.",
        inline=False,
    )
    quality.add_field(
        name="On a TV app (Roku, Fire TV, Apple TV, Android TV, smart TV)",
        value="While something's playing, press the **down/select button** (or press OK/menu, depending on remote) to bring up playback options, then look for **Quality** or **Settings**.",
        inline=False,
    )
    quality.add_field(
        name="On the web player (plex.tv in a browser) or desktop app",
        value="Click the **gear/settings icon** in the bottom-right of the player while something's playing → **Quality**.",
        inline=False,
    )
    quality.add_field(
        name="Rule of thumb",
        value="Wired/ethernet > WiFi for smooth 4K. If you're on WiFi and it's stuttering, try \"Original\" first — if it still buffers, drop one level at a time until it's smooth.",
        inline=False,
    )

    troubleshooting = discord.Embed(title="🧯 Something actually broken?", color=GOLD)
    troubleshooting.add_field(
        name="My request has been sitting for a while",
        value="Check the thread that was created when you requested it — it posts updates as things happen. If it says it gave up looking, that means nothing suitable was found yet, not that something's broken.",
        inline=False,
    )
    troubleshooting.add_field(
        name="A movie/show just won't play at all",
        value="Try restarting the Plex app first — that fixes it more often than you'd think. Still broken? Tell a mod exactly what you were trying to watch and what happened.",
        inline=False,
    )
    troubleshooting.add_field(
        name="Anything else weird",
        value="You will never break anything by asking — just describe what you did and what you expected to happen instead. A mod will sort it out.",
        inline=False,
    )
    return [upgrade, quality, troubleshooting]


def how_to_request_embeds() -> list[discord.Embed]:
    intro = discord.Embed(
        title="🎬 How to Request Something",
        description="Full walkthrough below using a real example — the exact same steps work for `/tv` and `/anime` too.",
        color=GOLD,
    )
    intro.add_field(name="Step 1", value="Type `/movie` in any channel. Discord will show a little form pop up.", inline=False)
    intro.add_field(name="Step 2", value="In the `title` box, type what you're looking for — e.g. `Inception` — and press Enter.", inline=False)
    intro.add_field(name="Step 3", value="The bot replies (privately — only you see it) with up to 5 matches, each showing the poster, year, and details. Numbered buttons appear below.", inline=False)

    step2 = discord.Embed(color=GOLD)
    step2.add_field(name="Step 4", value="Click the number that matches the one you actually want (check the year if there are multiple versions!).", inline=False)
    step2.add_field(name="Step 5", value="A bigger confirmation screen shows up with the full details. If it says '✅ Already in your library', it's already here — no need to add it again. Otherwise, click **Confirm**.", inline=False)
    step2.add_field(name="Step 6", value="A message posts in #requests with a thread attached — that thread is where you'll see updates: grabbed, downloading, and finally ready to watch.", inline=False)
    step2.add_field(name="Want it private instead?", value="Add `private: True` in step 1 — skips the public post entirely, everything happens over DM.", inline=False)
    step2.set_footer(text="That's the whole process. No accounts to link, nothing else to set up.")
    return [intro, step2]
