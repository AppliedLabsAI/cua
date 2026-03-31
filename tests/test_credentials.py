"""Tests for credential handling: SecretValue and wrap/unwrap."""

from __future__ import annotations

import json

import pytest

from credentials import (
    SecretValue,
    credential_refs_for_prompt,
    resolve_credential_ref,
    resolve_credentials,
)


class TestSecretValue:
    def test_get_secret_value(self):
        sv = SecretValue("hunter2")
        assert sv.get_secret_value() == "hunter2"

    def test_str_masked(self):
        assert str(SecretValue("hunter2")) == "******"

    def test_repr_masked(self):
        assert repr(SecretValue("hunter2")) == "SecretValue('******')"

    def test_not_json_serializable(self):
        with pytest.raises(TypeError):
            json.dumps({"key": SecretValue("x")})

    def test_bool(self):
        assert bool(SecretValue("x"))
        assert not bool(SecretValue(""))


class TestResolveCredentials:
    def test_wraps_values(self):
        raw = {"username": "admin", "password": "secret"}
        resolved = resolve_credentials(raw)
        assert resolved is not None
        assert resolved["username"].get_secret_value() == "admin"
        assert resolved["password"].get_secret_value() == "secret"

    def test_empty(self):
        assert resolve_credentials({}) is None

    def test_none(self):
        assert resolve_credentials(None) is None

    def test_multiple_keys(self):
        raw = {"username": "user", "password": "pass", "token": "tok"}
        resolved = resolve_credentials(raw)
        assert resolved is not None
        assert resolved["token"].get_secret_value() == "tok"


class TestCredentialRefsForPrompt:
    def test_returns_refs(self):
        secure = {"username": SecretValue("admin"), "password": SecretValue("pw")}
        refs = credential_refs_for_prompt(secure)
        assert refs == ["username", "password"]

    def test_empty(self):
        assert credential_refs_for_prompt({}) == []


class TestResolveCredentialRef:
    def test_unwraps_secret_value(self):
        secure = {"username": SecretValue("admin"), "password": SecretValue("pw")}
        assert resolve_credential_ref(secure, "password") == "pw"

    def test_supports_plain_string_values(self):
        assert resolve_credential_ref({"token": "abc123"}, "token") == "abc123"

    def test_missing_ref_raises(self):
        with pytest.raises(KeyError):
            resolve_credential_ref({"username": SecretValue("admin")}, "password")
