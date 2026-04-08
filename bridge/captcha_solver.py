"""Enhanced CAPTCHA solver — ported from SeleniumBase's CDP Mode.

Extends CUA's existing captcha.py (passive wait-for-resolution) with
SeleniumBase's active click strategies. The key insight from SeleniumBase:

1. **Turnstile**: The checkbox is inside a shadow DOM within a div.
   You locate the container, calculate its position, and click at
   offset (28, 32) from the top-left corner.

2. **hCaptcha**: Often nested inside an Incapsula iframe. You need to
   find the outer iframe, then locate the hCaptcha widget inside it.
   Click at offset (30, 36).

3. **reCAPTCHA**: The "I'm not a robot" checkbox is inside an iframe.
   Click at offset (26, 35). Skip invisible reCAPTCHA (bottom-right badge).

4. **DataDome**: Uses a slider puzzle. SeleniumBase solves this by
   opening the iframe source in a new tab, finding slider and target
   positions, then performing a drag-drop operation.

Strategy:
    1. First try passive auto-resolution (Patchright stealth often passes)
    2. If that fails, try active clicking (this module)
    3. If that fails, report back to the agent

References:
    - SeleniumBase/seleniumbase/core/sb_cdp.py lines 2036-2450
    - SeleniumBase's solve_captcha() / click_captcha() / gui_click_captcha()

NOTE: For educational purposes only.
"""

from __future__ import annotations

import asyncio
import logging
import time

from patchright.async_api import Page

from bridge.captcha import (
    CaptchaHandleResult,
    detect_captcha,
    wait_for_captcha_resolution,
)
from bridge.stealth import (
    try_click_hcaptcha,
    try_click_recaptcha,
    try_click_turnstile,
    try_solve_captcha,
)

logger = logging.getLogger(__name__)


async def handle_captcha_with_active_solving(
    page: Page,
    *,
    passive_timeout_ms: int = 5_000,
    post_click_wait_ms: int = 3_000,
) -> CaptchaHandleResult:
    """Enhanced CAPTCHA handler with active click-based solving.

    Flow (mirrors SeleniumBase's approach):
        1. Detect CAPTCHA type
        2. Wait briefly for passive auto-resolution (Patchright stealth)
        3. If still present, try active clicking (SeleniumBase strategy)
        4. Wait for resolution after click
        5. Report outcome

    This extends CUA's existing handle_captcha_if_present() with
    SeleniumBase's solve_captcha() logic.
    """
    detection = await detect_captcha(page)
    if not detection.detected:
        return CaptchaHandleResult(detected=False, message="")

    captcha_type = detection.captcha_type or "unknown"
    logger.info(
        "CAPTCHA detected: %s (blocking=%s) — trying passive resolution first",
        captcha_type,
        detection.is_blocking,
    )
    start = time.monotonic()

    # Phase 1: Passive wait (Patchright stealth may auto-resolve)
    resolved = await wait_for_captcha_resolution(page, timeout_ms=passive_timeout_ms)
    if resolved:
        wait_ms = int((time.monotonic() - start) * 1000)
        logger.info("CAPTCHA passively auto-resolved in %dms", wait_ms)
        return CaptchaHandleResult(
            detected=True,
            resolved=True,
            captcha_type=captcha_type,
            wait_time_ms=wait_ms,
            message=f"[{captcha_type} auto-resolved in {wait_ms}ms]",
        )

    # Phase 2: Active clicking (SeleniumBase strategy)
    logger.info(
        "Passive resolution failed after %dms — trying active click",
        int((time.monotonic() - start) * 1000),
    )

    click_result = await _try_active_click(page, captcha_type)

    if click_result:
        # Phase 3: Wait for resolution after click
        await asyncio.sleep(0.5)  # Small pause like SeleniumBase's time.sleep(0.75)
        resolved = await wait_for_captcha_resolution(
            page, timeout_ms=post_click_wait_ms
        )
        wait_ms = int((time.monotonic() - start) * 1000)

        if resolved:
            logger.info(
                "CAPTCHA solved via active click (%s) in %dms",
                click_result,
                wait_ms,
            )
            return CaptchaHandleResult(
                detected=True,
                resolved=True,
                captcha_type=captcha_type,
                wait_time_ms=wait_ms,
                message=f"[{captcha_type} solved via click in {wait_ms}ms: {click_result}]",
            )

    # Phase 4: Nothing worked — diagnose why
    wait_ms = int((time.monotonic() - start) * 1000)
    failure_hint = await _diagnose_captcha_failure(page, captcha_type)
    logger.warning(
        "CAPTCHA not resolved after passive + active attempts (%dms): %s",
        wait_ms,
        failure_hint,
    )
    return CaptchaHandleResult(
        detected=True,
        resolved=False,
        captcha_type=captcha_type,
        wait_time_ms=wait_ms,
        message=(
            f"[{captcha_type} challenge not solved after {wait_ms}ms. {failure_hint}]"
        ),
    )


