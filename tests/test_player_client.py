import base64
import json
import os
import tempfile
import unittest
from unittest import mock

import httpx

from tests import _env  # noqa: F401  (must come first)
from app import mpv_control
from app.config import settings

KEY = "pk-secret-123"


def _code(payload: dict) -> str:
    raw = json.dumps(payload).encode()
    return "MNP1-" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _fake_player(request: httpx.Request) -> httpx.Response:
    """A tiny stand-in for the movienight player's v1 API."""
    if request.headers.get("Authorization") != f"Bearer {KEY}":
        return httpx.Response(401, json={"ok": False, "error": "unauthorized"})
    assert "key" not in request.url.params
    path = request.url.path
    if path.endswith("/pair"):
        return httpx.Response(200, json={"ok": True, "player": "movienight-player", "name": "Den TV", "ready": True})
    if path.endswith("/status"):
        return httpx.Response(200, json={"ok": True, "state": "paused", "position_ms": 61000, "duration_ms": 7200000})
    if path.endswith("/play"):
        body = json.loads(request.content)
        if body.get("title") == "Missing":
            return httpx.Response(404, json={"ok": False, "error": "no matching movie"})
        return httpx.Response(200, json={"ok": True, "now_playing": {"title": body.get("title")}})
    if path.endswith("/seek"):
        return httpx.Response(200, json={"ok": True, "position_ms": 71000})
    if path.endswith("/pause") or path.endswith("/resume"):
        return httpx.Response(200, json={"ok": True, "changed": True})
    if path.endswith("/stop"):
        return httpx.Response(200, json={"ok": True, "state": "idle"})
    if path.endswith("/links"):
        return httpx.Response(200, json={"ok": True, "watch_url": "https://watch.example.org/", "viewer_url": "https://watch.example.org/?pwd=v", "admin_url": "https://watch.example.org/?pwd=a"})
    return httpx.Response(404, json={"ok": False})


_RealClient = httpx.AsyncClient


def _patched_client(*args, **kwargs):
    kwargs["transport"] = httpx.MockTransport(_fake_player)
    return _RealClient(*args, **kwargs)


class PairCodeTests(unittest.TestCase):
    def test_decode(self):
        d = mpv_control.decode_pair_code(_code({"v": 1, "u": "http://10.0.0.5:8080/", "p": None, "k": KEY}))
        self.assertEqual(d, {"url": "http://10.0.0.5:8080", "key": KEY, "public_url": ""})

    def test_bad_codes(self):
        for bad in ("hello", "MNP1-!!!", _code({"v": 2, "u": "x", "k": "y"}), _code({"v": 1, "u": "x"})):
            with self.assertRaises(ValueError):
                mpv_control.decode_pair_code(bad)

    def test_pair_file(self):
        path = os.path.join(tempfile.mkdtemp(), "pair.json")
        self.assertIsNone(mpv_control.load_pair_file(path))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "url": "http://player:8080", "api_key": KEY, "public_url": None}, fh)
        self.assertEqual(mpv_control.load_pair_file(path)["key"], KEY)


class MovienightClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(settings, "neko_mpv_shim_url", "http://player:8080"),
            mock.patch.object(settings, "player_key", KEY),
            mock.patch.object(settings, "player_mode", "movienight"),
            mock.patch.object(mpv_control.httpx, "AsyncClient", _patched_client),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        mpv_control._links.clear()

    async def test_key_is_a_bearer_header(self):
        self.assertEqual(mpv_control._headers(), {"Authorization": f"Bearer {KEY}"})

    async def test_status_play_seek(self):
        status = await mpv_control.get_status()
        self.assertEqual(status, {"time_ms": 61000, "duration_ms": 7200000, "state": "paused"})
        self.assertTrue(await mpv_control.play("Heat", 1995))
        self.assertFalse(await mpv_control.play("Missing"))
        self.assertTrue(await mpv_control.play_by_rating_key("123", 5000))
        self.assertEqual(await mpv_control.seek(10), 71)
        self.assertTrue(await mpv_control.pause())
        self.assertTrue(await mpv_control.resume())
        self.assertTrue(await mpv_control.stop())

    async def test_links_come_from_the_player(self):
        await mpv_control.refresh_links(force=True)
        self.assertEqual(mpv_control.viewer_link(), "https://watch.example.org/?pwd=v")
        self.assertEqual(mpv_control.admin_link(), "https://watch.example.org/?pwd=a")
        self.assertIn("https://watch.example.org", mpv_control.link_origins())

    async def test_connection_detects_movienight(self):
        found = await mpv_control.test_connection("http://player:8080", KEY)
        self.assertEqual(found["mode"], "movienight")
        self.assertEqual(found["name"], "Den TV")

    async def test_wrong_key_is_a_clear_error(self):
        with self.assertRaises(mpv_control.PlayerError):
            await mpv_control.test_connection("http://player:8080", "wrong")
        with mock.patch.object(settings, "player_key", "wrong"):
            with self.assertRaises(mpv_control.PlayerError):
                await mpv_control.get_status()


class NotConfiguredTests(unittest.IsolatedAsyncioTestCase):
    async def test_everything_degrades(self):
        with mock.patch.object(settings, "neko_mpv_shim_url", ""):
            self.assertEqual((await mpv_control.get_status())["state"], None)
            self.assertFalse(await mpv_control.play("x"))
            self.assertFalse(await mpv_control.pause())
            self.assertFalse(await mpv_control.stop())
            self.assertIsNone(await mpv_control.seek(10))


if __name__ == "__main__":
    unittest.main()
