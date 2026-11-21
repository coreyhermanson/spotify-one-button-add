# SpotifyAddSongButton — Project Context & Architecture Guide

## Overview
A single-user, serverless Python API (v1 complete and deployed) that provides a one-click way to add the currently playing (or paused) Spotify track to a default playlist from an Android widget/Quick Settings tile or a Windows global shortcut key.

## Tech Stack & Architecture
- **Python & Tooling:** Python 3.14, managed with `uv` (`pyproject.toml`, `uv.lock`, `src/` layout)
- **Framework:** FastAPI + async `httpx` + `pydantic-settings`
- **AWS Compute:** AWS Lambda (`python3.14`, `x86_64`) + Lambda Function URL (`Auth: NONE`, enforced via `X-API-Key`) using `Mangum` (`spotify_add_song_button.main.handler`)
- **AWS Secrets & Config (`config.py`):**
  - **Local dev:** Loads `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`, `PLAYLIST_ID`, and `API_KEY` from `.env` (`.env` also stores `LAMBDA_FUNCTION_URL` for `scripts/windows_trigger.ps1`).
  - **AWS Lambda (`AWS_LAMBDA_FUNCTION_NAME` set):** Reads `SPOTIFY_CLIENT_ID` and `PLAYLIST_ID` from Lambda environment variables; batch-fetches `SecureString` secrets from AWS SSM Parameter Store via `boto3`:
    - `/spotify-add-song-button/SPOTIFY_CLIENT_SECRET`
    - `/spotify-add-song-button/SPOTIFY_REFRESH_TOKEN`
    - `/spotify-add-song-button/API_KEY`
  - **Token rotation:** If Spotify returns a new `refresh_token` during refresh, `persist_refresh_token` updates it in memory and persists it back to `/spotify-add-song-button/SPOTIFY_REFRESH_TOKEN` in SSM (`Overwrite=True`), or logs a warning in local dev.
- **Deployment:** Manual zip upload in the AWS Lambda Console via `scripts/build_zip.ps1`, which packages `src/spotify_add_song_button/` + Linux wheels (`x86_64-manylinux_2_28` by default, `-Architecture arm64` optional) into `deployment.zip` (excluding `boto3` and its AWS-provided transitive dependencies) with POSIX paths and Unix permissions.
- **Testing:** `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`), `respx`.

## Critical Constraints & Gotchas (Preserve on Future Changes)
1. **Feb 2026 Spotify Web API playlist endpoints:** Always use `GET /v1/playlists/{id}/items` and `POST /v1/playlists/{id}/items` — **never** deprecated `/tracks`.
2. **Response schema:** Playlist entries use `item` instead of `track`. Parse `items[].item.uri` using `fields=items(item(uri)),total,next` and guard against `null` items.
3. **Pagination & rate limits:** `GET /v1/playlists/{id}/items` caps at 50 items/page. Fetch page 1 (`offset=0`) to read `total`, then fetch remaining pages concurrently with `asyncio.gather` throttled by `asyncio.Semaphore(5)`.
4. **Podcast detection ordering:** Check `currently_playing_type != "track"` *before* checking `item is None`, because Spotify returns `currently_playing_type: "episode"` with `item: null` for podcasts.
5. **Explicit UTF-8 JSON responses:** `main.py` uses `UTF8JSONResponse` (`application/json; charset=utf-8`) as FastAPI's `default_response_class` so Windows PowerShell 5.1 (`Invoke-RestMethod`) decodes `–` and non-ASCII track/artist names without mojibake.
6. **OAuth Redirect URI & Scopes (`scripts/auth_bootstrap.py`):**
   - Redirect URI: `http://127.0.0.1:8888/callback` (Spotify blocks `localhost`).
   - Scopes: `user-read-currently-playing`, `playlist-modify-private`, `playlist-modify-public`, `playlist-read-private`.

## API Endpoints (`src/spotify_add_song_button/main.py`)
- **`GET /health`**: Unauthenticated liveness check returning `{"status": "ok"}`.
- **`POST /add-current`**:
  1. **Auth:** Constant-time check of `X-API-Key` against `API_KEY`; returns `401` if missing/invalid.
  2. **Current playback:** Calls `GET /v1/me/player/currently-playing` (auto-refreshes access token and retries once on `401`):
     - `204 No Content` or no item → `{"status": "nothing_playing", "message": "Nothing is currently playing."}`
     - Non-track (`currently_playing_type != "track"`) → `{"status": "unsupported", "message": "Only music tracks can be added."}`
     - Local file (`item.is_local == True` or `spotify:local:`) → `{"status": "unsupported", "message": "Local files cannot be added."}`
     - Playing or paused track → proceed.
  3. **Duplicate check:** Fetches all URIs in `PLAYLIST_ID` concurrently (`Semaphore(5)`).
     - Already present → `{"status": "already_exists", "message": "Already in playlist: <Track> – <Artist>"}`.
  4. **Add track:** `POST /v1/playlists/{PLAYLIST_ID}/items` with `{"uris": [track_uri]}` → `{"status": "added", "message": "Added: <Track> – <Artist>"}`.
  5. **Upstream errors:** `SpotifyAPIError` returns `502 Bad Gateway`.

## Directory Structure
```text
SpotifyAddSongButton/
├── GEMINI.md
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── scripts/
│   ├── auth_bootstrap.py   # One-time local OAuth flow to obtain SPOTIFY_REFRESH_TOKEN
│   ├── build_zip.ps1       # Builds Linux-compatible deployment.zip for AWS Lambda upload
│   └── windows_trigger.ps1 # Windows global shortcut trigger + native WinRT toast notification
├── src/
│   └── spotify_add_song_button/
│       ├── __init__.py
│       ├── config.py       # Local .env + AWS SSM Parameter Store loading & token persistence
│       ├── spotify.py      # Async httpx client (auth refresh, playback, dupe check, add)
│       └── main.py         # FastAPI app, UTF8JSONResponse, X-API-Key, endpoints, Mangum handler
└── tests/
    └── test_api.py         # pytest + respx unit tests (17 tests)
```

## Common Dev & Release Commands
- **Run tests:** `uv run pytest -v`
- **Run locally:** `uv run uvicorn spotify_add_song_button.main:app --reload`
- **Rebuild Lambda bundle:** `.\scripts\build_zip.ps1` (then upload `deployment.zip` in the AWS Lambda Console)
- **Test Windows toast:** `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows_trigger.ps1 -TestToast`