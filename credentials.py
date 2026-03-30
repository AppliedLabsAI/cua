"""Secure credential handling: encryption, decryption, and in-memory masking.

Clients encrypt credentials with the server's RSA public key before sending.
The server decrypts with its private key and wraps values in :class:`SecretValue`
to prevent accidental logging or serialization.

Encryption uses hybrid RSA-OAEP + AES-256-GCM.
"""

from __future__ import annotations

import base64
import json
import os
from functools import lru_cache

from exceptions import ConfigError


class SecretValue:
    """Prevents accidental exposure of credential strings.

    ``str()`` / ``repr()`` return ``******``.
    ``json.dumps()`` raises ``TypeError``.
    Use ``.get_secret_value()`` for intentional access.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue('******')"

    def __str__(self) -> str:
        return "******"

    def __bool__(self) -> bool:
        return bool(self._value)


def resolve_credentials(
    raw: dict[str, str] | None,
) -> dict[str, SecretValue] | None:
    """Wrap plain credential values in :class:`SecretValue`."""
    if not raw:
        return None
    return {k: SecretValue(v) for k, v in raw.items()}


def credentials_for_prompt(
    creds: dict[str, SecretValue],
) -> dict[str, str]:
    """Unwrap credentials for embedding in the LLM system prompt."""
    return {k: sv.get_secret_value() for k, sv in creds.items()}


@lru_cache(maxsize=1)
def _load_private_key(private_key_pem: bytes):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    return load_pem_private_key(private_key_pem, password=None)


def _oaep_padding():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    return padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(),
        label=None,
    )


def encrypt_credentials(
    creds: dict[str, str],
    public_key_pem: bytes,
) -> str:
    """Encrypt a credentials dict with the server's public key.

    Returns a token: ``base64(rsa_encrypted_aes_key).base64(nonce+ciphertext)``.
    """
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    loaded = load_pem_public_key(public_key_pem)
    if not isinstance(loaded, RSAPublicKey):
        raise ConfigError("Public key must be RSA")
    plaintext = json.dumps(creds).encode()

    aes_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ciphertext = AESGCM(aes_key).encrypt(nonce, plaintext, None)
    encrypted_key = loaded.encrypt(aes_key, _oaep_padding())

    key_part = base64.b64encode(encrypted_key).decode()
    data_part = base64.b64encode(nonce + ciphertext).decode()
    return f"{key_part}.{data_part}"


def decrypt_credentials(
    token: str,
    private_key_pem: bytes,
) -> dict[str, str]:
    """Decrypt a token produced by :func:`encrypt_credentials`."""
    import binascii

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        key_b64, data_b64 = token.split(".", 1)
    except ValueError as exc:
        raise ConfigError("Invalid encrypted credentials format") from exc

    try:
        private_key = _load_private_key(private_key_pem)
        aes_key = private_key.decrypt(base64.b64decode(key_b64), _oaep_padding())

        payload = base64.b64decode(data_b64)
        nonce, ciphertext = payload[:12], payload[12:]
        plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext, None)
    except (binascii.Error, ValueError, Exception) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("Failed to decrypt credentials") from exc

    try:
        parsed = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise ConfigError("Decrypted payload is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("Decrypted credentials must be a JSON object")
    for key, val in parsed.items():
        if not isinstance(key, str) or not isinstance(val, str):
            raise ConfigError(f"Decrypted credentials: non-string entry '{key}'")
    return parsed


@lru_cache(maxsize=1)
def get_public_key_pem(private_key_pem: bytes) -> bytes:
    """Derive the PEM-encoded public key from a private key."""
    from cryptography.hazmat.primitives import serialization

    private_key = _load_private_key(private_key_pem)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
