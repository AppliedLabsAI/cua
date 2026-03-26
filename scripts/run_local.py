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

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("cua.local")


async def run(args: argparse.Namespace) -> int:
    from actionlog.actions import save_action_log
    from agent.loop import run_agent
    from blinders.filters import DOMBlinders
    from blinders.scope import extract_task_scope
    from bridge.browser import BrowserManager
    from bridge.router import ActionRouter
    from profiles.loader import apply_guardrail_overrides, load_profile

    width = args.width
    height = args.height
    display = args.display

    os.environ["DISPLAY"] = display

    # Load profile
    profile = load_profile(args.profile)
    guardrail_config = apply_guardrail_overrides(profile)
    if args.allow_private_networks:
        guardrail_config.allow_private_networks = True

    log.info("Display: %s (%dx%d), profile: %s", display, width, height, args.profile)
    log.info("Directive: %s", args.directive)

    # Launch browser
    browser = BrowserManager()
    try:
        await browser.launch(
            width=width,
            height=height,
            start_url=args.start_url,
            proxy=args.proxy,
        )
        log.info("Browser launched")
    except Exception as e:
        log.error("Failed to launch browser: %s", e)
        return 1

    # Set up Cognitive Blinders
    scope = extract_task_scope(args.directive, profile)
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
    )

    credentials = None
    if args.credentials:
        credentials = json.loads(args.credentials)

    # Run agent
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
    output_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output"
    )
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, "action_log.json")
    await save_action_log(result.action_log, log_path)
    log.info("Action log saved to %s", log_path)

    return 0 if result.success else 1


def main():
    parser = argparse.ArgumentParser(description="Run CUA agent locally (no Modal)")
    parser.add_argument("--directive", required=True, help="Task for the agent")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Claude model ID")
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

    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
