import hmac
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
REMOTE_KEY_HEADER = "X-Remote-Key"


def remote_key_matches(candidate: Optional[str]) -> bool:
    """Constant-time comparison against MPV_REMOTE_KEY. Always False when no
    key is configured, so an empty key can never unlock anything."""
    expected = settings.mpv_remote_key
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def check_remote_key(request: Request, candidate: str) -> bool:
    """Throttled key check — shares the per-IP failed-attempt window with
    the admin login, so the key can't be brute-forced either."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = _failed_attempts[client_ip]
    attempts[:] = [t for t in attempts if now - t < _WINDOW_SECONDS]
    if len(attempts) >= _MAX_ATTEMPTS:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many failed attempts — try again in a few minutes")
    if remote_key_matches(candidate):
        return True
    attempts.append(now)
    return False


def require_login_or_remote_key(
    request: Request, credentials: Optional[HTTPBasicCredentials] = Depends(_optional_security)
) -> str:
    """Lets the /mpv-remote page and its API calls work from a plain link
    instead of a Basic Auth popup, using the dedicated MPV_REMOTE_KEY.

    The key is accepted from the X-Remote-Key header (what the page's own
    JS sends on every request — works even inside an iframe where the
    browser blocks third-party cookies) or from the HttpOnly cookie set by
    POST /api/mpv/login. It is deliberately NOT accepted as a query
    parameter: a secret in a URL ends up in server/proxy access logs,
    browser history and Referer headers.

    Anyone without a key falls back to the normal admin login.
    """
    candidate = request.headers.get(REMOTE_KEY_HEADER) or request.cookies.get(MPV_REMOTE_COOKIE)
    if candidate and settings.mpv_remote_key:
        if check_remote_key(request, candidate):
            return "remote-key"
        raise HTTPException(status_code=401, detail="Invalid remote key")
    if credentials is not None:
        return check_credentials(request, credentials)
    raise HTTPException(status_code=401, detail="Login required", headers={"WWW-Authenticate": "Basic"})
