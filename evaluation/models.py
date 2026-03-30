"""Typed models for local evaluation suites and reports."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ExecutionMode(StrEnum):
    """Supported execution modes for evaluation cases."""

    HYBRID_AUTO = "hybrid_auto"
    PLAYBOOK_ONLY = "playbook_only"
    AGENT_ONLY = "agent_only"


class ObservedMode(StrEnum):
    """Observed mode for a completed trial or case result."""

    AGENT = "agent"
    PLAYBOOK = "playbook"
    HYBRID = "hybrid"


class HandoffExpectation(StrEnum):
    """Expected handoff behavior for a benchmark case."""

    ALLOW = "allow"
    REQUIRE = "require"
    FORBID = "forbid"


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
    handoff: HandoffExpectation = HandoffExpectation.ALLOW
    max_estimated_cost_usd: float | None = None
    min_trial_pass_rate: float | None = None
    max_p95_duration_ms: int | None = None


class EvalCase(BaseModel):
    """One local evaluation scenario."""

    id: str
    directive: str
    profile: str = "default"
    playbook: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.HYBRID_AUTO
    trials: int = 1
    benchmark_tags: list[str] = Field(default_factory=list)
    playbook_params: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, str] | None = None
    allow_private_networks: bool = False
    start_url: str | None = None
    max_steps: int = 50
    thinking: Literal["minimal", "low", "medium", "high", "xhigh"] = "high"
    output_schema: dict[str, Any] | None = None
    input_token_cost_per_million_usd: float | None = None
    output_token_cost_per_million_usd: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    expect: EvalExpectation = Field(default_factory=EvalExpectation)


class EvalSuite(BaseModel):
    """A collection of evaluation cases."""

    name: str = "local-eval"
    cases: list[EvalCase] = Field(default_factory=list)


class _EvalResultBase(BaseModel):
    """Shared fields for trial and case-level evaluation results."""

    id: str
    passed: bool
    success: bool
    mode: ObservedMode
    requested_mode: ExecutionMode = ExecutionMode.HYBRID_AUTO
    summary: str = ""
    error: str | None = None
    duration_ms: int = 0
    actions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    recovery_used: bool = False
    playbook_hit: bool = False
    handoff_occurred: bool = False
    handoff_succeeded: bool = False
    estimated_cost_usd: float = 0.0
    benchmark_tags: list[str] = Field(default_factory=list)
    data: dict[str, Any] | None = None
    extracted_texts: list[str] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)


class EvalTrialResult(_EvalResultBase):
    """Normalized result for a single benchmark trial."""

    trial_index: int = 1


class EvalCaseResult(_EvalResultBase):
    """Aggregate result for a benchmark case, including repeated trials."""

    trials_run: int = 1
    trial_pass_rate: float = 1.0
    avg_duration_ms: float = 0.0
    p95_duration_ms: int = 0
    avg_estimated_cost_usd: float = 0.0
    trial_results: list[EvalTrialResult] = Field(default_factory=list)


class EvalSuiteResult(BaseModel):
    """Aggregate report for a full evaluation suite."""

    name: str
    passed: int
    failed: int
    total: int
    pass_rate: float
    avg_duration_ms: float = 0.0
    p95_duration_ms: int = 0
    avg_estimated_cost_usd: float = 0.0
    deterministic_hit_rate: float = 0.0
    handoff_rate: float = 0.0
    handoff_rescue_rate: float = 0.0
    case_results: list[EvalCaseResult] = Field(default_factory=list)
