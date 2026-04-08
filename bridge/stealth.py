"""Anti-bot stealth evasion layer — ported from SeleniumBase's CDP Mode.

Educational implementation that adapts SeleniumBase's anti-detection techniques
for use with Patchright/Playwright. This module provides three layers of evasion:

1. **Browser Launch Args** — Chrome flags that reduce bot fingerprinting signals
   (SeleniumBase: Config._default_browser_args, browser_launcher.py)

2. **JavaScript Stealth Patches** — Runtime JS injected before page load to mask
   automation artifacts like navigator.webdriver, window.cdc_*, etc.
   (SeleniumBase: undetected/__init__.py, sb_cdp.py)

3. **CAPTCHA Interaction** — CDP-level click strategies for Turnstile, hCaptcha,
   and reCAPTCHA challenges using element rect calculations.
   (SeleniumBase: sb_cdp.py __click_captcha, __cdp_click_incapsula_hcaptcha)

References:
    - https://github.com/seleniumbase/SeleniumBase/blob/master/examples/cdp_mode/ReadMe.md
    - https://github.com/seleniumbase/SeleniumBase/blob/master/examples/cdp_mode/playwright/ReadMe.md
    - SeleniumBase/seleniumbase/undetected/__init__.py (cdc_props removal)
    - SeleniumBase/seleniumbase/undetected/cdp_driver/config.py (launch args)
    - SeleniumBase/seleniumbase/core/sb_cdp.py (captcha solving)

NOTE: Patchright already applies most navigator.webdriver patches internally.
      This module layers *additional* evasions on top for educational study.
"""

from __future__ import annotations

import logging
from pathlib import Path

from patchright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layer 1: Chrome Launch Arguments (from SeleniumBase Config)
# ---------------------------------------------------------------------------
# SeleniumBase uses these in undetected/cdp_driver/config.py and
# core/browser_launcher.py to reduce automation fingerprinting signals.
# Patchright handles many of these internally, but some extras help.

STEALTH_LAUNCH_ARGS: list[str] = [
    # Core anti-detection (from SeleniumBase UC Mode)
    "--disable-blink-features=AutomationControlled",
    # Privacy / anti-fingerprinting
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-client-side-phishing-detection",
    "--disable-ipc-flooding-protection",
    "--disable-renderer-backgrounding",
    # Reduce noise in navigator APIs
    "--disable-dev-shm-usage",
    "--disable-breakpad",
    "--no-pings",
    "--dns-prefetch-disable",
    # WebRTC IP leak prevention — critical when using proxies.
    # Without this, STUN requests reveal the real (datacenter) IP
    # even behind a residential proxy. hCaptcha checks for this.
    "--enforce-webrtc-ip-permission-check",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    # Suppress UI elements that leak automation context
    "--disable-infobars",
    "--disable-translate",
    "--disable-prompt-on-repost",
    "--password-store=basic",
    "--disable-save-password-bubble",
    "--disable-single-click-autofill",
    "--disable-password-generation",
    "--disable-search-engine-choice-screen",
    "--disable-device-discovery-notifications",
    # SeleniumBase feature flags (prevent leaky Chrome features)
    "--disable-features="
    "IsolateOrigins,"
    "site-per-process,"
    "Translate,"
    "InsecureDownloadWarnings,"
    "DownloadBubble,"
    "DownloadBubbleV2,"
    "OptimizationTargetPrediction,"
    "OptimizationGuideModelDownloading,"
    "SidePanelPinning,"
    "UserAgentClientHint,"
    "PrivacySandboxSettings4,"
    "OptimizationHintsFetching,"
    "InterestFeedContentSuggestions,"
    "WebRtcHideLocalIpsWithMdns",
    # Testing / animation suppression (reduces timing-based detection)
    "--wm-window-animations-disabled",
    "--animation-duration-scale=0",
    "--deny-permission-prompts",
]


def get_stealth_launch_args(
    width: int = 1280,
    height: int = 720,
) -> list[str]:
    """Return Chrome launch args with anti-bot evasions applied.

    Combines CUA's existing args with SeleniumBase's stealth flags.
    Some args overlap with what CUA already uses — that's fine, Chrome
    deduplicates them.
    """
    return [
        f"--window-size={width},{height}",
        "--window-position=0,0",
        "--no-first-run",
        "--no-default-browser-check",
        *STEALTH_LAUNCH_ARGS,
    ]


# ---------------------------------------------------------------------------
# Layer 2: JavaScript Stealth Patches
# ---------------------------------------------------------------------------
# SeleniumBase injects these via Page.addScriptToEvaluateOnNewDocument
# (see undetected/__init__.py _hook_remove_cdc_props, and the broader
# stealth JS ecosystem). Patchright handles navigator.webdriver natively,
# but these extras address edge cases that anti-bot systems probe.

STEALTH_JS = Path(__file__).parent / "scripts" / "stealth_evasions.js"


