#!/bin/bash
# =============================================================================
# Immaculate route-policy evaluation for the Evolve pipeline (main.py --eval-script).
# Runs evaluator.py on the step's code and writes step_N/results.json.
# Data: $ROUTE_POLICY_DATA_DIR (default: this experiment's data/), prepared by prepare.py.
# =============================================================================
set -euo pipefail

STEP_DIR="$(pwd)"
if [[ "$STEP_DIR" == */steps/step_* ]]; then
    EXPERIMENT_DIR="$(dirname "$(dirname "$STEP_DIR")")"
else
    EXPERIMENT_DIR="$(dirname "$STEP_DIR")"
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SRC_CODE_FILE="${STEP_DIR}/code"
RESULT_JSON="${STEP_DIR}/results.json"
LOG_FILE="${STEP_DIR}/eval.log"

if [ ! -f "$SRC_CODE_FILE" ]; then
    echo "Source code file not found: ${SRC_CODE_FILE}" | tee -a "$LOG_FILE" >&2
    exit 1
fi

# evaluator.py always writes results.json, with success=false and a floor fitness on failure.
python3 "${SCRIPT_DIR}/evaluator.py" "$SRC_CODE_FILE" "$RESULT_JSON" \
    --timeout-secs "${ROUTE_POLICY_TIMEOUT_SECS:-120}" >> "$LOG_FILE" 2>&1 || true
echo "experiment: ${EXPERIMENT_DIR}" >> "$LOG_FILE"
exit 0
