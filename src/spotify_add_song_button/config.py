"""Configuration and secrets loading (local .env + AWS SSM Parameter Store)."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

import boto3
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

DEFAULT_SSM_PREFIX = "/spotify-add-song-button"
SSM_SECRET_FIELDS: dict[str, str] = {
    "spotify_client_secret": "SPOTIFY_CLIENT_SECRET",
    "spotify_refresh_token": "SPOTIFY_REFRESH_TOKEN",
    "api_key": "API_KEY",
}


def is_aws_lambda_env() -> bool:
    """Return True when running inside AWS Lambda."""
    return bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


class Settings(BaseSettings):
    """Application settings loaded from .env locally or env + SSM in AWS Lambda."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    spotify_client_id: str
    playlist_id: str
    spotify_client_secret: str = ""
    spotify_refresh_token: str = ""
    api_key: str = ""

    ssm_prefix: str = DEFAULT_SSM_PREFIX
    use_ssm: bool | None = None

    @property
    def ssm_enabled(self) -> bool:
        if self.use_ssm is not None:
            return self.use_ssm
        return is_aws_lambda_env()

    def ssm_parameter_name(self, param_key: str) -> str:
        prefix = self.ssm_prefix.rstrip("/")
        return f"{prefix}/{param_key}"


def _create_ssm_client() -> Any:
    return boto3.client("ssm")


def load_settings(ssm_client: Any | None = None) -> Settings:
    """Load application settings, hydrating secrets from AWS SSM when in Lambda."""
    settings = Settings()  # type: ignore[call-arg]

    if settings.ssm_enabled:
        client = ssm_client or _create_ssm_client()
        param_names = {
            field: settings.ssm_parameter_name(key)
            for field, key in SSM_SECRET_FIELDS.items()
        }
        response = client.get_parameters(
            Names=list(param_names.values()),
            WithDecryption=True,
        )
        invalid = response.get("InvalidParameters") or []
        if invalid:
            raise RuntimeError(
                f"Missing required SSM Parameter Store secrets: {', '.join(invalid)}"
            )

        values_by_name = {
            param["Name"]: param["Value"]
            for param in response.get("Parameters", [])
        }
        for field, param_name in param_names.items():
            value = values_by_name.get(param_name, "").strip()
            if not value:
                raise RuntimeError(f"SSM parameter '{param_name}' is empty or missing.")
            setattr(settings, field, value)
    else:
        missing = [
            key
            for field, key in SSM_SECRET_FIELDS.items()
            if not getattr(settings, field, "").strip()
        ]
        if missing:
            raise RuntimeError(
                f"Missing required configuration values in environment/.env: {', '.join(missing)}"
            )

    return settings


def persist_refresh_token(
    settings: Settings,
    new_refresh_token: str,
    ssm_client: Any | None = None,
) -> None:
    """Persist a rotated Spotify refresh token to memory and SSM (when in AWS)."""
    settings.spotify_refresh_token = new_refresh_token

    if settings.ssm_enabled:
        client = ssm_client or _create_ssm_client()
        param_name = settings.ssm_parameter_name("SPOTIFY_REFRESH_TOKEN")
        client.put_parameter(
            Name=param_name,
            Value=new_refresh_token,
            Type="SecureString",
            Overwrite=True,
        )
        logger.info("Persisted rotated Spotify refresh token to SSM parameter '%s'.", param_name)
    else:
        logger.warning(
            "Spotify returned a rotated refresh_token in local dev mode. "
            "Updated in memory; update SPOTIFY_REFRESH_TOKEN in .env if needed."
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings for the process / warm Lambda container."""
    return load_settings()


def clear_settings_cache() -> None:
    """Clear the cached settings instance (primarily for tests)."""
    get_settings.cache_clear()
