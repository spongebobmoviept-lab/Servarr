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
        # Who's allowed to hijack the shared Neko browser with /play-movie
        # outside the normal vote — anyone with Manage Server always can;
        # this optionally extends it to a specific role (e.g. "Movie Night
        # DJ") without handing those people full mod permissions. Blank
        # means only Manage Server can use it.
        self.movie_night_dj_role_id = os.environ.get("MOVIE_NIGHT_DJ_ROLE_ID", "").strip()

        # Movie Night playback automation — Plex's own remote-control API
        # (/player/playback/*) doesn't work against a Plex Web browser
        # session (confirmed live: 404s even though the session shows up
        # fine in /status/sessions — only native apps support it), so
        # instead Neko's own control API drives the shared browser directly.
        # See app/neko_control.py.
        self.neko_url = os.environ.get("NEKO_URL", "").rstrip("/")
        self.neko_admin_password = os.environ.get("NEKO_ADMIN_PASSWORD", "")
        # Plex Desktop's own remote-debugging port only works with hardware
        # video overlay disabled (confirmed live — no known flag fixes this,
        # it looks like deliberate behavior in Plex's closed-source client),
        # so neko_control toggles it on only for the few seconds it takes to
        # resolve+launch a movie, then off again before anyone actually
        # watches. plex.ini lives on a volume shared read-write from the
        # neko container (see docker-compose.yml); the Supervisor RPC port
        # is what actually restarts the plex-desktop process to apply it,
        # since Servarr has no other way to reach into that container.
        self.neko_plex_ini_path = os.environ.get("NEKO_PLEX_INI_PATH", "/neko-plex-data/plex.ini")
        self.neko_supervisor_rpc_port = _env_int("NEKO_SUPERVISOR_RPC_PORT", 9001)
        self.neko_cdp_port = _env_int("NEKO_CDP_PORT", 9223)
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

        # Movie Night's player — neko-mpv/plex-mpv-shim (see neko-mpv/ and
        # app/mpv_control.py). Replaced the Plex-Desktop/neko_control.py
        # path above, which is now fully retired (that container's been
        # removed — neko_control.py/neko_url/neko_admin_password etc.
        # above are unused dead config at this point, left in place only
        # because main.py's setup-wizard test endpoint still references
        # neko_control; not worth a deeper cleanup pass right now).
        # plex-mpv-shim is a REAL Plex Companion client — no mouse-click
        # UI to fall back on once started (NEKO_DESKTOP_INPUT_ENABLED=false
        # there), so pause/resume/seek/stop go through its Companion HTTP
        # API instead (app/mpv_control.py).
        self.neko_mpv_shim_url = os.environ.get("NEKO_MPV_SHIM_URL", "").rstrip("/")
        self.neko_mpv_viewer_url = os.environ.get("NEKO_MPV_VIEWER_URL", "").rstrip("/")
        self.neko_mpv_public_url = os.environ.get("NEKO_MPV_PUBLIC_URL", "").rstrip("/") or self.neko_mpv_viewer_url
        # For building auto-login links (?pwd=...&usr=...) — a viewer-level
        # link for the public WATCH LIVE button (never shows the admin-only
        # native Playback tab), and an admin-level link for whoever started
        # it (does show that tab). See mpv_control.viewer_link/admin_link.
        self.neko_mpv_password = os.environ.get("NEKO_MPV_PASSWORD", "")
        self.neko_mpv_admin_password = os.environ.get("NEKO_MPV_ADMIN_PASSWORD", "")
        # Where Discord's /mpv-play reply links to for the /mpv-remote control
        # page — a plain LAN default since that page is only ever opened from
        # inside the house so far (see NEKO_PUBLIC_URL above for the pattern
        # this would follow if it's ever port-forwarded for outside friends).
        self.servarr_public_url = os.environ.get("SERVARR_PUBLIC_URL", "http://localhost:8888").rstrip("/")
        # Dedicated secret for the /mpv-remote link (see auth.py's
        # require_login_or_remote_key) — deliberately its OWN random
        # alphanumeric token, not a reuse of neko_admin_password. Confirmed
        # live that reusing a real password (which ended in "!") broke the
        # link: several link-auto-detectors (chat clients, browsers) strip
        # trailing punctuation off an auto-linkified URL since they can't
        # tell if it's part of the link or the end of a sentence, silently
        # truncating the key. Plain alphanumeric can't hit that class of bug.
        self.mpv_remote_key = os.environ.get("MPV_REMOTE_KEY", "")

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