def get_stealth_js() -> str:
    """Load the stealth JS payload. Returns empty string if file missing."""
    try:
        return STEALTH_JS.read_text()
    except FileNotFoundError:
        logger.warning("stealth_evasions.js not found — skipping JS patches")
        return ""


async def inject_stealth_scripts(context: BrowserContext) -> None:
    """Register stealth JS patches as an init script on the browser context.

    These run before any page script on every navigation, surviving
    soft navigations (SPA pushState) and hard navigations alike.

    In SeleniumBase, this is done via:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", ...)
    In Patchright/Playwright, we use context.add_init_script().
    """
    js = get_stealth_js()
    if js:
        await context.add_init_script(script=js)
        logger.info("Stealth JS evasions injected (%d bytes)", len(js))


# ---------------------------------------------------------------------------
# Layer 3: CAPTCHA Interaction Strategies
# ---------------------------------------------------------------------------
# SeleniumBase's CDP Mode clicks CAPTCHAs using precise element rect
# calculations via CDP. This is adapted from sb_cdp.py's __click_captcha,
# __cdp_click_incapsula_hcaptcha, and __gui_click_recaptcha methods.
#
# Key insight from SeleniumBase: CAPTCHAs use iframes. You need to locate
# the iframe, calculate the checkbox position *within* the iframe using
# element rects, then click with an offset. The offsets (x=28, y=32 for
# Turnstile; x=30, y=36 for hCaptcha) target the checkbox center.


async def try_click_turnstile(page: Page) -> bool:
    """Attempt to click a Cloudflare Turnstile checkbox via CDP.

    Port of SeleniumBase's __click_captcha Turnstile path.
    Searches for known Turnstile container selectors and clicks with offset.

    Returns True if a click was attempted, False if no Turnstile found.
    """
    # SeleniumBase selector cascade for Turnstile (from sb_cdp.py)
    turnstile_selectors = [
        '[class="cf-turnstile"]',
        "#challenge-form div > div",
        '[style="display: grid;"] div div',
        "[class*=spacer] + div div",
        ".spacer div:not([class])",
        '[data-testid*="challenge-"] div',
        "div#turnstile-widget div:not([class])",
        "ngx-turnstile div:not([class])",
        'form div:not([class]):has(input[name*="cf-turn"])',
        ".cf-turnstile-wrapper",
        '[id*="turnstile"] div:not([class])',
        '[class*="turnstile"] div:not([class])',
    ]

    for selector in turnstile_selectors:
        try:
            element = page.locator(selector).first
            if await element.is_visible(timeout=2_000):
                box = await element.bounding_box()
                if box:
                    # SeleniumBase offset: x=28, y=32 from element origin
                    x = box["x"] + 28
                    y = box["y"] + 32
                    await page.mouse.click(x, y)
                    logger.info(
                        "Clicked Turnstile at (%.0f, %.0f) via selector: %s",
                        x,
                        y,
                        selector,
                    )
                    return True
        except Exception:
            continue

    logger.debug("No Turnstile widget found with any selector")
    return False


async def try_click_hcaptcha(page: Page) -> bool:
    """Attempt to click an hCaptcha checkbox via CDP.

    Port of SeleniumBase's __cdp_click_incapsula_hcaptcha.

    hCaptcha has two modes:
    1. **Checkbox mode** — an iframe with the "I'm human" checkbox.
       Click at offset (30, 36) from the iframe's top-left corner.
    2. **Challenge mode** — the site auto-triggers hCaptcha on form submit
       and skips the checkbox entirely, jumping straight to an image-selection
       popup (iframe[title="hCaptcha challenge"]). The checkbox iframe is
       hidden (display:none, aria-hidden:true, bounding rect 0×0).
       In this case the challenge requires visual puzzle solving which
       cannot be done via simple clicking.

    Returns True if a click was attempted, False if no clickable hCaptcha found.
    """
    # hCaptcha loads asynchronously — give it time to render.
    iframe_timeout_ms = 2_000

    # --- Phase 1: Try to find a visible checkbox iframe ---
    # Selectors ordered from most specific to broadest.
    checkbox_selectors = [
        "iframe[data-hcaptcha-widget-id]",
        'iframe[src*="hcaptcha.com"]',
        ".h-captcha iframe",
        "#hcaptchaWidget iframe",
    ]

    # Check if we're inside an Incapsula wrapper first
    incapsula_wrapper = 'iframe[src*="_Incapsula_Resource?"]'
    try:
        wrapper = page.locator(incapsula_wrapper).first
        if await wrapper.is_visible(timeout=500):
            frame = wrapper.content_frame
            if frame:
                for sel in checkbox_selectors:
                    try:
                        inner = frame.locator(sel).first
                        if await inner.is_visible(timeout=iframe_timeout_ms):
                            box = await inner.bounding_box()
                            if box and box["width"] > 0:
                                x = box["x"] + 30
                                y = box["y"] + 36
                                await page.mouse.click(x, y)
                                logger.info(
                                    "Clicked nested hCaptcha checkbox at (%.0f, %.0f)",
                                    x,
                                    y,
                                )
                                return True
                    except Exception:
                        continue
    except Exception:
        pass

    # Direct checkbox (not nested in Incapsula)
    for sel in checkbox_selectors:
        try:
            element = page.locator(sel).first
            if await element.is_visible(timeout=iframe_timeout_ms):
                box = await element.bounding_box()
                if box and box["width"] > 0:
                    x = box["x"] + 30
                    y = box["y"] + 36
                    await page.mouse.click(x, y)
                    logger.info(
                        "Clicked hCaptcha checkbox at (%.0f, %.0f) via: %s",
                        x,
                        y,
                        sel,
                    )
                    return True
                else:
                    logger.debug(
                        "hCaptcha checkbox iframe (%s) has zero-size bounding box "
                        "— likely hidden (challenge mode)",
                        sel,
                    )
        except Exception as exc:
            logger.debug("hCaptcha selector %s failed: %s", sel, exc)
            continue

    # --- Phase 2: Detect if an image challenge popup is already showing ---
    # When the site auto-triggers hCaptcha, the checkbox is hidden and the
    # challenge popup iframe appears directly. We can't solve image puzzles,
    # but we should log it clearly so the agent knows what's happening.
    challenge_sel = 'iframe[title="hCaptcha challenge"]'
    try:
        challenge = page.locator(challenge_sel).first
        if await challenge.is_visible(timeout=500):
            logger.warning(
                "hCaptcha image challenge popup is visible — this requires "
                "visual puzzle solving which cannot be automated via clicking. "
                "The checkbox was skipped by the site."
            )
            return False
    except Exception:
        pass

    logger.debug("No hCaptcha iframe found with any selector")
    return False


