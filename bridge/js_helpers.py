"""Shared JavaScript helper payloads used by browser execution paths."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

JS_DIR = Path(__file__).parent / "scripts"
PAGE_CONTEXT_INIT_JS = (JS_DIR / "page_context.js").read_text()
CAPTCHA_DETECT_INIT_JS = (JS_DIR / "captcha_detect.js").read_text()
EXTRACT_VALUE_INIT_JS = (JS_DIR / "extract_value.js").read_text()
READABILITY_EXTRACT_INIT_JS = (JS_DIR / "readability_extract.js").read_text()
RECORDER_INIT_JS = (JS_DIR / "recorder.js").read_text()

# Stealth evasions JS — anti-bot fingerprint masking
# Ported from SeleniumBase's CDP Mode (educational)
try:
    STEALTH_EVASIONS_INIT_JS = (JS_DIR / "stealth_evasions.js").read_text()
except FileNotFoundError:
    STEALTH_EVASIONS_INIT_JS = ""
    logger.debug("stealth_evasions.js not found — stealth JS disabled")
