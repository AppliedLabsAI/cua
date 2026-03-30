# CUA — Computer Use Agent

LLM-powered browser automation for **internal dashboard operations** — tasks behind a login that lack API coverage and require clicking through UI flows manually.

**Playbook-first architecture**: known workflows run deterministically via Playwright with zero LLM calls. If a step breaks (UI changed, selector stale), CUA hands off to the full LLM agent to finish the job.

```text
Directive → Playbook Lookup → PlaybookRunner (deterministic) → Result
                 ↓ (miss)              ↓ (step fails 2x)
            Full LLM Agent    LLM Agent completes remaining steps
```

## Why CUA

- **Playbook + LLM hybrid** — deterministic YAML playbooks for known flows (0 LLM calls, 1-5s), automatic LLM fallback for unknown flows or broken selectors
- **DOM-first agent** — compact DOM snapshots instead of screenshots, keeping token usage flat regardless of run length
- **Multi-provider** — works with Anthropic, OpenAI, Google Gemini, and any [PydanticAI-supported model](https://ai.pydantic.dev/models/)
- **Safety by default** — Cognitive Blinders filter what the agent can see based on task type, preventing prompt injection and accidental destructive actions
- **Real-time streaming** — SSE event stream with full replay, `Last-Event-ID` reconnection, and post-completion persistence
- **Production-ready** — deploys to Modal with isolated sandboxes, multi-container support, session recording, and OpenTelemetry observability

## Quick Start

### Install

```bash
uv sync --dev
patchright install chromium
```

Requires Python `3.13+`.

### Run Locally

```bash
# Deterministic playbook (no LLM)
python scripts/run_local.py \
  --directive "Cancel order #12345" \
  --playbook cancel_order \
  --playbook-params '{"order_id": "12345"}' \
  --credentials '{"username": "admin", "password": "secret"}'

# LLM agent (for unknown flows)
python scripts/run_local.py \
  --directive "Go to the dashboard and find the latest order" \
  --credentials '{"username": "admin", "password": "secret"}'
```

### Deploy to Modal

```bash
pip install modal && modal setup

# Create secrets
modal secret create llm-secret \
  GOOGLE_API_KEY=... \
  CUA_API_KEY=your-secret-api-key \
  ENVIRONMENT=production

# Deploy
modal deploy api/server.py::modal_app
```

### Use the API

```bash
# Create a run
curl -X POST https://<workspace>--cua-serve.modal.run/runs \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret-api-key" \
  -d '{"directive": "Go to example.com and tell me the page title"}'

# Check status (works during and after the run)
curl https://<workspace>--cua-serve.modal.run/runs/{run_id} \
  -H "Authorization: Bearer your-secret-api-key"

# Stream events (SSE) — replays past events, then streams live
curl -N https://<workspace>--cua-serve.modal.run/runs/{run_id}/stream \
  -H "Authorization: Bearer your-secret-api-key"

# Stop a run
curl -X POST https://<workspace>--cua-serve.modal.run/runs/{run_id}/stop \
  -H "Authorization: Bearer your-secret-api-key"
```

## Tests

```bash
pytest -q                    # offline unit tests (no API keys needed)
pytest -q -m integration     # browser integration tests
```

## Project Structure

```text
cua/
├── playbooks/       Playbook system (schema, store, runner, parser, auth)
│   └── definitions/ YAML playbook files
├── agent/           LLM agent loop (fallback path)
├── blinders/        Cognitive Blinders (scope, DOM filters, verifier)
├── bridge/          Browser lifecycle, DOM execution, CAPTCHA handling, router
├── api/             FastAPI server, API models, SSE streaming, run registry
├── guardrails/      Domain/action/SSRF safety engine
├── recording/       Session recording (Playwright tracing + screenshots)
├── profiles/        Agent profile configuration
├── telemetry/       OpenTelemetry instrumentation
├── scripts/         Local dev runner
├── tests/           Unit + integration tests
├── docs/            Detailed documentation
└── config.py        Centralized configuration
```

## Documentation

| Topic | Description |
|---|---|
| [API Reference](docs/api.md) | Endpoints, SSE streaming, replay, multi-container support |
| [Browser Tools](docs/tools.md) | 9 browser actions, `execute_sequence` batching, design choices |
| [Playbooks](docs/playbooks.md) | Deterministic workflows, selector fallbacks, LLM handoff |
| [Authentication](docs/authentication.md) | Session persistence, credential security, `SecretValue` |
| [Guardrails](docs/guardrails.md) | Cognitive Blinders, runtime safety, domain/action controls |
| [Recording](docs/recording.md) | Playwright tracing, screenshots, session replay |
| [Observability](docs/observability.md) | OpenTelemetry traces, metrics, Jaeger setup |
| [Configuration](docs/configuration.md) | CLI parameters, model selection, provider setup |

## License

MIT — see [LICENSE](LICENSE).
