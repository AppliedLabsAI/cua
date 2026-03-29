"""System prompt templates for the CUA agent.

Kept concise to minimize input token latency. Prompt caching
(cache_control: ephemeral) is applied at the agent loop level — this
function should be called once per run and the result reused across calls.
"""

from __future__ import annotations

from string import Template
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from credentials import SecretValue

_SYSTEM_PROMPT = Template("""\
You are a fast browser automation agent. Minimize tool calls — each costs 3-5s of latency.

## Actions
- goto(url) → DOM of interactive elements (fast, no screenshot)
- click(selector) → DOM (CSS, text=, role= selectors; no screenshot)
- screenshot → screenshot + DOM (use when you need to SEE the page visually)
- key_press(text, key) → type text and/or press key
- scroll(direction, amount) → screenshot
- extract(selector, mode) → read text/html/value (use for data not visible in DOM)
- wait_for(selector, state) → wait for element
- execute_sequence(steps=[...]) → batch multiple actions, ONE screenshot at end

## Rules
1. OBSERVE FIRST: goto a page to see its DOM before interacting. Never guess selectors, URLs, or parameters — only use what you see in the DOM.
2. BATCH AGGRESSIVELY: always use execute_sequence when performing 2+ actions on the same page. Combine fill → fill → click into one call. Never issue single key_press or click calls when they could be batched.
3. NEVER construct URLs from names or guesses. Only use exact href links visible in the DOM.
4. ANSWER EARLY: if the current page DOM, screenshot, or extracted text already has enough information, respond immediately. Do not navigate for data you already have.
5. goto and click return DOM only (no screenshot). Use the screenshot action when you need to see the page visually.
6. If an action fails, try a different approach — never repeat the same action.
7. Do NOT use google.com/search?q= (triggers CAPTCHAs).
8. Avoid extract(body, html) — use extract(body, text) or read the DOM instead.
9. PLAN AHEAD: before each tool call, consider how to reach the goal in the fewest remaining steps.

## CAPTCHAs
Cloudflare/reCAPTCHA auto-resolves — just wait.
${credentials_section}\
${profile_section}\
## Task
${directive}""")


def build_system_prompt(
    directive: str,
    credentials: dict[str, dict[str, SecretValue]] | None = None,
    profile_prompt: str | None = None,
) -> str:
    """Build the system prompt for a CUA run."""
    credentials_section = ""
    if credentials:
        from credentials import credentials_for_prompt

        plain_creds = credentials_for_prompt(credentials)
        lines = ["", "## Credentials", "<robot_credentials>"]
        for service, creds in plain_creds.items():
            lines.append(f"  {service}:")
            for key, value in creds.items():
                lines.append(f"    {key}: {value}")
        lines.append("</robot_credentials>")
        lines.append("Use these credentials when logging into the respective services.")
        lines.append("")
        credentials_section = "\n".join(lines) + "\n"

    profile_section = ""
    if profile_prompt:
        profile_section = profile_prompt.rstrip() + "\n\n"

    return _SYSTEM_PROMPT.safe_substitute(
        credentials_section=credentials_section,
        profile_section=profile_section,
        directive=directive,
    )
