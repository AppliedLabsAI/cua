"""Pure helpers for evaluation scoring and aggregation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import mean
from typing import cast

from evaluation.models import (
    EvalCase,
    EvalCaseResult,
    EvalSuiteResult,
    EvalTrialResult,
    HandoffExpectation,
    ObservedMode,
)


def _lookup_data_path(data: dict | None, path: str) -> object | None:
    """Resolve a dotted path like ``details.title`` from nested result data."""
    current: object | None = data
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def estimate_cost_usd(
    case: EvalCase,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate cost from optional per-million token pricing."""
    input_cost = case.input_token_cost_per_million_usd
    output_cost = case.output_token_cost_per_million_usd
    if input_cost is None and output_cost is None:
        return 0.0
    return (input_tokens / 1_000_000) * float(input_cost or 0.0) + (
        output_tokens / 1_000_000
    ) * float(output_cost or 0.0)


def percentile_95(values: Sequence[int | float]) -> int:
    """Return the nearest-rank p95 for a non-empty list."""
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return int(ordered[index])


def _score_common_expectations(
    case: EvalCase,
    result: EvalTrialResult | EvalCaseResult,
    *,
    failed_checks: list[str],
) -> None:
    """Apply expectations that are shared by trial and aggregate results."""
    expect = case.expect

    if expect.must_succeed and not result.success:
        failed_checks.append("must_succeed")
    if expect.summary_contains:
        lower_summary = result.summary.lower()
        if not all(
            needle.lower() in lower_summary for needle in expect.summary_contains
        ):
            failed_checks.append("summary_contains")
    if expect.error_contains:
        lower_error = (result.error or "").lower()
        if not all(needle.lower() in lower_error for needle in expect.error_contains):
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
    if expect.handoff is HandoffExpectation.REQUIRE and not result.handoff_occurred:
        failed_checks.append("handoff")
    if expect.handoff is HandoffExpectation.FORBID and result.handoff_occurred:
        failed_checks.append("handoff")
    if (
        expect.max_estimated_cost_usd is not None
        and result.estimated_cost_usd > expect.max_estimated_cost_usd
    ):
        failed_checks.append("max_estimated_cost_usd")


def score_case_result(
    case: EvalCase,
    result: EvalTrialResult | EvalCaseResult,
) -> EvalTrialResult | EvalCaseResult:
    """Apply expectations and mark a trial or aggregate result as passed/failed."""
    failed_checks: list[str] = []
    _score_common_expectations(case, result, failed_checks=failed_checks)
    if isinstance(result, EvalCaseResult):
        if (
            case.expect.min_trial_pass_rate is not None
            and result.trial_pass_rate < case.expect.min_trial_pass_rate
        ):
            failed_checks.append("min_trial_pass_rate")
        if (
            case.expect.max_p95_duration_ms is not None
            and result.p95_duration_ms > case.expect.max_p95_duration_ms
        ):
            failed_checks.append("max_p95_duration_ms")

    result.failed_checks = failed_checks
    result.passed = not failed_checks
    return result


def _case_result_as_trial(result: EvalCaseResult) -> EvalTrialResult:
    """Down-project a case result into a single trial summary."""
    shared = result.model_dump(include=set(EvalTrialResult.model_fields.keys()))
    shared["trial_index"] = 1
    return EvalTrialResult.model_validate(shared)


