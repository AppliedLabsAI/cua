"""Shared JavaScript helper payloads used by browser execution paths."""

from __future__ import annotations

from pathlib import Path

JS_DIR = Path(__file__).parent / "scripts"
PAGE_CONTEXT_INIT_JS = (JS_DIR / "page_context.js").read_text()
CAPTCHA_DETECT_INIT_JS = (JS_DIR / "captcha_detect.js").read_text()
EXTRACT_VALUE_INIT_JS = (JS_DIR / "extract_value.js").read_text()
READABILITY_EXTRACT_INIT_JS = (JS_DIR / "readability_extract.js").read_text()
RECORDER_INIT_JS = (JS_DIR / "recorder.js").read_text()
