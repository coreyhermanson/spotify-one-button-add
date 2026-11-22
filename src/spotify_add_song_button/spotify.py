"""Async Spotify Web API client (Feb 2026 playlist /items schema)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from spotify_add_song_button.config import Settings, persist_refresh_token

logger = logging.getLogger(__name__)

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
PLAYLIST_PAGE_LIMIT = 50
MAX_CONCURRENT_REQUESTS = 5
TOKEN_EXPIRY_BUFFER_SECONDS = 60.0
PLAYLIST_ITEMS_FIELDS = "items(item(uri)),total,next"


class SpotifyAPIError(Exception):
    """Raised when an upstream Spotify API or OAuth request fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TrackInfo:
    """Minimal metadata for a Spotify music track."""

    uri: str
    name: str
    artists: tuple[str, ...]

    @property
    def display_label(self) -> str:
        artist_text = ", ".join(self.artists) if self.artists else "Unknown Artist"
        return f"{self.name} – {artist_text}"


@dataclass(frozen=True)
class PlaybackResult:
    """Result of querying Spotify's currently-playing endpoint."""

    status: Literal["track", "nothing_playing", "unsupported"]
    message: str | None = None
    track: TrackInfo | None = None


RefreshTokenCallback = Callable[[str], Awaitable[None]]


class SpotifyClient:
    """Async Spotify Web API client supporting token refresh, playback lookup, and playlists."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient,
        on_refresh_token_rotated: RefreshTokenCallback | None = None,
    ) -> None:
        self._settings = settings
        self._http = http_client
        self._on_refresh_token_rotated = on_refresh_token_rotated or self._default_persist_token
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    async def _default_persist_token(self, new_refresh_token: str) -> None:
        await asyncio.to_thread(persist_refresh_token, self._settings, new_refresh_token)

    async def _get_access_token(
        self, *, force_refresh: bool = False, failed_token: str | None = None
    ) -> str:
        now = time.monotonic()
        if (
            not force_refresh
            and self._access_token
            and now < self._token_expires_at
        ):
            return self._access_token

        async with self._token_lock:
            now = time.monotonic()
            if self._access_token and now < self._token_expires_at:
                # Another request may have already refreshed while we waited on the
                # lock; only force a second refresh if our failed token is still current.
                if not force_refresh or self._access_token != failed_token:
                    return self._access_token

            return await self._refresh_access_token()

    async def _refresh_access_token(self) -> str:
        response = await self._http.post(
            SPOTIFY_TOKEN_URL,
            auth=httpx.BasicAuth(
                self._settings.spotify_client_id,
                self._settings.spotify_client_secret,
            ),
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._settings.spotify_refresh_token,
            },
        )
        if response.is_error:
            raise SpotifyAPIError(
                f"Spotify token refresh failed ({response.status_code}): {response.text}",
                status_code=response.status_code,
            )

        payload = response.json()
        access_token = payload.get("access_token")
        if not access_token:
            raise SpotifyAPIError("Spotify token refresh response missing access_token.")

        expires_in = float(payload.get("expires_in", 3600))
        self._access_token = access_token
        self._token_expires_at = time.monotonic() + max(0.0, expires_in - TOKEN_EXPIRY_BUFFER_SECONDS)

        rotated_token = payload.get("refresh_token")
        if rotated_token and rotated_token != self._settings.spotify_refresh_token:
            await self._on_refresh_token_rotated(rotated_token)

        return access_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retry_on_401: bool = True,
    ) -> httpx.Response:
        token = await self._get_access_token()
        url = f"{SPOTIFY_API_BASE}{path}"

        response = await self._http.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json,
        )

        if response.status_code == 401 and retry_on_401:
            logger.info("Spotify access token rejected with 401; refreshing and retrying once.")
            token = await self._get_access_token(force_refresh=True, failed_token=token)
            response = await self._http.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                json=json,
            )

        if response.is_error:
            raise SpotifyAPIError(
                f"Spotify API {method} {path} failed ({response.status_code}): {response.text}",
                status_code=response.status_code,
            )

        return response

    async def get_currently_playing(self) -> PlaybackResult:
        """Fetch currently playing (or paused) Spotify item and classify it."""
        response = await self._request("GET", "/me/player/currently-playing")

        if response.status_code == 204 or not response.content:
            return PlaybackResult(
                status="nothing_playing",
                message="Nothing is currently playing.",
            )

        data = response.json()
        currently_playing_type = data.get("currently_playing_type")
        item = data.get("item")

        # Note: Spotify returns currently_playing_type="episode" with item=null for podcasts
        # unless additional_types=episode is passed, so check the type before item is None.
        if currently_playing_type is not None and currently_playing_type != "track":
            return PlaybackResult(
                status="unsupported",
                message="Only music tracks can be added.",
            )

        if not isinstance(item, dict):
            return PlaybackResult(
                status="nothing_playing",
                message="Nothing is currently playing.",
            )

        uri = item.get("uri") or ""
        if item.get("is_local") is True or uri.startswith("spotify:local:"):
            return PlaybackResult(
                status="unsupported",
                message="Local files cannot be added.",
            )

        if not uri:
            return PlaybackResult(
                status="unsupported",
                message="Only music tracks can be added.",
            )

        name = item.get("name") or "Unknown Track"
        artists = tuple(
            artist["name"]
            for artist in item.get("artists") or []
            if isinstance(artist, dict) and artist.get("name")
        )
        return PlaybackResult(
            status="track",
            track=TrackInfo(uri=uri, name=name, artists=artists),
        )

    @staticmethod
    def _extract_item_uris(payload: dict[str, Any]) -> set[str]:
        uris: set[str] = set()
        for entry in payload.get("items") or []:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item")
            if isinstance(item, dict):
                uri = item.get("uri")
                if isinstance(uri, str) and uri:
                    uris.add(uri)
        return uris

    async def _fetch_playlist_items_page(
        self,
        playlist_id: str,
        offset: int,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/playlists/{playlist_id}/items",
            params={
                "limit": PLAYLIST_PAGE_LIMIT,
                "offset": offset,
                "fields": PLAYLIST_ITEMS_FIELDS,
            },
        )
        return response.json()

    async def get_playlist_item_uris(self, playlist_id: str) -> set[str]:
        """Fetch all item URIs in a playlist using GET /v1/playlists/{id}/items.

        Fetches page 1 to discover `total`, then fetches remaining pages concurrently
        throttled by asyncio.Semaphore(5).
        """
        first_page = await self._fetch_playlist_items_page(playlist_id, offset=0)
        uris = self._extract_item_uris(first_page)

        total = int(first_page.get("total") or 0)
        if total <= PLAYLIST_PAGE_LIMIT:
            return uris

        remaining_offsets = range(PLAYLIST_PAGE_LIMIT, total, PLAYLIST_PAGE_LIMIT)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async def fetch_with_limit(offset: int) -> set[str]:
            async with semaphore:
                page = await self._fetch_playlist_items_page(playlist_id, offset=offset)
                return self._extract_item_uris(page)

        pages = await asyncio.gather(*(fetch_with_limit(offset) for offset in remaining_offsets))
        for page_uris in pages:
            uris.update(page_uris)

        return uris

    async def add_track_to_playlist(self, playlist_id: str, track_uri: str) -> None:
        """Append a track URI to the bottom of a playlist via POST /v1/playlists/{id}/items."""
        await self._request(
            "POST",
            f"/playlists/{playlist_id}/items",
            json={"uris": [track_uri]},
        )
