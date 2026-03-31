"""Tests for api.errors — structured API and runtime error helpers."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.errors import (
    ApiError,
    ApiErrorCode,
    ErrorResponse,
    _default_code_for_status,
    classify_runtime_error,
    coerce_api_error,
    coerce_http_error_response,
    error_response,
    raise_api_error,
)

# ---------------------------------------------------------------------------
# coerce_api_error
# ---------------------------------------------------------------------------


class TestCoerceApiError:
    def test_returns_none_for_none_input(self):
        assert coerce_api_error(None) is None

    def test_passthrough_api_error_unchanged(self):
        original = ApiError(code=ApiErrorCode.NOT_FOUND, message="missing")
        result = coerce_api_error(original)
        assert result is original

    def test_unwraps_error_response(self):
        inner = ApiError(code=ApiErrorCode.UNAUTHORIZED, message="bad token")
        response = ErrorResponse(error=inner)
        result = coerce_api_error(response)
        assert result is inner

    def test_dict_with_nested_error_key(self):
        payload = {
            "error": {
                "code": "NOT_FOUND",
                "message": "resource missing",
            }
        }
        result = coerce_api_error(payload)
        assert result is not None
        assert result.code == ApiErrorCode.NOT_FOUND
        assert result.message == "resource missing"

    def test_dict_without_error_key_validates_directly(self):
        payload = {"code": "UNAUTHORIZED", "message": "no token"}
        result = coerce_api_error(payload)
        assert result is not None
        assert result.code == ApiErrorCode.UNAUTHORIZED
        assert result.message == "no token"

    def test_dict_that_fails_validation_falls_back_to_default_code(self):
        bad_payload = {"not_a_code": "garbage", "also_bad": 123}
        result = coerce_api_error(bad_payload, default_code=ApiErrorCode.LLM_ERROR)
        assert result is not None
        assert result.code == ApiErrorCode.LLM_ERROR
        assert str(bad_payload) in result.message

    def test_dict_that_fails_validation_uses_internal_error_by_default(self):
        bad_payload = {"junk": True}
        result = coerce_api_error(bad_payload)
        assert result is not None
        assert result.code == ApiErrorCode.INTERNAL_ERROR

    def test_str_input_returns_api_error_with_message(self):
        result = coerce_api_error("something went wrong")
        assert result is not None
        assert result.code == ApiErrorCode.INTERNAL_ERROR
        assert result.message == "something went wrong"

    def test_str_input_respects_custom_default_code(self):
        result = coerce_api_error("timeout", default_code=ApiErrorCode.TIMEOUT)
        assert result is not None
        assert result.code == ApiErrorCode.TIMEOUT

    def test_dict_with_nested_error_key_that_is_not_dict_validates_flat(self):
        # "error" key exists but its value is not a dict — falls through to flat validate
        payload = {"error": "string value", "code": "INVALID_REQUEST", "message": "bad"}
        result = coerce_api_error(payload)
        # The flat dict has "error", "code", "message"; ApiError has code+message so
        # validation succeeds on the flat path (extra keys are ignored by pydantic).
        assert result is not None
        assert result.code == ApiErrorCode.INVALID_REQUEST


# ---------------------------------------------------------------------------
# coerce_http_error_response
# ---------------------------------------------------------------------------


class TestCoerceHttpErrorResponse:
    def test_none_detail_returns_request_failed_message(self):
        result = coerce_http_error_response(None, status_code=500)
        assert isinstance(result, ErrorResponse)
        assert result.error.message == "Request failed"
        assert result.error.code == ApiErrorCode.INTERNAL_ERROR

    def test_string_detail_wrapped_with_status_default_code(self):
        result = coerce_http_error_response("bad input", status_code=400)
        assert result.error.code == ApiErrorCode.INVALID_REQUEST
        assert result.error.message == "bad input"

    def test_api_error_detail_passed_through(self):
        api_err = ApiError(code=ApiErrorCode.TIMEOUT, message="timed out")
        result = coerce_http_error_response(api_err, status_code=500)
        assert result.error is api_err

    def test_error_response_detail_unwrapped(self):
        inner = ApiError(code=ApiErrorCode.GUARDRAIL_BLOCKED, message="blocked")
        err_resp = ErrorResponse(error=inner)
        result = coerce_http_error_response(err_resp, status_code=403)
        assert result.error is inner

    def test_dict_detail_with_nested_error(self):
        detail = {"error": {"code": "CAPTCHA_FAILED", "message": "captcha required"}}
        result = coerce_http_error_response(detail, status_code=422)
        assert result.error.code == ApiErrorCode.CAPTCHA_FAILED

    def test_404_status_uses_not_found_code_for_generic_string(self):
        result = coerce_http_error_response("not here", status_code=404)
        assert result.error.code == ApiErrorCode.NOT_FOUND

    def test_401_status_uses_unauthorized_code(self):
        result = coerce_http_error_response("forbidden", status_code=401)
        assert result.error.code == ApiErrorCode.UNAUTHORIZED

    def test_503_status_uses_server_misconfigured_code(self):
        result = coerce_http_error_response("down", status_code=503)
        assert result.error.code == ApiErrorCode.SERVER_MISCONFIGURED


# ---------------------------------------------------------------------------
# classify_runtime_error
# ---------------------------------------------------------------------------


class TestClassifyRuntimeError:
    def test_none_message_returns_none(self):
        assert classify_runtime_error(None) is None

    def test_empty_string_returns_none(self):
        assert classify_runtime_error("") is None

    def test_guardrail_blocked_keyword(self):
        result = classify_runtime_error("guardrail blocked: action not permitted")
        assert result is not None
        assert result.code == ApiErrorCode.GUARDRAIL_BLOCKED

    def test_not_allowed_for_keyword(self):
        result = classify_runtime_error("operation not allowed for this user")
        assert result is not None
        assert result.code == ApiErrorCode.GUARDRAIL_BLOCKED

    def test_timed_out_keyword(self):
        result = classify_runtime_error("request timed out after 30s")
        assert result is not None
        assert result.code == ApiErrorCode.TIMEOUT

    def test_timeout_keyword(self):
        result = classify_runtime_error("connection timeout")
        assert result is not None
        assert result.code == ApiErrorCode.TIMEOUT

    def test_captcha_failed_keyword(self):
        result = classify_runtime_error("captcha failed to solve")
        assert result is not None
        assert result.code == ApiErrorCode.CAPTCHA_FAILED

    def test_captcha_unresolved_keyword(self):
        result = classify_runtime_error("captcha unresolved after retries")
        assert result is not None
        assert result.code == ApiErrorCode.CAPTCHA_FAILED

    def test_captcha_blocked_keyword(self):
        result = classify_runtime_error("captcha blocked navigation")
        assert result is not None
        assert result.code == ApiErrorCode.CAPTCHA_FAILED

    def test_captcha_alone_without_qualifier_falls_through(self):
        # "captcha" alone without failed/unresolved/blocked goes to LLM_ERROR
        # because "model" or "provider" are not present either — hits INTERNAL_ERROR
        result = classify_runtime_error("captcha appeared")
        assert result is not None
        assert result.code == ApiErrorCode.INTERNAL_ERROR

    def test_circuit_breaker_keyword(self):
        result = classify_runtime_error("circuit breaker is open")
        assert result is not None
        assert result.code == ApiErrorCode.LLM_ERROR

    def test_model_keyword(self):
        result = classify_runtime_error("model rate limit exceeded")
        assert result is not None
        assert result.code == ApiErrorCode.LLM_ERROR

    def test_provider_keyword(self):
        result = classify_runtime_error("provider returned 429")
        assert result is not None
        assert result.code == ApiErrorCode.LLM_ERROR

    def test_unmatched_message_returns_internal_error(self):
        result = classify_runtime_error("something unexpected happened")
        assert result is not None
        assert result.code == ApiErrorCode.INTERNAL_ERROR

    def test_original_message_preserved_in_result(self):
        msg = "timed out connecting to service"
        result = classify_runtime_error(msg)
        assert result is not None
        assert result.message == msg

    def test_case_insensitive_matching(self):
        result = classify_runtime_error("GUARDRAIL BLOCKED by policy")
        assert result is not None
        assert result.code == ApiErrorCode.GUARDRAIL_BLOCKED


# ---------------------------------------------------------------------------
# _default_code_for_status
# ---------------------------------------------------------------------------


class TestDefaultCodeForStatus:
    def test_400_returns_invalid_request(self):
        assert _default_code_for_status(400) == ApiErrorCode.INVALID_REQUEST

    def test_401_returns_unauthorized(self):
        assert _default_code_for_status(401) == ApiErrorCode.UNAUTHORIZED

    def test_404_returns_not_found(self):
        assert _default_code_for_status(404) == ApiErrorCode.NOT_FOUND

    def test_503_returns_server_misconfigured(self):
        assert _default_code_for_status(503) == ApiErrorCode.SERVER_MISCONFIGURED

    def test_other_4xx_returns_invalid_request(self):
        assert _default_code_for_status(422) == ApiErrorCode.INVALID_REQUEST
        assert _default_code_for_status(429) == ApiErrorCode.INVALID_REQUEST
        assert _default_code_for_status(409) == ApiErrorCode.INVALID_REQUEST

    def test_5xx_returns_internal_error(self):
        assert _default_code_for_status(500) == ApiErrorCode.INTERNAL_ERROR
        assert _default_code_for_status(502) == ApiErrorCode.INTERNAL_ERROR
        assert _default_code_for_status(504) == ApiErrorCode.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# raise_api_error
# ---------------------------------------------------------------------------


class TestRaiseApiError:
    def test_raises_http_exception(self):
        with pytest.raises(HTTPException):
            raise_api_error(404, ApiErrorCode.NOT_FOUND, "resource not found")

    def test_exception_has_correct_status_code(self):
        with pytest.raises(HTTPException) as exc_info:
            raise_api_error(422, ApiErrorCode.INVALID_REQUEST, "bad data")
        assert exc_info.value.status_code == 422

    def test_exception_detail_has_error_key(self):
        with pytest.raises(HTTPException) as exc_info:
            raise_api_error(400, ApiErrorCode.INVALID_REQUEST, "missing field")
        detail = exc_info.value.detail
        assert "error" in detail

    def test_exception_detail_error_has_code(self):
        with pytest.raises(HTTPException) as exc_info:
            raise_api_error(401, ApiErrorCode.UNAUTHORIZED, "bad token")
        assert exc_info.value.detail["error"]["code"] == ApiErrorCode.UNAUTHORIZED

    def test_exception_detail_error_has_message(self):
        with pytest.raises(HTTPException) as exc_info:
            raise_api_error(500, ApiErrorCode.INTERNAL_ERROR, "boom")
        assert exc_info.value.detail["error"]["message"] == "boom"

    def test_exception_detail_includes_details_when_provided(self):
        with pytest.raises(HTTPException) as exc_info:
            raise_api_error(
                400,
                ApiErrorCode.INVALID_REQUEST,
                "validation failed",
                details={"field": "email"},
            )
        assert exc_info.value.detail["error"]["details"] == {"field": "email"}

    def test_exception_detail_details_none_when_not_provided(self):
        with pytest.raises(HTTPException) as exc_info:
            raise_api_error(404, ApiErrorCode.NOT_FOUND, "gone")
        assert exc_info.value.detail["error"]["details"] is None

    def test_annotated_as_no_return(self):
        """raise_api_error always raises — verify it never returns normally."""
        raised = False
        try:
            raise_api_error(503, ApiErrorCode.SERVER_MISCONFIGURED, "down")
        except HTTPException:
            raised = True
        assert raised


# ---------------------------------------------------------------------------
# error_response
# ---------------------------------------------------------------------------


class TestErrorResponse:
    def test_returns_error_response_instance(self):
        result = error_response(ApiErrorCode.NOT_FOUND, "not here")
        assert isinstance(result, ErrorResponse)

    def test_error_response_wraps_api_error(self):
        result = error_response(ApiErrorCode.TIMEOUT, "timed out")
        assert isinstance(result.error, ApiError)

    def test_error_response_code_matches(self):
        result = error_response(ApiErrorCode.GUARDRAIL_BLOCKED, "blocked")
        assert result.error.code == ApiErrorCode.GUARDRAIL_BLOCKED

    def test_error_response_message_matches(self):
        result = error_response(ApiErrorCode.INTERNAL_ERROR, "unexpected")
        assert result.error.message == "unexpected"

    def test_error_response_details_none_by_default(self):
        result = error_response(ApiErrorCode.INVALID_REQUEST, "bad")
        assert result.error.details is None

    def test_error_response_details_passed_through(self):
        result = error_response(
            ApiErrorCode.INVALID_REQUEST,
            "bad field",
            details={"field": "name", "reason": "too long"},
        )
        assert result.error.details == {"field": "name", "reason": "too long"}
