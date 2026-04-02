"""Tests for the session memory module."""

from __future__ import annotations

from agent.memory import SessionMemory, _extract_finding
from bridge import DOM_MARKER
from bridge.url_utils import compact_url, extract_visited_urls, normalize_url


class TestSessionMemoryRecord:
    def test_empty_render(self):
        mem = SessionMemory()
        assert mem.render() == ""

    def test_goto_records_page_visit(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="goto",
            tool_input={"url": "https://example.com/dashboard"},
            input_summary="navigate to https://example.com/dashboard",
            result_text="Page loaded",
            success=True,
        )
        assert len(mem.pages) == 1
        assert mem.pages[0].url == "https://example.com/dashboard"
        assert mem.pages[0].step == 1

    def test_failed_goto_does_not_record_page(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="goto",
            tool_input={"url": "https://example.com"},
            input_summary="navigate to https://example.com",
            result_text=None,
            success=False,
        )
        assert len(mem.pages) == 0

    def test_click_does_not_record_page(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="click",
            tool_input={"selector": "#btn"},
            input_summary="click '#btn'",
            result_text="Clicked button",
            success=True,
        )
        assert len(mem.pages) == 0
        assert len(mem.actions) == 1

    def test_execute_sequence_with_goto_records_page(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="execute_sequence",
            tool_input={
                "steps": [
                    {"action": "goto", "url": "https://example.com"},
                    {"action": "click", "selector": "#btn"},
                ]
            },
            input_summary="execute 2-step sequence",
            result_text="Sequence complete",
            success=True,
        )
        assert len(mem.pages) == 1
        assert mem.pages[0].url == "https://example.com"

    def test_click_navigation_records_page_when_visited_urls_provided(self):
        mem = SessionMemory()
        mem.record(
            step=2,
            action="click",
            tool_input={"selector": "a[href='/dashboard']"},
            input_summary="click 'a[href=/dashboard]'",
            result_text="Opened dashboard",
            success=True,
            visited_urls=["https://example.com/dashboard"],
        )
        assert len(mem.pages) == 1
        assert mem.pages[0].url == "https://example.com/dashboard"

    def test_actions_always_recorded(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="click",
            tool_input={"selector": "#a"},
            input_summary="click '#a'",
            result_text="OK",
            success=True,
        )
        mem.record(
            step=2,
            action="scroll",
            tool_input={"direction": "down"},
            input_summary="scroll down 3x",
            result_text="Scrolled",
            success=True,
        )
        assert len(mem.actions) == 2
        assert mem.actions[0].summary == "click '#a'"
        assert mem.actions[1].summary == "scroll down 3x"

    def test_finding_extracted_into_action_entry(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="goto",
            tool_input={"url": "https://example.com"},
            input_summary="navigate",
            result_text=f"Dashboard loaded with 3 projects\n{DOM_MARKER}\n<div>...</div>",
            success=True,
        )
        assert mem.actions[0].finding == "Dashboard loaded with 3 projects"

    def test_trivial_results_produce_empty_finding(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="click",
            tool_input={"selector": "#btn"},
            input_summary="click",
            result_text="OK",
            success=True,
        )
        assert mem.actions[0].finding == ""

    def test_failed_actions_produce_empty_finding(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="click",
            tool_input={"selector": "#btn"},
            input_summary="click",
            result_text="Element not found",
            success=False,
        )
        assert mem.actions[0].finding == ""


