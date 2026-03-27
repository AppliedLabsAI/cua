"""Tests for agent.output — CuaOutput, extraction, and converters."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from actionlog.actions import ActionLog
from agent.output import (
    DEFAULT_OUTPUT_SCHEMA,
    CuaOutput,
    agent_result_to_output,
    collect_extracted_texts,
    extract_structured_output,
    playbook_result_to_output,
)
from agent.result import AgentResult
from playbooks.schema import PlaybookResult

# ---------------------------------------------------------------------------
# collect_extracted_texts
# ---------------------------------------------------------------------------


def _log(step: int, action: str, success: bool, text: str | None = None) -> ActionLog:
    return ActionLog(
        step=step,
        timestamp="",
        tool="browser_dom",
        action=action,
        input_summary="",
        tool_input={},
        duration_ms=0,
        success=success,
        result_text=text,
    )


class TestCollectExtractedTexts:
    def test_collects_successful_extracts(self):
        logs = [
            _log(1, "extract", True, "price: $29.99"),
            _log(2, "click", True, "ignored"),
            _log(3, "extract", True, "in stock"),
        ]
        assert collect_extracted_texts(logs) == ["price: $29.99", "in stock"]

    def test_skips_failed_extracts(self):
        logs = [
            _log(1, "extract", True, "good"),
            _log(2, "extract", False, "failed extract"),
        ]
        assert collect_extracted_texts(logs) == ["good"]

    def test_skips_none_text(self):
        logs = [
            _log(1, "extract", True, None),
            _log(2, "extract", True, "has text"),
        ]
        assert collect_extracted_texts(logs) == ["has text"]

    def test_empty_log(self):
        assert collect_extracted_texts([]) == []

    def test_no_extracts(self):
        logs = [_log(1, "click", True, "text"), _log(2, "goto", True, "text")]
        assert collect_extracted_texts(logs) == []


# ---------------------------------------------------------------------------
# extract_structured_output
# ---------------------------------------------------------------------------


class TestExtractStructuredOutput:
    @pytest.mark.asyncio
    async def test_extracts_json_from_llm_response(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = '{"product": "iPhone", "price": "$799"}'
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response

        schema = {
            "type": "object",
            "properties": {
                "product": {"type": "string"},
                "price": {"type": "string"},
            },
        }

        data, in_tok, out_tok = await extract_structured_output(
            summary="Found iPhone 16 for $799",
            extracted_texts=["iPhone 16 - $799"],
            output_schema=schema,
            client=mock_client,
        )

        assert data == {"product": "iPhone", "price": "$799"}
        assert in_tok == 100
        assert out_tok == 50
        mock_client.messages.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 50
        mock_response.usage.output_tokens = 30
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = '```json\n{"key": "value"}\n```'
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response

        data, in_tok, out_tok = await extract_structured_output(
            summary="test",
            extracted_texts=[],
            output_schema={"type": "object"},
            client=mock_client,
        )

        assert data == {"key": "value"}
        assert in_tok == 50
        assert out_tok == 30

    @pytest.mark.asyncio
    async def test_returns_none_on_invalid_json(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 50
        mock_response.usage.output_tokens = 30
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "not valid json"
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response

        data, in_tok, out_tok = await extract_structured_output(
            summary="test",
            extracted_texts=[],
            output_schema={"type": "object"},
            client=mock_client,
        )

        assert data is None
        # Tokens are still counted even when JSON parsing fails
        assert in_tok == 50
        assert out_tok == 30

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_context(self):
        mock_client = AsyncMock()

        data, in_tok, out_tok = await extract_structured_output(
            summary="",
            extracted_texts=[],
            output_schema={"type": "object"},
            client=mock_client,
        )

        assert data is None
        assert in_tok == 0
        assert out_tok == 0
        mock_client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_on_non_object_json(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 50
        mock_response.usage.output_tokens = 30
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = '["a", "b"]'
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response

        data, in_tok, out_tok = await extract_structured_output(
            summary="test",
            extracted_texts=[],
            output_schema={"type": "object"},
            client=mock_client,
        )

        assert data is None
        assert in_tok == 50
        assert out_tok == 30

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        mock_client = AsyncMock()
        mock_client.messages.create.side_effect = RuntimeError("API down")

        data, in_tok, out_tok = await extract_structured_output(
            summary="test",
            extracted_texts=["data"],
            output_schema={"type": "object"},
            client=mock_client,
        )

        assert data is None
        assert in_tok == 0
        assert out_tok == 0

    @pytest.mark.asyncio
    async def test_works_with_default_schema(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 80
        mock_response.usage.output_tokens = 40
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = '{"summary": "Found the price.", "result": "The price is $799", "details": {"price": "$799"}}'
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response

        data, in_tok, out_tok = await extract_structured_output(
            summary="Found the price",
            extracted_texts=["$799"],
            output_schema=DEFAULT_OUTPUT_SCHEMA,
            client=mock_client,
        )

        assert data is not None
        assert data["summary"] == "Found the price."
        assert data["result"] == "The price is $799"
        assert in_tok == 80
        assert out_tok == 40


# ---------------------------------------------------------------------------
# DEFAULT_OUTPUT_SCHEMA
# ---------------------------------------------------------------------------


class TestDefaultOutputSchema:
    def test_is_valid_json_schema(self):
        assert DEFAULT_OUTPUT_SCHEMA["type"] == "object"
        assert "result" in DEFAULT_OUTPUT_SCHEMA["properties"]
        assert "summary" in DEFAULT_OUTPUT_SCHEMA["properties"]
        assert "summary" in DEFAULT_OUTPUT_SCHEMA["required"]
        assert "result" in DEFAULT_OUTPUT_SCHEMA["required"]

    def test_is_serializable(self):
        s = json.dumps(DEFAULT_OUTPUT_SCHEMA)
        assert json.loads(s) == DEFAULT_OUTPUT_SCHEMA


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


class TestAgentResultToOutput:
    def test_success_uses_data_summary(self):
        """When data has a 'summary' key, it's used as the output summary and removed from data."""
        result = AgentResult(
            success=True,
            summary="Long verbose agent summary...",
            action_count=5,
            total_duration_ms=1000,
            total_input_tokens=500,
            total_output_tokens=200,
            data={"summary": "Short summary.", "result": "answer", "details": {}},
            extracted_texts=["text1"],
        )
        output = agent_result_to_output(result)

        assert output.status == "completed"
        assert output.summary == "Short summary."
        assert output.data is not None
        assert "summary" not in output.data  # popped from data
        assert output.data == {"result": "answer", "details": {}}
        assert output.error is None
        assert output.actions == 5
        assert output.duration_ms == 1000

    def test_success_falls_back_to_agent_summary(self):
        """When data has no 'summary' key, falls back to agent summary."""
        result = AgentResult(
            success=True,
            summary="Agent prose summary",
            action_count=3,
            total_duration_ms=500,
            data={"result": "answer"},
        )
        output = agent_result_to_output(result)

        assert output.summary == "Agent prose summary"

    def test_failure(self):
        result = AgentResult(
            success=False,
            summary="",
            action_count=3,
            error="Timed out",
            extracted_texts=["partial"],
        )
        output = agent_result_to_output(result)

        assert output.status == "failed"
        assert output.data is None
        assert output.error == "Timed out"

    def test_no_data(self):
        result = AgentResult(
            success=True,
            summary="Done",
            action_count=1,
        )
        output = agent_result_to_output(result)

        assert output.summary == "Done"
        assert output.data is None


