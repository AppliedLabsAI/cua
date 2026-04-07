"""Directive parser — map natural language directives to playbooks + parameters."""

from __future__ import annotations

import logging
import re

from playbooks.schema import Playbook
from playbooks.store import PlaybookStore

logger = logging.getLogger(__name__)


class DirectiveParser:
    """Parse directives into playbook matches with extracted parameters."""

    def __init__(self, store: PlaybookStore) -> None:
        self._store = store

    def parse(self, directive: str) -> tuple[Playbook, dict] | None:
        """Match a directive to a playbook and extract parameters.

        Returns (playbook, params) or None if no playbook matches.
        """
        playbook = self._store.match_directive(directive)
        if not playbook:
            logger.info("No playbook matched directive: %s", directive[:80])
            return None

        params = self._extract_params(directive, playbook)
        logger.info(
            "Parsed directive → playbook='%s', params=%s",
            playbook.id,
            params,
        )
        return playbook, params

    def extract_params_for_playbook(self, directive: str, playbook: Playbook) -> dict:
        """Extract parameters for an already-selected playbook."""
        params = self._extract_params(directive, playbook)
        logger.info(
            "Extracted params for explicit playbook '%s': %s",
            playbook.id,
            params,
        )
        return params

    def _extract_params(self, directive: str, playbook: Playbook) -> dict:
        """Extract parameter values from a directive.

        Strategy per parameter:
        1. If the parameter has a regex pattern, use it
        2. Otherwise, use type-specific heuristics (numbers, quoted strings)
        """
        params: dict = {}

        for param in playbook.parameters:
            value = None

            # Try explicit regex pattern
            if param.pattern:
                match = re.search(param.pattern, directive, re.IGNORECASE)
                if match:
                    value = match.group(1) if match.lastindex else match.group()

            # Type-specific fallbacks
            if value is None and param.type == "int":
                # Extract first number-like token (e.g., #12345, order 67890)
                match = re.search(r"#?(\d{3,})", directive)
                if match:
                    value = match.group(1)

            if value is None and param.type == "string":
                # Look for quoted strings or strings after common prepositions
                match = re.search(
                    rf'(?:{re.escape(param.name)})\s*[=:]\s*["\']?([^"\',]+)',
                    directive,
                    re.IGNORECASE,
                )
                if match:
                    value = match.group(1).strip()

            if value is not None:
                params[param.name] = value
            else:
                logger.warning(
                    "Could not extract parameter '%s' from directive",
                    param.name,
                )

        return params
