#!/bin/bash
# PreToolUse(Bash): blocks unconditionally destructive git commands, adapted from the upstream
# mattpocock/skills "git-guardrails-claude-code" skill (skills/misc/, not part of the vendored
# mattpocock-suite capability). Deliberately does NOT block `git push` outright, unlike upstream —
# docs/agents/git-workflow.md requires pushing issue branches; push-to-base/integration is already
# covered by block-direct-master.sh.
INPUT=$(cat)
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить опасные Git-команды: Python 3.9+ не найден." >&2
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

for pattern in 'git reset --hard' 'git clean -f' 'git branch -D' 'git checkout \.' 'git restore \.'; do
  if printf '%s\n' "$COMMAND" | grep -qE "$pattern"; then
    echo "Заблокировано: команда матчит деструктивный паттерн '$pattern' — такие операции требуют явного запроса пользователя." >&2
    exit 2
  fi
done

exit 0
