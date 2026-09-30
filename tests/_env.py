"""Point the app at a throwaway data dir before any app module is imported."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="servarr-test-")
os.environ.setdefault("MPV_REMOTE_KEY", "testkey1234567890abcdef")
os.environ.setdefault("NEKO_MPV_PUBLIC_URL", "https://watch.example.com")


def load_main():
    """app.main imports bot.py, which builds discord.py persistent views at
    import time — those need a running event loop (uvicorn provides one in
    production). Import it inside a throwaway loop for the tests."""
    import asyncio
    import importlib

    async def _imp():
        return importlib.import_module("app.main")

    loop = asyncio.new_event_loop()
    return loop.run_until_complete(_imp())
