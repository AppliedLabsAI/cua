"""Centralized settings: model constants and environment configuration.

Single source of truth for model names and environment variables.
Import from here instead of scattering os.environ.get() calls and
hardcoded model strings across the codebase.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings

# ---------------------------------------------------------------------------
# Model constants — change here to switch everywhere
# ---------------------------------------------------------------------------

# PRIMARY_MODEL = "google-gla:gemini-3-flash-preview"
# UTILITY_MODEL = "google-gla:gemini-3.1-flash-lite-preview"
# PRIMARY_MODEL = "anthropic:claude-sonnet-4-6"
# UTILITY_MODEL = "anthropic:claude-haiku-4-5"
PRIMARY_MODEL = "openai-responses:gpt-5.4"
UTILITY_MODEL = "openai-responses:gpt-5.4-mini"

# ---------------------------------------------------------------------------
# Timeout constants (milliseconds unless noted) — change here to tune globally
# ---------------------------------------------------------------------------

ACTION_TIMEOUT_MS = 5_000  # clicks, fills, selects
NAVIGATION_TIMEOUT_MS = 7_000  # page loads / goto
LOGIN_TIMEOUT_MS = 15_000  # auth login flow (longer for SSO redirects)
SELECTOR_PROBE_TIMEOUT_MS = 800  # wait for a selector to appear
LOGIN_DETECT_TIMEOUT_MS = (
    1_500  # wait for login form elements (more generous than probe)
)
PAGE_SETTLE_TIMEOUT_MS = 8_000  # networkidle wait after navigation actions


# ---------------------------------------------------------------------------
# Environment-backed settings (reads from env vars automatically)
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Environment variables used by the CUA agent.

    Pydantic Settings reads env vars matching the field names
    (case-insensitive by default). Provide defaults for optional vars.
    """

    model_config = {"populate_by_name": True}

    environment: str = "local"

    # Agent runtime
    directive: str = ""
    model: str = PRIMARY_MODEL
    max_steps: int = 50
    thinking: Literal["minimal", "low", "medium", "high", "xhigh"] = "high"
    width: int = 1920
    height: int = 1080
    start_url: str = ""
    proxy_url: str = ""
    profile: str = "default"
    credentials_json: str = ""
    guardrails_json: str = ""
    recording_json: str = ""
    output_schema_json: str = ""

    # Infrastructure
    modal_sandbox_id: str = "local"
    cua_api_key: str = ""

    # OpenTelemetry (standard env var names)
    otel_sdk_disabled: bool = True
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_exporter_otlp_protocol: str = "grpc"
    otel_exporter_otlp_insecure: bool = False
    otel_resource_env: str = "local"
    otel_traces_sampler_arg: float = 1.0


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
