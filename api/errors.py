"""Structured API and runtime error helpers."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, NoReturn

from fastapi import HTTPException
from pydantic import BaseModel


class ApiErrorCode(StrEnum):
    """Machine-readable error categories exposed to clients."""

    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_PATH = "INVALID_PATH"
    UNAUTHORIZED = "UNAUTHORIZED"
    SERVER_MISCONFIGURED = "SERVER_MISCONFIGURED"
    NOT_FOUND = "NOT_FOUND"
    SANDBOX_CREATE_FAILED = "SANDBOX_CREATE_FAILED"
    SANDBOX_UNREACHABLE = "SANDBOX_UNREACHABLE"
    INVALID_STATUS_PAYLOAD = "INVALID_STATUS_PAYLOAD"
    RECORDING_NOT_FOUND = "RECORDING_NOT_FOUND"
    TRACE_NOT_AVAILABLE = "TRACE_NOT_AVAILABLE"
    RUN_TERMINATED = "RUN_TERMINATED"
    SETUP_FAILED = "SETUP_FAILED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    TIMEOUT = "TIMEOUT"
    CAPTCHA_FAILED = "CAPTCHA_FAILED"
    LLM_ERROR = "LLM_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ApiError(BaseModel):
    """Structured error payload returned by API endpoints and run status."""

    code: ApiErrorCode
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """Top-level error response body."""

    error: ApiError


def make_api_error(
    code: ApiErrorCode,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> ApiError:
    """Construct a structured error."""
    return ApiError(code=code, message=message, details=details)


def error_response(
    code: ApiErrorCode,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> ErrorResponse:
    """Construct an API error response body."""
    return ErrorResponse(error=make_api_error(code, message, details=details))


def raise_api_error(
    status_code: int,
    code: ApiErrorCode,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> NoReturn:
    """Raise an HTTPException with a structured error payload."""
    raise HTTPException(
        status_code=status_code,
        detail=error_response(code, message, details=details).model_dump(),
    )


def coerce_api_error(
    value: ApiError | ErrorResponse | dict[str, Any] | str | None,
    *,
    default_code: ApiErrorCode = ApiErrorCode.INTERNAL_ERROR,
) -> ApiError | None:
    """Normalize mixed error inputs to ApiError."""
    if value is None:
        return None
    if isinstance(value, ApiError):
        return value
    if isinstance(value, ErrorResponse):
        return value.error
    if isinstance(value, dict):
        if "error" in value and isinstance(value["error"], dict):
            return ApiError.model_validate(value["error"])
        return ApiError.model_validate(value)
    return make_api_error(default_code, str(value))


def coerce_http_error_response(
    detail: Any,
    *,
    status_code: int,
) -> ErrorResponse:
    """Convert arbitrary HTTPException detail into ErrorResponse."""
    error = coerce_api_error(detail, default_code=_default_code_for_status(status_code))
    if error is None:
        error = make_api_error(
            _default_code_for_status(status_code),
            "Request failed",
        )
    return ErrorResponse(error=error)


def classify_runtime_error(message: str | None) -> ApiError | None:
    """Map internal runtime errors to stable client-facing error codes."""
    if not message:
        return None

    lower = message.lower()
    if "guardrail blocked" in lower or "not allowed for" in lower:
        code = ApiErrorCode.GUARDRAIL_BLOCKED
    elif "timed out" in lower or "timeout" in lower:
        code = ApiErrorCode.TIMEOUT
    elif "captcha" in lower and (
        "failed" in lower or "unresolved" in lower or "blocked" in lower
    ):
        code = ApiErrorCode.CAPTCHA_FAILED
    elif "circuit breaker" in lower or "model" in lower or "provider" in lower:
        code = ApiErrorCode.LLM_ERROR
    else:
        code = ApiErrorCode.INTERNAL_ERROR

    return make_api_error(code, message)


def _default_code_for_status(status_code: int) -> ApiErrorCode:
    if status_code == 400:
        return ApiErrorCode.INVALID_REQUEST
    if status_code == 401:
        return ApiErrorCode.UNAUTHORIZED
    if status_code == 404:
        return ApiErrorCode.NOT_FOUND
    if status_code == 503:
        return ApiErrorCode.SERVER_MISCONFIGURED
    if 400 <= status_code < 500:
        return ApiErrorCode.INVALID_REQUEST
    return ApiErrorCode.INTERNAL_ERROR