class TestSessionMemoryRender:
    def test_render_includes_steps_completed(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="click",
            tool_input={"selector": "#a"},
            input_summary="click '#a'",
            result_text="Clicked",
            success=True,
        )
        output = mem.render()
        assert "Steps completed: 1" in output

    def test_render_includes_pages_visited(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="goto",
            tool_input={"url": "https://example.com/dash"},
            input_summary="navigate",
            result_text="Loaded",
            success=True,
        )
        output = mem.render()
        assert "Pages visited:" in output
        assert "example.com/dash" in output

    def test_render_deduplicates_pages(self):
        mem = SessionMemory()
        for step in (1, 5):
            mem.record(
                step=step,
                action="goto",
                tool_input={"url": "https://example.com/page"},
                input_summary="navigate",
                result_text="Loaded",
                success=True,
            )
        output = mem.render()
        assert "example.com/page (step 1, 5)" in output
        assert output.count("example.com/page") == 1

    def test_render_shows_finding_inline_with_action(self):
        mem = SessionMemory()
        mem.record(
            step=3,
            action="extract",
            tool_input={"selector": "table"},
            input_summary="extract text from 'table'",
            result_text="Found 15 entries in Q1 report",
            success=True,
        )
        output = mem.render()
        assert "Actions completed:" in output
        assert (
            "Step 3: extract text from 'table' → Found 15 entries in Q1 report"
            in output
        )

    def test_render_omits_arrow_for_trivial_findings(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="click",
            tool_input={"selector": "#btn"},
            input_summary="click '#btn'",
            result_text="Done",
            success=True,
        )
        output = mem.render()
        assert "Step 1: click '#btn'" in output
        assert "→" not in output

    def test_failed_actions_do_not_count_as_completed(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="click",
            tool_input={"selector": "#submit"},
            input_summary="click '#submit'",
            result_text="Element not found",
            success=False,
        )
        output = mem.render()
        assert "Steps completed: 0" in output
        assert "Failed attempts:" in output
        assert "Actions completed:" not in output

    def test_render_keeps_distinct_query_pages(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="goto",
            tool_input={"url": "https://example.com/search?q=alpha"},
            input_summary="navigate",
            result_text="Loaded",
            success=True,
        )
        mem.record(
            step=2,
            action="goto",
            tool_input={"url": "https://example.com/search?q=beta"},
            input_summary="navigate",
            result_text="Loaded",
            success=True,
        )
        output = mem.render()
        assert "example.com/search?q=alpha" in output
        assert "example.com/search?q=beta" in output

    def test_render_wraps_in_session_progress_tags(self):
        mem = SessionMemory()
        mem.record(
            step=1,
            action="click",
            tool_input={"selector": "#a"},
            input_summary="click",
            result_text="OK",
            success=True,
        )
        output = mem.render()
        assert output.startswith("<session_progress>")
        assert output.endswith("</session_progress>")


class TestExtractFinding:
    def test_strips_dom_content(self):
        text = f"Summary here\n{DOM_MARKER}\n<div>big dom</div>"
        assert _extract_finding(text) == "Summary here"

    def test_truncates_long_findings(self):
        text = "x" * 200
        result = _extract_finding(text)
        assert len(result) <= 154  # 150 + "..."
        assert result.endswith("...")

    def test_empty_text(self):
        assert _extract_finding("") == ""

    def test_trivial_text_skipped(self):
        assert _extract_finding("OK") == ""
        assert _extract_finding("Done") == ""
        assert _extract_finding("done") == ""


class TestCompactUrl:
    def test_full_url(self):
        assert (
            compact_url("https://example.com/path/to/page")
            == "example.com/path/to/page"
        )

    def test_root_url(self):
        assert compact_url("https://example.com/") == "example.com"

    def test_no_path(self):
        assert compact_url("https://example.com") == "example.com"

    def test_preserves_query_string(self):
        assert (
            compact_url("https://example.com/search?q=alpha&sort=desc")
            == "example.com/search?q=alpha&sort=desc"
        )

    def test_invalid_url(self):
        result = compact_url("not-a-url")
        assert isinstance(result, str)


class TestNormalizeUrl:
    def test_preserves_case_sensitive_path_and_query(self):
        assert (
            normalize_url("https://Example.com/Path?q=AbC")
            == "https://example.com/Path?q=AbC"
        )

    def test_strips_fragment(self):
        assert (
            normalize_url("https://example.com/path#section")
            == "https://example.com/path"
        )


class TestExtractVisitedUrls:
    def test_prefers_final_url_for_click_navigation(self):
        assert extract_visited_urls(
            "click",
            {"selector": "a[href='/dashboard']"},
            page_url_before="https://example.com/home",
            page_url_after="https://example.com/dashboard",
        ) == ["https://example.com/dashboard"]

    def test_prefers_final_url_for_redirected_goto(self):
        assert extract_visited_urls(
            "goto",
            {"url": "https://example.com/login"},
            page_url_before="about:blank",
            page_url_after="https://example.com/dashboard",
        ) == ["https://example.com/dashboard"]

    def test_execute_sequence_preserves_intermediate_and_final_urls(self):
        assert extract_visited_urls(
            "execute_sequence",
            {
                "steps": [
                    {"action": "goto", "url": "https://example.com/login"},
                    {"action": "goto", "url": "https://example.com/reports"},
                ]
            },
            page_url_before="about:blank",
            page_url_after="https://example.com/reports?filter=today",
        ) == [
            "https://example.com/login",
            "https://example.com/reports?filter=today",
        ]
