"""Shared JavaScript helper payloads used by browser execution paths."""

from __future__ import annotations

from pathlib import Path

JS_DIR = Path(__file__).parent / "scripts"
DOM_SNAPSHOT_INIT_JS = (JS_DIR / "dom_snapshot.js").read_text()
SMART_EXTRACT_INIT_JS = (JS_DIR / "smart_extract.js").read_text()
CAPTCHA_DETECT_INIT_JS = (JS_DIR / "captcha_detect.js").read_text()
EXTRACT_VALUE_INIT_JS = (JS_DIR / "extract_value.js").read_text()
