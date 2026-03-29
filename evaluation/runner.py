"""Local evaluation suite loader and runner."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from evaluation.models import (
    EvalCase,
    EvalCaseResult,
    EvalSuite,
    EvalSuiteResult,
)
from settings import PRIMARY_MODEL

if TYPE_CHECKING:
    from bridge.browser import BrowserManager
    from recording.manager import RecordingManager


def _lookup_data_path(data: dict | None, path: str) -> object | None:
    """Resolve a dotted path like ``details.title`` from nested result data."""
    current: object | None = data
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


async def load_suite(path: str | Path) -> EvalSuite:
    """Load an evaluation suite from YAML without blocking the event loop."""
    content = await asyncio.to_thread(Path(path).read_text)
    raw = yaml.safe_load(content) or {}
    return EvalSuite.model_validate(raw)


def _evaluate_case(case: EvalCase, result: EvalCaseResult) -> EvalCaseResult:
    """Apply expectations and mark the case as passed/failed."""
    failed_checks: list[str] = []
    expect = case.expect

    if expect.must_succeed and not result.success:
        failed_checks.append("must_succeed")
    if expect.summary_contains and not all(
        needle.lower() in result.summary.lower() for needle in expect.summary_contains
    ):
        failed_checks.append("summary_contains")
    if expect.error_contains and not all(
        needle.lower() in (result.error or "").lower()
        for needle in expect.error_contains
    ):
        failed_checks.append("error_contains")
    if expect.extracted_text_contains:
        joined = "\n".join(result.extracted_texts).lower()
        if not all(
            needle.lower() in joined for needle in expect.extracted_text_contains
        ):
            failed_checks.append("extracted_text_contains")
    if expect.required_data_keys and not all(
        _lookup_data_path(result.data, key) is not None
        for key in expect.required_data_keys
    ):
        failed_checks.append("required_data_keys")
    if expect.data_values_contain:
        for path, needle in expect.data_values_contain.items():
            value = _lookup_data_path(result.data, path)
            if value is None or needle.lower() not in str(value).lower():
                failed_checks.append("data_values_contain")
                break
    if (
        expect.max_duration_ms is not None
        and result.duration_ms > expect.max_duration_ms
    ):
        failed_checks.append("max_duration_ms")
    if expect.max_actions is not None and result.actions > expect.max_actions:
        failed_checks.append("max_actions")
    if expect.min_actions is not None and result.actions < expect.min_actions:
        failed_checks.append("min_actions")
    if (
        expect.max_input_tokens is not None
        and result.input_tokens > expect.max_input_tokens
    ):
        failed_checks.append("max_input_tokens")
    if (
        expect.max_output_tokens is not None
        and result.output_tokens > expect.max_output_tokens
    ):
        failed_checks.append("max_output_tokens")

    result.failed_checks = failed_checks
    result.passed = not failed_checks
    return result


def score_case_result(case: EvalCase, result: EvalCaseResult) -> EvalCaseResult:
    """Public wrapper for applying expectations to a normalized case result."""
    return _evaluate_case(case, result)


async def _launch_browser(
    *,
    display: str,
    width: int,
    height: int,
    start_url: str | None,
    proxy: str | None = None,
) -> BrowserManager:
    from bridge.browser import BrowserManager

    os.environ["DISPLAY"] = display
    browser = BrowserManager()
    await browser.launch(width=width, height=height, start_url=start_url, proxy=proxy)
    return browser


async def _init_recording(
    case_id: str,
    output_root: Path,
    browser: BrowserManager,
) -> RecordingManager:
    from recording import RecordingConfig
    from recording.manager import RecordingManager

    config = RecordingConfig(output_dir=str(output_root / case_id), upload=False)
    recording = RecordingManager(config, run_id=case_id)
    await recording.start(browser.context)
    return recording


async def _run_playbook_case(
    case: EvalCase,
    output_root: Path,
    *,
    display: str,
    width: int,
    height: int,
) -> EvalCaseResult:
    browser = await _launch_browser(
        display=display,
        width=width,
        height=height,
        start_url=case.start_url,
    )
    recording = await _init_recording(case.id, output_root, browser)
    try:
        from playbooks.auth import DashboardAuth
        from playbooks.runner import PlaybookRunner
        from playbooks.store import PlaybookStore

        store = PlaybookStore()
        playbook = store.load(case.playbook or "")
        if playbook.auth_required:
            from credentials import resolve_credentials

            auth = DashboardAuth(browser, resolve_credentials(case.credentials) or {})
            login_url = playbook.start_url or case.start_url or ""
            await auth.ensure_authenticated(login_url)

        runner = PlaybookRunner(browser, recording, output_schema=case.output_schema)
        raw = await runner.execute(playbook, case.playbook_params)
        extracted = [sr.extracted_text for sr in raw.step_results if sr.extracted_text]
        return EvalCaseResult(
            id=case.id,
            passed=False,
            success=raw.success,
            mode="playbook",
            summary=(raw.data or {}).get("summary", "") if raw.data else "",
            error=raw.error,
            duration_ms=raw.total_duration_ms,
            actions=len(raw.step_results),
            input_tokens=0,
            output_tokens=0,
            recovery_used=any(sr.recovery_used for sr in raw.step_results),
            data=raw.data,
            extracted_texts=extracted,
        )
    finally:
        await recording.stop()
        await browser.close()


async def _run_agent_case(
    case: EvalCase,
    output_root: Path,
    *,
    model: str,
    display: str,
    width: int,
    height: int,
) -> EvalCaseResult:
    from agent.loop import run_agent
    from agent.output import agent_result_to_output
    from blinders.filters import DOMBlinders
    from blinders.scope import extract_task_scope
    from bridge.router import ActionRouter
    from profiles.loader import apply_guardrail_overrides, load_profile

    browser = await _launch_browser(
        display=display,
        width=width,
        height=height,
        start_url=case.start_url,
    )
    recording = await _init_recording(case.id, output_root, browser)
    try:
        profile = load_profile(case.profile)
        guardrail_config = apply_guardrail_overrides(profile)
        if case.allow_private_networks:
            guardrail_config.allow_private_networks = True

        scope = await extract_task_scope(case.directive, profile)
        blinders = DOMBlinders(scope)
        bridge = ActionRouter(
            browser=browser,
            guardrail_config=guardrail_config,
            blinders=blinders,
            directive=case.directive,
            recording=recording,
        )

        from credentials import resolve_credentials

        raw = await run_agent(
            directive=case.directive,
            bridge=bridge,
            model=model,
            max_steps=case.max_steps,
            thinking=case.thinking,
            credentials=resolve_credentials(case.credentials),
            profile_prompt=profile.prompt_extension,
            allowed_actions=scope.allowed_actions,
            output_schema=case.output_schema,
        )
        output = agent_result_to_output(raw)
        return EvalCaseResult(
            id=case.id,
            passed=False,
            success=raw.success,
            mode="agent",
            summary=output.summary,
            error=raw.error,
            duration_ms=raw.total_duration_ms,
            actions=raw.action_count,
            input_tokens=raw.total_input_tokens,
            output_tokens=raw.total_output_tokens,
            recovery_used=False,
            data=output.data,
            extracted_texts=raw.extracted_texts,
        )
    finally:
        await recording.stop()
        await browser.close()


async def run_case(
    case: EvalCase,
    *,
    output_root: str | Path = "output/evals",
    model: str = PRIMARY_MODEL,
    display: str = ":99",
    width: int = 1280,
    height: int = 720,
) -> EvalCaseResult:
    """Run and score one evaluation case."""
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    if case.playbook:
        result = await _run_playbook_case(
            case,
            output_root,
            display=display,
            width=width,
            height=height,
        )
    else:
        result = await _run_agent_case(
            case,
            output_root,
            model=model,
            display=display,
            width=width,
            height=height,
        )
    return _evaluate_case(case, result)


async def run_suite(
    suite: EvalSuite,
    *,
    output_root: str | Path = "output/evals",
    model: str = PRIMARY_MODEL,
    display: str = ":99",
    width: int = 1280,
    height: int = 720,
) -> EvalSuiteResult:
    """Run a full evaluation suite serially and aggregate results."""
    results: list[EvalCaseResult] = []
    for case in suite.cases:
        results.append(
            await run_case(
                case,
                output_root=output_root,
                model=model,
                display=display,
                width=width,
                height=height,
            )
        )
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    return EvalSuiteResult(
        name=suite.name,
        passed=passed,
        failed=total - passed,
        total=total,
        pass_rate=(passed / total) if total else 0.0,
        case_results=results,
    )


async def write_suite_report(report: EvalSuiteResult, path: str | Path) -> None:
    """Write a suite report as JSON without blocking the event loop."""
    report_path = Path(path)
    await asyncio.to_thread(report_path.parent.mkdir, parents=True, exist_ok=True)
    payload = json.dumps(report.model_dump(), indent=2)
    await asyncio.to_thread(report_path.write_text, payload)
