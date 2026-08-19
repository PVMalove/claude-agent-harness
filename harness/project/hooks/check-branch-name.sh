#!/bin/bash
# PreToolUse(Bash): enforces the branch_pattern from .harness/project.json (docs/agents/git-workflow.md).
INPUT=$(cat)

if echo "$INPUT" | grep -qE '"command"[[:space:]]*:[[:space:]]*"[^"]*git checkout -b'; then
  RAW=$(echo "$INPUT" | grep -oE '"command"[[:space:]]*:[[:space:]]*"[^"]*"')
  BRANCH=$(echo "$RAW" | sed -E 's/.*git checkout -b[[:space:]]+([^ "]+).*/\1/')

  PROJECT_JSON="${CLAUDE_PROJECT_DIR:-.}/.harness/project.json"
  PATTERN=$(grep -oE '"branch_pattern"[[:space:]]*:[[:space:]]*"[^"]*"' "$PROJECT_JSON" 2>/dev/null | sed -E 's/.*"branch_pattern"[[:space:]]*:[[:space:]]*"(.*)"/\1/')
  PATTERN=${PATTERN:-^feature/issue-[0-9]+-.+}

  if ! echo "$BRANCH" | grep -qE "$PATTERN"; then
    echo "Ветка '$BRANCH' не соответствует branch_pattern из .harness/project.json ('$PATTERN', docs/agents/git-workflow.md)." >&2
    exit 2
  fi
fi

exit 0
