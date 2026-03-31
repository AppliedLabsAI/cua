"""Unit tests for bridge/background.py — BackgroundTasks."""

from __future__ import annotations

import logging

from bridge.background import BackgroundTasks

# ── helpers ─────────────────────────────────────────────────────────


async def _noop() -> None:
    """Coroutine that completes immediately without side effects."""


async def _fail(message: str = "boom") -> None:
    """Coroutine that raises a RuntimeError."""
    raise RuntimeError(message)


async def _set_flag(flag: list[bool]) -> None:
    """Coroutine that appends True to flag so callers can observe execution."""
    flag.append(True)


# ── schedule ────────────────────────────────────────────────────────


async def test_schedule_adds_task_to_internal_set():
    bt = BackgroundTasks()
    bt.schedule(_noop())
    assert len(bt._tasks) == 1


async def test_schedule_multiple_tasks_adds_all():
    bt = BackgroundTasks()
    bt.schedule(_noop())
    bt.schedule(_noop())
    bt.schedule(_noop())
    assert len(bt._tasks) == 3


async def test_schedule_returns_none():
    bt = BackgroundTasks()
    result = bt.schedule(_noop())
    assert result is None


# ── drain ────────────────────────────────────────────────────────────


async def test_drain_on_empty_set_does_not_raise():
    bt = BackgroundTasks()
    await bt.drain()  # must not raise


async def test_drain_awaits_pending_tasks():
    bt = BackgroundTasks()
    flag: list[bool] = []
    bt.schedule(_set_flag(flag))
    await bt.drain()
    assert flag == [True]


async def test_drain_multiple_tasks_all_complete():
    bt = BackgroundTasks()
    flags: list[bool] = []
    for _ in range(5):
        bt.schedule(_set_flag(flags))
    await bt.drain()
    assert len(flags) == 5


async def test_drain_is_idempotent_on_already_completed_tasks():
    """Second drain on a drained BackgroundTasks should not raise."""
    bt = BackgroundTasks()
    bt.schedule(_noop())
    await bt.drain()
    await bt.drain()  # must not raise


# ── done callback removes completed task ─────────────────────────────


async def test_completed_task_removed_from_internal_set():
    bt = BackgroundTasks()
    bt.schedule(_noop())
    # drain() uses gather(), which fully awaits the task; the done callback
    # fires synchronously inside gather, so the set is empty on return.
    await bt.drain()
    assert len(bt._tasks) == 0


async def test_multiple_completed_tasks_all_removed():
    bt = BackgroundTasks()
    for _ in range(3):
        bt.schedule(_noop())
    await bt.drain()
    assert len(bt._tasks) == 0


# ── exception handling ───────────────────────────────────────────────


async def test_failing_task_does_not_raise_from_drain(caplog):
    bt = BackgroundTasks()
    bt.schedule(_fail("intentional error"))
    with caplog.at_level(logging.WARNING, logger="bridge.background"):
        await bt.drain()
    # drain must return normally — no exception propagated


async def test_failing_task_logs_warning(caplog):
    bt = BackgroundTasks()
    bt.schedule(_fail("task went wrong"))
    with caplog.at_level(logging.WARNING, logger="bridge.background"):
        await bt.drain()
    assert any("task went wrong" in record.message for record in caplog.records)


async def test_failing_task_removed_from_internal_set(caplog):
    """The done callback must discard a failed task just like a successful one."""
    bt = BackgroundTasks()
    bt.schedule(_fail())
    with caplog.at_level(logging.WARNING, logger="bridge.background"):
        await bt.drain()
    assert len(bt._tasks) == 0


async def test_mix_of_passing_and_failing_tasks(caplog):
    """drain() collects all tasks; failed ones warn, successful ones run silently."""
    bt = BackgroundTasks()
    flag: list[bool] = []
    bt.schedule(_set_flag(flag))
    bt.schedule(_fail("partial failure"))
    bt.schedule(_set_flag(flag))
    with caplog.at_level(logging.WARNING, logger="bridge.background"):
        await bt.drain()
    assert len(flag) == 2
    assert any("partial failure" in record.message for record in caplog.records)
