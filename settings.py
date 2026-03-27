"""Centralized settings: model constants and environment configuration.

Single source of truth for model names and environment variables.
Import from here instead of scattering os.environ.get() calls and
hardcoded model strings across the codebase.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings

# ---------------------------------------------------------------------------
# Model constants — change here to switch everywhere
# ---------------------------------------------------------------------------

AGENT_MODEL = "claude-sonnet-4-6"
SAFETY_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Timeout constants (milliseconds unless noted) — change here to tune globally
# ---------------------------------------------------------------------------

ACTION_TIMEOUT_MS = 5_000  # clicks, fills, selects
NAVIGATION_TIMEOUT_MS = 7_000  # page loads / goto
LOGIN_TIMEOUT_MS = 15_000  # auth login flow (longer for SSO redirects)
SELECTOR_PROBE_TIMEOUT_MS = 800  # wait for a selector to appear
SETTLE_TIMEOUT_MS = 3_000  # DOM stabilization after actions
SETTLE_SLEEP_S = 0.3  # sleep between DOM stability checks


# ---------------------------------------------------------------------------
# Environment-backed settings (reads from env vars automatically)
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Environment variables used by the CUA agent.

    Pydantic Settings reads env vars matching the field names
    (case-insensitive by default). Provide defaults for optional vars.
    """

    model_config = {"populate_by_name": True}

    # Anthropic API
    anthropic_api_key: str = ""

    # Agent runtime
    directive: str = ""
    model: str = AGENT_MODEL
    max_steps: int = 50
    thinking_budget: int = 4096
    width: int = 1920
    height: int = 1080
    start_url: str = ""
    proxy_url: str = ""
    profile: str = "default"
    credentials_json: str = ""
    guardrails_json: str = ""
    recording_json: str = ""

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
