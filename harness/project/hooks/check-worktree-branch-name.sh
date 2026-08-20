#!/bin/bash
# PreToolUse(EnterWorktree): same branch_pattern convention as check-branch-name.sh,
# extended to the native worktree tool (docs/agents/worktrees.md).
INPUT=$(cat)

# Entering an existing worktree by path, or letting the tool assign a random name, isn't ticket
# branch creation - only check when a name was explicitly given.
NAME=$(echo "$INPUT" | grep -oE '"name"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')

if [ -n "$NAME" ]; then
  REPO_DIR="${CLAUDE_PROJECT_DIR:-.}"
  PROJECT_JSON="$REPO_DIR/.harness/project.json"
  PATTERN=$(grep -oE '"branch_pattern"[[:space:]]*:[[:space:]]*"[^"]*"' "$PROJECT_JSON" 2>/dev/null | sed -E 's/.*"branch_pattern"[[:space:]]*:[[:space:]]*"(.*)"/\1/')
  PATTERN=${PATTERN:-^feature/issue-[0-9]+-.+}

  if ! echo "$NAME" | grep -qE "$PATTERN"; then
    echo "Имя worktree/ветки '$NAME' не соответствует branch_pattern из .harness/project.json ('$PATTERN', docs/agents/git-workflow.md, docs/agents/worktrees.md)." >&2
    exit 2
  fi

  # Pattern match alone only proves the name has the right shape - confirm the ID it encodes is a
  # real registered issue, not a made-up number ("Issue First", docs/agents/git-workflow.md). Only
  # checkable against GitHub/GitLab; the local-markdown tracker has no global issue numbering.
  ID=$(echo "$NAME" | grep -oE '[0-9]+' | head -n1)
  if [ -n "$ID" ]; then
    if command -v gh >/dev/null 2>&1 && git -C "$REPO_DIR" remote -v 2>/dev/null | grep -q 'github\.com'; then
      if ! (cd "$REPO_DIR" && gh issue view "$ID" >/dev/null 2>&1); then
        echo "Issue First (docs/agents/git-workflow.md): issue #$ID из имени '$NAME' не найден в трекере — сначала заведи его ('gh issue create' или /to-spec, /to-tickets)." >&2
        exit 2
      fi
    elif command -v glab >/dev/null 2>&1 && git -C "$REPO_DIR" remote -v 2>/dev/null | grep -q 'gitlab\.'; then
      if ! (cd "$REPO_DIR" && glab issue view "$ID" >/dev/null 2>&1); then
        echo "Issue First (docs/agents/git-workflow.md): issue #$ID из имени '$NAME' не найден в трекере — сначала заведи его ('glab issue create' или /to-spec, /to-tickets)." >&2
        exit 2
      fi
    fi
  fi
fi

exit 0
