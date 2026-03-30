Create a CUA evaluation suite for benchmarking agent and playbook performance.

## Workflow

Follow these steps in order:

### Step 1: Gather information

Ask the user for:
1. **Suite name**: A short identifier for this suite (e.g., "order-management-smoke")
2. **Cases**: For each case, ask:
   - **Directive**: What task should be evaluated? (e.g., "Open example.com and return the page title")
   - **Execution mode**: `agent_only`, `playbook_only`, or `hybrid_auto` (default)?
   - **Playbook** (if playbook_only or hybrid_auto): Which playbook ID to use?
   - **Start URL**: Where does the task begin?
   - **Trials**: How many times to repeat? (default: 1, use more for reliability benchmarks)
   - **Credentials needed?**: Does this case require login?
   - **What defines success?**: What should be checked — output content, performance limits, cost bounds?

Collect all cases before proceeding. For each case, suggest a snake_case `id` derived from the directive.

### Step 2: Define expectations

For each case, help the user define the `expect` block. Walk through the relevant checks:

**Content checks (ask about these):**
- `must_succeed`: Should the case always complete successfully? (default: true)
- `summary_contains`: Any substrings the summary must include?
- `required_data_keys`: Any dotted paths that must exist in output data? (e.g., `details.title`)
- `data_values_contain`: Any data values that must contain specific text?
- `extracted_text_contains`: Any text that must appear in extracted page text?

**Performance bounds (ask if relevant):**
- `max_duration_ms`: Maximum wall-clock time?
- `max_actions`: Maximum number of browser actions?
- `max_input_tokens` / `max_output_tokens`: Token limits?
- `max_estimated_cost_usd`: Cost ceiling? (requires token pricing on the case)

**Trial-level checks (ask if trials > 1):**
- `min_trial_pass_rate`: Minimum fraction of trials that must pass? (e.g., 0.8 for 80%)
- `max_p95_duration_ms`: Maximum p95 latency across trials?

**Handoff behavior (ask for playbook cases):**
- `handoff`: `allow` (default), `require` (must hand off to agent), or `forbid` (no LLM fallback)?

Only include checks the user cares about — omitted checks are skipped.

### Step 3: Define output schemas (if needed)

If any case needs structured output, help define the JSON schema:

```yaml
output_schema:
  type: object
  properties:
    summary:
      type: string
    details:
      type: object
      properties:
        title:
          type: string
      required:
        - title
  required:
    - summary
    - details
```

Only include `output_schema` for cases that need structured data extraction. Cases that only check `must_succeed` or `summary_contains` don't need it.

### Step 4: Generate the suite YAML

Convert the gathered information into a suite YAML file. Apply these rules:

**Case IDs:**
- Use `snake_case` derived from the directive (e.g., "Check dashboard totals" → `check_dashboard_totals`)
- Prefix with execution mode hint if mixed suite (e.g., `agent_check_totals`, `playbook_cancel_order`)

**Tags:**
- Generate 2-4 `benchmark_tags` per case from the directive and execution mode
- Use consistent tag categories across the suite: `smoke`, `regression`, `deterministic`, `public`, `internal_replica`

**Cost tracking (for agent cases):**
- If the user set `max_estimated_cost_usd`, add `input_token_cost_per_million_usd` and `output_token_cost_per_million_usd` fields with current pricing for the model they use. Approximate rates (verify against provider pricing pages as these change frequently):
  - Claude Sonnet: input ~$3, output ~$15
  - Gemini Flash: input ~$0.10, output ~$0.40
  - GPT-4.1: input ~$2, output ~$8

**Defaults to omit:**
- `execution_mode: hybrid_auto` (it's the default)
- `trials: 1` (it's the default)
- `must_succeed: true` (it's the default)
- `handoff: allow` (it's the default)
- `thinking: high` (it's the default)
- `max_steps: 50` (it's the default)
- Only include fields that differ from defaults to keep the YAML clean.

**Playbook cases:**
- Must include `playbook: <id>` field
- For `playbook_only`: Add `execution_mode: playbook_only`
- For deterministic playbooks: Consider `handoff: forbid` and tags like `deterministic`
- Include `playbook_params` if the playbook requires parameters

### Step 5: Write and validate

1. Choose a file name (snake_case, matching suite name — e.g., `order_management_smoke.yaml`)
2. Write the YAML to `evaluation/suites/<name>.yaml`
3. Validate it loads correctly:

```bash
.venv/bin/python -c "
import asyncio
from evaluation import load_suite
suite = asyncio.run(load_suite('evaluation/suites/<name>.yaml'))
print(f'Suite: {suite.name}')
for case in suite.cases:
    print(f'  - {case.id} ({case.execution_mode}, {case.trials} trial(s))')
print(f'Total: {len(suite.cases)} cases')
"
```

4. Show the user the generated suite and the command to run it:

```bash
.venv/bin/python -c "
import asyncio
from evaluation import load_suite, run_suite, write_suite_report

async def main():
    suite = await load_suite('evaluation/suites/<name>.yaml')
    report = await run_suite(suite)
    await write_suite_report(report, 'output/evals/<name>_report.json')
    print(f'{report.passed}/{report.total} passed ({report.pass_rate:.0%})')

asyncio.run(main())
"
```

5. Ask if they want any adjustments

### Reference: Suite YAML schema

```yaml
name: suite-name
cases:
  - id: case_id
    directive: Natural language task description
    execution_mode: agent_only          # agent_only | playbook_only | hybrid_auto
    playbook: playbook_id               # required for playbook_only
    playbook_params:                    # parameters for playbook execution
      param_name: "value"
    trials: 3                           # repeated runs for reliability
    benchmark_tags: [smoke, public]
    start_url: "https://example.com"
    credentials:                        # for authenticated flows
      username: "admin"
      password: "secret"
    allow_private_networks: false
    max_steps: 50
    thinking: high                      # minimal | low | medium | high | xhigh
    input_token_cost_per_million_usd: 3.0   # for cost tracking
    output_token_cost_per_million_usd: 15.0
    output_schema:                      # JSON schema for structured output
      type: object
      properties:
        summary: { type: string }
    metadata: {}                        # arbitrary metadata (not used by runner)
    expect:
      # Content checks
      must_succeed: true
      summary_contains: ["expected text"]
      error_contains: ["expected error"]
      extracted_text_contains: ["page text"]
      required_data_keys: ["details.title"]
      data_values_contain:
        details.title: "Expected Title"

      # Performance bounds
      max_duration_ms: 30000
      max_actions: 10
      min_actions: 1
      max_input_tokens: 5000
      max_output_tokens: 1000
      max_estimated_cost_usd: 0.05

      # Trial aggregation
      min_trial_pass_rate: 0.8
      max_p95_duration_ms: 15000

      # Handoff behavior
      handoff: allow                    # allow | require | forbid
```
