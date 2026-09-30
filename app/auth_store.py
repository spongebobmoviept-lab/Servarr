import hashlib
import hmac
import json
import os
import secrets as secrets_mod

from .config import settings
from .connections_store import write_private_json

_overrides_path = os.path.join(settings.data_dir, "auth_overrides.json")
_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS).hex()


def _load() -> dict | None:
    if not os.path.exists(_overrides_path):
        return None
    with open(_overrides_path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_admin_configured() -> bool:
    return _load() is not None


def set_admin(username: str, password: str) -> None:
    salt = secrets_mod.token_bytes(16)
    payload = {"username": username, "salt": salt.hex(), "hash": _hash_password(password, salt)}
    write_private_json(_overrides_path, payload)


def verify_admin(username: str, password: str) -> bool:
    stored = _load()
    if stored is None:
        return False
    salt = bytes.fromhex(stored["salt"])
    candidate = _hash_password(password, salt)
    return hmac.compare_digest(candidate, stored["hash"]) and hmac.compare_digest(username, stored["username"])


def is_setup_complete() -> bool:
    """Gates whether "/" serves the app or redirects to the wizard.

    Only an admin login plus a Discord bot token are required — without a
    token the bot can't do anything at all, but every individual service
    connection (Radarr, Plex, Neko, etc.) only gates its own feature and is
    editable later from the same page.
    """
    if not is_admin_configured():
        return False
    if not settings.discord_bot_token:
        return False
    return True
