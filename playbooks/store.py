"""Playbook store — load, save, and match playbooks from YAML definitions."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from playbooks.schema import (
    KNOWN_FAILURE_MODES,
    KNOWN_PARAMETER_TYPES,
    KNOWN_PLAYBOOK_ACTIONS,
    Playbook,
    PlaybookGuardrails,
    PlaybookParameter,
    PlaybookStep,
    SelectorStrategy,
    StepVerification,
)

log = logging.getLogger(__name__)

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
                log.warning("Failed to parse playbook %s: %s", path.name, exc)
        return playbooks

    def save(self, playbook: Playbook) -> None:
        """Save a playbook to YAML."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{playbook.id}.yaml"

        data = self._to_dict(playbook)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        self._cache[playbook.id] = playbook
        log.info("Playbook saved: %s", path)

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
            log.info(
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

        steps = []
        for step_data in data.get("steps", []):
            action = step_data["action"]
            if action not in KNOWN_PLAYBOOK_ACTIONS:
                raise ValueError(f"Unknown playbook action '{action}' in {path.name}")

            on_failure = step_data.get("on_failure", "llm_recover")
            if on_failure not in KNOWN_FAILURE_MODES:
                raise ValueError(
                    f"Unknown on_failure mode '{on_failure}' in {path.name}"
                )

            selector = None
            sel_data = step_data.get("selector")
            if sel_data:
                if isinstance(sel_data, str):
                    selector = SelectorStrategy(primary=sel_data)
                else:
                    selector = SelectorStrategy(
                        primary=sel_data["primary"],
                        fallbacks=sel_data.get("fallbacks", []),
                        description=sel_data.get("description", ""),
                    )

            verify = None
            verify_data = step_data.get("verify")
            if verify_data:
                verify = StepVerification(
                    expect_url_contains=verify_data.get("expect_url_contains"),
                    expect_element_visible=verify_data.get("expect_element_visible"),
                    expect_element_gone=verify_data.get("expect_element_gone"),
                    expect_text_on_page=verify_data.get("expect_text_on_page"),
                    timeout_ms=verify_data.get("timeout_ms", 5000),
                )

            steps.append(
                PlaybookStep(
                    action=action,
                    params=step_data.get("params", {}),
                    selector=selector,
                    verify=verify,
                    description=step_data.get("description", ""),
                    on_failure=on_failure,
                    store_as=step_data.get("store_as", ""),
                )
            )

        parameters = []
        for param_data in data.get("parameters", []):
            parameter_type = param_data.get("type", "string")
            if parameter_type not in KNOWN_PARAMETER_TYPES:
                raise ValueError(
                    f"Unknown parameter type '{parameter_type}' in {path.name}"
                )
            parameters.append(
                PlaybookParameter(
                    name=param_data["name"],
                    type=parameter_type,
                    description=param_data.get("description", ""),
                    inject_into=param_data.get("inject_into", ""),
                    pattern=param_data.get("pattern", ""),
                )
            )

        return Playbook(
            id=data["id"],
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            parameters=parameters,
            auth_required=data.get("auth_required", True),
            steps=steps,
            tags=data.get("tags", []),
            start_url=data.get("start_url", ""),
            guardrails=PlaybookGuardrails.from_dict(data.get("guardrails")),
        )

    @staticmethod
    def _to_dict(playbook: Playbook) -> dict:
        """Convert a Playbook to a YAML-serializable dict."""
        steps = []
        for step in playbook.steps:
            step_dict: dict = {"action": step.action}
            if step.params:
                step_dict["params"] = step.params
            if step.selector:
                sel: dict = {"primary": step.selector.primary}
                if step.selector.fallbacks:
                    sel["fallbacks"] = step.selector.fallbacks
                if step.selector.description:
                    sel["description"] = step.selector.description
                step_dict["selector"] = sel
            if step.verify:
                v = step.verify
                verify_dict: dict = {}
                if v.expect_url_contains:
                    verify_dict["expect_url_contains"] = v.expect_url_contains
                if v.expect_element_visible:
                    verify_dict["expect_element_visible"] = v.expect_element_visible
                if v.expect_element_gone:
                    verify_dict["expect_element_gone"] = v.expect_element_gone
                if v.expect_text_on_page:
                    verify_dict["expect_text_on_page"] = v.expect_text_on_page
                if v.timeout_ms != 5000:
                    verify_dict["timeout_ms"] = v.timeout_ms
                step_dict["verify"] = verify_dict
            if step.description:
                step_dict["description"] = step.description
            if step.on_failure != "llm_recover":
                step_dict["on_failure"] = step.on_failure
            if step.store_as:
                step_dict["store_as"] = step.store_as
            steps.append(step_dict)

        params = []
        for p in playbook.parameters:
            pd: dict = {"name": p.name}
            if p.type != "string":
                pd["type"] = p.type
            if p.description:
                pd["description"] = p.description
            if p.inject_into:
                pd["inject_into"] = p.inject_into
            if p.pattern:
                pd["pattern"] = p.pattern
            params.append(pd)

        result: dict = {
            "id": playbook.id,
            "name": playbook.name,
        }
        if playbook.description:
            result["description"] = playbook.description
        if playbook.tags:
            result["tags"] = playbook.tags
        result["auth_required"] = playbook.auth_required
        if playbook.start_url:
            result["start_url"] = playbook.start_url
        if playbook.guardrails.has_overrides():
            result["guardrails"] = playbook.guardrails.to_dict()
        if params:
            result["parameters"] = params
        result["steps"] = steps

        return result
