"""CLI entry point for the CUA agent — runs inside the Modal sandbox.

Reads configuration from environment variables (set by the entrypoint.sh),
initializes the bridge and browser, runs the agent loop, and reports results
to the in-sandbox status API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("cua.agent")


async def main() -> int:
    # Import here to avoid import errors when checking syntax outside sandbox
    from agent.loop import run_agent
    from api.streaming import complete_run, init_status, push_action
    from bridge.browser import BrowserManager
    from bridge.router import ActionRouter
    from guardrails import GuardrailConfig
    from profiles.loader import apply_guardrail_overrides, load_profile

    # Read config from environment
    directive = os.environ.get("DIRECTIVE", "")
    if not directive:
        log.error("DIRECTIVE env var is required")
        return 1

    model = os.environ.get("MODEL", "claude-sonnet-4-6")
    max_steps = int(os.environ.get("MAX_STEPS", "50"))
    thinking_budget = int(os.environ.get("THINKING_BUDGET", "4096"))
    width = int(os.environ.get("WIDTH", "1920"))
    height = int(os.environ.get("HEIGHT", "1080"))
    start_url = os.environ.get("START_URL") or None
    proxy_url = os.environ.get("PROXY_URL") or None

    credentials = None
    creds_json = os.environ.get("CREDENTIALS_JSON")
    if creds_json:
        try:
            credentials = json.loads(creds_json)
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("Invalid CREDENTIALS_JSON env var: %s", exc)
            return 1

    guardrail_config = None
    guardrails_json = os.environ.get("GUARDRAILS_JSON")
    if guardrails_json:
        try:
            guardrail_config = GuardrailConfig.from_dict(json.loads(guardrails_json))
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("Invalid GUARDRAILS_JSON env var: %s", exc)
            return 1

    # Load profile
    profile_name = os.environ.get("PROFILE", "default")
    try:
        profile = load_profile(profile_name)
        guardrail_config = apply_guardrail_overrides(profile, guardrail_config)
        log.info("Loaded profile: %s", profile_name)
    except ValueError as exc:
        log.error("Failed to load profile: %s", exc)
        return 1

    # Use sandbox object ID as run ID (set by Modal)
    run_id = os.environ.get("MODAL_SANDBOX_ID", "local")

    log.info(
        "Starting CUA agent: model=%s, max_steps=%d, %dx%d, profile=%s",
        model,
        max_steps,
        width,
        height,
        profile_name,
    )
    log.info("Directive: %s", directive[:200])

    # Initialize status API
    init_status(run_id)

    # Set up browser
    browser = BrowserManager()
    try:
        await browser.launch(
            width=width,
            height=height,
            start_url=start_url,
            proxy=proxy_url,
        )
        log.info("Browser launched")
    except Exception as e:
        log.error("Failed to launch browser: %s", e)
        await complete_run(error=f"Browser launch failed: {e}")
        return 1

    bridge = ActionRouter(browser=browser, guardrail_config=guardrail_config)

    # Run the agent
    try:
        result = await run_agent(
            directive=directive,
            bridge=bridge,
            model=model,
            max_steps=max_steps,
            thinking_budget=thinking_budget,
            credentials=credentials,
            on_action=push_action,
            profile_prompt=profile.prompt_extension,
        )

        if result.success:
            summary_preview = (result.summary or "")[:200]
            log.info("Agent succeeded: %s", summary_preview)
            await complete_run(summary=result.summary)
        else:
            error_text = str(result.error) if result.error is not None else ""
            log.error("Agent failed: %s", error_text)
            await complete_run(error=result.error)

        log.info(
            "Stats: %d actions, %dms, %d input tokens, %d output tokens",
            result.action_count,
            result.total_duration_ms,
            result.total_input_tokens,
            result.total_output_tokens,
        )

        return 0 if result.success else 1

    except Exception as e:
        log.error("Agent loop crashed: %s", e, exc_info=True)
        await complete_run(error=str(e))
        return 1

    finally:
        await browser.close()
        log.info("Browser closed")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
