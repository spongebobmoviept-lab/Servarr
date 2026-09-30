import json
import os

from .config import settings

# Plain fields: safe to show as-is (addresses/ids, not credentials).
PLAIN_KEYS = [
    "discord_guild_id",
    "sonarr_url",
    "radarr_url",
    "tautulli_url",
    "reclaimarr_url",
    "reclaimarr_auth_user",
    "plex_url",
    "neko_mpv_shim_url",
    "player_mode",
    "neko_mpv_public_url",
    "servarr_public_url",
]
# Secret fields: never returned in full — masked on read, only overwritten
# when the caller actually sends a new (non-empty) value.
SECRET_KEYS = [
    "discord_bot_token",
    "sonarr_api_key",
    "radarr_api_key",
    "tautulli_api_key",
    "reclaimarr_auth_pass",
    "plex_token",
    "player_key",
    "neko_mpv_password",
    "neko_mpv_admin_password",
    "mpv_remote_key",
]

ALL_KEYS = PLAIN_KEYS + SECRET_KEYS

_overrides_path = os.path.join(settings.data_dir, "connections_overrides.json")


def load_overrides() -> None:
    if not os.path.exists(_overrides_path):
        return
    with open(_overrides_path, "r", encoding="utf-8") as f:
        overrides = json.load(f)
    for key, value in overrides.items():
        if key in ALL_KEYS:
            setattr(settings, key, value)


def _persist_all() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    payload = {key: getattr(settings, key) for key in ALL_KEYS}
    tmp_path = _overrides_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, _overrides_path)


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•••••" + value[-4:]


def current_display() -> dict:
    """Never returns a secret in full — masked fields only ever show a
    trailing-4-character hint, matching the display convention used by
    every major service's own "your key is ****1234" UI.
    """
    result = {}
    for key in PLAIN_KEYS:
        result[key] = {"value": getattr(settings, key, ""), "is_secret": False}
    for key in SECRET_KEYS:
        value = getattr(settings, key, "")
        result[key] = {"value": _mask(value), "is_secret": True, "is_set": bool(value)}
    return result


def save_overrides(update: dict) -> dict:
    """Empty string for a secret field means "leave it alone" — the frontend
    never has the real value to send back, so an empty submission must never
    be interpreted as "clear this credential."
    """
    for key, value in update.items():
        if key not in ALL_KEYS:
            continue
        if key in SECRET_KEYS and not value:
            continue
        setattr(settings, key, value)
    _persist_all()
    return current_display()


def ensure_remote_key() -> bool:
    """Generates and saves a random MPV_REMOTE_KEY on first start if none is
    set, so the /mpv-remote link works without anyone inventing a secret.
    Returns True if a new key was created."""
    import secrets

    if settings.mpv_remote_key:
        return False
    settings.mpv_remote_key = secrets.token_urlsafe(24).replace("-", "").replace("_", "")
    _persist_all()
    return True


def auto_pair_from_file() -> bool:
    """Zero-click pairing for the bundle: if no player is configured yet and
    a movienight player has exported pair.json into the shared volume, use
    it. Returns True if a player was paired."""
    from . import mpv_control

    if settings.neko_mpv_shim_url or not settings.player_pair_file:
        return False
    data = mpv_control.load_pair_file(settings.player_pair_file)
    if not data:
        return False
    settings.neko_mpv_shim_url = data["url"]
    settings.player_key = data["key"]
    settings.player_mode = "movienight"
    _persist_all()
    return True
