"""Deterministic HTTP request execution for playbook API steps."""

from __future__ import annotations

import json
from typing import Any

import httpx

from guardrails import GuardrailConfig, GuardrailEngine
from playbooks.schema import ApiRequestConfig


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _resolve_json_path(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(part)
            current = current[part]
            continue
        if isinstance(current, list):
            current = current[int(part)]
            continue
        raise KeyError(part)
    return current


async def execute_api_request(
    request: ApiRequestConfig,
    *,
    guardrail_config: GuardrailConfig,
    default_headers: dict[str, str] | None = None,
) -> str:
    """Execute a guarded API request and return the requested response material."""
    guardrails = GuardrailEngine(guardrail_config)
    decision = guardrails.check_navigation(request.url)
    if not decision.allowed:
        raise RuntimeError(decision.reason or "API request blocked by guardrails")

    headers = dict(default_headers or {})
    headers.update(request.headers)

    form_data = request.form or None
    json_data = request.json_body
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.request(
            method=request.method.upper(),
            url=request.url,
            params=request.query,
            headers=headers,
            cookies=request.cookies,
            json=json_data,
            data=form_data,
            timeout=request.timeout_ms / 1000,
        )

    for item in [*(response.history or []), response]:
        url_decision = guardrails.check_url(str(item.request.url))
        if not url_decision.allowed:
            raise RuntimeError(
                url_decision.reason or "API response redirect blocked by guardrails"
            )

    response.raise_for_status()

    if request.response.mode == "text":
        return response.text

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Response body was not valid JSON") from exc

    if request.response.mode == "json":
        return _to_text(payload)

    if not request.response.json_path:
        raise RuntimeError("json_path response mode requires response.json_path")

    try:
        value = _resolve_json_path(payload, request.response.json_path)
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(
            f"Response json_path not found: {request.response.json_path}"
        ) from exc
    return _to_text(value)
