"""Starts the web server (the setup page and the API).

By default it listens on every network interface, port 8888, exactly like
`uvicorn app.main:app --host 0.0.0.0 --port 8888`. Two settings change that:

* SERVARR_HOST: the address to listen on. `127.0.0.1` keeps the setup page
  reachable only from the machine itself, e.g. with host networking on a
  server that faces the internet (open it through an SSH tunnel).
* SERVARR_PORT: the port.

`python -m app.serve --check` asks the running server's /health and exits
0 (up) or 1: the container health check, wherever it listens.
"""

import os
import sys

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8888


def bind(environ=None) -> tuple[str, int]:
    """(host, port) from SERVARR_HOST / SERVARR_PORT. Raises ValueError with
    a plain message for a port that isn't one."""
    environ = os.environ if environ is None else environ
    host = (environ.get("SERVARR_HOST") or "").strip() or DEFAULT_HOST
    raw_port = (environ.get("SERVARR_PORT") or "").strip()
    if not raw_port:
        return host, DEFAULT_PORT
    if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
        raise ValueError(f"SERVARR_PORT must be a port number from 1 to 65535 (got '{raw_port}')")
    return host, int(raw_port)


def health_url(environ=None) -> str:
    host, port = bind(environ)
    if host in ("0.0.0.0", "::", "[::]"):  # "every interface": ask over loopback
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/health"


def check() -> int:
    import urllib.request

    try:
        with urllib.request.urlopen(health_url(), timeout=5) as resp:
            return 0 if resp.status == 200 else 1
    except Exception:  # noqa: BLE001 — not up (yet)
        return 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        host, port = bind()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if "--check" in argv:
        return check()
    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
