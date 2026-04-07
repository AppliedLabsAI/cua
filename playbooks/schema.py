"""Playbook data model for deterministic dashboard automation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PlaybookAction = Literal[
    "goto",
    "click",
    "key_press",
    "wait_for",
    "select",
    "evaluate",
    "extract",
    "llm_extract",
    "api_request",
]
OnFailureMode = Literal["llm_recover", "retry", "abort"]
ParameterType = Literal["string", "int", "selector_text"]
AuthMode = Literal["form_login", "manual", "none"]
StorageScope = Literal["local", "session"]
ApiResponseMode = Literal["text", "json", "json_path"]

KNOWN_PLAYBOOK_ACTIONS: tuple[PlaybookAction, ...] = (
    "goto",
    "click",
    "key_press",
    "wait_for",
    "select",
    "evaluate",
    "extract",
    "llm_extract",
    "api_request",
)
KNOWN_FAILURE_MODES: tuple[OnFailureMode, ...] = ("llm_recover", "retry", "abort")
KNOWN_PARAMETER_TYPES: tuple[ParameterType, ...] = ("string", "int", "selector_text")


class SelectorStrategy(BaseModel):
    """Multi-strategy selector with fallback chain.

    Selectors use Playwright syntax:
    - CSS: "input[placeholder*='Search']"
    - Text: "text=Cancel Order"
    - Role: "role=button[name='Submit']"
    """

    primary: str
    fallbacks: list[str] = Field(default_factory=list)
    description: str = ""

    @property
    def all_selectors(self) -> list[str]:
        return [self.primary, *self.fallbacks]


class StepVerification(BaseModel):
    """Post-action verification to ensure a step succeeded."""

    expect_url_contains: str | None = None
    expect_element_visible: str | None = None
    expect_element_gone: str | None = None
    expect_text_on_page: str | None = None
    timeout_ms: int = 5000


class AuthSuccessCriteria(BaseModel):
    """Signals that indicate the login flow reached an authenticated state."""

    model_config = ConfigDict(extra="forbid")

    url_contains: str | None = None
    element_visible: str | None = None
    text_on_page: str | None = None
    cookie_present: str | None = None
    timeout_ms: int = 15000


class PlaybookAuthConfig(BaseModel):
    """Authentication behavior for a playbook run."""

    model_config = ConfigDict(extra="forbid")

    mode: AuthMode = "form_login"
    login_url: str = ""
    success: AuthSuccessCriteria | None = None


class CookieCapture(BaseModel):
    """Cookie value to capture from the authenticated browser context."""

    model_config = ConfigDict(extra="forbid")

    name: str
    store_as: str
    domain: str = ""


class StorageCapture(BaseModel):
    """Storage key to capture from the active page."""

    model_config = ConfigDict(extra="forbid")

    key: str
    store_as: str
    scope: StorageScope = "local"


class PlaybookCaptureConfig(BaseModel):
    """Session artifacts and headers captured outside normal browser steps."""

    model_config = ConfigDict(extra="forbid")

    cookies: list[CookieCapture] = Field(default_factory=list)
    storage: list[StorageCapture] = Field(default_factory=list)
    static_headers: dict[str, str] = Field(default_factory=dict)

    def sensitive_runtime_param_names(self) -> set[str]:
        names = {item.store_as for item in self.cookies}
        names.update(item.store_as for item in self.storage)
        return names


class ApiResponseConfig(BaseModel):
    """How an API response should be materialized into playbook output."""

    model_config = ConfigDict(extra="forbid")

    mode: ApiResponseMode = "text"
    json_path: str = ""


class ApiRequestConfig(BaseModel):
    """Deterministic HTTP request executed from a playbook step."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    method: str = "GET"
    url: str
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | list[Any] | None = Field(
        default=None,
        alias="json",
        serialization_alias="json",
    )
    form: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = 10000
    response: ApiResponseConfig = Field(default_factory=ApiResponseConfig)


class PlaybookGuardrails(BaseModel):
    """Optional per-playbook guardrail overrides for LLM handoff."""

    model_config = ConfigDict(extra="forbid")

    allowed_domains: list[str] | None = None
    blocked_domains: list[str] | None = None
    max_urls_visited: int | None = None
    max_consecutive_errors: int | None = None
    allow_private_networks: bool | None = None
    enable_llm_action_check: bool | None = None

    def has_overrides(self) -> bool:
        return any(getattr(self, name) is not None for name in type(self).model_fields)

    def to_runtime_config(self):
        from guardrails import GuardrailConfig

        overrides = self.model_dump(exclude_none=True)
        if not overrides:
            return GuardrailConfig()
        return GuardrailConfig.model_validate(overrides)


class PlaybookStep(BaseModel):
    """Single action in a playbook."""

    action: PlaybookAction
    params: dict[str, Any] = Field(default_factory=dict)
    request: ApiRequestConfig | None = None
    selector: SelectorStrategy | None = None
    verify: StepVerification | None = None
    description: str = ""
    on_failure: OnFailureMode = "llm_recover"
    failure_message: str = ""  # User-facing error when the step cannot complete
    store_as: str = ""  # Save extracted output for later {param} substitution
    prompt: str = ""  # LLM prompt for llm_extract — analyzed against page content


class PlaybookParameter(BaseModel):
    """Variable slot in a playbook template."""

    name: str
    type: ParameterType = "string"
    description: str = ""
    inject_into: str = ""  # Dot-path: "steps.2.params.text"
    pattern: str = ""  # Regex for extraction from directive


class Playbook(BaseModel):
    """Complete workflow definition."""

    id: str
    name: str
    description: str = ""
    parameters: list[PlaybookParameter] = Field(default_factory=list)
    auth_required: bool = True
    auth: PlaybookAuthConfig | None = None
    capture: PlaybookCaptureConfig = Field(default_factory=PlaybookCaptureConfig)
    steps: list[PlaybookStep] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    start_url: str = ""  # Override start URL for this playbook
    guardrails: PlaybookGuardrails = Field(default_factory=PlaybookGuardrails)

    @property
    def auth_config(self) -> PlaybookAuthConfig:
        """Return the explicit auth config or derive it from legacy fields."""
        if self.auth is not None:
            return self.auth
        if self.auth_required:
            return PlaybookAuthConfig(mode="form_login", login_url=self.start_url)
        return PlaybookAuthConfig(mode="none", login_url=self.start_url)

    def sensitive_runtime_param_names(self) -> set[str]:
        return self.capture.sensitive_runtime_param_names()


class StepResult(BaseModel):
    """Outcome of a single playbook step."""

    step_index: int
    action: str
    success: bool
    duration_ms: int = 0
    description: str = ""
    error: str | None = None
    recovery_used: bool = False
    extracted_text: str | None = None  # Text extracted by 'extract' action
    input_tokens: int = 0
    output_tokens: int = 0
    session_memory: str = ""


class PlaybookResult(BaseModel):
    """Outcome of a full playbook execution."""

    playbook_id: str
    success: bool
    step_results: list[StepResult] = Field(default_factory=list)
    total_duration_ms: int = 0
    error: str | None = None
    screenshot_b64: str | None = None  # Final screenshot on completion/failure
    extracted_text: str | None = None  # Data extracted during execution
    data: dict[str, Any] | None = None  # Schema-driven structured extraction
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    extracted_texts: list[str] = Field(default_factory=list)
    session_memory: str = ""
