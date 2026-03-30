"""CAPTCHA detection and avoidance.

Strategy: detect CAPTCHAs via fast DOM inspection, then wait for Patchright's
stealth patches to auto-resolve them (most Cloudflare challenges pass within
2-5 seconds). No external solving service needed.

If a CAPTCHA doesn't auto-resolve, report back to the agent with a descriptive
message so it can try an alternative approach.
"""

from __future__ import annotations

import asyncio
import logging
import time

from patchright.async_api import Page
from pydantic import BaseModel, ConfigDict

from bridge.js_helpers import CAPTCHA_DETECT_INIT_JS

logger = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 500
_DEFAULT_TIMEOUT_MS = 30_000

# Per-type timeouts: Patchright stealth auto-resolves Cloudflare, but cannot
# solve reCAPTCHA or hCaptcha — fail fast for those instead of burning 30s.
_TYPE_TIMEOUT_MS: dict[str, int] = {
    "cloudflare": 30_000,
    "recaptcha": 5_000,
    "hcaptcha": 5_000,
}

# Self-healing JS: re-inject if missing (isolated context may not have init scripts).
_DETECT_JS = """(initJS) => {
    if (!window.__detectCaptcha) new Function(initJS)();
    return window.__detectCaptcha ? window.__detectCaptcha() : null;
}"""
_STILL_PRESENT_JS = """(initJS) => {
    if (!window.__captchaStillPresent) new Function(initJS)();
    return window.__captchaStillPresent ? window.__captchaStillPresent() : false;
}"""


class CaptchaDetection(BaseModel):
    """Result of a CAPTCHA detection check."""

    model_config = ConfigDict(frozen=True)

    detected: bool
    captcha_type: str | None = None  # "cloudflare" | "recaptcha" | "hcaptcha"
    is_blocking: bool = False  # True if CAPTCHA blocks page content


class CaptchaHandleResult(BaseModel):
    """Outcome of a CAPTCHA detection + wait cycle."""

    model_config = ConfigDict(frozen=True)

    detected: bool
    resolved: bool = False
    captcha_type: str | None = None
    wait_time_ms: int = 0
    message: str = ""


async def detect_captcha(page: Page) -> CaptchaDetection:
    """Fast DOM check (<100ms) for known CAPTCHA patterns."""
    try:
        result = await page.evaluate(_DETECT_JS, CAPTCHA_DETECT_INIT_JS)
    except Exception as exc:
        logger.debug("detect_captcha failed during page.evaluate: %s", exc)
        return CaptchaDetection(detected=False)

    if result is None:
        return CaptchaDetection(detected=False)

    return CaptchaDetection(
        detected=True,
        captcha_type=result["type"],
        is_blocking=result.get("blocking", False),
    )


async def wait_for_captcha_resolution(
    page: Page,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
) -> bool:
    """Wait for a CAPTCHA to resolve on its own.

    Patchright's stealth patches often auto-pass Cloudflare challenges
    within 2-5 seconds without user intervention. Polls every 500ms.

    Returns True if the CAPTCHA resolved, False if it timed out.
    """
    deadline = time.monotonic() + (timeout_ms / 1000)

    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_MS / 1000)
        try:
            still_present = await page.evaluate(
                _STILL_PRESENT_JS, CAPTCHA_DETECT_INIT_JS
            )
            if not still_present:
                return True
        except Exception:
            # Page may have navigated away (which means the challenge resolved)
            return True

    return False


async def handle_captcha_if_present(page: Page) -> CaptchaHandleResult:
    """Detect CAPTCHAs, wait for auto-resolution, report outcome.

    Called automatically by the ActionRouter after navigation actions.
    """
    detection = await detect_captcha(page)

    if not detection.detected:
        return CaptchaHandleResult(detected=False, message="")

    logger.info(
        "CAPTCHA detected: %s (blocking=%s)",
        detection.captcha_type,
        detection.is_blocking,
    )
    start = time.monotonic()

    timeout = _TYPE_TIMEOUT_MS.get(detection.captcha_type or "", _DEFAULT_TIMEOUT_MS)
    resolved = await wait_for_captcha_resolution(page, timeout_ms=timeout)
    wait_ms = int((time.monotonic() - start) * 1000)

    if resolved:
        logger.info("CAPTCHA auto-resolved in %dms", wait_ms)
        return CaptchaHandleResult(
            detected=True,
            resolved=True,
            captcha_type=detection.captcha_type,
            wait_time_ms=wait_ms,
            message=f"[{detection.captcha_type} challenge auto-resolved in {wait_ms}ms]",
        )

    logger.warning("CAPTCHA did not resolve within timeout (%dms)", wait_ms)
    return CaptchaHandleResult(
        detected=True,
        resolved=False,
        captcha_type=detection.captcha_type,
        wait_time_ms=wait_ms,
        message=(
            f"[{detection.captcha_type} challenge detected but did not auto-resolve after "
            f"{wait_ms}ms. Try navigating to a different URL or using an alternative approach.]"
        ),
    )
