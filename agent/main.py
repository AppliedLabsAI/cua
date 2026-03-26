"""CLI entry point for the CUA agent — runs inside the Modal sandbox.

Reads configuration from environment variables (set by the entrypoint.sh),
initializes the bridge and browser, runs the agent loop, and reports results
to the in-sandbox status API.
"""

from __future__ import annotations

import asyncio
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
    from config import CUAConfig

    # Load all configuration from environment
    try:
        config = CUAConfig.from_env()
    except (ValueError, Exception) as exc:
        log.error("Configuration error: %s", exc)
        return 1

    # Use sandbox object ID as run ID (set by Modal)
    run_id = os.environ.get("MODAL_SANDBOX_ID", "local")

    log.info(
        "Starting CUA agent: model=%s, max_steps=%d, %dx%d, profile=%s",
        config.model,
        config.max_steps,
        config.width,
        config.height,
        config.profile_name,
    )
    log.info("Directive: %s", config.directive[:200])

    # Initialize status API
    init_status(run_id)

    # Set up browser
    browser = BrowserManager()
    try:
        await browser.launch(
            width=config.width,
            height=config.height,
            start_url=config.start_url,
            proxy=config.proxy_url,
        )
        log.info("Browser launched")
    except Exception as e:
        log.error("Failed to launch browser: %s", e)
        await complete_run(error=f"Browser launch failed: {e}")
        return 1

    bridge = ActionRouter(browser=browser, guardrail_config=config.guardrail_config)

    # Run the agent
    try:
        profile_prompt = config.profile.prompt_extension if config.profile else None
        result = await run_agent(
            directive=config.directive,
            bridge=bridge,
            model=config.model,
            max_steps=config.max_steps,
            thinking_budget=config.thinking_budget,
            credentials=config.credentials,
            on_action=push_action,
            profile_prompt=profile_prompt,
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
