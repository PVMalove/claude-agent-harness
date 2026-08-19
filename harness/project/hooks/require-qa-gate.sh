#!/bin/bash
# PreToolUse(Bash): blocks `gh pr create` unless the qa-gate skill's commands passed this session.
# Marker is written by mark-qa-gate-passed.sh (PostToolUse on the last qa_gate_commands entry).
INPUT=$(cat)

if echo "$INPUT" | grep -qE '"command"[[:space:]]*:[[:space:]]*"[^"]*gh pr create'; then
  SESSION_ID=$(echo "$INPUT" | grep -oE '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"session_id"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')
  MARKER="${CLAUDE_PROJECT_DIR:-.}/.claude/.qa-gate/passed"
  if [ ! -f "$MARKER" ] || [ "$(cat "$MARKER")" != "$SESSION_ID" ]; then
    echo "gh pr create заблокирован: сначала запусти skill qa-gate и дождись успеха всех его команд в этой сессии." >&2
    exit 2
  fi
fi

exit 0
