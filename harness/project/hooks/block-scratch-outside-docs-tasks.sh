#!/bin/bash
# PreToolUse(Write|Edit): task artifacts belong in docs/tasks/, while one-shot PR metadata belongs
# only in the repository scratch directory .harness/.sandboxes/scratch/tmp/ (docs/agents/artifacts.md and git-workflow.md §1).
INPUT=$(cat)
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить путь Write/Edit: Python 3.9+ не найден." >&2
  exit 2
fi

FILE_PATH="$(printf '%s' "$INPUT" | "$PY" -c '
import json
import os
import sys
from pathlib import Path

try:
    value = json.load(sys.stdin)["tool_input"]["file_path"]
except (json.JSONDecodeError, KeyError, TypeError):
    raise SystemExit(1)
if not isinstance(value, str):
    raise SystemExit(1)

project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
if project_dir:
    try:
        p = Path(value)
        proj = Path(project_dir).resolve()
        resolved = p.resolve()
        if resolved.is_relative_to(proj):
            value = resolved.relative_to(proj).as_posix()
    except Exception:
        pass

sys.stdout.write(value.replace("\\", "/"))
')"
if [ $? -ne 0 ]; then
  echo "Невозможно проверить путь Write/Edit: hook payload не содержит tool_input.file_path." >&2
  exit 2
fi

if echo "$FILE_PATH" | grep -qiE 'pr-body|pr-comment|issue-comment'; then
  if printf '%s' "$FILE_PATH" | grep -qiE '(^|/)\.harness/\.sandboxes/scratch/tmp/'; then
    exit 0
  fi
  echo "git-workflow.md §1: тело PR/комментария пишется только в .harness/.sandboxes/scratch/tmp/ (например, .harness/.sandboxes/scratch/tmp/pr-body-<issue>-<slug>.md), не в docs/tasks/; удали его после успешного gh/glab: $FILE_PATH" >&2
  exit 2
fi

if ! printf '%s' "$FILE_PATH" | grep -qiE '(^|/)\.harness/\.sandboxes/scratch(/|$)' && printf '%s' "$FILE_PATH" | grep -qiE '(^|/)\.harness/\.sandboxes/(cache|logs|runs|reports|worktrees)(/|$)'; then
  exit 0
fi

if printf '%s' "$FILE_PATH" | grep -qiE '(^|/)\.harness/\.sandboxes/scratch/tmp/'; then
  echo "git-workflow.md §1: .harness/.sandboxes/scratch/tmp/ разрешён только для тела PR/issue-комментария — имя файла должно содержать pr-body, pr-comment или issue-comment (например, .harness/.sandboxes/scratch/tmp/issue-comment-<issue>-<slug>.md); любой другой скретч-файл в этой директории отклоняется, даже если директория верная. Для прочих скретч-файлов используй docs/tasks/ или путь вне репозитория: $FILE_PATH" >&2
  exit 2
fi

if ! printf '%s' "$FILE_PATH" | grep -qiE '(^|/)(\.scratch|\.claude|\.agents)/(tmp|scratch|temp)(/|$)' && printf '%s' "$FILE_PATH" | grep -qiE '(AppData.(Local|Roaming).Temp|/tmp/|/scratchpad/)'; then
  echo "artifacts.md: спецификации и скретчпады пишем в docs/tasks/, не в системный temp: $FILE_PATH" >&2
  exit 2
fi

exit 0