class TestPlaybookResultToOutput:
    def test_success_with_data(self):
        result = PlaybookResult(
            playbook_id="p1",
            success=True,
            total_duration_ms=500,
            extracted_text="some data",
            data={"summary": "Playbook done.", "structured": True},
        )
        output = playbook_result_to_output(result)

        assert output.status == "completed"
        assert output.summary == "Playbook done."
        assert output.data is not None
        assert "summary" not in output.data
        assert output.error is None

    def test_failure_no_data(self):
        result = PlaybookResult(
            playbook_id="p2",
            success=False,
            error="Step 3 failed",
        )
        output = playbook_result_to_output(result)

        assert output.status == "failed"
        assert output.summary == ""
        assert output.error == "Step 3 failed"


# ---------------------------------------------------------------------------
# CuaOutput
# ---------------------------------------------------------------------------


class TestCuaOutput:
    def test_defaults(self):
        out = CuaOutput(status="completed")
        assert out.data is None
        assert out.summary == ""
        assert out.error is None
        assert out.actions == 0
        assert out.duration_ms == 0

    def test_full(self):
        out = CuaOutput(
            status="failed",
            data={"x": 1},
            summary="Failed task.",
            error="timeout",
            actions=10,
            duration_ms=5000,
        )
        assert out.status == "failed"
        assert out.data == {"x": 1}
        assert out.actions == 10
        assert out.duration_ms == 5000

    def test_to_dict(self):
        out = CuaOutput(
            status="completed",
            summary="Found the price.",
            data={"price": "$10"},
            actions=3,
            duration_ms=1500,
        )
        d = out.to_dict()
        assert d["status"] == "completed"
        assert d["summary"] == "Found the price."
        assert d["data"] == {"price": "$10"}
        assert d["actions"] == 3
        assert d["duration_ms"] == 1500
        assert d["error"] is None

    def test_to_dict_is_json_serializable(self):
        out = CuaOutput(
            status="completed",
            data={"nested": {"list": [1, 2, 3]}},
        )
        s = json.dumps(out.to_dict())
        parsed = json.loads(s)
        assert parsed["data"]["nested"]["list"] == [1, 2, 3]

    def test_no_extracted_texts_or_token_fields(self):
        """CuaOutput should not have extracted_texts or token fields."""
        out = CuaOutput(status="completed")
        d = out.to_dict()
        assert "extracted_texts" not in d
        assert "total_input_tokens" not in d
        assert "total_output_tokens" not in d


# ---------------------------------------------------------------------------
# Backwards compatibility — RunConfig / RunStatus
# ---------------------------------------------------------------------------


class TestAPIModelsBackwardsCompat:
    def test_run_config_without_output_schema(self):
        from api.models import RunConfig

        rc = RunConfig(directive="do something")
        assert rc.output_schema is None

    def test_run_config_with_output_schema(self):
        from api.models import RunConfig

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        rc = RunConfig(directive="do something", output_schema=schema)
        assert rc.output_schema == schema

    def test_run_status_defaults(self):
        from api.models import RunStatus

        rs = RunStatus(run_id="r1", status="completed")
        assert rs.data is None
        assert rs.extracted_texts == []
        assert rs.result is None
        assert rs.error is None

    def test_run_status_with_new_fields(self):
        from api.models import RunStatus

        rs = RunStatus(
            run_id="r1",
            status="completed",
            result="summary text",
            data={"price": "$10"},
            extracted_texts=["raw text"],
        )
        assert rs.result == "summary text"
        assert rs.data == {"price": "$10"}
        assert rs.extracted_texts == ["raw text"]

    def test_run_status_json_serialization(self):
        from api.models import RunStatus

        rs = RunStatus(
            run_id="r1",
            status="completed",
            data={"nested": {"key": [1, 2, 3]}},
        )
        dumped = rs.model_dump()
        assert dumped["data"] == {"nested": {"key": [1, 2, 3]}}
        assert dumped["extracted_texts"] == []
