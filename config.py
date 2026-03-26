"""Centralized configuration for the CUA agent.

Consolidates runtime settings from environment variables, API RunConfig,
and profile overrides into a single typed structure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from exceptions import ConfigError
from guardrails import GuardrailConfig
from profiles.loader import apply_guardrail_overrides, load_profile
from settings import AGENT_MODEL, get_settings

if TYPE_CHECKING:
    from api.models import RunConfig
    from profiles.loader import Profile


@dataclass
class CUAConfig:
    """Complete runtime configuration for a CUA agent run."""

    directive: str
    model: str = AGENT_MODEL
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
        env = get_settings()

        if not env.directive:
            raise ConfigError("DIRECTIVE env var is required")

        credentials = None
        if env.credentials_json:
            credentials = json.loads(env.credentials_json)

        guardrail_config = None
        if env.guardrails_json:
            guardrail_config = GuardrailConfig.from_dict(
                json.loads(env.guardrails_json)
            )

        profile = load_profile(env.profile)
        guardrail_config = apply_guardrail_overrides(profile, guardrail_config)

        return cls(
            directive=env.directive,
            model=env.model,
            max_steps=env.max_steps,
            thinking_budget=env.thinking_budget,
            width=env.width,
            height=env.height,
            start_url=env.start_url or None,
            proxy_url=env.proxy_url or None,
            profile_name=env.profile,
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
