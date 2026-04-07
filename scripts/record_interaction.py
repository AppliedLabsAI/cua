"""Record user browser interactions for playbook generation.

Launches a headful Patchright browser and captures clicks, typing, navigation,
and selections as the user interacts manually. Outputs a structured JSON
interaction log that can be converted into a CUA playbook.

Usage:
    python scripts/record_interaction.py --start-url https://dashboard.internal/orders
    python scripts/record_interaction.py --start-url https://example.com --output /tmp/rec.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contextlib

from patchright.async_api import Page, async_playwright

from bridge.js_helpers import RECORDER_INIT_JS
from settings import LOGIN_TIMEOUT_MS

logger = logging.getLogger("cua.recorder")

_DEFAULT_OUTPUT = "output/recording.json"


class InteractionRecorder:
    """Accumulates interaction events sent from the browser JS layer."""

    def __init__(self) -> None:
        self._log: list[dict] = []
        self._seq: int = 0
        self._start_time: float = time.monotonic()
        self._done = asyncio.Event()

    def on_event(self, payload_json: str) -> None:
        """Callback for window.__cuaRecordEvent. Must be sync for expose_function."""
        try:
            data = json.loads(payload_json)
        except (json.JSONDecodeError, Exception) as exc:
            logger.error(
                "Failed to parse recorder event: %s (payload: %s)",
                exc,
                payload_json[:200],
            )
            return

        if data.get("action") == "__done":
            logger.info("Done signal received (Ctrl+Shift+S)")
            self._done.set()
            return

        if data.get("action") == "scroll":
            # Playbooks do not model manual scroll; selector-based steps already
            # auto-scroll targets into view when interacting with them.
            return

        self._seq += 1
        data["seq"] = self._seq  # Override JS seq (resets on navigation)
        self._log.append(data)

        action = data.get("action", "?")
        desc = self._describe_event(data)
        logger.info("  [%d] %s  %s", self._seq, action.ljust(10), desc)

    @staticmethod
    def _describe_event(data: dict) -> str:
        action = data.get("action", "")
        params = data.get("params", {})
        selector = data.get("selector", {}) or {}
        el_text = (data.get("elementText") or "")[:50]
        el_tag = data.get("elementTag", "")
        primary = selector.get("primary", "")

        if action == "click":
            target = el_text or selector.get("description", "") or primary
            return f"'{target}' <{el_tag}>" if el_tag else target

        if action == "key_press":
            text = params.get("text", "")
            key = params.get("key", "")
            if text and key:
                return f"typed '{text}' + {key}"
            if text:
                masked = text if len(text) <= 40 else text[:37] + "..."
                return f"typed '{masked}'"
            if key:
                return f"[{key}]"
            return ""

        if action == "goto":
            return params.get("url", "")[:80]

        if action == "select":
            value = params.get("optionText") or params.get("value", "")
            return f"'{value}' <{el_tag}>" if el_tag else f"'{value}'"

        return el_text or primary

    @property
    def interactions(self) -> list[dict]:
        return list(self._log)

    @property
    def done(self) -> asyncio.Event:
        return self._done

    @property
    def duration_ms(self) -> int:
        return int((time.monotonic() - self._start_time) * 1000)


async def run(args: argparse.Namespace) -> int:
    display = os.environ.get("DISPLAY", "")
    if sys.platform == "linux" and not display:
        os.environ["DISPLAY"] = args.display

    recorder = InteractionRecorder()

    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                f"--window-size={args.width},{args.height}",
                "--window-position=0,0",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
            ],
        )

        context = await browser.new_context(
            viewport={"width": args.width, "height": args.height},
            bypass_csp=True,
        )

        # Wire JS -> Python bridge (survives navigations, works in all frames)
        await context.expose_function("__cuaRecordEvent", recorder.on_event)

        # Detect browser close — "disconnected" fires reliably when the user
        # closes Chrome, unlike context.on("close") which requires a clean shutdown.
        browser.on("disconnected", lambda _: recorder.done.set())

        page = await context.new_page()

        # Inject recorder via page.evaluate after each navigation
        # (add_init_script is suppressed by Patchright to avoid bot detection)
        async def inject_recorder(page_ref: Page) -> None:
            with contextlib.suppress(Exception):
                await page_ref.evaluate(RECORDER_INIT_JS)

        def wire_page(p: Page) -> None:
            """Attach recorder injection to a page's navigation events."""
            p.on(
                "domcontentloaded",
                lambda _p=p: asyncio.ensure_future(inject_recorder(_p)),
            )

        wire_page(page)

        # When a new tab/popup opens, wire it up and inject the recorder
        def on_new_page(new_page: Page) -> None:
            logger.info("New tab opened: %s", new_page.url[:80] or "about:blank")
            wire_page(new_page)
            asyncio.ensure_future(inject_recorder(new_page))

        context.on("page", on_new_page)

        # Handle Ctrl+C
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, recorder.done.set)
        else:
            signal.signal(signal.SIGINT, lambda *_: recorder.done.set())

        logger.info("=" * 60)
        logger.info("RECORDING STARTED")
        logger.info("Interact with the browser to record your workflow.")
        logger.info("When done: close the browser OR press Ctrl+Shift+S")
        logger.info("=" * 60)

        if args.start_url:
            await page.goto(
                args.start_url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS
            )
            logger.info("Navigated to %s", args.start_url)

        # Inject on the initial page as well
        await inject_recorder(page)

        # Wait for done signal
        await recorder.done.wait()

    except Exception:
        if recorder.done.is_set():
            pass  # Browser disconnected — expected during shutdown
        else:
            logger.exception("Recording failed")
            return 1
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        with contextlib.suppress(Exception):
            await pw.stop()

    # Build and write output
    output = {
        "version": 1,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "start_url": args.start_url or "",
        "duration_ms": recorder.duration_ms,
        "interactions": recorder.interactions,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))

    count = len(output["interactions"])
    logger.info(
        "Recording saved: %s (%d events, %dms)",
        output_path,
        count,
        output["duration_ms"],
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record browser interactions for playbook generation"
    )
    parser.add_argument("--start-url", default=None, help="URL to open on launch")
    parser.add_argument(
        "--output",
        default=_DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {_DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--width", type=int, default=1280, help="Browser width (default: 1280)"
    )
    parser.add_argument(
        "--height", type=int, default=720, help="Browser height (default: 720)"
    )
    parser.add_argument(
        "--display", default=":99", help="X display for Linux (default: :99)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
