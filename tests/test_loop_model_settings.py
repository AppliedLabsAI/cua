"""Tests for per-model runtime settings in the agent loop."""

from agent.loop import _build_model_settings


def test_build_model_settings_uses_openai_responses_settings_for_openai_models():
    settings = _build_model_settings("openai-responses:gpt-5.4", "high")

    assert isinstance(settings, dict)
    assert settings.get("thinking") == "high"
    assert settings.get("openai_previous_response_id") == "auto"


def test_build_model_settings_keeps_generic_settings_for_non_openai_models():
    settings = _build_model_settings("anthropic:claude-sonnet-4-6", "medium")

    assert isinstance(settings, dict)
    assert settings.get("thinking") == "medium"
    assert "openai_previous_response_id" not in settings
