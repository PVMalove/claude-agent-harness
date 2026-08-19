#!/bin/bash
# PreToolUse(Bash): "Zero Auto-Merge" from docs/agents/git-workflow.md, enforced deterministically.
INPUT=$(cat)

if echo "$INPUT" | grep -qE '"command"[[:space:]]*:[[:space:]]*"[^"]*gh pr merge'; then
  echo "Zero Auto-Merge: 'gh pr merge' запрещён агенту — мердж в основную ветку выполняет только разработчик вручную (docs/agents/git-workflow.md)." >&2
  exit 2
fi

exit 0
