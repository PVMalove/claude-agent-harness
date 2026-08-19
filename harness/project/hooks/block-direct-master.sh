#!/bin/bash
# PreToolUse(Bash): "Zero Direct Commits" from docs/agents/git-workflow.md, enforced deterministically.
INPUT=$(cat)

if echo "$INPUT" | grep -qE '"command"[[:space:]]*:[[:space:]]*"[^"]*\b(git commit|git push)\b'; then
  BRANCH=$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ "$BRANCH" = "master" ] || [ "$BRANCH" = "main" ]; then
    echo "Zero Direct Commits: коммит/push в ветку '$BRANCH' запрещён — работай на feature-ветке (docs/agents/git-workflow.md)." >&2
    exit 2
  fi
fi

exit 0
