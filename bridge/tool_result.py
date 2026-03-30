"""Adapters for converting bridge results into model-facing tool payloads."""

from __future__ import annotations

from bridge import ActionResult


def _image_block(b64: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
    }


def screenshot_result(b64: str) -> dict:
    return {"content": [_image_block(b64)], "is_error": False}


def text_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": False}


def screenshot_and_text_result(b64: str, text: str) -> dict:
    return {
        "content": [_image_block(b64), {"type": "text", "text": text}],
        "is_error": False,
    }


def error_result(msg: str) -> dict:
    return {
        "content": [{"type": "text", "text": f"Error: {msg}"}],
        "is_error": True,
    }


def action_result_to_tool_result(result: ActionResult) -> dict:
    """Convert an ActionResult into Anthropic's tool_result format."""
    if result.error:
        return error_result(result.error)
    if result.screenshot_b64 and result.text:
        return screenshot_and_text_result(result.screenshot_b64, result.text)
    if result.screenshot_b64:
        return screenshot_result(result.screenshot_b64)
    if result.text:
        return text_result(result.text)
    return text_result("Done")
