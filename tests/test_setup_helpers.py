import unittest
from unittest import mock

import httpx

from tests import _env  # noqa: F401  (must come first)
from app import connections_store, friendly, mpv_control, settings_store
from app.config import settings


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://radarr:7878/api/v3/system/status?apikey=SECRET123")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


class FriendlyErrorTests(unittest.TestCase):
    def test_messages_are_plain_and_never_echo_secrets(self):
        cases = [
            httpx.ConnectError("[Errno 111] Connection refused"),
            httpx.ReadTimeout("timed out"),
            httpx.UnsupportedProtocol("Request URL is missing an 'http://' or 'https://' protocol."),
            _status_error(401),
            _status_error(404),
            _status_error(500),
            RuntimeError("GET http://x/?apikey=SECRET123 failed"),
        ]
        for exc in cases:
            msg = friendly.explain(exc, "Radarr")
            self.assertNotIn("SECRET123", msg)
            self.assertTrue(len(msg) > 20, msg)
        self.assertIn("rejected", friendly.explain(_status_error(401), "Radarr"))
        self.assertIn("LAN IP", friendly.explain(httpx.ConnectError("x"), "Radarr"))


class SettingsValidationTests(unittest.TestCase):
    def test_valid(self):
        settings_store.validate({"movie_night_hour_utc": 3, "leaderboard_channel": "top", "extra_read_only_channels": ["a"]})

    def test_invalid(self):
        for bad in ({"movie_night_hour_utc": 24}, {"xp_min_per_message": "5"}, {"extra_read_only_channels": "a"}, {"leaderboard_channel": 5}):
            with self.assertRaises(ValueError):
                settings_store.validate(bad)


class RemoteKeyTests(unittest.TestCase):
    def test_generated_once(self):
        with mock.patch.object(settings, "mpv_remote_key", ""):
            self.assertTrue(connections_store.ensure_remote_key())
            key = settings.mpv_remote_key
            self.assertGreaterEqual(len(key), 20)
            self.assertTrue(key.isalnum())
            self.assertFalse(connections_store.ensure_remote_key())
            self.assertEqual(settings.mpv_remote_key, key)


class PlayerHeaderTests(unittest.TestCase):
    def test_key_goes_in_header_not_url(self):
        with mock.patch.object(settings, "player_key", "pk-123"), mock.patch.object(settings, "neko_mpv_shim_url", "http://player:3000"):
            client = mpv_control._client()
            self.assertEqual(client.headers.get("Authorization"), "Bearer pk-123")
            self.assertNotIn("pk-123", str(client.base_url))

    def test_no_header_without_key(self):
        with mock.patch.object(settings, "player_key", ""):
            self.assertEqual(mpv_control._headers(), {})


if __name__ == "__main__":
    unittest.main()
