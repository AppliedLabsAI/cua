"""Tests for browser fingerprint generation and cache validation."""

from __future__ import annotations

import json
from types import SimpleNamespace

from bridge import fingerprint


def _fake_browserforge_fingerprint(
    *,
    renderer: str,
    screen_width: int = 1600,
    screen_height: int = 1200,
):
    navigator = SimpleNamespace(
        userAgent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        appVersion="5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        platform="Linux x86_64",
        language="en-US",
        languages=["en-US", "en"],
        hardwareConcurrency=8,
        deviceMemory=8,
        maxTouchPoints=0,
        vendor="Google Inc.",
        userAgentData=None,
        extraProperties={},
    )
    screen = SimpleNamespace(
        width=screen_width,
        height=screen_height,
        availWidth=screen_width,
        availHeight=screen_height,
        colorDepth=24,
        pixelDepth=24,
        devicePixelRatio=1,
        outerWidth=screen_width,
        outerHeight=screen_height,
    )
    video_card = SimpleNamespace(vendor="Intel Inc.", renderer=renderer)
    return SimpleNamespace(
        navigator=navigator,
        screen=screen,
        videoCard=video_card,
        audioCodecs={},
        videoCodecs={},
        pluginsData={},
    )


def test_generate_fresh_retries_platform_mismatched_renderer(monkeypatch):
    class _FakeGenerator:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return _fake_browserforge_fingerprint(
                    renderer="Intel Iris OpenGL Engine"
                )
            return _fake_browserforge_fingerprint(
                renderer="ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)"
            )

    fake_generator = _FakeGenerator()
    monkeypatch.setattr(fingerprint, "_generator", fake_generator)

    result = fingerprint._generate_fresh(1280, 720)

    assert fake_generator.calls == 2
    assert result["screen"]["width"] == 1280
    assert result["screen"]["height"] == 720
    assert result["screen"]["availWidth"] == 1280
    assert result["screen"]["outerWidth"] == 1280
    assert "OpenGL Engine" not in result["webgl"]["renderer"]


def test_generate_fingerprint_discards_stale_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "example_cached.json"
    cache_path.write_text(
        json.dumps(
            {
                "userAgent": "Mozilla/5.0 (X11; Linux x86_64)",
                "platform": "Linux x86_64",
                "screen": {"width": 1600, "height": 1200},
                "webgl": {"renderer": "Intel Iris OpenGL Engine"},
            }
        )
    )

    fresh = {
        "userAgent": "Mozilla/5.0 (X11; Linux x86_64)",
        "platform": "Linux x86_64",
        "screen": {"width": 1280, "height": 720},
        "webgl": {
            "renderer": "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)"
        },
    }

    monkeypatch.setattr(fingerprint, "_cache_path_for_url", lambda _url: cache_path)
    monkeypatch.setattr(fingerprint, "_generate_fresh", lambda _w, _h: fresh)

    result = fingerprint.generate_fingerprint(
        1280,
        720,
        start_url="https://example.com",
    )

    assert result == fresh
    assert json.loads(cache_path.read_text()) == fresh
