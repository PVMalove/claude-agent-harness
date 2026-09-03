#!/bin/bash
# PreToolUse(Write|Edit): docs/agents/artifacts.md forbids saving specs/scratchpads under system temp dirs.
# Exception: docs/agents/git-workflow.md §1 ("Body via File, Not Inline") explicitly requires a PR
# body or comment to be written to a temp file first and deleted once `gh`/`glab` succeeds - that's
# a one-shot file, not the durable, folder-per-ticket spec/scratchpad artifacts.md is about.
INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | grep -oE '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"file_path"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')

if echo "$FILE_PATH" | grep -qiE 'pr-body|pr-comment|issue-comment'; then
  if echo "$FILE_PATH" | grep -qiE '(^|[\\/])docs[\\/]'; then
    echo "git-workflow.md §1: тело PR/комментария — одноразовый temp-файл, не docs/: пиши во временный каталог (scratchpad) и удали после успешного gh/glab: $FILE_PATH" >&2
    exit 2
  fi
  exit 0
fi

if echo "$FILE_PATH" | grep -qiE '(AppData.(Local|Roaming).Temp|[\\/]tmp[\\/]|[\\/]scratchpad[\\/])'; then
  echo "artifacts.md: спецификации и скретчпады пишем в docs/tasks/, не в системный temp: $FILE_PATH" >&2
  exit 2
fi

exit 0
