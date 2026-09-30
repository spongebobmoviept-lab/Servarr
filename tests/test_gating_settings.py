import unittest

from tests import _env  # noqa: F401  (must come first)
from app import gating
from app.config import _env_list, settings


class GatingChannelSettingsTests(unittest.TestCase):
    def setUp(self):
        self._saved = (settings.leaderboard_channel, settings.movie_night_control_channel, settings.extra_read_only_channels)

    def tearDown(self):
        settings.leaderboard_channel, settings.movie_night_control_channel, settings.extra_read_only_channels = self._saved

    def test_defaults_are_generic(self):
        settings.leaderboard_channel = "leaderboard"
        settings.movie_night_control_channel = "movie-night-control"
        settings.extra_read_only_channels = []
        names = gating.read_only_channel_names()
        self.assertIn("leaderboard", names)
        self.assertIn("movie-night-control", names)
        self.assertNotIn("general", names)
        self.assertNotIn("requests", names)

    def test_configured_names_are_used(self):
        settings.leaderboard_channel = "top-chatters"
        settings.movie_night_control_channel = "dj-booth"
        settings.extra_read_only_channels = ["status", "announcements", "status"]
        names = gating.read_only_channel_names()
        for name in ("top-chatters", "dj-booth", "status", "announcements"):
            self.assertIn(name, names)
        self.assertEqual(names.count("status"), 1)
        self.assertNotIn("leaderboard", names)

    def test_env_list_parsing(self):
        import os

        os.environ["_SERVARR_TEST_LIST"] = " a, ,b ,c"
        self.assertEqual(_env_list("_SERVARR_TEST_LIST"), ["a", "b", "c"])
        self.assertEqual(_env_list("_SERVARR_TEST_MISSING"), [])


class PinnedTileTests(unittest.TestCase):
    def test_off_by_default(self):
        from app import release_digest

        self.assertEqual(settings.pinned_tile_channel, "")
        self.assertFalse(release_digest._tile_enabled())



class LogRedactionTests(unittest.TestCase):
    def test_secrets_redacted(self):
        from app.logger import _redact

        line = _redact("GET http://p/x?X-Plex-Token=abc123&token=zzz http://n/?pwd=hunter2 /mpv-remote#key=k1 ?apikey=q")
        for secret in ("abc123", "zzz", "hunter2", "k1", "=q"):
            self.assertNotIn(secret, line)


if __name__ == "__main__":
    unittest.main()
