"""Tests for the local evaluation suite loader and scorer."""

from __future__ import annotations

import json

import pytest

from evaluation.models import (
    EvalCase,
    EvalCaseResult,
    EvalExpectation,
    EvalSuiteResult,
    EvalTrialResult,
    ExecutionMode,
    HandoffExpectation,
    ObservedMode,
)
from evaluation.runner import load_suite, write_suite_report
from evaluation.scoring import aggregate_case_results, score_case_result


class TestLoadSuite:
    @pytest.mark.asyncio
    async def test_loads_yaml_suite(self, tmp_path):
        suite_path = tmp_path / "suite.yaml"
        suite_path.write_text(
            """
name: smoke
cases:
  - id: agent-basic
    directive: Find the page title
    start_url: https://example.com
    execution_mode: agent_only
    trials: 3
    benchmark_tags: [public, smoke]
    output_schema:
      type: object
      properties:
        details:
          type: object
          properties:
            title:
              type: string
    expect:
      summary_contains: ["title"]
      max_actions: 3
      min_trial_pass_rate: 0.66
  - id: playbook-basic
    directive: Cancel order 123
    playbook: cancel_order
    execution_mode: playbook_only
"""
        )

        suite = await load_suite(suite_path)

        assert suite.name == "smoke"
        assert [case.id for case in suite.cases] == ["agent-basic", "playbook-basic"]
        assert suite.cases[0].output_schema is not None
        assert (
            suite.cases[0].output_schema["properties"]["details"]["properties"][
                "title"
            ]["type"]
            == "string"
        )
        assert suite.cases[0].execution_mode is ExecutionMode.AGENT_ONLY
        assert suite.cases[0].trials == 3
        assert suite.cases[0].benchmark_tags == ["public", "smoke"]
        assert suite.cases[0].expect.min_trial_pass_rate == pytest.approx(0.66)
        assert suite.cases[0].expect.summary_contains == ["title"]
        assert suite.cases[1].execution_mode is ExecutionMode.PLAYBOOK_ONLY
        assert suite.cases[1].playbook == "cancel_order"


class TestScoreCaseResult:
    def test_marks_case_passed_when_all_expectations_match(self):
        case = EvalCase(
            id="ok",
            directive="Check dashboard totals",
            expect=EvalExpectation(
                summary_contains=["totals"],
                extracted_text_contains=["42"],
                required_data_keys=["total", "details.title"],
                data_values_contain={"details.title": "Dashboard"},
                max_duration_ms=250,
                min_actions=1,
                max_actions=3,
                max_input_tokens=100,
                max_output_tokens=50,
                handoff=HandoffExpectation.FORBID,
                max_estimated_cost_usd=0.01,
            ),
            execution_mode=ExecutionMode.AGENT_ONLY,
            input_token_cost_per_million_usd=10,
            output_token_cost_per_million_usd=20,
        )
        result = EvalTrialResult(
            id="ok",
            trial_index=1,
            passed=False,
            success=True,
            mode=ObservedMode.AGENT,
            requested_mode=ExecutionMode.AGENT_ONLY,
            summary="Dashboard totals loaded",
            duration_ms=120,
            actions=2,
            input_tokens=80,
            output_tokens=30,
            data={"total": 42, "details": {"title": "Dashboard Totals"}},
            extracted_texts=["Order count: 42"],
            estimated_cost_usd=0.0014,
        )

        scored = score_case_result(case, result)

        assert scored.passed is True
        assert scored.failed_checks == []

    def test_marks_case_failed_for_multiple_mismatches(self):
        case = EvalCase(
            id="fail",
            directive="Collect order data",
            expect=EvalExpectation(
                summary_contains=["orders"],
                error_contains=["timeout"],
                extracted_text_contains=["invoice"],
                required_data_keys=["orders", "details.title"],
                data_values_contain={"details.title": "Example"},
                max_duration_ms=100,
                max_actions=1,
                min_actions=1,
                max_input_tokens=10,
                max_output_tokens=10,
                handoff=HandoffExpectation.REQUIRE,
                max_estimated_cost_usd=0.0001,
            ),
            execution_mode=ExecutionMode.HYBRID_AUTO,
        )
        result = EvalTrialResult(
            id="fail",
            trial_index=1,
            passed=False,
            success=False,
            mode=ObservedMode.AGENT,
            requested_mode=ExecutionMode.HYBRID_AUTO,
            summary="Could not load dashboard",
            error="network unavailable",
            duration_ms=250,
            actions=3,
            input_tokens=40,
            output_tokens=20,
            data={"message": "missing"},
            extracted_texts=["nothing useful"],
            estimated_cost_usd=0.005,
        )

        scored = score_case_result(case, result)

        assert scored.passed is False
        assert scored.failed_checks == [
            "must_succeed",
            "summary_contains",
            "error_contains",
            "extracted_text_contains",
            "required_data_keys",
            "data_values_contain",
            "max_duration_ms",
            "max_actions",
            "max_input_tokens",
            "max_output_tokens",
            "handoff",
            "max_estimated_cost_usd",
        ]


