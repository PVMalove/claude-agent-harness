#!/bin/bash
# PostToolUse(Bash): fires only on success. Records that the last command in qa_gate_commands
# (.harness/project.json) passed this session, so require-qa-gate.sh can require it before a PR.
# Sequential-stop-on-failure in the qa-gate skill means reaching the last command implies every
# earlier one already passed.
INPUT=$(cat)

PROJECT_JSON="${CLAUDE_PROJECT_DIR:-.}/.harness/project.json"
LAST_COMMAND=$(grep -oE '"qa_gate_commands"[[:space:]]*:[[:space:]]*\[[^]]*\]' "$PROJECT_JSON" 2>/dev/null \
  | grep -oE '"[^"]*"' | tail -n 1 | sed -E 's/^"(.*)"$/\1/')

[ -z "$LAST_COMMAND" ] && exit 0

ESCAPED=$(echo "$LAST_COMMAND" | sed -E 's/[][\.*^$/]/\\&/g')
echo "$INPUT" | grep -qE "\"command\"[[:space:]]*:[[:space:]]*\"[^\"]*$ESCAPED" || exit 0

SESSION_ID=$(echo "$INPUT" | grep -oE '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"session_id"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')
MARKER_DIR="${CLAUDE_PROJECT_DIR:-.}/.claude/.qa-gate"
mkdir -p "$MARKER_DIR"
echo "$SESSION_ID" > "$MARKER_DIR/passed"

exit 0
