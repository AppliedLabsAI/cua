"""Custom exception hierarchy for the CUA agent.

Provides specific exception types so callers can catch and handle
different failure modes distinctly.
"""

from __future__ import annotations


class CUAError(Exception):
    """Base exception for all CUA errors."""


class BrowserError(CUAError):
    """Error during browser interaction (launch, navigation, DOM)."""


class GuardrailError(CUAError):
    """Action blocked by safety guardrails."""


class ConfigError(CUAError):
    """Invalid or missing configuration."""


class SandboxError(CUAError):
    """Error creating or managing the sandbox environment."""


class LLMError(CUAError):
    """Error communicating with the LLM provider (rate limit, timeout, etc.)."""


class RecordingError(CUAError):
    """Error during session recording or trace upload."""
