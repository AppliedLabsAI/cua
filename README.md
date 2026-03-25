# CUA — Computer Use Agent

Autonomous browser automation powered by Claude. POST a natural-language directive, get back a run ID, a noVNC URL for real-time observation, and structured results when done.

CUA uses a DOM-first approach — Patchright for fast, precise browser interactions via CSS/text/role selectors. No pixel-hunting or screenshot-heavy loops. Each action completes in ~1-2s.

## Architecture

```text
POST /runs { directive: "...", profile: "research" }
     |
     v
+-- API Server (FastAPI) -----------------------+
|  Bearer token auth (CUA_API_KEY)              |
|  Creates Modal Sandbox (or Docker container)  |
|  Proxies status + SSE events                  |
+-------------------+---------------------------+
                    |
                    v
+-- Sandbox -----------------------------------------+
|  +-- Desktop ------------------------------------+ |
|  |  Xvfb + openbox + noVNC (:6080)              | |
|  |  Chromium via Patchright (stealth)            | |
|  +-----------------------------------------------+ |
|  +-- Agent Loop ---------------------------------+ |
|  |  Claude API (streaming, interleaved thinking) | |
|  |       |                                       | |
|  |  browser_dom tool (Patchright)                | |
|  |  - goto, click, screenshot, key_press         | |
|  |  - scroll, extract, get_dom, wait_for         | |
|  |  - execute_sequence (batched actions)          | |
|  +-----------------------------------------------+ |
|  Status API (:8090) -- SSE action stream           |
+----------------------------------------------------+
```

## Profiles

Profiles specialize the agent for different use cases by bundling a prompt extension and guardrail overrides. The same tools and agent loop are used for all profiles.

| Profile | Description |
|---|---|
| `default` | General-purpose browser automation |
| `research` | Broader navigation (100 URLs), no destructive action blocks, citation-focused |
| `form_filling` | Unblocks purchase/submit actions, aggressive batching for form workflows |

Create custom profiles by adding a YAML file to `profiles/`. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Quick Start — Modal

```bash
pip install -e .

# Store your Anthropic API key as a Modal secret
modal secret create anthropic-secret ANTHROPIC_API_KEY=sk-ant-...

# Deploy the API server
modal deploy api/server.py

# Create a run
curl -X POST https://your-app--cua.modal.run/runs \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CUA_API_KEY" \
  -d '{"directive": "Go to example.com and find the contact page"}'
```

## Quick Start — Docker

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export DIRECTIVE="Go to example.com and find the contact page"

docker compose up

# Open noVNC to watch: http://localhost:6080
# Status API: http://localhost:8090/status
```

## Quick Start — Local Dev

```bash
pip install -e ".[dev]"
patchright install chromium

# Start Xvfb (Linux only)
Xvfb :99 -screen 0 1280x720x24 &

python scripts/run_local.py \
  --directive "Go to example.com and find the contact page" \
  --profile research \
  --allow-private-networks  # disable SSRF protection for local testing
```

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

## Guardrails

CUA includes a safety guardrail system that runs before every action:

- **Domain blocklist**: Blocks navigation to banking, government, email, payment, and social media sites by default. Configurable via `allowed_domains` (allowlist) or `blocked_domains` (blocklist).
- **Destructive action detection**: Blocks clicks on selectors matching purchase, account deletion, or message sending keywords.
- **SSRF protection**: Blocks navigation to private IP ranges, localhost, and cloud metadata endpoints (169.254.169.254). Disable with `--allow-private-networks` (local runner) or `allow_private_networks: true` in guardrails config.
- **URL visit limit**: Caps the number of unique URLs visited per run (default: 50).
- **Consecutive error limit**: Stops the agent after 5 consecutive errors.

## Security Considerations

- **Credentials**: When provided, credentials are injected as plaintext into the system prompt sent to the Anthropic API. They will appear in Anthropic's request logs. Use service-specific tokens with minimal scope.
- **API authentication**: Set `CUA_API_KEY` environment variable to require Bearer token auth on all endpoints. Without it, the API is unauthenticated.
- **Action logs**: The `text` field in `key_press` actions is truncated in logs but not fully redacted. Avoid typing sensitive data that shouldn't appear in logs.

## Cost Estimation

Per run (typical 10-20 step task with DOM-first actions):
- **Claude API**: ~$0.02-0.15 (Sonnet 4.6, minimal screenshots)
- **Modal compute**: ~$0.02-0.10 (1-5 min sandbox runtime)
- **Total**: ~$0.05-0.25 per run

DOM-first actions drastically reduce cost vs screenshot-heavy approaches — each screenshot is ~1-2K image tokens, while DOM snapshots are ~200-500 text tokens.

## Project Structure

```text
cua/
├── agent/           Agent loop, tool definitions, system prompt
├── bridge/          Patchright browser executor, CAPTCHA handling, action router
├── api/             FastAPI server (outer) + streaming server (inner sandbox)
├── actionlog/       Action log dataclass, persistence, SSE formatting
├── profiles/        YAML profile definitions (prompt + guardrails)
├── sandbox/         Modal image definition + entrypoint script
├── scripts/         Local dev runner
├── tests/           Unit tests
├── guardrails.py    Domain/action/SSRF safety engine
├── Dockerfile       Docker-based sandbox (alternative to Modal)
└── docs/            Archived design documents
```

## License

MIT — see [LICENSE](LICENSE).
