"""LLM-based action validation using Haiku.

Validates potentially risky actions before execution by asking Haiku
whether the action is aligned with the task directive and safe to perform.
Only validates risky actions — read-only actions skip validation entirely.

For execute_sequence, validates ALL steps in a SINGLE Haiku call (batched).
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

# Actions that are always safe — skip LLM validation
_SAFE_ACTIONS = frozenset({
    "extract",
    "screenshot",
    "scroll",
    "get_dom",
    "wait_for",
})

# Actions within execute_sequence that don't need individual validation
# (typing and key presses are almost always part of a form-fill workflow)
_SAFE_IN_SEQUENCE = frozenset({
    "key_press",
    *_SAFE_ACTIONS,
})

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
- When in doubt, ALLOW the action

Respond with ONLY a JSON object:
{{"safe": true, "reason": "brief reason"}} or {{"safe": false, "reason": "brief reason"}}"""


class ActionValidator:
    """Validates risky actions using Haiku before execution."""

    def __init__(self, directive: str) -> None:
        self.directive = directive
        self._enabled = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self._client = None

    def _get_client(self):
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic()
        return self._client

    def validate(
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
        page_context = f"{page_title} ({page_url})" if page_title else page_url or "unknown"

        prompt = _VALIDATION_PROMPT.format(
            directive=self.directive,
            page_context=page_context,
            action_description=action_desc,
        )

        try:
            response = self._get_client().messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )

            block = response.content[0]
            text: str = str(block.text) if hasattr(block, "text") else ""
            text = text.strip()

            # Handle markdown wrapping
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            data = json.loads(text)
            is_safe = data.get("safe", True)
            reason = data.get("reason", "")

            if not is_safe:
                log.warning("Action validator blocked %s: %s", action_desc, reason)
                return f"Action validator blocked: {reason}"

            log.debug("Action validator approved: %s (%s)", action_desc, reason)
            return None

        except Exception as e:
            # On any error, allow the action (fail open)
            log.debug("Action validation skipped (%s), allowing action", e)
            return None


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
