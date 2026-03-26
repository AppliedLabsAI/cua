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

from settings import SAFETY_MODEL
from telemetry import get_tracer
from telemetry.spans import (
    ATTR_GENAI_INPUT_TOKENS,
    ATTR_GENAI_MODEL,
    ATTR_GENAI_OUTPUT_TOKENS,
    ATTR_GENAI_SYSTEM,
)

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
    Raises on API/auth errors so the caller can fall back to keyword matching.
    """
    client = client or Anthropic()
    tracer = get_tracer()

    with tracer.start_as_current_span(
        "cua.blinders.classify",
        attributes={
            ATTR_GENAI_SYSTEM: "anthropic",
            ATTR_GENAI_MODEL: SAFETY_MODEL,
            "cua.blinders.directive": directive[:200],
        },
    ) as span:
        response = client.messages.create(
            model=SAFETY_MODEL,
            max_tokens=100,
            messages=[
                {
                    "role": "user",
                    "content": _CLASSIFICATION_PROMPT + directive,
                }
            ],
        )

        span.set_attributes(
            {
                ATTR_GENAI_INPUT_TOKENS: response.usage.input_tokens,
                ATTR_GENAI_OUTPUT_TOKENS: response.usage.output_tokens,
            }
        )

        block = response.content[0]
        text: str = _extract_text(block)
        text = text.strip()

        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        data = json.loads(text)
        goal_type = data.get("goal_type", "interact")

        if goal_type not in _VALID_GOAL_TYPES:
            log.warning(
                "LLM returned unknown goal_type: %s, defaulting to interact",
                goal_type,
            )
            span.set_attributes({"cua.blinders.goal_type": "interact"})
            return "interact"

        span.set_attributes(
            {
                "cua.blinders.goal_type": goal_type,
                "cua.blinders.needs_login": data.get("needs_login", False),
            }
        )
        log.info(
            "Classified directive as: %s (needs_login=%s)",
            goal_type,
            data.get("needs_login"),
        )
        return goal_type


def _extract_text(block: Any) -> str:
    """Extract text from an Anthropic content block, handling union types."""
    if hasattr(block, "text"):
        return str(block.text)
    return ""
