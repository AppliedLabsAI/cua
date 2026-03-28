"""Tests for the local evaluation suite loader and scorer."""

from __future__ import annotations

import json

import pytest

from evaluation.models import EvalCase, EvalCaseResult, EvalExpectation, EvalSuiteResult
from evaluation.runner import load_suite, score_case_result, write_suite_report


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
  - id: playbook-basic
    directive: Cancel order 123
    playbook: cancel_order
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
        assert suite.cases[0].expect.summary_contains == ["title"]
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
            ),
        )
        result = EvalCaseResult(
            id="ok",
            passed=False,
            success=True,
            mode="agent",
            summary="Dashboard totals loaded",
            duration_ms=120,
            actions=2,
            input_tokens=80,
            output_tokens=30,
            data={"total": 42, "details": {"title": "Dashboard Totals"}},
            extracted_texts=["Order count: 42"],
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
            ),
        )
        result = EvalCaseResult(
            id="fail",
            passed=False,
            success=False,
            mode="agent",
            summary="Could not load dashboard",
            error="network unavailable",
            duration_ms=250,
            actions=3,
            input_tokens=40,
            output_tokens=20,
            data={"message": "missing"},
            extracted_texts=["nothing useful"],
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
        ]


class TestWriteSuiteReport:
    @pytest.mark.asyncio
    async def test_writes_json_report(self, tmp_path):
        report = EvalSuiteResult(
            name="smoke",
            passed=1,
            failed=0,
            total=1,
            pass_rate=1.0,
            case_results=[
                EvalCaseResult(
                    id="ok",
                    passed=True,
                    success=True,
                    mode="agent",
                    summary="Completed",
                )
            ],
        )
        report_path = tmp_path / "reports" / "eval.json"

        await write_suite_report(report, report_path)

        payload = json.loads(report_path.read_text())
        assert payload["name"] == "smoke"
        assert payload["case_results"][0]["id"] == "ok"
