"""Profile loader — reads YAML profile definitions from the profiles/ directory.

Profiles bundle a prompt extension and guardrail overrides to specialize
the agent for different use cases without changing the tools or agent loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from guardrails import GuardrailConfig

_PROFILES_DIR = Path(__file__).parent


@dataclass(slots=True)
class Profile:
    """A CUA agent profile — prompt extension + guardrail overrides."""

    name: str
    description: str = ""
    prompt_extension: str | None = None
    guardrail_overrides: dict = field(default_factory=dict)


_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def load_profile(name: str) -> Profile:
    """Load a profile by name from the profiles/ directory.

    Args:
        name: Profile name (without .yaml extension).

    Raises:
        ValueError: If the profile name is invalid or file doesn't exist.
    """
    if not _PROFILE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid profile name '{name}': must contain only alphanumeric "
            "characters, hyphens, and underscores"
        )
    path = _PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        available = list_profiles()
        raise ValueError(f"Profile '{name}' not found. Available profiles: {available}")

    with open(path) as f:
        data = yaml.safe_load(f)

    return Profile(
        name=data.get("name", name),
        description=data.get("description", ""),
        prompt_extension=data.get("prompt_extension"),
        guardrail_overrides=data.get("guardrail_overrides") or {},
    )


def list_profiles() -> list[str]:
    """Return names of all available profiles."""
    return sorted(p.stem for p in _PROFILES_DIR.glob("*.yaml"))


def apply_guardrail_overrides(
    profile: Profile,
    base: GuardrailConfig | None = None,
) -> GuardrailConfig:
    """Merge a profile's guardrail overrides into a base config.

    Profile provides defaults; explicit base config values take precedence.
    This ensures env vars and API-specified guardrails override profile defaults.
    """
    if not profile.guardrail_overrides:
        return base or GuardrailConfig()

    from dataclasses import asdict

    defaults = asdict(GuardrailConfig())
    base_dict = asdict(base) if base else defaults
    # Start with profile overrides, then layer explicit base values on top.
    # Only apply base values that differ from defaults (i.e., explicitly set).
    merged = {**defaults, **profile.guardrail_overrides}
    if base:
        explicit = {k: v for k, v in base_dict.items() if v != defaults.get(k)}
        merged.update(explicit)
    return GuardrailConfig.from_dict(merged)
