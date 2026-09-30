"""Servarr's two modes, and what runs in each.

* "full" (the default): everything, exactly as before.
* "movienight": Movie Night only. Meant for someone who uses a friend's Plex
  server (the Guest edition of the Movie Night player). Leveling,
  moderation, release calendars, the rules gate and server theming, and
  the Sonarr/Radarr/Tautulli/Plex/Reclaimarr integrations are switched off:
  their loops never start, their slash commands aren't synced, their
  listeners return early, and their setup sections and setup endpoints are
  gone. The library comes from the paired Movie Night player instead.

SERVARR_MODE wins when it's set, then the choice made on the setup page,
then "full".
"""

import asyncio
import re
from typing import Callable, Optional

from .config import settings

FULL = "full"
MOVIENIGHT = "movienight"
MODES = (FULL, MOVIENIGHT)
LABELS = {FULL: "Everything", MOVIENIGHT: "Movie Night only"}

_ALIASES = {
    "full": FULL,
    "everything": FULL,
    "all": FULL,
    "movienight": MOVIENIGHT,
    "movienightonly": MOVIENIGHT,
}


def normalize(value) -> str:
    """"full" or "movienight" ("Movie Night only", "movie-night", "everything"
    are accepted too), or "" for anything else."""
    if not isinstance(value, str):
        return ""
    return _ALIASES.get(re.sub(r"[^a-z]", "", value.lower()), "")


def resolve(env_value, stored_value) -> str:
    return normalize(env_value) or normalize(stored_value) or FULL


def current() -> str:
    return resolve(settings.servarr_mode_env, settings.servarr_mode)


def is_movienight() -> bool:
    return current() == MOVIENIGHT


def locked() -> bool:
    """True when SERVARR_MODE sets the mode, so the setup page can't change it."""
    return bool(normalize(settings.servarr_mode_env))


def chosen() -> bool:
    """False on a fresh install until someone picks a mode (or sets SERVARR_MODE)."""
    return bool(normalize(settings.servarr_mode_env) or normalize(settings.servarr_mode))


def unknown_env_value() -> str:
    """SERVARR_MODE's raw value when it's set to something that isn't a mode."""
    raw = settings.servarr_mode_env
    return raw if raw and not normalize(raw) else ""


def refusal(feature: str) -> str:
    """The error a setup endpoint gives for a feature this mode switches off."""
    if locked():
        how = "Set SERVARR_MODE=full to use it."
    else:
        how = "Switch to Everything at the top of the setup page to use it."
    return f"{feature} is turned off because Servarr is in Movie Night only mode. {how}"


# Slash commands that only exist in full mode. Everything else (the Movie
# Night commands and /help) is synced in both modes.
FULL_ONLY_COMMANDS = frozenset({
    "upcoming", "refresh-calendar",  # release calendar
    "rank", "leaderboard",  # leveling
    "setup-server",  # theming + rules gate
    "kick", "ban", "timeout", "warn",  # moderation
})

# Setup-page settings (settings_store) and connections (connections_store)
# that Movie Night only mode uses. The setup API refuses the others there.
MOVIE_NIGHT_SETTINGS = frozenset({
    "movie_night_channel_id",
    "movie_night_daily_vote",
    "movie_night_vote_hour_utc",
    "movie_night_hour_utc",
    "movie_night_candidate_count",
    "movie_night_dj_role_id",
    "movie_night_control_channel",
})
MOVIE_NIGHT_CONNECTIONS = frozenset({
    "discord_bot_token",
    "discord_guild_id",
    "neko_mpv_shim_url",
    "player_mode",
    "player_key",
    "servarr_public_url",
    "mpv_remote_key",
})


def refused_keys(keys, allowed: frozenset) -> list[str]:
    """The keys of a settings/connections update this mode doesn't accept."""
    if not is_movienight():
        return []
    return sorted(k for k in keys if k not in allowed)


# ---------------------------------------------------------------------------
# background loops
# ---------------------------------------------------------------------------

# name -> the modes it runs in
TASK_MODES = {
    "plex_monitor": (FULL,),
    "release_digest": (FULL,),
    "movie_night_vote": MODES,
    "movie_night_announce": MODES,
    "movie_night_status": MODES,
    "player_pairing": MODES,
}


def wanted_tasks(mode: Optional[str] = None) -> list[str]:
    mode = normalize(mode) or current()
    return [name for name, modes in TASK_MODES.items() if mode in modes]


def _factories() -> dict[str, Callable]:
    from . import movie_night, mpv_control, plex_monitor, release_digest

    return {
        "plex_monitor": plex_monitor.poll_loop,
        "release_digest": release_digest.daily_loop,
        "movie_night_vote": movie_night.vote_loop,
        "movie_night_announce": movie_night.announce_loop,
        "movie_night_status": movie_night.status_loop,
        "player_pairing": mpv_control.pairing_watch_loop,
    }


_tasks: dict[str, asyncio.Task] = {}


def reconcile_tasks(factories: Optional[dict[str, Callable]] = None) -> None:
    """Starts the loops the current mode needs and cancels the others. Safe
    to call again after the mode changes. A loop that already ended on its
    own (e.g. the Plex monitor without Tautulli) isn't restarted."""
    wanted = wanted_tasks()
    for name in list(_tasks):
        if name not in wanted:
            _tasks.pop(name).cancel()
    factories = factories or _factories()
    for name in wanted:
        if name not in _tasks:
            _tasks[name] = asyncio.create_task(factories[name](), name=f"servarr:{name}")


def running_tasks() -> list[str]:
    return [name for name, task in _tasks.items() if not task.done()]


def stop_tasks() -> None:
    for task in _tasks.values():
        task.cancel()
    _tasks.clear()
