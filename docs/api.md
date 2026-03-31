# API Reference

CUA deploys to [Modal](https://modal.com) as a managed API. Each run spawns an isolated sandbox with its own browser, desktop environment, and agent runtime.

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/runs` | POST | Create a run. Returns `{run_id, status, status_url, stream_url}` |
| `/runs/{run_id}` | GET | Poll run status. Works during execution and after completion |
| `/runs/{run_id}/stream` | GET | SSE event stream with replay and `Last-Event-ID` support |
| `/runs/{run_id}/stop` | POST | Terminate a run immediately |
| `/runs/{run_id}/recording/manifest` | GET | List recording artifacts |
| `/runs/{run_id}/recording/trace` | GET | Download Playwright trace ZIP |

## Request Body (`POST /runs`)

| Field | Type | Default | Description |
|---|---|---|---|
| `directive` | string | (required) | Natural language task |
| `model` | string | `google-gla:gemini-3-flash-preview` | LLM model |
| `max_steps` | int | 50 | Max agent iterations |
| `timeout_seconds` | int | 600 | Sandbox timeout (30-3600) |
| `thinking` | string | `high` | Thinking effort level |
| `start_url` | string | null | URL to open on launch |
| `credentials` | object | null | `{"username": "...", "password": "..."}` (see [Credential Security](authentication.md#credential-security)) |
| `profile` | string | `default` | Agent profile |
| `guardrails` | object | null | Domain/action safety config |
| `recording` | object | null | `{"enabled": true, "trace": true}` |
| `output_schema` | object | null | JSON schema for structured output extraction |

### Error Shape

All API error responses use a structured payload:

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Run abc123 not found",
    "details": {
      "run_id": "abc123"
    }
  }
}
```

Long-running run failures exposed by `GET /runs/{run_id}` use the same `error` object shape inside `RunStatus`.

Common runtime error codes include:

- `GUARDRAIL_BLOCKED`
- `TIMEOUT`
- `LLM_ERROR`
- `CAPTCHA_FAILED`
- `RUN_TERMINATED`

## SSE Event Streaming

The `/runs/{run_id}/stream` endpoint provides real-time Server-Sent Events (SSE) for monitoring run progress.

### Event Format

Each action produces an event with an `id` field (the step number) and a JSON data payload:

```
id: 3
data: {"step": 3, "timestamp": "2026-03-30T...", "tool": "browser_dom", "action": "click", "input_summary": "click '#submit'", "duration_ms": 150, "success": true}
```

On completion:

```
event: complete
data: {"status": "completed"}
```

### Replay

Connecting to the stream at any point replays **all past events** first, then continues with live events. This means a client connecting mid-run won't miss any actions.

### Reconnection with `Last-Event-ID`

If the connection drops, reconnect with the `Last-Event-ID` header set to the last received event ID. Only events after that ID will be sent:

```bash
curl -N https://<workspace>--cua-serve.modal.run/runs/{run_id}/stream \
  -H "Authorization: Bearer your-api-key" \
  -H "Last-Event-ID: 5"
```

Browsers handle this automatically via the `EventSource` API.

### Post-Completion Replay

After a run completes and the sandbox terminates, events are still available. The run status and action log are persisted to a Modal Volume, so both `/runs/{run_id}` and `/runs/{run_id}/stream` continue to work.

If a sandbox receives a shutdown signal mid-run, CUA writes the last known structured status before browser/recording cleanup so clients can still retrieve a terminal state after the sandbox exits.

## Multi-Container Support

The API server can run across multiple Modal containers. If a run isn't in the local in-memory registry, the endpoint reconstructs the handle from Modal's API via `Sandbox.from_id()`. After sandbox termination, status and events are served from the persisted volume. No sticky sessions required.

## Run Lifecycle

```
POST /runs
    |
    v
[Sandbox Created] --> status: "running"
    |
    |--- GET /runs/{id}        --> proxied from sandbox
    |--- GET /runs/{id}/stream --> SSE from sandbox (replay + live)
    |--- POST /runs/{id}/stop  --> terminate immediately
    |
    v
[Sandbox Terminated] --> status: "completed" / "failed" / "terminated"
    |
    |--- GET /runs/{id}        --> read from persisted volume
    |--- GET /runs/{id}/stream --> replay from persisted volume
```

## Authentication

All endpoints require a Bearer token (`CUA_API_KEY`). Set `ENVIRONMENT=local` to disable auth for local development.

```bash
curl -H "Authorization: Bearer your-secret-api-key" \
  https://<workspace>--cua-serve.modal.run/runs/{run_id}
```
