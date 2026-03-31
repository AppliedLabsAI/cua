"""Tests for API auth behavior in api.server."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from api.auth import verify_api_key
from api.errors import ApiErrorCode


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class TestVerifyApiKey:
    @pytest.mark.asyncio
    async def test_allows_local_without_api_key(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "local")
        monkeypatch.delenv("CUA_API_KEY", raising=False)

        await verify_api_key(None)

    @pytest.mark.asyncio
    async def test_rejects_production_without_api_key(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("CUA_API_KEY", raising=False)

        with pytest.raises(HTTPException) as exc:
            await verify_api_key(None)

        assert exc.value.status_code == 503
        assert exc.value.detail["error"]["code"] == ApiErrorCode.SERVER_MISCONFIGURED
        assert "CUA_API_KEY is required" in exc.value.detail["error"]["message"]

    @pytest.mark.asyncio
    async def test_rejects_missing_credentials_when_api_key_set(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("CUA_API_KEY", "secret-token")

        with pytest.raises(HTTPException) as exc:
            await verify_api_key(None)

        assert exc.value.status_code == 401
        assert exc.value.detail["error"]["code"] == ApiErrorCode.UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_rejects_wrong_credentials(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("CUA_API_KEY", "secret-token")

        with pytest.raises(HTTPException) as exc:
            await verify_api_key(_creds("wrong-token"))

        assert exc.value.status_code == 401
        assert exc.value.detail["error"]["code"] == ApiErrorCode.UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_allows_matching_credentials(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("CUA_API_KEY", "secret-token")

        await verify_api_key(_creds("secret-token"))
