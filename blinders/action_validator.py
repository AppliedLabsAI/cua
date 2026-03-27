"""LLM-based action validation using Pydantic AI.

Validates potentially risky actions before execution by asking Haiku
whether the action is aligned with the task directive and safe to perform.
Only validates risky actions — read-only actions skip validation entirely.

For execute_sequence, validates ALL steps in a SINGLE Haiku call (batched).
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel
from pydantic_ai import Agent

from settings import UTILITY_MODEL
from telemetry import get_tracer
from telemetry.metrics import safety_degraded_total
from telemetry.spans import (
    ATTR_GENAI_INPUT_TOKENS,
    ATTR_GENAI_MODEL,
    ATTR_GENAI_OUTPUT_TOKENS,
    ATTR_GUARD_ALLOWED,
    ATTR_GUARD_REASON,
)

log = logging.getLogger(__name__)

# Actions that are always safe — skip LLM validation
_SAFE_ACTIONS = frozenset(
    {
        "extract",
        "screenshot",
        "scroll",
        "get_dom",
        "wait_for",
    }
)

# Click selectors that are obviously safe — skip Haiku validation.
# Used as a fast-path to avoid LLM overhead for common UI patterns.
_SAFE_SELECTOR_RE = re.compile(
    r"(text=|role=)?(nav|menu|tab|link|filter|sort|search|view|show|open|expand"
    r"|collapse|back|next|prev|page|details|info|settings|edit|close"
    r"|cancel|dismiss|log.?in|sign.?in|submit|save|apply|select|choose"
    r"|row|cell|table|column|header|pagination|arrow|chevron|icon"
    r"|conversation|chat|thread|shop|store|order|status)",
    re.IGNORECASE,
)

# Actions within execute_sequence that don't need individual validation
# (typing and key presses are almost always part of a form-fill workflow)
_SAFE_IN_SEQUENCE = frozenset(
    {
        "key_press",
        *_SAFE_ACTIONS,
    }
)

_VALIDATION_PROMPT = """\
You are a safety validator for a browser automation agent. Decide whether \
the proposed action(s) are SAFE and ALIGNED with the user's task.

User's task: {directive}
Current page: {page_context}

Proposed action: {action_description}

