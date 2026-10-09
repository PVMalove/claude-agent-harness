#!/bin/bash
# PreToolUse(Bash): blocks an unbounded full test/quality-gate run — one of the project's own
# qa_gate_commands (.harness/project.json), or a full-suite pytest/unittest invocation with no
# node-id/specific test — unless it already goes through the bounded-output wrapper
# (skills/first-party/pvmalove/qa-gate/scripts/test_summary.py), which redacts and truncates output
# instead of dumping a raw multi-thousand-line run into the transcript. A point run of a single test
# (a pytest node-id containing "::", or a fully-qualified unittest test path) is never blocked.
# The shared shell parser keeps argv boundaries: literal text arguments, comments and inert
# heredoc bodies do not fire; executable substitutions and nested shell commands still do.
# Absent .harness/project.json this hook is inactive, so it never fires on an unharnessed project.
INPUT=$(cat)

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
PROJECT_JSON="$PROJECT_DIR/.harness/project.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$PROJECT_JSON" ] || exit 0

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить ограниченный прогон проверок: Python 3.10+ не найден." >&2
  exit 2
fi

printf '%s' "$INPUT" | PROJECT_JSON="$PROJECT_JSON" HOOK_DIR="$SCRIPT_DIR" "$PY" -c '
import json
import os
import re
import sys

# Share the shell parser with the other project hooks; do not write runtime bytecode.
sys.dont_write_bytecode = True
sys.path.insert(0, os.environ["HOOK_DIR"])
import pr_commands

PYTHON = re.compile(r"python[0-9.]*")
PYTEST = re.compile(r"(?<![A-Za-z0-9_])pytest(?![A-Za-z0-9_])")
UNITTEST = re.compile(r"(?<![A-Za-z0-9_])python[0-9.]*\s+-m\s+unittest(?![A-Za-z0-9_])")
UNITTEST_TEST = re.compile(
    r"unittest\s+[A-Za-z0-9_.]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+"
)


def candidates(argv):
    """Command argv, including programs started by wrappers, but never split text arguments."""
    for start in pr_commands.positions(argv):
        part = [pr_commands.program(argv[start]), *argv[start + 1 :]]
        yield part
        if (
            part[0] in pr_commands.PRINTERS
            or tuple(part[:2]) in pr_commands.TEXT_COMMANDS
            or tuple(part[:3]) in pr_commands.TEXT_COMMANDS
        ):
            break


try:
    command = json.load(sys.stdin)["tool_input"]["command"]
except (json.JSONDecodeError, KeyError, TypeError):
    raise SystemExit(0)
if not isinstance(command, str) or "test_summary.py" in command:
    raise SystemExit(0)
try:
    with open(os.environ["PROJECT_JSON"], encoding="utf-8") as source:
        gates = json.load(source).get("qa_gate_commands", [])
except (OSError, ValueError, AttributeError):
    gates = []

parsed = pr_commands.parse(command)
runnable = [part for argv in parsed.commands for part in candidates(argv)]
for gate in gates:
    parts = pr_commands.parse(gate).commands if isinstance(gate, str) else []
    if parts and all(
        any(
            text[: len(part)] == [pr_commands.program(part[0]), *part[1:]]
            for text in runnable
        )
        or any(" ".join(part) in fragment for fragment in parsed.opaque)
        for part in parts
    ):
        raise SystemExit(2)
for argv in runnable:
    text = " ".join(argv)
    pytest = argv[0] == "pytest" or (
        PYTHON.fullmatch(argv[0]) and argv[1:3] == ["-m", "pytest"]
    )
    if pytest and "::" not in text:
        raise SystemExit(2)
    if (
        PYTHON.fullmatch(argv[0])
        and argv[1:3] == ["-m", "unittest"]
        and not UNITTEST_TEST.search(text)
    ):
        raise SystemExit(2)
# Interpreter code, an incomplete shell command or dynamic shell stdin cannot be classified
# as literal text: keep the conservative full-run check for these opaque fragments.
for text in parsed.opaque:
    if PYTEST.search(text) and "::" not in text:
        raise SystemExit(2)
    if UNITTEST.search(text) and not UNITTEST_TEST.search(text):
        raise SystemExit(2)
'
STATUS=$?
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 2 ]; then
  echo "Невозможно проверить ограниченный прогон: ошибка Python или общего shell-парсера pr_commands.py." >&2
  exit 2
fi
if [ "$STATUS" -eq 2 ]; then
  echo "Заблокировано: полносьютный тестовый/quality-gate прогон без bounded-враппера раздувает историю dispatch-сессии. Оберни вызов:" >&2
  echo "  python .harness/skills/qa-gate/scripts/test_summary.py -- bash -lc '<исходная команда>'" >&2
  echo "(PowerShell-эквивалент — .harness/skills/qa-gate/SKILL.md). Точечный прогон одного теста (node-id с '::', либо полный dotted-путь unittest) не блокируется." >&2
  exit 2
fi

exit 0
