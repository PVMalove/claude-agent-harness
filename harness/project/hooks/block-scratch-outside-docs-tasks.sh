#!/bin/bash
# PreToolUse(Write|Edit): task artifacts belong in docs/tasks/, while one-shot PR metadata belongs
# only in the repository scratch directory .claude/tmp/ (docs/agents/artifacts.md and git-workflow.md §1).
INPUT=$(cat)
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить путь Write/Edit: Python 3.9+ не найден." >&2
  exit 2
fi

FILE_PATH="$(printf '%s' "$INPUT" | "$PY" -c '
import json
import sys

try:
    value = json.load(sys.stdin)["tool_input"]["file_path"]
except (json.JSONDecodeError, KeyError, TypeError):
    raise SystemExit(1)
if not isinstance(value, str):
    raise SystemExit(1)
sys.stdout.write(value.replace("\\", "/"))
')"
if [ $? -ne 0 ]; then
  echo "Невозможно проверить путь Write/Edit: hook payload не содержит tool_input.file_path." >&2
  exit 2
fi

if echo "$FILE_PATH" | grep -qiE 'pr-body|pr-comment|issue-comment'; then
  if printf '%s' "$FILE_PATH" | grep -qiE '(^|/)\.claude/tmp/'; then
    exit 0
  fi
  echo "git-workflow.md §1: тело PR/комментария пишется только в .claude/tmp/ (например, .claude/tmp/pr-body-<issue>-<slug>.md), не в docs/tasks/; удали его после успешного gh/glab: $FILE_PATH" >&2
  exit 2
fi

if printf '%s' "$FILE_PATH" | grep -qiE '(AppData.(Local|Roaming).Temp|/tmp/|/scratchpad/)'; then
  echo "artifacts.md: спецификации и скретчпады пишем в docs/tasks/, не в системный temp: $FILE_PATH" >&2
  exit 2
fi

exit 0
