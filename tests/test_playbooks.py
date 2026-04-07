"""Tests for the playbook system — schema, store, parser, and runner."""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from playbooks.output import collect_step_extracted_texts
from playbooks.params import bind_step_params, materialize_playbook
from playbooks.parser import DirectiveParser
from playbooks.recovery import StepRecoveryPolicy
from playbooks.schema import (
    ApiRequestConfig,
    ApiResponseConfig,
    AuthSuccessCriteria,
    CookieCapture,
    Playbook,
    PlaybookAuthConfig,
    PlaybookCaptureConfig,
    PlaybookGuardrails,
    PlaybookParameter,
    PlaybookStep,
    SelectorStrategy,
    StepResult,
    StepVerification,
    StorageCapture,
)
from playbooks.store import PlaybookStore

# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSelectorStrategy:
    def test_all_selectors_includes_primary_and_fallbacks(self):
        s = SelectorStrategy(
            primary="text=Submit",
            fallbacks=["button.submit", "role=button[name=Submit]"],
        )
        assert s.all_selectors == [
            "text=Submit",
            "button.submit",
            "role=button[name=Submit]",
        ]

    def test_all_selectors_primary_only(self):
        s = SelectorStrategy(primary=".btn")
        assert s.all_selectors == [".btn"]


class TestPlaybookSchema:
    def test_defaults(self):
        pb = Playbook(id="test", name="Test")
        assert pb.auth_required is True
        assert pb.auth_config.mode == "form_login"
        assert pb.steps == []
        assert pb.tags == []
        assert pb.parameters == []
        assert pb.start_url == ""
        assert pb.capture.static_headers == {}
        assert pb.guardrails.has_overrides() is False

    def test_step_defaults(self):
        step = PlaybookStep(action="click")
        assert step.on_failure == "llm_recover"
        assert step.failure_message == ""
        assert step.selector is None
        assert step.verify is None
        assert step.request is None

    def test_scroll_is_not_a_valid_playbook_action(self):
        with pytest.raises(ValidationError):
            PlaybookStep(action="scroll")

    def test_guardrails_to_runtime_config(self):
        guardrails = PlaybookGuardrails(allow_private_networks=True)
        runtime = guardrails.to_runtime_config()
        assert runtime.allow_private_networks is True

    def test_auth_config_uses_explicit_config_when_present(self):
        pb = Playbook(
            id="manual",
            name="Manual",
            auth_required=True,
            auth=PlaybookAuthConfig(mode="manual", login_url="https://login.example"),
        )
        assert pb.auth_config.mode == "manual"
        assert pb.auth_config.login_url == "https://login.example"

    def test_capture_sensitive_runtime_names_include_cookies_and_storage(self):
        pb = Playbook(
            id="capture",
            name="Capture",
            capture=PlaybookCaptureConfig(
                cookies=[CookieCapture(name="session", store_as="session_cookie")],
                storage=[StorageCapture(key="token", store_as="user_token")],
            ),
        )
        assert pb.sensitive_runtime_param_names() == {"session_cookie", "user_token"}


# ---------------------------------------------------------------------------
# Store tests
# ---------------------------------------------------------------------------


