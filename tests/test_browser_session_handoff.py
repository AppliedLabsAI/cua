"""Integration test for browser-login to API-session handoff."""

from __future__ import annotations

import multiprocessing
import time

import httpx
import pytest
import uvicorn
from patchright.async_api import async_playwright

from playbooks.auth import DashboardAuth
from playbooks.params import materialize_playbook
from playbooks.runner import PlaybookRunner
from playbooks.schema import (
    ApiRequestConfig,
    ApiResponseConfig,
    AuthSuccessCriteria,
    CookieCapture,
    Playbook,
    PlaybookAuthConfig,
    PlaybookCaptureConfig,
    PlaybookStep,
)

pytestmark = pytest.mark.integration


def _run_server(port: int) -> None:
    from tests.fixtures.session_handoff_server import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


@pytest.fixture(scope="module")
def server_port():
    port = 18924
    proc = multiprocessing.Process(target=_run_server, args=(port,), daemon=True)
    proc.start()
    for _ in range(20):
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/login", timeout=1)
            if response.status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    yield port
    proc.terminate()
    proc.join(timeout=3)


class _BrowserWrapper:
    def __init__(self, page, context) -> None:
        self.page = page
        self.context = context

    async def wait_for_active_page(self) -> None:
        return None


@pytest.mark.asyncio
async def test_browser_login_can_capture_cookie_and_reuse_it_for_api_calls(
    server_port,
):
    base_url = f"http://127.0.0.1:{server_port}"
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment-specific
            pytest.skip(f"Patchright browser launch unavailable: {exc}")
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        wrapped_browser = _BrowserWrapper(page, context)

        playbook = Playbook(
            id="invoice_handoff",
            name="Invoice Handoff",
            auth_required=True,
            auth=PlaybookAuthConfig(
                mode="form_login",
                login_url="{base_url}/login",
                success=AuthSuccessCriteria(cookie_present="user_session"),
            ),
            capture=PlaybookCaptureConfig(
                cookies=[
                    CookieCapture(
                        name="user_session",
                        store_as="session_cookie",
                    )
                ],
                static_headers={"FFF-Auth": "V1.1"},
            ),
            steps=[
                PlaybookStep(
                    action="api_request",
                    request=ApiRequestConfig(
                        method="GET",
                        url="{base_url}/api/invoices",
                        query={"email": "{customer_email}"},
                        cookies={"user_session": "{session_cookie}"},
                        response=ApiResponseConfig(mode="json"),
                    ),
                    description="Fetch invoice details for the customer",
                )
            ],
        )
        playbook = materialize_playbook(playbook, {"base_url": base_url})

        auth = DashboardAuth(
            wrapped_browser,
            credentials={"email": "agent@example.com", "password": "hunter2"},
        )
        assert await auth.ensure_authenticated(playbook) is True

        artifacts = await auth.capture_session_artifacts(playbook)
        assert artifacts["FFF-Auth"] == "V1.1"
        assert artifacts["session_cookie"] == "session-123"

        runner = PlaybookRunner(browser=wrapped_browser)
        result = await runner.execute(
            playbook,
            {"customer_email": "alice@example.com", **artifacts},
        )

        assert result.success is True
        assert result.extracted_text is not None
        assert "alice@example.com" in result.extracted_text
        assert "19.99" in result.extracted_text

        await browser.close()
