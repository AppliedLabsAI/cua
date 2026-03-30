# Autoresearch: CUA Extraction & Agent Optimization

## Objective
Optimize the CUA browser automation agent's accuracy, reliability, and speed — measured by total steps taken across diverse tasks. The agent recently switched from `smart_extract.js` to a Jina Reader-inspired readability extraction + HTML→markdown pipeline. We're tuning this pipeline and the surrounding agent behavior.

## Metrics
- **Primary**: total_steps (unitless, lower is better) — composite of raw steps + 10-step penalty per failed case
- **Secondary**: raw_steps, success_count, duration_s, case_count

## How to Run
`./autoresearch.sh` — runs 3 diverse CUA tasks and outputs `METRIC name=number` lines.

## Test Cases
1. **admin-shop-hours**: Login to admin, navigate to conversations, find shop, extract opening hours (multi-step, login + navigation + extraction)
2. **example-dot-com**: Go to example.com, extract heading and paragraph (simple extraction)
3. **admin-count-shops**: Login to admin, navigate to shops, count them (login + counting)

## Files in Scope
- `bridge/markdown.py` — HTML→markdown converter and truncation
- `bridge/scripts/readability_extract.js` — JS-side content extraction (readability scoring)
- `bridge/page_actions.py` — extract action routing and markdown pipeline
- `agent/prompts.py` — system prompt (agent behavior, tool usage hints)
- `agent/tools.py` — tool definitions and parameter handling
- `bridge/execution.py` — action execution, DOM snapshots
- `bridge/browser.py` — browser manager, init scripts
- `bridge/js_helpers.py` — JS script loading
- `settings.py` — timeouts, model config

## Off Limits
- `evaluation/` — eval framework code (read-only reference)
- `recording/` — recording infrastructure
- `telemetry/` — telemetry/tracing

## Constraints
- No new Python dependencies beyond what's in pyproject.toml
- Don't break the existing eval suite
- Changes should be generalizable, not overfit to specific test cases

## Architecture Notes
- Agent uses Pydantic AI with Gemini Flash as primary model
- DOM snapshots are compact (2500 char auto-attached to goto/click responses)
- `extract(body, markdown)` is the default extraction mode
- Pipeline: readability_extract.js (finds main content) → JSON {html, title, url} → markdownify → truncate at 2000 chars
- `execute_sequence` batches multiple actions into one tool call (very efficient)
- The agent uses "blinders" (DOMBlinders) to filter DOM elements based on task scope

## What's Been Tried
- **Baseline**: Initial readability extraction + markdown pipeline. ~8 steps for admin-shop-hours, ~2-3 for example-dot-com.
