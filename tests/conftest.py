"""Shared pytest fixtures."""

from unittest.mock import AsyncMock, patch

import pytest

from settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Clear the cached Settings singleton between tests.

    Without this, monkeypatch.setenv changes won't be picked up
    because get_settings() is decorated with @lru_cache.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _skip_llm_classification():
    """Patch LLM classifier to force keyword fallback in all tests.

    Prevents real LLM API calls during testing. The keyword fallback
    in _detect_goal_type produces deterministic results for test assertions.
    """
    with patch(
        "blinders.classifier.classify_directive",
        side_effect=RuntimeError("LLM disabled in tests"),
    ):
        yield


@pytest.fixture(autouse=True)
def _block_live_llm_calls():
    """Fail fast if a test accidentally triggers a real PydanticAI model call."""
    with patch(
        "pydantic_ai.Agent.run",
        new=AsyncMock(side_effect=RuntimeError("Live LLM calls disabled in tests")),
    ):
        yield
