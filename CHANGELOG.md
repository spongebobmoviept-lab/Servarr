# Changelog

## 1.2.0 — 2026-09-30

### Added
- **Movie Night only mode** (`SERVARR_MODE=movienight`, or the first choice on the setup page). For people who only want the watch party, typically with the Guest edition of the [Movie Night player](https://github.com/spongebobmoviept-lab/movienight-player) on a friend's Plex server. Release calendars, leveling, moderation, the rules gate, the now-playing board and the Sonarr/Radarr/Tautulli/Plex/Reclaimarr connections are switched off: no background jobs, no slash commands, no message handling, no setup sections, and their setup endpoints answer `409`. The default, `full` (Everything), behaves as before. `SERVARR_MODE` wins over the setup page, which then shows the mode as locked.
- A short setup wizard for that mode: bot token, invite link, server and Movie Night channel, player, done. The invite asks only for View Channels, Send Messages, Embed Links and Read Message History. No privileged intents are required (Server Members is optional, for sending DJs their link by DM), and the bot never pings `@everyone`.
- In that mode the vote, `/play-movie` and the web remote read the library through the paired Movie Night player, so Servarr needs no Plex, Radarr or Sonarr connection.
- The bot connects as soon as a token is saved on the setup page, and reconnects when the token or the mode changes. No container restart.
- "Too many viewers" line on the Now playing posts when the Movie Night player (1.1 or newer) reports more viewers than the host's upload can carry. The setup page shows the player's edition when pairing. Older players are unaffected.
- **Movie Night channel** setting (`MOVIE_NIGHT_CHANNEL_ID`): where the vote and the announcements go. The default is still `#general`.
- **Post a vote every day** switch (`MOVIE_NIGHT_DAILY_VOTE`), on by default.
- `SERVARR_HOST` and `SERVARR_PORT`: where the web page listens (default `0.0.0.0:8888`, as before). The image now starts through `python -m app.serve`, and `python -m app.serve --check` is a health check that follows those settings.
- "Which edition do I need?" table at the top of the README.
- Images are tagged `1.2.0`, `1.2`, `1` and `latest`.

### Changed
- The bundled player's pairing file is watched continuously, so it doesn't matter which container starts first. When the pairing came from that file, Servarr follows the file's address and key (player 1.1 bundles change the address). A pairing typed on the setup page still wins and is left alone.
- If Discord can't be reached at start (network not up yet), the bot keeps retrying instead of staying offline until the next restart.
- Picking another server on the setup page moves the slash commands there right away.
- `/mpv-play` gives the DJ the Movie Night player's admin link (before, only with a plain Companion player).
- The DJ control panel appears as soon as a player is paired or the control channel is picked, not at the next restart.

### Security
- `data/connections_overrides.json` (bot token, API keys) and the admin login file are written with mode `0600`; an existing file is tightened on start.
- Movie Night only mode connects without the Message Content intent and invites the bot with four permissions.

### Fixed
- The first-run Fine-tuning step showed empty number fields and saved zeros for the vote hours, the number of movies offered and the XP settings.
- Reloading the setup page after creating the login but before saving a token got stuck on "An admin login already exists". It now asks you to log in and carries on.
- A bot token set with `DISCORD_TOKEN` no longer has to be pasted again in the wizard.
- A player that reports `buffering` counts as playing.
- The Now playing post no longer fails when the player has no watch link yet.

## 1.1.1 — 2026-09-30

### Fixed
- A fresh `./data` folder created by Docker (owned by root) no longer stops the app: the image's entrypoint hands `/data` to `PUID:PGID` (default 1000) and then drops root before starting the app. Compose passes `PUID`/`PGID` instead of `user:`.
- Reads the Movie Night player bundle's pairing file at `/pairing/pair.json` (`MOVIENIGHT_PAIRING_FILE`), and re-reads it when the player rotates its key.
- `DISCORD_TOKEN` works as an alias for `DISCORD_BOT_TOKEN`.

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