async def _diagnose_captcha_failure(page: Page, captcha_type: str) -> str:
    """Diagnose why CAPTCHA solving failed and return an actionable hint."""
    if captcha_type == "hcaptcha":
        try:
            # Check if the image challenge popup is showing
            challenge = page.locator('iframe[title="hCaptcha challenge"]').first
            if await challenge.is_visible(timeout=300):
                return (
                    "An hCaptcha image-selection challenge is showing. "
                    "This requires visual puzzle solving and cannot be "
                    "bypassed automatically. Try reloading the page for "
                    "a fresh challenge, or use a different login method "
                    "(e.g. SSO, magic link, OTP) if available."
                )
            # Check if checkbox iframe exists but is hidden
            hidden = page.locator(
                'iframe[data-hcaptcha-widget-id][aria-hidden="true"]'
            ).first
            if await hidden.count() > 0:
                return (
                    "The hCaptcha checkbox is hidden (the site auto-triggered "
                    "the challenge on form submit). Try a different login "
                    "approach or reload the page."
                )
        except Exception:
            pass
    return "Try navigating to a different URL or using an alternative approach."


async def _try_active_click(page: Page, captcha_type: str) -> str | None:
    """Dispatch to the appropriate click handler based on CAPTCHA type.

    Returns a description string if a click was attempted, None otherwise.
    """
    if captcha_type == "cloudflare":
        if await try_click_turnstile(page):
            return "turnstile_click"
    elif captcha_type == "hcaptcha":
        if await try_click_hcaptcha(page):
            return "hcaptcha_click"
    elif captcha_type == "recaptcha" and await try_click_recaptcha(page):
        return "recaptcha_click"

    # Fallback: try the generic solver which probes page source
    result = await try_solve_captcha(page)
    if result:
        return result

    return None


# ---------------------------------------------------------------------------
# Turnstile Alignment Fix
# ---------------------------------------------------------------------------
# SeleniumBase has an elaborate strategy to fix CSS alignment of Turnstile
# widgets. Some sites center-align the Turnstile container, which makes
# the bounding_box() coordinates inaccurate for clicking. SeleniumBase
# rewrites the CSS to left-align before clicking.
#
# From sb_cdp.py ~line 2290:
#   script = """var $elements = document.querySelectorAll(
#     'form[class], form div[class]');
#     ... new_class = the_class.replaceAll('center', 'left'); ..."""

TURNSTILE_ALIGNMENT_FIX_JS = """
(() => {
    // Fix centered Turnstile widgets so bounding_box is accurate
    const fixCenter = (selectors) => {
        for (const sel of selectors) {
            const elements = document.querySelectorAll(sel);
            for (const el of elements) {
                const cls = el.getAttribute('class') || '';
                if (cls.includes('center') || cls.includes('right')) {
                    el.setAttribute('class',
                        cls.replaceAll('center', 'left').replaceAll('right', 'left')
                    );
                }
                const style = el.getAttribute('style') || '';
                if (style.includes('center') || style.includes('right')) {
                    el.setAttribute('style',
                        style.replaceAll('center', 'left').replaceAll('right', 'left')
                    );
                }
            }
        }
    };
    fixCenter([
        'form[class]', 'form div[class]',
        'form[style]', 'form div[style]',
        '[id*="turnstile"]', '[class*="turnstile"]',
        '[style*="text-align: center;"]',
    ]);
})();
"""


async def fix_turnstile_alignment(page: Page) -> None:
    """Fix CSS alignment of Turnstile widgets before clicking.

    Ported from SeleniumBase's __click_captcha alignment fix logic.
    Call this before try_click_turnstile() for better accuracy.
    """
    try:
        await page.evaluate(TURNSTILE_ALIGNMENT_FIX_JS)
    except Exception as exc:
        logger.debug("Turnstile alignment fix failed: %s", exc)
