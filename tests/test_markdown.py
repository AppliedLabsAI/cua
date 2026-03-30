"""Tests for bridge.markdown — HTML-to-markdown and smart truncation."""

from bridge.markdown import html_to_markdown, truncate_markdown


class TestHtmlToMarkdown:
    def test_headings(self):
        html = "<h1>Title</h1><h2>Section</h2><p>Body text.</p>"
        md = html_to_markdown(html)
        assert "# Title" in md
        assert "## Section" in md
        assert "Body text." in md

    def test_links_preserved(self):
        html = '<p>Visit <a href="https://example.com">Example</a> for more.</p>'
        md = html_to_markdown(html)
        assert "[Example](https://example.com)" in md

    def test_relative_links_resolved(self):
        html = '<p><a href="/docs/api">API docs</a></p>'
        md = html_to_markdown(html, base_url="https://example.com")
        assert "https://example.com/docs/api" in md

    def test_unordered_list(self):
        html = "<ul><li>First</li><li>Second</li><li>Third</li></ul>"
        md = html_to_markdown(html)
        assert "- First" in md
        assert "- Second" in md

    def test_ordered_list(self):
        html = "<ol><li>Alpha</li><li>Beta</li></ol>"
        md = html_to_markdown(html)
        assert "1." in md
        assert "Alpha" in md

    def test_bold_italic(self):
        html = "<p><strong>bold</strong> and <em>italic</em></p>"
        md = html_to_markdown(html)
        assert "**bold**" in md
        assert "*italic*" in md

    def test_code_block(self):
        html = '<pre><code class="language-python">def hello():\n    pass</code></pre>'
        md = html_to_markdown(html)
        assert "```" in md
        assert "def hello():" in md

    def test_inline_code(self):
        html = "<p>Use <code>pip install</code> to install.</p>"
        md = html_to_markdown(html)
        assert "`pip install`" in md

    def test_table(self):
        html = (
            "<table><thead><tr><th>Name</th><th>Value</th></tr></thead>"
            "<tbody><tr><td>A</td><td>1</td></tr></tbody></table>"
        )
        md = html_to_markdown(html)
        assert "Name" in md
        assert "Value" in md
        assert "|" in md

    def test_empty_html(self):
        assert html_to_markdown("") == ""

    def test_collapses_blank_lines(self):
        html = "<p>One</p><br><br><br><br><p>Two</p>"
        md = html_to_markdown(html)
        assert "\n\n\n" not in md

    def test_strips_javascript_links(self):
        html = '<p><a href="javascript:void(0)">Click</a></p>'
        md = html_to_markdown(html)
        assert "javascript:" not in md

    def test_strips_anchor_links(self):
        html = '<p><a href="#section">Jump</a></p>'
        md = html_to_markdown(html)
        assert "[Jump](#section)" not in md
        assert "Jump" in md


class TestTruncateMarkdown:
    def test_short_text_unchanged(self):
        text = "Short paragraph."
        assert truncate_markdown(text) == text

    def test_truncates_at_paragraph_boundary(self):
        para1 = "First paragraph. " * 50  # ~850 chars
        para2 = "Second paragraph. " * 50
        para3 = "Third paragraph. " * 50
        text = f"{para1}\n\n{para2}\n\n{para3}"
        result = truncate_markdown(text, max_chars=2000)
        assert "truncated" in result
        assert len(result) < len(text)
        # Should end at a paragraph boundary
        assert result.count("\n\n") >= 1

    def test_truncation_indicator(self):
        text = "x" * 3000
        result = truncate_markdown(text, max_chars=100)
        assert "3000 chars total" in result

    def test_exact_limit_not_truncated(self):
        text = "a" * 2000
        assert truncate_markdown(text, max_chars=2000) == text

    def test_falls_back_to_newline(self):
        # No double-newline, but has single newlines
        text = "line\n" * 500
        result = truncate_markdown(text, max_chars=100)
        assert "truncated" in result
        # Cut point is at a newline boundary, content before indicator stays reasonable
        content_before = result.split("[...truncated")[0].rstrip("\n")
        assert len(content_before) <= 100
