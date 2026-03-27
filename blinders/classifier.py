"""LLM-based scope classification using Pydantic AI.

Primary classification method for Cognitive Blinders. Uses a lightweight
Haiku call to understand directive intent, with keyword matching as
a fast offline fallback when no API key is available.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from pydantic_ai import Agent

from settings import UTILITY_MODEL
from telemetry import get_tracer
from telemetry.spans import (
    ATTR_GENAI_INPUT_TOKENS,
    ATTR_GENAI_MODEL,
    ATTR_GENAI_OUTPUT_TOKENS,
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
- If unsure between navigate and interact, prefer "interact" (safer)"""


class ClassificationResult(BaseModel):
    """Structured response from the directive classifier."""

    goal_type: str
    needs_login: bool = False


_classifier = Agent[None, ClassificationResult](
    UTILITY_MODEL,
    output_type=ClassificationResult,
    instructions=_CLASSIFICATION_PROMPT,
    model_settings={"max_tokens": 100},
)


async def classify_directive(directive: str) -> str:
    """Classify a directive into a goal type using Haiku.

    Returns one of: "read", "navigate", "interact", "fill_form".
    Raises on API/auth errors so the caller can fall back to keyword matching.
    """
    tracer = get_tracer()

    with tracer.start_as_current_span(
        "cua.blinders.classify",
        attributes={
            ATTR_GENAI_MODEL: UTILITY_MODEL,
            "cua.blinders.directive": directive,
        },
    ) as span:
        result = await _classifier.run(directive)
        usage = result.usage()

        span.set_attributes(
            {
                ATTR_GENAI_INPUT_TOKENS: usage.input_tokens or 0,
                ATTR_GENAI_OUTPUT_TOKENS: usage.output_tokens or 0,
            }
        )

        goal_type = result.output.goal_type

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
                "cua.blinders.needs_login": result.output.needs_login,
            }
        )
        log.info(
            "Classified directive as: %s (needs_login=%s)",
            goal_type,
            result.output.needs_login,
        )
        return goal_type