Rules:
- ALLOW actions that help accomplish the user's task, even indirectly
- ALLOW navigation within the same site/domain to find information
- ALLOW searching, filtering, and sorting to locate data
- ALLOW form filling and clicks that are part of the task workflow
- ALLOW clicking into records, detail pages, or sub-pages to find data
- BLOCK actions that delete data, modify accounts, or send messages \
UNLESS the user's task explicitly requires it
- BLOCK navigation to completely unrelated external domains
- When in doubt, ALLOW the action"""


class ValidationResult(BaseModel):
    """Structured response from the action validator."""

    safe: bool = True
    reason: str = ""


_validator_agent = Agent[None, ValidationResult](
    UTILITY_MODEL,
    output_type=ValidationResult,
    model_settings={"max_tokens": 100},
)


class ActionValidator:
    """Validates risky actions using Haiku before execution."""

    def __init__(self, directive: str) -> None:
        self.directive = directive
        self._enabled = True
        self._approved_domains: set[str] = set()  # domains already validated
        self._approved_selectors: set[str] = set()  # click targets already validated

    async def validate(
        self,
        action: str,
        tool_input: dict,
        page_url: str = "",
        page_title: str = "",
    ) -> str | None:
        """Validate an action before execution.

        Returns None if safe, or a reason string if blocked.
        For execute_sequence, validates all steps in ONE call (batched).
        Safe read-only actions skip validation entirely.
        """
        if not self._enabled:
            return None

        # Safe actions skip validation entirely
        if action in _SAFE_ACTIONS:
            return None

        # Click fast-path: skip Haiku for obviously safe selectors and
        # CSS/attribute selectors (programmatic DOM references, not destructive text)
        if action == "click":
            selector = tool_input.get("selector", "")
            normalized = selector.strip().lower()
            if normalized in self._approved_selectors:
                return None
            if _SAFE_SELECTOR_RE.search(normalized):
                self._approved_selectors.add(normalized)
                return None
            if any(c in normalized for c in ".#[>:~+"):
                self._approved_selectors.add(normalized)
                return None

        # Domain caching: skip Haiku for goto to already-approved domains
        if action == "goto":
            domain = _extract_domain(tool_input.get("url", ""))
            if domain and domain in self._approved_domains:
                log.debug("Skipping validation for approved domain: %s", domain)
                return None

        # For execute_sequence, check if any steps actually need validation
        if action == "execute_sequence":
            steps = tool_input.get("steps", [])
            has_risky = any(
                isinstance(s, dict) and s.get("action", "") not in _SAFE_IN_SEQUENCE
                for s in steps
            )
            if not has_risky:
                return None  # All steps are safe (typing + reads)

        # Build action description for the LLM
        action_desc = _describe_action(action, tool_input)

        # Build page context
        page_context = (
            f"{page_title} ({page_url})" if page_title else page_url or "unknown"
        )

        prompt = _VALIDATION_PROMPT.format(
            directive=self.directive,
            page_context=page_context,
            action_description=action_desc,
        )

        tracer = get_tracer()
        with tracer.start_as_current_span(
            "cua.blinders.validate_action",
            attributes={
                ATTR_GENAI_MODEL: UTILITY_MODEL,
                "cua.blinders.action": action_desc,
            },
        ) as span:
            try:
                result = await _validator_agent.run(prompt)
                usage = result.usage()

                span.set_attributes(
                    {
                        ATTR_GENAI_INPUT_TOKENS: usage.input_tokens or 0,
                        ATTR_GENAI_OUTPUT_TOKENS: usage.output_tokens or 0,
                    }
                )

                is_safe = result.output.safe
                reason = result.output.reason

                if not is_safe:
                    log.warning("Action validator blocked %s: %s", action_desc, reason)
                    span.set_attributes(
                        {
                            ATTR_GUARD_ALLOWED: False,
                            ATTR_GUARD_REASON: reason[:500],
                        }
                    )
                    return f"Action validator blocked: {reason}"

                span.set_attributes({ATTR_GUARD_ALLOWED: True})
                log.debug("Action validator approved: %s (%s)", action_desc, reason)
                if action == "goto":
                    domain = _extract_domain(tool_input.get("url", ""))
                    if domain:
                        self._approved_domains.add(domain)
                return None

            except Exception as exc:
                log.warning(
                    "Action validation unavailable, blocking ambiguous action: %s", exc
                )
                safety_degraded_total().add(
                    1, {"component": "action_validator", "fallback": "block"}
                )
                span.set_attributes(
                    {
                        ATTR_GUARD_ALLOWED: False,
                        ATTR_GUARD_REASON: "validation unavailable",
                    }
                )
                return "Action validator blocked: safety validation unavailable"


def _extract_domain(url: str) -> str:
    """Extract domain from a URL for caching."""
    try:
        from urllib.parse import urlparse

        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _describe_action(action: str, tool_input: dict) -> str:
    """Build a human-readable description of the action for the LLM."""
    if action == "goto":
        return f"Navigate to: {tool_input.get('url', 'unknown')}"
    elif action == "click":
        selector = tool_input.get("selector", "unknown")
        return f"Click element: {selector}"
    elif action == "key_press":
        text = tool_input.get("text", "")
        key = tool_input.get("key", "")
        parts = []
        if text:
            preview = text[:20] + "..." if len(text) > 20 else text
            parts.append(f"type '{preview}'")
        if key:
            parts.append(f"press {key}")
        return f"Keyboard: {', '.join(parts)}"
    elif action == "execute_sequence":
        steps = tool_input.get("steps", [])
        step_descs = []
        for s in steps[:5]:
            if isinstance(s, dict):
                step_descs.append(_describe_action(s.get("action", "?"), s))
        desc = "; ".join(step_descs)
        if len(steps) > 5:
            desc += f" ... and {len(steps) - 5} more steps"
        return f"Sequence: [{desc}]"
    else:
        return f"{action}({json.dumps(tool_input, default=str)[:100]})"
