"""Local development entrypoint for testing the CUA agent without Modal.

Usage:
    python scripts/run_local.py --directive "Go to example.com and find the contact page"
    python scripts/run_local.py --directive "Search for cats" --display :99 --start-url https://google.com

Requires:
- Linux with Xvfb running (or a real X display)
- Chromium + Patchright installed (`patchright install chromium`)
- ANTHROPIC_API_KEY set in environment
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import TYPE_CHECKING

# Add project root to path — must precede project imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.browser import BrowserManager
from recording.manager import RecordingManager

if TYPE_CHECKING:
    from profiles.loader import Profile

import contextlib

from telemetry.logging import setup_logging

setup_logging()

# Suppress noisy HTTP request logs from httpx
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("cua.local")


async def run(args: argparse.Namespace) -> int:
    from profiles.loader import load_profile
    from recording import RecordingConfig

    os.environ["DISPLAY"] = args.display

    profile = load_profile(args.profile)
    log.info(
        "Display: %s (%dx%d), profile: %s",
        args.display,
        args.width,
        args.height,
        args.profile,
    )
    log.info("Directive: %s", args.directive)

    # Launch browser
    browser = BrowserManager()
    try:
        await browser.launch(
            width=args.width,
            height=args.height,
            start_url=args.start_url,
            proxy=args.proxy,
        )
        log.info("Browser launched")
    except Exception as e:
        log.error("Failed to launch browser: %s", e)
        return 1

    # Initialize recording
    local_output_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output"
    )
    recording_config = RecordingConfig(output_dir=local_output_dir, upload=False)
    recording = RecordingManager(recording_config, run_id="local")
    await recording.start(browser.context)
    log.info("Session recording started: %s", local_output_dir)

    credentials = None
    if args.credentials:
        try:
            credentials = json.loads(args.credentials)
        except json.JSONDecodeError as exc:
            log.error("Invalid --credentials JSON: %s", exc)
            with contextlib.suppress(Exception):
                await recording.stop()
            await browser.close()
            return 1

    # --- Playbook path (deterministic, no LLM loop) ---
    # Skips blinders, scope extraction, guardrails, and ActionRouter entirely.
    if args.playbook:
        return await _run_playbook(args, browser, recording, credentials)

    # --- Standard LLM agent path ---
    return await _run_agent(
        args, browser, profile, recording, credentials, local_output_dir
    )


async def _run_playbook(
    args: argparse.Namespace,
    browser: BrowserManager,
    recording: RecordingManager,
    credentials: dict | None,
) -> int:
    """Execute a playbook deterministically — no blinders, no guardrails, no LLM loop."""
    from playbooks.auth import DashboardAuth
    from playbooks.runner import PlaybookRunner
    from playbooks.store import PlaybookStore

    store = PlaybookStore()
    playbook = store.load(args.playbook)
    log.info("Loaded playbook: %s (%d steps)", playbook.id, len(playbook.steps))

    # Parse playbook params from --playbook-params or directive
    pb_params: dict = {}
    if args.playbook_params:
        pb_params = json.loads(args.playbook_params)
    elif playbook.parameters:
        from playbooks.parser import DirectiveParser

        parsed = DirectiveParser(store).parse(args.directive)
        if parsed:
            _, pb_params = parsed

    # Authenticate if needed (attempt session restore even without fresh credentials)
    if playbook.auth_required:
        auth = DashboardAuth(browser, credentials or {})
        login_url = playbook.start_url or ""
        if not await auth.ensure_authenticated(login_url):
            log.error("Authentication failed")
            with contextlib.suppress(Exception):
                await recording.stop()
            await browser.close()
            return 1

    runner = PlaybookRunner(browser, recording)
    pb_result = await runner.execute(playbook, pb_params)

    # Print results
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
        log.info("Recording saved: %d artifacts", len(manifest.artifacts))
    except Exception as rec_exc:
        log.warning("Recording finalization failed: %s", rec_exc)
    await browser.close()
    log.info("Browser closed")
    return 0 if pb_result.success else 1


async def _run_agent(
    args: argparse.Namespace,
    browser: BrowserManager,
    profile: Profile,
    recording: RecordingManager,
    credentials: dict | None,
    output_dir: str,
) -> int:
    """Run the full LLM agent loop with blinders, guardrails, and scope extraction."""
    from actionlog.actions import save_action_log
    from agent.loop import run_agent
    from blinders.filters import DOMBlinders
    from blinders.scope import extract_task_scope
    from bridge.router import ActionRouter
    from profiles.loader import apply_guardrail_overrides

    guardrail_config = apply_guardrail_overrides(profile)
    if args.allow_private_networks:
        guardrail_config.allow_private_networks = True

    scope = extract_task_scope(args.directive, profile, use_llm=False)
    blinders = DOMBlinders(scope)
    log.info(
        "Blinders: goal_type=%s, actions=%d",
        scope.goal_type,
        len(scope.allowed_actions),
    )

    bridge = ActionRouter(
        browser=browser,
        guardrail_config=guardrail_config,
        blinders=blinders,
        directive=args.directive,
        recording=recording,
    )

    try:
        result = await run_agent(
            directive=args.directive,
            bridge=bridge,
            model=args.model,
            max_steps=args.max_steps,
            thinking_budget=args.thinking_budget,
            credentials=credentials,
            profile_prompt=profile.prompt_extension,
            on_action=lambda a: log.info(
                "  Step %d: %s (%dms)", a.step, a.input_summary, a.duration_ms
            ),
            allowed_actions=scope.allowed_actions,
        )
    finally:
        try:
            manifest = await recording.stop()
            log.info(
                "Recording saved: %d artifacts at %s",
                len(manifest.artifacts),
                output_dir,
            )
            trace_path = os.path.join(output_dir, "trace.zip")
            if os.path.exists(trace_path):
                log.info("View trace: npx playwright show-trace %s", trace_path)
        except Exception as rec_exc:
            log.warning("Recording finalization failed: %s", rec_exc)
        await browser.close()
        log.info("Browser closed")

    # Print results
    print("\n" + "=" * 60)
    print(f"Status:  {'SUCCESS' if result.success else 'FAILED'}")
    print(f"Summary: {result.summary}")
    if result.error:
        print(f"Error:   {result.error}")
    print(f"Actions: {result.action_count}")
    print(f"Duration: {result.total_duration_ms}ms")
    print(f"Tokens:  {result.total_input_tokens} in / {result.total_output_tokens} out")
    print("=" * 60)

    # Save action log
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, "action_log.json")
    await save_action_log(result.action_log, log_path)
    log.info("Action log saved to %s", log_path)

    return 0 if result.success else 1


def main():
    parser = argparse.ArgumentParser(description="Run CUA agent locally (no Modal)")
    parser.add_argument("--directive", required=True, help="Task for the agent")
    from settings import AGENT_MODEL

    parser.add_argument("--model", default=AGENT_MODEL, help="Claude model ID")
    parser.add_argument(
        "--max-steps", type=int, default=50, help="Max tool-call iterations"
    )
    parser.add_argument(
        "--thinking-budget", type=int, default=4096, help="Extended thinking tokens"
    )
    parser.add_argument("--width", type=int, default=1280, help="Display width")
    parser.add_argument("--height", type=int, default=720, help="Display height")
    parser.add_argument("--display", default=":99", help="X display (default: :99)")
    parser.add_argument(
        "--start-url", default=None, help="URL to open on browser launch"
    )
    parser.add_argument(
        "--proxy", default=None, help="Proxy URL (http://user:pass@host:port)"
    )
    parser.add_argument("--credentials", default=None, help="JSON credentials dict")
    parser.add_argument(
        "--profile",
        default="default",
        help="Agent profile name (default, research, form_filling)",
    )
    parser.add_argument(
        "--allow-private-networks",
        action="store_true",
        help="Disable SSRF protection (allow localhost and private IPs)",
    )
    parser.add_argument(
        "--playbook",
        default=None,
        help="Playbook ID to execute (e.g., cancel_order). Skips LLM agent loop.",
    )
    parser.add_argument(
        "--playbook-params",
        default=None,
        help='JSON dict of playbook parameters (e.g., \'{"order_id": "12345"}\')',
    )

    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
