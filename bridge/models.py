"""Data models for the bridge layer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ActionResult:
    """Result of executing a bridge action."""

    screenshot_b64: str | None = None
    text: str | None = None
    error: str | None = None


# Sentinel used to attach/detect DOM snapshots in action results.
DOM_MARKER = "--- Page DOM ---"
