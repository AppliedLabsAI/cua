"""Tests for conversation context pruning."""

from __future__ import annotations

from pydantic_ai import BinaryContent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)

from agent.context import DOM_MARKER, MAX_OLD_TEXT, MAX_SCREENSHOTS, prune_context


def _tool_return(text: str, tool_name: str = "browser_dom") -> ModelRequest:
    return ModelRequest(
        parts=[ToolReturnPart(tool_name=tool_name, content=text, tool_call_id="t")]
    )


def _tool_return_with_screenshot(text: str, img: bytes = b"jpeg") -> ModelRequest:
    return ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name="browser_dom",
                content=[text, BinaryContent(data=img, media_type="image/jpeg")],
                tool_call_id="t",
            )
        ]
    )


def _response(text: str = "ok", thinking: str | None = None) -> ModelResponse:
    parts: list = []
    if thinking:
        parts.append(ThinkingPart(content=thinking))
    parts.append(TextPart(content=text))
    parts.append(
        ToolCallPart(
            tool_name="browser_dom", args={"action": "click"}, tool_call_id="t"
        )
    )
    return ModelResponse(parts=parts)


def _get_tool_return(msg: ModelRequest) -> ToolReturnPart:
    """Extract the first ToolReturnPart from a ModelRequest."""
    for part in msg.parts:
        if isinstance(part, ToolReturnPart):
            return part
    raise ValueError("No ToolReturnPart found")


class TestScreenshotRemoval:
    def test_old_screenshots_removed(self):
        msgs: list = [
            _tool_return_with_screenshot("step 1"),
            _response(),
            _tool_return_with_screenshot("step 2"),
            _response(),
            _tool_return_with_screenshot("step 3"),
            _response(),
            _tool_return_with_screenshot("step 4"),
            _response(),
            # Recent (last KEEP_LAST)
            _tool_return_with_screenshot("step 5"),
            _response(),
        ]
        result = prune_context(msgs)

        # Count surviving screenshots
        screenshots = 0
        for msg in result:
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart) and isinstance(
                        part.content, list
                    ):
                        for item in part.content:
                            if isinstance(item, BinaryContent):
                                screenshots += 1
        assert screenshots == MAX_SCREENSHOTS

    def test_recent_screenshots_preserved(self):
        msgs: list = [
            _tool_return_with_screenshot("old"),
            _response(),
            # Last KEEP_LAST messages
            _tool_return_with_screenshot("recent 1"),
            _response(),
            _tool_return_with_screenshot("recent 2"),
            _response(),
        ]
        result = prune_context(msgs)
        # The most recent screenshot should survive
        recent_req = result[-2]
        assert isinstance(recent_req, ModelRequest)
        tr = _get_tool_return(recent_req)
        assert isinstance(tr.content, list)
        assert any(isinstance(item, BinaryContent) for item in tr.content)


class TestDomTruncation:
    def test_dom_truncated_in_old_messages(self):
        dom_text = f"Clicked\n\n{DOM_MARKER}\n<div>big dom content here</div>"
        msgs: list = [
            _tool_return(dom_text),
            _response(),
            _tool_return(dom_text),
            _response(),
            # Recent
            _tool_return(dom_text),
            _response(),
            _tool_return(dom_text),
            _response(),
        ]
        result = prune_context(msgs)

        # Old messages should have DOM removed
        old_req = result[0]
        assert isinstance(old_req, ModelRequest)
        tr = _get_tool_return(old_req)
        assert isinstance(tr.content, str)
        assert "[DOM removed]" in tr.content
        assert "big dom content" not in tr.content
        # Action summary preserved
        assert "Clicked" in tr.content

    def test_recent_dom_untouched(self):
        dom_text = f"Navigated\n\n{DOM_MARKER}\n<div>full dom</div>"
        msgs: list = [
            _tool_return("old"),
            _response(),
            # Recent
            _tool_return(dom_text),
            _response(),
            _tool_return(dom_text),
            _response(),
        ]
        result = prune_context(msgs)

        # Recent messages should keep full DOM
        recent_req = result[-2]
        assert isinstance(recent_req, ModelRequest)
        tr = _get_tool_return(recent_req)
        assert isinstance(tr.content, str)
        assert "full dom" in tr.content

    def test_dom_in_list_content(self):
        dom_text = f"Clicked\n\n{DOM_MARKER}\n<div>dom</div>"
        msgs: list = [
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="browser_dom", content=[dom_text], tool_call_id="t"
                    )
                ]
            ),
            _response(),
            _response(),
            _response(),
            # Recent padding
            _tool_return("r1"),
            _response(),
            _tool_return("r2"),
            _response(),
        ]
        result = prune_context(msgs)
        old_req = result[0]
        assert isinstance(old_req, ModelRequest)
        tr = _get_tool_return(old_req)
        assert isinstance(tr.content, list)
        first = tr.content[0]
        assert isinstance(first, str)
        assert "[DOM removed]" in first


class TestThinkingStripped:
    def test_thinking_stripped_from_old_responses(self):
        msgs: list = [
            _tool_return("a"),
            _response(thinking="let me think about this..."),
            # Recent
            _tool_return("b"),
            _response(thinking="recent thinking"),
            _tool_return("c"),
            _response(thinking="also recent"),
        ]
        result = prune_context(msgs)

        # Old response should have thinking removed
        old_resp = result[1]
        assert isinstance(old_resp, ModelResponse)
        assert not any(isinstance(p, ThinkingPart) for p in old_resp.parts)

        # Recent response should keep thinking
        recent_resp = result[-1]
        assert isinstance(recent_resp, ModelResponse)
        assert any(isinstance(p, ThinkingPart) for p in recent_resp.parts)


class TestLongTextTruncation:
    def test_long_text_truncated_in_list_content(self):
        long_text = "x" * 500
        msgs: list = [
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="browser_dom", content=[long_text], tool_call_id="t"
                    )
                ]
            ),
            _response(),
            # Recent
            _tool_return("r1"),
            _response(),
            _tool_return("r2"),
            _response(),
        ]
        result = prune_context(msgs)
        old_req = result[0]
        assert isinstance(old_req, ModelRequest)
        tr = _get_tool_return(old_req)
        assert isinstance(tr.content, list)
        first = tr.content[0]
        assert isinstance(first, str)
        assert len(first) <= MAX_OLD_TEXT + 3  # +3 for "..."


class TestEdgeCases:
    def test_short_history_untouched(self):
        msgs: list = [_tool_return("a"), _response()]
        result = prune_context(msgs)
        assert len(result) == 2
        first = result[0]
        assert isinstance(first, ModelRequest)
        tr = _get_tool_return(first)
        assert tr.content == "a"

    def test_empty_history(self):
        assert prune_context([]) == []