class TestAggregateCaseResults:
    def test_aggregates_trials_and_applies_trial_level_expectations(self):
        case = EvalCase(
            id="repeatable",
            directive="Check dashboard totals",
            execution_mode=ExecutionMode.AGENT_ONLY,
            trials=3,
            benchmark_tags=["public"],
            expect=EvalExpectation(
                min_trial_pass_rate=0.66,
                max_p95_duration_ms=220,
            ),
        )
        trial_results = [
            EvalTrialResult(
                id="repeatable",
                trial_index=1,
                passed=True,
                success=True,
                mode=ObservedMode.AGENT,
                requested_mode=ExecutionMode.AGENT_ONLY,
                duration_ms=100,
                estimated_cost_usd=0.001,
                benchmark_tags=["public"],
            ),
            EvalTrialResult(
                id="repeatable",
                trial_index=2,
                passed=False,
                success=False,
                mode=ObservedMode.AGENT,
                requested_mode=ExecutionMode.AGENT_ONLY,
                duration_ms=180,
                error="timeout",
                estimated_cost_usd=0.002,
                benchmark_tags=["public"],
                failed_checks=["must_succeed"],
            ),
            EvalTrialResult(
                id="repeatable",
                trial_index=3,
                passed=True,
                success=True,
                mode=ObservedMode.AGENT,
                requested_mode=ExecutionMode.AGENT_ONLY,
                duration_ms=210,
                estimated_cost_usd=0.003,
                benchmark_tags=["public"],
            ),
        ]

        aggregate = aggregate_case_results(case, trial_results)

        assert aggregate.passed is True
        assert aggregate.trials_run == 3
        assert aggregate.trial_pass_rate == pytest.approx(2 / 3)
        assert aggregate.p95_duration_ms == 210
        assert aggregate.avg_estimated_cost_usd == pytest.approx(0.002)
        assert len(aggregate.trial_results) == 3

    def test_requires_all_trials_when_no_pass_rate_threshold_is_set(self):
        case = EvalCase(
            id="strict",
            directive="Run strict benchmark",
            execution_mode=ExecutionMode.AGENT_ONLY,
            trials=2,
        )
        trial_results = [
            EvalTrialResult(
                id="strict",
                trial_index=1,
                passed=True,
                success=True,
                mode=ObservedMode.AGENT,
                requested_mode=ExecutionMode.AGENT_ONLY,
            ),
            EvalTrialResult(
                id="strict",
                trial_index=2,
                passed=False,
                success=False,
                mode=ObservedMode.AGENT,
                requested_mode=ExecutionMode.AGENT_ONLY,
                failed_checks=["must_succeed"],
            ),
        ]

        aggregate = aggregate_case_results(case, trial_results)

        assert aggregate.passed is False
        assert "trial_pass_rate" in aggregate.failed_checks


class TestWriteSuiteReport:
    @pytest.mark.asyncio
    async def test_writes_json_report(self, tmp_path):
        report = EvalSuiteResult(
            name="smoke",
            passed=1,
            failed=0,
            total=1,
            pass_rate=1.0,
            avg_duration_ms=125.0,
            p95_duration_ms=125,
            avg_estimated_cost_usd=0.001,
            case_results=[
                EvalCaseResult(
                    id="ok",
                    passed=True,
                    success=True,
                    mode=ObservedMode.AGENT,
                    requested_mode=ExecutionMode.AGENT_ONLY,
                    summary="Completed",
                    benchmark_tags=["smoke"],
                )
            ],
        )
        report_path = tmp_path / "reports" / "eval.json"

        await write_suite_report(report, report_path)

        payload = json.loads(report_path.read_text())
        assert payload["name"] == "smoke"
        assert payload["avg_duration_ms"] == 125.0
        assert payload["case_results"][0]["id"] == "ok"
