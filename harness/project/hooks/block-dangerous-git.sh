#!/bin/bash
# PreToolUse(Bash): blocks unconditionally destructive git commands, adapted from the upstream
# mattpocock/skills "git-guardrails-claude-code" skill (skills/misc/, not part of the vendored
# mattpocock-suite capability). Deliberately does NOT block `git push` outright, unlike upstream —
# docs/agents/git-workflow.md requires pushing feature branches; push-to-master is already covered
# by block-direct-master.sh.
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | grep -oE '"command"[[:space:]]*:[[:space:]]*"[^"]*"')

for pattern in 'git reset --hard' 'git clean -f' 'git branch -D' 'git checkout \.' 'git restore \.'; do
  if echo "$COMMAND" | grep -qE "$pattern"; then
    echo "Заблокировано: команда матчит деструктивный паттерн '$pattern' — такие операции требуют явного запроса пользователя." >&2
    exit 2
  fi
done

exit 0
