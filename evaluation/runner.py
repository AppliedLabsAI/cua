"""Local evaluation suite loader and runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml

from agent.memory import SessionMemory
from evaluation.models import (
    EvalCase,
    EvalCaseResult,
    EvalSuite,
    EvalTrialResult,
    ExecutionMode,
    ObservedMode,
)
from evaluation.runtime import trial_runtime
from evaluation.scoring import (
    aggregate_case_results,
    estimate_cost_usd,
    score_case_result,
    summarize_suite_results,
)
from settings import PRIMARY_MODEL

if TYPE_CHECKING:
    from evaluation.models import EvalSuiteResult


async def load_suite(path: str | Path) -> EvalSuite:
    """Load an evaluation suite from YAML without blocking the event loop."""
    content = await asyncio.to_thread(Path(path).read_text)
    raw = yaml.safe_load(content) or {}
    return EvalSuite.model_validate(raw)


async def _run_playbook_case(
    case: EvalCase,
    output_root: Path,
    *,
    trial_index: int,
    run_id: str,
    display: str,
    width: int,
    height: int,
) -> EvalTrialResult:
    async with trial_runtime(
        run_id=run_id,
        output_root=output_root,
        display=display,
        width=width,
        height=height,
        start_url=case.start_url,
    ) as runtime:
        from agent.output import playbook_result_to_output
        from playbooks.auth import DashboardAuth
        from playbooks.runner import PlaybookRunner
        from playbooks.store import PlaybookStore

        store = PlaybookStore()
        playbook = store.load(case.playbook or "")
        if playbook.auth_required:
            from credentials import resolve_credentials

            auth = DashboardAuth(
                runtime.browser,
                resolve_credentials(case.credentials) or {},
            )
            login_url = playbook.start_url or case.start_url or ""
            await auth.ensure_authenticated(login_url)

        runner = PlaybookRunner(
            runtime.browser,
            runtime.recording,
            output_schema=case.output_schema,
        )
        eval_params = dict(case.playbook_params or {})
        eval_params.setdefault("directive", case.directive or "")
        raw = await runner.execute(playbook, eval_params)
        output = playbook_result_to_output(raw)
        extracted = [sr.extracted_text for sr in raw.step_results if sr.extracted_text]
        recovery_used = any(sr.recovery_used for sr in raw.step_results)
        return EvalTrialResult(
            id=case.id,
            trial_index=trial_index,
            passed=False,
            success=raw.success,
            mode=ObservedMode.HYBRID if recovery_used else ObservedMode.PLAYBOOK,
            requested_mode=case.execution_mode,
            summary=output.summary,
            error=raw.error,
            duration_ms=raw.total_duration_ms,
            actions=len(raw.step_results),
            input_tokens=0,
            output_tokens=0,
            recovery_used=recovery_used,
            playbook_hit=raw.success and not recovery_used,
            handoff_occurred=recovery_used,
            handoff_succeeded=recovery_used and raw.success,
            estimated_cost_usd=0.0,
            benchmark_tags=list(case.benchmark_tags),
            data=output.data,
            extracted_texts=extracted,
        )


async def _run_agent_case(
    case: EvalCase,
    output_root: Path,
    *,
    trial_index: int,
    run_id: str,
    model: str,
    display: str,
    width: int,
    height: int,
) -> EvalTrialResult:
    from agent.loop import run_agent
    from agent.output import agent_result_to_output
    from blinders.filters import DOMBlinders
    from blinders.scope import extract_task_scope
    from bridge.router import ActionRouter
    from profiles.loader import apply_guardrail_overrides, load_profile

    async with trial_runtime(
        run_id=run_id,
        output_root=output_root,
        display=display,
        width=width,
        height=height,
        start_url=case.start_url,
    ) as runtime:
        profile = load_profile(case.profile)
        guardrail_config = apply_guardrail_overrides(profile)
        if case.allow_private_networks:
            guardrail_config.allow_private_networks = True

        scope = await extract_task_scope(case.directive, profile, case.start_url)
        blinders = DOMBlinders(scope)

        session_memory = SessionMemory()
        bridge = ActionRouter(
            browser=runtime.browser,
            guardrail_config=guardrail_config,
            blinders=blinders,
            directive=case.directive,
            session_memory=session_memory,
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
            session_memory=session_memory,
        )
        output = agent_result_to_output(raw)
        return EvalTrialResult(
            id=case.id,
            trial_index=trial_index,
            passed=False,
            success=raw.success,
            mode=ObservedMode.AGENT,
            requested_mode=case.execution_mode,
            summary=output.summary,
            error=raw.error,
            duration_ms=raw.total_duration_ms,
            actions=raw.action_count,
            input_tokens=raw.total_input_tokens,
            output_tokens=raw.total_output_tokens,
            recovery_used=False,
            playbook_hit=False,
            handoff_occurred=False,
            handoff_succeeded=False,
            estimated_cost_usd=estimate_cost_usd(
                case,
                raw.total_input_tokens,
                raw.total_output_tokens,
            ),
            benchmark_tags=list(case.benchmark_tags),
            data=output.data,
            extracted_texts=raw.extracted_texts,
        )


async def _run_trial(
    case: EvalCase,
    output_root: Path,
    *,
    trial_index: int,
    model: str,
    display: str,
    width: int,
    height: int,
) -> EvalTrialResult:
    """Run one trial in the requested execution mode."""
    run_id = case.id if case.trials == 1 else f"{case.id}/trial-{trial_index:02d}"
    if case.execution_mode is ExecutionMode.PLAYBOOK_ONLY:
        if not case.playbook:
            raise ValueError("playbook_only evaluation cases require a playbook")
        return await _run_playbook_case(
            case,
            output_root,
            trial_index=trial_index,
            run_id=run_id,
            display=display,
            width=width,
            height=height,
        )
    if case.execution_mode is ExecutionMode.AGENT_ONLY:
        return await _run_agent_case(
            case,
            output_root,
            trial_index=trial_index,
            run_id=run_id,
            model=model,
            display=display,
            width=width,
            height=height,
        )
    if case.playbook:
        return await _run_playbook_case(
            case,
            output_root,
            trial_index=trial_index,
            run_id=run_id,
            display=display,
            width=width,
            height=height,
        )
    return await _run_agent_case(
        case,
        output_root,
        trial_index=trial_index,
        run_id=run_id,
        model=model,
        display=display,
        width=width,
        height=height,
    )


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
    trial_results: list[EvalTrialResult] = []
    for trial_index in range(1, case.trials + 1):
        trial = await _run_trial(
            case,
            output_root=output_root,
            trial_index=trial_index,
            model=model,
            display=display,
            width=width,
            height=height,
        )
        trial_results.append(cast(EvalTrialResult, score_case_result(case, trial)))
    return aggregate_case_results(case, trial_results)


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
    return summarize_suite_results(suite.name, results)


async def write_suite_report(report: EvalSuiteResult, path: str | Path) -> None:
    """Write a suite report as JSON without blocking the event loop."""
    report_path = Path(path)
    await asyncio.to_thread(report_path.parent.mkdir, parents=True, exist_ok=True)
    payload = json.dumps(report.model_dump(), indent=2)
    await asyncio.to_thread(report_path.write_text, payload)
