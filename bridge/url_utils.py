"""Shared URL utilities for the bridge and guardrails layers."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def extract_goto_urls(action: str, tool_input: dict) -> list[str]:
    """Extract all goto URLs from a top-level action or execute_sequence."""
    urls: list[str] = []
    if action == "goto":
        url = tool_input.get("url", "")
        if url:
            urls.append(url)
    elif action == "execute_sequence":
        for step in tool_input.get("steps", []):
            if isinstance(step, dict) and step.get("action") == "goto":
                url = step.get("url", "")
                if url:
                    urls.append(url)
    return urls


def normalize_url(url: str) -> str:
    """Normalize a URL for comparison.

    Strips trailing slashes, lowercases, and removes fragments so that
    ``https://Example.com/path/`` and ``https://example.com/path`` match.
    """
    url = url.strip().lower()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        normalized = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path.rstrip("/") or "/",
                parsed.params,
                parsed.query,
                "",
            )
        )
        return normalized
    except Exception:
        return url


def compact_url(url: str) -> str:
    """Shorten a URL to domain + path for display."""
    try:
        parsed = urlparse(url)
        domain = parsed.hostname or ""
        path = parsed.path.rstrip("/")
        if path and path != "/":
            return f"{domain}{path}"
        return domain
    except Exception:
        return url[:80]
