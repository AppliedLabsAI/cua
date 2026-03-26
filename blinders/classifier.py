"""LLM-based scope classification using Haiku.

Primary classification method for Cognitive Blinders. Uses a lightweight
Haiku call to understand directive intent, with keyword matching as
a fast offline fallback when no API key is available.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import Anthropic

log = logging.getLogger(__name__)

_VALID_GOAL_TYPES = frozenset({"read", "navigate", "interact", "fill_form"})

_CLASSIFICATION_PROMPT = """\
Classify this browser automation directive into exactly one goal type.

Goal types:
- "read": Information gathering only. Finding, searching, checking, comparing, \
summarizing content. No typing or form submission needed.
- "navigate": Simply going to a page. No interaction beyond clicking links.
- "interact": Clicking buttons, selecting options, downloading. Requires \
interacting with UI elements but not filling text fields.
- "fill_form": Requires typing text into fields, logging in, submitting forms, \
registering, booking, or any action that needs keyboard input.

IMPORTANT rules:
- If the task requires logging in, signing in, or using credentials -> "fill_form"
- If the task mentions passwords, usernames, email/password fields -> "fill_form"
- If the task is ONLY reading/finding information but requires login first -> \
still "fill_form" (login needs typing)
- If unsure between read and fill_form, prefer "fill_form" (safer)
- If unsure between navigate and interact, prefer "interact" (safer)

Respond with ONLY a JSON object, no markdown:
{"goal_type": "<type>", "needs_login": <true/false>}

Directive: """


def classify_directive(
    directive: str,
    client: Anthropic | None = None,
) -> str:
    """Classify a directive into a goal type using Haiku.

    Returns one of: "read", "navigate", "interact", "fill_form".
    Falls back to "interact" (most permissive) on any error.
    """
    client = client or Anthropic()

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[
                {
                    "role": "user",
                    "content": _CLASSIFICATION_PROMPT + directive,
                }
            ],
        )

        # Extract text from first content block (type-safe)
        block = response.content[0]
        text: str = _extract_text(block)
        text = text.strip()

        # Handle markdown code block wrapping
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        data = json.loads(text)
        goal_type = data.get("goal_type", "interact")

        if goal_type not in _VALID_GOAL_TYPES:
            log.warning(
                "LLM returned unknown goal_type: %s, defaulting to interact",
                goal_type,
            )
            return "interact"

        log.info(
            "LLM classified directive as: %s (needs_login=%s)",
            goal_type,
            data.get("needs_login"),
        )
        return goal_type

    except Exception as e:
        log.warning("LLM classification failed (%s), falling back to interact", e)
        return "interact"


def _extract_text(block: Any) -> str:
    """Extract text from an Anthropic content block, handling union types."""
    if hasattr(block, "text"):
        return str(block.text)
    return ""
