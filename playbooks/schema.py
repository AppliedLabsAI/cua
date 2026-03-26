"""Playbook data model for deterministic dashboard automation."""

from __future__ import annotations

from dataclasses import dataclass, field


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
class PlaybookStep:
    """Single action in a playbook."""

    action: str  # goto, click, key_press, scroll, wait_for
    params: dict = field(default_factory=dict)
    selector: SelectorStrategy | None = None
    verify: StepVerification | None = None
    description: str = ""
    on_failure: str = "llm_recover"  # "llm_recover" | "retry" | "abort"


@dataclass
class PlaybookParameter:
    """Variable slot in a playbook template."""

    name: str
    type: str = "string"  # "string" | "int" | "selector_text"
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
    guardrails: dict = field(
        default_factory=dict
    )  # GuardrailConfig overrides for LLM handoff


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
