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

Playbooks support these actions:

| Action | Description | Playbook-specific notes |
|---|---|---|
| `goto(url)` | Navigate to a URL | Supports `{param}` placeholders in URL |
| `click(selector)` | Click an element | Uses selector fallback chain |
| `key_press(text, key)` | Type text and/or press a key | `text` supports `{param}` placeholders; if selector provided, uses `page.fill()` |
| `wait_for(selector, state)` | Wait for element state | Comma-separated selectors supported |
| `extract(selector, mode)` | Extract text, HTML, or form values | Modes: `text` (default), `html`, `value`. Output shown in results |
| `select(selector, value)` | Select a dropdown option | Uses selector fallback chain |
| `api_request(request)` | Call an authenticated API directly | Uses captured session artifacts plus static/default headers |

Manual `scroll` is intentionally not part of the playbook surface. Selector-based
steps already auto-scroll targets into view through Playwright when needed, and
API-backed flows should prefer `api_request` over viewport-driven scraping.

## Design Rules

Use these rules when authoring playbooks:

- Prefer selector-driven actions: `click`, `key_press`, `wait_for`, `select`, and `extract`.
- Prefer `api_request` once an authenticated browser session exists and the data can be fetched directly.
- Do not model viewport-control behavior such as manual scrolling. If a step seems to require scroll, the selector strategy is usually too weak or the workflow should move to an API call.
- Use verification and `store_as` to make steps deterministic instead of relying on page position or visual layout.

## Browser Session Handoff

For sites that require interactive browser login before their APIs become usable
(for example hCaptcha-gated portals), a playbook can now:

1. Open the login page in the browser
2. Authenticate with either:
   - `form_login` — CUA fills common username/password forms
   - `manual` — a person completes the interactive login in the browser window
3. Capture allowlisted session artifacts such as cookies or storage keys
4. Reuse those artifacts in deterministic `api_request` steps

The intended authoring shape is deliberately small:

```yaml
auth:
  mode: manual
  login_url: "https://login.example.com"
  success:
    cookie_present: "user_session"

capture:
  cookies:
    - name: "user_session"
      store_as: "session_cookie"
  static_headers:
    FFF-Auth: "V1.1"

steps:
  - action: api_request
    description: Fetch invoice details
    request:
      method: GET
      url: "https://api.example.com/invoices"
      query:
        email: "{customer_email}"
      cookies:
        user_session: "{session_cookie}"
      response:
        mode: json
```

Notes:

- Captured values are session artifacts, not raw login credentials.
- `static_headers` are merged into every `api_request` unless the step overrides
  the same header name.
- `response.mode: json_path` lets a step pull a single field out of a JSON
  response and store it via `store_as` for later steps.

## Playbook Features

- **Selector fallback chains**: Each step tries `primary` selector first, then `fallbacks` in order (800ms timeout per selector). Handles dashboard UI changes without playbook edits.
- **Step verification**: Assert expected state after every action — `expect_url_contains`, `expect_element_visible`, `expect_element_gone`, `expect_text_on_page`. Catches failures immediately.
- **Parameter injection**: `{param_name}` placeholders in selectors, params, descriptions, and verification are replaced at runtime.
- **Session capture**: `capture.cookies` and `capture.storage` copy allowlisted
  browser session artifacts into runtime params for later `api_request` steps.
- **Step outputs**: `extract` steps can set `store_as` and later steps can reference `{stored_value}` in URLs, selectors, and verification without custom JavaScript.
- **Data extraction**: `extract` steps capture text from elements and display it in the output.
- **Direct API replay**: `api_request` steps let a playbook call backend APIs
  directly once the authenticated browser session exists.
- **Declarative navigation**: Prefer `extract` + `store_as` + `goto` over raw JavaScript for FK traversal and similar flows.
- **Per-playbook guardrails**: Each playbook can specify its own `guardrails` config (private networks, LLM checks, URL limits). Used both during playbook execution and when falling back to the LLM agent.
- **Failure handling**: Each step can specify `on_failure`:
  - `llm_recover` (default) — after 2 failures, hands off ALL remaining steps to the full LLM agent
  - `retry` — retry without LLM fallback
  - `abort` — stop immediately
- **Authentication**: Built-in browser login flow plus allowlisted session-artifact capture for downstream API replay. Cross-run session persistence is not implemented.

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
