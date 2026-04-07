# Playbook System

Playbooks define deterministic action sequences for known dashboard workflows. Each step has a selector fallback chain and post-action verification.

## Creating a Playbook from a Recording

Instead of writing YAML by hand, use the `/create-playbook` Claude Code skill to record a browser session and generate a playbook automatically:

1. Run `/create-playbook` in Claude Code
2. Provide a directive (e.g., "Cancel order #12345") and the dashboard URL
3. A browser window opens — perform the workflow manually
4. Close the browser or press Ctrl+Shift+S when done
5. Claude reads the recorded interactions and generates an optimized playbook with selector fallback chains, verification steps, and parameterized values

The recording captures clicks, typing, navigation, and selections, then generates multiple selector candidates per element (role, text, attribute, structural) for resilience against UI changes.

You can also record standalone without the skill:

```bash
python scripts/record_interaction.py \
  --start-url https://dashboard.internal/orders \
  --output output/recording.json
```

## Defining a Playbook Manually

You can also write playbooks by hand. See the examples in `playbooks/definitions/` for the full YAML schema — each file demonstrates steps, selector fallback chains, parameter injection, and verification.

## Available Actions

Playbooks support these actions (same as the LLM agent's `browser_dom` tool):

| Action | Description | Playbook-specific notes |
|---|---|---|
| `goto(url)` | Navigate to a URL | Supports `{param}` placeholders in URL |
| `click(selector)` | Click an element | Uses selector fallback chain |
| `key_press(text, key)` | Type text and/or press a key | `text` supports `{param}` placeholders; if selector provided, uses `page.fill()` |
| `scroll(direction, amount)` | Scroll the page | Direction: up/down/left/right |
| `wait_for(selector, state)` | Wait for element state | Comma-separated selectors supported |
| `extract(selector, mode)` | Extract text, HTML, or form values | Modes: `text` (default), `html`, `value`. Output shown in results |
| `select(selector, value)` | Select a dropdown option | Uses selector fallback chain |

## Playbook Features

- **Selector fallback chains**: Each step tries `primary` selector first, then `fallbacks` in order (800ms timeout per selector). Handles dashboard UI changes without playbook edits.
- **Step verification**: Assert expected state after every action — `expect_url_contains`, `expect_element_visible`, `expect_element_gone`, `expect_text_on_page`. Catches failures immediately.
- **Parameter injection**: `{param_name}` placeholders in selectors, params, descriptions, and verification are replaced at runtime.
- **Step outputs**: `extract` steps can set `store_as` and later steps can reference `{stored_value}` in URLs, selectors, and verification without custom JavaScript.
- **Data extraction**: `extract` steps capture text from elements and display it in the output.
- **Declarative navigation**: Prefer `extract` + `store_as` + `goto` over raw JavaScript for FK traversal and similar flows.
- **Per-playbook guardrails**: Each playbook can specify its own `guardrails` config (private networks, LLM checks, URL limits). Used both during playbook execution and when falling back to the LLM agent.
- **Failure handling**: Each step can specify `on_failure`:
  - `llm_recover` (default) — after 2 failures, hands off ALL remaining steps to the full LLM agent
  - `retry` — retry without LLM fallback
  - `abort` — stop immediately
- **Authentication**: Built-in login flow that detects common form patterns (email/username + password fields) and performs fresh login when `auth_required: true`.

## Execution Tiers

| Tier | When | LLM Calls | Latency |
|------|------|-----------|---------|
| Playbook hit | Known flow, selectors match | 0 | 1-5s |
| Playbook + LLM handoff | Known flow, page changed | 5-15 | 15-30s |
| Full LLM agent | No playbook, unknown flow | 5-15 | 30-60s |

## LLM Handoff on Failure

When a playbook step fails twice, CUA doesn't try to patch individual steps — the page state has likely diverged from what the playbook expects. Instead, it hands off the **entire remaining task** to the full LLM agent with:
- The playbook's name, description, and guardrails config
- What failed and the current page URL
- Descriptions of ALL remaining steps

The LLM agent takes full browser control and drives to completion, just like a normal CUA run but with a focused directive.
