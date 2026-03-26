"""Centralized runtime configuration assembly for the CUA agent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from api.models import CredentialsMap, GuardrailSettings, RecordingSettings
from exceptions import ConfigError
from guardrails import GuardrailConfig
from profiles.loader import apply_guardrail_overrides, load_profile
from recording import RecordingConfig
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
    credentials: CredentialsMap | None = None
    guardrail_config: GuardrailConfig = field(default_factory=GuardrailConfig)
    recording_config: RecordingConfig = field(default_factory=RecordingConfig)
    profile: Profile | None = field(default=None, repr=False)

    @staticmethod
    def _parse_credentials(raw_json: str) -> CredentialsMap | None:
        if not raw_json:
            return None

        parsed = json.loads(raw_json)
        if not isinstance(parsed, dict):
            raise ConfigError("CREDENTIALS_JSON must be a JSON object")

        normalized: CredentialsMap = {}
        for service, creds in parsed.items():
            if not isinstance(service, str) or not isinstance(creds, dict):
                raise ConfigError(
                    "CREDENTIALS_JSON entries must map strings to objects"
                )
            normalized[service] = {str(k): str(v) for k, v in creds.items()}
        return normalized

    @staticmethod
    def _parse_recording(raw_json: str) -> RecordingConfig:
        if not raw_json:
            return RecordingConfig()

        settings = RecordingSettings.model_validate(json.loads(raw_json))
        return RecordingConfig.from_dict(settings.to_dict())

    @staticmethod
    def _parse_guardrails(raw_json: str) -> GuardrailConfig | None:
        if not raw_json:
            return None

        settings = GuardrailSettings.model_validate(json.loads(raw_json))
        return GuardrailConfig.from_dict(settings.to_dict())

    @classmethod
    def from_env(cls) -> CUAConfig:
        """Build config from environment variables (used inside sandbox)."""
        env = get_settings()

        if not env.directive:
            raise ConfigError("DIRECTIVE env var is required")

        credentials = cls._parse_credentials(env.credentials_json)
        guardrail_config = cls._parse_guardrails(env.guardrails_json)
        recording_config = cls._parse_recording(env.recording_json)
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
            recording_config=recording_config,
            profile=profile,
        )

    @classmethod
    def from_run_config(cls, rc: RunConfig) -> CUAConfig:
        """Build config from an API RunConfig (used by the outer API)."""
        guardrail_config = None
        if rc.guardrails:
            guardrail_config = GuardrailConfig.from_dict(rc.guardrails.to_dict())

        recording_config = RecordingConfig()
        if rc.recording:
            recording_config = RecordingConfig.from_dict(rc.recording.to_dict())

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
            recording_config=recording_config,
            profile=profile,
        )
