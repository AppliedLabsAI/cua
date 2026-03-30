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


class TestCredentialsForPrompt:
    def test_unwraps(self):
        secure = {"username": SecretValue("admin"), "password": SecretValue("pw")}
        plain = credentials_for_prompt(secure)
        assert plain == {"username": "admin", "password": "pw"}

    def test_empty(self):
        assert credentials_for_prompt({}) == {}


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
        creds = {"username": "admin", "password": "s3cret"}

        token = encrypt_credentials(creds, public_pem)
        decrypted = decrypt_credentials(token, private_pem)

        assert decrypted == creds

    def test_large_credentials(self, rsa_keys):
        private_pem, public_pem = rsa_keys
        creds = {f"key_{i}": f"val_{'x' * 200}" for i in range(10)}

        token = encrypt_credentials(creds, public_pem)
        assert decrypt_credentials(token, private_pem) == creds

    def test_wrong_key_fails(self, rsa_keys):
        _, public_pem = rsa_keys

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        wrong_pem = wrong_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        token = encrypt_credentials({"a": "b"}, public_pem)
        with pytest.raises(ValueError, match="decryption|padding|Decryption"):
            decrypt_credentials(token, wrong_pem)

    def test_tampered_token_fails(self, rsa_keys):
        from cryptography.exceptions import InvalidTag

        private_pem, public_pem = rsa_keys
        token = encrypt_credentials({"a": "b"}, public_pem)

        key_part, data_part = token.split(".")
        tampered = key_part + "." + data_part[:-4] + "XXXX"
        with pytest.raises(InvalidTag):
            decrypt_credentials(tampered, private_pem)

    def test_invalid_format_raises(self, rsa_keys):
        private_pem, _ = rsa_keys
        with pytest.raises(ConfigError, match="Invalid"):
            decrypt_credentials("no-dot-separator", private_pem)

    def test_non_string_value_rejected(self, rsa_keys):
        """Decryption rejects non-string values."""
        private_pem, public_pem = rsa_keys
        # Manually encrypt a dict with a non-string value
        import base64
        import os

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        from credentials import _load_private_key, _oaep_padding

        pub = _load_private_key(private_pem).public_key()
        plaintext = json.dumps({"key": 123}).encode()
        aes_key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        ct = AESGCM(aes_key).encrypt(nonce, plaintext, None)
        ek = pub.encrypt(aes_key, _oaep_padding())
        token = (
            base64.b64encode(ek).decode() + "." + base64.b64encode(nonce + ct).decode()
        )

        with pytest.raises(ConfigError, match="non-string"):
            decrypt_credentials(token, private_pem)

    def test_get_public_key_pem(self, rsa_keys):
        private_pem, expected_public_pem = rsa_keys
        derived = get_public_key_pem(private_pem)
        assert derived == expected_public_pem
