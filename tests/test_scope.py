"""Tests for blinders.scope — task scope extraction."""

from blinders.scope import (
    ALL_ACTIONS,
    _detect_goal_type,
    _extract_domains,
    extract_task_scope,
)


class TestDetectGoalType:
    def test_read_keywords(self):
        assert _detect_goal_type("Find the price of iPhone") == "read"
        assert _detect_goal_type("What is the weather today?") == "read"
        assert _detect_goal_type("Tell me about Python") == "read"
        assert _detect_goal_type("Search for restaurants nearby") == "read"
        assert _detect_goal_type("Summarize this article") == "read"

    def test_fill_form_keywords(self):
        assert _detect_goal_type("Fill out the registration form") == "fill_form"
        assert _detect_goal_type("Submit the application") == "fill_form"
        assert _detect_goal_type("Sign up for an account") == "fill_form"
        assert _detect_goal_type("Register a new user") == "fill_form"

    def test_navigate_keywords(self):
        assert _detect_goal_type("Go to example.com") == "navigate"
        assert _detect_goal_type("Navigate to the settings page") == "navigate"
        assert _detect_goal_type("Open the dashboard") == "navigate"
        assert _detect_goal_type("Visit https://google.com") == "navigate"

    def test_interact_keywords(self):
        assert _detect_goal_type("Click the download button") == "interact"
        assert _detect_goal_type("Select the premium plan") == "interact"

    def test_default_is_interact(self):
        assert _detect_goal_type("Do something complex", use_llm=False) == "interact"

    def test_fill_form_takes_precedence_over_navigate(self):
        # "fill" is checked before "go to"
        assert _detect_goal_type("Go to example.com and fill the form") == "fill_form"


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


class TestExtractTaskScope:
    def test_read_scope(self):
        scope = extract_task_scope(
            "Find the price of iPhone on apple.com", use_llm=False
        )
        assert scope.goal_type == "read"
        assert "apple.com" in scope.allowed_domains
        assert scope.visibility.show_forms is False
        assert scope.visibility.show_action_buttons is False
        assert "key_press" not in scope.allowed_actions
        assert "extract" in scope.allowed_actions
        assert "goto" in scope.allowed_actions

    def test_fill_form_scope(self):
        scope = extract_task_scope(
            "Fill out the form on example.com/register", use_llm=False
        )
        assert scope.goal_type == "fill_form"
        assert scope.visibility.show_forms is True
        assert scope.visibility.show_action_buttons is True
        assert scope.visibility.show_account_controls is True
        assert scope.allowed_actions == ALL_ACTIONS

    def test_navigate_scope(self):
        scope = extract_task_scope("Go to example.com", use_llm=False)
        assert scope.goal_type == "navigate"
        assert scope.visibility.show_forms is False
        assert "key_press" not in scope.allowed_actions

    def test_interact_scope(self):
        scope = extract_task_scope(
            "Click the download button on example.com", use_llm=False
        )
        assert scope.goal_type == "interact"
        assert scope.visibility.show_forms is True
        assert scope.visibility.show_action_buttons is True
        assert scope.visibility.show_account_controls is False
        assert scope.allowed_actions == ALL_ACTIONS

    def test_no_domains_is_permissive(self):
        scope = extract_task_scope("Find the best restaurant nearby", use_llm=False)
        assert scope.allowed_domains == []

    def test_profile_overrides_widen_scope(self):
        from profiles.loader import Profile

        research_profile = Profile(
            name="research",
            guardrail_overrides={
                "enable_llm_action_check": False,
                "max_urls_visited": 100,
            },
        )
        scope = extract_task_scope(
            "Research AI safety papers", profile=research_profile, use_llm=False
        )
        assert scope.goal_type == "read"
        # Research profile should widen visibility for action buttons
        assert scope.visibility.show_action_buttons is True
