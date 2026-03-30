"""Span name constants and attribute keys — single source of truth.

Import from here instead of scattering string literals across the codebase.
"""

# ---------------------------------------------------------------------------
# Span names
# ---------------------------------------------------------------------------

SESSION = "cua.session"
SANDBOX_CREATE = "cua.sandbox.create"
AGENT_RUN = "cua.agent.run"
AGENT_SETUP = "cua.agent.setup"
BROWSER_LAUNCH = "cua.browser.launch"
BLINDERS_EXTRACT = "cua.blinders.extract_scope"
AGENT_ITERATION = "cua.agent.iteration"
CONTEXT_PRUNE = "cua.context.prune"
LLM_CALL = "cua.llm.call"
TOOL_EXECUTE = "cua.tool.execute"
GUARDRAIL_CHECK = "cua.guardrail.check"
GUARDRAIL_LLM = "cua.guardrail.llm"
BROWSER_ACTION = "cua.browser.action"
CAPTCHA_HANDLE = "cua.captcha.handle"
RECORDING_START = "cua.recording.start"
RECORDING_STOP = "cua.recording.stop"
RECORDING_UPLOAD = "cua.recording.upload"

# ---------------------------------------------------------------------------
# Attribute keys — session / resource
# ---------------------------------------------------------------------------

ATTR_SESSION_ID = "cua.session.id"
ATTR_DIRECTIVE = "cua.directive"
ATTR_MODEL = "cua.model"
ATTR_MAX_STEPS = "cua.max_steps"
ATTR_PROFILE = "cua.profile"
ATTR_DISPLAY_WIDTH = "cua.display.width"
ATTR_DISPLAY_HEIGHT = "cua.display.height"
ATTR_START_URL = "cua.start_url"

# Blinders
ATTR_BLINDERS_GOAL = "cua.blinders.goal_type"
ATTR_BLINDERS_ACTIONS = "cua.blinders.allowed_actions"

# ---------------------------------------------------------------------------
# Attribute keys — iteration
# ---------------------------------------------------------------------------

ATTR_ITER_NUMBER = "cua.iteration.number"
ATTR_ITER_TOOL_CALLS = "cua.iteration.tool_call_count"
ATTR_ITER_THINKING = "cua.iteration.thinking"
ATTR_ITER_STREAMING = "cua.iteration.is_streaming"

# ---------------------------------------------------------------------------
# Attribute keys — LLM (GenAI semantic conventions)
# ---------------------------------------------------------------------------

ATTR_GENAI_MODEL = "gen_ai.request.model"
ATTR_GENAI_MAX_TOKENS = "gen_ai.request.max_tokens"
ATTR_GENAI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_GENAI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_GENAI_THINKING = "gen_ai.request.thinking"
ATTR_LLM_STREAMING = "cua.llm.streaming"
ATTR_LLM_HAS_TOOL_CALLS = "cua.llm.has_tool_calls"
ATTR_LLM_TEXT_RESPONSE = "cua.llm.text_response"

# ---------------------------------------------------------------------------
# Attribute keys — tool execution
# ---------------------------------------------------------------------------

ATTR_TOOL_NAME = "cua.tool.name"
ATTR_TOOL_ACTION = "cua.tool.action"
ATTR_TOOL_STEP = "cua.tool.step"
ATTR_TOOL_SELECTOR = "cua.tool.selector"
ATTR_TOOL_URL = "cua.tool.url"
ATTR_TOOL_SUCCESS = "cua.tool.success"
ATTR_TOOL_DURATION_MS = "cua.tool.duration_ms"
ATTR_TOOL_HAS_SCREENSHOT = "cua.tool.has_screenshot"
ATTR_TOOL_ERROR = "cua.tool.error"
ATTR_TOOL_SKIPPED = "cua.tool.skipped"
ATTR_TOOL_INPUT_SUMMARY = "cua.tool.input_summary"

# ---------------------------------------------------------------------------
# Attribute keys — guardrails
# ---------------------------------------------------------------------------

ATTR_GUARD_ALLOWED = "cua.guardrail.allowed"
ATTR_GUARD_REASON = "cua.guardrail.reason"
ATTR_GUARD_NEEDS_CONFIRM = "cua.guardrail.needs_confirmation"
ATTR_GUARD_CHECK_TYPE = "cua.guardrail.check_type"
ATTR_GUARD_USED_LLM = "cua.guardrail.used_llm"

# ---------------------------------------------------------------------------
# Attribute keys — browser action
# ---------------------------------------------------------------------------

ATTR_BROWSER_ACTION = "cua.browser.action"
ATTR_BROWSER_PAGE_URL = "cua.browser.page_url"
ATTR_BROWSER_PAGE_CHANGED = "cua.browser.page_changed"
ATTR_BROWSER_DOM_CHARS = "cua.browser.dom_chars"

# ---------------------------------------------------------------------------
# Event names
# ---------------------------------------------------------------------------

EVENT_THINKING = "agent.thinking"
EVENT_TEXT_OUTPUT = "agent.text_output"
EVENT_STUCK = "stuck.detected"
ATTR_STUCK_SEVERITY = "cua.stuck.severity"
ATTR_STUCK_SUMMARY = "cua.stuck.input_summary"
EVENT_PAGE_CHANGED = "page.changed"
EVENT_TOOL_SKIPPED = "tool.skipped"
EVENT_CAPTCHA = "captcha.detected"
EVENT_CONTEXT_PRUNED = "context.pruned"
EVENT_AGENT_COMPLETED = "agent.completed"
