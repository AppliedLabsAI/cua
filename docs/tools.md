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
| `extract(selector, mode)` | Extract text, HTML, or form values from elements | Content string |
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

- **DOM-first, not screenshot-first.** `goto` and `click` return a compact DOM snapshot (~200-500 tokens) instead of a screenshot (~1-2K image tokens). The agent only takes screenshots when it needs to *see* the page visually.
- **Streaming execution.** Tool calls execute as they arrive from the Claude API stream, not after the full response.
- **Adaptive thinking budget.** Full budget for planning (first 2 steps), reduced after 3+ consecutive successes, reset on errors.
- **Aggressive context pruning.** Old screenshots, DOM snapshots, and thinking blocks are stripped every iteration. Input tokens stay flat regardless of run length.
- **Page-change detection.** After `goto`/`click`/`execute_sequence`, remaining tool calls in the same response are skipped — they were planned on stale state.
- **CAPTCHA auto-resolution.** Patchright stealth patches + auto-wait up to 30s for Cloudflare/reCAPTCHA/hCaptcha.
- **Stuck detection.** System hint after 4+ of the last 6 actions produce identical results.
