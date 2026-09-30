# Changelog

## 1.1.0 — 2026-09-30

### Added
- **Movie Night player integration**: pair the [Movie Night player](https://github.com/spongebobmoviept-lab/movienight-player) by pasting its `MNP1-` pair code (or automatically via its bundle's `pair.json`); Servarr drives its v1 API with an `Authorization: Bearer` key and uses the watch links it reports. Plain Plex Companion players (e.g. plex-mpv-shim) still work. This replaces the old browser automation. Everything else works without a player.
- **Turnkey setup page**: pick your Discord server, channels and roles from dropdowns read live from Discord (with an invite link), pair the player, and get plain-English errors from every connection test. The web-remote key is generated automatically.
- DJ commands `/play-movie`, `/stop-movie`, `/mpv-play`, `/mpv-pause`, `/mpv-resume`, `/mpv-seek`, `/mpv-stop`, gated by Manage Server or a configurable DJ role (ID or name). Blocked attempts are logged to the mod log.
- Pause/seek/stop buttons on the "Now playing" message, and a persistent DJ control panel with **Search & Play** in a private control channel (`MOVIE_NIGHT_CONTROL_CHANNEL`).
- `/mpv-remote` web remote (search, library browsing, TV episodes, continue watching) and its `/api/mpv/*` endpoints.
- Movie Night vote shows a live tally and persists across restarts.
- Self-updating top-10 leaderboard board (`LEADERBOARD_CHANNEL`).
- Release digests stay at the bottom of their channels when other bots post there.
- Optional pinned-tile support for another bot's webhook board (`PINNED_TILE_CHANNEL`, `PINNED_TILE_TITLE_MATCH`, off by default).
- `POST /api/release-digest/force` (admin login).
- `EXTRA_READ_ONLY_CHANNELS`; the mod log now quotes messages moved out of read-only channels.
- Unit tests (`tests/`).
- Prebuilt multi-arch images (amd64 + arm64) at `ghcr.io/spongebobmoviept-lab/servarr` (`1.1.0`, `1.1`, `latest`); `docker-compose.yml` uses the image by default.

### Security
- The web remote's key is carried in the URL fragment and sent as an `X-Remote-Key` header or HttpOnly cookie; the API no longer accepts it as a `?key=` query parameter. Key checks are constant-time and throttled.
- CORS and Private Network Access headers are only sent to allow-listed origins (previously any origin was reflected with credentials); cross-origin POSTs from other origins are refused.
- The admin player link is only posted in a control channel that is hidden from @everyone and the Member role.
- Plex keys in `/api/mpv/*` must be numeric; seek and offset values are bounded.
- Log redaction also covers `X-Plex-Token`, `pwd=`, `key=` and webhook URLs.
- Docker base image pinned to `python:3.12-slim-bookworm`.

### Changed
- `SERVARR_PUBLIC_URL` defaults to `http://localhost:8888`, and the setup page pre-fills it from the address you opened it on.
- Player env vars are now `PLAYER_URL`, `PLAYER_KEY`, `PLAYER_VIEWER_URL`, … (the `NEKO_MPV_*` names still work).
- The `.env` template is now `.env.example`, and `.env` is optional and git-ignored. The container runs as `PUID`/`PGID` (default 1000).

### Removed
- The browser-automation Neko path (`neko_control.py`), `PLEX_HOME_PIN`, `/api/setup/test-neko` and the shared Neko env mount.

## 1.0.0

Initial release.
