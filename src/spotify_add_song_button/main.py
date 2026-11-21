"""FastAPI application, X-API-Key authentication, POST /add-current, and Mangum Lambda handler."""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from mangum import Mangum
from pydantic import BaseModel

from spotify_add_song_button.config import Settings, get_settings
from spotify_add_song_button.spotify import SpotifyAPIError, SpotifyClient

logger = logging.getLogger(__name__)


class AddCurrentResponse(BaseModel):
    """Response payload for POST /add-current."""

    status: Literal["added", "already_exists", "nothing_playing", "unsupported"]
    message: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage a shared async httpx client for the application lifecycle."""
    async with httpx.AsyncClient(timeout=15.0) as http_client:
        app.state.http_client = http_client
        app.state.spotify_client = None
        yield


class UTF8JSONResponse(JSONResponse):
    """JSONResponse with explicit UTF-8 charset for legacy clients like Windows PowerShell 5.1."""

    media_type = "application/json; charset=utf-8"


app = FastAPI(
    title="SpotifyAddSongButton",
    version="0.1.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)


@app.exception_handler(SpotifyAPIError)
async def spotify_api_error_handler(_request: Request, exc: SpotifyAPIError) -> JSONResponse:
    logger.error("Upstream Spotify API error: %s", exc)
    return UTF8JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": "Spotify API request failed."},
    )


def verify_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Validate the X-API-Key header against the configured API_KEY."""
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )


async def get_spotify_client(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> AsyncIterator[SpotifyClient]:
    """Provide a SpotifyClient backed by the app-level httpx client when available."""
    app_http_client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    if app_http_client is not None:
        cached_client: SpotifyClient | None = getattr(request.app.state, "spotify_client", None)
        if cached_client is None or cached_client._settings is not settings:
            cached_client = SpotifyClient(settings=settings, http_client=app_http_client)
            request.app.state.spotify_client = cached_client
        yield cached_client
        return

    async with httpx.AsyncClient(timeout=15.0) as fallback_client:
        yield SpotifyClient(settings=settings, http_client=fallback_client)


@app.get("/health")
async def health() -> dict[str, str]:
    """Lightweight health check endpoint."""
    return {"status": "ok"}


@app.post(
    "/add-current",
    response_model=AddCurrentResponse,
    dependencies=[Depends(verify_api_key)],
)
async def add_current_track(
    settings: Settings = Depends(get_settings),
    spotify: SpotifyClient = Depends(get_spotify_client),
) -> AddCurrentResponse:
    """Add the currently playing (or paused) Spotify track to the configured playlist."""
    playback = await spotify.get_currently_playing()

    if playback.status == "nothing_playing":
        return AddCurrentResponse(
            status="nothing_playing",
            message=playback.message or "Nothing is currently playing.",
        )

    if playback.status == "unsupported" or playback.track is None:
        return AddCurrentResponse(
            status="unsupported",
            message=playback.message or "Only music tracks can be added.",
        )

    track = playback.track
    existing_uris = await spotify.get_playlist_item_uris(settings.playlist_id)
    if track.uri in existing_uris:
        return AddCurrentResponse(
            status="already_exists",
            message=f"Already in playlist: {track.display_label}",
        )

    await spotify.add_track_to_playlist(settings.playlist_id, track.uri)
    return AddCurrentResponse(
        status="added",
        message=f"Added: {track.display_label}",
    )


handler = Mangum(app)
