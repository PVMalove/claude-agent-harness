#!/bin/bash
# PostToolUse(Bash): fallback marker when the last configured QA command succeeds.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python "$SCRIPT_DIR/qa-gate-state.py" mark
