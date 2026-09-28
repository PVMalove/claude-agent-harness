#!/bin/bash
# PreToolUse(Write|Edit): task artifacts belong in docs/tasks/, while one-shot PR metadata belongs
# only in .harness/.sandboxes/pr_body/ (docs/agents/artifacts.md and git-workflow.md §1).
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
        if not resolved.is_relative_to(proj):
            raise SystemExit(2)
        value = resolved.relative_to(proj).as_posix()
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(2)

sys.stdout.write(value.replace("\\", "/"))
')"
if [ $? -ne 0 ]; then
  echo "Невозможно проверить путь Write/Edit: hook payload не содержит tool_input.file_path." >&2
  exit 2
fi

BASE_NAME="${FILE_PATH##*/}"
if printf '%s' "$BASE_NAME" | grep -qiE 'pr-body|pr-comment|issue-comment'; then
  if printf '%s' "$FILE_PATH" | grep -qiE '^\.harness/\.sandboxes/pr_body/[^/]+$'; then
    exit 0
  fi
  echo "git-workflow.md §1: тело PR/комментария пишется только в .harness/.sandboxes/pr_body/ (например, .harness/.sandboxes/pr_body/pr-body-<issue>-<slug>.md), не в docs/tasks/; удали его после успешного gh/glab: $FILE_PATH" >&2
  exit 2
fi

if ! printf '%s' "$FILE_PATH" | grep -qiE '(^|/)\.harness/\.sandboxes/(scratch|pr_body)(/|$)' && printf '%s' "$FILE_PATH" | grep -qiE '(^|/)\.harness/\.sandboxes/(cache|logs|runs|reports|worktrees)(/|$)'; then
  exit 0
fi

if printf '%s' "$FILE_PATH" | grep -qiE '(^|/)\.harness/\.sandboxes/pr_body/'; then
  echo "git-workflow.md §1: .harness/.sandboxes/pr_body/ разрешён только для тела PR/issue-комментария — имя файла должно содержать pr-body, pr-comment или issue-comment (например, .harness/.sandboxes/pr_body/issue-comment-<issue>-<slug>.md); любой другой файл в этой директории отклоняется. Для прочих черновиков используй docs/tasks/: $FILE_PATH" >&2
  exit 2
fi

if ! printf '%s' "$FILE_PATH" | grep -qiE '(^|/)(\.scratch|\.claude|\.agents)/(tmp|scratch|temp)(/|$)' && printf '%s' "$FILE_PATH" | grep -qiE '(AppData.(Local|Roaming).Temp|/tmp/|/scratchpad/)'; then
  echo "artifacts.md: спецификации и скретчпады пишем в docs/tasks/, не в системный temp: $FILE_PATH" >&2
  exit 2
fi

exit 0