class TestPlaybookStore:
    def test_load_from_yaml(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            id: test_flow
            name: Test Flow
            description: A test playbook
            tags: ["test", "flow"]
            auth_required: false
            parameters:
              - name: item_id
                type: string
                description: The item to process
                pattern: "#?(\\\\d+)"
            steps:
              - action: goto
                params:
                  url: "https://example.com"
                verify:
                  expect_url_contains: "example.com"
                description: Navigate to example
              - action: click
                selector:
                  primary: "text=Submit"
                  fallbacks:
                    - "button.submit"
                description: Click submit
        """)
        (tmp_path / "test_flow.yaml").write_text(yaml_content)

        store = PlaybookStore(tmp_path)
        pb = store.load("test_flow")

        assert pb.id == "test_flow"
        assert pb.name == "Test Flow"
        assert pb.auth_required is False
        assert len(pb.steps) == 2
        assert pb.steps[0].action == "goto"
        assert pb.steps[0].verify is not None
        assert pb.steps[0].verify.expect_url_contains == "example.com"
        assert pb.steps[1].selector is not None
        assert pb.steps[1].selector.primary == "text=Submit"
        assert pb.steps[1].selector.fallbacks == ["button.submit"]
        assert len(pb.parameters) == 1
        assert pb.parameters[0].name == "item_id"

    def test_load_caches(self, tmp_path: Path):
        yaml_content = "id: cached\nname: Cached\nsteps: []"
        (tmp_path / "cached.yaml").write_text(yaml_content)

        store = PlaybookStore(tmp_path)
        pb1 = store.load("cached")
        pb2 = store.load("cached")
        assert pb1 is pb2

    def test_load_missing_raises(self, tmp_path: Path):
        store = PlaybookStore(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.load("nonexistent")

    def test_list_all(self, tmp_path: Path):
        for name in ["a", "b", "c"]:
            (tmp_path / f"{name}.yaml").write_text(
                f"id: {name}\nname: {name.upper()}\nsteps: []"
            )

        store = PlaybookStore(tmp_path)
        playbooks = store.list_all()
        assert len(playbooks) == 3
        assert {pb.id for pb in playbooks} == {"a", "b", "c"}

    def test_list_all_empty_dir(self, tmp_path: Path):
        store = PlaybookStore(tmp_path)
        assert store.list_all() == []

    def test_match_directive(self, tmp_path: Path):
        (tmp_path / "cancel.yaml").write_text(
            'id: cancel\nname: Cancel\ntags: ["cancel", "cancel order"]\nsteps: []'
        )
        (tmp_path / "update.yaml").write_text(
            'id: update\nname: Update\ntags: ["update", "update status"]\nsteps: []'
        )

        store = PlaybookStore(tmp_path)
        result = store.match_directive("Please cancel order #123")
        assert result is not None
        assert result.id == "cancel"

    def test_match_directive_prefers_longer_tag(self, tmp_path: Path):
        (tmp_path / "generic.yaml").write_text(
            'id: generic\nname: Generic\ntags: ["order"]\nsteps: []'
        )
        (tmp_path / "specific.yaml").write_text(
            'id: specific\nname: Specific\ntags: ["cancel order"]\nsteps: []'
        )

        store = PlaybookStore(tmp_path)
        result = store.match_directive("cancel order #456")
        assert result is not None
        assert result.id == "specific"

    def test_match_directive_no_match(self, tmp_path: Path):
        (tmp_path / "cancel.yaml").write_text(
            'id: cancel\nname: Cancel\ntags: ["cancel"]\nsteps: []'
        )

        store = PlaybookStore(tmp_path)
        result = store.match_directive("do something completely unrelated")
        assert result is None

    def test_save_and_reload(self, tmp_path: Path):
        pb = Playbook(
            id="saved",
            name="Saved Flow",
            tags=["save", "test"],
            steps=[
                PlaybookStep(
                    action="goto",
                    params={"url": "https://example.com"},
                    description="Navigate",
                ),
                PlaybookStep(
                    action="click",
                    selector=SelectorStrategy(
                        primary="text=OK",
                        fallbacks=[".ok-btn"],
                    ),
                    verify=StepVerification(expect_text_on_page="Success"),
                    description="Confirm",
                ),
            ],
        )

        store = PlaybookStore(tmp_path)
        store.save(pb)

        # Clear cache and reload
        store._cache.clear()
        loaded = store.load("saved")
        assert loaded.id == "saved"
        assert loaded.name == "Saved Flow"
        assert len(loaded.steps) == 2
        assert loaded.steps[1].selector is not None
        assert loaded.steps[1].selector.fallbacks == [".ok-btn"]
        assert loaded.steps[1].verify is not None
        assert loaded.steps[1].verify.expect_text_on_page == "Success"

    def test_save_and_reload_preserves_auth_capture_and_request(self, tmp_path: Path):
        playbook = Playbook(
            id="handoff",
            name="Handoff",
            auth_required=True,
            auth=PlaybookAuthConfig(
                mode="manual",
                login_url="https://login.example.com",
                success=AuthSuccessCriteria(cookie_present="sm_session_info"),
            ),
            capture=PlaybookCaptureConfig(
                cookies=[
                    CookieCapture(
                        name="sm_session_info",
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
                        url="https://api.example.com/users",
                        headers={"X-Test": "1"},
                        response=ApiResponseConfig(
                            mode="json_path", json_path="data.0"
                        ),
                    ),
                    store_as="first_user",
                )
            ],
        )

        store = PlaybookStore(tmp_path)
        store.save(playbook)
        store._cache.clear()

        loaded = store.load("handoff")
        assert loaded.auth is not None
        assert loaded.auth.mode == "manual"
        assert loaded.capture.cookies[0].store_as == "session_cookie"
        assert loaded.steps[0].request is not None
        assert loaded.steps[0].request.response.json_path == "data.0"


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestDirectiveParser:
    def _make_store(self, tmp_path: Path) -> PlaybookStore:
        (tmp_path / "cancel_order.yaml").write_text(
            textwrap.dedent("""\
                id: cancel_order
                name: Cancel Order
                tags: ["cancel", "cancel order"]
                parameters:
                  - name: order_id
                    type: string
                    pattern: "#?(\\\\d{3,})"
                steps: []
            """)
        )
        return PlaybookStore(tmp_path)

    def test_parse_matches_and_extracts_params(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        parser = DirectiveParser(store)

        result = parser.parse("Cancel order #12345")
        assert result is not None
        playbook, params = result
        assert playbook.id == "cancel_order"
        assert params["order_id"] == "12345"

    def test_parse_no_match_returns_none(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        parser = DirectiveParser(store)

        result = parser.parse("Send an email to john@example.com")
        assert result is None

    def test_parse_extracts_int_type_param(self, tmp_path: Path):
        (tmp_path / "refund.yaml").write_text(
            textwrap.dedent("""\
                id: refund
                name: Refund
                tags: ["refund"]
                parameters:
                  - name: amount
                    type: int
                steps: []
            """)
        )
        store = PlaybookStore(tmp_path)
        parser = DirectiveParser(store)

        result = parser.parse("Process refund for $500")
        assert result is not None
        _, params = result
        assert params["amount"] == "500"

    def test_materialize_playbook_applies_inject_path(self):
        playbook = Playbook(
            id="cancel",
            name="Cancel",
            parameters=[
                PlaybookParameter(
                    name="order_id",
                    inject_into="steps.1.params.text",
                )
            ],
            steps=[
                PlaybookStep(action="goto", params={"url": "https://example.com"}),
                PlaybookStep(action="key_press", params={"text": "placeholder"}),
            ],
        )

        bound = materialize_playbook(playbook, {"order_id": "12345"})
        assert bound.steps[1].params["text"] == "12345"

    def test_materialize_playbook_binds_auth_capture_and_request_placeholders(self):
        playbook = Playbook(
            id="handoff",
            name="Handoff",
            auth_required=True,
            auth=PlaybookAuthConfig(
                mode="manual",
                login_url="https://login.example.com?email={email}",
                success=AuthSuccessCriteria(text_on_page="Welcome {email}"),
            ),
            capture=PlaybookCaptureConfig(
                static_headers={"X-Org": "{org_id}"},
                cookies=[
                    CookieCapture(name="session_{org_id}", store_as="session_cookie")
                ],
            ),
            steps=[
                PlaybookStep(
                    action="api_request",
                    request=ApiRequestConfig(
                        method="GET",
                        url="https://api.example.com/users/{user_id}",
                        query={"email": "{email}"},
                        cookies={"session": "{session_cookie}"},
                        response=ApiResponseConfig(
                            mode="json_path",
                            json_path="users.{index}.id",
                        ),
                    ),
                )
            ],
        )

        bound = materialize_playbook(
            playbook,
            {
                "email": "user@example.com",
                "org_id": "north",
                "user_id": "42",
                "session_cookie": "cookie-value",
                "index": "0",
            },
        )

        assert bound.auth is not None
        assert bound.auth.login_url.endswith("email=user@example.com")
        assert bound.auth.success is not None
        assert bound.auth.success.text_on_page == "Welcome user@example.com"
        assert bound.capture.static_headers["X-Org"] == "north"
        assert bound.capture.cookies[0].name == "session_north"
        assert bound.steps[0].request is not None
        assert bound.steps[0].request.url == "https://api.example.com/users/42"
        assert bound.steps[0].request.query["email"] == "user@example.com"
        assert bound.steps[0].request.cookies["session"] == "cookie-value"
        assert bound.steps[0].request.response.json_path == "users.0.id"

    def test_save_and_reload_preserves_store_as(self, tmp_path: Path):
        playbook = Playbook(
            id="extractor",
            name="Extractor",
            steps=[
                PlaybookStep(
                    action="extract",
                    params={"mode": "value"},
                    store_as="shop_id",
                )
            ],
        )

        store = PlaybookStore(tmp_path)
        store.save(playbook)
        store._cache.clear()

        loaded = store.load("extractor")
        assert loaded.steps[0].store_as == "shop_id"

    def test_save_and_reload_preserves_failure_message(self, tmp_path: Path):
        playbook = Playbook(
            id="fails_cleanly",
            name="Fails Cleanly",
            steps=[
                PlaybookStep(
                    action="click",
                    failure_message="Customer {email} not found",
                )
            ],
        )

        store = PlaybookStore(tmp_path)
        store.save(playbook)
        store._cache.clear()

        loaded = store.load("fails_cleanly")
        assert loaded.steps[0].failure_message == "Customer {email} not found"


# ---------------------------------------------------------------------------
# Runner parameter injection tests (no browser needed)
# ---------------------------------------------------------------------------


class TestRunnerParamInjection:
    def test_inject_params_replaces_placeholders(self):
        step = PlaybookStep(
            action="key_press",
            params={"text": "{order_id}"},
            selector=SelectorStrategy(primary="input[data-id='{order_id}']"),
            verify=StepVerification(expect_text_on_page="Order {order_id}"),
            description="Type order {order_id}",
            failure_message="Order {order_id} could not be processed",
        )

        result = bind_step_params(step, {"order_id": "12345"})
        assert result.params["text"] == "12345"
        assert result.selector is not None
        assert result.selector.primary == "input[data-id='12345']"
        assert result.verify is not None
        assert result.verify.expect_text_on_page == "Order 12345"
        assert result.description == "Type order 12345"
        assert result.failure_message == "Order 12345 could not be processed"

    def test_inject_params_no_params_returns_same(self):
        step = PlaybookStep(action="click", description="Click button")
        result = bind_step_params(step, {})
        assert result.action == "click"
        assert result.description == "Click button"

    def test_inject_params_replaces_api_request_placeholders(self):
        step = PlaybookStep(
            action="api_request",
            request=ApiRequestConfig(
                method="GET",
                url="https://api.example.com/users/{user_id}",
                query={"email": "{email}"},
                cookies={"session": "{session_cookie}"},
                response=ApiResponseConfig(
                    mode="json_path", json_path="users.{index}.id"
                ),
            ),
        )

        result = bind_step_params(
            step,
            {
                "user_id": "42",
                "email": "user@example.com",
                "session_cookie": "cookie-value",
                "index": "0",
            },
        )

        assert result.request is not None
        assert result.request.url == "https://api.example.com/users/42"
        assert result.request.query["email"] == "user@example.com"
        assert result.request.cookies["session"] == "cookie-value"
        assert result.request.response.json_path == "users.0.id"


class _FakeExecutor:
    def __init__(self, results):
        self.results = list(results)

    async def execute_step(self, step, page):
        return self.results.pop(0)


class TestRunnerFailurePolicy:
    def test_abort_does_not_retry(self):
        executor = _FakeExecutor(
            [
                StepResult(
                    step_index=0,
                    action="click",
                    success=False,
                    error="boom",
                )
            ]
        )
        policy = StepRecoveryPolicy(
            browser=SimpleNamespace(page=object()),
            recording=None,
            executor=executor,
            retry_delay_s=0,
        )

        step = PlaybookStep(action="click", on_failure="abort")
        result = asyncio.run(
            policy.run(
                playbook=Playbook(id="p", name="P"),
                step=step,
                remaining_steps=[step],
                page=object(),
            )
        )
        assert result.success is False
        assert result.recovery_used is False

    def test_retry_does_not_use_llm_handoff(self, monkeypatch):
        monkeypatch.setattr("playbooks.recovery.RETRY_DELAY_S", 0)

        executor = _FakeExecutor(
            [
                StepResult(
                    step_index=0,
                    action="click",
                    success=False,
                    error="boom",
                ),
                StepResult(
                    step_index=0,
                    action="click",
                    success=False,
                    error="still boom",
                ),
            ]
        )

        async def _should_not_run(**kwargs):
            raise AssertionError("LLM handoff should not run for retry mode")

        policy = StepRecoveryPolicy(
            browser=SimpleNamespace(page=object()),
            recording=None,
            executor=executor,
            retry_delay_s=0,
            handoff_runner=_should_not_run,
        )

        step = PlaybookStep(action="click", on_failure="retry")
        result = asyncio.run(
            policy.run(
                playbook=Playbook(id="p", name="P"),
                step=step,
                remaining_steps=[step],
                page=object(),
            )
        )
        assert result.success is False
        assert result.error == "still boom"

    def test_llm_extract_hands_off_without_retrying_same_step(self):
        executor_calls = 0

        class _FailingLLMExecutor:
            async def execute_step(self, step, page):
                nonlocal executor_calls
                executor_calls += 1
                return StepResult(
                    step_index=0,
                    action=step.action,
                    success=False,
                    error="boom",
                )

        async def _handoff_runner(**kwargs):
            return StepResult(
                step_index=0,
                action="llm_handoff",
                success=True,
                extracted_text="Recovered",
            )

        policy = StepRecoveryPolicy(
            browser=SimpleNamespace(page=object()),
            recording=None,
            executor=_FailingLLMExecutor(),
            retry_delay_s=0,
            handoff_runner=_handoff_runner,
        )

        step = PlaybookStep(action="llm_extract", on_failure="llm_recover")
        result = asyncio.run(
            policy.run(
                playbook=Playbook(id="p", name="P"),
                step=step,
                remaining_steps=[step],
                page=object(),
            )
        )

        assert executor_calls == 1
        assert result.success is True
        assert result.recovery_used is True

    def test_llm_handoff_failure_surfaces_recovery_error_with_step_context(self):
        executor = _FakeExecutor(
            [
                StepResult(
                    step_index=0,
                    action="click",
                    success=False,
                    error="boom",
                ),
                StepResult(
                    step_index=0,
                    action="click",
                    success=False,
                    error="still boom",
                ),
            ]
        )

        async def _failed_handoff(**kwargs):
            return StepResult(
                step_index=0,
                action="llm_handoff",
                success=False,
                error="LLM handoff failed: Unknown tool name",
                input_tokens=5,
                output_tokens=3,
                session_memory="handoff-mem",
            )

        policy = StepRecoveryPolicy(
            browser=SimpleNamespace(page=object()),
            recording=None,
            executor=executor,
            retry_delay_s=0,
            handoff_runner=_failed_handoff,
        )

        result = asyncio.run(
            policy.run(
                playbook=Playbook(id="p", name="P"),
                step=PlaybookStep(action="click", on_failure="llm_recover"),
                remaining_steps=[PlaybookStep(action="click")],
                page=object(),
            )
        )

        assert result.success is False
        assert result.action == "llm_handoff"
        assert result.error == (
            "step error: still boom; "
            "recovery error: LLM handoff failed: Unknown tool name"
        )
        assert result.recovery_used is True
        assert result.input_tokens == 5
        assert result.output_tokens == 3
        assert result.session_memory == "handoff-mem"

    def test_handoff_directive_redacts_sensitive_runtime_params(self):
        from playbooks.recovery import build_handoff_directive

        playbook = Playbook(
            id="handoff",
            name="Handoff",
            capture=PlaybookCaptureConfig(
                cookies=[CookieCapture(name="session", store_as="session_cookie")]
            ),
        )

        directive = build_handoff_directive(
            playbook=playbook,
            remaining_steps=[PlaybookStep(action="click", description="Click next")],
            error="boom",
            page_url="https://example.com",
            runtime_params={"session_cookie": "secret", "customer_id": "42"},
        )

        assert "session_cookie = [redacted]" in directive
        assert "customer_id = 42" in directive


class TestPlaybookOutputHelpers:
    def test_collect_step_extracted_texts_filters_successful_texts(self):
        step_results = [
            StepResult(
                step_index=0,
                action="extract",
                success=True,
                extracted_text="first",
            ),
            StepResult(
                step_index=1,
                action="extract",
                success=False,
                extracted_text="ignored failure",
            ),
            StepResult(
                step_index=2,
                action="click",
                success=True,
                extracted_text=None,
            ),
            StepResult(
                step_index=3,
                action="llm_handoff",
                success=True,
                extracted_text="second",
            ),
        ]

        assert collect_step_extracted_texts(step_results) == ["first", "second"]


class TestApiRequestExecution:
    def test_execute_api_request_merges_default_headers_and_extracts_json_path(
        self, monkeypatch
    ):
        import httpx

        from guardrails import GuardrailConfig
        from playbooks.http import execute_api_request

        captured: dict[str, object] = {}

        class _FakeAsyncClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def request(self, **kwargs):
                captured.update(kwargs)
                return httpx.Response(
                    200,
                    json={"users": [{"id": "42"}]},
                    request=httpx.Request(kwargs["method"], kwargs["url"]),
                )

        monkeypatch.setattr("playbooks.http.httpx.AsyncClient", _FakeAsyncClient)

        result = asyncio.run(
            execute_api_request(
                ApiRequestConfig(
                    method="GET",
                    url="https://api.example.com/users",
                    query={"email": "user@example.com"},
                    headers={"X-Customer": "123"},
                    cookies={"session": "cookie-value"},
                    response=ApiResponseConfig(
                        mode="json_path", json_path="users.0.id"
                    ),
                ),
                guardrail_config=GuardrailConfig(),
                default_headers={"FFF-Auth": "V1.1"},
            )
        )

        assert result == "42"
        assert captured["headers"] == {
            "FFF-Auth": "V1.1",
            "X-Customer": "123",
        }
        assert captured["cookies"] == {"session": "cookie-value"}


class TestRunnerStepOutputs:
    def test_executor_uses_custom_failure_message_for_verification_error(self):
        from playbooks.executor import PlaybookStepExecutor

        class _MissingPage:
            url = "https://example.com"

            async def wait_for_selector(self, *args, **kwargs):
                raise RuntimeError("missing")

        executor = PlaybookStepExecutor()
        result = asyncio.run(
            executor.execute_step(
                PlaybookStep(
                    action="goto",
                    verify=StepVerification(expect_element_visible="table"),
                    failure_message="Customer search returned no visible results",
                ),
                _MissingPage(),
            )
        )

        assert result.success is False
        assert result.error == "Customer search returned no visible results"

    def test_executor_verifies_against_active_browser_page(self, monkeypatch):
        from playbooks.executor import PlaybookStepExecutor

        stale_page = SimpleNamespace(url="https://example.com/customer-profile/123")
        active_page = SimpleNamespace(url="https://example.com/invoices/456")

        class _FakeBrowser:
            def __init__(self) -> None:
                self.page = stale_page
                self.wait_calls = 0

            async def wait_for_active_page(self) -> None:
                self.wait_calls += 1

        browser = _FakeBrowser()
        executor = PlaybookStepExecutor(browser=browser)

        async def _fake_run_action(step, page) -> None:
            browser.page = active_page

        monkeypatch.setattr(executor, "_run_action", _fake_run_action)

        result = asyncio.run(
            executor.execute_step(
                PlaybookStep(
                    action="click",
                    verify=StepVerification(expect_url_contains="/invoices/"),
                ),
                stale_page,
            )
        )

        assert result.success is True
        assert browser.wait_calls == 1

    def test_llm_extract_output_is_not_cached_when_verification_fails(
        self, monkeypatch
    ):
        from playbooks.executor import PlaybookStepExecutor

        executor = PlaybookStepExecutor()

        async def _fake_execute_page_action(page, action, params, config):
            return SimpleNamespace(text="Page content")

        async def _fake_llm_analyze(page_content, *, prompt="", directive=""):
            return "Extracted answer", 5, 3

        monkeypatch.setattr(
            "playbooks.executor.execute_page_action",
            _fake_execute_page_action,
        )
        monkeypatch.setattr(executor, "_llm_analyze", _fake_llm_analyze)

        result = asyncio.run(
            executor.execute_step(
                PlaybookStep(
                    action="llm_extract",
                    verify=StepVerification(expect_url_contains="/missing"),
                ),
                SimpleNamespace(url="https://example.com"),
            )
        )

        assert result.success is False
        assert result.extracted_text is None
        assert executor._last_extracted is None

    def test_extracted_output_is_available_to_later_steps(self):
        from playbooks.runner import PlaybookRunner

        observed_urls: list[str] = []

        class _OutputExecutor:
            async def execute_step(self, step, page):
                if step.action == "extract":
                    return StepResult(
                        step_index=0,
                        action=step.action,
                        success=True,
                        extracted_text="42",
                    )
                observed_urls.append(step.params["url"])
                return StepResult(
                    step_index=0,
                    action=step.action,
                    success=True,
                )

        runner = PlaybookRunner(
            browser=SimpleNamespace(page=object()),
            step_executor=_OutputExecutor(),
        )

        playbook = Playbook(
            id="shop-hours",
            name="Shop Hours",
            steps=[
                PlaybookStep(
                    action="extract",
                    params={"mode": "value"},
                    store_as="shop_id",
                ),
                PlaybookStep(
                    action="goto",
                    params={"url": "https://example.com/shop/{shop_id}"},
                ),
            ],
        )

        result = asyncio.run(runner.execute(playbook))

        assert result.success is True
        assert observed_urls == ["https://example.com/shop/42"]

    def test_runner_accumulates_token_usage_and_structured_output_costs(
        self, monkeypatch
    ):
        from playbooks.runner import PlaybookRunner

        class _TokenExecutor:
            def __init__(self) -> None:
                self.calls = 0

            async def execute_step(self, step, page):
                self.calls += 1
                if self.calls == 1:
                    return StepResult(
                        step_index=0,
                        action=step.action,
                        success=True,
                        extracted_text="Promo code was not applied.",
                        input_tokens=11,
                        output_tokens=7,
                    )
                return StepResult(
                    step_index=0,
                    action=step.action,
                    success=True,
                )

        async def _fake_extract_structured_data(*args, **kwargs):
            return {"summary": "Promo code was not applied."}, 3, 2

        monkeypatch.setattr(
            "playbooks.runner.extract_structured_data",
            _fake_extract_structured_data,
        )

        runner = PlaybookRunner(
            browser=SimpleNamespace(page=object()),
            step_executor=_TokenExecutor(),
            output_schema={"type": "object"},
        )
        playbook = Playbook(
            id="promo",
            name="Promo",
            steps=[
                PlaybookStep(action="extract", store_as="promo_summary"),
                PlaybookStep(action="goto", params={"url": "https://example.com"}),
            ],
        )

        result = asyncio.run(runner.execute(playbook))

        assert result.success is True
        assert result.total_input_tokens == 14
        assert result.total_output_tokens == 9
        assert result.extracted_texts == ["Promo code was not applied."]
        assert result.data == {"summary": "Promo code was not applied."}

    def test_runner_includes_failed_step_result_on_abort(self):
        from playbooks.runner import PlaybookRunner

        class _FailExecutor:
            async def execute_step(self, step, page):
                return StepResult(
                    step_index=0,
                    action=step.action,
                    success=False,
                    error="boom",
                    description=step.description,
                )

        runner = PlaybookRunner(
            browser=SimpleNamespace(page=object()),
            step_executor=_FailExecutor(),
        )
        playbook = Playbook(
            id="fails",
            name="Fails",
            steps=[
                PlaybookStep(
                    action="click",
                    description="Click broken button",
                    on_failure="abort",
                )
            ],
        )

        result = asyncio.run(runner.execute(playbook))

        assert result.success is False
        assert len(result.step_results) == 1
        assert result.step_results[0].success is False
        assert result.step_results[0].error == "boom"

    def test_runner_waits_for_active_page_between_steps(self):
        from playbooks.runner import PlaybookRunner

        class _PassExecutor:
            async def execute_step(self, step, page):
                return StepResult(
                    step_index=0,
                    action=step.action,
                    success=True,
                )

        class _FakeBrowser:
            def __init__(self) -> None:
                self.page = object()
                self.wait_calls = 0

            async def wait_for_active_page(self) -> None:
                self.wait_calls += 1

        browser = _FakeBrowser()
        runner = PlaybookRunner(browser=browser, step_executor=_PassExecutor())
        playbook = Playbook(
            id="waits",
            name="Waits",
            steps=[
                PlaybookStep(action="goto", params={"url": "https://example.com"}),
                PlaybookStep(action="click"),
            ],
        )

        result = asyncio.run(runner.execute(playbook))

        assert result.success is True
        assert browser.wait_calls == 4

    def test_runner_uses_configurable_step_sleep(self, monkeypatch):
        from playbooks.runner import PlaybookRunner

        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        class _PassExecutor:
            async def execute_step(self, step, page):
                return StepResult(
                    step_index=0,
                    action=step.action,
                    success=True,
                )

        monkeypatch.setattr("playbooks.runner.asyncio.sleep", _fake_sleep)

        runner = PlaybookRunner(
            browser=SimpleNamespace(page=object()),
            step_executor=_PassExecutor(),
            step_sleep_seconds=0.05,
        )
        playbook = Playbook(
            id="delays",
            name="Delays",
            steps=[
                PlaybookStep(action="goto", params={"url": "https://example.com"}),
                PlaybookStep(action="click"),
            ],
        )

        result = asyncio.run(runner.execute(playbook))

        assert result.success is True
        assert sleep_calls == [0.05, 0.05]

    def test_runner_emits_step_results_via_callback(self):
        from playbooks.runner import PlaybookRunner

        emitted: list[tuple[int, str, bool]] = []

        class _PassExecutor:
            async def execute_step(self, step, page):
                return StepResult(
                    step_index=0,
                    action=step.action,
                    success=True,
                )

        runner = PlaybookRunner(
            browser=SimpleNamespace(page=object()),
            step_executor=_PassExecutor(),
            on_step_result=lambda step_index, step, result: emitted.append(
                (step_index, step.action, result.success)
            ),
        )
        playbook = Playbook(
            id="callbacks",
            name="Callbacks",
            steps=[
                PlaybookStep(action="goto", params={"url": "https://example.com"}),
                PlaybookStep(action="click"),
            ],
        )

        result = asyncio.run(runner.execute(playbook))

        assert result.success is True
        assert emitted == [(0, "goto", True), (1, "click", True)]


class TestPlaybookActionLogs:
    def test_api_request_action_log_redacts_sensitive_values(self):
        from playbooks.actionlog import build_playbook_action_log

        log = build_playbook_action_log(
            step_index=0,
            step=PlaybookStep(
                action="api_request",
                request=ApiRequestConfig(
                    method="POST",
                    url="https://api.example.com/invoices",
                    headers={"Authorization": "Bearer secret-token"},
                    cookies={"session": "cookie-secret"},
                    json_body={"customerId": "123"},
                    response=ApiResponseConfig(mode="json"),
                ),
            ),
            result=StepResult(
                step_index=0,
                action="api_request",
                success=True,
                extracted_text='{"ok": true}',
            ),
            sensitive_values={"Bearer secret-token", "cookie-secret"},
        )

        payload = json.dumps(log.model_dump())

        assert log.input_summary == "POST https://api.example.com/invoices"
        assert log.tool_input["header_names"] == ["Authorization"]
        assert log.tool_input["cookie_names"] == ["session"]
        assert "cookie-secret" not in payload
        assert "Bearer secret-token" not in payload

    def test_key_press_action_log_redacts_typed_text(self):
        from playbooks.actionlog import build_playbook_action_log

        log = build_playbook_action_log(
            step_index=1,
            step=PlaybookStep(
                action="key_press",
                params={"text": "super-secret-password", "key": "Enter"},
                selector=SelectorStrategy(primary="input[type='password']"),
            ),
            result=StepResult(
                step_index=1,
                action="key_press",
                success=True,
            ),
            sensitive_values={"super-secret-password"},
        )

        payload = json.dumps(log.model_dump())

        assert log.input_summary == "type into 'input[type='password']' + press Enter"
        assert log.tool_input["text"] == "[redacted]"
        assert "super-secret-password" not in payload
