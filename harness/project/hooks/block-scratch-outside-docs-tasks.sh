#!/bin/bash
# PreToolUse(Write|Edit): task artifacts belong in docs/tasks/, while one-shot PR metadata belongs
# only in .harness/.sandboxes/pr_body/ (docs/agents/artifacts.md and git-workflow.md §1).
# Verdict envelope (hook_response.py): reruns this script, answers with the JSON verdict and the
# exit code of the hook policy. Without Python or the helper the script below decides alone.
if [ -z "${HARNESS_HOOK_ENVELOPE:-}" ]; then
  ENVELOPE_DIR="$(cd "$(dirname "$0")" && pwd)"
  ENVELOPE_PY="$(command -v python3 || command -v python)"
  if [ -n "$ENVELOPE_PY" ] && [ -f "$ENVELOPE_DIR/hook_response.py" ]; then
    exec "$ENVELOPE_PY" "$ENVELOPE_DIR/hook_response.py" run block-scratch-outside-docs-tasks "${BASH:-bash}" "$0"
  fi
fi
INPUT=$(cat)
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить путь Write/Edit: Python 3.9+ не найден." >&2
  exit 2
fi

FILE_PATH="$(printf '%s' "$INPUT" | "$PY" -c '
import json
import os
import re
import sys
from pathlib import Path

try:
    payload = json.load(sys.stdin)
    value = payload["tool_input"]["file_path"]
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
            sys.stdout.write(str(resolved))
            # The runtime memory directory, <config>/projects/<slug>/memory/, sits outside
            # every project by design.
            config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
            config = config.expanduser().resolve()
            if resolved.is_relative_to(config):
                parts = resolved.relative_to(config).parts
                if len(parts) > 3 and parts[0] == "projects" and parts[2] == "memory":
                    raise SystemExit(3)
            raise SystemExit(2)
        value = resolved.relative_to(proj).as_posix()
        parts = resolved.relative_to(proj).parts
        if parts[:4] == (".harness", ".sandboxes", "scratch", "sessions"):
            session = payload.get("session_id")
            if (isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session)
                    and len(parts) >= 7 and parts[4] == session and parts[5] == "scratchpad"):
                sys.stdout.write(value)
                raise SystemExit(5)
            raise SystemExit(6)
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(4)

sys.stdout.write(value.replace("\\", "/"))
')"
# Exit codes: 0 — project-relative path on stdout; 2 — path outside the project (on stdout);
# 3 — runtime memory; 5 — bound project session scratchpad; 6 — wrong session;
# 4 — path cannot be resolved; 1 — no file_path in the payload.
case $? in
  0) ;;
  2)
    echo "artifacts.md: путь Write/Edit вне проекта ${CLAUDE_PROJECT_DIR} — артефакты задач пишем в docs/tasks/ внутри проекта: $FILE_PATH" >&2
    exit 2
    ;;
  3) exit 0 ;;
  5) BOUND_SESSION_SCRATCHPAD=1 ;;
  6)
    echo "artifacts.md: проектный scratchpad должен принадлежать текущему session_id runtime; для обычных черновиков используй docs/tasks/." >&2
    exit 2
    ;;
  4)
    echo "Невозможно проверить путь Write/Edit: путь не удаётся разрешить относительно проекта." >&2
    exit 2
    ;;
  *)
    echo "Невозможно проверить путь Write/Edit: hook payload не содержит tool_input.file_path." >&2
    exit 2
    ;;
esac

BASE_NAME="${FILE_PATH##*/}"
if printf '%s' "$BASE_NAME" | grep -qiE 'pr-body|pr-comment|issue-comment'; then
  if printf '%s' "$FILE_PATH" | grep -qiE '^\.harness/\.sandboxes/pr_body/[^/]+$'; then
    exit 0
  fi
  echo "git-workflow.md §1: тело PR/комментария пишется только в .harness/.sandboxes/pr_body/ (например, .harness/.sandboxes/pr_body/pr-body-<issue>-<slug>.md), не в docs/tasks/; удали его после успешного gh/glab: $FILE_PATH" >&2
  exit 2
fi

if [ "${BOUND_SESSION_SCRATCHPAD:-}" = "1" ]; then
  exit 0
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
