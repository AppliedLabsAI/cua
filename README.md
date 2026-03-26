# CUA — Computer Use Agent

Autonomous browser automation powered by Claude. POST a natural-language directive, get back a run ID, a noVNC URL for real-time observation, and structured results when done.

CUA uses a DOM-first approach — Patchright for fast, precise browser interactions via CSS/text/role selectors. No pixel-hunting or screenshot-heavy loops. Each action completes in ~1-2s.

```mermaid
graph LR
    A["POST /runs { directive }"] --> B[API Server<br/>FastAPI + Auth]
    B --> C[Modal Sandbox<br/>or Docker]
    C --> D[Xvfb + Chromium<br/>noVNC :6080]
    C --> E[Agent Loop<br/>Claude + Patchright]
    E -->|browser_dom| D
    C --> F[Status API :8090<br/>SSE stream]
```

## Quick Start

**Modal** (recommended):
```bash
pip install -e .
modal secret create anthropic-secret ANTHROPIC_API_KEY=sk-ant-...
modal deploy api/server.py

curl -X POST https://your-app--cua.modal.run/runs \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUA_API_KEY" \
  -d '{"directive": "Go to example.com and find the contact page"}'
```

**Docker:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up
# noVNC: http://localhost:6080 | Status: http://localhost:8090/status
```

**Local dev:**
```bash
pip install -e ".[dev]" && patchright install chromium
git config core.hooksPath .githooks  # enable pre-commit checks (ruff + ty)
Xvfb :99 -screen 0 1280x720x24 &  # Linux only

python scripts/run_local.py \
  --directive "Go to example.com and find the contact page" \
  --profile research \
  --allow-private-networks  # disable SSRF protection for local testing
```

## Tools

CUA exposes a single `browser_dom` tool with 9 actions. The agent chooses which action to call based on the task and page state.

| Action | Description | Returns |
|---|---|---|
| `goto(url)` | Navigate to a URL | DOM snapshot |
| `click(selector)` | Click an element (CSS, `text=`, `role=` selectors) | DOM snapshot |
| `screenshot` | Capture the viewport | Screenshot + DOM |
| `key_press(text, key)` | Type text and/or press a key (Enter, Tab, etc.) | Confirmation |
| `scroll(direction, amount)` | Scroll the page | Screenshot |
| `extract(selector, mode)` | Extract text, HTML, or form values from elements | Content string |
| `get_dom(selector?)` | Get a compact DOM snapshot (optionally scoped) | DOM string |
| `wait_for(selector, state)` | Wait for an element to be visible, hidden, etc. | Confirmation |
| `execute_sequence(steps)` | **Batch multiple actions in a single tool call** | Combined results + DOM |

### Why `execute_sequence` matters

Each tool call has ~3-5s of overhead (API round-trip + thinking). Without batching, filling a 5-field form takes 5 separate calls = ~20s of pure overhead. With `execute_sequence`, it's a single call:

```json
{
  "action": "execute_sequence",
  "steps": [
    {"action": "click", "selector": "#email"},
    {"action": "key_press", "text": "user@example.com"},
    {"action": "click", "selector": "#password"},
    {"action": "key_press", "text": "secretpass"},
    {"action": "click", "selector": "button[type=submit]"}
  ]
}
```

Intermediate steps skip screenshots for speed. Only the final step captures the DOM, so the agent sees the result of the entire sequence in one response.

### Design choices

- **DOM-first, not screenshot-first.** `goto` and `click` return a compact DOM snapshot (~200-500 tokens) instead of a screenshot (~1-2K image tokens). The agent only takes screenshots when it needs to *see* the page visually.
- **Streaming execution.** Tool calls execute as they arrive from the Claude API stream, not after the full response.
- **Adaptive thinking budget.** Full budget for planning (first 2 steps), reduced after 3+ consecutive successes, reset on errors.
- **Aggressive context pruning.** Old screenshots, DOM snapshots, and thinking blocks are stripped every iteration. Input tokens stay flat regardless of run length.
- **Page-change detection.** After `goto`/`click`/`execute_sequence`, remaining tool calls in the same response are skipped — they were planned on stale state.
- **CAPTCHA auto-resolution.** Patchright stealth patches + auto-wait up to 30s for Cloudflare/reCAPTCHA/hCaptcha.
- **Stuck detection.** System hint after 4+ of the last 6 actions produce identical results.

## Guardrails

CUA uses a layered safety architecture combining proactive observation control with traditional runtime checks.

### Cognitive Blinders

The primary safety mechanism is **Cognitive Blinders** — a proactive observation filtering system that controls what the agent can see, rather than reactively blocking what it tries to do.

**The core insight**: if the agent can't see a "delete account" button, it can't click it. If it can't see injected instructions in a sidebar ad, it can't follow them. Research shows that filtering observations drops prompt injection attack success from 80%+ to under 2% ([FocusAgent, 2025](https://arxiv.org/html/2510.03204)), while also improving performance by reducing noise.

Traditional guardrails are reactive — the agent sees everything, decides to act, then rules block bad actions. This is fragile because the agent has already been influenced by what it saw. Cognitive Blinders flips the model: control what enters the agent's observation space proactively.

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

**How it works:**

**1. Task Scope Extraction** — Before the agent sees any web content, the directive is classified into a goal type. This determines what the agent can see and do for the entire run.

```mermaid
graph LR
    A["'Find price on apple.com'"] -->|classify| B["read"]
    C["'Log in and find orders'"] -->|classify| D["fill_form"]
    E["'Click download button'"] -->|classify| F["interact"]
    G["'Go to example.com'"] -->|classify| H["navigate"]
