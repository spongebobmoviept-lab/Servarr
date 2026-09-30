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
# Saved alongside, but not settable or shown through the API: how the player
# got paired (see auto_pair_from_file).
_INTERNAL_KEYS = ["player_pair_source", "player_pair_file_url"]

_overrides_path = os.path.join(settings.data_dir, "connections_overrides.json")

PRIVATE_FILE_MODE = 0o600


def write_private_json(path: str, payload: dict) -> None:
    """Writes a file that holds secrets (bot token, API keys, password hash):
    readable and writable by the app's own user only (0600), replaced
    atomically so a crash never leaves half a file behind."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        _tighten(tmp_path)  # O_CREAT's mode doesn't apply to a leftover .tmp file
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)


def _tighten(path: str) -> None:
    """Best effort: some bind mounts (e.g. Docker Desktop on Windows) ignore it."""
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, PRIVATE_FILE_MODE)
    except OSError:
        pass


def load_overrides() -> None:
    if not os.path.exists(_overrides_path):
        return
    _tighten(_overrides_path)  # 1.1.x wrote this file world-readable
    with open(_overrides_path, "r", encoding="utf-8") as f:
        overrides = json.load(f)
    for key, value in overrides.items():
        if key in ALL_KEYS or (key in _INTERNAL_KEYS and isinstance(value, str)):
            setattr(settings, key, value)


def _persist_all() -> None:
    payload = {key: getattr(settings, key) for key in ALL_KEYS + _INTERNAL_KEYS}
    write_private_json(_overrides_path, payload)


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•••••" + value[-4:]


def current_display(keys=None) -> dict:
    """Never returns a secret in full — masked fields only ever show a
    trailing-4-character hint, matching the display convention used by
    every major service's own "your key is ****1234" UI. `keys` limits it
    to the fields the current mode uses.
    """
    result = {}
    for key in PLAIN_KEYS:
        if keys is None or key in keys:
            result[key] = {"value": getattr(settings, key, ""), "is_secret": False}
    for key in SECRET_KEYS:
        if keys is None or key in keys:
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
        if key in _PAIRING_KEYS and value != getattr(settings, key):
            # The player's address or key was changed by hand: from now on
            # the pairing file no longer overrides it (see auto_pair_from_file).
            settings.player_pair_source = "manual"
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


_PAIRING_KEYS = ("neko_mpv_shim_url", "player_key")


def mark_paired_by_hand() -> None:
    """Called when someone pairs on the setup page (pair code, or address
    and key): that pairing wins over the pairing file from now on."""
    settings.player_pair_source = "manual"
    _persist_all()


def paired_from_file(file_key: str = "") -> bool:
    """True when the saved pairing came from the pairing file rather than
    being typed on the setup page. Recorded since 1.2; a pairing made before
    that counts as the file's when it has the file's key."""
    saved_url = settings.neko_mpv_shim_url.rstrip("/")
    if not saved_url or settings.player_pair_source == "manual":
        return False
    if settings.player_pair_source == "file":
        return settings.player_pair_file_url.rstrip("/") == saved_url
    return bool(file_key) and settings.player_key == file_key


def auto_pair_from_file() -> bool:
    """Zero-click pairing for the bundle: a movienight player can export
    pair.json into a volume shared with Servarr.

    * Nothing paired yet: pair from the file (whenever it shows up, so the
      start order of the two containers doesn't matter).
    * The saved pairing came from the file: the file is the authority, so
      follow its address and key when the player changes them.
    * A pairing typed by hand wins and is left alone. If it's the same
      address as the file's, only a rotated key is picked up.

    Returns True if the saved pairing changed."""
    from . import mpv_control

    data = mpv_control.find_pair_file()
    if not data:
        return False
    saved_url = settings.neko_mpv_shim_url.rstrip("/")
    same_player = saved_url == data["url"]
    from_file = paired_from_file(data["key"])
    if saved_url and not same_player and not from_file:
        return False  # paired with a different player by hand; leave it
    follows_file = from_file or not saved_url
    if same_player and settings.player_key == data["key"] and settings.player_mode == "movienight":
        if follows_file and (settings.player_pair_source != "file" or settings.player_pair_file_url != data["url"]):
            # Paired from this file before 1.2: remember that, so a later
            # address change in the file is followed.
            settings.player_pair_source, settings.player_pair_file_url = "file", data["url"]
            _persist_all()
        return False
    settings.neko_mpv_shim_url = data["url"]
    settings.player_key = data["key"]
    settings.player_mode = "movienight"
    if follows_file:
        settings.player_pair_source, settings.player_pair_file_url = "file", data["url"]
    _persist_all()
    return True
