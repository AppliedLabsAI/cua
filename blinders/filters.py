"""DOM Blinders — Python-side observation filtering.

Applies task-scoped filtering to DOM snapshots returned by the browser,
marks content provenance, and detects potential prompt injection patterns.
Works in concert with the JS-side __shouldShow filter in dom_snapshot.js.
"""

from __future__ import annotations

import re

from blinders.scope import TaskScope

# Single combined regex for injection detection — one search per line instead of 9.
# Patterns that suggest prompt injection attempts in web content.
_INJECTION_RE = re.compile(
    r"ignore\s+(?:previous|above|all|prior)\s+(?:instructions?|prompts?|rules?)"
    r"|you\s+are\s+(?:now|a|an)\s+"
    r"|system\s*prompt"
    r"|new\s+instructions?\s*:"
    r"|</?system"
    r"|IMPORTANT\s*:.*override"
    r"|disregard\s+(?:all|any|previous)"
    r"|forget\s+(?:everything|all|previous)"
    r"|\[INST\]|\[/INST\]|<\|im_start\|>|<\|im_end\|>",
    re.IGNORECASE,
)

# Default dangerous action text patterns (used when action buttons are hidden)
_DANGEROUS_TEXT_PATTERNS = [
    "delete account",
    "close account",
    "deactivate account",
    "remove account",
    "delete my",
    "terminate",
    "place order",
    "submit order",
    "complete purchase",
    "pay now",
    "purchase now",
    "buy now",
    "send email",
    "send message",
    "publish post",
]

_PROVENANCE_START = "[web-content-start]"
_PROVENANCE_END = "[web-content-end]"


class DOMBlinders:
    """Applies task-scoped filtering to DOM snapshots."""

    def __init__(self, scope: TaskScope) -> None:
        self.scope = scope
        # Pre-compute JS filter config (scope is immutable after creation)
        vis = scope.visibility
        self._js_config = {
            "showForms": vis.show_forms,
            "showNavLinks": vis.show_nav_links,
            "showActionButtons": vis.show_action_buttons,
            "showAccountControls": vis.show_account_controls,
            "excludeSelectors": vis.exclude_selectors,
            "includeSelectors": vis.include_selectors,
            "excludeTextPatterns": _get_dangerous_text_patterns(scope),
        }

    def to_js_filter_config(self) -> dict:
        """Return pre-computed JS filterConfig for window.__domSnapshot()."""
        return self._js_config

    def filter_snapshot(self, dom_text: str) -> str:
        """Apply Python-side filtering to a DOM snapshot string.

        Handles filtering that can't be done in JS:
        - Injection pattern detection and redaction
        - Content provenance marking
        """
        if not dom_text:
            return dom_text

        lines = dom_text.split("\n")
        filtered: list[str] = []

        for line in lines:
            # Check for injection patterns
            if _contains_injection(line):
                filtered.append("[content redacted — suspicious pattern]")
                continue
            filtered.append(line)

        result = "\n".join(filtered)
        return self.mark_provenance(result)

    def mark_provenance(self, dom_text: str) -> str:
        """Wrap web-sourced content with provenance markers.

        These markers help the model distinguish between trusted system
        instructions and untrusted web content.
        """
        return f"{_PROVENANCE_START}\n{dom_text}\n{_PROVENANCE_END}"


def _contains_injection(text: str) -> bool:
    """Check if a line of text contains potential injection patterns."""
    return _INJECTION_RE.search(text) is not None


def _get_dangerous_text_patterns(scope: TaskScope) -> list[str]:
    """Return text patterns to exclude based on task scope.

    For read/navigate tasks, all dangerous patterns are excluded.
    For interact/fill_form, only account deletion patterns are excluded.
    """
    if scope.goal_type in ("read", "navigate"):
        return list(_DANGEROUS_TEXT_PATTERNS)
    elif scope.goal_type == "interact":
        # Only block account-destructive patterns
        return [
            p for p in _DANGEROUS_TEXT_PATTERNS if "account" in p or "terminate" in p
        ]
    else:  # fill_form — most permissive
        return [
            p
            for p in _DANGEROUS_TEXT_PATTERNS
            if "delete account" in p or "close account" in p
        ]
