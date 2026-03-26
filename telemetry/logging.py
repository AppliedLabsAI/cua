"""Compact OTel-aware log formatter for console output.

Embeds the current span name and trace context into log lines so console
output reads like a simplified trace waterfall::

    12:34:56 INFO  [cua.agent.iteration] API call: 450ms, 2 tool calls, tokens: 1200 in
    12:34:56 INFO  [cua.tool.execute]    Step 3: browser_dom.click (150ms) OK
    12:34:57 WARN  [cua.agent.iteration] Streaming failed (timeout), falling back

When no span is active, falls back to the logger name::

    12:34:55 INFO  [cua.agent] Starting CUA agent: model=claude-sonnet-4-6
"""

from __future__ import annotations

import logging
import sys

from opentelemetry import trace as otel_trace

# ANSI color codes for level-based coloring
_COLORS = {
    "DEBUG": "\033[90m",  # gray
    "INFO": "\033[36m",  # cyan
    "WARNING": "\033[33m",  # yellow
    "ERROR": "\033[31m",  # red
    "CRITICAL": "\033[1;31m",  # bold red
}
_RESET = "\033[0m"
_DIM = "\033[90m"

# Short level names for compact output
_SHORT_LEVELS = {
    "DEBUG": "DEBUG",
    "INFO": "INFO ",
    "WARNING": "WARN ",
    "ERROR": "ERROR",
    "CRITICAL": "CRIT ",
}


class SpanFormatter(logging.Formatter):
    """Compact formatter that includes the current OTel span name."""

    def __init__(self, *, color: bool | None = None) -> None:
        super().__init__()
        self._color = color if color is not None else _is_tty()

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        level = _SHORT_LEVELS.get(record.levelname, record.levelname.ljust(5))
        context = _current_span_name() or record.name
        message = record.getMessage()

        if self._color:
            color = _COLORS.get(record.levelname, "")
            line = (
                f"{_DIM}{ts}{_RESET} "
                f"{color}{level}{_RESET} "
                f"{_DIM}[{context}]{_RESET} "
                f"{message}"
            )
        else:
            line = f"{ts} {level} [{context}] {message}"

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line = f"{line}\n{record.exc_text}"

        return line


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger with the compact span-aware formatter.

    Call once at process startup (before any log output).
    Adds our SpanFormatter handler if one isn't already present,
    without removing handlers from other libraries.
    """
    root = logging.getLogger()

    # Only add our handler if not already present
    already_installed = any(
        isinstance(getattr(h, "formatter", None), SpanFormatter) for h in root.handlers
    )
    if not already_installed:
        # Remove default handlers (basicConfig) but not library-added ones
        root.handlers = [
            h
            for h in root.handlers
            if not (isinstance(h, logging.StreamHandler) and h.stream is sys.stderr)
        ]
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(SpanFormatter())
        root.addHandler(handler)

    root.setLevel(level)


def _current_span_name() -> str:
    """Return the name of the current active span, or empty string."""
    span = otel_trace.get_current_span()
    if span and span.is_recording():
        return getattr(span, "name", "")
    return ""


def _is_tty() -> bool:
    """Check if stderr is a terminal (for color support)."""
    try:
        return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
    except Exception:
        return False
