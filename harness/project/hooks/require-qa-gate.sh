#!/bin/bash
# PreToolUse(Bash): blocks `gh pr create` unless the qa-gate skill's commands passed against the
# current worktree state. Marker is written by the qa-gate skill itself (record-qa-gate-pass.sh),
# or as a fallback by mark-qa-gate-passed.sh (PostToolUse on the last qa_gate_commands entry run
# directly in the main session). Keyed on HEAD + diff content rather than session_id: the qa-gate
# skill runs in a forked sub-session (context: fork), whose session_id differs from the session
# that later runs `gh pr create`, so a session-scoped marker could never match.
INPUT=$(cat)
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить требование qa-gate: Python 3.9+ не найден." >&2
  exit 2
fi

COMMAND="$(printf '%s' "$INPUT" | "$PY" -c '
import json, sys
try:
    data = json.load(open(0, encoding="utf-8", errors="ignore"))
    command = data.get("tool_input", {}).get("command", "")
    if isinstance(command, str):
        sys.stdout.write(command)
except Exception:
    pass
')"

if printf '%s\n' "$COMMAND" | grep -qE '\bgh[[:space:]]+pr[[:space:]]+create\b'; then
  PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
  STATE="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null):$(git -C "$PROJECT_DIR" diff HEAD 2>/dev/null | git -C "$PROJECT_DIR" hash-object --stdin 2>/dev/null)"
  MARKER="$PROJECT_DIR/.claude/.qa-gate/passed"
  if [ ! -f "$MARKER" ] || [ "$(cat "$MARKER")" != "$STATE" ]; then
    echo "gh pr create заблокирован: сначала запусти skill qa-gate и дождись успеха всех его команд для текущего состояния рабочего дерева." >&2
    exit 2
  fi
fi

exit 0
