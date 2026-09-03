#!/bin/bash
# PreToolUse(Bash): keeps commit messages and PR metadata project-only.
INPUT=$(cat)
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить публичные Git-метаданные: Python 3.9+ не найден." >&2
  exit 2
fi
# Parse the hook JSON properly instead of grepping a "command" field regex, so an embedded/escaped
# quote inside a multi-line commit message never truncates matching — and so this only inspects
# the actual command text, not unrelated payload fields (cwd, transcript_path, session_id, ...),
# which always contain this repo's own name ("claude-agent-harness") and would otherwise
# self-trigger FORBIDDEN on every single invocation regardless of the command being run.
COMMAND="$(printf '%s' "$INPUT" | "$PY" -c '
import json, sys
try:
    data = json.load(sys.stdin)
    command = data["tool_input"]["command"]
except (json.JSONDecodeError, KeyError, TypeError):
    sys.exit(1)
if not isinstance(command, str):
    sys.exit(1)
sys.stdout.write(command)
')"
if [ $? -ne 0 ]; then
  echo "Невозможно проверить публичные Git-метаданные: hook payload не содержит tool_input.command." >&2
  exit 2
fi

FORBIDDEN='claude|claude\.ai/code/session|openai|chatgpt|gpt[-_ ]?[0-9]|copilot|gemini|codex|co-authored[- ]by|ai[-_ ]?(agent|assistant|generated)'

is_commit=0
is_pr=0
is_push=0
printf '%s\n' "$COMMAND" | grep -qiE 'git[[:space:]]+commit' && is_commit=1
printf '%s\n' "$COMMAND" | grep -qiE '(gh[[:space:]]+pr|glab[[:space:]]+mr)[[:space:]]+(create|edit)' && is_pr=1
printf '%s\n' "$COMMAND" | grep -qiE 'git[[:space:]]+push' && is_push=1

[ "$is_commit" -eq 0 ] && [ "$is_pr" -eq 0 ] && [ "$is_push" -eq 0 ] && exit 0

validate_command_metadata() {
  printf '%s' "$COMMAND" | FORBIDDEN="$FORBIDDEN" "$PY" -c '
import os
import re
import shlex
import sys


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(2)


try:
    lexer = shlex.shlex(sys.stdin.read(), posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    tokens = list(lexer)
except ValueError:
    fail("Невозможно проверить публичные Git-метаданные: команда содержит незакрытую кавычку.")

forbidden = re.compile(os.environ["FORBIDDEN"], re.IGNORECASE)


def has_sequence(*parts):
    return any(tokens[index : index + len(parts)] == list(parts) for index in range(len(tokens)))


def values(options):
    result = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in options:
            if index + 1 >= len(tokens):
                fail("Невозможно проверить публичные Git-метаданные: у параметра сообщения нет значения.")
            result.append(tokens[index + 1])
            index += 2
            continue
        for option in options:
            if option.startswith("--") and token.startswith(option + "="):
                result.append(token[len(option) + 1 :])
                break
        else:
            index += 1
            continue
        index += 1
    return result


def check_text(text):
    if forbidden.search(text):
        fail("Публичные Git-метаданные должны содержать только сведения об изменении проекта: автоматическая атрибуция, имена моделей и session URL запрещены.")


def check_files(paths):
    for path in paths:
        if not os.path.isfile(path):
            fail("Невозможно проверить публичные Git-метаданные: файл сообщения должен быть доступен по literal-пути.")
        try:
            with open(path, encoding="utf-8") as source:
                check_text(source.read())
        except OSError:
            fail("Невозможно прочитать файл публичных Git-метаданных.")


if has_sequence("git", "commit"):
    check_texts = values({"-m", "--message", "--trailer"})
    check_files(values({"-F", "--file"}))
    for text in check_texts:
        check_text(text)

if (has_sequence("gh", "pr", "create") or has_sequence("gh", "pr", "edit") or
        has_sequence("glab", "mr", "create") or has_sequence("glab", "mr", "edit")):
    check_files(values({"--body-file", "--description-file"}))
    for text in values({"--title", "--body", "--description"}):
        check_text(text)
'
}

validate_command_metadata
if [ $? -ne 0 ]; then
  exit 2
fi

if [ "$is_push" -eq 1 ] || [ "$is_pr" -eq 1 ]; then
  LOCAL_MESSAGES="$(git -C "${CLAUDE_PROJECT_DIR:-.}" log --format=%B --no-merges --not --remotes 2>/dev/null)"
  if printf '%s\n' "$LOCAL_MESSAGES" | grep -qiE "$FORBIDDEN"; then
    echo "Публикация заблокирована: непереданный commit message содержит запрещённую автоматическую атрибуцию или ссылку на сессию." >&2
    exit 2
  fi
fi

exit 0
