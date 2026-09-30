"""Point the app at a throwaway data dir before any app module is imported."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="servarr-test-")
os.environ.setdefault("MPV_REMOTE_KEY", "testkey1234567890abcdef")
os.environ.setdefault("NEKO_MPV_PUBLIC_URL", "https://watch.example.com")


def load_main():
    """Imports app.main the way production does: with an event loop running
    (uvicorn provides one), in case a module creates asyncio objects at
    import time."""
    import asyncio
    import importlib

    async def _imp():
        return importlib.import_module("app.main")

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_imp())
    finally:
        loop.close()
