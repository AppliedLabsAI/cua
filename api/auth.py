"""Authentication helpers for the API layer."""

from __future__ import annotations

import secrets

from fastapi.security import HTTPAuthorizationCredentials

from api.errors import ApiErrorCode, raise_api_error
from settings import get_settings


def auth_settings() -> tuple[str, str]:
    """Return current auth settings from the runtime environment."""
    settings = get_settings()
    return settings.environment, settings.cua_api_key or ""


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    """Verify Bearer token. Auth is required unless environment=local."""
    environment, api_key = auth_settings()
    if not api_key:
        if environment == "local":
            return  # No auth configured — local dev mode
        raise_api_error(
            503,
            ApiErrorCode.SERVER_MISCONFIGURED,
            "Server misconfigured: CUA_API_KEY is required in production. "
            "Set environment=local to disable auth for local development.",
        )
    if credentials is None or not secrets.compare_digest(
        credentials.credentials, api_key
    ):
        raise_api_error(
            401,
            ApiErrorCode.UNAUTHORIZED,
            "Invalid or missing API key",
        )
