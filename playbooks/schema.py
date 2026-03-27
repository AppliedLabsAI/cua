"""Playbook data model for deterministic dashboard automation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PlaybookAction = Literal[
    "goto",
    "click",
    "key_press",
    "scroll",
    "wait_for",
    "select",
    "evaluate",
    "extract",
]
OnFailureMode = Literal["llm_recover", "retry", "abort"]
ParameterType = Literal["string", "int", "selector_text"]

KNOWN_PLAYBOOK_ACTIONS: tuple[PlaybookAction, ...] = (
    "goto",
    "click",
    "key_press",
    "scroll",
    "wait_for",
    "select",
    "evaluate",
    "extract",
)
KNOWN_FAILURE_MODES: tuple[OnFailureMode, ...] = ("llm_recover", "retry", "abort")
KNOWN_PARAMETER_TYPES: tuple[ParameterType, ...] = ("string", "int", "selector_text")


@dataclass
class SelectorStrategy:
    """Multi-strategy selector with fallback chain.

    Selectors use Playwright syntax:
    - CSS: "input[placeholder*='Search']"
    - Text: "text=Cancel Order"
    - Role: "role=button[name='Submit']"
    """

    primary: str
    fallbacks: list[str] = field(default_factory=list)
    description: str = ""

    @property
    def all_selectors(self) -> list[str]:
        return [self.primary, *self.fallbacks]


@dataclass
class StepVerification:
    """Post-action verification to ensure a step succeeded."""

    expect_url_contains: str | None = None
    expect_element_visible: str | None = None
    expect_element_gone: str | None = None
    expect_text_on_page: str | None = None
    timeout_ms: int = 5000


@dataclass
class PlaybookGuardrails:
    """Optional per-playbook guardrail overrides for LLM handoff."""

    allowed_domains: list[str] | None = None
    blocked_domains: list[str] | None = None
    max_urls_visited: int | None = None
    max_consecutive_errors: int | None = None
    allow_private_networks: bool | None = None
    enable_llm_action_check: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PlaybookGuardrails:
        payload = data or {}
        known_fields = cls.__dataclass_fields__
        unknown_fields = sorted(set(payload) - set(known_fields))
        if unknown_fields:
            raise ValueError(
                f"Unknown playbook guardrail overrides: {', '.join(unknown_fields)}"
            )
        filtered = {key: value for key, value in payload.items() if key in known_fields}
        return cls(**filtered)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if value is not None:
                result[field_name] = value
        return result

    def has_overrides(self) -> bool:
        return any(
            getattr(self, field_name) is not None
            for field_name in self.__dataclass_fields__
        )

    def to_runtime_config(self):
        from guardrails import GuardrailConfig

        if not self.has_overrides():
            return GuardrailConfig()
        return GuardrailConfig.from_dict(self.to_dict())


@dataclass
class PlaybookStep:
    """Single action in a playbook."""

    action: PlaybookAction
    params: dict[str, Any] = field(default_factory=dict)
    selector: SelectorStrategy | None = None
    verify: StepVerification | None = None
    description: str = ""
    on_failure: OnFailureMode = "llm_recover"
    store_as: str = ""  # Save extracted output for later {param} substitution


@dataclass
class PlaybookParameter:
    """Variable slot in a playbook template."""

    name: str
    type: ParameterType = "string"
    description: str = ""
    inject_into: str = ""  # Dot-path: "steps.2.params.text"
    pattern: str = ""  # Regex for extraction from directive


@dataclass
class Playbook:
    """Complete workflow definition."""

    id: str
    name: str
    description: str = ""
    parameters: list[PlaybookParameter] = field(default_factory=list)
    auth_required: bool = True
    steps: list[PlaybookStep] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    start_url: str = ""  # Override start URL for this playbook
    guardrails: PlaybookGuardrails = field(default_factory=PlaybookGuardrails)


@dataclass
class StepResult:
    """Outcome of a single playbook step."""

    step_index: int
    action: str
    success: bool
    duration_ms: int = 0
    description: str = ""
    error: str | None = None
    recovery_used: bool = False
    extracted_text: str | None = None  # Text extracted by 'extract' action


@dataclass
class PlaybookResult:
    """Outcome of a full playbook execution."""

    playbook_id: str
    success: bool
    step_results: list[StepResult] = field(default_factory=list)
    total_duration_ms: int = 0
    error: str | None = None
    screenshot_b64: str | None = None  # Final screenshot on completion/failure
    extracted_text: str | None = None  # Data extracted during execution
    data: dict[str, Any] | None = None  # Schema-driven structured extraction
