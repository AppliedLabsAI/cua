"""Tests for the dry-run validation endpoint."""

from __future__ import annotations

from api.models import RunConfig
from api.run_service import validate_run_config


class TestValidateRunConfig:
    def test_valid_minimal_config(self):
        config = RunConfig(directive="Go to example.com")
        result = validate_run_config(config)
        assert result.valid is True
        assert all(c.passed for c in result.checks)
        assert result.config_summary["model"] is not None

    def test_valid_with_credentials(self):
        config = RunConfig(
            directive="Log in",
            credentials={"username": "admin", "password": "secret"},
        )
        result = validate_run_config(config)
        assert result.valid is True
        cred_check = next(c for c in result.checks if c.name == "credentials")
        assert cred_check.passed is True
        assert "2 credential(s)" in cred_check.message

    def test_empty_credential_value_fails(self):
        config = RunConfig(
            directive="Log in",
            credentials={"username": "admin", "password": ""},
        )
        result = validate_run_config(config)
        assert result.valid is False
        cred_check = next(c for c in result.checks if c.name == "credentials")
        assert cred_check.passed is False
        assert "password" in cred_check.message

    def test_invalid_profile_fails(self):
        config = RunConfig(directive="test", profile="nonexistent_profile_xyz")
        result = validate_run_config(config)
        assert result.valid is False
        profile_check = next(c for c in result.checks if c.name == "profile")
        assert profile_check.passed is False

    def test_invalid_start_url_fails(self):
        config = RunConfig(directive="test", start_url="not-a-url")
        result = validate_run_config(config)
        url_check = next(c for c in result.checks if c.name == "start_url")
        assert url_check.passed is False

    def test_valid_start_url(self):
        config = RunConfig(directive="test", start_url="https://example.com")
        result = validate_run_config(config)
        url_check = next(c for c in result.checks if c.name == "start_url")
        assert url_check.passed is True

    def test_high_max_steps_warning(self):
        config = RunConfig(directive="test", max_steps=150)
        result = validate_run_config(config)
        assert result.valid is True
        assert any("max_steps" in w for w in result.warnings)

    def test_high_timeout_warning(self):
        config = RunConfig(directive="test", timeout_seconds=2400)
        result = validate_run_config(config)
        assert result.valid is True
        assert any("timeout_seconds" in w for w in result.warnings)

    def test_config_summary_fields(self):
        config = RunConfig(directive="test")
        result = validate_run_config(config)
        summary = result.config_summary
        assert "model" in summary
        assert "max_steps" in summary
        assert "display" in summary
        assert "profile" in summary
        assert summary["has_credentials"] is False
        assert summary["has_guardrails"] is False

    def test_output_schema_without_type(self):
        config = RunConfig(
            directive="test",
            output_schema={"properties": {"name": {"type": "string"}}},
        )
        result = validate_run_config(config)
        assert any("type" in w for w in result.warnings)

    def test_no_credentials_passes(self):
        config = RunConfig(directive="test")
        result = validate_run_config(config)
        cred_check = next(c for c in result.checks if c.name == "credentials")
        assert cred_check.passed is True
        assert "anonymous" in cred_check.message.lower()
