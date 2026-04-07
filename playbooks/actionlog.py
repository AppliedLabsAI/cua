"""Helpers for emitting safe ActionLog entries for playbook execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from actionlog.actions import ActionLog
from playbooks.schema import PlaybookStep, StepResult

_ACTION_RESULT_PREVIEW_CHARS = 240
_URL_PREVIEW_CHARS = 120


def _preview_text(
    value: str | None,
    *,
    max_chars: int = _ACTION_RESULT_PREVIEW_CHARS,
) -> str | None:
    if not value:
        return None
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}... [{len(value)} chars total]"


def _preview_url(url: str, *, max_chars: int = _URL_PREVIEW_CHARS) -> str:
    if len(url) <= max_chars:
        return url
    return f"{url[:max_chars]}..."


def _redact_value(value: Any, sensitive_values: set[str]) -> Any:
    if isinstance(value, str):
        return "[redacted]" if value in sensitive_values else value
    if isinstance(value, list):
        return [_redact_value(item, sensitive_values) for item in value]
    if isinstance(value, dict):
        return {
            key: _redact_value(item, sensitive_values) for key, item in value.items()
        }
    return value


def _playbook_input_summary(step: PlaybookStep) -> str:
    if step.action == "api_request" and step.request is not None:
        return f"{step.request.method.upper()} {_preview_url(step.request.url)}"
    if step.action == "goto":
        return f"navigate to {_preview_url(str(step.params.get('url', '?')))}"
    if step.action == "key_press":
        key = step.params.get("key")
        if step.selector is not None:
            if key:
                return f"type into '{step.selector.primary}' + press {key}"
            return f"type into '{step.selector.primary}'"
        if key:
            return f"press {key}"
        return "type text"
    if (
        step.action in {"click", "select", "extract", "wait_for"}
        and step.selector is not None
    ):
        verb = "wait for" if step.action == "wait_for" else step.action
        return f"{verb} '{step.selector.primary}'"
    if step.action == "llm_extract":
        return step.description or "extract with llm"
    return step.description or step.action


def _playbook_tool_input(
    step: PlaybookStep,
    sensitive_values: set[str],
) -> dict[str, Any]:
    if step.action == "api_request" and step.request is not None:
        tool_input: dict[str, Any] = {
            "method": step.request.method.upper(),
            "url": step.request.url,
            "query_keys": sorted(step.request.query.keys()),
            "header_names": sorted(step.request.headers.keys()),
            "cookie_names": sorted(step.request.cookies.keys()),
            "response_mode": step.request.response.mode,
        }
        if step.request.json_body is not None:
            tool_input["has_json_body"] = True
        if step.request.form:
            tool_input["form_keys"] = sorted(step.request.form.keys())
        if step.request.response.json_path:
            tool_input["json_path"] = step.request.response.json_path
        return tool_input

    params = _redact_value(step.params, sensitive_values)
    if step.action == "key_press" and "text" in params:
        params = dict(params)
        params["text"] = "[redacted]"
    if step.selector is not None:
        params = dict(params)
        params.setdefault("selector", step.selector.primary)
    return params


def build_playbook_action_log(
    *,
    step_index: int,
    step: PlaybookStep,
    result: StepResult,
    sensitive_values: set[str],
) -> ActionLog:
    """Build a redacted ActionLog entry for a deterministic playbook step."""
    return ActionLog(
        step=step_index + 1,
        timestamp=datetime.now(UTC).isoformat(),
        tool="playbook",
        action=step.action,
        input_summary=_playbook_input_summary(step),
        tool_input=_playbook_tool_input(step, sensitive_values),
        duration_ms=result.duration_ms,
        success=result.success,
        result_text=_preview_text(result.extracted_text),
        error=result.error,
    )
