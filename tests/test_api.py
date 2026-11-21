"""Unit tests for SpotifyAddSongButton API, SpotifyClient, and config/SSM loading."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from spotify_add_song_button.config import (
    Settings,
    load_settings,
    persist_refresh_token,
)
from spotify_add_song_button.main import app, get_settings, get_spotify_client
from spotify_add_song_button.spotify import (
    PLAYLIST_ITEMS_FIELDS,
    SPOTIFY_API_BASE,
    SPOTIFY_TOKEN_URL,
    SpotifyClient,
)

TEST_API_KEY = "test-secret-api-key"
TEST_PLAYLIST_ID = "playlist_2026_id"
TEST_TRACK_URI = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"


def make_test_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "spotify_client_id": "test_client_id",
        "spotify_client_secret": "test_client_secret",
        "spotify_refresh_token": "test_refresh_token",
        "playlist_id": TEST_PLAYLIST_ID,
        "api_key": TEST_API_KEY,
        "use_ssm": False,
    }
    defaults.update(overrides)
    return Settings.model_construct(**defaults)


def mock_token_route(
    router: respx.MockRouter,
    *,
    access_token: str = "access_token_1",
    refresh_token: str | None = None,
) -> respx.Route:
    body: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    if refresh_token is not None:
        body["refresh_token"] = refresh_token
    return router.post(SPOTIFY_TOKEN_URL).mock(return_value=httpx.Response(200, json=body))


def make_track_payload(
    *,
    uri: str = TEST_TRACK_URI,
    name: str = "Never Gonna Give You Up",
    artists: list[str] | None = None,
    is_playing: bool = True,
    is_local: bool = False,
) -> dict[str, Any]:
    artist_names = artists if artists is not None else ["Rick Astley"]
    return {
        "is_playing": is_playing,
        "currently_playing_type": "track",
        "item": {
            "uri": uri,
            "name": name,
            "is_local": is_local,
            "artists": [{"name": artist} for artist in artist_names],
        },
    }


@pytest.fixture
def test_settings() -> Settings:
    return make_test_settings()


@pytest.fixture
async def api_client(test_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Async test client against the FastAPI app with isolated test settings."""
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.state.http_client = None
    app.state.spotify_client = None
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def test_health_endpoint(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_auth_missing_api_key_returns_401(api_client: httpx.AsyncClient) -> None:
    response = await api_client.post("/add-current")
    assert response.status_code == 401


async def test_auth_invalid_api_key_returns_401(api_client: httpx.AsyncClient) -> None:
    response = await api_client.post("/add-current", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401


@respx.mock
async def test_nothing_playing_204(api_client: httpx.AsyncClient) -> None:
    mock_token_route(respx)
    respx.get(f"{SPOTIFY_API_BASE}/me/player/currently-playing").mock(
        return_value=httpx.Response(204)
    )

    response = await api_client.post("/add-current", headers={"X-API-Key": TEST_API_KEY})
    assert response.status_code == 200
    assert response.json() == {
        "status": "nothing_playing",
        "message": "Nothing is currently playing.",
    }


@respx.mock
async def test_nothing_playing_null_item(api_client: httpx.AsyncClient) -> None:
    mock_token_route(respx)
    respx.get(f"{SPOTIFY_API_BASE}/me/player/currently-playing").mock(
        return_value=httpx.Response(
            200,
            json={"is_playing": False, "currently_playing_type": "track", "item": None},
        )
    )

    response = await api_client.post("/add-current", headers={"X-API-Key": TEST_API_KEY})
    assert response.status_code == 200
    assert response.json() == {
        "status": "nothing_playing",
        "message": "Nothing is currently playing.",
    }


@respx.mock
async def test_podcast_episode_unsupported(api_client: httpx.AsyncClient) -> None:
    mock_token_route(respx)
    # Spotify returns item=null when an episode plays without additional_types=episode.
    respx.get(f"{SPOTIFY_API_BASE}/me/player/currently-playing").mock(
        return_value=httpx.Response(
            200,
            json={"is_playing": True, "currently_playing_type": "episode", "item": None},
        )
    )

    response = await api_client.post("/add-current", headers={"X-API-Key": TEST_API_KEY})
    assert response.status_code == 200
    assert response.json() == {
        "status": "unsupported",
        "message": "Only music tracks can be added.",
    }


@respx.mock
async def test_local_file_unsupported(api_client: httpx.AsyncClient) -> None:
    mock_token_route(respx)
    respx.get(f"{SPOTIFY_API_BASE}/me/player/currently-playing").mock(
        return_value=httpx.Response(
            200,
            json=make_track_payload(
                uri="spotify:local:Artist:Album:Demo:180",
                name="Demo Track",
                is_local=True,
            ),
        )
    )

    response = await api_client.post("/add-current", headers={"X-API-Key": TEST_API_KEY})
    assert response.status_code == 200
    assert response.json() == {
        "status": "unsupported",
        "message": "Local files cannot be added.",
    }


@respx.mock
@pytest.mark.parametrize("is_playing", [True, False])
async def test_adds_playing_or_paused_track_not_in_playlist(
    api_client: httpx.AsyncClient,
    is_playing: bool,
) -> None:
    mock_token_route(respx)
    respx.get(f"{SPOTIFY_API_BASE}/me/player/currently-playing").mock(
        return_value=httpx.Response(
            200,
            json=make_track_payload(
                name="Midnight City",
                artists=["M83"],
                is_playing=is_playing,
            ),
        )
    )
    get_items_route = respx.get(
        f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items",
        params={"limit": "50", "offset": "0", "fields": PLAYLIST_ITEMS_FIELDS},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 2,
                "next": None,
                "items": [
                    {"item": {"uri": "spotify:track:other1"}},
                    {"item": {"uri": "spotify:track:other2"}},
                ],
            },
        )
    )
    post_items_route = respx.post(
        f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items"
    ).mock(return_value=httpx.Response(201, json={"snapshot_id": "snap_1"}))

    response = await api_client.post("/add-current", headers={"X-API-Key": TEST_API_KEY})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.json() == {
        "status": "added",
        "message": "Added: Midnight City – M83",
    }
    assert get_items_route.called
    assert post_items_route.called
    assert json.loads(post_items_route.calls[0].request.content) == {"uris": [TEST_TRACK_URI]}


