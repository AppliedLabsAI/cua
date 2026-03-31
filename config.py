"""Centralized runtime configuration assembly for the CUA agent."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from api.models import CredentialsMap
from credentials import SecretValue, resolve_credentials
from exceptions import ConfigError
from guardrails import GuardrailConfig
from profiles.loader import Profile, apply_guardrail_overrides, load_profile
from recording import RecordingConfig
from settings import PRIMARY_MODEL, get_settings


class CUAConfig(BaseModel):
    """Complete runtime configuration for a CUA agent run."""

    model_config = {"arbitrary_types_allowed": True}

    directive: str = Field(max_length=10_000)
    model: str = PRIMARY_MODEL
    max_steps: int = Field(default=50, ge=1, le=200)
    thinking: Literal["minimal", "low", "medium", "high", "xhigh"] = "high"
    width: int = Field(default=1920, ge=800, le=3840)
    height: int = Field(default=1080, ge=600, le=2160)
    start_url: str | None = None
    proxy_url: str | None = None
    profile_name: str = "default"
    credentials: dict[str, SecretValue] | None = None
    guardrail_config: GuardrailConfig = Field(default_factory=GuardrailConfig)
    recording_config: RecordingConfig = Field(default_factory=RecordingConfig)
    output_schema: dict[str, Any] | None = None
    profile: Profile | None = Field(default=None, repr=False)

    @staticmethod
    def _parse_credentials(raw_json: str) -> dict[str, SecretValue] | None:
        if not raw_json:
            return None

        parsed = json.loads(raw_json)
        if not isinstance(parsed, dict):
            raise ConfigError("CREDENTIALS_JSON must be a JSON object")

        normalized: CredentialsMap = {str(k): str(v) for k, v in parsed.items()}
        return resolve_credentials(normalized)

    @staticmethod
    def _parse_recording(raw_json: str) -> RecordingConfig:
        if not raw_json:
            return RecordingConfig()
        return RecordingConfig.model_validate(json.loads(raw_json))

    @staticmethod
    def _parse_guardrails(raw_json: str) -> GuardrailConfig | None:
        if not raw_json:
            return None
        return GuardrailConfig.model_validate(json.loads(raw_json))

    @classmethod
    def from_env(cls) -> CUAConfig:
        """Build config from environment variables (used inside sandbox)."""
        env = get_settings()

        if not env.directive:
            raise ConfigError("DIRECTIVE env var is required")

        credentials = cls._parse_credentials(env.credentials_json)
        guardrail_config = cls._parse_guardrails(env.guardrails_json)
        recording_config = cls._parse_recording(env.recording_json)
        output_schema = None
        if env.output_schema_json:
            parsed = json.loads(env.output_schema_json)
            if not isinstance(parsed, dict):
                raise ConfigError("OUTPUT_SCHEMA_JSON must be a JSON object")
            output_schema = parsed
        profile = load_profile(env.profile)
        guardrail_config = apply_guardrail_overrides(profile, guardrail_config)

        return cls(
            directive=env.directive,
            model=env.model,
            max_steps=env.max_steps,
            thinking=env.thinking,
            width=env.width,
            height=env.height,
            start_url=env.start_url or None,
            proxy_url=env.proxy_url or None,
            profile_name=env.profile,
            credentials=credentials,
            guardrail_config=guardrail_config,
            recording_config=recording_config,
            output_schema=output_schema,
            profile=profile,
        )
