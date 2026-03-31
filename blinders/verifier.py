"""Scope Verifier — pre-execution gating against TaskScope.

Combines deterministic structural checks (action types, domain scope,
navigation limits) with optional LLM-based task-alignment validation.
When no directive is available, verifier stays deterministic and leaves
click intent decisions to the outer runtime.
"""

from __future__ import annotations

import fnmatch
import logging
from urllib.parse import urlparse

from blinders.action_validator import ActionValidator
from blinders.scope import TaskScope
from guardrails import GuardrailEngine

logger = logging.getLogger(__name__)

# Max recursion depth for execute_sequence validation
_MAX_SEQUENCE_DEPTH = 10


class ScopeVerifier:
    """Pre-execution check combining deterministic scope checks with LLM validation."""

    def __init__(
        self,
        scope: TaskScope,
        guardrails: GuardrailEngine,
        directive: str = "",
        *,
        skip_llm_validation: bool = False,
    ) -> None:
        self.scope = scope
        self.guardrails = guardrails
        self._has_directive = bool(directive)
        # When skip_llm_validation is True, skip the task-alignment LLM call.
        # If no directive is available, keep verifier fully deterministic and
        # leave click intent decisions to the outer runtime.
        self._validator = (
            None
            if skip_llm_validation
            else (ActionValidator(directive) if directive else None)
        )

    async def check(
        self,
        action: str,
        tool_input: dict,
        *,
        page_url: str = "",
        page_title: str = "",
        _depth: int = 0,
        _skip_llm: bool = False,
    ) -> str | None:
        """Check if an action is allowed.

        Returns reason string if blocked, None if allowed.
        Runs deterministic checks first, then LLM validation for risky actions.

        For execute_sequence: deterministic checks run on each sub-step,
        but LLM validation runs ONCE on the whole sequence (batched).
        """
        # Guard against deeply nested sequences
        if _depth > _MAX_SEQUENCE_DEPTH:
            return "Sequence nesting too deep"

        # --- Layer 1: Deterministic checks (fast, non-bypassable) ---

        # 1. Action type restriction — structural
        if action not in self.scope.allowed_actions:
            logger.info(
                "Scope blocked action '%s' (goal_type=%s)",
                action,
                self.scope.goal_type,
            )
            return f"Action '{action}' not allowed for {self.scope.goal_type} tasks"

        # 2. Domain scope (for goto actions)
        if action == "goto":
            url = tool_input.get("url", "")
            domain_block = self._check_domain(url)
            if domain_block:
                return domain_block

            # Existing navigation limit path also performs URL/SSRF checks.
            nav = self.guardrails.check_navigation(url)
            if not nav.allowed:
                return nav.reason

        # 3. Router/guardrail check — skip its LLM-backed click classification when
        #    ActionValidator owns task alignment, or when this verifier is running
        #    without a directive and must stay deterministic.
        action_check = await self.guardrails.check_action(
            action,
            tool_input,
            skip_llm=bool(self._validator) or not self._has_directive,
        )
        if not action_check.allowed:
            return action_check.reason

        # 4. execute_sequence: deterministic checks on each step,
        #    but skip LLM on sub-steps (validated once for whole sequence)
        if action == "execute_sequence":
            for step in tool_input.get("steps", []):
                if isinstance(step, dict):
                    step_action = step.get("action", "")
                    result = await self.check(
                        step_action,
                        step,
                        page_url=page_url,
                        page_title=page_title,
                        _depth=_depth + 1,
                        _skip_llm=True,  # LLM validates the sequence as a whole
                    )
                    if result:
                        return result

        # --- Layer 2: LLM validation (Haiku) ---
        # Validates the top-level action (or whole sequence) in ONE call.
        # Skipped for sub-steps inside execute_sequence (_skip_llm=True).
        if self._validator and not _skip_llm:
            llm_block = await self._validator.validate(
                action,
                tool_input,
                page_url=page_url,
                page_title=page_title,
            )
            if llm_block:
                return llm_block

        return None

    def _check_domain(self, url: str) -> str | None:
        """Check if URL's domain is within the task scope.

        If allowed_domains is empty (no URLs found in directive),
        domain scoping is not applied — falls through to existing
        guardrail domain checks.
        """
        if not self.scope.allowed_domains:
            return None  # No domain restriction from scope

        try:
            parsed = urlparse(url)
            domain = (parsed.hostname or "").lower()
        except Exception:
            return f"Invalid URL: {url}"

        if not domain:
            return None

        for pattern in self.scope.allowed_domains:
            if fnmatch.fnmatch(domain, pattern):
                return None

        logger.info(
            "Scope blocked domain '%s' (allowed: %s)",
            domain,
            self.scope.allowed_domains,
        )
        return f"Domain '{domain}' not in task scope"