@respx.mock
async def test_duplicate_found_on_first_page_skips_add(api_client: httpx.AsyncClient) -> None:
    mock_token_route(respx)
    respx.get(f"{SPOTIFY_API_BASE}/me/player/currently-playing").mock(
        return_value=httpx.Response(
            200,
            json=make_track_payload(
                name="Get Lucky",
                artists=["Daft Punk", "Pharrell Williams"],
            ),
        )
    )
    respx.get(
        f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items",
        params={"limit": "50", "offset": "0", "fields": PLAYLIST_ITEMS_FIELDS},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 2,
                "next": None,
                "items": [
                    {"item": None},
                    {"item": {"uri": TEST_TRACK_URI}},
                ],
            },
        )
    )
    post_items_route = respx.post(
        f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items"
    ).mock(return_value=httpx.Response(201, json={"snapshot_id": "should_not_be_called"}))

    response = await api_client.post("/add-current", headers={"X-API-Key": TEST_API_KEY})

    assert response.status_code == 200
    assert response.json() == {
        "status": "already_exists",
        "message": "Already in playlist: Get Lucky – Daft Punk, Pharrell Williams",
    }
    assert not post_items_route.called


@respx.mock
async def test_multipage_playlist_duplicate_found_on_later_page(
    api_client: httpx.AsyncClient,
) -> None:
    mock_token_route(respx)
    respx.get(f"{SPOTIFY_API_BASE}/me/player/currently-playing").mock(
        return_value=httpx.Response(
            200,
            json=make_track_payload(name="Time", artists=["Pink Floyd"]),
        )
    )

    page_1 = respx.get(
        f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items",
        params={"limit": "50", "offset": "0", "fields": PLAYLIST_ITEMS_FIELDS},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 130,
                "next": f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items?offset=50&limit=50",
                "items": [{"item": {"uri": f"spotify:track:p1_{i}"}} for i in range(50)],
            },
        )
    )
    page_2 = respx.get(
        f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items",
        params={"limit": "50", "offset": "50", "fields": PLAYLIST_ITEMS_FIELDS},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 130,
                "next": f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items?offset=100&limit=50",
                "items": [{"item": {"uri": f"spotify:track:p2_{i}"}} for i in range(50)],
            },
        )
    )
    page_3 = respx.get(
        f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items",
        params={"limit": "50", "offset": "100", "fields": PLAYLIST_ITEMS_FIELDS},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 130,
                "next": None,
                "items": [
                    {"item": None},
                    {"item": {"uri": TEST_TRACK_URI}},
                ],
            },
        )
    )
    post_items_route = respx.post(
        f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items"
    ).mock(return_value=httpx.Response(201, json={"snapshot_id": "unused"}))

    response = await api_client.post("/add-current", headers={"X-API-Key": TEST_API_KEY})

    assert response.status_code == 200
    assert response.json() == {
        "status": "already_exists",
        "message": "Already in playlist: Time – Pink Floyd",
    }
    assert page_1.call_count == 1
    assert page_2.call_count == 1
    assert page_3.call_count == 1
    assert not post_items_route.called


