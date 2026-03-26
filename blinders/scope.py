"""Task scope extraction for Cognitive Blinders.

Analyzes the user directive to determine what the agent should be allowed
to see and do. Runs BEFORE any web content is observed — operates only
on trusted user input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from profiles.loader import Profile

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

# Keyword groups for goal type detection
_READ_KEYWORDS = [
    "find",
    "search",
    "look up",
    "lookup",
    "what is",
    "what are",
    "tell me",
    "summarize",
    "research",
    "check",
    "compare",
    "how much",
    "how many",
    "price of",
    "list of",
    "show me",
    "get info",
    "get information",
    "read",
    "review",
]

_FILL_FORM_KEYWORDS = [
    "fill",
    "fill out",
    "fill in",
    "submit",
    "register",
    "sign up",
    "signup",
    "apply",
    "book",
    "reserve",
    "create account",
    "create an account",
]

_NAVIGATE_KEYWORDS = [
    "go to",
    "navigate to",
    "open",
    "visit",
    "load",
]

_INTERACT_KEYWORDS = [
    "click",
    "type",
    "enter",
    "select",
    "choose",
    "toggle",
    "download",
    "upload",
    "drag",
    "drop",
]

# Patterns for extracting URLs from directives
_URL_PATTERN = re.compile(
    r"https?://[^\s,\"'<>]+|(?:www\.)?[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,}(?:/[^\s,\"'<>]*)?"
)


@dataclass
class ElementVisibility:
    """Controls which DOM element categories pass through the blinders."""

    show_forms: bool = True
    show_nav_links: bool = True
    show_action_buttons: bool = True
    show_account_controls: bool = False
    include_selectors: list[str] = field(default_factory=list)
    exclude_selectors: list[str] = field(default_factory=list)


@dataclass
class TaskScope:
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


def _detect_goal_type(directive: str) -> str:
    """Classify the directive into a goal type based on keywords."""
    lower = directive.lower()

    # Check most specific patterns first
    for kw in _FILL_FORM_KEYWORDS:
        if kw in lower:
            return "fill_form"

    for kw in _INTERACT_KEYWORDS:
        if kw in lower:
            return "interact"

    for kw in _READ_KEYWORDS:
        if kw in lower:
            return "read"

    for kw in _NAVIGATE_KEYWORDS:
        if kw in lower:
            return "navigate"

    # Default: interact (most permissive)
    return "interact"


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
) -> TaskScope:
    """Analyze directive to determine task scope. Pure function, no LLM call.

    The scope defines:
    - What goal type the task is (read, navigate, interact, fill_form)
    - Which domains are in scope (from URLs in the directive)
    - Which actions are available (narrowed by goal type)
    - What DOM elements are visible (adaptive by goal type)
    """
    goal_type = _detect_goal_type(directive)
    allowed_domains = _extract_domains(directive)
    visibility = _default_visibility(goal_type)
    allowed_actions = _default_actions(goal_type)

    # Profile overrides can widen scope
    if profile and profile.guardrail_overrides:
        overrides = profile.guardrail_overrides
        # Research profile disables action blocks → widen visibility
        if overrides.get("blocked_action_categories") == []:
            visibility.show_action_buttons = True
        # Higher URL limits suggest broader navigation needs
        if overrides.get("max_urls_visited", 50) > 50:
            visibility.show_nav_links = True

    return TaskScope(
        goal_type=goal_type,
        allowed_domains=allowed_domains,
        allowed_actions=allowed_actions,
        visibility=visibility,
    )
