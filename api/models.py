"""Typed request/response models for the CUA API."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from api.errors import ApiError
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

    allowed_domains: list[str] | None = Field(default=None, max_length=100)
    blocked_domains: list[str] | None = Field(default=None, max_length=100)
    max_urls_visited: int = Field(default=50, ge=1, le=500)
    max_consecutive_errors: int = Field(default=5, ge=1, le=50)
    allow_private_networks: bool = False
    enable_llm_action_check: bool = True
    stuck_window_size: int = Field(default=8, ge=2, le=50)
    stuck_repeat_hint: int = Field(default=3, ge=2, le=20)
    stuck_repeat_warn: int = Field(default=5, ge=2, le=30)
    stuck_repeat_stop: int = Field(default=7, ge=2, le=40)
    stuck_cycle_max_length: int = Field(default=3, ge=2, le=10)
    stuck_cycle_repeats: int = Field(default=3, ge=2, le=20)


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
    display_width: int = Field(default=1280, ge=800, le=3840)
    display_height: int = Field(default=720, ge=600, le=2160)
    profile: str = Field(default="default", max_length=100)
    start_url: str | None = Field(default=None, max_length=2048)
    credentials: CredentialsMap | None = None
    proxy: str | None = Field(default=None, max_length=2048)
    guardrails: GuardrailSettings | None = None
    recording: RecordingSettings | None = None
    output_schema: dict[str, Any] | None = None

    @field_validator("credentials")
    @classmethod
    def cap_credential_count(cls, v: CredentialsMap | None) -> CredentialsMap | None:
        if v is not None and len(v) > 20:
            raise ValueError("Maximum 20 credentials allowed per run")
        return v


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
    error: ApiError | None = None
    duration_ms: int | None = None
    data: dict[str, Any] | None = None
    extracted_texts: list[str] = Field(default_factory=list)
