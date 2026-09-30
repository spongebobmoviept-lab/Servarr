import os

APP_VERSION = "1.2.0"


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in ("0", "false", "no", "off")


def _env_list(name: str, default: str = "") -> list[str]:
    return [part.strip() for part in os.environ.get(name, default).split(",") if part.strip()]


class Settings:
    def __init__(self) -> None:
        # Which features run: "full" (everything, the default) or
        # "movienight" (Movie Night only). Also picked on the setup page;
        # SERVARR_MODE wins when set. See features.py.
        self.servarr_mode_env = os.environ.get("SERVARR_MODE", "").strip()
        self.servarr_mode = ""  # the setup page's choice (settings_overrides.json)

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
        # login to call its pause-upgrade endpoint (optional) so nobody's
        # shared viewing gets interrupted by a 4K swap mid-movie.
        self.reclaimarr_url = os.environ.get("RECLAIMARR_URL", "")
        self.reclaimarr_auth_user = os.environ.get("RECLAIMARR_AUTH_USER", "")
        self.reclaimarr_auth_pass = os.environ.get("RECLAIMARR_AUTH_PASS", "")
        self.movie_night_vote_hour_utc = _env_int("MOVIE_NIGHT_VOTE_HOUR_UTC", 22)  # ~6pm US Eastern
        self.movie_night_hour_utc = _env_int("MOVIE_NIGHT_HOUR_UTC", 1)  # ~9pm US Eastern, next UTC day
        self.movie_night_candidate_count = _env_int("MOVIE_NIGHT_CANDIDATE_COUNT", 5)
        # The automatic part: post the vote every day and start the winner
        # at showtime. Off = Movie Night only happens when a DJ/mod runs it
        # (/play-movie, /movie-night, /movie-night-play).
        self.movie_night_daily_vote = _env_bool("MOVIE_NIGHT_DAILY_VOTE", True)
        self.movie_night_pause_upgrade_minutes = _env_int("MOVIE_NIGHT_PAUSE_UPGRADE_MINUTES", 240)
        # Who's allowed to start/stop/control playback with /play-movie,
        # /stop-movie, and /mpv-* outside the normal nightly vote — anyone
        # with Manage Server always can;
        # this optionally extends it to a specific role (e.g. "Movie Night
        # DJ") without handing those people full mod permissions. Blank
        # means only Manage Server can use it.
        self.movie_night_dj_role_id = os.environ.get("MOVIE_NIGHT_DJ_ROLE_ID", "").strip()
        # Where the vote, the announcements and the Watch Live button go
        # (a channel ID, picked in the setup page). Blank means #general.
        self.movie_night_channel_id = os.environ.get("MOVIE_NIGHT_CHANNEL_ID", "").strip()

        # Movie Night's player (optional) — any player that speaks the Plex
        # Companion HTTP API (e.g. plex-mpv-shim) and is shown to viewers
        # through a web page such as a Neko virtual browser. See the README's
        # "Movie Night player" section for the exact HTTP contract.
        # Normally paired in the setup wizard (paste the player's pair code);
        # these env vars are the fallback.
        #   PLAYER_URL         — the player's address (keep it on your LAN)
        #   PLAYER_KEY         — pairing key, sent as "Authorization: Bearer"
        #   PLAYER_MODE        — "movienight" or "companion"; set by pairing
        #   PLAYER_PAIR_FILE   — pair.json a bundled player exports; read on
        #                        start when no player is set (zero-click)
        #   PLAYER_VIEWER_URL  — companion mode only: the page people open
        # The older NEKO_MPV_SHIM_URL / NEKO_MPV_PUBLIC_URL / NEKO_MPV_VIEWER_URL
        # names still work.
        self.neko_mpv_shim_url = (os.environ.get("PLAYER_URL") or os.environ.get("NEKO_MPV_SHIM_URL", "")).rstrip("/")
        self.player_key = os.environ.get("PLAYER_KEY", "")
        self.player_mode = os.environ.get("PLAYER_MODE", "").strip().lower()
        # Also accepts MOVIENIGHT_PAIRING_FILE (the player bundle's name).
        # Empty = look in /pairing/pair.json and /pair/pair.json.
        self.player_pair_file = (os.environ.get("PLAYER_PAIR_FILE") or os.environ.get("MOVIENIGHT_PAIRING_FILE", "")).strip()
        # Where the saved pairing came from (kept by connections_store, not
        # set by hand): "file" = the pairing file, "manual" = typed on the
        # setup page, "" = unknown (paired before 1.2, or from PLAYER_URL).
        # player_pair_file_url is the address the pairing file last gave us.
        self.player_pair_source = ""
        self.player_pair_file_url = ""
        self.neko_mpv_viewer_url = os.environ.get("NEKO_MPV_VIEWER_URL", "").rstrip("/")
        self.neko_mpv_public_url = (
            os.environ.get("PLAYER_VIEWER_URL") or os.environ.get("NEKO_MPV_PUBLIC_URL", "")
        ).rstrip("/") or self.neko_mpv_viewer_url
        # Optional auto-login links (?pwd=...): a viewer-level password for
        # the public WATCH LIVE button, and an admin-level one that only
        # DJs/mods ever receive (ephemeral replies or the private control
        # channel). Leave blank to link to the bare viewer page.
        self.neko_mpv_password = os.environ.get("PLAYER_VIEWER_PASSWORD") or os.environ.get("NEKO_MPV_PASSWORD", "")
        self.neko_mpv_admin_password = os.environ.get("PLAYER_ADMIN_PASSWORD") or os.environ.get("NEKO_MPV_ADMIN_PASSWORD", "")
        # Base URL used to build the /mpv-remote control-page link Servarr
        # sends to DJs. Set it to however your DJs reach Servarr.
        self.servarr_public_url = os.environ.get("SERVARR_PUBLIC_URL", "http://localhost:8888").rstrip("/")
        # Shared secret for the /mpv-remote page (see auth.py). Use a long
        # random alphanumeric string (link auto-detectors can strip trailing
        # punctuation). The link carries it in the URL *fragment* (#key=...),
        # which browsers never send to the server; the page then sends it
        # in an X-Remote-Key header. Generated automatically on first start
        # if you don't set one.
        self.mpv_remote_key = os.environ.get("MPV_REMOTE_KEY", "")
        # Extra browser origins allowed to call Servarr cross-origin — e.g.
        # the viewer page's origin if it embeds /mpv-remote in an iframe.
        # The viewer/public player URLs above are allowed automatically.
        self.cors_allowed_origins = _env_list("CORS_ALLOWED_ORIGINS")

        # Channel names (without the #). Defaults suit a fresh server; change
        # them to match an existing one.
        # Private channel for DJs/mods: the always-on Movie Night control
        # panel and the admin player link are posted here, so make it
        # visible to DJs/mods only.
        self.movie_night_control_channel = os.environ.get("MOVIE_NIGHT_CONTROL_CHANNEL", "movie-night-control").strip()
        self.leaderboard_channel = os.environ.get("LEADERBOARD_CHANNEL", "leaderboard").strip()
        # Any other channels that should be read-only (bot/webhook posts
        # only), in addition to the built-in list in gating.py.
        self.extra_read_only_channels = _env_list("EXTRA_READ_ONLY_CHANNELS")
        # Optional: keep another bot's single self-editing webhook "tile"
        # (e.g. a status board) at the bottom of its channel. When anything
        # else posts after the tile, Servarr deletes the stale tile so its
        # owner re-posts a fresh one at the bottom, and stray posts in that
        # channel are removed while no tile exists. Only enable this for a
        # channel dedicated to that tile, and only if the tile's owner
        # re-posts when its message is deleted. Needs Manage Messages.
        #   PINNED_TILE_CHANNEL      — channel name; blank = off (default)
        #   PINNED_TILE_TITLE_MATCH  — text the tile's embed title contains
        self.pinned_tile_channel = os.environ.get("PINNED_TILE_CHANNEL", "").strip()
        self.pinned_tile_title_match = os.environ.get("PINNED_TILE_TITLE_MATCH", "").strip()

        self.plex_url = os.environ.get("PLEX_URL", "").rstrip("/")
        self.plex_token = os.environ.get("PLEX_TOKEN", "")

        self.discord_bot_token = (os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN", "")).strip()
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
