#!/bin/bash
# PreToolUse(Bash): enforces the branch_pattern from .harness/project.json, and that the
# numeric ID it encodes is a real, registered tracker issue ("Issue First", docs/agents/git-workflow.md).
INPUT=$(cat)
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить имя ветки: Python 3.9+ не найден." >&2
  exit 2
fi

BRANCH="$(printf '%s' "$INPUT" | "$PY" -c '
import json, shlex, sys
try:
    data = json.load(open(0, encoding="utf-8", errors="ignore"))
    cmd = data.get("tool_input", {}).get("command", "")
    if not isinstance(cmd, str):
        sys.exit(0)
    tokens = shlex.split(cmd)
    for i, t in enumerate(tokens):
        if t in ("checkout", "switch") and i > 0 and tokens[i-1] == "git":
            for j in range(i+1, len(tokens)):
                if tokens[j] in ("-b", "-B", "-c", "-C", "--create", "--force-create") and j + 1 < len(tokens):
                    sys.stdout.write(tokens[j+1])
                    sys.exit(0)
except Exception:
    pass
')"

if [ -n "$BRANCH" ]; then
  REPO_DIR="${CLAUDE_PROJECT_DIR:-.}"
  PROJECT_JSON="$REPO_DIR/.harness/project.json"
  PATTERN="$( [ -f "$PROJECT_JSON" ] && "$PY" -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="ignore") as f:
        data = json.load(f)
        sys.stdout.write(str(data.get("branch_pattern", "")))
except Exception:
    pass
' "$PROJECT_JSON" )"
  PATTERN=${PATTERN:-^feature/issue-[0-9]+-.+}

  if ! echo "$BRANCH" | grep -qE "$PATTERN"; then
    echo "Ветка '$BRANCH' не соответствует branch_pattern из .harness/project.json ('$PATTERN', docs/agents/git-workflow.md)." >&2
    exit 2
  fi

  # Pattern match alone only proves the branch name has the right shape - confirm the ID it
  # encodes is a real registered issue, not a made-up number. tracker-issue.py addresses the issue
  # through the project tracker resolver and skips a local tracker or a missing gh/glab.
  ID=$(echo "$BRANCH" | grep -oE '[0-9]+' | head -n1)
  if [ -n "$ID" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    "$PY" "$SCRIPT_DIR/tracker-issue.py" "$REPO_DIR" "$ID" "$BRANCH" || exit 2
  fi
fi

exit 0
