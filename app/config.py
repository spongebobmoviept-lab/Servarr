import os


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


class Settings:
    def __init__(self) -> None:
        self.sonarr_url = os.environ.get("SONARR_URL", "")
        self.sonarr_api_key = os.environ.get("SONARR_API_KEY", "")

        self.radarr_url = os.environ.get("RADARR_URL", "")
        self.radarr_api_key = os.environ.get("RADARR_API_KEY", "")

        # Live "now playing" monitor — reuses Tautulli, which already tracks
        # every Plex session, instead of talking to Plex directly.
        self.tautulli_url = os.environ.get("TAUTULLI_URL", "")
        self.tautulli_api_key = os.environ.get("TAUTULLI_API_KEY", "")
        self.plex_monitor_interval_seconds = _env_int("PLEX_MONITOR_INTERVAL_SECONDS", 20)

        # Movie Night — a daily vote among already-downloaded movies, winner
        # announced at movie_night_hour_utc. Reuses Reclaimarr's own admin
        # login to call its pause-upgrade endpoint (see reclaimarr/app/main.py)
        # so nobody's shared viewing gets interrupted by a 4K swap mid-movie.
        self.reclaimarr_url = os.environ.get("RECLAIMARR_URL", "")
        self.reclaimarr_auth_user = os.environ.get("RECLAIMARR_AUTH_USER", "")
        self.reclaimarr_auth_pass = os.environ.get("RECLAIMARR_AUTH_PASS", "")
        self.movie_night_vote_hour_utc = _env_int("MOVIE_NIGHT_VOTE_HOUR_UTC", 22)  # ~6pm US Eastern
        self.movie_night_hour_utc = _env_int("MOVIE_NIGHT_HOUR_UTC", 1)  # ~9pm US Eastern, next UTC day
        self.movie_night_candidate_count = _env_int("MOVIE_NIGHT_CANDIDATE_COUNT", 5)
        self.movie_night_pause_upgrade_minutes = _env_int("MOVIE_NIGHT_PAUSE_UPGRADE_MINUTES", 240)

        # Movie Night playback automation — Plex's own remote-control API
        # (/player/playback/*) doesn't work against a Plex Web browser
        # session (confirmed live: 404s even though the session shows up
        # fine in /status/sessions — only native apps support it), so
        # instead Neko's own control API drives the shared browser directly.
        # See app/neko_control.py.
        self.neko_url = os.environ.get("NEKO_URL", "").rstrip("/")
        self.neko_admin_password = os.environ.get("NEKO_ADMIN_PASSWORD", "")
        # The link posted in Discord — separate from neko_url because that
        # one's the LAN address Servarr itself uses to drive the browser
        # (fast, doesn't depend on the router's NAT loopback working),
        # while friends outside the house need the public address instead.
        # Falls back to neko_url if unset (same-network-only setups).
        self.neko_public_url = os.environ.get("NEKO_PUBLIC_URL", "").rstrip("/") or self.neko_url
        # Read-only bind mount of neko/.env (see docker-compose.yml) — lets
        # movie_night read whatever the daily rotation script last set
        # NEKO_PASSWORD to, without Servarr needing any way to change it itself.
        self.neko_shared_env_file = os.environ.get("NEKO_SHARED_ENV_FILE", "/neko-shared/neko.env")

        self.plex_url = os.environ.get("PLEX_URL", "").rstrip("/")
        self.plex_token = os.environ.get("PLEX_TOKEN", "")
        # The Plex Home profile Neko logs in as has its own PIN lock (kept
        # intentionally, rather than removed, to not weaken the account) —
        # the picker screen resets after any Neko/Chromium restart, so
        # neko_control always types this defensively before navigating.
        self.plex_home_pin = os.environ.get("PLEX_HOME_PIN", "")

        self.discord_bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        # Same reasoning as Requestarr — instant per-guild command sync
        # instead of waiting up to an hour for a global sync to propagate.
        self.discord_guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()

        # Leveling (Phase 2)
        self.xp_min_per_message = _env_int("XP_MIN_PER_MESSAGE", 15)
        self.xp_max_per_message = _env_int("XP_MAX_PER_MESSAGE", 25)
        self.xp_cooldown_seconds = _env_int("XP_COOLDOWN_SECONDS", 60)

        # Moderation (Phase 4) — where kick/ban/timeout/warn actions get
        # logged. Blank means mod actions still work, just aren't logged
        # anywhere visible — set this before relying on it for accountability.
        self.mod_log_channel_id = os.environ.get("MOD_LOG_CHANNEL_ID", "").strip()

        self.app_port = _env_int("APP_PORT", 8888)

        self.data_dir = os.environ.get("DATA_DIR", "/data")
        self.xp_file = os.path.join(self.data_dir, "xp.json")
        self.log_file = os.path.join(self.data_dir, "log.txt")


settings = Settings()
