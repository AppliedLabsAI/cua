# CUA — Computer Use Agent

Autonomous browser automation agent deployed on Modal. Accepts natural-language directives, executes them via a headless Chromium browser in a sandboxed VM, and returns structured results.

## Quick reference

```bash
# Run tests
.venv/bin/python -m pytest tests/ -x -q

# Lint
.venv/bin/ruff check .

# Type check
.venv/bin/ty check

# Deploy to Modal
.venv/bin/modal deploy api/server.py::modal_app

# Run agent locally (requires DISPLAY / Xvfb)
.venv/bin/python scripts/run_local.py --directive "..." --start-url "https://..."
```

## Architecture

```
api/             → Outer FastAPI service (Modal Function), handles /runs CRUD
  server.py      → FastAPI app + Modal ASGI entrypoint (modal_app lives in modal_app.py)
  modal_app.py   → Modal App definition, image builds, volume/dict setup
  runs/          → RunService, RunRegistry (in-memory + Modal Dict), RunHandle
  streaming.py   → In-sandbox status API (port 8090), SSE events, status persistence
  models.py      → RunConfig, RunStatus, GuardrailSettings, ActionEvent

agent/           → Agent loop (runs inside Modal Sandbox)
  main.py        → Sandbox entrypoint — starts status API, runs session, handles shutdown
  loop.py        → PydanticAI agent loop with tool definitions
  session/       → SessionRunner (browser + agent lifecycle), RunFinalizer
  tools.py       → browser_dom tool implementation
  hooks.py       → PydanticAI hooks (preflight guardrails, thinking capture, error recovery)

bridge/          → Browser abstraction layer
  browser.py     → BrowserManager (Patchright wrapper, page lifecycle)
  execution.py   → Action handlers (click, extract, goto, etc.), SequenceExecutor
  page_actions.py → Primitive page actions with shared semantics
  observation.py → DOM snapshots, mutations, screenshots
  scripts/       → JS injected into pages (page_context.js, recorder.js)

sandbox/         → Modal Sandbox definition
  image.py       → Ubuntu 24.04 image with desktop env, Patchright, agent runtime
  entrypoint.sh  → Starts Xvfb + openbox, runs agent/main.py

guardrails/      → Runtime safety checks
  stuck.py       → Stuck detection (repetition, cycles, failure clusters, URL revisits)
  scope.py       → Domain allowlist/blocklist, action permissions

blinders/        → Directive classification (goal type, login detection, action filtering)
playbooks/       → YAML-defined deterministic workflows with LLM fallback
evaluation/      → Benchmark suite runner and scoring engine
recording/       → Playwright trace capture and artifact management
telemetry/       → OpenTelemetry tracing, structured logging, metrics
```

## Key conventions

- **Python 3.13+**, managed with `uv`. Virtual env at `.venv/`.
- **Environment**: uses `direnv` — secrets loaded from `.envrc` (not committed).
- **Settings**: all env vars centralized in `settings.py` via Pydantic Settings. Never scatter `os.environ.get()`.
- **Models**: `PRIMARY_MODEL` and `UTILITY_MODEL` constants in `settings.py`. Change there to switch everywhere.
- **Modal deploy target**: `api/server.py::modal_app` — the app variable is named `modal_app`, not `app`.
- **Sandbox vs Function**: Code in `agent/` runs inside Modal Sandboxes (no Modal API token). Code in `api/` runs in Modal Functions (has Modal auth). Don't call `modal.Volume.commit()` from sandbox code.
- **Tests**: `pytest` with `asyncio_mode = "auto"`. Integration tests marked `@pytest.mark.integration`. Run `pytest tests/ -x -q` for the full suite.
- **Lint**: `ruff` with bugbear, isort, pyupgrade, and pep8-naming rules. Line length 88.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | /runs/dry-run | Validate config without executing |
| POST | /runs | Create and start a new run |
| GET | /runs/{run_id} | Poll run status |
| POST | /runs/{run_id}/stop | Terminate a run |
| GET | /runs/{run_id}/stream | SSE event stream |
| GET | /runs/{run_id}/recording/manifest | List recording artifacts |
| GET | /runs/{run_id}/recording/trace | Download Playwright trace ZIP |

Auth: `Authorization: Bearer $CUA_API_KEY` (set in Modal secret `cua-secret`).

## Common patterns

- **DOM snapshot truncation**: `page_context.js` truncates hrefs to 60 chars. The extract action has a fallback that retries with `href^=` (starts-with) when exact match fails.
- **Stuck detection**: sliding window over recent actions, checks for repetition, cycles, failure clusters, and URL revisits. Configurable via `GuardrailSettings`.
- **Session memory**: injected into the system prompt before each LLM request so the agent retains awareness of prior work even after context pruning.
- **Playbook execution**: YAML-defined step sequences with selector fallbacks, verification checks, and LLM handoff on failure.
