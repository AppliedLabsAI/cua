"""Tests for interaction recording used by playbook generation."""

from __future__ import annotations

import json

from scripts.record_interaction import InteractionRecorder


def test_interaction_recorder_ignores_scroll_events():
    recorder = InteractionRecorder()

    recorder.on_event(
        json.dumps(
            {
                "action": "scroll",
                "params": {"direction": "down", "amount": 1200},
            }
        )
    )

    assert recorder.interactions == []


def test_interaction_recorder_keeps_non_scroll_events():
    recorder = InteractionRecorder()

    recorder.on_event(
        json.dumps(
            {
                "action": "click",
                "params": {},
                "selector": {"primary": "text=Submit"},
                "elementText": "Submit",
                "elementTag": "button",
            }
        )
    )

    assert len(recorder.interactions) == 1
    assert recorder.interactions[0]["action"] == "click"
