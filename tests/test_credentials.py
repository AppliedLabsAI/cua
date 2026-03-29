"""Tests for credential handling: SecretValue, wrap/unwrap, and encryption."""

from __future__ import annotations

import json

import pytest

from credentials import (
    SecretValue,
    credentials_for_prompt,
    decrypt_credentials,
    encrypt_credentials,
    get_public_key_pem,
    resolve_credentials,
)
from exceptions import ConfigError

# ---------------------------------------------------------------------------
# SecretValue
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Wrap / unwrap
# ---------------------------------------------------------------------------


class TestResolveCredentials:
    def test_wraps_values(self):
        raw = {"gh": {"user": "admin", "pass": "secret"}}
        resolved = resolve_credentials(raw)
        assert resolved is not None
        assert resolved["gh"]["user"].get_secret_value() == "admin"
        assert resolved["gh"]["pass"].get_secret_value() == "secret"

    def test_empty(self):
        assert resolve_credentials({}) is None

    def test_none(self):
        assert resolve_credentials(None) is None

    def test_multiple_services(self):
        raw = {"a": {"k": "v1"}, "b": {"k": "v2"}}
        resolved = resolve_credentials(raw)
        assert resolved is not None
        assert resolved["a"]["k"].get_secret_value() == "v1"
        assert resolved["b"]["k"].get_secret_value() == "v2"


class TestCredentialsForPrompt:
    def test_unwraps(self):
        secure = {"svc": {"user": SecretValue("admin"), "pass": SecretValue("pw")}}
        plain = credentials_for_prompt(secure)
        assert plain == {"svc": {"user": "admin", "pass": "pw"}}

    def test_empty(self):
        assert credentials_for_prompt({}) == {}


# ---------------------------------------------------------------------------
# Encryption / decryption
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keys():
    """Generate a test RSA key pair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


class TestEncryption:
    def test_roundtrip(self, rsa_keys):
        private_pem, public_pem = rsa_keys
        creds = {"gh": {"user": "admin", "pass": "s3cret"}}

        token = encrypt_credentials(creds, public_pem)
        decrypted = decrypt_credentials(token, private_pem)

        assert decrypted == creds

    def test_large_credentials(self, rsa_keys):
        private_pem, public_pem = rsa_keys
        creds = {f"svc_{i}": {"token": f"tok_{'x' * 200}"} for i in range(10)}

        token = encrypt_credentials(creds, public_pem)
        assert decrypt_credentials(token, private_pem) == creds

    def test_wrong_key_fails(self, rsa_keys):
        _, public_pem = rsa_keys

        # Generate a different private key
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        wrong_pem = wrong_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        token = encrypt_credentials({"a": {"b": "c"}}, public_pem)
        with pytest.raises(ValueError, match="decryption|padding|Decryption"):
            decrypt_credentials(token, wrong_pem)

    def test_tampered_token_fails(self, rsa_keys):
        from cryptography.exceptions import InvalidTag

        private_pem, public_pem = rsa_keys
        token = encrypt_credentials({"a": {"b": "c"}}, public_pem)

        # Tamper with the data portion
        key_part, data_part = token.split(".")
        tampered = key_part + "." + data_part[:-4] + "XXXX"
        with pytest.raises(InvalidTag):
            decrypt_credentials(tampered, private_pem)

    def test_invalid_format_raises(self, rsa_keys):
        private_pem, _ = rsa_keys
        with pytest.raises(ConfigError, match="Invalid"):
            decrypt_credentials("no-dot-separator", private_pem)

    def test_get_public_key_pem(self, rsa_keys):
        private_pem, expected_public_pem = rsa_keys
        derived = get_public_key_pem(private_pem)
        assert derived == expected_public_pem
