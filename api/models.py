"""Pydantic request/response models for the CUA API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RunStatusValue = Literal[
    "starting", "running", "completed", "failed", "timeout", "terminated"
]


class RunConfig(BaseModel):
    """POST /runs request body."""

    directive: str
    model: str = "claude-sonnet-4-6"
    max_steps: int = Field(default=50, ge=1, le=200)
    timeout_seconds: int = Field(default=600, ge=30, le=3600)
    thinking_budget: int = Field(default=4096, ge=0, le=32768)
    display_width: int = 1280
    display_height: int = 720
    profile: str = "default"
    start_url: str | None = None
    credentials: dict | None = None
    proxy: str | None = None
    guardrails: dict | None = None  # GuardrailConfig as dict, or None for defaults


class RunResponse(BaseModel):
    """POST /runs response."""

    run_id: str
    novnc_url: str
    status_url: str
    stream_url: str


class RunStatus(BaseModel):
    """GET /runs/{run_id} response."""

    run_id: str
    status: RunStatusValue
    action_count: int = 0
    actions: list[dict] = Field(default_factory=list)
    result: str | None = None
    error: str | None = None
    duration_ms: int | None = None