def aggregate_case_results(
    case: EvalCase,
    trial_results: list[EvalTrialResult],
) -> EvalCaseResult:
    """Aggregate repeated trial results into a case-level benchmark result."""
    if not trial_results:
        raise ValueError("trial_results must not be empty")

    durations = [trial.duration_ms for trial in trial_results]
    actions = [trial.actions for trial in trial_results]
    input_tokens = [trial.input_tokens for trial in trial_results]
    output_tokens = [trial.output_tokens for trial in trial_results]
    estimated_costs = [trial.estimated_cost_usd for trial in trial_results]
    passed_trials = sum(1 for trial in trial_results if trial.passed)
    trial_pass_rate = passed_trials / len(trial_results)
    aggregate_success = (
        trial_pass_rate >= case.expect.min_trial_pass_rate
        if case.expect.min_trial_pass_rate is not None
        else all(trial.success for trial in trial_results)
    )
    unique_modes = {trial.mode for trial in trial_results}
    avg_duration = mean(durations)
    avg_cost = mean(estimated_costs)
    aggregate = EvalCaseResult(
        id=case.id,
        passed=False,
        success=aggregate_success,
        mode=next(iter(unique_modes))
        if len(unique_modes) == 1
        else ObservedMode.HYBRID,
        requested_mode=case.execution_mode,
        summary=(
            trial_results[0].summary
            if len(trial_results) == 1
            else f"{passed_trials}/{len(trial_results)} trials passed"
        ),
        error=next((trial.error for trial in trial_results if trial.error), None),
        duration_ms=int(round(avg_duration)),
        actions=int(round(mean(actions))),
        input_tokens=int(round(mean(input_tokens))),
        output_tokens=int(round(mean(output_tokens))),
        recovery_used=any(trial.recovery_used for trial in trial_results),
        playbook_hit=all(trial.playbook_hit for trial in trial_results),
        handoff_occurred=any(trial.handoff_occurred for trial in trial_results),
        handoff_succeeded=any(trial.handoff_succeeded for trial in trial_results),
        estimated_cost_usd=avg_cost,
        benchmark_tags=list(case.benchmark_tags),
        data=next(
            (trial.data for trial in reversed(trial_results) if trial.data), None
        ),
        extracted_texts=trial_results[0].extracted_texts,
        trials_run=len(trial_results),
        trial_pass_rate=trial_pass_rate,
        avg_duration_ms=avg_duration,
        p95_duration_ms=percentile_95(durations),
        avg_estimated_cost_usd=avg_cost,
        trial_results=trial_results if len(trial_results) > 1 else [],
    )

    if len(trial_results) == 1:
        aggregate.failed_checks = list(trial_results[0].failed_checks)
        aggregate.passed = trial_results[0].passed
        return aggregate

    aggregate = cast(EvalCaseResult, score_case_result(case, aggregate))
    if case.expect.min_trial_pass_rate is None and passed_trials != len(trial_results):
        aggregate.failed_checks.append("trial_pass_rate")
        aggregate.failed_checks = list(dict.fromkeys(aggregate.failed_checks))
        aggregate.passed = False
    return aggregate


def summarize_suite_results(
    name: str,
    case_results: list[EvalCaseResult],
) -> EvalSuiteResult:
    """Build suite-level metrics from case results."""
    passed = sum(1 for result in case_results if result.passed)
    trial_results = [
        trial
        for result in case_results
        for trial in (result.trial_results or [_case_result_as_trial(result)])
    ]
    durations = [trial.duration_ms for trial in trial_results]
    costs = [trial.estimated_cost_usd for trial in trial_results]
    handoffs = [trial for trial in trial_results if trial.handoff_occurred]
    total = len(case_results)
    return EvalSuiteResult(
        name=name,
        passed=passed,
        failed=total - passed,
        total=total,
        pass_rate=(passed / total) if total else 0.0,
        avg_duration_ms=mean(durations) if durations else 0.0,
        p95_duration_ms=percentile_95(durations),
        avg_estimated_cost_usd=mean(costs) if costs else 0.0,
        deterministic_hit_rate=(
            sum(1 for trial in trial_results if trial.playbook_hit) / len(trial_results)
            if trial_results
            else 0.0
        ),
        handoff_rate=(len(handoffs) / len(trial_results) if trial_results else 0.0),
        handoff_rescue_rate=(
            sum(1 for trial in handoffs if trial.handoff_succeeded) / len(handoffs)
            if handoffs
            else 0.0
        ),
        case_results=case_results,
    )
