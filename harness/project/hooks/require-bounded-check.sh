#!/bin/bash
# PreToolUse(Bash): blocks an unbounded full test/quality-gate run — one of the project's own
# qa_gate_commands (.harness/project.json), or a full-suite pytest/unittest invocation with no
# node-id/specific test — unless it already goes through the bounded-output wrapper
# (skills/first-party/pvmalove/qa-gate/scripts/test_summary.py), which redacts and truncates output
# instead of dumping a raw multi-thousand-line run into the transcript. A point run of a single test
# (a pytest node-id containing "::", or a fully-qualified unittest test path) is never blocked.
# The command is split into simple commands: a read command (cat, sed, grep, ...), heredoc text
# and comments never fire, so a name they merely mention is not mistaken for a run.
# Absent .harness/project.json this hook is inactive, so it never fires on an unharnessed project.
INPUT=$(cat)

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
PROJECT_JSON="$PROJECT_DIR/.harness/project.json"
[ -f "$PROJECT_JSON" ] || exit 0

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить ограниченный прогон проверок: Python 3.9+ не найден." >&2
  exit 2
fi

printf '%s' "$INPUT" | PROJECT_JSON="$PROJECT_JSON" "$PY" -c '
import io
import json
import os
import re
import shlex
import sys

SEPARATORS = "();<>|&\n"
READERS = {"cat", "diff", "egrep", "fgrep", "grep", "head", "jq", "ls", "rg", "sed", "tail", "wc"}
SHELLS = {"bash", "dash", "sh", "zsh"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*([\x27\"]?)([A-Za-z_][A-Za-z0-9_.-]*)\2")
PYTEST = re.compile(r"(?<![A-Za-z0-9_])pytest(?![A-Za-z0-9_])")
UNITTEST = re.compile(r"(?<![A-Za-z0-9_])python[0-9.]*\s+-m\s+unittest(?![A-Za-z0-9_])")
UNITTEST_TEST = re.compile(
    r"unittest\s+[A-Za-z0-9_.]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+"
)


class Source(io.StringIO):
    """Ends a comment before its newline, so the newline still separates commands."""

    def readline(self, size=-1):
        line = super().readline(size)
        if line.endswith("\n"):
            self.seek(self.tell() - 1)
            return line[:-1]
        return line


def without_heredocs(text):
    """Drop heredoc bodies, except one fed to a shell, which runs as commands."""
    kept, pending = [], []
    for line in text.split("\n"):
        if pending:
            strip_tabs, word = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == word:
                pending.pop(0)
            continue
        kept.append(line)
        pending = [
            (match.group(1), match.group(3))
            for match in HEREDOC.finditer(line)
            if not SHELLS & {os.path.basename(word) for word in line[: match.start()].split()}
        ]
    return "\n".join(kept)


def simple_commands(text):
    """Words of each simple command, without leading VAR=value assignments."""
    text = without_heredocs(text)
    lexer = shlex.shlex(Source(text.replace("\\\n", " ")), posix=True, punctuation_chars=SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    commands, words = [], []
    try:
        for token in lexer:
            if token and set(token) <= set(SEPARATORS):
                commands.append(words)
                words = []
            elif words or not ASSIGNMENT.fullmatch(token):
                words.append(token)
    except ValueError:
        # An unbalanced quote defeats the parser: fall back to plain words per line.
        return [line.split() for line in text.split("\n") if line.split()]
    commands.append(words)
    return [words for words in commands if words]


def reads_only(words):
    return os.path.basename(words[0]) in READERS and not any(
        "$(" in word or "`" in word for word in words
    )


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

runnable = [" ".join(words) for words in simple_commands(command) if not reads_only(words)]
for gate in gates:
    parts = [" ".join(words) for words in simple_commands(gate)] if isinstance(gate, str) else []
    if parts and all(any(part in text for text in runnable) for part in parts):
        raise SystemExit(2)
for text in runnable:
    if PYTEST.search(text) and "::" not in text:
        raise SystemExit(2)
    if UNITTEST.search(text) and not UNITTEST_TEST.search(text):
        raise SystemExit(2)
'
if [ $? -eq 2 ]; then
  echo "Заблокировано: полносьютный тестовый/quality-gate прогон без bounded-враппера раздувает историю dispatch-сессии. Оберни вызов:" >&2
  echo "  python .harness/skills/qa-gate/scripts/test_summary.py -- bash -lc '<исходная команда>'" >&2
  echo "(PowerShell-эквивалент — skills/first-party/pvmalove/qa-gate/SKILL.md). Точечный прогон одного теста (node-id с '::', либо полный dotted-путь unittest) не блокируется." >&2
  exit 2
fi

exit 0
