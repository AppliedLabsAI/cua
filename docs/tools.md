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

| Design | How it works |
|---|---|
| **Semantic page understanding** | `goto`/`click` return a structured page map with three layers: (1) page metadata from Schema.org JSON-LD and Open Graph for page type classification, (2) semantic landmarks summarizing regions (`form#login: 3 inputs`, `table#results: 5 cols, 47 rows`), and (3) all interactive elements with parent-context disambiguation (`Edit [row: "john@example.com"]`). Falls back to Playwright's accessibility tree (ARIA roles/states) when JS extraction fails. |
| **Action-outcome verification** | Click actions use a DOM Mutation Observer to report exactly what changed: `[URL → /dashboard; +modal.dialog; 3 attr changes]`. The agent knows immediately whether its action worked. |
| **Readability-based extraction** | `extract` defaults to `markdown` mode — Readability-style content extractor + markdown conversion preserving headings, links, lists, tables, and code blocks. |
| **Streaming execution** | Tool calls execute as they arrive from the Claude API stream, not after the full response. |
| **Context pruning** | Old screenshots, DOM snapshots, and thinking blocks are pruned via Pydantic AI `HistoryProcessor` before each model request. Input tokens stay flat regardless of run length. |
| **Speculative prefetch** | Page map fetch starts as a background task during click actions, overlapping with mutation observer collection. Result is consumed by the next page context request. |
| **Page-change detection** | After `goto`/`click`/`execute_sequence`, remaining tool calls in the same response are skipped — they were planned on stale state. |
| **CAPTCHA auto-resolution** | Patchright stealth patches + auto-wait up to 30s for Cloudflare, 5s for reCAPTCHA/hCaptcha. |
| **Stuck detection** | Repetition and cycle analysis with 3-tier escalation (hint → warning → stop). Hard stops block all subsequent actions. See [Guardrails](guardrails.md#stuck-detection). |
