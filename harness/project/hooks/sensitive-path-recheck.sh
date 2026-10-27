#!/bin/bash
# PreToolUse(Write|Edit), advisory, strict profile only: warns on every new edit of a sensitive path
# (manifest or lock file, CI, migrations, auth, permissions, hooks, .harness/project.json) and names
# an evidence record. The hook never blocks (ADR 0024, section 2a).
# Verdict envelope (hook_response.py): applies the hook profile and answers with the JSON verdict.
# Without the helper the body decides alone; without Python the hook allows.
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_PY="$(command -v python3 || command -v python)"
[ -n "$HOOK_PY" ] || exit 0
if [ -z "${HARNESS_HOOK_ENVELOPE:-}" ] && [ -f "$HOOK_DIR/hook_response.py" ]; then
  exec "$HOOK_PY" "$HOOK_DIR/hook_response.py" run sensitive-path-recheck "$HOOK_PY" "$HOOK_DIR/change_impact_hooks.py" sensitive-path-recheck
fi
exec "$HOOK_PY" "$HOOK_DIR/change_impact_hooks.py" sensitive-path-recheck
