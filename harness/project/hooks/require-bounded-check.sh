#!/bin/bash
# PreToolUse(Bash): blocks an unbounded full test/quality-gate run — one of the project's own
# qa_gate_commands (.harness/project.json), or a full-suite pytest/unittest invocation with no
# node-id/specific test — unless it already goes through the bounded-output wrapper
# (skills/first-party/pvmalove/qa-gate/scripts/test_summary.py), which redacts and truncates output
# instead of dumping a raw multi-thousand-line run into the transcript. A point run of a single test
# (a pytest node-id containing "::", or a fully-qualified unittest test path) is never blocked.
# Absent .harness/project.json this hook is inactive, so it never fires on an unharnessed project.
INPUT=$(cat)

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
PROJECT_JSON="$PROJECT_DIR/.harness/project.json"
[ -f "$PROJECT_JSON" ] || exit 0

COMMAND=$(echo "$INPUT" | grep -oE '"command"[[:space:]]*:[[:space:]]*"[^"]*"')
[ -z "$COMMAND" ] && exit 0

echo "$COMMAND" | grep -q "test_summary.py" && exit 0

FULL_SUITE=0

while IFS= read -r gate_command; do
  [ -z "$gate_command" ] && continue
  ESCAPED=$(echo "$gate_command" | sed -E 's/[][\.*^$/]/\\&/g')
  echo "$COMMAND" | grep -qE "$ESCAPED" && FULL_SUITE=1
done < <(
  grep -oE '"qa_gate_commands"[[:space:]]*:[[:space:]]*\[[^]]*\]' "$PROJECT_JSON" 2>/dev/null \
    | grep -oE '"[^"]*"' | sed -E 's/^"(.*)"$/\1/'
)

if echo "$COMMAND" | grep -qE '(^|[^a-zA-Z0-9_])(pytest|python[0-9.]*[[:space:]]+-m[[:space:]]+pytest)([^a-zA-Z0-9_]|$)'; then
  echo "$COMMAND" | grep -q "::" || FULL_SUITE=1
fi

if echo "$COMMAND" | grep -qE '(^|[^a-zA-Z0-9_])python[0-9.]*[[:space:]]+-m[[:space:]]+unittest([^a-zA-Z0-9_]|$)'; then
  # A specific unittest test is a dotted path with a module, a class and a method
  # (pkg.module.Class.test_method) — at least three dots after "unittest".
  echo "$COMMAND" | grep -qE 'unittest[[:space:]]+[A-Za-z0-9_.]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+' || FULL_SUITE=1
fi

if [ "$FULL_SUITE" = "1" ]; then
  echo "Заблокировано: полносьютный тестовый/quality-gate прогон без bounded-враппера раздувает историю dispatch-сессии. Оберни вызов:" >&2
  echo "  python .harness/skills/qa-gate/scripts/test_summary.py -- bash -lc '<исходная команда>'" >&2
  echo "(PowerShell-эквивалент — skills/first-party/pvmalove/qa-gate/SKILL.md). Точечный прогон одного теста (node-id с '::', либо полный dotted-путь unittest) не блокируется." >&2
  exit 2
fi

exit 0
