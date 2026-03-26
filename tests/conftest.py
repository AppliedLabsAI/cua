"""Shared pytest fixtures."""

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