@respx.mock
async def test_multipage_playlist_concurrency_throttled_by_semaphore_5() -> None:
    """Verify remaining playlist pages run concurrently but never exceed Semaphore(5)."""
    mock_token_route(respx)

    total_items = 600  # 12 pages of 50 -> page 0 first, then 11 concurrent pages
    in_flight = 0
    max_in_flight = 0
    seen_offsets: set[int] = set()

    async def handle_page(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        offset = int(request.url.params["offset"])
        assert request.url.params["limit"] == "50"
        assert request.url.params["fields"] == PLAYLIST_ITEMS_FIELDS
        seen_offsets.add(offset)

        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.02)
            return httpx.Response(
                200,
                json={
                    "total": total_items,
                    "next": None if offset + 50 >= total_items else "next",
                    "items": [{"item": {"uri": f"spotify:track:offset_{offset}"}}],
                },
            )
        finally:
            in_flight -= 1

    respx.get(f"{SPOTIFY_API_BASE}/playlists/{TEST_PLAYLIST_ID}/items").mock(
        side_effect=handle_page
    )

    settings = make_test_settings()
    async with httpx.AsyncClient() as http_client:
        spotify = SpotifyClient(settings=settings, http_client=http_client)
        uris = await spotify.get_playlist_item_uris(TEST_PLAYLIST_ID)

    assert seen_offsets == set(range(0, total_items, 50))
    assert len(uris) == 12
    assert 1 < max_in_flight <= 5


@respx.mock
async def test_refresh_token_rotation_invokes_callback() -> None:
    mock_token_route(
        respx,
        access_token="fresh_access",
        refresh_token="rotated_refresh_token_xyz",
    )
    respx.get(f"{SPOTIFY_API_BASE}/me/player/currently-playing").mock(
        return_value=httpx.Response(204)
    )

    rotated_tokens: list[str] = []

    async def on_rotated(new_token: str) -> None:
        rotated_tokens.append(new_token)

    settings = make_test_settings()
    async with httpx.AsyncClient() as http_client:
        spotify = SpotifyClient(
            settings=settings,
            http_client=http_client,
            on_refresh_token_rotated=on_rotated,
        )
        result = await spotify.get_currently_playing()

    assert result.status == "nothing_playing"
    assert rotated_tokens == ["rotated_refresh_token_xyz"]


