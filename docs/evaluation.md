# Evaluation

Local evaluation suites for benchmarking CUA against known tasks. Define cases in YAML, run them with repeated trials, and get scored results with pass/fail expectations.

## Quick Start

```bash
# Run a suite
python -c "
import asyncio
from evaluation import load_suite, run_suite, write_suite_report

async def main():
    suite = await load_suite('evaluation/suites/example.yaml')
    report = await run_suite(suite)
    await write_suite_report(report, 'output/evals/report.json')
    print(f'{report.passed}/{report.total} passed ({report.pass_rate:.0%})')

asyncio.run(main())
"
```

## Suite Format

Suites are YAML files with a list of evaluation cases:

```yaml
name: my-suite
cases:
  - id: agent-example
    directive: Open example.com and return the page title
    start_url: https://example.com
    execution_mode: agent_only    # agent_only | playbook_only | hybrid_auto
    trials: 3                     # repeat count for statistical confidence
    benchmark_tags: [smoke, public]
    max_steps: 5
    output_schema:                # optional JSON schema for structured output
      type: object
      properties:
        summary: { type: string }
    expect:
      must_succeed: true
      summary_contains: ["example"]
      max_actions: 4
      min_trial_pass_rate: 0.8    # at least 80% of trials must pass

  - id: playbook-cancel-order
    directive: Cancel order 12345
    playbook: cancel_order
    execution_mode: playbook_only
    benchmark_tags: [deterministic]
    playbook_params:
      order_id: "12345"
    expect:
      must_succeed: true
      handoff: forbid             # no LLM fallback allowed
```

## Execution Modes

| Mode | Behavior |
|---|---|
| `hybrid_auto` (default) | Uses playbook if one is specified, otherwise uses the LLM agent. Playbook failures can hand off to the agent. |
| `playbook_only` | Runs the specified playbook only. Requires the `playbook` field. |
| `agent_only` | Runs the LLM agent directly, ignoring any playbook. |

## Expectations

Each case has an `expect` block that defines pass/fail criteria. All expectations are optional; omitted checks are skipped.

### Content Checks

| Field | Type | Description |
|---|---|---|
| `must_succeed` | `bool` | The case must complete successfully (default: `true`) |
| `summary_contains` | `list[str]` | Summary must contain all listed substrings (case-insensitive) |
| `error_contains` | `list[str]` | Error message must contain all listed substrings |
| `extracted_text_contains` | `list[str]` | Extracted text from steps must contain all substrings |
| `required_data_keys` | `list[str]` | Dotted paths (e.g., `details.title`) that must exist in result data |
| `data_values_contain` | `dict[str, str]` | Dotted path to expected substring in result data values |

### Performance Bounds

| Field | Type | Description |
|---|---|---|
| `max_duration_ms` | `int` | Maximum wall-clock time per trial |
| `max_actions` | `int` | Maximum number of actions per trial |
| `min_actions` | `int` | Minimum number of actions per trial |
| `max_input_tokens` | `int` | Maximum input tokens per trial |
| `max_output_tokens` | `int` | Maximum output tokens per trial |
| `max_estimated_cost_usd` | `float` | Maximum estimated cost per trial |

### Trial Aggregation

| Field | Type | Description |
|---|---|---|
| `min_trial_pass_rate` | `float` | Minimum fraction of trials that must pass (e.g., `0.8` for 80%) |
| `max_p95_duration_ms` | `int` | Maximum p95 duration across trials |

### Handoff Behavior

| Field | Type | Description |
|---|---|---|
| `handoff` | `allow\|require\|forbid` | Whether LLM handoff is allowed, required, or forbidden (default: `allow`) |

## Trials

Set `trials: N` on a case to run it multiple times. This is useful for measuring reliability and performance variance of non-deterministic LLM agent cases.

When `trials > 1`:

- Each trial is scored independently against the case expectations
- Results are aggregated into a single `EvalCaseResult` with averages and p95 metrics
- The case passes only if **all trials pass** (unless `min_trial_pass_rate` is set)
- The report includes `trial_pass_rate`, `avg_duration_ms`, `p95_duration_ms`, and `avg_estimated_cost_usd`

## Cost Estimation

To track estimated LLM costs, set per-million token pricing on the case:

```yaml
- id: cost-tracked-case
  directive: ...
  input_token_cost_per_million_usd: 3.0
  output_token_cost_per_million_usd: 15.0
  expect:
    max_estimated_cost_usd: 0.05
```

Cost is computed as `(input_tokens / 1M) * input_rate + (output_tokens / 1M) * output_rate`. Playbook-only cases always report `$0.00` since they make no LLM calls.

## Case Configuration

| Field | Default | Description |
|---|---|---|
| `id` | (required) | Unique case identifier |
| `directive` | (required) | Natural language task description |
| `profile` | `default` | Agent profile to use |
| `playbook` | None | Playbook ID for deterministic execution |
| `execution_mode` | `hybrid_auto` | How to run the case |
| `trials` | `1` | Number of repeated runs |
| `benchmark_tags` | `[]` | Tags for filtering and grouping |
| `credentials` | None | Credentials dict for authenticated flows |
| `allow_private_networks` | `false` | Allow localhost and private IPs |
| `start_url` | None | URL to open on browser launch |
| `max_steps` | `50` | Maximum agent loop iterations |
| `thinking` | `high` | LLM thinking effort level |
| `output_schema` | None | JSON schema for structured output |
| `metadata` | `{}` | Arbitrary metadata (not used by the runner) |

## Report Structure

`write_suite_report` outputs a JSON file with the full result hierarchy:

```text
EvalSuiteResult
  name, passed, failed, total, pass_rate
  avg_duration_ms, p95_duration_ms, avg_estimated_cost_usd
  deterministic_hit_rate, handoff_rate, handoff_rescue_rate
  case_results: [EvalCaseResult]
    id, passed, success, mode, summary, error
    duration_ms, actions, input_tokens, output_tokens
    trials_run, trial_pass_rate, avg_duration_ms, p95_duration_ms
    avg_estimated_cost_usd, failed_checks
    trial_results: [EvalTrialResult]   # included when trials > 1
      trial_index, passed, success, mode, duration_ms, ...
```

## Architecture

```text
evaluation/
├── models.py          Pydantic models (EvalCase, EvalTrialResult, EvalCaseResult, etc.)
├── scoring.py         Pure scoring and aggregation (no I/O)
├── runner.py          Browser orchestration, trial loop, suite runner
├── __init__.py        Public API
└── suites/
    └── example.yaml   Example benchmark suite
```

Scoring is fully separated from execution -- `scoring.py` contains pure functions with no I/O dependencies, making expectations and aggregation independently testable.
