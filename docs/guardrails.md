# Guardrails

CUA uses a layered safety architecture combining proactive observation control with runtime checks. Playbook execution bypasses most of these (pre-approved flows), but the LLM fallback path enforces all layers.

## Cognitive Blinders

The primary safety mechanism is **Cognitive Blinders** — a proactive observation filtering system that controls what the agent can see, rather than reactively blocking what it tries to do.

**The core insight**: if the agent can't see a "delete account" button, it can't click it. If it can't see injected instructions in a sidebar ad, it can't follow them.

```mermaid
graph LR
    A["User Directive"] --> B["Task Scope<br/>Extraction"]
    B --> C["DOM<br/>Blinders"]
    C --> D["Filtered<br/>DOM"]
    D --> E["Agent"]
    E --> F["Scope Verifier +<br/>Action Validator"]
    F -->|Safe| G["Execute"]
    F -->|Blocked| H["Feedback"]

    style A fill:#e8f5e9
    style D fill:#e3f2fd
    style G fill:#e8f5e9
    style H fill:#ffebee
```

### How It Works

**1. Task Scope Extraction** — Before the agent sees any web content, the directive is classified into a goal type that determines what the agent can see and do.

| Goal Type | Forms | Dangerous Buttons | Account Controls | `key_press` | `execute_sequence` |
|---|---|---|---|---|---|
| `read` | Hidden | Hidden | Hidden | Blocked | Blocked |
| `navigate` | Hidden | Hidden | Hidden | Blocked | Blocked |
| `interact` | Visible | Visible | Hidden | Allowed | Allowed |
| `fill_form` | Visible | Visible | Visible | Allowed | Allowed |

**2. DOM Blinders** — The DOM snapshot sent to the agent is filtered at two levels:

| Level | Where | What it does |
|---|---|---|
| **JS-side** | `page_context.js` in browser | Filters elements by category (forms, action buttons, account controls) based on task scope via the shared `__shouldShow` filter. Elements are removed before they leave the browser. |
| **Python-side** | `blinders/filters.py` | Scans for prompt injection patterns (`"ignore previous instructions"`, `SYSTEM:`, `[INST]` tokens) and redacts them. Wraps content with provenance markers (`[web-content-start/end]`). |

**3. Scope Verifier + Action Validator** — Multi-layer pre-execution check:

| Layer | Speed | What it checks |
|---|---|---|
| **Deterministic** | ~25us | Action type allowed for goal? Domain in scope? SSRF? Navigation limit? |
| **Regex fast-path** | ~5us | Is this a known-safe selector (navigation, menus, filters)? |
| **Action Validator (Haiku)** | ~500ms | Is this action aligned with the user's task? Should a potentially destructive click proceed? (LLM fallback path only) |

**4. Tool Schema Restriction** — The tool definition sent to Claude only includes actions allowed by the task scope. For a `read` task, `key_press` and `execute_sequence` are absent from the schema — the model cannot select them.

## Runtime Guardrails

Defense-in-depth checks that run alongside Cognitive Blinders. Configurable per-playbook via the `guardrails` section in YAML:

| Guard | Default | Configurable |
|---|---|---|
| Domain blocklist | Banking, government, email, payment, social media | `allowed_domains` / `blocked_domains` |
| Destructive action handling | Task-alignment and click safety are decided in the LLM validation path when enabled; deterministic scope/domain checks still apply regardless | `enable_llm_action_check` |
| SSRF protection | Private IPs blocked (override per-playbook) | `allow_private_networks` |
| URL visit limit | 50 unique URLs per run | `max_urls_visited` |
| Consecutive error limit | 5 errors | `max_consecutive_errors` |
| Stuck detection | Repetition + cycle analysis with 3-tier escalation | `stuck_repeat_hint/warn/stop`, `stuck_cycle_*` |
| CAPTCHA handling | Auto-detect + type-specific timeouts (Cloudflare 30s, reCAPTCHA 5s) | Skipped for dashboard goal type |

Notes:

- The default offline test suite does not make live LLM calls; it exercises degraded and deterministic paths only.
- In real agent runs, `enable_llm_action_check=true` lets the model decide whether an ambiguous click is aligned with the task.
- Playbook execution remains deterministic; the LLM safety path is relevant for ad hoc agent runs and LLM handoff flows.

## Stuck Detection

Detects when the agent repeats the same action or cycles between a small set of actions. Runs after every tool execution via `GuardrailEngine.record_action()` in the `ActionRouter`.

Two detection strategies on a sliding window of recent action signatures:

| Strategy | What it catches | Example |
|---|---|---|
| **Repetition** | Same action+target repeated consecutively | `click '#submit'` 5 times in a row |
| **Cycle** | Short pattern repeated N times | `click '#next'` → `click '#prev'` → `click '#next'` → ... |

Escalation is three-tiered:

| Severity | Repetition trigger | Cycle trigger | Effect |
|---|---|---|---|
| `HINT` | 3 same in window | 1st detection | Gentle hint prepended to tool result |
| `WARNING` | 5 same in window | 2nd detection | Strong warning prepended |
| `STOP` | 7 same in window | 3rd detection | Agent stopped with error |

Action signatures are normalized from the browser action plus its target, for example selector, URL, or execute-sequence contents. This reduces false positives from broad action-type matching alone. Repetition escalation only considers the consecutive tail of identical signatures, which is more tolerant of legitimate retries separated by other actions.

Hints are prepended to the tool result text (the only way to communicate with the agent mid-loop in Pydantic AI). Each detection emits a `stuck.detected` telemetry event with severity and action summary.

## Configuration

Set per-playbook or per-profile in the YAML `guardrails` section:

```yaml
guardrails:
  allow_private_networks: true          # Allow localhost/internal IPs
  enable_llm_action_check: false        # Skip Haiku safety check for pre-approved flows
  max_urls_visited: 200                 # URL navigation limit
  max_consecutive_errors: 10            # Error limit before aborting
  allowed_domains: ["*.internal.com"]   # Domain allowlist (optional)

  # Stuck detection thresholds
  stuck_window_size: 8                  # Sliding window of recent actions
  stuck_repeat_hint: 3                  # Same action N times → hint
  stuck_repeat_warn: 5                  # Same action N times → warning
  stuck_repeat_stop: 7                  # Same action N times → hard stop
  stuck_cycle_max_length: 3             # Max cycle pattern length (e.g. A-B-C)
  stuck_cycle_repeats: 3               # Cycle must repeat N times to trigger
```

When omitted, safe defaults apply (private networks blocked, LLM checks enabled, standard limits).
