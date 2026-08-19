#!/bin/bash
# PreToolUse(EnterWorktree): same branch_pattern convention as check-branch-name.sh,
# extended to the native worktree tool (docs/agents/worktrees.md).
INPUT=$(cat)

# Entering an existing worktree by path, or letting the tool assign a random name, isn't ticket
# branch creation - only check when a name was explicitly given.
NAME=$(echo "$INPUT" | grep -oE '"name"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')

if [ -n "$NAME" ]; then
  PROJECT_JSON="${CLAUDE_PROJECT_DIR:-.}/.harness/project.json"
  PATTERN=$(grep -oE '"branch_pattern"[[:space:]]*:[[:space:]]*"[^"]*"' "$PROJECT_JSON" 2>/dev/null | sed -E 's/.*"branch_pattern"[[:space:]]*:[[:space:]]*"(.*)"/\1/')
  PATTERN=${PATTERN:-^feature/issue-[0-9]+-.+}

  if ! echo "$NAME" | grep -qE "$PATTERN"; then
    echo "Имя worktree/ветки '$NAME' не соответствует branch_pattern из .harness/project.json ('$PATTERN', docs/agents/git-workflow.md, docs/agents/worktrees.md)." >&2
    exit 2
  fi
fi

exit 0
