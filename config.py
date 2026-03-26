"""Centralized configuration for the CUA agent.

Consolidates runtime settings from environment variables, API RunConfig,
and profile overrides into a single typed structure.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from exceptions import ConfigError
from guardrails import GuardrailConfig
from profiles.loader import apply_guardrail_overrides, load_profile

if TYPE_CHECKING:
    from api.models import RunConfig
    from profiles.loader import Profile


@dataclass
class CUAConfig:
    """Complete runtime configuration for a CUA agent run."""

    directive: str
    model: str = "claude-sonnet-4-6"
    max_steps: int = 50
    thinking_budget: int = 4096
    width: int = 1920
    height: int = 1080
    start_url: str | None = None
    proxy_url: str | None = None
    profile_name: str = "default"
    credentials: dict | None = None
    guardrail_config: GuardrailConfig | None = None
    profile: Profile | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> CUAConfig:
        """Build config from environment variables (used inside sandbox)."""
        directive = os.environ.get("DIRECTIVE", "")
        if not directive:
            raise ConfigError("DIRECTIVE env var is required")

        credentials = None
        creds_json = os.environ.get("CREDENTIALS_JSON")
        if creds_json:
            credentials = json.loads(creds_json)

        guardrail_config = None
        guardrails_json = os.environ.get("GUARDRAILS_JSON")
        if guardrails_json:
            guardrail_config = GuardrailConfig.from_dict(json.loads(guardrails_json))

        profile_name = os.environ.get("PROFILE", "default")
        profile = load_profile(profile_name)
        guardrail_config = apply_guardrail_overrides(profile, guardrail_config)

        return cls(
            directive=directive,
            model=os.environ.get("MODEL", "claude-sonnet-4-6"),
            max_steps=int(os.environ.get("MAX_STEPS", "50")),
            thinking_budget=int(os.environ.get("THINKING_BUDGET", "4096")),
            width=int(os.environ.get("WIDTH", "1920")),
            height=int(os.environ.get("HEIGHT", "1080")),
            start_url=os.environ.get("START_URL") or None,
            proxy_url=os.environ.get("PROXY_URL") or None,
            profile_name=profile_name,
            credentials=credentials,
            guardrail_config=guardrail_config,
            profile=profile,
        )

    @classmethod
    def from_run_config(cls, rc: RunConfig) -> CUAConfig:
        """Build config from an API RunConfig (used by the outer API)."""
        guardrail_config = None
        if rc.guardrails:
            guardrail_config = GuardrailConfig.from_dict(rc.guardrails)

        profile = load_profile(rc.profile)
        guardrail_config = apply_guardrail_overrides(profile, guardrail_config)

        return cls(
            directive=rc.directive,
            model=rc.model,
            max_steps=rc.max_steps,
            thinking_budget=rc.thinking_budget,
            width=rc.display_width,
            height=rc.display_height,
            start_url=rc.start_url,
            proxy_url=rc.proxy,
            profile_name=rc.profile,
            credentials=rc.credentials,
            guardrail_config=guardrail_config,
            profile=profile,
        )
