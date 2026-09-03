#!/bin/bash
# PreToolUse(Bash): enforces the branch_pattern from .harness/project.json, and that the
# numeric ID it encodes is a real, registered tracker issue ("Issue First", docs/agents/git-workflow.md).
INPUT=$(cat)

if echo "$INPUT" | grep -qE '"command"[[:space:]]*:[[:space:]]*"[^"]*git (checkout -b|switch -c)'; then
  RAW=$(echo "$INPUT" | grep -oE '"command"[[:space:]]*:[[:space:]]*"[^"]*"')
  BRANCH=$(echo "$RAW" | sed -E 's/.*git (checkout -b|switch -c)[[:space:]]+([^ "]+).*/\2/')

  REPO_DIR="${CLAUDE_PROJECT_DIR:-.}"
  PROJECT_JSON="$REPO_DIR/.harness/project.json"
  PATTERN=$(grep -oE '"branch_pattern"[[:space:]]*:[[:space:]]*"[^"]*"' "$PROJECT_JSON" 2>/dev/null | sed -E 's/.*"branch_pattern"[[:space:]]*:[[:space:]]*"(.*)"/\1/')
  PATTERN=${PATTERN:-^feature/issue-[0-9]+-.+}

  if ! echo "$BRANCH" | grep -qE "$PATTERN"; then
    echo "Ветка '$BRANCH' не соответствует branch_pattern из .harness/project.json ('$PATTERN', docs/agents/git-workflow.md)." >&2
    exit 2
  fi

  # Pattern match alone only proves the branch name has the right shape - confirm the ID it
  # encodes is a real registered issue, not a made-up number. Only checkable against GitHub/GitLab;
  # the local-markdown tracker has no global issue numbering, so there's nothing to verify there.
  ID=$(echo "$BRANCH" | grep -oE '[0-9]+' | head -n1)
  if [ -n "$ID" ]; then
    if command -v gh >/dev/null 2>&1 && git -C "$REPO_DIR" remote -v 2>/dev/null | grep -q 'github\.com'; then
      if ! (cd "$REPO_DIR" && gh issue view "$ID" >/dev/null 2>&1); then
        echo "Issue First (docs/agents/git-workflow.md): issue #$ID из имени ветки '$BRANCH' не найден в трекере — сначала заведи его ('gh issue create' или /to-spec, /to-tickets)." >&2
        exit 2
      fi
    elif command -v glab >/dev/null 2>&1 && git -C "$REPO_DIR" remote -v 2>/dev/null | grep -q 'gitlab\.'; then
      if ! (cd "$REPO_DIR" && glab issue view "$ID" >/dev/null 2>&1); then
        echo "Issue First (docs/agents/git-workflow.md): issue #$ID из имени ветки '$BRANCH' не найден в трекере — сначала заведи его ('glab issue create' или /to-spec, /to-tickets)." >&2
        exit 2
      fi
    fi
  fi
fi

exit 0