```

- **Primary**: Fast keyword matching (~25μs) — regex-based classification that handles common directive patterns
- **LLM fallback**: Haiku LLM call (~200ms, one-time) — available for nuanced directives when `use_llm=True` is passed

Each goal type gets adaptive defaults:

| Goal Type | Forms | Dangerous Buttons | Account Controls | `key_press` | `execute_sequence` |
|---|---|---|---|---|---|
| `read` | Hidden | Hidden | Hidden | Blocked | Blocked |
| `navigate` | Hidden | Hidden | Hidden | Blocked | Blocked |
| `interact` | Visible | Visible | Hidden | Allowed | Allowed |
| `fill_form` | Visible | Visible | Visible | Allowed | Allowed |

**2. DOM Blinders** — The DOM snapshot sent to the agent is filtered at two levels:

| Level | Where | What it does |
|---|---|---|
| **JS-side** | `dom_snapshot.js` in browser | Filters elements by category (forms, action buttons, account controls) based on task scope. Elements are removed before they leave the browser. |
| **Python-side** | `blinders/filters.py` | Scans for prompt injection patterns (`"ignore previous instructions"`, `SYSTEM:`, `[INST]` tokens) and redacts them. Wraps content with provenance markers (`[web-content-start/end]`). |

**3. Scope Verifier + Action Validator** — Optimized multi-layer pre-execution check using DAG pruning to eliminate redundant LLM calls:

| Layer | Speed | What it checks |
|---|---|---|
| **Deterministic** | ~25μs | Action type allowed for goal? Domain in scope? SSRF? Navigation limit? |
| **Regex fast-path** | ~5μs | Is this a known-safe selector (navigation, menus, filters, CSS selectors)? |
| **Action Validator (Haiku)** | ~500ms | Is this action aligned with the user's task? Context-aware destructive detection. |

The pipeline is optimized to minimize LLM overhead while maximizing safety:
- **Regex fast-path** — safe selectors (navigation, menus, table rows, CSS selectors) skip Haiku entirely
- **Context-aware safety** — the Action Validator considers the user's directive, so "click refund" is allowed when the task is "issue a refund" but blocked when the task is "check order status"
- **DAG pruning** — redundant checks are eliminated: the Action Validator subsumes the destructive check (1 Haiku call per risky action, not 2-3)
- **Selector caching** — once a selector is approved, future clicks skip re-validation
- **Domain caching** — once a domain is approved for goto, future navigations skip re-validation
- **Safe action skipping** — `extract`, `screenshot`, `scroll`, `get_dom`, `wait_for` bypass validation entirely
- **Batched sequences** — `execute_sequence` sub-steps share a single validation pass

**4. Tool Schema Restriction** — The tool definition sent to Claude only includes actions allowed by the task scope. For a `read` task, `key_press` and `execute_sequence` are literally absent from the schema — the model cannot select them.

### Runtime Guardrails

Defense-in-depth checks that run alongside Cognitive Blinders:

| Guard | Default | Configurable |
|---|---|---|
| Domain blocklist | Banking, government, email, payment, social media | `allowed_domains` / `blocked_domains` |
| Destructive action detection | Context-aware Action Validator (Haiku) with regex fast-path for safe selectors; regex-only fallback via `enable_llm_action_check: false` | `enable_llm_action_check` |
| SSRF protection | Private IPs, localhost, cloud metadata (169.254.x.x) | `allow_private_networks` |
| URL visit limit | 50 unique URLs per run | `max_urls_visited` |
| Consecutive error limit | 5 errors | `max_consecutive_errors` |

## Profiles

Profiles specialize the agent by bundling a prompt extension and guardrail overrides. The same tools and agent loop are used for all profiles.

| Profile | Description |
|---|---|
| `default` | General-purpose browser automation |
| `research` | Broader navigation (100 URLs), no destructive action blocks, citation-focused |
| `form_filling` | Unblocks purchase/submit actions, aggressive batching for form workflows |

Create custom profiles by adding a YAML file to `profiles/`. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## API Reference

### POST /runs

Create a new CUA run. Requires `Authorization: Bearer <CUA_API_KEY>` header (if `CUA_API_KEY` env var is set).

```json
{
  "directive": "Go to example.com and click the signup button",
  "model": "claude-sonnet-4-6",
  "max_steps": 50,
  "timeout_seconds": 600,
  "thinking_budget": 4096,
  "display_width": 1280,
  "display_height": 720,
  "profile": "default",
  "start_url": "https://example.com",
  "credentials": {
    "github": { "username": "user", "password": "pass" }
  },
  "proxy": "http://user:pass@proxy:8080",
  "guardrails": {
    "allowed_domains": ["example.com", "*.example.org"],
    "max_urls_visited": 100
  }
}
```

### GET /runs/{run_id}
Get run status and action history.

### POST /runs/{run_id}/stop
Terminate a run early.

### GET /runs/{run_id}/stream
SSE stream of real-time action events.

```bash
curl -N https://your-app--cua.modal.run/runs/sb-abc123/stream
# data: {"step":1,"action":"goto","input_summary":"navigate to https://example.com",...}
# event: complete
# data: {"status":"completed"}
```

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `directive` | (required) | Natural language task for the agent |
| `model` | `claude-sonnet-4-6` | Claude model ID |
| `max_steps` | 50 | Maximum tool-call iterations (1-200) |
| `timeout_seconds` | 600 | Sandbox timeout (30-3600s) |
| `thinking_budget` | 4096 | Extended thinking token budget |
| `display_width/height` | 1280x720 | Virtual display resolution |
| `profile` | `default` | Agent profile name |
| `start_url` | None | URL to open on browser launch |
| `credentials` | None | Service credentials (injected into system prompt) |
| `proxy` | None | Proxy URL for bot avoidance |
| `guardrails` | None | GuardrailConfig overrides (domains, actions, limits) |

## Security

- **Credentials**: Injected as plaintext into the system prompt sent to the Anthropic API. Use service-specific tokens with minimal scope.
- **API authentication**: Set `CUA_API_KEY` environment variable to require Bearer token auth on all endpoints.
- **Action logs**: `key_press` text is truncated in logs but not fully redacted. Avoid typing sensitive data that shouldn't appear in logs.

## Cost Estimation

Per run (typical 10-20 step task with DOM-first actions):
- **Claude API**: ~$0.02-0.15 (Sonnet, minimal screenshots)
- **Modal Sandbox**: ~$0.01-0.02 (1-5 min, 1 core + 2GB RAM at $0.04/core/hr + $0.007/GiB/hr sandbox rates)
- **Total**: ~$0.03-0.17 per run

DOM-first actions reduce cost vs screenshot-heavy approaches — each screenshot is ~1-2K image tokens, while DOM snapshots are ~200-500 text tokens.

## Observability

CUA includes built-in OpenTelemetry instrumentation for distributed tracing, metrics, and structured logging.

### Traces

Every session produces a single trace linking the outer API request to every agent step inside the sandbox. The waterfall view shows:

```text
cua.session                          → API request lifecycle
  cua.sandbox.create                 → Modal sandbox creation
  cua.agent.run                      → Full agent run (linked via W3C Traceparent)
    cua.agent.setup                  → Browser launch + blinders init
    cua.agent.iteration [×N]         → One per loop iteration
      cua.llm.call                   → Claude API call (streaming or fallback)
      cua.tool.execute [×M]          → Each browser action
        cua.guardrail.check          → Safety verification
        cua.browser.action           → Patchright execution
        cua.sequence.step [×K]       → Sub-steps in execute_sequence
