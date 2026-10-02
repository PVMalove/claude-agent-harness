#!/bin/bash
# PreToolUse(Bash): "Zero Auto-Merge" from docs/agents/git-workflow.md, enforced deterministically.
# pr_commands.py blocks `gh pr merge` and `glab mr merge`/`accept` text unless every command bash
# would run is an allowlisted inert one (echo, cat, grep, git commit, gh pr comment, ...).
# Python decides every call; only its exit 0 with the word `allow` lets the command run, so a
# missing interpreter, any nonzero status or a partial copy of the helper blocks (fail closed).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Zero Auto-Merge: Python 3 не найден, команду нельзя проверить." >&2
  exit 2
fi

if VERDICT="$("$PY" "$SCRIPT_DIR/pr_commands.py" merge)" && [ "$VERDICT" = "allow" ]; then
  exit 0
fi
echo "Zero Auto-Merge: 'gh pr merge' и 'glab mr merge'/'accept' запрещены агенту — мердж в основную ветку выполняет только разработчик вручную (docs/agents/git-workflow.md). Упоминание merge допускается только в командах echo/printf/cat/grep/head/tail/wc, git commit и текстовых подкомандах gh/glab без \$, обратных кавычек, комментариев и скобок." >&2
exit 2
