#!/bin/bash
# PreToolUse(Bash): require QA evidence for the checkout supplying the PR branch.
# Verdict envelope (hook_response.py): reruns this script, answers with the JSON verdict and the
# exit code of the hook policy. Without Python or the helper the script below decides alone.
if [ -z "${HARNESS_HOOK_ENVELOPE:-}" ]; then
  ENVELOPE_DIR="$(cd "$(dirname "$0")" && pwd)"
  ENVELOPE_PY="$(command -v python3 || command -v python)"
  if [ -n "$ENVELOPE_PY" ] && [ -f "$ENVELOPE_DIR/hook_response.py" ]; then
    exec "$ENVELOPE_PY" "$ENVELOPE_DIR/hook_response.py" run require-qa-gate "${BASH:-bash}" "$0"
  fi
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python "$SCRIPT_DIR/qa-gate-state.py" require
