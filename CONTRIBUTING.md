# Contributing to CUA

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
# Clone and install in dev mode
git clone https://github.com/AppliedLabsAI/cua.git
cd cua
pip install -e ".[dev]"

# Install Patchright's Chromium
patchright install chromium
```

### Running locally (Linux with Xvfb)

```bash
# Start Xvfb
Xvfb :99 -screen 0 1280x720x24 &
export DISPLAY=:99

# Run the agent
python scripts/run_local.py --directive "Go to example.com" --profile default
```

### Running via Docker

```bash
ANTHROPIC_API_KEY=sk-... DIRECTIVE="Go to example.com" docker compose up
```

## Code Style

- Python 3.13+
- Formatted and linted with [ruff](https://docs.astral.sh/ruff/): `ruff check --fix . && ruff format .`
- Type hints everywhere, `X | None` instead of `Optional[X]`

## Adding a Profile

1. Create `profiles/your_profile.yaml`:
   ```yaml
   name: your_profile
   description: What this profile optimizes for.
   prompt_extension: |
     ## Your Profile Mode
     Additional instructions for the agent.
   guardrail_overrides:
     max_urls_visited: 100
   ```
2. Test it: `python scripts/run_local.py --directive "..." --profile your_profile`

## Running Tests

```bash
pytest tests/
```

Tests are unit tests that don't require a browser, Xvfb, or Modal.

## Pull Requests

- One concern per PR
- Include tests for bug fixes
- Run `ruff check --fix . && ruff format .` before submitting
- Keep PRs focused — avoid unrelated refactors

## Security

If you discover a security vulnerability, please report it via GitHub Security Advisories rather than opening a public issue.