```

Spans include GenAI semantic convention attributes (`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`), action details (selector, URL, success/error), guardrail decisions, and timing.

### Configuration

| Env Var | Default | Description |
|---|---|---|
| `OTEL_SDK_DISABLED` | `true` | Set to `false` to enable tracing and metrics |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP gRPC collector endpoint |
| `OTEL_EXPORTER_OTLP_INSECURE` | `true` | Set to `false` for TLS in production |
| `OTEL_RESOURCE_ENV` | `local` | Deployment environment label |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Sampling rate (0.0–1.0) |
| `OTEL_PII_REDACTION` | `false` | Redact emails, phones, credit cards from logs and spans |

Quick start with Jaeger:

```bash
docker run -d --name jaeger -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one
OTEL_SDK_DISABLED=false python scripts/run_local.py --directive "..."
# Open http://localhost:16686 to see traces
```

## Project Structure

```text
cua/
├── agent/           Agent loop, tools, prompts, context management
├── blinders/        Cognitive Blinders (scope, DOM filters, verifier, action validator)
├── bridge/          Patchright executor, CAPTCHA handling, action router
├── api/             FastAPI servers (outer API + inner status API)
├── guardrails/      Domain/action/SSRF safety engine
├── profiles/        YAML profile definitions
├── sandbox/         Modal image definition + entrypoint script
├── telemetry/       OpenTelemetry instrumentation, metrics, span helpers, logging
├── scripts/         Local dev runner
├── tests/           Unit + browser integration tests
├── config.py        Centralized CUAConfig (env vars, RunConfig, profiles)
├── exceptions.py    Custom exception hierarchy
└── Dockerfile       Docker-based sandbox (alternative to Modal)
```

## License

MIT — see [LICENSE](LICENSE).
