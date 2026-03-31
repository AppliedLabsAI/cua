"""Tests for ModalDictRunRegistry in api.run_registry."""

from __future__ import annotations

from unittest.mock import MagicMock

from api.run_registry import (
    _SERIALIZABLE_FIELDS,
    ModalDictRunRegistry,
    RunHandle,
    RunPhase,
)


def _make_handle(run_id: str = "run-123") -> RunHandle:
    """Return a minimal RunHandle with a mock sandbox."""
    return RunHandle(
        run_id=run_id,
        sandbox=MagicMock(),
        status_base_url="https://example.com/status",
        phase=RunPhase.RUNNING,
    )


def _make_registry() -> tuple[ModalDictRunRegistry, MagicMock]:
    """Return a registry and the underlying mock dict."""
    modal_dict = MagicMock()
    registry = ModalDictRunRegistry(modal_dict)
    return registry, modal_dict


class TestSerializableFields:
    def test_does_not_include_sandbox(self):
        assert "sandbox" not in _SERIALIZABLE_FIELDS

    def test_includes_run_id(self):
        assert "run_id" in _SERIALIZABLE_FIELDS

    def test_includes_status_base_url(self):
        assert "status_base_url" in _SERIALIZABLE_FIELDS

    def test_includes_phase(self):
        assert "phase" in _SERIALIZABLE_FIELDS


class TestModalDictRunRegistryAdd:
    def test_add_stores_handle_in_local_cache(self):
        registry, _ = _make_registry()
        handle = _make_handle("run-1")

        registry.add(handle)

        assert registry._local["run-1"] is handle

    def test_add_calls_dict_put_with_serialized_fields(self):
        registry, modal_dict = _make_registry()
        handle = _make_handle("run-1")

        registry.add(handle)

        modal_dict.put.assert_called_once_with(
            "run-1",
            handle.model_dump(include=_SERIALIZABLE_FIELDS),
        )

    def test_add_dict_put_does_not_include_sandbox(self):
        registry, modal_dict = _make_registry()
        handle = _make_handle("run-1")

        registry.add(handle)

        _args, _kwargs = modal_dict.put.call_args
        serialized = _args[1]
        assert "sandbox" not in serialized

    def test_add_local_cache_retained_when_dict_put_raises(self):
        registry, modal_dict = _make_registry()
        modal_dict.put.side_effect = RuntimeError("modal unavailable")
        handle = _make_handle("run-2")

        registry.add(handle)  # must not raise

        assert registry._local["run-2"] is handle

    def test_add_does_not_raise_when_dict_put_raises(self):
        registry, modal_dict = _make_registry()
        modal_dict.put.side_effect = Exception("network error")
        handle = _make_handle("run-3")

        # Failure must be swallowed
        registry.add(handle)


class TestModalDictRunRegistryGet:
    def test_get_returns_handle_from_local_cache_on_hit(self):
        registry, _ = _make_registry()
        handle = _make_handle("run-1")
        registry._local["run-1"] = handle

        result = registry.get("run-1")

        assert result is handle

    def test_get_returns_none_on_cache_miss(self):
        registry, modal_dict = _make_registry()

        result = registry.get("nonexistent-run")

        assert result is None

    def test_get_does_not_query_remote_on_cache_miss(self):
        registry, modal_dict = _make_registry()

        registry.get("nonexistent-run")

        modal_dict.get.assert_not_called()
        modal_dict.contains.assert_not_called()


class TestModalDictRunRegistryRemove:
    def test_remove_deletes_handle_from_local_cache(self):
        registry, _ = _make_registry()
        handle = _make_handle("run-1")
        registry._local["run-1"] = handle

        registry.remove("run-1")

        assert "run-1" not in registry._local

    def test_remove_calls_dict_pop(self):
        registry, modal_dict = _make_registry()
        handle = _make_handle("run-1")
        registry._local["run-1"] = handle

        registry.remove("run-1")

        modal_dict.pop.assert_called_once_with("run-1")

    def test_remove_returns_the_handle(self):
        registry, _ = _make_registry()
        handle = _make_handle("run-1")
        registry._local["run-1"] = handle

        result = registry.remove("run-1")

        assert result is handle

    def test_remove_returns_none_when_run_id_not_in_local(self):
        registry, _ = _make_registry()

        result = registry.remove("nonexistent-run")

        assert result is None

    def test_remove_returns_handle_when_dict_pop_raises(self):
        registry, modal_dict = _make_registry()
        modal_dict.pop.side_effect = RuntimeError("modal unavailable")
        handle = _make_handle("run-1")
        registry._local["run-1"] = handle

        result = registry.remove("run-1")

        assert result is handle

    def test_remove_does_not_raise_when_dict_pop_raises(self):
        registry, modal_dict = _make_registry()
        modal_dict.pop.side_effect = Exception("network error")
        handle = _make_handle("run-1")
        registry._local["run-1"] = handle

        # Failure must be swallowed
        registry.remove("run-1")


class TestModalDictRunRegistryContains:
    def test_contains_returns_true_for_local_entry(self):
        registry, _ = _make_registry()
        registry._local["run-1"] = _make_handle("run-1")

        assert registry.contains("run-1") is True

    def test_contains_does_not_query_remote_when_local_hit(self):
        registry, modal_dict = _make_registry()
        registry._local["run-1"] = _make_handle("run-1")

        registry.contains("run-1")

        modal_dict.contains.assert_not_called()

    def test_contains_falls_through_to_dict_on_local_miss(self):
        registry, modal_dict = _make_registry()
        modal_dict.contains.return_value = True

        result = registry.contains("remote-run")

        modal_dict.contains.assert_called_once_with("remote-run")
        assert result is True

    def test_contains_returns_false_when_not_in_local_or_remote(self):
        registry, modal_dict = _make_registry()
        modal_dict.contains.return_value = False

        result = registry.contains("nonexistent-run")

        assert result is False

    def test_contains_returns_false_when_dict_contains_raises(self):
        registry, modal_dict = _make_registry()
        modal_dict.contains.side_effect = Exception("modal unavailable")

        result = registry.contains("run-1")

        assert result is False
