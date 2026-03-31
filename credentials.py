"""Secure credential handling: in-memory masking via SecretValue.

Credentials are accepted as plain ``dict[str, str]`` over HTTPS and
immediately wrapped in :class:`SecretValue` to prevent accidental
logging or serialization.
"""

from __future__ import annotations

from collections.abc import Mapping


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


def credential_refs_for_prompt(creds: Mapping[str, SecretValue]) -> list[str]:
    """Return non-secret credential reference names for the LLM prompt."""
    return list(creds)


def resolve_credential_ref(
    creds: Mapping[str, SecretValue | str] | None,
    ref: str,
) -> str:
    """Resolve a credential reference to a plaintext value at execution time."""
    if not ref:
        raise KeyError("credential_ref is required")
    if not creds or ref not in creds:
        raise KeyError(f"Unknown credential_ref: {ref}")

    value = creds[ref]
    if isinstance(value, SecretValue):
        return value.get_secret_value()
    return str(value)
