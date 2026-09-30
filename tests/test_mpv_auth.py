import base64
import unittest
from unittest import mock

from tests import _env  # noqa: F401  (must come first)
from fastapi.testclient import TestClient

from app import auth, auth_store, mpv_control
from app.config import settings

app = _env.load_main().app

KEY = settings.mpv_remote_key
ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:correct-horse-battery").decode()}


class MpvAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not auth_store.is_admin_configured():
            auth_store.set_admin("admin", "correct-horse-battery")
        cls.client = TestClient(app)

    def setUp(self):
        auth._failed_attempts.clear()

    def _status(self, **kw):
        with mock.patch.object(mpv_control, "get_status", mock.AsyncMock(return_value={"state": None, "time_ms": None, "duration_ms": None})):
            return self.client.get("/api/mpv/status", **kw)

    def test_no_credentials_is_401(self):
        self.assertEqual(self._status().status_code, 401)

    def test_header_key_works(self):
        self.assertEqual(self._status(headers={"X-Remote-Key": KEY}).status_code, 200)

    def test_wrong_header_key_is_401(self):
        self.assertEqual(self._status(headers={"X-Remote-Key": "nope"}).status_code, 401)

    def test_query_key_is_not_accepted(self):
        self.assertEqual(self._status(params={"key": KEY}).status_code, 401)

    def test_admin_login_still_works(self):
        self.assertEqual(self._status(headers=ADMIN).status_code, 200)

    def test_login_sets_httponly_cookie(self):
        client = TestClient(app)
        r = client.post("/api/mpv/login", json={"key": KEY})
        self.assertEqual(r.status_code, 200)
        self.assertIn("httponly", r.headers["set-cookie"].lower())
        with mock.patch.object(mpv_control, "get_status", mock.AsyncMock(return_value={"state": None})):
            self.assertEqual(client.get("/api/mpv/status").status_code, 200)

    def test_login_rejects_bad_key(self):
        self.assertEqual(self.client.post("/api/mpv/login", json={"key": "wrong"}).status_code, 401)

    def test_bad_keys_are_throttled(self):
        for _ in range(10):
            self._status(headers={"X-Remote-Key": "wrong"})
        self.assertEqual(self._status(headers={"X-Remote-Key": KEY}).status_code, 429)

    def test_empty_configured_key_never_matches(self):
        with mock.patch.object(settings, "mpv_remote_key", ""):
            self.assertFalse(auth.remote_key_matches(""))
            self.assertEqual(self._status(headers={"X-Remote-Key": ""}).status_code, 401)

    def test_non_numeric_rating_key_rejected(self):
        r = self.client.get("/api/mpv/children/..%2F..%2Faccounts", headers={"X-Remote-Key": KEY})
        self.assertIn(r.status_code, (400, 404))
        r = self.client.get("/api/mpv/children/abc", headers={"X-Remote-Key": KEY})
        self.assertEqual(r.status_code, 400)

    def test_player_not_configured_is_503(self):
        with mock.patch.object(settings, "neko_mpv_shim_url", ""):
            r = self.client.post("/api/mpv/stop", headers={"X-Remote-Key": KEY})
        self.assertEqual(r.status_code, 503)

    def test_page_served_without_secret_in_url(self):
        r = self.client.get("/mpv-remote")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(KEY, r.text)

    def test_digest_force_requires_login(self):
        self.assertEqual(self.client.post("/api/release-digest/force").status_code, 401)
        self.assertEqual(self.client.post("/api/movie-night/force").status_code, 401)


class CorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_allowed_origin_preflight_gets_pna(self):
        r = self.client.options(
            "/mpv-remote",
            headers={"Origin": "https://watch.example.com", "Access-Control-Request-Private-Network": "true", "Access-Control-Request-Method": "GET"},
        )
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "https://watch.example.com")
        self.assertEqual(r.headers.get("access-control-allow-private-network"), "true")

    def test_unknown_origin_is_not_reflected(self):
        r = self.client.get("/health", headers={"Origin": "https://evil.example"})
        self.assertNotIn("access-control-allow-origin", r.headers)
        r = self.client.options("/api/connections", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_cross_origin_post_blocked(self):
        r = self.client.post("/api/mpv/stop", headers={"Origin": "https://evil.example", "X-Remote-Key": KEY})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
