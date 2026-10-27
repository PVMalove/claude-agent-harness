#!/bin/bash
# PreToolUse(Write|Edit), advisory: warns on the first touch of a risky path whose impact ChangeImpact
# cannot compute from the import graph (build and verification config, capabilities, skills). The
# warning goes to the model as additionalContext; the hook never blocks (ADR 0024, section 2a).
# Verdict envelope (hook_response.py): applies the hook profile and answers with the JSON verdict.
# Without the helper the body decides alone; without Python the hook allows.
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_PY="$(command -v python3 || command -v python)"
[ -n "$HOOK_PY" ] || exit 0
if [ -z "${HARNESS_HOOK_ENVELOPE:-}" ] && [ -f "$HOOK_DIR/hook_response.py" ]; then
  exec "$HOOK_PY" "$HOOK_DIR/hook_response.py" run impact-first-touch "$HOOK_PY" "$HOOK_DIR/change_impact_hooks.py" impact-first-touch
fi
exec "$HOOK_PY" "$HOOK_DIR/change_impact_hooks.py" impact-first-touch
