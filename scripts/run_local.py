"""Local development entrypoint for testing the CUA agent without Modal.

Usage:
    python scripts/run_local.py --directive "Go to example.com and find the contact page"
    python scripts/run_local.py --directive "Search for cats" --display :99 --start-url https://google.com

Requires:
- Linux with Xvfb running (or a real X display)
- Chromium + Patchright installed (`patchright install chromium`)
- LLM API key set in environment (ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from typing import Any, Literal

import click

# Add project root to path — must precede project imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.browser import BrowserManager
from recording.manager import RecordingManager
from settings import PRIMARY_MODEL
from telemetry.logging import C_DIM, C_RESET, setup_logging

setup_logging()

# Suppress noisy HTTP request logs from httpx
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("cua.local")


async def _launch_browser(
    display: str, width: int, height: int, start_url: str | None, proxy: str | None
) -> BrowserManager:
    os.environ["DISPLAY"] = display
    browser = BrowserManager()
    await browser.launch(width=width, height=height, start_url=start_url, proxy=proxy)
    logger.info("Browser launched")
    return browser


async def _init_recording(browser: BrowserManager) -> tuple[RecordingManager, str]:
    from recording import RecordingConfig

    output_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output"
    )
    config = RecordingConfig(output_dir=output_dir, upload=False)
    recording = RecordingManager(config, run_id="local")
    await recording.start(browser.context)
    logger.info("Session recording started: %s", output_dir)
    return recording, output_dir


async def _run_playbook(
    directive: str,
    playbook_id: str,
    playbook_params: str | None,
    browser: BrowserManager,
    recording: RecordingManager,
    credentials: dict | None,
) -> int:
    """Execute a playbook deterministically."""
    from playbooks.auth import DashboardAuth
    from playbooks.runner import PlaybookRunner
    from playbooks.store import PlaybookStore

    store = PlaybookStore()
    playbook = store.load(playbook_id)
    logger.info("Loaded playbook: %s (%d steps)", playbook.id, len(playbook.steps))

    pb_params: dict = {}
    if playbook_params:
        pb_params = json.loads(playbook_params)
    elif playbook.parameters:
        from playbooks.parser import DirectiveParser

        parsed = DirectiveParser(store).parse(directive)
        if parsed:
            _, pb_params = parsed

    if playbook.auth_required:
        auth = DashboardAuth(browser, credentials or {})
        login_url = playbook.start_url or ""
        if not await auth.ensure_authenticated(login_url):
            logger.error("Authentication failed")
            with contextlib.suppress(Exception):
                await recording.stop()
            await browser.close()
            return 1

    runner = PlaybookRunner(browser, recording)
    pb_result = await runner.execute(playbook, pb_params)

    print("\n" + "=" * 60)
    print(f"Status:  {'SUCCESS' if pb_result.success else 'FAILED'}")
    print(f"Playbook: {pb_result.playbook_id}")
    if pb_result.error:
        print(f"Error:   {pb_result.error}")
    print(f"Steps:   {len(pb_result.step_results)}")
    print(f"Duration: {pb_result.total_duration_ms}ms")
    for sr in pb_result.step_results:
        status = "OK" if sr.success else "FAIL"
        recovery = " (LLM recovery)" if sr.recovery_used else ""
        print(
            f"  Step {sr.step_index + 1}: {sr.action} — {status} ({sr.duration_ms}ms){recovery}"
        )
        if sr.extracted_text:
            print(f"\n--- Extracted Data ---\n{sr.extracted_text}\n")
    if pb_result.extracted_text and not any(
        sr.extracted_text for sr in pb_result.step_results
    ):
        print(f"\n--- Extracted Data ---\n{pb_result.extracted_text}\n")
    print("=" * 60)

    try:
        manifest = await recording.stop()
        logger.info("Recording saved: %d artifacts", len(manifest.artifacts))
    except Exception as rec_exc:
        logger.warning("Recording finalization failed: %s", rec_exc)
    await browser.close()
    logger.info("Browser closed")
    return 0 if pb_result.success else 1


async def _run_agent(
    directive: str,
    model: str,
    max_steps: int,
    thinking: Literal["minimal", "low", "medium", "high", "xhigh"],
    browser: BrowserManager,
    profile: Any,
    recording: RecordingManager,
    output_dir: str,
    credentials: dict | None,
    allow_private_networks: bool,
    output_schema: dict[str, Any] | None = None,
) -> int:
    """Run the full LLM agent loop with blinders, guardrails, and scope extraction."""
    from actionlog.actions import save_action_log
    from agent.loop import run_agent
    from agent.output import agent_result_to_output
    from blinders.filters import DOMBlinders
    from blinders.scope import extract_task_scope
    from bridge.router import ActionRouter
    from profiles.loader import apply_guardrail_overrides

    guardrail_config = apply_guardrail_overrides(profile)
    if allow_private_networks:
        guardrail_config.allow_private_networks = True

    scope = await extract_task_scope(directive, profile)
    blinders = DOMBlinders(scope)
    logger.info(
        "Blinders: goal_type=%s, actions=%d",
        scope.goal_type,
        len(scope.allowed_actions),
    )

    bridge = ActionRouter(
        browser=browser,
        guardrail_config=guardrail_config,
        blinders=blinders,
        directive=directive,
    )

    try:
        result = await run_agent(
            directive=directive,
            bridge=bridge,
            model=model,
            max_steps=max_steps,
            thinking=thinking,
            credentials=credentials,
            profile_prompt=profile.prompt_extension,
            on_action=lambda a: logger.info(
                "  Step %d: %s %s(%dms)%s",
                a.step,
                a.input_summary,
                C_DIM,
                a.duration_ms,
                C_RESET,
            ),
            allowed_actions=scope.allowed_actions,
            output_schema=output_schema,
        )
    finally:
        try:
            manifest = await recording.stop()
            logger.info(
                "Recording saved: %d artifacts at %s",
                len(manifest.artifacts),
                output_dir,
            )
            trace_path = os.path.join(output_dir, "trace.zip")
            if os.path.exists(trace_path):
                logger.info("View trace: npx playwright show-trace %s", trace_path)
        except Exception as rec_exc:
            logger.warning("Recording finalization failed: %s", rec_exc)
        await browser.close()
        logger.info("Browser closed")

    output = agent_result_to_output(result)
    print("\n" + "=" * 60)
    print(json.dumps(output.model_dump(), indent=2, ensure_ascii=False))
    print("=" * 60)

    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, "action_log.json")
    await save_action_log(result.action_log, log_path)
    logger.info("Action log saved to %s", log_path)

    return 0 if result.success else 1


@click.command()
@click.option("--directive", required=True, help="Task for the agent")
@click.option(
    "--model", default=PRIMARY_MODEL, help="Model ID (any PydanticAI-supported model)"
)
@click.option("--max-steps", default=50, type=int, help="Max tool-call iterations")
@click.option(
    "--thinking",
    type=click.Choice(["minimal", "low", "medium", "high", "xhigh"]),
    help="Thinking effort level",
)
@click.option("--width", default=1280, type=int, help="Display width")
@click.option("--height", default=720, type=int, help="Display height")
@click.option("--display", default=":99", help="X display")
@click.option("--start-url", default=None, help="URL to open on browser launch")
@click.option("--proxy", default=None, help="Proxy URL (http://user:pass@host:port)")
@click.option("--credentials", default=None, help="JSON credentials dict")
@click.option("--profile", default="default", help="Agent profile name")
@click.option(
    "--allow-private-networks",
    is_flag=True,
    help="Disable SSRF protection (allow localhost and private IPs)",
)
@click.option(
    "--output-schema", default=None, help="JSON Schema for structured output extraction"
)
@click.option(
    "--playbook", default=None, help="Playbook ID to execute (skips LLM agent loop)"
)
@click.option(
    "--playbook-params", default=None, help="JSON dict of playbook parameters"
)
def main(
    directive: str,
    model: str,
    max_steps: int,
    thinking: Literal["minimal", "low", "medium", "high", "xhigh"],
    width: int,
    height: int,
    display: str,
    start_url: str | None,
    proxy: str | None,
    credentials: str | None,
    profile: str,
    allow_private_networks: bool,
    output_schema: str | None,
    playbook: str | None,
    playbook_params: str | None,
) -> None:
    """Run CUA agent locally (no Modal)."""

    async def _main() -> int:
        from profiles.loader import load_profile

        prof = load_profile(profile)
        logger.info("Display: %s (%dx%d), profile: %s", display, width, height, profile)
        logger.info("Directive: %s", directive)

        try:
            browser = await _launch_browser(display, width, height, start_url, proxy)
        except Exception as e:
            logger.error("Failed to launch browser: %s", e)
            return 1

        recording, output_dir = await _init_recording(browser)

        creds = None
        if credentials:
            try:
                raw_creds = json.loads(credentials)
            except json.JSONDecodeError as exc:
                logger.error("Invalid --credentials JSON: %s", exc)
                with contextlib.suppress(Exception):
                    await recording.stop()
                await browser.close()
                return 1

            from credentials import resolve_credentials

            creds = resolve_credentials(raw_creds)

        if playbook:
            return await _run_playbook(
                directive, playbook, playbook_params, browser, recording, creds
            )

        parsed_schema = None
        if output_schema:
            try:
                parsed_schema = json.loads(output_schema)
            except json.JSONDecodeError as exc:
                logger.error("Invalid --output-schema JSON: %s", exc)
                with contextlib.suppress(Exception):
                    await recording.stop()
                await browser.close()
                return 1

        return await _run_agent(
            directive=directive,
            model=model,
            max_steps=max_steps,
            thinking=thinking,
            browser=browser,
            profile=prof,
            recording=recording,
            output_dir=output_dir,
            credentials=creds,
            allow_private_networks=allow_private_networks,
            output_schema=parsed_schema,
        )

    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
