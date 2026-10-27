#!/bin/bash
# Advisory. PreToolUse(Write|Edit): records the touched file in the journal of the operation.
# Stop and SubagentStop (end of a role turn): reminds to report the targeted checks of the brief in
# checks_run when files changed after the last report. The reminder is a systemMessage; the hook never
# blocks and never continues the turn (ADR 0024, section 2a). It runs no tests, make or git.
# Verdict envelope (hook_response.py): applies the hook profile and answers with the JSON verdict.
# Without the helper the body decides alone; without Python the hook allows.
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_PY="$(command -v python3 || command -v python)"
[ -n "$HOOK_PY" ] || exit 0
if [ -z "${HARNESS_HOOK_ENVELOPE:-}" ] && [ -f "$HOOK_DIR/hook_response.py" ]; then
  exec "$HOOK_PY" "$HOOK_DIR/hook_response.py" run targeted-check-reminder "$HOOK_PY" "$HOOK_DIR/change_impact_hooks.py" targeted-check-reminder
fi
exec "$HOOK_PY" "$HOOK_DIR/change_impact_hooks.py" targeted-check-reminder
