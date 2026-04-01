"""Tests for blinders.scope — task scope extraction."""

import pytest

from blinders.scope import (
    ALL_ACTIONS,
    _detect_goal_type_fallback,
    _extract_domains,
    extract_task_scope,
)


class TestDetectGoalType:
    def test_read_keywords(self):
        assert _detect_goal_type_fallback("Find the price of iPhone") == "read"
        assert _detect_goal_type_fallback("What is the weather today?") == "read"
        assert _detect_goal_type_fallback("Tell me about Python") == "read"
        assert _detect_goal_type_fallback("Search for restaurants nearby") == "read"
        assert _detect_goal_type_fallback("Summarize this article") == "read"

    def test_fill_form_keywords(self):
        assert (
            _detect_goal_type_fallback("Fill out the registration form") == "fill_form"
        )
        assert _detect_goal_type_fallback("Submit the application") == "fill_form"
        assert _detect_goal_type_fallback("Sign up for an account") == "fill_form"
        assert _detect_goal_type_fallback("Register a new user") == "fill_form"

    def test_navigate_keywords(self):
        assert _detect_goal_type_fallback("Go to example.com") == "navigate"
        assert _detect_goal_type_fallback("Navigate to the settings page") == "navigate"
        assert _detect_goal_type_fallback("Open the dashboard") == "navigate"
        assert _detect_goal_type_fallback("Visit https://google.com") == "navigate"

    def test_interact_keywords(self):
        assert _detect_goal_type_fallback("Click the download button") == "interact"
        assert _detect_goal_type_fallback("Select the premium plan") == "interact"

    def test_default_is_read(self):
        assert _detect_goal_type_fallback("Do something complex") == "read"

    def test_fill_form_takes_precedence_over_navigate(self):
        # "fill" is checked before "go to"
        assert (
            _detect_goal_type_fallback("Go to example.com and fill the form")
            == "fill_form"
        )


class TestExtractDomains:
    def test_extracts_from_url(self):
        domains = _extract_domains("Go to https://example.com/page")
        assert "example.com" in domains
        assert "*.example.com" in domains

    def test_extracts_multiple_urls(self):
        domains = _extract_domains("Compare prices on amazon.com and ebay.com")
        assert "amazon.com" in domains
        assert "ebay.com" in domains

    def test_empty_when_no_urls(self):
        domains = _extract_domains("Find the best restaurant nearby")
        assert domains == []

    def test_extracts_www_prefix(self):
        domains = _extract_domains("Visit www.example.com")
        assert "www.example.com" in domains

    def test_deduplicates(self):
        domains = _extract_domains("Go to example.com and then example.com/about")
        # Should have example.com and *.example.com, not duplicates
        assert domains.count("example.com") == 1

    def test_ignores_email_addresses(self):
        domains = _extract_domains("Find the order for mjw.ntl@gmail.com")
        assert "gmail.com" not in domains
        assert "mjw.ntl" not in domains
        assert domains == []

    def test_ignores_email_but_keeps_real_urls(self):
        domains = _extract_domains(
            "Search user@example.com on https://dashboard.company.com"
        )
        assert "example.com" not in domains
        assert "dashboard.company.com" in domains

    def test_start_url_included(self):
        domains = _extract_domains(
            "Find the order for user@example.com",
            start_url="https://admin.company.com",
        )
        assert "admin.company.com" in domains
        assert "*.admin.company.com" in domains
        assert "example.com" not in domains

    def test_start_url_deduplicates_with_directive(self):
        domains = _extract_domains(
            "Go to example.com/page",
            start_url="https://example.com",
        )
        assert domains.count("example.com") == 1


class TestExtractTaskScope:
    @pytest.mark.asyncio
    async def test_read_scope(self):
        scope = await extract_task_scope("Find the price of iPhone on apple.com")
        assert scope.goal_type == "read"
        assert "apple.com" in scope.allowed_domains
        assert scope.visibility.show_forms is False
        assert scope.visibility.show_action_buttons is False
        assert "key_press" not in scope.allowed_actions
        assert "extract" in scope.allowed_actions
        assert "goto" in scope.allowed_actions

    @pytest.mark.asyncio
    async def test_fill_form_scope(self):
        scope = await extract_task_scope("Fill out the form on example.com/register")
        assert scope.goal_type == "fill_form"
        assert scope.visibility.show_forms is True
        assert scope.visibility.show_action_buttons is True
        assert scope.visibility.show_account_controls is True
        assert scope.allowed_actions == ALL_ACTIONS

    @pytest.mark.asyncio
    async def test_navigate_scope(self):
        scope = await extract_task_scope("Go to example.com")
        assert scope.goal_type == "navigate"
        assert scope.visibility.show_forms is False
        assert "key_press" not in scope.allowed_actions

    @pytest.mark.asyncio
    async def test_interact_scope(self):
        scope = await extract_task_scope("Click the download button on example.com")
        assert scope.goal_type == "interact"
        assert scope.visibility.show_forms is True
        assert scope.visibility.show_action_buttons is True
        assert scope.visibility.show_account_controls is False
        assert scope.allowed_actions == ALL_ACTIONS

    @pytest.mark.asyncio
    async def test_no_domains_is_permissive(self):
        scope = await extract_task_scope("Find the best restaurant nearby")
        assert scope.allowed_domains == []

    @pytest.mark.asyncio
    async def test_profile_overrides_widen_scope(self):
        from profiles.loader import Profile

        dashboard_profile = Profile(
            name="default",
            guardrail_overrides={
                "enable_llm_action_check": False,
                "max_urls_visited": 200,
            },
        )
        scope = await extract_task_scope(
            "Find the order total", profile=dashboard_profile
        )
        assert scope.goal_type == "read"
        # Profile with enable_llm_action_check=False should widen visibility
        assert scope.visibility.show_action_buttons is True
