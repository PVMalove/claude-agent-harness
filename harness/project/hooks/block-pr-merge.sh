#!/bin/bash
# PreToolUse(Bash): "Zero Auto-Merge" from docs/agents/git-workflow.md, enforced deterministically.
# pr_commands.py decides on the tokens of tool_input.command: `gh pr merge` and `glab mr merge`/
# `accept` are blocked wherever bash would run them; a mention inside one quoted argument is
# not. Any failure to decide blocks (fail closed).
INPUT=$(cat)

# Fast path without Python: a payload that never mentions merge/accept cannot request one.
printf '%s' "$INPUT" | grep -qE 'merge|accept' || exit 0

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Zero Auto-Merge: Python 3 не найден, команду с merge/accept нельзя проверить." >&2
  exit 2
fi

if ! printf '%s' "$INPUT" | "$PY" "$SCRIPT_DIR/pr_commands.py" merge; then
  echo "Zero Auto-Merge: 'gh pr merge' и 'glab mr merge'/'accept' запрещены агенту — мердж в основную ветку выполняет только разработчик вручную (docs/agents/git-workflow.md)." >&2
  exit 2
fi

exit 0
