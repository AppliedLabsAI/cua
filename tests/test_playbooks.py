"""Tests for the playbook system — schema, store, parser, and runner."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from playbooks.parser import DirectiveParser
from playbooks.schema import (
    Playbook,
    PlaybookStep,
    SelectorStrategy,
    StepVerification,
)
from playbooks.store import PlaybookStore

# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSelectorStrategy:
    def test_all_selectors_includes_primary_and_fallbacks(self):
        s = SelectorStrategy(
            primary="text=Submit",
            fallbacks=["button.submit", "role=button[name=Submit]"],
        )
        assert s.all_selectors == [
            "text=Submit",
            "button.submit",
            "role=button[name=Submit]",
        ]

    def test_all_selectors_primary_only(self):
        s = SelectorStrategy(primary=".btn")
        assert s.all_selectors == [".btn"]


class TestPlaybookSchema:
    def test_defaults(self):
        pb = Playbook(id="test", name="Test")
        assert pb.auth_required is True
        assert pb.steps == []
        assert pb.tags == []
        assert pb.parameters == []
        assert pb.start_url == ""

    def test_step_defaults(self):
        step = PlaybookStep(action="click")
        assert step.on_failure == "llm_recover"
        assert step.selector is None
        assert step.verify is None


# ---------------------------------------------------------------------------
# Store tests
# ---------------------------------------------------------------------------


class TestPlaybookStore:
    def test_load_from_yaml(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            id: test_flow
            name: Test Flow
            description: A test playbook
            tags: ["test", "flow"]
            auth_required: false
            parameters:
              - name: item_id
                type: string
                description: The item to process
                pattern: "#?(\\\\d+)"
            steps:
              - action: goto
                params:
                  url: "https://example.com"
                verify:
                  expect_url_contains: "example.com"
                description: Navigate to example
              - action: click
                selector:
                  primary: "text=Submit"
                  fallbacks:
                    - "button.submit"
                description: Click submit
        """)
        (tmp_path / "test_flow.yaml").write_text(yaml_content)

        store = PlaybookStore(tmp_path)
        pb = store.load("test_flow")

        assert pb.id == "test_flow"
        assert pb.name == "Test Flow"
        assert pb.auth_required is False
        assert len(pb.steps) == 2
        assert pb.steps[0].action == "goto"
        assert pb.steps[0].verify is not None
        assert pb.steps[0].verify.expect_url_contains == "example.com"
        assert pb.steps[1].selector is not None
        assert pb.steps[1].selector.primary == "text=Submit"
        assert pb.steps[1].selector.fallbacks == ["button.submit"]
        assert len(pb.parameters) == 1
        assert pb.parameters[0].name == "item_id"

    def test_load_caches(self, tmp_path: Path):
        yaml_content = "id: cached\nname: Cached\nsteps: []"
        (tmp_path / "cached.yaml").write_text(yaml_content)

        store = PlaybookStore(tmp_path)
        pb1 = store.load("cached")
        pb2 = store.load("cached")
        assert pb1 is pb2

    def test_load_missing_raises(self, tmp_path: Path):
        store = PlaybookStore(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.load("nonexistent")

    def test_list_all(self, tmp_path: Path):
        for name in ["a", "b", "c"]:
            (tmp_path / f"{name}.yaml").write_text(
                f"id: {name}\nname: {name.upper()}\nsteps: []"
            )

        store = PlaybookStore(tmp_path)
        playbooks = store.list_all()
        assert len(playbooks) == 3
        assert {pb.id for pb in playbooks} == {"a", "b", "c"}

    def test_list_all_empty_dir(self, tmp_path: Path):
        store = PlaybookStore(tmp_path)
        assert store.list_all() == []

    def test_match_directive(self, tmp_path: Path):
        (tmp_path / "cancel.yaml").write_text(
            'id: cancel\nname: Cancel\ntags: ["cancel", "cancel order"]\nsteps: []'
        )
        (tmp_path / "update.yaml").write_text(
            'id: update\nname: Update\ntags: ["update", "update status"]\nsteps: []'
        )

        store = PlaybookStore(tmp_path)
        result = store.match_directive("Please cancel order #123")
        assert result is not None
        assert result.id == "cancel"

    def test_match_directive_prefers_longer_tag(self, tmp_path: Path):
        (tmp_path / "generic.yaml").write_text(
            'id: generic\nname: Generic\ntags: ["order"]\nsteps: []'
        )
        (tmp_path / "specific.yaml").write_text(
            'id: specific\nname: Specific\ntags: ["cancel order"]\nsteps: []'
        )

        store = PlaybookStore(tmp_path)
        result = store.match_directive("cancel order #456")
        assert result is not None
        assert result.id == "specific"

    def test_match_directive_no_match(self, tmp_path: Path):
        (tmp_path / "cancel.yaml").write_text(
            'id: cancel\nname: Cancel\ntags: ["cancel"]\nsteps: []'
        )

        store = PlaybookStore(tmp_path)
        result = store.match_directive("do something completely unrelated")
        assert result is None

    def test_save_and_reload(self, tmp_path: Path):
        pb = Playbook(
            id="saved",
            name="Saved Flow",
            tags=["save", "test"],
            steps=[
                PlaybookStep(
                    action="goto",
                    params={"url": "https://example.com"},
                    description="Navigate",
                ),
                PlaybookStep(
                    action="click",
                    selector=SelectorStrategy(
                        primary="text=OK",
                        fallbacks=[".ok-btn"],
                    ),
                    verify=StepVerification(expect_text_on_page="Success"),
                    description="Confirm",
                ),
            ],
        )

        store = PlaybookStore(tmp_path)
        store.save(pb)

        # Clear cache and reload
        store._cache.clear()
        loaded = store.load("saved")
        assert loaded.id == "saved"
        assert loaded.name == "Saved Flow"
        assert len(loaded.steps) == 2
        assert loaded.steps[1].selector is not None
        assert loaded.steps[1].selector.fallbacks == [".ok-btn"]
        assert loaded.steps[1].verify is not None
        assert loaded.steps[1].verify.expect_text_on_page == "Success"


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestDirectiveParser:
    def _make_store(self, tmp_path: Path) -> PlaybookStore:
        (tmp_path / "cancel_order.yaml").write_text(
            textwrap.dedent("""\
                id: cancel_order
                name: Cancel Order
                tags: ["cancel", "cancel order"]
                parameters:
                  - name: order_id
                    type: string
                    pattern: "#?(\\\\d{3,})"
                steps: []
            """)
        )
        return PlaybookStore(tmp_path)

    def test_parse_matches_and_extracts_params(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        parser = DirectiveParser(store)

        result = parser.parse("Cancel order #12345")
        assert result is not None
        playbook, params = result
        assert playbook.id == "cancel_order"
        assert params["order_id"] == "12345"

    def test_parse_no_match_returns_none(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        parser = DirectiveParser(store)

        result = parser.parse("Send an email to john@example.com")
        assert result is None

    def test_parse_extracts_int_type_param(self, tmp_path: Path):
        (tmp_path / "refund.yaml").write_text(
            textwrap.dedent("""\
                id: refund
                name: Refund
                tags: ["refund"]
                parameters:
                  - name: amount
                    type: int
                steps: []
            """)
        )
        store = PlaybookStore(tmp_path)
        parser = DirectiveParser(store)

        result = parser.parse("Process refund for $500")
        assert result is not None
        _, params = result
        assert params["amount"] == "500"


# ---------------------------------------------------------------------------
# Runner parameter injection tests (no browser needed)
# ---------------------------------------------------------------------------


class TestRunnerParamInjection:
    def test_inject_params_replaces_placeholders(self):
        from playbooks.runner import PlaybookRunner

        runner = PlaybookRunner.__new__(PlaybookRunner)

        step = PlaybookStep(
            action="key_press",
            params={"text": "{order_id}"},
            selector=SelectorStrategy(primary="input[data-id='{order_id}']"),
            verify=StepVerification(expect_text_on_page="Order {order_id}"),
            description="Type order {order_id}",
        )

        result = runner._inject_params(step, {"order_id": "12345"})
        assert result.params["text"] == "12345"
        assert result.selector.primary == "input[data-id='12345']"
        assert result.verify.expect_text_on_page == "Order 12345"
        assert result.description == "Type order 12345"

    def test_inject_params_no_params_returns_same(self):
        from playbooks.runner import PlaybookRunner

        runner = PlaybookRunner.__new__(PlaybookRunner)

        step = PlaybookStep(action="click", description="Click button")
        result = runner._inject_params(step, {})
        assert result.action == "click"
        assert result.description == "Click button"