async def try_click_recaptcha(page: Page) -> bool:
    """Attempt to click a reCAPTCHA checkbox via CDP.

    Port of SeleniumBase's __gui_click_recaptcha.
    Detects both visible and invisible reCAPTCHA. Skips invisible ones
    (bottom-right corner placement) since they don't have a clickable checkbox.

    Returns True if a click was attempted, False if no reCAPTCHA found.
    """
    selector = 'iframe[title="reCAPTCHA"]'
    try:
        element = page.locator(selector).first
        if not await element.is_visible(timeout=500):
            return False

        box = await element.bounding_box()
        if not box:
            return False

        # SeleniumBase heuristic: if the iframe is in the bottom-right corner
        # (x > 1040, y > 640), it's probably the invisible reCAPTCHA badge.
        viewport = page.viewport_size
        if viewport and box["x"] > 1040 and box["y"] > 640:
            x_dist = abs(viewport["width"] - box["x"])
            y_dist = abs(viewport["height"] - box["y"])
            if x_dist < 110 and y_dist < 110:
                logger.debug("Skipping invisible reCAPTCHA badge")
                return False

        # Click offset for reCAPTCHA checkbox: x=26, y=35
        x = box["x"] + 26
        y = box["y"] + 35
        await page.mouse.click(x, y)
        logger.info("Clicked reCAPTCHA at (%.0f, %.0f)", x, y)
        return True

    except Exception as exc:
        logger.debug("reCAPTCHA click failed: %s", exc)
        return False


async def try_solve_captcha(page: Page) -> str | None:
    """Attempt to solve any detected CAPTCHA by clicking it.

    Tries all known CAPTCHA types in order (Turnstile → hCaptcha → reCAPTCHA).
    Returns a status message if a click was attempted, None otherwise.

    This mirrors SeleniumBase's solve_captcha() dispatch logic from sb_cdp.py.
    """
    # Check page source for CAPTCHA indicators (SeleniumBase pattern)
    try:
        source = await page.content()
    except Exception:
        return None

    # Cloudflare Turnstile detection (from _on_a_cf_turnstile_page)
    is_turnstile = (
        'data-callback="onCaptchaSuccess"' in source
        and 'title="reCAPTCHA"' not in source
    ) or any(
        marker in source
        for marker in [
            "/challenge-platform/h/b/",
            'id="challenge-widget-',
            "challenges.cloudf",
            "cf-turnstile-",
        ]
    )

    if is_turnstile:
        if await try_click_turnstile(page):
            return "Clicked Cloudflare Turnstile checkbox"
        return None

    # hCaptcha detection (from _on_an_incapsula_hcaptcha_page)
    is_hcaptcha = "data-hcaptcha-widget-id" in source or "_Incapsula_Resource" in source
    if is_hcaptcha:
        if await try_click_hcaptcha(page):
            return "Clicked hCaptcha checkbox"
        return None

    # reCAPTCHA detection (from _on_a_g_recaptcha_page)
    is_recaptcha = (
        'id="recaptcha-token"' in source
        or 'title="reCAPTCHA"' in source
        or "com/recaptcha/api.js" in source
    )
    if is_recaptcha:
        if await try_click_recaptcha(page):
            return "Clicked reCAPTCHA checkbox"
        return None

    return None
