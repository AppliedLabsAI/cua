"""Tool definitions sent to Claude's API.

Single tool: browser_dom (custom JSON Schema) for all browser interactions
via Patchright. No screenshot-based computer tool — DOM-based execution is
sufficient and faster.
"""

from __future__ import annotations

import copy

_STATIC_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "browser_dom",
        "description": (
            "Browser: CSS/text=/role= selectors. "
            "goto/click return DOM, screenshot returns screenshot+DOM."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "goto",
                        "click",
                        "screenshot",
                        "key_press",
                        "scroll",
                        "extract",
                        "get_dom",
                        "wait_for",
                        "execute_sequence",
                    ],
                },
                "selector": {"type": "string"},
                "text": {"type": "string", "description": "key_press: text to type."},
                "key": {
                    "type": "string",
                    "description": "key_press: key (Enter, Tab, etc).",
                },
                "url": {"type": "string"},
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                },
                "amount": {"type": "integer"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "selector": {"type": "string"},
                            "text": {"type": "string"},
                            "key": {"type": "string"},
                            "url": {"type": "string"},
                            "direction": {"type": "string"},
                            "amount": {"type": "integer"},
                            "mode": {"type": "string"},
                            "state": {"type": "string"},
                        },
                        "required": ["action"],
                    },
                    "description": "execute_sequence: batched actions array.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["text", "html", "value"],
                },
                "state": {
                    "type": "string",
                    "enum": ["visible", "hidden", "attached", "detached"],
                },
                "dom_only": {
                    "type": "boolean",
                    "description": (
                        "Skip screenshot, return DOM only. Saves tokens when visual check unneeded."
                    ),
                },
            },
            "required": ["action"],
        },
    },
]


def get_tools(allowed_actions: frozenset[str] | None = None) -> list[dict]:
    """Return tool definitions for the browser_dom agent API call.

    When allowed_actions is provided (from Cognitive Blinders TaskScope),
    the action enum is restricted to only those actions — the model
    literally doesn't know about actions it can't use.

    Returns a deep copy to prevent callers from mutating shared state.
    Cache control is pre-applied to the last tool definition.
    """
    tools = copy.deepcopy(_STATIC_TOOLS)
    if allowed_actions:
        schema = tools[0]["input_schema"]
        schema["properties"]["action"]["enum"] = sorted(allowed_actions)
    tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools
