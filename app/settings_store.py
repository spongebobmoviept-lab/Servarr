import json
import os

from .config import settings

EDITABLE_KEYS = [
    "movie_night_channel_id",
    "movie_night_daily_vote",
    "movie_night_vote_hour_utc",
    "movie_night_hour_utc",
    "movie_night_candidate_count",
    "movie_night_pause_upgrade_minutes",
    "movie_night_dj_role_id",
    "xp_min_per_message",
    "xp_max_per_message",
    "xp_cooldown_seconds",
    "mod_log_channel_id",
    "leaderboard_channel",
    "movie_night_control_channel",
    "extra_read_only_channels",
    "pinned_tile_channel",
    "pinned_tile_title_match",
]

# Expected type per key, so a bad value from the settings API can't break
# the bot at runtime (e.g. a number where a channel name belongs).
_TYPES = {
    "movie_night_vote_hour_utc": int,
    "movie_night_hour_utc": int,
    "movie_night_candidate_count": int,
    "movie_night_pause_upgrade_minutes": int,
    "xp_min_per_message": int,
    "xp_max_per_message": int,
    "xp_cooldown_seconds": int,
    "extra_read_only_channels": list,
    "movie_night_daily_vote": bool,
}

# Text keys that hold a Discord ID (or blank).
_ID_KEYS = {"movie_night_channel_id"}

# The mode picked on the setup page ("full"/"movienight"). Stored in the same
# file but not one of the EDITABLE_KEYS: it's changed via /api/setup/mode,
# which also restarts what depends on it. SERVARR_MODE always wins.
_MODE_KEY = "servarr_mode"


def validate(update: dict) -> None:
    """Raises ValueError with a plain-English message for a bad value."""
    for key, value in update.items():
        expected = _TYPES.get(key, str)
        if expected is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} must be a whole number of 0 or more")
            if key.endswith("_hour_utc") and value > 23:
                raise ValueError(f"{key} must be an hour between 0 and 23")
        elif expected is list:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{key} must be a list of channel names")
        elif not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        elif key in _ID_KEYS and value.strip() and not value.strip().isdigit():
            raise ValueError(f"{key} must be a Discord channel ID (numbers only)")

_overrides_path = os.path.join(settings.data_dir, "settings_overrides.json")


def load_overrides() -> None:
    if not os.path.exists(_overrides_path):
        return
    with open(_overrides_path, "r", encoding="utf-8") as f:
        overrides = json.load(f)
    for key, value in overrides.items():
        if key in EDITABLE_KEYS:
            setattr(settings, key, value)
    if isinstance(overrides.get(_MODE_KEY), str):
        settings.servarr_mode = overrides[_MODE_KEY]


def _persist_all() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    payload = {key: getattr(settings, key) for key in EDITABLE_KEYS}
    if settings.servarr_mode:
        payload[_MODE_KEY] = settings.servarr_mode
    tmp_path = _overrides_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, _overrides_path)


def current_editable(keys=None) -> dict:
    return {key: getattr(settings, key) for key in EDITABLE_KEYS if keys is None or key in keys}


def save_overrides(update: dict) -> dict:
    for key, value in update.items():
        if key in EDITABLE_KEYS:
            setattr(settings, key, value)
    _persist_all()
    return current_editable()


def save_mode(mode: str) -> None:
    """Stores the setup page's mode choice ("full" or "movienight")."""
    settings.servarr_mode = mode
    _persist_all()
