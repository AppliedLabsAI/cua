Test the deployed CUA API on Modal to verify it's working correctly.

## Prerequisites

Before running tests, determine the API base URL and ensure `$CUA_API_KEY` is set in the environment (loaded via direnv). The base URL depends on the Modal workspace — check the deploy output or run:

```bash
.venv/bin/modal app list | grep cua
```

Set the base URL for the session:
```bash
BASE_URL="https://<workspace>--cua-serve.modal.run"
```

## Test cases

Run all 4 test cases. For Tests 3 and 4, save the `run_id` from the create response and poll until `status` is `completed` (or a terminal state).

### Test 1: Dry-run validation (config check, no sandbox)

```bash
curl -s -X POST "$BASE_URL/runs/dry-run" \
  -H "Authorization: Bearer $CUA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"directive": "Test directive", "max_steps": 10}' | python3 -m json.tool
```

**Expected**: `"valid": true`, all checks passed.

### Test 2: Input validation (reject invalid config)

```bash
curl -s -X POST "$BASE_URL/runs/dry-run" \
  -H "Authorization: Bearer $CUA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"directive": "Test", "max_steps": 0}' | python3 -m json.tool
```

**Expected**: HTTP 422 with `"code": "INVALID_REQUEST"` and error about `max_steps`.

### Test 3: Simple directive (example.com heading)

Create a run:
```bash
curl -s -X POST "$BASE_URL/runs" \
  -H "Authorization: Bearer $CUA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "directive": "Go to https://example.com and tell me the heading text on the page",
    "max_steps": 10,
    "timeout_seconds": 120,
    "start_url": "https://example.com"
  }' | python3 -m json.tool
```

Poll until completed:
```bash
curl -s "$BASE_URL/runs/<run_id>" \
  -H "Authorization: Bearer $CUA_API_KEY" | python3 -m json.tool
```

**Expected**: `"status": "completed"`, result mentions "Example Domain".

### Test 4: Structured output extraction (HN top 3)

Create a run with `output_schema`:
```bash
curl -s -X POST "$BASE_URL/runs" \
  -H "Authorization: Bearer $CUA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "directive": "Go to https://news.ycombinator.com and extract the titles of the top 3 stories",
    "max_steps": 15,
    "timeout_seconds": 180,
    "start_url": "https://news.ycombinator.com",
    "output_schema": {
      "type": "object",
      "properties": {
        "stories": {
          "type": "array",
          "items": {"type": "object", "properties": {"rank": {"type": "integer"}, "title": {"type": "string"}}},
          "maxItems": 3
        }
      },
      "required": ["stories"]
    }
  }' | python3 -m json.tool
```

Poll until completed (may take 20-30s).

**Expected**: `"status": "completed"`, `data.stories` array with 3 items each having `rank` and `title`, no extract timeout errors in actions.

## Evaluating results

For each test, check:
1. **Status**: should be `completed` (not `failed` or `timeout`)
2. **Errors**: `error` field should be `null`
3. **Actions**: verify no repeated timeout errors (stuck detection should catch these)
4. **Duration**: simple directives should complete in under 30s, structured output under 60s

## Troubleshooting sandbox logs

If a run fails or behaves unexpectedly, check the sandbox logs in the Modal dashboard.

Look for:
- `CancelledError` in Starlette lifespan — graceful shutdown issue
- `AuthError: Token missing` — volume commit called from sandbox context
- `AsyncUsageWarning` — sync Modal API call in async context
- `browser_dom.extract failed: Timeout` — selector mismatch (check if href was truncated)

Report a summary table of all test results with status, duration, and any errors found.
