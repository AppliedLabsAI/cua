"""Typed models for local evaluation suites and reports."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EvalExpectation(BaseModel):
    """Success criteria for an evaluation case."""

    must_succeed: bool = True
    summary_contains: list[str] = Field(default_factory=list)
    error_contains: list[str] = Field(default_factory=list)
    extracted_text_contains: list[str] = Field(default_factory=list)
    required_data_keys: list[str] = Field(default_factory=list)
    data_values_contain: dict[str, str] = Field(default_factory=dict)
    max_duration_ms: int | None = None
    max_actions: int | None = None
    min_actions: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None


class EvalCase(BaseModel):
    """One local evaluation scenario."""

    id: str
    directive: str
    profile: str = "default"
    playbook: str | None = None
    playbook_params: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, str] | None = None
    allow_private_networks: bool = False
    start_url: str | None = None
    max_steps: int = 50
    thinking: Literal["minimal", "low", "medium", "high", "xhigh"] = "high"
    output_schema: dict[str, Any] | None = None
    expect: EvalExpectation = Field(default_factory=EvalExpectation)


class EvalSuite(BaseModel):
    """A collection of evaluation cases."""

    name: str = "local-eval"
    cases: list[EvalCase] = Field(default_factory=list)


class EvalCaseResult(BaseModel):
    """Normalized result for a completed evaluation case."""

    id: str
    passed: bool
    success: bool
    mode: Literal["agent", "playbook"]
    summary: str = ""
    error: str | None = None
    duration_ms: int = 0
    actions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    recovery_used: bool = False
    data: dict[str, Any] | None = None
    extracted_texts: list[str] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)


class EvalSuiteResult(BaseModel):
    """Aggregate report for a full evaluation suite."""

    name: str
    passed: int
    failed: int
    total: int
    pass_rate: float
    case_results: list[EvalCaseResult] = Field(default_factory=list)
