#!/bin/bash
# PreToolUse(Bash): blocks `gh pr create` unless the qa-gate skill's commands passed against the
# current worktree state. Marker is written by mark-qa-gate-passed.sh (PostToolUse on the last
# qa_gate_commands entry). Keyed on HEAD + diff content rather than session_id: the qa-gate skill
# always runs in a forked sub-session (context: fork), whose session_id differs from the session
# that later runs `gh pr create`, so a session-scoped marker could never match.
INPUT=$(cat)

if echo "$INPUT" | grep -qE '"command"[[:space:]]*:[[:space:]]*"[^"]*gh pr create'; then
  PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
  STATE="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null):$(git -C "$PROJECT_DIR" diff HEAD 2>/dev/null | git -C "$PROJECT_DIR" hash-object --stdin 2>/dev/null)"
  MARKER="$PROJECT_DIR/.claude/.qa-gate/passed"
  if [ ! -f "$MARKER" ] || [ "$(cat "$MARKER")" != "$STATE" ]; then
    echo "gh pr create заблокирован: сначала запусти skill qa-gate и дождись успеха всех его команд для текущего состояния рабочего дерева." >&2
    exit 2
  fi
fi

exit 0
