Create a CUA playbook by recording a user's browser interactions.

## Workflow

Follow these steps in order:

### Step 1: Gather information

Ask the user for:
1. **Directive**: What task does this playbook automate? (e.g., "Cancel order #12345")
2. **Start URL**: Where does the workflow begin? (e.g., https://dashboard.internal/orders)
3. **Parameters**: What values in the directive should be parameterized? (e.g., order_id = "12345")
4. **Auth required?**: Does this dashboard need login credentials?

### Step 2: Record browser interactions

Run the recording script. The user will interact with the browser manually.

```bash
.venv/bin/python scripts/record_interaction.py --start-url <url> --output output/recording.json
```

Tell the user:
- A browser window will open — perform the workflow manually
- When finished, close the browser or press Ctrl+Shift+S

Wait for the script to complete.

### Step 3: Read the recording

Read the file `output/recording.json`. This contains the recorded interactions: clicks, typing, navigation, selections, and scrolling, each with generated selectors.

### Step 4: Generate the playbook

Convert the recorded interactions into a playbook YAML file. Apply these optimizations:

**Auth handling (IMPORTANT):**
- **NEVER include login/auth steps** (typing username, password, clicking login) in the playbook.
- Authentication is handled externally by the auth system before the playbook runs. It detects login forms and performs fresh login automatically.
- If the recording includes login interactions, **strip them out**. The playbook should start from the first post-login action.
- Set `auth_required: true` if login was part of the recording — this tells the runner to authenticate before executing.

**Action mapping:**
- `click` events → `click` steps with the recorded `selector` (primary + fallbacks)
- `key_press` events with `text` → `key_press` steps with `params.text`
- `key_press` events with `key: Enter/Tab` → `key_press` steps with `params.key`
- `goto` events → `goto` steps with `params.url`
- `select` events → `select` steps with `params.value`
- `scroll` events → `scroll` steps (only include if meaningful, skip minor scrolls)

**Parameterization:**
- Replace concrete values from the directive with `{parameter_name}` placeholders
- For each parameter, add a `parameters` entry with `name`, `type`, `description`, and a `pattern` regex for extraction from directives

**Step descriptions:**
- Write clear, human-readable descriptions using the element context (e.g., "Click the Cancel button in order row" not "Click button")
- **Include `{parameter_name}` placeholders in descriptions** so they materialize with actual values at runtime (e.g., "Click on contact {user_id}" not "Click on the target contact by user ID"). This is critical — when the LLM agent takes over on failure, it reads these descriptions to understand what to do.

**Verification (infer from context):**
- After `goto` steps: add `expect_url_contains` based on the target URL path
- After `click` that triggers navigation: add `expect_url_contains` for the new URL
- After form submissions: add `expect_element_visible` or `expect_text_on_page` if the next recorded action implies a new page state
- After search/filter actions: add `expect_element_visible: "table tbody tr"` or similar

**Guardrails:**
- If start URL is localhost or private IP → set `allow_private_networks: true`
- If auth was recorded → set `auth_required: true`
- Set `enable_llm_action_check: false` (pre-approved flows)

**Tags:**
- Generate 2-4 tags from the directive for matching (e.g., ["cancel", "cancel order"])

**Cleanup:**
- Remove redundant scroll steps (minor scrolls between real actions)
- Merge consecutive `key_press` steps targeting the same element into one
- Skip interactions that look like accidental clicks (clicks on body/html)
- **Strip login/auth interactions**: Remove any events on login pages (look for URLs containing `/login`, `/signin`, `/sign-in`, or page titles with "Log in"/"Sign in", and events targeting `#id_username`, `#id_password`, `input[type='password']`, or submit buttons on those pages). These are handled by the auth system.

### Step 5: Write and validate

1. Choose a playbook ID (snake_case, derived from the task — e.g., `cancel_order`)
2. Write the YAML to `playbooks/definitions/<id>.yaml`
3. Validate it loads correctly:

```bash
.venv/bin/python -c "from playbooks.store import PlaybookStore; pb = PlaybookStore().load('<id>'); print(f'Loaded: {pb.name} ({len(pb.steps)} steps)')"
```

4. Show the user the generated playbook and the command to test it:

```bash
.venv/bin/python scripts/run_local.py \
  --directive "<example directive with concrete param values>" \
  --playbook <id> \
  --playbook-params '{"param1": "value1", "param2": "value2"}' \
  --credentials '{"username": "...", "password": "..."}' \
  --allow-private-networks
```

Replace the parameter values and credentials with real examples. Omit `--credentials` if `auth_required` is false, omit `--allow-private-networks` if not needed.

5. Ask if they want any adjustments

### Reference: Playbook YAML schema

```yaml
id: playbook_id
name: Human Readable Name
description: What this playbook does
tags: ["tag1", "tag2"]
auth_required: true
start_url: "https://dashboard.internal/path"
guardrails:
  allow_private_networks: true
  enable_llm_action_check: false
  max_urls_visited: 200
  max_consecutive_errors: 10
parameters:
  - name: param_name
    type: string
    description: What this parameter is
    pattern: "regex for extraction from directive"
steps:
  - action: goto
    params:
      url: "https://dashboard.internal/path"
    verify:
      expect_url_contains: "/path"
    description: Navigate to the page

  - action: click
    selector:
      primary: "role=button[name='Search']"
      fallbacks:
        - "text=Search"
        - "button.search-btn"
    description: Click search button

  - action: key_press
    params:
      text: "{param_name}"
    description: Type the parameter value

  - action: key_press
    params:
      key: Enter
    verify:
      expect_element_visible: "table tbody tr"
    description: Submit search

  - action: select
    selector:
      primary: "select[name='status']"
    params:
      value: "cancelled"
    description: Select status from dropdown

  - action: extract
    selector:
      primary: ".result-id"
    params:
      mode: text
    store_as: extracted_value
    description: Extract the result ID
```