@respx.mock
async def test_401_triggers_token_refresh_and_single_retry() -> None:
    token_route = respx.post(SPOTIFY_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "expired_token", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "new_token", "expires_in": 3600}),
        ]
    )
    playback_route = respx.get(f"{SPOTIFY_API_BASE}/me/player/currently-playing").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"status": 401, "message": "Token expired"}}),
            httpx.Response(204),
        ]
    )

    settings = make_test_settings()
    async with httpx.AsyncClient() as http_client:
        spotify = SpotifyClient(settings=settings, http_client=http_client)
        result = await spotify.get_currently_playing()

    assert result.status == "nothing_playing"
    assert token_route.call_count == 2
    assert playback_route.call_count == 2
    assert playback_route.calls[0].request.headers["Authorization"] == "Bearer expired_token"
    assert playback_route.calls[1].request.headers["Authorization"] == "Bearer new_token"


@respx.mock
async def test_upstream_spotify_error_returns_502(api_client: httpx.AsyncClient) -> None:
    mock_token_route(respx)
    respx.get(f"{SPOTIFY_API_BASE}/me/player/currently-playing").mock(
        return_value=httpx.Response(500, text="Internal Spotify Error")
    )

    response = await api_client.post("/add-current", headers={"X-API-Key": TEST_API_KEY})
    assert response.status_code == 502
    assert response.json() == {"detail": "Spotify API request failed."}


def test_load_settings_from_ssm_in_lambda(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "spotify-add-song-fn")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "lambda_client_id")
    monkeypatch.setenv("PLAYLIST_ID", "lambda_playlist_id")
    monkeypatch.setenv("SSM_PREFIX", "/spotify-add-song-button")
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SPOTIFY_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    mock_ssm = MagicMock()
    mock_ssm.get_parameters.return_value = {
        "Parameters": [
            {
                "Name": "/spotify-add-song-button/SPOTIFY_CLIENT_SECRET",
                "Value": "ssm_secret",
            },
            {
                "Name": "/spotify-add-song-button/SPOTIFY_REFRESH_TOKEN",
                "Value": "ssm_refresh",
            },
            {
                "Name": "/spotify-add-song-button/API_KEY",
                "Value": "ssm_api_key",
            },
        ],
        "InvalidParameters": [],
    }

    settings = load_settings(ssm_client=mock_ssm)

    assert settings.ssm_enabled is True
    assert settings.spotify_client_id == "lambda_client_id"
    assert settings.playlist_id == "lambda_playlist_id"
    assert settings.spotify_client_secret == "ssm_secret"
    assert settings.spotify_refresh_token == "ssm_refresh"
    assert settings.api_key == "ssm_api_key"
    mock_ssm.get_parameters.assert_called_once_with(
        Names=[
            "/spotify-add-song-button/SPOTIFY_CLIENT_SECRET",
            "/spotify-add-song-button/SPOTIFY_REFRESH_TOKEN",
            "/spotify-add-song-button/API_KEY",
        ],
        WithDecryption=True,
    )

    persist_refresh_token(settings, "new_rotated_token", ssm_client=mock_ssm)
    assert settings.spotify_refresh_token == "new_rotated_token"
    mock_ssm.put_parameter.assert_called_once_with(
        Name="/spotify-add-song-button/SPOTIFY_REFRESH_TOKEN",
        Value="new_rotated_token",
        Type="SecureString",
        Overwrite=True,
    )


async def test_dependency_override_custom_spotify_client(test_settings: Settings) -> None:
    """Sanity-check that get_spotify_client works cleanly via FastAPI DI."""
    mock_client = MagicMock(spec=SpotifyClient)
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_spotify_client] = lambda: mock_client
    try:
        assert app.dependency_overrides[get_spotify_client]() is mock_client
    finally:
        app.dependency_overrides.clear()
