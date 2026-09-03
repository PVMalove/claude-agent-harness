#!/bin/bash
# PreToolUse(Bash): "Zero Direct Commits" from docs/agents/git-workflow.md, enforced deterministically.
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | grep -oE '"command"[[:space:]]*:[[:space:]]*"[^"]*"')
REPO_DIR="${CLAUDE_PROJECT_DIR:-.}"
PROJECT_JSON="$REPO_DIR/.harness/project.json"
BASE_BRANCH=$(grep -oE '"base_branch"[[:space:]]*:[[:space:]]*"[^"]*"' "$PROJECT_JSON" 2>/dev/null | sed -E 's/.*"base_branch"[[:space:]]*:[[:space:]]*"([^"]*)"/\1/')

is_protected_branch() {
  BRANCH_NAME="$1"
  if [ "$BRANCH_NAME" = "master" ] || [ "$BRANCH_NAME" = "main" ]; then
    return 0
  fi
  if [ -n "$BASE_BRANCH" ] && [ "$BRANCH_NAME" = "$BASE_BRANCH" ]; then
    return 0
  fi
  echo "$BRANCH_NAME" | grep -qE '^integration/'
}

if echo "$COMMAND" | grep -qE '\b(git commit|git push)\b'; then
  BRANCH=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)
  if is_protected_branch "$BRANCH"; then
    echo "Zero Direct Commits: коммит/push в защищённую ветку '$BRANCH' запрещён — работай на issue-ветке (docs/agents/git-workflow.md)." >&2
    exit 2
  fi
fi

# Also catch a push whose *target* refspec is a protected branch even from an issue branch
# (e.g. `git push origin HEAD:master`, `git push origin feature-x:master`, bare `git push
# origin master`, or `git push origin HEAD:integration/payments`) — the current-branch check
# above only sees where HEAD is, not where the ref is going.
if echo "$COMMAND" | grep -qE '\bgit push\b'; then
  for target in master main "$BASE_BRANCH"; do
    [ -n "$target" ] || continue
    if echo "$COMMAND" | grep -qE "(^|[\"[:space:]:])(refs/heads/)?$target([\"[:space:]]|\$)"; then
      echo "Zero Direct Commits: push с целевым рефом '$target' запрещён — работай на issue-ветке (docs/agents/git-workflow.md)." >&2
      exit 2
    fi
  done
  if echo "$COMMAND" | grep -qE '(^|["[:space:]:])(refs/heads/)?integration/[^"[:space:]]+(["[:space:]]|$)'; then
    echo "Zero Direct Commits: push с целевым integration-рефом запрещён — работай на issue-ветке (docs/agents/git-workflow.md)." >&2
    exit 2
  fi
fi

exit 0
