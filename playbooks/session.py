"""Session artifact capture for authenticated playbook runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from playbooks.schema import PlaybookCaptureConfig

if TYPE_CHECKING:
    from bridge.browser import BrowserManager

_READ_STORAGE_ITEM_JS = """([scope, key]) => {
    try {
        const storage = scope === "session" ? window.sessionStorage : window.localStorage;
        if (!storage) return null;
        return storage.getItem(key);
    } catch (_) {
        return null;
    }
}"""


def _cookie_domain_matches(cookie_domain: str, expected_domain: str) -> bool:
    actual = cookie_domain.lstrip(".").lower()
    expected = expected_domain.lstrip(".").lower()
    return actual == expected or actual.endswith(f".{expected}")


async def capture_session_artifacts(
    browser: BrowserManager,
    capture: PlaybookCaptureConfig,
) -> dict[str, str]:
    """Capture allowlisted cookies/storage values from the active browser context."""
    artifacts = dict(capture.static_headers)

    if capture.cookies:
        cookies = await browser.context.cookies()
        for item in capture.cookies:
            value = None
            for cookie in cookies:
                if cookie.get("name") != item.name:
                    continue
                if item.domain and not _cookie_domain_matches(
                    cookie.get("domain", ""), item.domain
                ):
                    continue
                value = cookie.get("value")
                if value:
                    break
            if value is None:
                domain_msg = f" for domain {item.domain}" if item.domain else ""
                raise RuntimeError(
                    f"Required cookie '{item.name}' not found{domain_msg}"
                )
            artifacts[item.store_as] = value

    if capture.storage:
        page = browser.page
        for item in capture.storage:
            value = await page.evaluate(_READ_STORAGE_ITEM_JS, [item.scope, item.key])
            if value is None:
                raise RuntimeError(
                    f"Required {item.scope} storage key '{item.key}' not found"
                )
            artifacts[item.store_as] = value

    return artifacts
