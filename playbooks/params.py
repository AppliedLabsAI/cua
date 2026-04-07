"""Pure helpers for binding runtime parameters into playbook definitions."""

from __future__ import annotations

from typing import Any

from playbooks.schema import (
    ApiRequestConfig,
    ApiResponseConfig,
    AuthSuccessCriteria,
    CookieCapture,
    Playbook,
    PlaybookAuthConfig,
    PlaybookCaptureConfig,
    PlaybookParameter,
    PlaybookStep,
    SelectorStrategy,
    StepVerification,
    StorageCapture,
)


def materialize_playbook(playbook: Playbook, params: dict[str, Any]) -> Playbook:
    """Return a playbook with placeholders and explicit inject paths applied."""
    if not params:
        return playbook

    auth = None
    if playbook.auth:
        auth = PlaybookAuthConfig(
            mode=playbook.auth.mode,
            login_url=_replace_text(playbook.auth.login_url, params),
            success=_bind_auth_success(playbook.auth.success, params),
        )

    capture = _bind_capture(playbook.capture, params)
    steps = [
        materialize_step(playbook, step_index, params)
        for step_index, _ in enumerate(playbook.steps)
    ]

    return playbook.model_copy(
        update={"auth": auth, "capture": capture, "steps": steps}
    )


def materialize_step(
    playbook: Playbook,
    step_index: int,
    params: dict[str, Any],
) -> PlaybookStep:
    """Bind a single step against the current runtime parameter state."""
    step = _bind_step_placeholders(playbook.steps[step_index], params)
    for parameter in playbook.parameters:
        if not parameter.inject_into or parameter.name not in params:
            continue
        prefix = f"steps.{step_index}."
        if not parameter.inject_into.startswith(prefix):
            continue
        _set_path_value(
            step,
            parameter.inject_into[len(prefix) :].split("."),
            params[parameter.name],
            parameter.name,
        )
    return step


def bind_step_params(
    step: PlaybookStep,
    params: dict[str, Any],
) -> PlaybookStep:
    """Bind placeholders into a single step for compatibility and tests."""
    if not params:
        return step
    return _bind_step_placeholders(step, params)


def _bind_step_placeholders(step: PlaybookStep, params: dict[str, Any]) -> PlaybookStep:
    selector = None
    if step.selector:
        selector = SelectorStrategy(
            primary=_replace_text(step.selector.primary, params),
            fallbacks=[_replace_text(item, params) for item in step.selector.fallbacks],
            description=_replace_text(step.selector.description, params),
        )

    verify = None
    if step.verify:
        verify = StepVerification(
            expect_url_contains=_replace_optional(
                step.verify.expect_url_contains, params
            ),
            expect_element_visible=_replace_optional(
                step.verify.expect_element_visible, params
            ),
            expect_element_gone=_replace_optional(
                step.verify.expect_element_gone, params
            ),
            expect_text_on_page=_replace_optional(
                step.verify.expect_text_on_page, params
            ),
            timeout_ms=step.verify.timeout_ms,
        )

    return PlaybookStep(
        action=step.action,
        params=_replace_value(step.params, params),
        request=_bind_request(step.request, params),
        selector=selector,
        verify=verify,
        description=_replace_text(step.description, params),
        on_failure=step.on_failure,
        failure_message=(
            _replace_text(step.failure_message, params) if step.failure_message else ""
        ),
        store_as=step.store_as,
        prompt=_replace_text(step.prompt, params) if step.prompt else "",
    )


def _replace_value(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _replace_text(value, params)
    if isinstance(value, list):
        return [_replace_value(item, params) for item in value]
    if isinstance(value, dict):
        return {key: _replace_value(item, params) for key, item in value.items()}
    return value


def _replace_text(text: str, params: dict[str, Any]) -> str:
    result = text
    for key, value in params.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def _replace_optional(value: str | None, params: dict[str, Any]) -> str | None:
    if value is None:
        return None
    return _replace_text(value, params)


def _bind_auth_success(
    success: AuthSuccessCriteria | None,
    params: dict[str, Any],
) -> AuthSuccessCriteria | None:
    if success is None:
        return None
    return AuthSuccessCriteria(
        url_contains=_replace_optional(success.url_contains, params),
        element_visible=_replace_optional(success.element_visible, params),
        text_on_page=_replace_optional(success.text_on_page, params),
        cookie_present=_replace_optional(success.cookie_present, params),
        timeout_ms=success.timeout_ms,
    )


def _bind_capture(
    capture: PlaybookCaptureConfig,
    params: dict[str, Any],
) -> PlaybookCaptureConfig:
    return PlaybookCaptureConfig(
        cookies=[
            CookieCapture(
                name=_replace_text(item.name, params),
                store_as=_replace_text(item.store_as, params),
                domain=_replace_text(item.domain, params),
            )
            for item in capture.cookies
        ],
        storage=[
            StorageCapture(
                key=_replace_text(item.key, params),
                store_as=_replace_text(item.store_as, params),
                scope=item.scope,
            )
            for item in capture.storage
        ],
        static_headers={
            key: _replace_text(value, params)
            for key, value in capture.static_headers.items()
        },
    )


def _bind_request(
    request: ApiRequestConfig | None,
    params: dict[str, Any],
) -> ApiRequestConfig | None:
    if request is None:
        return None
    return ApiRequestConfig(
        method=_replace_text(request.method, params),
        url=_replace_text(request.url, params),
        query=_replace_value(request.query, params),
        headers=_replace_value(request.headers, params),
        cookies=_replace_value(request.cookies, params),
        json_body=_replace_value(request.json_body, params),
        form=_replace_value(request.form, params),
        timeout_ms=request.timeout_ms,
        response=ApiResponseConfig(
            mode=request.response.mode,
            json_path=_replace_text(request.response.json_path, params),
        ),
    )


def _assign_inject_path(
    steps: list[PlaybookStep],
    parameter: PlaybookParameter,
    value: Any,
) -> None:
    tokens = parameter.inject_into.split(".")
    if len(tokens) < 3 or tokens[0] != "steps":
        raise ValueError(
            f"Invalid inject path for parameter '{parameter.name}': {parameter.inject_into}"
        )

    try:
        step_index = int(tokens[1])
    except ValueError as exc:
        raise ValueError(
            f"Invalid step index in inject path for parameter '{parameter.name}'"
        ) from exc

    try:
        target = steps[step_index]
    except IndexError as exc:
        raise ValueError(
            f"Inject path points to missing step {step_index} for parameter '{parameter.name}'"
        ) from exc

    _set_path_value(target, tokens[2:], value, parameter.name)


def _set_path_value(
    target: Any,
    tokens: list[str],
    value: Any,
    parameter_name: str,
) -> None:
    current = target
    for token in tokens[:-1]:
        if isinstance(current, dict):
            if token not in current:
                current[token] = {}
            current = current[token]
            continue
        if isinstance(current, list):
            current = current[int(token)]
            continue
        if hasattr(current, token):
            current = getattr(current, token)
            continue
        raise ValueError(
            f"Invalid inject path segment '{token}' for parameter '{parameter_name}'"
        )

    leaf = tokens[-1]
    if isinstance(current, dict):
        current[leaf] = value
        return
    if isinstance(current, list):
        current[int(leaf)] = value
        return
    if hasattr(current, leaf):
        setattr(current, leaf, value)
        return
    raise ValueError(
        f"Invalid inject path leaf '{leaf}' for parameter '{parameter_name}'"
    )
