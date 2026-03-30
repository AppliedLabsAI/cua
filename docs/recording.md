# Session Recording & Replay

Every CUA session is recorded by default using Playwright's built-in tracing. After a run completes, you get a `trace.zip` that you can open in [Playwright Trace Viewer](https://trace.playwright.dev) for frame-by-frame session replay with DOM snapshots, screenshots, network requests, and console logs at each action.

## Local Runs

Recordings are saved to the `output/` directory:

```bash
python scripts/run_local.py --directive "Cancel order #123" --playbook cancel_order
# After completion:
# output/trace.zip         — Playwright trace (open in trace viewer)
# output/screenshots/      — per-action JPEGs
# output/action_log.json   — structured action log (LLM path only)
```

Replay the session:

```bash
npx playwright show-trace output/trace.zip
```

Or drag `trace.zip` into [trace.playwright.dev](https://trace.playwright.dev) in your browser.

## Modal Runs

Recordings are persisted to a Modal Volume (`cua-recordings`) and accessible via the API:

```bash
# List recording artifacts
curl https://<workspace>--cua-serve.modal.run/runs/{run_id}/recording/manifest \
  -H "Authorization: Bearer your-secret-api-key"

# Download the trace
curl -o trace.zip https://<workspace>--cua-serve.modal.run/runs/{run_id}/recording/trace \
  -H "Authorization: Bearer your-secret-api-key"

# Download a specific screenshot
curl -o shot.jpg https://<workspace>--cua-serve.modal.run/runs/{run_id}/recording/screenshots/0003_click.jpg \
  -H "Authorization: Bearer your-secret-api-key"
```
