"""Playbook store — load, save, and match playbooks from YAML definitions."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from playbooks.schema import (
    Playbook,
    PlaybookStep,
)

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).parent / "definitions"


class PlaybookStore:
    """Manages playbook definitions on disk."""

    def __init__(self, playbook_dir: str | Path | None = None) -> None:
        self._dir = Path(playbook_dir) if playbook_dir else _DEFAULT_DIR
        self._cache: dict[str, Playbook] = {}

    def load(self, playbook_id: str) -> Playbook:
        """Load a single playbook by ID."""
        if playbook_id in self._cache:
            return self._cache[playbook_id]

        path = self._dir / f"{playbook_id}.yaml"
        try:
            playbook = self._parse_yaml(path)
        except FileNotFoundError:
            raise FileNotFoundError(f"Playbook not found: {path}") from None
        self._cache[playbook.id] = playbook
        return playbook

    def list_all(self) -> list[Playbook]:
        """Load and return all playbooks in the definitions directory."""
        if not self._dir.exists():
            return []

        playbooks: list[Playbook] = []
        for path in sorted(self._dir.glob("*.yaml")):
            pb_id = path.stem
            if pb_id in self._cache:
                playbooks.append(self._cache[pb_id])
                continue
            try:
                pb = self._parse_yaml(path)
                self._cache[pb.id] = pb
                playbooks.append(pb)
            except Exception as exc:
                logger.warning("Failed to parse playbook %s: %s", path.name, exc)
        return playbooks

    def save(self, playbook: Playbook) -> None:
        """Save a playbook to YAML."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{playbook.id}.yaml"

        data = self._to_dict(playbook)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        self._cache[playbook.id] = playbook
        logger.info("Playbook saved: %s", path)

    def match_directive(self, directive: str) -> Playbook | None:
        """Find the best matching playbook for a directive using tag matching.

        Returns None if no playbook matches.
        """
        playbooks = self.list_all()
        if not playbooks:
            return None

        directive_lower = directive.lower()
        best: Playbook | None = None
        best_score = 0

        for pb in playbooks:
            score = 0
            for tag in pb.tags:
                tag_lower = tag.lower()
                if tag_lower in directive_lower:
                    # Longer tag matches are worth more
                    score += len(tag_lower)

            if score > best_score:
                best_score = score
                best = pb

        if best and best_score > 0:
            logger.info(
                "Matched directive to playbook '%s' (score=%d)",
                best.id,
                best_score,
            )

        return best

    # -----------------------------------------------------------------------
    # YAML parsing
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_yaml(path: Path) -> Playbook:
        """Parse a YAML file into a Playbook."""
        with open(path) as f:
            data = yaml.safe_load(f)

        try:
            steps = []
            for step_data in data.get("steps", []):
                # Handle selector shorthand: plain string → SelectorStrategy
                sel_data = step_data.get("selector")
                if isinstance(sel_data, str):
                    step_data = {**step_data, "selector": {"primary": sel_data}}

                steps.append(PlaybookStep.model_validate(step_data))

            playbook_data = {**data, "steps": steps}
            # Default name to id if not provided
            playbook_data.setdefault("name", data["id"])
            return Playbook.model_validate(playbook_data)
        except ValidationError as exc:
            raise ValueError(f"Invalid playbook {path.name}: {exc}") from exc

    @staticmethod
    def _to_dict(playbook: Playbook) -> dict:
        """Convert a Playbook to a YAML-serializable dict.

        Uses exclude_defaults to produce compact output, then ensures
        required structural fields are always present.
        """
        result = playbook.model_dump(
            exclude_defaults=True,
            exclude_none=True,
            by_alias=True,
        )
        # Always include auth_required and steps even when they match defaults
        result["auth_required"] = playbook.auth_required
        if "steps" not in result:
            result["steps"] = []
        return result
