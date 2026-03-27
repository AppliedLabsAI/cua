"""Task scope extraction for Cognitive Blinders.

Analyzes the user directive to determine what the agent should be allowed
to see and do. Runs BEFORE any web content is observed — operates only
on trusted user input.

Classification uses a Haiku LLM call for accuracy, with a lightweight
keyword fallback for offline/test environments.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from telemetry.metrics import safety_degraded_total

if TYPE_CHECKING:
    from profiles.loader import Profile

log = logging.getLogger(__name__)

# All available browser_dom actions
ALL_ACTIONS = frozenset(
    {
        "goto",
        "click",
        "screenshot",
        "key_press",
        "scroll",
        "extract",
        "get_dom",
        "wait_for",
        "execute_sequence",
    }
)

# Patterns for extracting URLs from directives
_URL_PATTERN = re.compile(
    r"https?://[^\s,\"'<>]+|(?:www\.)?[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,}(?:/[^\s,\"'<>]*)?"
)

# Minimal keyword sets used ONLY as offline fallback when no API key is available.
# The LLM classifier is the primary classification method.
_FALLBACK_FILL_FORM_RE = re.compile(
    r"fill\b|submit|register|sign.?up|log.?in|sign.?in|book\b|reserve|create.+account",
    re.IGNORECASE,
)
_FALLBACK_INTERACT_RE = re.compile(
    r"click|select\b|choose|download|upload|toggle|drag\b|drop\b",
    re.IGNORECASE,
)
_FALLBACK_READ_RE = re.compile(
    r"find\b|search|tell me|summarize|research|check\b|compare|price|show me|read\b|review|what|how",
    re.IGNORECASE,
)
_FALLBACK_NAVIGATE_RE = re.compile(
    r"go to|navigate to|open\b|visit\b|load\b",
    re.IGNORECASE,
)


class ElementVisibility(BaseModel):
    """Controls which DOM element categories pass through the blinders."""

    show_forms: bool = True
    show_nav_links: bool = True
    show_action_buttons: bool = True
    show_account_controls: bool = False
    include_selectors: list[str] = Field(default_factory=list)
    exclude_selectors: list[str] = Field(default_factory=list)


class TaskScope(BaseModel):
    """Defines the agent's observation and action boundaries for a run."""

    goal_type: str  # "read" | "navigate" | "interact" | "fill_form"
    allowed_domains: list[str]  # glob patterns
    allowed_actions: frozenset[str]
    visibility: ElementVisibility
    max_steps_override: int | None = None


def _extract_domains(directive: str) -> list[str]:
    """Extract domain patterns from URLs found in the directive.

    Returns glob patterns like ["*.example.com", "example.com"].
    If no domains found, returns empty list (permissive — any domain allowed).
    """
    domains: list[str] = []
    seen: set[str] = set()

    for match in _URL_PATTERN.finditer(directive):
        url = match.group()
        if not url.startswith("http"):
            url = "https://" + url
        try:
            parsed = urlparse(url)
            domain = (parsed.hostname or "").lower()
        except Exception:
            continue
        if not domain or domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
        # Also allow subdomains
        if not domain.startswith("*."):
            domains.append(f"*.{domain}")

    return domains


def _detect_goal_type(directive: str, *, use_llm: bool = True) -> str:
    """Classify the directive into a goal type.

    Primary: LLM-based classification via Haiku (accurate, handles edge cases).
    Fallback: Minimal keyword matching (fast, for offline/test environments).

    Args:
        use_llm: If True (default), attempts Haiku LLM classification.
                 Falls back to keyword matching if the LLM call fails.
                 Set False to skip LLM entirely.
    """
    if use_llm:
        try:
            from blinders.classifier import classify_directive

            return classify_directive(directive)
        except Exception as exc:
            log.warning("LLM classification unavailable, using degraded scope: %s", exc)
            safety_degraded_total().add(
                1, {"component": "scope_classifier", "fallback": "keyword_read"}
            )

    return _detect_goal_type_fallback(directive)


def _detect_goal_type_fallback(directive: str) -> str:
    """Offline fallback: classify directive using minimal keyword regex.

    Used only when no API key is available (tests, offline mode).
    Intentionally simple — the LLM classifier handles nuance.
    """
    if _FALLBACK_FILL_FORM_RE.search(directive):
        return "fill_form"
    if _FALLBACK_INTERACT_RE.search(directive):
        return "interact"
    if _FALLBACK_READ_RE.search(directive):
        return "read"
    if _FALLBACK_NAVIGATE_RE.search(directive):
        return "navigate"
    # Default to read in degraded mode — safer than widening permissions.
    return "read"


def _default_visibility(goal_type: str) -> ElementVisibility:
    """Return adaptive visibility defaults based on goal type.

    Stricter for read-only tasks, more permissive for interactive ones.
    """
    if goal_type in ("read", "navigate"):
        return ElementVisibility(
            show_forms=False,
            show_nav_links=True,
            show_action_buttons=False,
            show_account_controls=False,
        )
    elif goal_type == "fill_form":
        return ElementVisibility(
            show_forms=True,
            show_nav_links=True,
            show_action_buttons=True,
            show_account_controls=True,
        )
    else:  # "interact"
        return ElementVisibility(
            show_forms=True,
            show_nav_links=True,
            show_action_buttons=True,
            show_account_controls=False,
        )


def _default_actions(goal_type: str) -> frozenset[str]:
    """Return allowed actions based on goal type."""
    if goal_type == "read":
        return frozenset(
            {
                "goto",
                "click",
                "extract",
                "screenshot",
                "scroll",
                "get_dom",
                "wait_for",
            }
        )
    elif goal_type == "navigate":
        return frozenset(
            {
                "goto",
                "click",
                "screenshot",
                "scroll",
                "get_dom",
            }
        )
    else:  # "interact" or "fill_form"
        return ALL_ACTIONS


def extract_task_scope(
    directive: str,
    profile: Profile | None = None,
    *,
    use_llm: bool = True,
) -> TaskScope:
    """Analyze directive to determine task scope.

    Uses Haiku LLM for accurate classification by default.
    Falls back to keyword matching if the LLM call fails.

    The scope defines:
    - What goal type the task is (read, navigate, interact, fill_form)
    - Which domains are in scope (from URLs in the directive)
    - Which actions are available (narrowed by goal type)
    - What DOM elements are visible (adaptive by goal type)
    """
    goal_type = _detect_goal_type(directive, use_llm=use_llm)
    allowed_domains = _extract_domains(directive)
    visibility = _default_visibility(goal_type)
    allowed_actions = _default_actions(goal_type)

    # Profile overrides can widen scope
    if profile and profile.guardrail_overrides:
        overrides = profile.guardrail_overrides
        updates: dict[str, bool] = {}
        # Research profile disables LLM action check → widen visibility
        if overrides.get("enable_llm_action_check") is False:
            updates["show_action_buttons"] = True
        # Higher URL limits suggest broader navigation needs
        if overrides.get("max_urls_visited", 50) > 50:
            updates["show_nav_links"] = True
        if updates:
            visibility = visibility.model_copy(update=updates)

    return TaskScope(
        goal_type=goal_type,
        allowed_domains=allowed_domains,
        allowed_actions=allowed_actions,
        visibility=visibility,
    )
