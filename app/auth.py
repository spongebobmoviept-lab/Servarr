import time
from collections import defaultdict
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import auth_store
from .config import settings

security = HTTPBasic()
# auto_error=False so a request with no Basic Auth header at all doesn't
# immediately 401 here — require_login_or_remote_key needs the chance to
# check the remote-key cookie first, and only fall back to demanding real
# login if that's also missing.
_optional_security = HTTPBasic(auto_error=False)

# No lockout at all previously meant unlimited password guesses against a
# real credential over the LAN (or wherever this ends up exposed once
# shared with other people) — simple in-memory per-IP throttle, reset on
# restart. Proportionate for a single-process home-lab tool; not trying to
# be a full auth service.
_MAX_ATTEMPTS = 10
_WINDOW_SECONDS = 300
_failed_attempts: dict[str, list[float]] = defaultdict(list)


def check_credentials(request: Request, credentials: HTTPBasicCredentials) -> str:
    """Shared logic behind require_login — split out so the root route can
    invoke it manually (after its own "is setup complete?" redirect check)
    instead of going through FastAPI's Depends() every time.
    """
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = _failed_attempts[client_ip]
    attempts[:] = [t for t in attempts if now - t < _WINDOW_SECONDS]
    if len(attempts) >= _MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts — try again in a few minutes",
        )

    if not auth_store.verify_admin(credentials.username, credentials.password):
        attempts.append(now)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_login(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> str:
    return check_credentials(request, credentials)


MPV_REMOTE_COOKIE = "mpv_remote_key"


def require_login_or_remote_key(
    request: Request, credentials: Optional[HTTPBasicCredentials] = Depends(_optional_security)
) -> str:
    """Lets the /mpv-remote page and its API calls work from a plain link
    (see mpv_remote_page/bot.py's mpv-play command) instead of a Basic Auth
    popup — checks against its own dedicated settings.mpv_remote_key (a
    plain alphanumeric token, not a reused password — see config.py for why).
    Falls back to the normal admin login for anyone who navigates to the
    page directly (bookmarked, no key cookie yet).
    """
    if settings.mpv_remote_key and request.cookies.get(MPV_REMOTE_COOKIE) == settings.mpv_remote_key:
        return "remote-key"
    if credentials is not None:
        return check_credentials(request, credentials)
    raise HTTPException(status_code=401, detail="Login required", headers={"WWW-Authenticate": "Basic"})
