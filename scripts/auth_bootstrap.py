"""One-time local Spotify OAuth bootstrap script to obtain SPOTIFY_REFRESH_TOKEN.

Usage:
    uv run python scripts/auth_bootstrap.py
"""

from __future__ import annotations

import html
import os
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from dotenv import load_dotenv

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8888
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

SCOPES = [
    "user-read-currently-playing",
    "playlist-modify-private",
    "playlist-modify-public",
    "playlist-read-private",
]


class _OAuthResult:
    def __init__(self) -> None:
        self.done = False
        self.code: str | None = None
        self.error: str | None = None


def _make_handler(expected_state: str, result: _OAuthResult) -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != REDIRECT_PATH:
                self.send_response(404)
                self.end_headers()
                return

            params = urllib.parse.parse_qs(parsed.query)
            state = params.get("state", [None])[0]
            error = params.get("error", [None])[0]
            code = params.get("code", [None])[0]

            if state != expected_state:
                result.error = "State mismatch in OAuth callback (possible CSRF or stale request)."
                result.done = True
                self._send_html(400, "Authorization Failed", result.error)
                return

            if error:
                result.error = f"Spotify returned an error: {error}"
                result.done = True
                self._send_html(400, "Authorization Failed", result.error)
                return

            if not code:
                result.error = "No authorization code was returned in the callback."
                result.done = True
                self._send_html(400, "Authorization Failed", result.error)
                return

            result.code = code
            result.done = True
            self._send_html(
                200,
                "Spotify Authorization Complete",
                "Authorization code received! You can close this tab and return to your terminal.",
            )

        def _send_html(self, status_code: int, title: str, message: str) -> None:
            body = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                f"<title>{html.escape(title)}</title></head>"
                "<body style='font-family: system-ui, sans-serif; margin: 3rem;'>"
                f"<h1>{html.escape(title)}</h1>"
                f"<p>{html.escape(message)}</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            # Suppress noisy BaseHTTPRequestHandler stderr logs.
            return

    return CallbackHandler


def _require_env(name: str, placeholder: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value == placeholder:
        print(
            f"Error: {name} is missing or still set to a placeholder in .env.",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    client_id = _require_env("SPOTIFY_CLIENT_ID", "your_client_id")
    client_secret = _require_env("SPOTIFY_CLIENT_SECRET", "your_client_secret")

    state = secrets.token_urlsafe(32)
    result = _OAuthResult()
    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _make_handler(state, result))

    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "scope": " ".join(SCOPES),
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "show_dialog": "true",
        }
    )
    auth_url = f"{SPOTIFY_AUTH_URL}?{query}"

    print(f"Listening for Spotify OAuth callback on {REDIRECT_URI} ...")
    print("Opening browser for Spotify authorization.")
    print(f"If your browser does not open automatically, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    try:
        while not result.done:
            server.handle_request()
    finally:
        server.server_close()

    if result.error or not result.code:
        print(f"OAuth failed: {result.error or 'Unknown error'}", file=sys.stderr)
        sys.exit(1)

    response = httpx.post(
        SPOTIFY_TOKEN_URL,
        auth=httpx.BasicAuth(client_id, client_secret),
        data={
            "grant_type": "authorization_code",
            "code": result.code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=15.0,
    )

    if response.is_error:
        print(
            f"Token exchange failed ({response.status_code}): {response.text}",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = response.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        print(
            f"Token response did not contain a refresh_token: {payload}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nSuccess! Copy this line into your .env file:\n")
    print(f"SPOTIFY_REFRESH_TOKEN={refresh_token}")
    if granted_scope := payload.get("scope"):
        print(f"\nGranted scopes: {granted_scope}")


if __name__ == "__main__":
    main()
