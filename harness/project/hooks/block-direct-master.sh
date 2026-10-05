#!/bin/bash
# PreToolUse(Bash): "Zero Direct Commits" from docs/agents/git-workflow.md, enforced deterministically.
# direct_commits.py checks each git commit/push in the checkout it runs in (git -C/--work-tree/
# --git-dir, a preceding cd, the payload cwd, then CLAUDE_PROJECT_DIR) and each push target ref.
# Only its exit 0 with the word `allow` lets the command run, so a missing interpreter, any
# nonzero status or a partial copy of the helper blocks (fail closed).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить запрет прямого коммита: Python 3.9+ не найден." >&2
  exit 2
fi

VERDICT="$("$PY" "$SCRIPT_DIR/direct_commits.py")"
CODE=$?
if [ "$CODE" -eq 0 ] && [ "$VERDICT" = "allow" ]; then
  exit 0
fi
if [ "$CODE" -ne 2 ]; then
  echo "Zero Direct Commits: direct_commits.py не дал решения (код $CODE) — команда заблокирована." >&2
fi
exit 2
