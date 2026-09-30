"""Movie Night only mode (SERVARR_MODE=movienight), and that Everything mode
is unchanged. Fakes only: no network, no real Discord, no real player."""

import asyncio
import base64
import json
import os
import stat
import tempfile
import types
import unittest
from unittest import mock

import discord
import httpx

from tests import _env  # noqa: F401  (must come first)
from fastapi.testclient import TestClient

from app import (
    auth,
    auth_store,
    bot,
    connections_store,
    features,
    gating,
    invite,
    movie_night,
    mpv_control,
    player_library,
    plex,
    plex_monitor,
    radarr,
    reclaimarr_client,
    release_digest,
    serve,
    settings_store,
    xp_store,
)
from app.config import Settings, settings

main = _env.load_main()
app = main.app

ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:correct-horse-battery").decode()}
TOKEN = "FAKE.bot-token.for-tests-only-0123456789abcdefWXYZ"  # not a real token

MOVIE_NIGHT_COMMANDS = {
    "movie-night", "movie-night-play", "play-movie", "stop-movie",
    "mpv-play", "mpv-pause", "mpv-resume", "mpv-seek", "mpv-stop", "help",
}
FULL_ONLY_COMMANDS = {"upcoming", "refresh-calendar", "rank", "leaderboard", "setup-server", "kick", "ban", "timeout", "warn"}
# The invite's permission number in 1.1 (Everything mode must keep asking for exactly this).
FULL_PERMISSIONS_1_1 = sum(1 << bit for bit in (1, 2, 4, 5, 6, 10, 11, 13, 14, 15, 16, 28, 35, 38, 40))


def in_mode(mode: str, locked: bool = False):
    """Everything ("full") or Movie Night only ("movienight"), chosen on the
    setup page or, with locked=True, set by SERVARR_MODE."""
    if locked:
        return mock.patch.multiple(settings, servarr_mode_env=mode, servarr_mode="")
    return mock.patch.multiple(settings, servarr_mode_env="", servarr_mode=mode)


class _Isolated:
    """Restores the settings singleton after each test and points the two
    settings stores at throwaway files."""

    def setUp(self):
        super().setUp()
        self._snapshot = dict(vars(settings))
        self.tmp = tempfile.mkdtemp(prefix="servarr-mode-test-")
        self._store_patches = [
            mock.patch.object(connections_store, "_overrides_path", os.path.join(self.tmp, "connections_overrides.json")),
            mock.patch.object(settings_store, "_overrides_path", os.path.join(self.tmp, "settings_overrides.json")),
        ]
        self._store_patches.append(mock.patch("app.logger.print", create=True))  # keep the test output quiet
        for p in self._store_patches:
            p.start()
        auth._failed_attempts.clear()

    def tearDown(self):
        for p in self._store_patches:
            p.stop()
        vars(settings).clear()
        vars(settings).update(self._snapshot)
        super().tearDown()


class _Api(_Isolated):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not auth_store.is_admin_configured():
            auth_store.set_admin("admin", "correct-horse-battery")
        cls.client = TestClient(app)


def _text_channel(name: str, channel_id: int = 1):
    channel = mock.Mock(spec=discord.TextChannel)
    channel.name = name
    channel.id = channel_id
    channel.send = mock.AsyncMock(return_value=mock.Mock(edit=mock.AsyncMock()))
    return channel


def _http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://discord.com/api/v10/users/@me")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


# ---------------------------------------------------------------------------
# mode resolution
# ---------------------------------------------------------------------------


class ModeResolutionTests(_Isolated, unittest.TestCase):
    def test_default_is_full(self):
        self.assertEqual(features.resolve("", ""), "full")
        self.assertEqual(features.resolve(None, None), "full")
        with mock.patch.multiple(settings, servarr_mode_env="", servarr_mode=""):
            self.assertEqual(features.current(), "full")
            self.assertFalse(features.is_movienight())
            self.assertFalse(features.locked())
            self.assertFalse(features.chosen())

    def test_env_wins_over_the_stored_choice(self):
        self.assertEqual(features.resolve("movienight", "full"), "movienight")
        self.assertEqual(features.resolve("full", "movienight"), "full")
        with mock.patch.multiple(settings, servarr_mode_env="full", servarr_mode="movienight"):
            self.assertEqual(features.current(), "full")
            self.assertTrue(features.locked())

    def test_stored_choice_is_used_without_env(self):
        self.assertEqual(features.resolve("", "movienight"), "movienight")
        with in_mode("movienight"):
            self.assertTrue(features.is_movienight())
            self.assertFalse(features.locked())
            self.assertTrue(features.chosen())

    def test_spellings_and_unknown_values(self):
        for value in ("movienight", " MovieNight ", "movie-night", "MOVIE_NIGHT", "Movie Night only"):
            self.assertEqual(features.normalize(value), "movienight", value)
        for value in ("full", "FULL", "everything"):
            self.assertEqual(features.normalize(value), "full", value)
        for value in ("", "banana", "guest", None, 1, True):
            self.assertEqual(features.normalize(value), "", value)
        # An unknown SERVARR_MODE is ignored (and reported), it doesn't lock anything.
        with mock.patch.multiple(settings, servarr_mode_env="banana", servarr_mode="movienight"):
            self.assertEqual(features.current(), "movienight")
            self.assertFalse(features.locked())
            self.assertEqual(features.unknown_env_value(), "banana")

    def test_settings_read_the_environment(self):
        with mock.patch.dict(os.environ, {"SERVARR_MODE": " movienight "}):
            self.assertEqual(Settings().servarr_mode_env, "movienight")
        env = {k: v for k, v in os.environ.items() if k != "SERVARR_MODE"}
        with mock.patch.dict(os.environ, env, clear=True):
            fresh = Settings()
            self.assertEqual((fresh.servarr_mode_env, fresh.servarr_mode), ("", ""))
            self.assertEqual(features.resolve(fresh.servarr_mode_env, fresh.servarr_mode), "full")

    def test_the_choice_is_kept_in_the_settings_store(self):
        with mock.patch.multiple(settings, servarr_mode_env="", servarr_mode=""):
            settings_store.save_mode("movienight")
            with open(settings_store._overrides_path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["servarr_mode"], "movienight")
            settings.servarr_mode = ""
            settings_store.load_overrides()
            self.assertEqual(features.current(), "movienight")

    def test_env_value_is_never_written_to_the_store(self):
        with mock.patch.multiple(settings, servarr_mode_env="movienight", servarr_mode=""):
            settings_store.save_overrides({"movie_night_candidate_count": 4})
            with open(settings_store._overrides_path, encoding="utf-8") as fh:
                self.assertNotIn("servarr_mode", json.load(fh))


# ---------------------------------------------------------------------------
# Movie Night only: everything else is off
# ---------------------------------------------------------------------------


