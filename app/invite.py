"""Bot invite links that ask only for the permissions the enabled features use."""

from . import features

# Everything mode (unchanged from 1.1): view/send/embed/attach/history/react,
# threads, manage messages (digest pinning), manage channels + roles
# (/setup-server and the rules gate), kick/ban/timeout, manage server (AutoMod).
FULL_PERMISSION_BITS = (1, 2, 4, 5, 6, 10, 11, 13, 14, 15, 16, 28, 35, 38, 40)

# Movie Night only: see the Movie Night and DJ control channels, post the
# vote / "Now playing" / Watch Live messages (embeds), and read the channel
# history to find and edit its own control panel and now-playing message.
# (name shown to people, discord.py Permissions attribute, bit)
MOVIE_NIGHT_PERMISSIONS = (
    ("View Channels", "view_channel", 10),
    ("Send Messages", "send_messages", 11),
    ("Embed Links", "embed_links", 14),
    ("Read Message History", "read_message_history", 16),
)


def permissions_value(mode: str) -> int:
    bits = FULL_PERMISSION_BITS if mode != features.MOVIENIGHT else tuple(bit for _, _, bit in MOVIE_NIGHT_PERMISSIONS)
    return sum(1 << bit for bit in bits)


def permission_names(mode: str) -> list[str]:
    """What the invite asks for, in words (Movie Night only mode lists them
    on the setup page; Everything mode asks for a longer admin-ish set)."""
    if mode == features.MOVIENIGHT:
        return [name for name, _, _ in MOVIE_NIGHT_PERMISSIONS]
    return []


def invite_url(application_id: str, mode: str) -> str:
    """The OAuth2 link that adds the bot (with slash commands) to a server."""
    app_id = "".join(ch for ch in str(application_id or "") if ch.isdigit())
    return (
        f"https://discord.com/oauth2/authorize?client_id={app_id}"
        f"&scope=bot%20applications.commands&permissions={permissions_value(mode)}"
    )
