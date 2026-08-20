#!/bin/bash
# PreToolUse(Bash): "Zero Direct Commits" from docs/agents/git-workflow.md, enforced deterministically.
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | grep -oE '"command"[[:space:]]*:[[:space:]]*"[^"]*"')

if echo "$COMMAND" | grep -qE '\b(git commit|git push)\b'; then
  BRANCH=$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ "$BRANCH" = "master" ] || [ "$BRANCH" = "main" ]; then
    echo "Zero Direct Commits: коммит/push в ветку '$BRANCH' запрещён — работай на feature-ветке (docs/agents/git-workflow.md)." >&2
    exit 2
  fi
fi

# Also catch a push whose *target* refspec is master/main even from a feature branch
# (e.g. `git push origin HEAD:master`, `git push origin feature-x:master`, bare `git push
# origin master`) — the current-branch check above only sees where HEAD is, not where the
# ref is going.
if echo "$COMMAND" | grep -qE '\bgit push\b'; then
  for target in master main; do
    if echo "$COMMAND" | grep -qE "(^|[\"[:space:]:])(refs/heads/)?$target([\"[:space:]]|\$)"; then
      echo "Zero Direct Commits: push с целевым рефом '$target' запрещён — работай на feature-ветке (docs/agents/git-workflow.md)." >&2
      exit 2
    fi
  done
fi

exit 0
