# SpotifyAddSongButton

A single-user, serverless Python API that provides a one-click way to add your currently playing (or paused) Spotify track to a default playlist from a Windows global hotkey or an Android Quick Settings tile / home screen widget.

## Features

- **One-click add (`POST /add-current`):** Protected by an `X-API-Key` header.
- **Handles all playback states:** Works for playing or paused music tracks; gracefully skips podcasts/episodes, local files, or when nothing is playing.
- **Duplicate detection:** Scans the target playlist via Spotify's `/v1/playlists/{id}/items` endpoint (Feb 2026 schema), paginating concurrently (`asyncio.Semaphore(5)`) to stay within rate limits while avoiding duplicates.
- **Serverless AWS Lambda deployment:** Uses FastAPI + async `httpx` + `Mangum` behind a Lambda Function URL, loading secrets from **AWS SSM Parameter Store** (`SecureString`) and persisting rotated Spotify refresh tokens back to SSM automatically.
- **Desktop & mobile triggers:** Includes a zero-dependency PowerShell toast trigger script for Windows hotkeys and setup instructions for Android **HTTP Shortcuts**.

## Project Structure

```text
SpotifyAddSongButton/
├── scripts/
│   ├── auth_bootstrap.py   # One-time local OAuth flow to get SPOTIFY_REFRESH_TOKEN
│   ├── build_zip.ps1       # Builds Linux-compatible deployment.zip for AWS Lambda
│   └── windows_trigger.ps1 # Windows shortcut trigger with native toast notifications
├── src/
│   └── spotify_add_song_button/
│       ├── config.py       # Local .env + AWS SSM Parameter Store configuration
│       ├── spotify.py      # Async httpx Spotify Web API client
│       └── main.py         # FastAPI app, X-API-Key auth, Mangum Lambda handler
└── tests/
    └── test_api.py         # pytest + respx unit tests
```

## Prerequisites

- Python 3.14+ and [`uv`](https://docs.astral.sh/uv/)
- A Spotify app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) with Redirect URI set to `http://127.0.0.1:8888/callback`
- An AWS account (for Lambda + SSM Parameter Store deployment)

## Local Setup

1. **Install dependencies:**
   ```powershell
   uv sync
   ```

2. **Create `.env` from `.env.example`:**
   ```powershell
   Copy-Item .env.example .env
   ```
   Fill in `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `PLAYLIST_ID`, and a strong random `API_KEY`.

3. **Generate `SPOTIFY_REFRESH_TOKEN` (one-time OAuth flow):**
   ```powershell
   uv run python scripts/auth_bootstrap.py
   ```
   Paste the printed `SPOTIFY_REFRESH_TOKEN=...` line into `.env`.

4. **Run tests:**
   ```powershell
   uv run pytest -v
   ```

5. **Run locally:**
   ```powershell
   uv run uvicorn spotify_add_song_button.main:app --reload
   ```
   Test with:
   ```powershell
   Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/add-current" -Headers @{ "X-API-Key" = "<YOUR_API_KEY>" }
   ```

## AWS Lambda Deployment

1. **Build `deployment.zip`:**
   ```powershell
   .\scripts\build_zip.ps1
   ```
   *(Targets Python 3.14 `x86_64` by default; pass `-Architecture arm64` for Graviton/ARM64. Excludes `boto3` since Lambda provides it.)*

2. **Create SSM Parameter Store secrets (`SecureString`):**
   - `/spotify-add-song-button/SPOTIFY_CLIENT_SECRET`
   - `/spotify-add-song-button/SPOTIFY_REFRESH_TOKEN`
   - `/spotify-add-song-button/API_KEY`

3. **Configure AWS Lambda:**
   - **Runtime:** Python 3.14 (`x86_64`)
   - **Upload:** `deployment.zip`
   - **Handler:** `spotify_add_song_button.main.handler`
   - **Memory / Timeout:** `256 MB` / `15 sec`
   - **Environment Variables:** `SPOTIFY_CLIENT_ID`, `PLAYLIST_ID`
   - **IAM Execution Role:** Allow `ssm:GetParameters` and `ssm:PutParameter` on `arn:aws:ssm:<REGION>:<ACCOUNT_ID>:parameter/spotify-add-song-button/*`
   - **Function URL:** Auth type `NONE` (endpoint authentication is enforced via `X-API-Key`)

## Client Triggers

### Windows Hotkey
1. Add `LAMBDA_FUNCTION_URL=https://<your-url>.lambda-url.<region>.on.aws` to `.env`.
2. Create a Start Menu or Desktop shortcut (`.lnk`) pointing to:
   ```text
   powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "<repo-path>\scripts\windows_trigger.ps1"
   ```
3. Set **Run: Minimized** and assign a **Shortcut key** (e.g., `Ctrl+Alt+S`) in the shortcut's Properties.

### Android (HTTP Shortcuts)
1. Install **HTTP Shortcuts** and create a `POST` shortcut to `https://<your-url>.lambda-url.<region>.on.aws/add-current` with header `X-API-Key: <API_KEY>`.
2. Set **Response Handling → UI Type** to **None**, and add a **Run on Success** script:
   ```javascript
   const data = JSON.parse(response.body);
   showToast(data.message || "Done");
   ```
3. Pin it to your home screen or enable it as an Android **Quick Settings Tile**.
