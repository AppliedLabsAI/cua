"""Evaluation helpers for running and scoring local CUA suites."""

from evaluation.models import (
    EvalCase,
    EvalCaseResult,
    EvalExpectation,
    EvalSuite,
    EvalSuiteResult,
    EvalTrialResult,
    ExecutionMode,
    HandoffExpectation,
    ObservedMode,
)
from evaluation.runner import (
    load_suite,
    run_suite,
    write_suite_report,
)
from evaluation.scoring import (
    aggregate_case_results,
    score_case_result,
)

__all__ = [
    "EvalCase",
    "EvalCaseResult",
    "EvalExpectation",
    "EvalSuite",
    "EvalSuiteResult",
    "EvalTrialResult",
    "ExecutionMode",
    "HandoffExpectation",
    "ObservedMode",
    "aggregate_case_results",
    "load_suite",
    "run_suite",
    "score_case_result",
    "write_suite_report",
]
