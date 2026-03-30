# Browser Tools

CUA exposes a single `browser_dom` tool with 9 actions. The agent chooses which action to call based on the task and page state.

## Actions

| Action | Description | Returns |
|---|---|---|
| `goto(url)` | Navigate to a URL | DOM snapshot |
| `click(selector)` | Click an element (CSS, `text=`, `role=` selectors) | DOM snapshot |
| `screenshot` | Capture the viewport | Screenshot + DOM |
| `key_press(text, key)` | Type text and/or press a key (Enter, Tab, etc.) | Confirmation |
| `scroll(direction, amount)` | Scroll the page | Screenshot |
| `extract(selector, mode)` | Extract content as markdown (default), text, HTML, or form values | Content string + DOM |
| `get_dom(selector?)` | Get a compact DOM snapshot (optionally scoped) | DOM string |
| `wait_for(selector, state)` | Wait for an element to be visible, hidden, etc. | Confirmation |
| `execute_sequence(steps)` | **Batch multiple actions in a single tool call** | Combined results + DOM |

## Why `execute_sequence` matters

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

## Design Choices

- **Semantic page understanding.** `goto` and `click` return a structured page map with three layers: (1) page metadata from Schema.org JSON-LD and Open Graph tags for instant page type classification, (2) semantic landmarks summarizing regions (`form#login: 3 inputs, 1 button`, `table#results: 5 cols, 47 rows`), and (3) all interactive elements with parent-context disambiguation (`Edit [row: "john@example.com"]`). Fallback to Playwright's accessibility tree (ARIA roles/states) when JS-based extraction fails.
- **Action-outcome verification.** Click actions use a DOM Mutation Observer to report exactly what changed: `[URL → /dashboard; +modal.dialog; 3 attr changes]`. The agent knows immediately whether its action worked without needing a screenshot.
- **Readability-based extraction.** `extract` defaults to `markdown` mode, using a Readability-style content extractor + markdown conversion to produce clean, structured output with headings, links, and tables preserved.
- **Streaming execution.** Tool calls execute as they arrive from the Claude API stream, not after the full response.
- **Adaptive thinking budget.** Full budget for planning (first 2 steps), reduced after 3+ consecutive successes, reset on errors.
- **Context pruning via HistoryProcessor.** Old screenshots, DOM snapshots, and thinking blocks are automatically pruned before each model request. Input tokens stay flat regardless of run length.
- **Page-change detection.** After `goto`/`click`/`execute_sequence`, remaining tool calls in the same response are skipped — they were planned on stale state.
- **CAPTCHA auto-resolution.** Patchright stealth patches + auto-wait up to 30s for Cloudflare/reCAPTCHA/hCaptcha.
- **Stuck detection.** Repetition and cycle analysis with 3-tier escalation (hint → warning → stop). See [Guardrails](guardrails.md#stuck-detection).
