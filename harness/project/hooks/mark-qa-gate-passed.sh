#!/bin/bash
# PostToolUse(Bash): fires only on success. Records that the last command in qa_gate_commands
# (.harness/project.json) passed against the current worktree state, so require-qa-gate.sh can
# require it before a PR. Sequential-stop-on-failure in the qa-gate skill means reaching the last
# command implies every earlier one already passed. Keyed on HEAD + diff content rather than
# session_id: the qa-gate skill always runs in a forked sub-session (context: fork), whose
# session_id differs from the session that later runs `gh pr create`.
INPUT=$(cat)

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
PROJECT_JSON="$PROJECT_DIR/.harness/project.json"
LAST_COMMAND=$(grep -oE '"qa_gate_commands"[[:space:]]*:[[:space:]]*\[[^]]*\]' "$PROJECT_JSON" 2>/dev/null \
  | grep -oE '"[^"]*"' | tail -n 1 | sed -E 's/^"(.*)"$/\1/')

[ -z "$LAST_COMMAND" ] && exit 0

ESCAPED=$(echo "$LAST_COMMAND" | sed -E 's/[][\.*^$/]/\\&/g')
echo "$INPUT" | grep -qE "\"command\"[[:space:]]*:[[:space:]]*\"[^\"]*$ESCAPED" || exit 0

STATE="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null):$(git -C "$PROJECT_DIR" diff HEAD 2>/dev/null | git -C "$PROJECT_DIR" hash-object --stdin 2>/dev/null)"
MARKER_DIR="$PROJECT_DIR/.claude/.qa-gate"
mkdir -p "$MARKER_DIR"
echo "$STATE" > "$MARKER_DIR/passed"

exit 0
