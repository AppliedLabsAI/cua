"""Typed request/response models for the CUA API."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from settings import PRIMARY_MODEL


class RunStatusValue(StrEnum):
    """Lifecycle status of a CUA run."""

    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    TERMINATED = "terminated"


CredentialsMap = dict[str, str]


class RecordingSettings(BaseModel):
    """API-facing representation of recording configuration."""

    enabled: bool = True
    trace: bool = True


class GuardrailSettings(BaseModel):
    """API-facing representation of guardrail configuration."""

    allowed_domains: list[str] | None = None
    blocked_domains: list[str] | None = None
    max_urls_visited: int = 50
    max_consecutive_errors: int = 5
    allow_private_networks: bool = False
    enable_llm_action_check: bool = True


class ActionEvent(BaseModel):
    """Serializable action event returned by the status API."""

    step: int
    timestamp: str
    tool: str
    action: str
    input_summary: str
    duration_ms: int
    success: bool
    result_text: str | None = None
    has_screenshot: bool = False
    error: str | None = None


class RunConfig(BaseModel):
    """POST /runs request body."""

    directive: str = Field(..., max_length=10_000)
    model: str = PRIMARY_MODEL
    max_steps: int = Field(default=50, ge=1, le=200)
    timeout_seconds: int = Field(default=600, ge=30, le=3600)
    thinking: Literal["minimal", "low", "medium", "high", "xhigh"] = "high"
    display_width: int = 1280
    display_height: int = 720
    profile: str = "default"
    start_url: str | None = None
    credentials: CredentialsMap | None = None
    proxy: str | None = None
    guardrails: GuardrailSettings | None = None
    recording: RecordingSettings | None = None
    output_schema: dict[str, Any] | None = None


class RunResponse(BaseModel):
    """POST /runs response."""

    run_id: str
    status: RunStatusValue = RunStatusValue.STARTING
    status_url: str
    stream_url: str


class RunStatus(BaseModel):
    """GET /runs/{run_id} response."""

    run_id: str
    status: RunStatusValue
    action_count: int = 0
    actions: list[ActionEvent] = Field(default_factory=list)
    result: str | None = None
    error: str | None = None
    duration_ms: int | None = None
    data: dict[str, Any] | None = None
    extracted_texts: list[str] = Field(default_factory=list)
