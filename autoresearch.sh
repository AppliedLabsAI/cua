#!/bin/bash
set -euo pipefail

# Run 3 diverse CUA agent tasks and report composite metrics
# Primary metric: total_steps (lower = better, captures efficiency)
# Secondary: duration_s, success_count, total_tokens

PYTHON=".venv/bin/python"
SCRIPT="scripts/run_local.py"
TOTAL_STEPS=0
TOTAL_DURATION_MS=0
TOTAL_INPUT_TOKENS=0
TOTAL_OUTPUT_TOKENS=0
SUCCESS_COUNT=0
CASE_COUNT=0
FAILED_CASES=""

run_case() {
    local case_name="$1"
    local directive="$2"
    local max_steps="$3"
    local extra_flags="${4:-}"

    CASE_COUNT=$((CASE_COUNT + 1))
    echo "=== Case $CASE_COUNT: $case_name ==="

    local output
    if output=$(timeout 180 $PYTHON $SCRIPT \
        --directive "$directive" \
        --max-steps "$max_steps" \
        $extra_flags 2>&1); then
        local exit_code=0
    else
        local exit_code=$?
    fi

    # Extract JSON output between === lines
    local json_block
    json_block=$(echo "$output" | sed -n '/^{$/,/^}$/p' | head -30)

    if [ -n "$json_block" ]; then
        local status actions duration_ms
        status=$(echo "$json_block" | $PYTHON -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','failed'))" 2>/dev/null || echo "failed")
        actions=$(echo "$json_block" | $PYTHON -c "import sys,json; d=json.load(sys.stdin); print(d.get('actions',0))" 2>/dev/null || echo "0")
        duration_ms=$(echo "$json_block" | $PYTHON -c "import sys,json; d=json.load(sys.stdin); print(d.get('duration_ms',0))" 2>/dev/null || echo "0")

        echo "  Status: $status, Actions: $actions, Duration: ${duration_ms}ms"

        TOTAL_STEPS=$((TOTAL_STEPS + actions))
        TOTAL_DURATION_MS=$((TOTAL_DURATION_MS + duration_ms))

        if [ "$status" = "completed" ]; then
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        else
            FAILED_CASES="$FAILED_CASES $case_name"
        fi
    else
        echo "  CRASHED (no JSON output), exit_code=$exit_code"
        # Penalize crashes with max_steps
        TOTAL_STEPS=$((TOTAL_STEPS + max_steps))
        FAILED_CASES="$FAILED_CASES $case_name"
    fi
}

# Case 1: Multi-step admin task (login + navigate + extract)
run_case "admin-shop-hours" \
    "Go to http://localhost:8000/admin, use test@appliedlabs.ai for email and icarus9211 for the password, login, find the shop of the latest conversation sorted by creation date, and tell me the opening hours of that shop" \
    20 \
    "--allow-private-networks"

# Case 2: Simple public page extraction
run_case "example-dot-com" \
    "Go to https://example.com and tell me the exact text of the heading and the first paragraph" \
    8

# Case 3: Admin counting task
run_case "admin-count-shops" \
    "Go to http://localhost:8000/admin, use test@appliedlabs.ai for email and icarus9211 for the password, login, go to the Shops section, and tell me how many shops are listed" \
    15 \
    "--allow-private-networks"

# Compute composite score: total_steps + penalty for failures
# Each failure adds 10 steps penalty to make success critical
FAILURE_PENALTY=$(( (CASE_COUNT - SUCCESS_COUNT) * 10 ))
COMPOSITE_SCORE=$((TOTAL_STEPS + FAILURE_PENALTY))
DURATION_S=$((TOTAL_DURATION_MS / 1000))

echo ""
echo "=== SUMMARY ==="
echo "Cases: $CASE_COUNT, Success: $SUCCESS_COUNT/$CASE_COUNT"
echo "Total steps: $TOTAL_STEPS, Failure penalty: $FAILURE_PENALTY"
if [ -n "$FAILED_CASES" ]; then
    echo "Failed:$FAILED_CASES"
fi
echo ""
echo "METRIC total_steps=$COMPOSITE_SCORE"
echo "METRIC raw_steps=$TOTAL_STEPS"
echo "METRIC success_count=$SUCCESS_COUNT"
echo "METRIC duration_s=$DURATION_S"
echo "METRIC case_count=$CASE_COUNT"