class CommandTests(_Isolated, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.sync = mock.AsyncMock(return_value=[])
        self._patches = [
            mock.patch.object(bot.tree, "sync", self.sync),
            mock.patch.object(bot, "log", mock.AsyncMock()),
            mock.patch.object(bot, "_synced_guild_id", ""),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()

    def _registered(self, guild_id=None):
        guild = discord.Object(id=guild_id) if guild_id else None
        return {c.name for c in bot.tree.get_commands(guild=guild)}

    def test_command_lists(self):
        self.assertEqual(set(bot.command_names("movienight")), MOVIE_NIGHT_COMMANDS)
        self.assertEqual(set(bot.command_names("full")), MOVIE_NIGHT_COMMANDS | FULL_ONLY_COMMANDS)
        self.assertEqual(features.FULL_ONLY_COMMANDS, FULL_ONLY_COMMANDS)

    async def test_movienight_syncs_only_movie_night_commands(self):
        settings.discord_guild_id = "4242"
        with in_mode("movienight"):
            await bot.sync_commands()
        self.assertEqual(self._registered(4242), MOVIE_NIGHT_COMMANDS)
        self.assertEqual(self._registered(), set())  # nothing global
        synced = [call.kwargs.get("guild") for call in self.sync.await_args_list]
        self.assertEqual([getattr(g, "id", None) for g in synced], [4242, None])

    async def test_movienight_global_sync_without_a_server(self):
        settings.discord_guild_id = ""
        with in_mode("movienight", locked=True):
            await bot.sync_commands()
        self.assertEqual(self._registered(), MOVIE_NIGHT_COMMANDS)

    async def test_full_mode_syncs_every_command_as_before(self):
        settings.discord_guild_id = "4242"
        with in_mode("full"):
            await bot.sync_commands()
        self.assertEqual(self._registered(4242), MOVIE_NIGHT_COMMANDS | FULL_ONLY_COMMANDS)
        self.assertEqual(self._registered(), set())
        settings.discord_guild_id = ""
        with in_mode("full"):
            await bot.sync_commands()
        self.assertEqual(self._registered(), MOVIE_NIGHT_COMMANDS | FULL_ONLY_COMMANDS)

    async def test_switching_back_to_full_brings_the_commands_back(self):
        settings.discord_guild_id = "4242"
        with in_mode("movienight"):
            await bot.sync_commands()
        with in_mode("full"):
            await bot.sync_commands()
        self.assertEqual(self._registered(4242), MOVIE_NIGHT_COMMANDS | FULL_ONLY_COMMANDS)

    async def test_picking_another_server_clears_the_old_one(self):
        settings.discord_guild_id = "4242"
        await bot.sync_commands()
        settings.discord_guild_id = "777"
        await bot.sync_commands()
        self.assertEqual(self._registered(4242), set())
        self.assertEqual(self._registered(777), MOVIE_NIGHT_COMMANDS | FULL_ONLY_COMMANDS)

    async def test_help_lists_only_movie_night_in_movienight_mode(self):
        interaction = mock.Mock()
        interaction.guild = None
        interaction.response.send_message = mock.AsyncMock()
        with in_mode("movienight"):
            await bot.help_command.callback(interaction)
        text = json.dumps(interaction.response.send_message.await_args.kwargs["embed"].to_dict())
        self.assertIn("/play-movie", text)
        for gone in ("/rank", "/kick", "/upcoming", "Requestarr"):
            self.assertNotIn(gone, text)
        with in_mode("full"):
            await bot.help_command.callback(interaction)
        text = json.dumps(interaction.response.send_message.await_args.kwargs["embed"].to_dict(), ensure_ascii=False)
        for kept in ("/rank", "/kick", "/upcoming", "Requestarr", "A vote posts in #general every evening"):
            self.assertIn(kept, text)


class BackgroundTaskTests(_Isolated, unittest.IsolatedAsyncioTestCase):
    MOVIE_NIGHT = ["movie_night_vote", "movie_night_announce", "movie_night_status", "player_pairing"]

    def test_wanted_tasks(self):
        self.assertEqual(features.wanted_tasks("movienight"), self.MOVIE_NIGHT)
        self.assertEqual(features.wanted_tasks("full"), ["plex_monitor", "release_digest", *self.MOVIE_NIGHT])

    async def test_full_only_loops_never_start_in_movienight_mode(self):
        started = []

        def factory(name):
            async def loop():
                started.append(name)
                await asyncio.Event().wait()

            return loop

        factories = {name: factory(name) for name in features.TASK_MODES}
        try:
            with in_mode("movienight"):
                features.reconcile_tasks(factories)
                await asyncio.sleep(0)
                self.assertEqual(sorted(features.running_tasks()), sorted(self.MOVIE_NIGHT))
                self.assertEqual(sorted(started), sorted(self.MOVIE_NIGHT))
            with in_mode("full"):  # switching to Everything starts the rest...
                features.reconcile_tasks(factories)
                await asyncio.sleep(0)
                self.assertEqual(sorted(features.running_tasks()), sorted(features.TASK_MODES))
            with in_mode("movienight"):  # ...and switching back stops them again
                features.reconcile_tasks(factories)
                await asyncio.sleep(0)
                self.assertEqual(sorted(features.running_tasks()), sorted(self.MOVIE_NIGHT))
        finally:
            features.stop_tasks()
            await asyncio.sleep(0)
        self.assertEqual(features.running_tasks(), [])

    async def test_full_only_loops_return_at_once_if_called_anyway(self):
        settings.tautulli_url, settings.tautulli_api_key = "http://tautulli.example", "k"
        with in_mode("movienight"), mock.patch.object(plex_monitor, "get_activity", mock.AsyncMock()) as activity:
            await asyncio.wait_for(plex_monitor.poll_loop(), timeout=1)
            await asyncio.wait_for(release_digest.daily_loop(), timeout=1)
            activity.assert_not_called()

    async def test_reclaimarr_is_not_called(self):
        settings.reclaimarr_url = "http://reclaimarr.example"
        with in_mode("movienight"), mock.patch.object(reclaimarr_client.httpx, "AsyncClient", side_effect=AssertionError("no network")):
            self.assertFalse(await reclaimarr_client.pause_upgrade(7, 240))


class ListenerTests(_Isolated, unittest.IsolatedAsyncioTestCase):
    def _message(self, channel_name="chat"):
        message = mock.Mock()
        message.author.bot = False
        message.author.id = 99
        message.author.send = mock.AsyncMock()
        message.content = "hello"
        message.attachments = []
        message.guild.roles = []
        message.channel = _text_channel(channel_name)
        return message

    async def _run(self, message):
        with mock.patch.object(bot, "_already_warned", set()),                 mock.patch.object(bot.release_digest, "on_channel_message") as digest, \
                mock.patch.object(xp_store.store, "try_add_xp", mock.AsyncMock(return_value=None)) as xp, \
                mock.patch.object(bot, "_relocate_stray_message", mock.AsyncMock()) as relocate, \
                mock.patch.object(bot, "_mod_log", mock.AsyncMock()):
            await bot.on_message(message)
        return digest, xp, relocate

    async def test_on_message_does_nothing_in_movienight_mode(self):
        for name in ("chat", "rules"):  # "rules" is a read-only channel in Everything mode
            with in_mode("movienight"):
                digest, xp, relocate = await self._run(self._message(name))
            digest.assert_not_called()
            xp.assert_not_called()
            relocate.assert_not_called()

    async def test_on_message_still_works_in_full_mode(self):
        with in_mode("full"):
            digest, xp, relocate = await self._run(self._message("chat"))
            digest.assert_called_once()
            xp.assert_awaited_once_with(99)
            relocate.assert_not_called()
            _, xp, relocate = await self._run(self._message("rules"))
            relocate.assert_awaited_once()
            xp.assert_not_called()

    async def test_rules_gate_button_is_off(self):
        interaction = mock.Mock()
        interaction.response.send_message = mock.AsyncMock()
        interaction.user.add_roles = mock.AsyncMock()
        with in_mode("movienight"):
            await gating.RulesGateView().agree.callback(interaction)
        interaction.user.add_roles.assert_not_called()
        self.assertIn("turned off", interaction.response.send_message.await_args.args[0])

    async def test_persistent_views_and_intents_follow_the_mode(self):
        try:
            with in_mode("movienight"), mock.patch.object(bot.client, "add_view") as add_view:
                bot._register_views()
                bot._configure_intents()
                self.assertEqual([type(c.args[0]).__name__ for c in add_view.call_args_list], ["CommanderPanelView"])
                self.assertFalse(bot.intents.message_content)  # never reads messages
                self.assertTrue(bot.intents.members)
            with in_mode("full"), mock.patch.object(bot.client, "add_view") as add_view:
                bot._register_views()
                bot._configure_intents()
                self.assertEqual([type(c.args[0]).__name__ for c in add_view.call_args_list], ["RulesGateView", "CommanderPanelView"])
                self.assertTrue(bot.intents.message_content)
                self.assertTrue(bot.intents.members)
        finally:
            bot.intents.message_content = bot.intents.members = True

    async def test_blocked_command_is_not_posted_to_the_mod_log(self):
        interaction = mock.Mock()
        interaction.response.send_message = mock.AsyncMock()
        error = discord.app_commands.CheckFailure("no")
        with mock.patch.object(bot, "_mod_log", mock.AsyncMock()) as mod_log:
            with in_mode("movienight"):
                await bot.on_tree_error(interaction, error)
            mod_log.assert_not_called()
            with in_mode("full"):
                await bot.on_tree_error(interaction, error)
            mod_log.assert_awaited_once()


# ---------------------------------------------------------------------------
# setup API
# ---------------------------------------------------------------------------


class SetupApiTests(_Api, unittest.TestCase):
    REFUSED = [
        ("/api/setup/test-radarr", {"url": "http://radarr.example:7878", "api_key": "k"}),
        ("/api/setup/test-sonarr", {"url": "http://sonarr.example:8989", "api_key": "k"}),
        ("/api/setup/test-tautulli", {"url": "http://tautulli.example:8181", "api_key": "k"}),
        ("/api/setup/test-plex", {"url": "http://plex.example:32400", "token": "t"}),
        ("/api/setup/test-reclaimarr", {"url": "http://reclaimarr.example", "auth_user": "u", "auth_pass": "p"}),
        ("/api/connections", {"radarr_url": "http://radarr.example:7878"}),
        ("/api/connections", {"plex_token": "t"}),
        ("/api/settings", {"xp_min_per_message": 5}),
        ("/api/settings", {"mod_log_channel_id": "1"}),
        ("/api/release-digest/force", None),
    ]

    def test_other_features_endpoints_refuse_in_movienight_mode(self):
        with in_mode("movienight"):
            for path, body in self.REFUSED:
                r = self.client.post(path, json=body, headers=ADMIN)
                self.assertEqual(r.status_code, 409, path)
                self.assertIn("Movie Night only mode", r.json()["detail"], path)
                self.assertIn("Switch to Everything", r.json()["detail"], path)
        self.assertEqual(settings.radarr_url, self._snapshot["radarr_url"])  # nothing was saved

    def test_refusal_says_how_when_the_mode_is_locked(self):
        with in_mode("movienight", locked=True):
            r = self.client.post("/api/setup/test-radarr", json={"url": "x", "api_key": "k"}, headers=ADMIN)
        self.assertEqual(r.status_code, 409)
        self.assertIn("SERVARR_MODE=full", r.json()["detail"])

    def test_login_is_still_checked_first(self):
        with in_mode("movienight"):
            for path, body in self.REFUSED:
                self.assertEqual(self.client.post(path, json=body).status_code, 401, path)

    def test_movie_night_settings_still_save_in_movienight_mode(self):
        with in_mode("movienight"):
            r = self.client.post("/api/settings", json={"movie_night_vote_hour_utc": 20, "movie_night_channel_id": "123"}, headers=ADMIN)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(set(r.json()), features.MOVIE_NIGHT_SETTINGS)
            self.assertEqual(settings.movie_night_vote_hour_utc, 20)
            shown = self.client.get("/api/connections", headers=ADMIN).json()
            self.assertEqual(set(shown), features.MOVIE_NIGHT_CONNECTIONS)
            self.assertEqual(self.client.post("/api/movie-night/force", headers=ADMIN).status_code, 503)  # allowed; bot just isn't connected
        r = self.client.post("/api/settings", json={"movie_night_channel_id": "general"}, headers=ADMIN)
        self.assertEqual(r.status_code, 400)

    def test_full_mode_endpoints_are_unchanged(self):
        with in_mode("full"), mock.patch.object(main.radarr, "test_connection", mock.AsyncMock(return_value={"version": "5.0"})), \
                mock.patch.object(main.plex, "test_connection", mock.AsyncMock(return_value={"library_count": 2, "movie_libraries": ["Movies"]})):
            r = self.client.post("/api/setup/test-radarr", json={"url": "http://radarr.example:7878", "api_key": "k"}, headers=ADMIN)
            self.assertEqual((r.status_code, r.json()), (200, {"version": "5.0"}))
            self.assertEqual(self.client.post("/api/setup/test-plex", json={"url": "x", "token": "t"}, headers=ADMIN).status_code, 200)
            r = self.client.post("/api/settings", json={"xp_min_per_message": 5, "mod_log_channel_id": "1"}, headers=ADMIN)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(set(r.json()), set(settings_store.EDITABLE_KEYS))
            r = self.client.post("/api/connections", json={"radarr_url": "http://radarr.example:7878"}, headers=ADMIN)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(set(r.json()), set(connections_store.ALL_KEYS))
            self.assertEqual(settings.radarr_url, "http://radarr.example:7878")
            self.assertEqual(self.client.post("/api/release-digest/force", headers=ADMIN).status_code, 503)  # not refused

    def test_status_reports_the_mode(self):
        with in_mode("movienight", locked=True):
            status = self.client.get("/api/setup/status").json()
        self.assertEqual((status["mode"], status["mode_locked"], status["mode_chosen"]), ("movienight", True, True))
        with mock.patch.multiple(settings, servarr_mode_env="", servarr_mode=""):
            status = self.client.get("/api/setup/status").json()
        self.assertEqual((status["mode"], status["mode_locked"], status["mode_chosen"]), ("full", False, False))

    def test_switching_mode_on_the_setup_page(self):
        with mock.patch.multiple(settings, servarr_mode_env="", servarr_mode=""), \
                mock.patch.object(main, "_apply_mode_change", mock.AsyncMock()) as apply_change:
            self.assertEqual(self.client.post("/api/setup/mode", json={"mode": "movienight"}).status_code, 401)
            self.assertEqual(self.client.post("/api/setup/mode", json={"mode": "banana"}, headers=ADMIN).status_code, 400)
            r = self.client.post("/api/setup/mode", json={"mode": "movienight"}, headers=ADMIN)
            self.assertEqual((r.status_code, r.json()["mode"], r.json()["mode_locked"]), (200, "movienight", False))
            self.assertTrue(features.is_movienight())
            apply_change.assert_awaited_once()  # loops and the bot follow the new mode
            self.client.post("/api/setup/mode", json={"mode": "movienight"}, headers=ADMIN)
            apply_change.assert_awaited_once()  # nothing to do when it didn't change

    def test_mode_set_by_env_cannot_be_switched(self):
        with in_mode("movienight", locked=True), mock.patch.object(main, "_apply_mode_change", mock.AsyncMock()) as apply_change:
            r = self.client.post("/api/setup/mode", json={"mode": "full"}, headers=ADMIN)
            self.assertEqual(r.status_code, 409)
            self.assertIn("SERVARR_MODE", r.json()["detail"])
            self.assertTrue(features.is_movienight())
            self.assertEqual(settings.servarr_mode, "")
            apply_change.assert_not_called()

    def test_setup_page_has_only_this_modes_sections(self):
        with in_mode("movienight", locked=True):
            r = self.client.get("/setup")
        self.assertEqual(r.status_code, 200)
        for wanted in ('data-step="mode"', 'data-step="mn-token"', 'data-step="mn-invite"', 'data-step="mn-server"', 'data-step="mn-player"', 'data-step="mn-done"'):
            self.assertIn(wanted, r.text)
        for gone in ('id="radarr-url"', 'id="sonarr-url"', 'id="plex-token"', 'id="tautulli-url"', 'id="reclaimarr-url"', 'id="xp-min"', 'id="mod-log-channel"', 'data-step="3"'):
            self.assertNotIn(gone, r.text)
        with in_mode("full"):
            page = self.client.get("/setup").text
        self.assertIn('id="radarr-url"', page)
        self.assertIn('data-step="7"', page)
        self.assertNotIn('data-step="mn-token"', page)
        with mock.patch.multiple(settings, servarr_mode_env="", servarr_mode=""):  # fresh install: the page asks first
            page = self.client.get("/setup").text
        self.assertIn('id="radarr-url"', page)
        self.assertIn('data-step="mn-token"', page)
        self.assertNotIn("https://", page.split("<script>")[0].replace("https://watch.example.com", ""))  # no external assets

    def test_movie_night_channel_wizard_step(self):
        client = self.client
        channels = [{"id": "555", "name": "movie-night", "type": 0}, {"id": "556", "name": "dj-booth", "type": 0}, {"id": "600", "name": "Voice", "type": 2}]
        settings.discord_bot_token, settings.discord_guild_id = TOKEN, ""
        with in_mode("movienight"), mock.patch.object(main, "_discord_get", mock.AsyncMock(return_value=channels)) as discord_get, \
                mock.patch.object(bot, "request_resync") as resync, mock.patch.object(bot, "channel_problems", return_value=["Send Messages"]):
            body = {"guild_id": "42", "channel_id": "555", "dj_role_id": "77", "control_channel": "dj-booth"}
            r = client.post("/api/setup/movienight-server", json=body, headers=ADMIN)
            self.assertEqual((r.status_code, r.json()["channel"], r.json()["missing_permissions"]), (200, "movie-night", ["Send Messages"]))
            discord_get.assert_awaited_once_with("/guilds/42/channels", TOKEN)
            self.assertEqual((settings.discord_guild_id, settings.movie_night_channel_id), ("42", "555"))
            self.assertEqual((settings.movie_night_dj_role_id, settings.movie_night_control_channel), ("77", "dj-booth"))
            resync.assert_called_once_with()  # slash commands move to the picked server
            for bad in ({**body, "channel_id": "600"}, {**body, "channel_id": ""}, {**body, "guild_id": "x"}, {**body, "control_channel": "nope"}):
                self.assertEqual(client.post("/api/setup/movienight-server", json=bad, headers=ADMIN).status_code, 400, bad)
            self.assertEqual(client.post("/api/setup/movienight-server", json=body).status_code, 401)

    def test_first_choice_is_saved_with_the_admin_login(self):
        with mock.patch.multiple(settings, servarr_mode_env="", servarr_mode=""), \
                mock.patch.object(main.auth_store, "is_admin_configured", return_value=False), \
                mock.patch.object(main.auth_store, "set_admin") as set_admin, \
                mock.patch.object(main, "_apply_mode_change", mock.AsyncMock()) as apply_change:
            r = self.client.post("/api/setup/admin", json={"username": "me", "password": "long-enough-pw", "mode": "banana"})
            self.assertEqual(r.status_code, 400)
            set_admin.assert_not_called()
            r = self.client.post("/api/setup/admin", json={"username": "me", "password": "long-enough-pw", "mode": "movienight"})
            self.assertEqual((r.status_code, r.json()["mode"]), (200, "movienight"))
            set_admin.assert_called_once_with("me", "long-enough-pw")
            apply_change.assert_awaited_once()

    def test_companion_player_is_refused_in_movienight_mode(self):
        found = {"mode": "companion", "name": "Plex Companion player", "ready": True, "edition": None}
        settings.neko_mpv_shim_url = ""
        with in_mode("movienight"), mock.patch.object(mpv_control, "test_connection", mock.AsyncMock(return_value=found)), \
                mock.patch.object(mpv_control, "refresh_links", mock.AsyncMock()):
            r = self.client.post("/api/setup/pair-player", json={"url": "http://192.168.1.50:3000"}, headers=ADMIN)
            self.assertEqual(r.status_code, 400)
            self.assertIn("Movie Night player", r.json()["detail"])
            self.assertEqual(settings.neko_mpv_shim_url, "")
            settings.neko_mpv_shim_url, settings.player_mode = "http://192.168.1.50:3000", "companion"
            self.assertFalse(mpv_control.is_configured())  # a saved Companion player doesn't count either
        with in_mode("full"):
            self.assertTrue(mpv_control.is_configured())

    def test_web_remote_library_comes_from_the_player(self):
        key = {"X-Remote-Key": settings.mpv_remote_key}
        items = [{"rating_key": "5", "title": "Heat", "year": 1995, "type": "movie", "index": None, "poster_url": "/api/mpv/poster/5"}]
        settings.neko_mpv_shim_url, settings.player_mode, settings.player_key = "http://192.168.1.50:8090", "movienight", "k"
        with in_mode("movienight"), mock.patch.object(player_library, "search", mock.AsyncMock(return_value=items)) as search, \
                mock.patch.object(player_library, "sections", mock.AsyncMock(return_value=[{"key": "1", "title": "Movies", "type": "movie"}])), \
                mock.patch.object(player_library, "poster", mock.AsyncMock(return_value=(b"jpeg", "image/jpeg"))), \
                mock.patch.object(plex, "search_library", mock.AsyncMock(side_effect=AssertionError("Servarr's own Plex is off"))):
            r = self.client.get("/api/mpv/search", params={"q": "heat"}, headers=key)
            self.assertEqual((r.status_code, r.json()), (200, items))
            search.assert_awaited_once_with("heat")
            self.assertEqual(self.client.get("/api/mpv/sections", headers=key).json()[0]["title"], "Movies")
            self.assertEqual(self.client.get("/api/mpv/poster/5", headers=key).content, b"jpeg")
            settings.neko_mpv_shim_url = ""
            self.assertEqual(self.client.get("/api/mpv/search", params={"q": "heat"}, headers=key).status_code, 503)
        with in_mode("full"), mock.patch.object(plex, "search_library", mock.AsyncMock(return_value=[])) as own_plex:
            self.assertEqual(self.client.get("/api/mpv/search", params={"q": "heat"}, headers=key).status_code, 200)
            own_plex.assert_awaited_once()


# ---------------------------------------------------------------------------
# invite link
# ---------------------------------------------------------------------------


class InviteTests(_Api, unittest.TestCase):
    def test_movie_night_asks_for_four_permissions(self):
        self.assertEqual(invite.permission_names("movienight"), ["View Channels", "Send Messages", "Embed Links", "Read Message History"])
        value = invite.permissions_value("movienight")
        self.assertEqual(value, (1 << 10) | (1 << 11) | (1 << 14) | (1 << 16))
        self.assertEqual(value, 84992)
        granted = discord.Permissions(value)
        self.assertTrue(granted.view_channel and granted.send_messages and granted.embed_links and granted.read_message_history)
        for unwanted in ("administrator", "manage_guild", "manage_channels", "manage_roles", "manage_messages", "kick_members",
                         "ban_members", "moderate_members", "mention_everyone", "attach_files"):
            self.assertFalse(getattr(granted, unwanted), unwanted)
        # The names shown on the setup page are exactly what the number grants.
        named = discord.Permissions(**{attr: True for _, attr, _ in invite.MOVIE_NIGHT_PERMISSIONS})
        self.assertEqual(named.value, value)
        self.assertEqual([1 << bit for _, _, bit in invite.MOVIE_NIGHT_PERMISSIONS], [1024, 2048, 16384, 65536])

    def test_full_mode_invite_is_unchanged(self):
        self.assertEqual(invite.permissions_value("full"), FULL_PERMISSIONS_1_1)

    def test_invite_url(self):
        url = invite.invite_url("123456789012345678", "movienight")
        self.assertEqual(url, "https://discord.com/oauth2/authorize?client_id=123456789012345678&scope=bot%20applications.commands&permissions=84992")
        self.assertIn("client_id=123&", invite.invite_url("12<script>3", "full"))  # only digits get through

    def test_invite_endpoint_uses_the_bots_application_id(self):
        me = mock.AsyncMock(return_value={"id": "123456789012345678", "username": "MovieBot", "bot": True})
        settings.discord_bot_token = TOKEN
        with in_mode("movienight"), mock.patch.object(main, "_discord_get", me):
            r = self.client.get("/api/setup/invite", headers=ADMIN)
            self.assertEqual(r.status_code, 200)
            self.assertIn("client_id=123456789012345678", r.json()["invite_url"])
            self.assertIn("permissions=84992", r.json()["invite_url"])
            self.assertEqual(r.json()["permissions"], ["View Channels", "Send Messages", "Embed Links", "Read Message History"])
            self.assertNotIn(TOKEN, r.text)
        me.assert_awaited_once_with("/users/@me", TOKEN)
        guilds = mock.AsyncMock(side_effect=[{"id": "123", "username": "Servarr"}, [{"id": "9", "name": "Den"}]])
        with in_mode("full"), mock.patch.object(main, "_discord_get", guilds):
            r = self.client.post("/api/setup/discord-guilds", json={}, headers=ADMIN)
            self.assertEqual(r.json()["guilds"], [{"id": "9", "name": "Den"}])
            self.assertIn(f"permissions={FULL_PERMISSIONS_1_1}", r.json()["invite_url"])
        settings.discord_bot_token = ""
        self.assertEqual(self.client.get("/api/setup/invite", headers=ADMIN).status_code, 400)


# ---------------------------------------------------------------------------
# saving the bot token on the setup page
# ---------------------------------------------------------------------------


class TokenSaveTests(_Api, unittest.TestCase):
    ME = {"id": "123456789012345678", "username": "MovieBot", "bot": True}

    def _save(self, token=TOKEN, me=None, **kwargs):
        discord_get = mock.AsyncMock(return_value=self.ME) if me is None else me
        with mock.patch.object(main, "_discord_get", discord_get), mock.patch.object(bot, "request_restart") as restart:
            r = self.client.post("/api/setup/discord-token", json={"token": token}, **kwargs)
        return r, restart, discord_get

    def test_token_is_saved_privately_and_never_echoed(self):
        settings.discord_bot_token = ""
        with in_mode("movienight"):
            r, restart, discord_get = self._save(headers=ADMIN)
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(TOKEN, r.text)
            self.assertEqual(r.json()["username"], "MovieBot")
            self.assertIn("permissions=84992", r.json()["invite_url"])
            discord_get.assert_awaited_once_with("/users/@me", TOKEN)
            self.assertEqual(settings.discord_bot_token, TOKEN)
            restart.assert_called_once_with()  # the bot connects right away

            path = connections_store._overrides_path
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["discord_bot_token"], TOKEN)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertFalse(os.path.exists(path + ".tmp"))

            shown = self.client.get("/api/connections", headers=ADMIN)
            self.assertNotIn(TOKEN, shown.text)
            self.assertEqual(shown.json()["discord_bot_token"], {"value": "•••••" + TOKEN[-4:], "is_secret": True, "is_set": True})
            self.assertTrue(self.client.get("/api/setup/status").json()["discord_configured"])
        with open(settings.log_file, encoding="utf-8") as fh:
            self.assertNotIn(TOKEN, fh.read())

    def test_an_existing_loose_file_is_tightened(self):
        if os.name != "posix":
            self.skipTest("file modes are a POSIX thing")
        path = connections_store._overrides_path
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"discord_bot_token": "old"}, fh)
        os.chmod(path, 0o644)  # what 1.1 wrote
        connections_store.load_overrides()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(settings.discord_bot_token, "old")

    def test_a_rejected_token_is_not_saved(self):
        settings.discord_bot_token = ""
        r, restart, _ = self._save(me=mock.AsyncMock(side_effect=_http_error(401)), headers=ADMIN)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Reset Token", r.json()["detail"])
        self.assertNotIn(TOKEN, r.text)
        self.assertEqual(settings.discord_bot_token, "")
        restart.assert_not_called()
        for bad in ("", "   ", "two words", "x" * 300):
            r, restart, discord_get = self._save(token=bad, headers=ADMIN)
            self.assertEqual(r.status_code, 400, bad)
            discord_get.assert_not_called()
        r, restart, _ = self._save(me=mock.AsyncMock(return_value={"id": "1", "username": "someone", "bot": False}), headers=ADMIN)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(settings.discord_bot_token, "")

    def test_needs_the_admin_login(self):
        r, restart, discord_get = self._save()
        self.assertEqual(r.status_code, 401)
        discord_get.assert_not_called()
        restart.assert_not_called()

    def test_saving_the_same_token_again_does_not_reconnect_a_running_bot(self):
        settings.discord_bot_token = TOKEN
        with mock.patch.object(bot, "status", return_value={"connected": True}):
            r, restart, _ = self._save(headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        restart.assert_not_called()

    def test_full_wizard_token_save_also_starts_the_bot(self):
        settings.discord_bot_token = ""
        with in_mode("full"), mock.patch.object(bot, "request_restart") as restart, mock.patch.object(bot, "request_resync") as resync, \
                mock.patch.object(mpv_control, "refresh_links", mock.AsyncMock()):
            r = self.client.post("/api/connections", json={"discord_bot_token": TOKEN, "discord_guild_id": "42"}, headers=ADMIN)
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(TOKEN, r.text)
            restart.assert_called_once_with()
            self.client.post("/api/connections", json={"discord_guild_id": "43"}, headers=ADMIN)
            restart.assert_called_once_with()  # token unchanged: only the commands move
            resync.assert_called_once_with()


class BotLifecycleTests(_Isolated, unittest.IsolatedAsyncioTestCase):
    """bot.launch()/restart() against a fake Discord: client.start() is
    replaced, everything around it (close, re-open, views) is discord.py's own."""

    def setUp(self):
        super().setUp()
        self.starts = []
        self.fail_with = []
        self.log = mock.AsyncMock()
        self._patches = [
            mock.patch.object(bot.client, "start", self._fake_start),
            mock.patch.object(bot, "log", self.log),
            mock.patch.object(bot, "_runner", None),
            mock.patch.object(bot, "_members_fallback", False),
            mock.patch.object(bot, "_RETRY_SECONDS", (0,)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        bot.intents.message_content = bot.intents.members = True
        super().tearDown()

    async def _fake_start(self, token, *, reconnect=True):
        self.starts.append((token, bot.intents.members, bot.intents.message_content))
        if self.fail_with:
            raise self.fail_with.pop(0)
        await asyncio.Event().wait()  # "connected" until the task is cancelled

    async def _settle(self):
        for _ in range(20):
            await asyncio.sleep(0)

    def _logged(self):
        return " ".join(call.args[0] for call in self.log.await_args_list)

    async def test_no_token_means_no_connection(self):
        settings.discord_bot_token = ""
        await bot.start()
        self.assertEqual(self.starts, [])
        self.assertIn("no Discord bot token yet", self._logged())
        self.assertNotIn(".env", self._logged())

    async def test_saving_a_token_starts_the_bot_and_a_new_one_restarts_it(self):
        settings.discord_bot_token = ""
        with in_mode("movienight"):
            bot.launch()
            await self._settle()
            self.assertEqual(self.starts, [])  # nothing to connect with yet

            settings.discord_bot_token = "token-one"  # what the setup page's save does
            await bot.restart()
            await self._settle()
            self.assertEqual(self.starts, [("token-one", True, False)])  # no Message Content intent in this mode
            self.assertFalse(bot.client.is_closed())

            settings.discord_bot_token = "token-two"
            await bot.restart()
            await self._settle()
            self.assertEqual([s[0] for s in self.starts], ["token-one", "token-two"])
            self.assertFalse(bot.client.is_closed())  # re-opened after the disconnect

            await bot.shutdown()
            self.assertTrue(bot._runner.done())
            self.assertTrue(bot.client.is_closed())
        self.assertNotIn("token-one", self._logged())

    async def test_full_mode_connects_with_both_intents(self):
        settings.discord_bot_token = "token-one"
        with in_mode("full"):
            bot.launch()
            await self._settle()
            self.assertEqual(self.starts, [("token-one", True, True)])
            await bot.shutdown()

    async def test_movienight_connects_without_the_members_intent_if_refused(self):
        settings.discord_bot_token = "token-one"
        self.fail_with = [discord.PrivilegedIntentsRequired(None)]
        with in_mode("movienight"):
            bot.launch()
            await self._settle()
            self.assertEqual(self.starts, [("token-one", True, False), ("token-one", False, False)])
            self.assertIn("Server Members Intent is off", self._logged())
            await bot.shutdown()

    async def test_full_mode_still_requires_both_intents(self):
        settings.discord_bot_token = "token-one"
        self.fail_with = [discord.PrivilegedIntentsRequired(None)]
        with in_mode("full"):
            await bot.start()
        self.assertEqual(len(self.starts), 1)
        self.assertIn("enable BOTH", self._logged())

    async def test_a_rejected_token_is_reported_not_retried(self):
        settings.discord_bot_token = "token-one"
        self.fail_with = [discord.LoginFailure("Improper token has been passed.")]
        await bot.start()
        self.assertEqual(len(self.starts), 1)
        self.assertIn("Discord rejected the bot token", self._logged())

    async def test_unreachable_discord_is_retried(self):
        settings.discord_bot_token = "token-one"
        self.fail_with = [OSError("Temporary failure in name resolution")]
        bot.launch()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(self.starts) == 2:
                break
        self.assertEqual(len(self.starts), 2)
        self.assertIn("trying again", self._logged())
        await bot.shutdown()


class BotReconnectTests(_Isolated, unittest.IsolatedAsyncioTestCase):
    """One layer further down: discord.py's real login (it builds the HTTP
    session) and close. Only its HTTP request function and the gateway are
    faked. Guards the re-open after a disconnect, which a plain
    close()/start() on discord.py 2.4 doesn't survive."""

    def tearDown(self):
        bot.intents.message_content = bot.intents.members = True
        super().tearDown()

    async def _until(self, condition):
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("timed out")

    async def test_restart_logs_in_again_with_a_fresh_session(self):
        client = bot.client
        sessions, connected = [], []

        async def fake_request(route, **kwargs):
            session = client.http._HTTPClient__session
            sessions.append(session)
            assert not session.closed, "login got a closed HTTP session"
            return {"id": "123456789012345678", "username": "MovieBot", "discriminator": "0", "avatar": None, "bot": True}

        async def fake_application_info():
            return types.SimpleNamespace(id=123456789012345678, flags=None, interactions_endpoint_url=None)

        async def fake_connect(*, reconnect=True):
            connected.append((client.http.token, client.loop is asyncio.get_running_loop()))
            await asyncio.Event().wait()

        log = mock.AsyncMock()
        with mock.patch.object(client.http, "request", fake_request), mock.patch.object(client, "application_info", fake_application_info),                 mock.patch.object(client, "connect", fake_connect), mock.patch.object(bot, "log", log),                 mock.patch.object(bot, "_runner", None), in_mode("movienight"):
            settings.discord_bot_token = "token-one"
            bot.launch()
            await self._until(lambda: len(connected) == 1)

            settings.discord_bot_token = "token-two"
            await bot.restart()
            await self._until(lambda: len(connected) == 2)
            self.assertEqual(connected, [("token-one", True), ("token-two", True)])
            self.assertTrue(sessions[0].closed)  # the old connection was shut down cleanly...
            self.assertFalse(sessions[-1].closed)  # ...and the new login has a working one
            self.assertFalse(client.is_closed())
            self.assertEqual([type(v).__name__ for v in client.persistent_views], ["CommanderPanelView"])

            await bot.shutdown()
            self.assertTrue(client.is_closed())
            self.assertTrue(sessions[-1].closed)
        self.assertEqual([c.args[0] for c in log.await_args_list if "couldn't connect" in c.args[0]], [])


# ---------------------------------------------------------------------------
# pairing with the player (pair.json, editions)
# ---------------------------------------------------------------------------


class PairingFileTests(_Api, unittest.IsolatedAsyncioTestCase):
    FOUND = {"mode": "movienight", "name": "Den TV", "ready": True, "edition": "guest"}

    def setUp(self):
        super().setUp()
        self.pair_file = os.path.join(self.tmp, "pairing", "pair.json")
        settings.player_pair_file = self.pair_file
        settings.neko_mpv_shim_url = settings.player_key = settings.player_mode = ""
        settings.player_pair_source = settings.player_pair_file_url = ""
        self._patches = [
            mock.patch.object(mpv_control, "test_connection", mock.AsyncMock(return_value=dict(self.FOUND))),
            mock.patch.object(mpv_control, "refresh_links", mock.AsyncMock()),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()

    def _write(self, url="http://192.168.1.50:8090", key="mnp_key_one"):
        os.makedirs(os.path.dirname(self.pair_file), exist_ok=True)
        with open(self.pair_file, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "url": url, "public_url": None, "api_key": key, "pair_code": "MNP1-x"}, fh)

    def _pairing(self):
        return settings.neko_mpv_shim_url, settings.player_key, settings.player_mode

    def test_paired_automatically_from_the_pairing_file(self):
        self._write()
        with in_mode("movienight"):
            r = self.client.get("/api/setup/player", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        info = r.json()
        self.assertEqual((info["paired"], info["auto"], info["pairing_file_found"]), (True, True, True))
        self.assertEqual((info["name"], info["ready"], info["edition"], info["url"]), ("Den TV", True, "guest", "http://192.168.1.50:8090"))
        self.assertNotIn("mnp_key_one", r.text)  # the key stays on the server
        self.assertEqual(self._pairing(), ("http://192.168.1.50:8090", "mnp_key_one", "movienight"))
        mpv_control.test_connection.assert_awaited_once_with("http://192.168.1.50:8090", "mnp_key_one")

    def test_not_paired_without_a_file(self):
        with in_mode("movienight"):
            info = self.client.get("/api/setup/player", headers=ADMIN).json()
        self.assertEqual((info["paired"], info["auto"], info["pairing_file_found"]), (False, False, False))
        mpv_control.test_connection.assert_not_called()
        self.assertEqual(self.client.get("/api/setup/player").status_code, 401)

    def test_a_pair_code_typed_by_hand_is_not_auto(self):
        self._write()
        code = "MNP1-" + base64.urlsafe_b64encode(json.dumps({"v": 1, "u": "http://10.0.0.9:8090", "p": None, "k": "mnp_typed"}).encode()).decode().rstrip("=")
        with in_mode("movienight"), mock.patch.object(bot, "request_panel_refresh") as panel:
            r = self.client.post("/api/setup/pair-player", json={"url": code}, headers=ADMIN)
            self.assertEqual((r.status_code, r.json()["edition"]), (200, "guest"))
            panel.assert_called_once_with()  # the DJ control panel shows up without a restart
            self.assertNotIn("mnp_typed", r.text)
            info = self.client.get("/api/setup/player", headers=ADMIN).json()
        self.assertEqual((info["paired"], info["auto"], info["url"]), (True, False, "http://10.0.0.9:8090"))
        self.assertEqual(self._pairing(), ("http://10.0.0.9:8090", "mnp_typed", "movienight"))  # the file didn't override it

    def test_start_order_does_not_matter(self):
        self.assertFalse(connections_store.auto_pair_from_file())  # Servarr is up first: no file yet
        self.assertEqual(self._pairing(), ("", "", ""))
        self._write()
        self.assertTrue(connections_store.auto_pair_from_file())
        self.assertEqual(self._pairing(), ("http://192.168.1.50:8090", "mnp_key_one", "movienight"))
        self.assertFalse(connections_store.auto_pair_from_file())  # nothing changed since

    async def test_the_watch_loop_picks_up_a_file_written_later(self):
        with mock.patch.object(mpv_control, "_PAIRING_WAIT_SECONDS", 0.01), mock.patch.object(mpv_control, "_PAIRING_CHECK_SECONDS", 0.01), \
                mock.patch("app.logger.log", mock.AsyncMock()):
            task = asyncio.create_task(mpv_control.pairing_watch_loop())
            try:
                await asyncio.sleep(0.05)
                self.assertEqual(self._pairing(), ("", "", ""))
                self._write()
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if settings.neko_mpv_shim_url:
                        break
                self.assertEqual(self._pairing(), ("http://192.168.1.50:8090", "mnp_key_one", "movienight"))
                self._write(url="http://movienight:8090", key="mnp_key_two")  # and keeps following the file
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if settings.player_key == "mnp_key_two":
                        break
                self.assertEqual(self._pairing(), ("http://movienight:8090", "mnp_key_two", "movienight"))
            finally:
                task.cancel()

    def test_a_pairing_that_came_from_the_file_follows_the_file(self):
        self._write()
        self.assertTrue(connections_store.auto_pair_from_file())
        self.assertEqual(settings.player_pair_source, "file")
        self._write(url="http://movienight:8090")  # player 1.1 bundles: the address changes, same key
        self.assertTrue(connections_store.auto_pair_from_file())
        self.assertEqual(self._pairing(), ("http://movienight:8090", "mnp_key_one", "movienight"))
        self._write(url="http://movienight:8090", key="mnp_rotated")  # and a rotated key
        self.assertTrue(connections_store.auto_pair_from_file())
        self.assertEqual(settings.player_key, "mnp_rotated")
        with open(connections_store._overrides_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual((saved["neko_mpv_shim_url"], saved["player_pair_source"]), ("http://movienight:8090", "file"))

    def test_a_pairing_from_before_1_2_with_the_files_key_counts_as_the_files(self):
        settings.neko_mpv_shim_url, settings.player_key, settings.player_mode = "http://192.168.1.50:8090", "mnp_key_one", "movienight"
        self._write(url="http://movienight:8090")
        self.assertTrue(connections_store.auto_pair_from_file())
        self.assertEqual(self._pairing(), ("http://movienight:8090", "mnp_key_one", "movienight"))
        self.assertEqual(settings.player_pair_source, "file")

    def test_same_address_from_before_1_2_is_remembered_as_the_files(self):
        settings.neko_mpv_shim_url, settings.player_key, settings.player_mode = "http://192.168.1.50:8090", "mnp_key_one", "movienight"
        self._write()
        self.assertFalse(connections_store.auto_pair_from_file())  # nothing to change...
        self.assertEqual(settings.player_pair_source, "file")  # ...but now it follows the file
        self._write(url="http://movienight:8090", key="mnp_rotated")  # even if both change at once
        self.assertTrue(connections_store.auto_pair_from_file())
        self.assertEqual(self._pairing(), ("http://movienight:8090", "mnp_rotated", "movienight"))

    def test_a_pairing_typed_by_hand_wins(self):
        settings.neko_mpv_shim_url, settings.player_key, settings.player_mode = "http://10.0.0.9:8090", "mnp_key_one", "movienight"
        connections_store.mark_paired_by_hand()
        self._write(url="http://movienight:8090")  # same key, other address: still left alone
        self.assertFalse(connections_store.auto_pair_from_file())
        self.assertEqual(self._pairing(), ("http://10.0.0.9:8090", "mnp_key_one", "movienight"))
        self._write(url="http://10.0.0.9:8090", key="mnp_rotated")  # same player: only the new key is taken
        self.assertTrue(connections_store.auto_pair_from_file())
        self.assertEqual(self._pairing(), ("http://10.0.0.9:8090", "mnp_rotated", "movienight"))
        self.assertEqual(settings.player_pair_source, "manual")

    def test_changing_the_address_by_hand_makes_it_manual(self):
        self._write()
        self.assertTrue(connections_store.auto_pair_from_file())
        connections_store.save_overrides({"neko_mpv_shim_url": "http://10.0.0.9:8090"})
        self.assertEqual(settings.player_pair_source, "manual")
        self.assertFalse(connections_store.auto_pair_from_file())
        self.assertEqual(settings.neko_mpv_shim_url, "http://10.0.0.9:8090")
        self.assertNotIn("player_pair_source", connections_store.current_display())  # internal, not an API field
        with in_mode("full"):
            r = self.client.post("/api/connections", json={"player_pair_source": "file"}, headers=ADMIN)
        self.assertEqual(r.status_code, 400)


class PlayerFieldsTests(_Isolated, unittest.IsolatedAsyncioTestCase):
    """The optional fields player 1.1 added, and players that don't have them."""

    def _client(self, pair):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "player": "movienight-player", "name": "Den TV", "ready": True, **pair})

        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        return mock.patch.object(mpv_control.httpx, "AsyncClient", factory)

    async def test_edition_from_pair(self):
        for pair, expected in (({"edition": "guest"}, "guest"), ({"edition": "owner"}, "owner"), ({"edition": None}, None), ({}, None), ({"edition": "banana"}, None)):
            with self._client(pair):
                found = await mpv_control.test_connection("http://player.example:8090", "k")
            self.assertEqual((found["mode"], found["edition"]), ("movienight", expected), pair)

    async def test_buffering_counts_as_playing(self):
        settings.neko_mpv_shim_url, settings.player_key, settings.player_mode = "http://player.example:8090", "k", "movienight"
        with mock.patch.object(mpv_control, "_mn", mock.AsyncMock(return_value=(200, {"ok": True, "state": "buffering", "position_ms": 5000, "duration_ms": 9000}))):
            self.assertEqual((await mpv_control.get_status())["state"], "playing")


# ---------------------------------------------------------------------------
# "more viewers than the host's connection can carry"
# ---------------------------------------------------------------------------

WARNING = "⚠️ More viewers (12) than the host's connection can comfortably carry (about 8). Video may stutter."
OVER = {"ok": True, "state": "playing", "viewers": 12, "viewer_capacity": 8, "over_capacity": True}


class CapacityWarningTests(_Isolated, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        settings.neko_mpv_shim_url, settings.player_key, settings.player_mode = "http://player.example:8090", "k", "movienight"
        movie_night._live.clear()

    def tearDown(self):
        movie_night._live.clear()
        super().tearDown()

    def test_text_when_over_capacity(self):
        self.assertEqual(mpv_control.capacity_warning(OVER), WARNING)

    def test_absent_when_fine_or_not_reported(self):
        older_player = {"ok": True, "state": "playing", "position_ms": 1000, "duration_ms": 7200000, "now_playing": {"title": "Heat"}}
        for status in (older_player, {}, None, "playing", {**OVER, "over_capacity": False}, {**OVER, "over_capacity": None},
                       {**OVER, "over_capacity": "true"}, {"viewers": 99, "viewer_capacity": 1}, {"viewers": None, "viewer_capacity": None, "over_capacity": False}):
            self.assertEqual(mpv_control.capacity_warning(status), "", status)

    def test_still_friendly_when_the_numbers_are_missing(self):
        self.assertEqual(
            mpv_control.capacity_warning({"over_capacity": True}),
            "⚠️ More viewers than the host's connection can comfortably carry. Video may stutter.",
        )
        self.assertEqual(
            mpv_control.capacity_warning({"over_capacity": True, "viewers": 5, "viewer_capacity": None}),
            "⚠️ More viewers (5) than the host's connection can comfortably carry. Video may stutter.",
        )
        self.assertIn("(about 3)", mpv_control.capacity_warning({"over_capacity": True, "viewers": True, "viewer_capacity": 3}))

    async def _play(self, status):
        channel = _text_channel("movie-night")
        movie = radarr.LibraryMovie(id=5, title="Heat", year=1995, poster_url=None, rating_key="5")
        with mock.patch.object(movie_night, "start_in_neko", mock.AsyncMock(return_value=True)), \
                mock.patch.object(movie_night, "_post_admin_now_playing", mock.AsyncMock()), \
                mock.patch.object(mpv_control, "player_status", mock.AsyncMock(return_value=status)), \
                mock.patch.object(mpv_control, "viewer_link", return_value="https://watch.example.com/?pwd=v"):
            self.assertTrue(await movie_night.play_now(channel, movie))
        return channel.send.await_args.kwargs["embed"].description, channel.send.return_value

    async def test_now_playing_message_carries_the_warning(self):
        description, _ = await self._play(OVER)
        self.assertTrue(description.startswith("**Heat** (1995)"))
        self.assertTrue(description.endswith("\n" + WARNING))

    async def test_now_playing_message_without_the_fields_is_unchanged(self):
        for status in ({"ok": True, "state": "playing"}, {**OVER, "over_capacity": False}, None):
            description, _ = await self._play(status)
            self.assertEqual(description, "**Heat** (1995)\nIt's already loading up, click below to watch together!")
            self.assertNotIn("⚠️", description)

    async def test_the_message_is_updated_while_the_movie_plays(self):
        _, message = await self._play({"ok": True, "state": "playing", "viewers": 2, "viewer_capacity": 8, "over_capacity": False})
        status = mock.AsyncMock(return_value=OVER)
        with mock.patch.object(mpv_control, "player_status", status):
            await movie_night.refresh_live_messages()
            self.assertTrue(message.edit.await_args.kwargs["embed"].description.endswith(WARNING))
            await movie_night.refresh_live_messages()
            self.assertEqual(message.edit.await_count, 1)  # nothing changed: no second edit

            status.return_value = {**OVER, "viewers": 6, "over_capacity": False}
            await movie_night.refresh_live_messages()
            self.assertEqual(message.edit.await_count, 2)
            self.assertNotIn("⚠️", message.edit.await_args.kwargs["embed"].description)

            status.return_value = None  # the player didn't answer: keep watching
            await movie_night.refresh_live_messages()
            self.assertIn("public", movie_night._live)

            status.return_value = {"ok": True, "state": "idle"}  # movie over: stop
            await movie_night.refresh_live_messages()
            self.assertEqual(movie_night._live, {})
        self.assertEqual(message.edit.await_count, 2)


# ---------------------------------------------------------------------------
# the library comes from the player in Movie Night only mode
# ---------------------------------------------------------------------------

_LIBRARY = [
    {"rating_key": str(100 + i), "type": "movie", "title": f"Movie {i}", "year": 2000 + i, "playable": True, "has_poster": True}
    for i in range(7)
]


def _fake_player(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("Authorization") == "Bearer k"
    path = request.url.path.removeprefix("/movienight/api/v1")
    params = request.url.params
    if path == "/sections":
        return httpx.Response(200, json={"ok": True, "sections": [
            {"key": "1", "title": "Movies", "type": "movie"}, {"key": "2", "title": "TV", "type": "show"}, {"key": "3", "title": "Kids", "type": "movie"},
        ]})
    if path in ("/browse/1", "/browse/3"):
        items = _LIBRARY[:5] if path.endswith("1") else _LIBRARY[5:]
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 50))
        return httpx.Response(200, json={"ok": True, "items": items[offset:offset + limit], "total": len(items), "offset": offset})
    if path == "/search":
        return httpx.Response(200, json={"ok": True, "items": [
            _LIBRARY[0],
            {"rating_key": "900", "type": "show", "title": "Movie Club", "year": 2010, "playable": False, "has_poster": False},
            {"rating_key": "901", "type": "episode", "title": "Pilot", "show_title": "Movie Club", "season": 1, "episode": 1, "playable": True, "has_poster": True},
        ]})
    return httpx.Response(404, json={"ok": False, "error": "not found"})


class PlayerLibraryTests(_Isolated, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        settings.neko_mpv_shim_url, settings.player_key, settings.player_mode = "http://player.example:8090", "k", "movienight"
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_fake_player)
            return real(*args, **kwargs)

        self._patch = mock.patch.object(mpv_control.httpx, "AsyncClient", factory)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        super().tearDown()

    async def test_random_movies_for_the_vote(self):
        picks = await player_library.random_movies(4)
        self.assertEqual(len(picks), 4)
        self.assertEqual(len({m.rating_key for m in picks}), 4)
        for movie in picks:
            self.assertIn(movie.rating_key, {item["rating_key"] for item in _LIBRARY})
            self.assertEqual(movie.id, int(movie.rating_key))
            self.assertIsNone(movie.poster_url)
        self.assertEqual(len(await player_library.random_movies(50)), 7)  # never more than the library has
        self.assertEqual(await player_library.random_movies(0), [])

    async def test_search_shapes(self):
        movies = await player_library.find_movies("movie")
        self.assertEqual([(m.title, m.year, m.rating_key) for m in movies], [("Movie 0", 2000, "100")])
        rows = await player_library.search("movie")
        self.assertEqual([r["type"] for r in rows], ["movie", "show", "episode"])
        self.assertEqual(rows[0], {"rating_key": "100", "title": "Movie 0", "year": 2000, "type": "movie", "index": None, "poster_url": "/api/mpv/poster/100"})
        self.assertEqual((rows[1]["poster_url"], rows[2]["index"]), (None, 1))
        self.assertEqual([s["title"] for s in await player_library.sections()], ["Movies", "TV", "Kids"])

    async def test_needs_a_paired_movienight_player(self):
        settings.neko_mpv_shim_url = ""
        with self.assertRaises(mpv_control.PlayerError):
            await player_library.random_movies(3)


class MovieNightFlowTests(_Isolated, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        settings.neko_mpv_shim_url, settings.player_key, settings.player_mode = "http://player.example:8090", "k", "movienight"
        self.picks = [radarr.LibraryMovie(id=100 + i, title=f"Movie {i}", year=2000 + i, poster_url=None, rating_key=str(100 + i)) for i in range(3)]
        self.movie_night_channel = _text_channel("movie-night", 555)
        self.general = _text_channel("general", 1)
        self.guild = mock.Mock()
        self.guild.text_channels = [self.general, self.movie_night_channel]
        self.guild.get_channel = lambda cid: {555: self.movie_night_channel, 1: self.general}.get(cid)
        self._patches = [
            mock.patch.object(movie_night, "_save_vote_state"),
            mock.patch.object(movie_night, "_clear_vote_state"),
            mock.patch.object(movie_night, "log", mock.AsyncMock()),
            mock.patch.dict(movie_night._current_vote, {}, clear=True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()

    async def test_vote_uses_the_players_library_and_the_picked_channel(self):
        settings.movie_night_channel_id = "555"
        with in_mode("movienight"), mock.patch.object(player_library, "random_movies", mock.AsyncMock(return_value=self.picks)) as library, \
                mock.patch.object(radarr, "list_downloaded_movies", mock.AsyncMock(side_effect=AssertionError("Radarr is off"))):
            self.assertTrue(await movie_night.post_vote(self.guild))
        library.assert_awaited_once_with(settings.movie_night_candidate_count)
        self.general.send.assert_not_called()
        sent = self.movie_night_channel.send.await_args.kwargs
        self.assertIsNone(sent["content"])  # no @everyone ping in this mode
        self.assertEqual(sorted(f.name.split(" (")[0] for f in sent["embed"].fields), ["Movie 0", "Movie 1", "Movie 2"])

    async def test_vote_in_full_mode_is_unchanged(self):
        with in_mode("full"), mock.patch.object(radarr, "list_downloaded_movies", mock.AsyncMock(return_value=self.picks)), \
                mock.patch.object(player_library, "random_movies", mock.AsyncMock(side_effect=AssertionError("not in full mode"))):
            self.assertTrue(await movie_night.post_vote(self.guild))
        self.assertEqual(self.general.send.await_args.kwargs["content"], "@everyone")
        self.movie_night_channel.send.assert_not_called()

    async def test_other_servers_are_left_alone(self):
        settings.movie_night_channel_id = "999"  # a channel in some other server
        with in_mode("movienight"), mock.patch.object(player_library, "random_movies", mock.AsyncMock(return_value=self.picks)) as library:
            self.assertFalse(await movie_night.post_vote(self.guild))
        library.assert_not_called()
        self.general.send.assert_not_called()

    async def test_vote_is_skipped_when_the_player_cannot_be_read(self):
        with in_mode("movienight"), mock.patch.object(player_library, "random_movies", mock.AsyncMock(side_effect=mpv_control.PlayerError("down"))):
            self.assertFalse(await movie_night.post_vote(self.guild))
        with in_mode("movienight"), mock.patch.object(player_library, "random_movies", mock.AsyncMock(return_value=self.picks[:1])):
            self.assertFalse(await movie_night.post_vote(self.guild))
        self.general.send.assert_not_called()
        with in_mode("movienight"), mock.patch.object(player_library, "random_movies", mock.AsyncMock(side_effect=mpv_control.PlayerError("down"))):
            self.assertFalse(await movie_night.force_winner_now(self.guild))  # /movie-night-play answers instead of erroring

    def test_movie_night_channel(self):
        self.assertIs(movie_night._vote_channel(self.guild), self.general)  # nothing picked: #general, as before
        settings.movie_night_channel_id = "555"
        self.assertIs(movie_night._vote_channel(self.guild), self.movie_night_channel)
        self.assertEqual(movie_night.vote_channel_label(self.guild), "#movie-night")
        settings.movie_night_channel_id = "999"  # picked, but in another server: not #general
        self.assertIsNone(movie_night._vote_channel(self.guild))

    async def test_player_library_movies_play_by_rating_key(self):
        with mock.patch.object(mpv_control, "play_by_rating_key", mock.AsyncMock(return_value=True)) as by_key, \
                mock.patch.object(mpv_control, "play", mock.AsyncMock(return_value=True)) as by_title:
            self.assertTrue(await movie_night.start_in_neko(self.picks[0]))
            by_key.assert_awaited_once_with("100")
            by_title.assert_not_called()
            self.assertTrue(await movie_night.start_in_neko(radarr.LibraryMovie(id=1, title="Heat", year=1995, poster_url=None)))
            by_title.assert_awaited_once_with("Heat", 1995)

    async def test_play_movie_searches_the_players_library(self):
        interaction = mock.Mock()
        interaction.followup.send = mock.AsyncMock()
        interaction.channel = self.movie_night_channel
        with in_mode("movienight"), mock.patch.object(player_library, "find_movies", mock.AsyncMock(return_value=self.picks[:1])) as find, \
                mock.patch.object(movie_night, "play_now", mock.AsyncMock(return_value=False)) as play_now, \
                mock.patch.object(radarr, "list_downloaded_movies", mock.AsyncMock(side_effect=AssertionError("Radarr is off"))):
            await movie_night.play_specific(interaction, "movie 0")
            find.assert_awaited_once_with("movie 0")
            play_now.assert_awaited_once_with(self.movie_night_channel, self.picks[0])
            find.return_value = []
            await movie_night.play_specific(interaction, "nothing")
            self.assertIn("in the Plex library", interaction.followup.send.await_args.args[0])


    async def _run_once(self, loop_fn):
        """Runs one round of a daily loop (its sleep is skipped), then stops it."""
        with mock.patch.object(movie_night, "_sleep_until_hour_utc", mock.AsyncMock(side_effect=[None, asyncio.CancelledError()])),                 mock.patch.object(movie_night, "_client", mock.Mock(guilds=[self.guild])),                 mock.patch.object(movie_night, "post_vote", mock.AsyncMock(return_value=True)) as post_vote,                 mock.patch.object(movie_night, "announce_winner", mock.AsyncMock(return_value=True)) as announce:
            with self.assertRaises(asyncio.CancelledError):
                await loop_fn()
        return post_vote, announce

    async def test_daily_vote_runs_by_default(self):
        self.assertTrue(Settings().movie_night_daily_vote)
        settings.movie_night_daily_vote = True
        post_vote, _ = await self._run_once(movie_night.vote_loop)
        post_vote.assert_awaited_once_with(self.guild)
        _, announce = await self._run_once(movie_night.announce_loop)
        announce.assert_awaited_once_with(self.guild)

    async def test_daily_vote_can_be_switched_off(self):
        settings.movie_night_daily_vote = False
        post_vote, _ = await self._run_once(movie_night.vote_loop)
        post_vote.assert_not_called()
        _, announce = await self._run_once(movie_night.announce_loop)
        announce.assert_not_called()
        movie_night._current_vote["candidates"] = self.picks  # a vote a mod started with /movie-night is still tallied
        _, announce = await self._run_once(movie_night.announce_loop)
        announce.assert_awaited_once_with(self.guild)

    def test_daily_vote_setting(self):
        settings_store.validate({"movie_night_daily_vote": False})
        for bad in ("no", 0, None):
            with self.assertRaises(ValueError):
                settings_store.validate({"movie_night_daily_vote": bad})
        self.assertIn("movie_night_daily_vote", features.MOVIE_NIGHT_SETTINGS)
        for value, expected in (("false", False), ("0", False), ("off", False), ("true", True), ("", True)):
            with mock.patch.dict(os.environ, {"MOVIE_NIGHT_DAILY_VOTE": value}):
                self.assertIs(Settings().movie_night_daily_vote, expected, value)

    async def _mpv_play_buttons(self):
        interaction = mock.Mock()
        interaction.response.defer = mock.AsyncMock()
        interaction.followup.send = mock.AsyncMock()
        with mock.patch.object(mpv_control, "play", mock.AsyncMock(return_value=True)),                 mock.patch.object(mpv_control, "admin_link", return_value="https://watch.example.com/?pwd=a"):
            await bot.mpv_play_command.callback(interaction, "Heat")
        sent = interaction.followup.send.await_args.kwargs
        self.assertTrue(sent["ephemeral"])  # the admin link only ever goes to the DJ
        return [item.label for item in sent["view"].children]

    async def test_mpv_play_gives_the_dj_the_players_link(self):
        settings.mpv_remote_key, settings.neko_mpv_public_url = "remotekey123", ""
        settings.servarr_public_url = "http://localhost:8888"
        with in_mode("movienight"):
            self.assertEqual(await self._mpv_play_buttons(), ["▶️  WATCH LIVE"])  # no dead localhost remote link
            settings.servarr_public_url = "http://192.168.1.50:8888"
            self.assertEqual(await self._mpv_play_buttons(), ["▶️  WATCH LIVE", "🎮  REMOTE CONTROLS"])
        settings.servarr_public_url = "http://localhost:8888"
        with in_mode("full"):
            self.assertEqual(await self._mpv_play_buttons(), ["▶️  WATCH LIVE", "🎮  REMOTE CONTROLS"])


# ---------------------------------------------------------------------------
# where the web server listens
# ---------------------------------------------------------------------------


class BindAddressTests(unittest.TestCase):
    def test_default_is_every_interface_port_8888(self):
        self.assertEqual(serve.bind({}), ("0.0.0.0", 8888))
        self.assertEqual(serve.bind({"SERVARR_HOST": " ", "SERVARR_PORT": ""}), ("0.0.0.0", 8888))
        self.assertEqual(serve.health_url({}), "http://127.0.0.1:8888/health")

    def test_host_and_port_from_the_environment(self):
        self.assertEqual(serve.bind({"SERVARR_HOST": "127.0.0.1", "SERVARR_PORT": "9000"}), ("127.0.0.1", 9000))
        self.assertEqual(serve.health_url({"SERVARR_HOST": "192.168.1.50", "SERVARR_PORT": "9000"}), "http://192.168.1.50:9000/health")
        self.assertEqual(serve.health_url({"SERVARR_HOST": "::1"}), "http://[::1]:8888/health")

    def test_bad_port_is_a_plain_error(self):
        for bad in ("abc", "0", "70000", "-1", "88.8"):
            with self.assertRaises(ValueError):
                serve.bind({"SERVARR_PORT": bad})
        with mock.patch.dict(os.environ, {"SERVARR_PORT": "abc"}), mock.patch("sys.stderr"):
            self.assertEqual(serve.main([]), 1)

    def test_launcher_passes_them_to_uvicorn(self):
        import uvicorn

        with mock.patch.dict(os.environ, {"SERVARR_HOST": "127.0.0.1", "SERVARR_PORT": "9000"}), mock.patch.object(uvicorn, "run") as run:
            self.assertEqual(serve.main([]), 0)
        run.assert_called_once_with("app.main:app", host="127.0.0.1", port=9000)
        env = {k: v for k, v in os.environ.items() if k not in ("SERVARR_HOST", "SERVARR_PORT")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(uvicorn, "run") as run:
            serve.main([])
        run.assert_called_once_with("app.main:app", host="0.0.0.0", port=8888)


if __name__ == "__main__":
    unittest.main()
