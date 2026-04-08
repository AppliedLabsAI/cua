"""Tests for the stealth evasion module.

Verifies the SeleniumBase-ported techniques are correctly structured
without requiring a running browser.
"""

from __future__ import annotations

import pytest


class TestStealthLaunchArgs:
    """Test Chrome launch argument generation."""

    def test_includes_automation_controlled(self):
        """SeleniumBase's key anti-detection flag should be present."""
        from bridge.stealth import get_stealth_launch_args

        args = get_stealth_launch_args(1280, 720)
        assert "--disable-blink-features=AutomationControlled" in args

    def test_includes_window_size(self):
        from bridge.stealth import get_stealth_launch_args

        args = get_stealth_launch_args(1920, 1080)
        assert "--window-size=1920,1080" in args

    def test_includes_no_first_run(self):
        from bridge.stealth import get_stealth_launch_args

        args = get_stealth_launch_args(1280, 720)
        assert "--no-first-run" in args

    def test_includes_disable_features(self):
        """SeleniumBase feature flags should be present."""
        from bridge.stealth import get_stealth_launch_args

        args = get_stealth_launch_args(1280, 720)
        disable_features = [a for a in args if a.startswith("--disable-features=")]
        assert len(disable_features) == 1
        assert "UserAgentClientHint" in disable_features[0]
        assert "IsolateOrigins" in disable_features[0]

    def test_superset_of_old_defaults(self):
        """Stealth args should include all previous CUA default args."""
        from bridge.stealth import get_stealth_launch_args

        args = set(get_stealth_launch_args(1280, 720))
        assert "--window-position=0,0" in args
        assert "--no-default-browser-check" in args
        assert "--disable-infobars" in args


class TestStealthJS:
    """Test JavaScript stealth payload."""

    def test_loads(self):
        from bridge.stealth import get_stealth_js

        js = get_stealth_js()
        assert isinstance(js, str)
        assert len(js) > 100

    def test_contains_webdriver_patch(self):
        from bridge.stealth import get_stealth_js

        js = get_stealth_js()
        assert "Navigator.prototype" in js

    def test_contains_chrome_runtime(self):
        from bridge.stealth import get_stealth_js

        js = get_stealth_js()
        assert "chrome.runtime" in js

    def test_contains_cdc_removal(self):
        from bridge.stealth import get_stealth_js

        js = get_stealth_js()
        assert "cdc" in js.lower()

    def test_contains_webgl_patch(self):
        from bridge.stealth import get_stealth_js

        js = get_stealth_js()
        assert "WebGLRenderingContext" in js

    def test_contains_plugins_patch(self):
        from bridge.stealth import get_stealth_js

        js = get_stealth_js()
        assert "plugins" in js
        assert "PDF" in js

    def test_is_iife(self):
        from bridge.stealth import get_stealth_js

        js = get_stealth_js().strip()
        assert "(() => {" in js
        assert js.endswith("})();")

    def test_exported_in_js_helpers(self):
        from bridge.js_helpers import STEALTH_EVASIONS_INIT_JS

        assert isinstance(STEALTH_EVASIONS_INIT_JS, str)
        assert len(STEALTH_EVASIONS_INIT_JS) > 100


class TestCaptchaSolverSelectors:
    """Test CAPTCHA selector cascades from SeleniumBase."""

    def test_turnstile_selectors_defined(self):
        import inspect

        from bridge.stealth import try_click_turnstile

        source = inspect.getsource(try_click_turnstile)
        assert "cf-turnstile" in source
        assert "challenge-form" in source

    def test_hcaptcha_selectors_defined(self):
        import inspect

        from bridge.stealth import try_click_hcaptcha

        source = inspect.getsource(try_click_hcaptcha)
        assert "hcaptcha-widget-id" in source
        assert "Incapsula_Resource" in source

    def test_recaptcha_skips_invisible(self):
        import inspect

        from bridge.stealth import try_click_recaptcha

        source = inspect.getsource(try_click_recaptcha)
        assert "1040" in source  # Invisible reCAPTCHA corner check

    def test_solve_dispatches_all_types(self):
        import inspect

        from bridge.stealth import try_solve_captcha

        source = inspect.getsource(try_solve_captcha)
        assert "turnstile" in source.lower()
        assert "hcaptcha" in source.lower()
        assert "recaptcha" in source.lower()


class TestCaptchaAlignmentFix:
    """Test the Turnstile CSS alignment fix from SeleniumBase."""

    def test_alignment_fix_js(self):
        from bridge.captcha_solver import TURNSTILE_ALIGNMENT_FIX_JS

        assert "replaceAll" in TURNSTILE_ALIGNMENT_FIX_JS
        assert "center" in TURNSTILE_ALIGNMENT_FIX_JS
        assert "left" in TURNSTILE_ALIGNMENT_FIX_JS


class TestBrowserManager:
    """Test that BrowserManager has stealth capabilities built in."""

    def test_has_solve_captcha(self):
        from bridge.browser import BrowserManager

        browser = BrowserManager()
        assert hasattr(browser, "solve_captcha")
        assert callable(browser.solve_captcha)

    def test_has_launch(self):
        from bridge.browser import BrowserManager

        browser = BrowserManager()
        assert hasattr(browser, "launch")

    def test_page_raises_before_launch(self):
        from bridge.browser import BrowserManager

        browser = BrowserManager()
        with pytest.raises(RuntimeError, match="not launched"):
            _ = browser.page
