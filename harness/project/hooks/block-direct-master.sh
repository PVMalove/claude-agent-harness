#!/bin/bash
# PreToolUse(Bash): "Zero Direct Commits" from docs/agents/git-workflow.md, enforced deterministically.
INPUT=$(cat)
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить запрет прямого коммита: Python 3.9+ не найден." >&2
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

REPO_DIR="${CLAUDE_PROJECT_DIR:-.}"
PROJECT_JSON="$REPO_DIR/.harness/project.json"

BASE_BRANCH="$( [ -f "$PROJECT_JSON" ] && "$PY" -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="ignore") as f:
        data = json.load(f)
        sys.stdout.write(str(data.get("base_branch", "")))
except Exception:
    pass
' "$PROJECT_JSON" )"

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

if printf '%s\n' "$COMMAND" | grep -qE '\b(git commit|git push)\b'; then
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
if printf '%s\n' "$COMMAND" | grep -qE '\bgit push\b'; then
  for target in master main "$BASE_BRANCH"; do
    [ -n "$target" ] || continue
    if printf '%s\n' "$COMMAND" | grep -qE "(^|[\"[:space:]:])(refs/heads/)?$target([\"[:space:]]|\$)"; then
      echo "Zero Direct Commits: push с целевым рефом '$target' запрещён — работай на issue-ветке (docs/agents/git-workflow.md)." >&2
      exit 2
    fi
  done
  if printf '%s\n' "$COMMAND" | grep -qE '(^|["[:space:]:])(refs/heads/)?integration/[^"[:space:]]+(["[:space:]]|$)'; then
    echo "Zero Direct Commits: push с целевым integration-рефом запрещён — работай на issue-ветке (docs/agents/git-workflow.md)." >&2
    exit 2
  fi
fi

exit 0
