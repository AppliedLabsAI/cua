# Configuration

## CLI Parameters

| Parameter | Default | Description |
|---|---|---|
| `--directive` | (required) | Natural language task description |
| `--playbook` | None | Playbook ID to execute deterministically |
| `--playbook-params` | None | JSON dict of playbook parameters |
| `--credentials` | None | JSON credentials: `'{"username": "...", "password": "..."}'` |
| `--model` | `openai-responses:gpt-5.4` | Model for LLM agent (any PydanticAI-supported model) |
| `--max-steps` | 50 | Max tool-call iterations (LLM path only) |
| `--thinking` | `high` | Thinking effort level (`minimal`, `low`, `medium`, `high`, `xhigh`) |
| `--allow-private-networks` | false | Allow localhost and private IPs |
| `--start-url` | None | URL to open on browser launch |
| `--display` | `:99` | X display (Linux) |

## Model Configuration

CUA uses [PydanticAI](https://ai.pydantic.dev/) and works with any model it supports — Anthropic, OpenAI, Google Gemini, Groq, and more. Set the model in `settings.py`:

```python
PRIMARY_MODEL = "openai-responses:gpt-5.4"      # main agent
UTILITY_MODEL = "openai-responses:gpt-5.4-mini"  # classification, guardrails, extraction
```

To switch providers, change the model string and set the corresponding API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # Anthropic
export OPENAI_API_KEY=sk-...          # OpenAI
export GOOGLE_API_KEY=...             # Google Gemini
```

PydanticAI reads these automatically — no code changes needed. See [PydanticAI models documentation](https://ai.pydantic.dev/models/) for supported providers and model string formats.

### Recommended Models

| Model | Thinking | Notes |
|---|---|---|
| `google-gla:gemini-3-flash-preview` | >= `high` | Fastest response times. Sonnet or GPT-5.4 have better precision for complex selectors. |
| `anthropic:claude-sonnet-4-6` | >= `medium` | Strong selector accuracy and DOM reasoning. |
| `openai-responses:gpt-5.4` | >= `medium` | Strong quality. Must use `openai-responses:` prefix (not `openai:`) for thinking + tools support. |

The agent relies on accurate CSS selector generation from DOM snapshots — weaker models produce selectors that timeout or miss elements.

For `UTILITY_MODEL` (classification, guardrails), a fast/cheap model like `anthropic:claude-haiku-4-5` or `openai:gpt-5.4-mini` works well.
