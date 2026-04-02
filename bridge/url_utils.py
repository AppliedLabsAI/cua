"""Shared URL utilities for the bridge and guardrails layers."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


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


def extract_visited_urls(
    action: str,
    tool_input: dict,
    *,
    page_url_before: str = "",
    page_url_after: str = "",
) -> list[str]:
    """Infer URLs actually visited by an action.

    Prefers the final browser URL so click-driven navigations and redirects are
    reflected accurately. Falls back to declared goto targets when needed.
    """
    goto_urls = extract_goto_urls(action, tool_input)
    visited: list[str] = []

    if goto_urls:
        visited.extend(goto_urls)
        if page_url_after:
            visited[-1] = page_url_after
    elif page_url_after and page_url_after != page_url_before:
        visited.append(page_url_after)

    deduped: list[str] = []
    seen: set[str] = set()
    for url in visited:
        key = url.strip()
        if not key or key in seen:
            continue
        deduped.append(key)
        seen.add(key)
    return deduped


def _normalize_netloc(parsed) -> str:
    """Lowercase the hostname while preserving userinfo and port."""
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return parsed.netloc

    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"

    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{userinfo}{hostname}{port}"


def normalize_url(url: str) -> str:
    """Normalize a URL for comparison.

    Strips trailing slashes, lowercases, and removes fragments so that
    ``https://Example.com/path/`` and ``https://example.com/path`` match.
    """
    url = url.strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        normalized = urlunparse(
            (
                parsed.scheme.lower(),
                _normalize_netloc(parsed),
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
        query = urlencode(parse_qsl(parsed.query, keep_blank_values=True))
        display = f"{domain}{path}" if path and path != "/" else domain
        if query:
            display = f"{display}?{query}"
        return display
    except Exception:
        return url[:80]
