"""HTML-to-markdown conversion and smart truncation for extract(body, markdown)."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from markdownify import MarkdownConverter


class _CuaMarkdownConverter(MarkdownConverter):
    """Customized converter tuned for LLM-friendly output.

    - GFM-style code fences instead of indented blocks
    - Inline links (not reference-style)
    - Strips excessive blank lines
    """

    def __init__(self, base_url: str = "") -> None:
        super().__init__(
            heading_style="atx",
            bullets="-",
            strong_em_symbol="*",
            code_language_callback=self._detect_language,
        )
        self._base_url = base_url

    @staticmethod
    def _detect_language(el: object) -> str | None:
        """Extract language hint from class='language-xxx' or 'highlight-xxx'."""
        cls = getattr(el, "get", lambda *_: None)("class", "") or ""
        if isinstance(cls, list):
            cls = " ".join(cls)
        for token in cls.split():
            for prefix in ("language-", "lang-", "highlight-"):
                if token.startswith(prefix):
                    return token[len(prefix) :]
        return None

    def convert_a(self, el: object, text: str, **kwargs: object) -> str:
        """Resolve relative URLs and produce inline markdown links."""
        href = el.get("href", "") if hasattr(el, "get") else ""  # type: ignore[union-attr]
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            return text
        if self._base_url and not href.startswith(("http://", "https://", "//")):
            href = urljoin(self._base_url, href)
        title = el.get("title", "") if hasattr(el, "get") else ""  # type: ignore[union-attr]
        if title:
            return f'[{text}]({href} "{title}")'
        return f"[{text}]({href})"


def html_to_markdown(html: str, base_url: str = "") -> str:
    """Convert clean HTML to GFM markdown.

    Preserves headings, links, lists, tables, code blocks, and emphasis.
    Resolves relative URLs against *base_url*.
    """
    md = _CuaMarkdownConverter(base_url=base_url).convert(html)
    # Collapse 3+ blank lines into 2
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def truncate_markdown(text: str, max_chars: int = 4000) -> str:
    """Truncate markdown at paragraph boundaries.

    Finds the last double-newline before *max_chars* and cuts there,
    so we never break inside a table row, code block, or heading.
    """
    if len(text) <= max_chars:
        return text

    # Look for the last paragraph break within the budget
    search_region = text[:max_chars]
    last_break = search_region.rfind("\n\n")

    if last_break > max_chars // 3:
        # Good break point found — use it
        truncated = text[:last_break]
    else:
        # No good paragraph break; fall back to last newline
        last_nl = search_region.rfind("\n")
        truncated = text[:last_nl] if last_nl > max_chars // 3 else text[:max_chars]

    return truncated + f"\n\n[...truncated, {len(text)} chars total]"
