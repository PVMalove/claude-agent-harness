#!/bin/bash
# PreToolUse(Bash): require QA evidence for the checkout supplying the PR branch.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python "$SCRIPT_DIR/qa-gate-state.py" require
