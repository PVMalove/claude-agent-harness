#!/bin/bash
# PreToolUse(Write|Edit): task artifacts belong in docs/tasks/, while one-shot PR metadata belongs
# only in the repository scratch directory .claude/tmp/ (docs/agents/artifacts.md and git-workflow.md §1).
INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | grep -oE '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"file_path"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')

if echo "$FILE_PATH" | grep -qiE 'pr-body|pr-comment|issue-comment'; then
  if echo "$FILE_PATH" | grep -qiE '(^|[\\/])\.claude[\\/]tmp[\\/]'; then
    exit 0
  fi
  echo "git-workflow.md §1: тело PR/комментария пишется только в .claude/tmp/ (например, .claude/tmp/pr-body-<issue>-<slug>.md), не в docs/tasks/; удали его после успешного gh/glab: $FILE_PATH" >&2
  exit 2
fi

if echo "$FILE_PATH" | grep -qiE '(AppData.(Local|Roaming).Temp|[\\/]tmp[\\/]|[\\/]scratchpad[\\/])'; then
  echo "artifacts.md: спецификации и скретчпады пишем в docs/tasks/, не в системный temp: $FILE_PATH" >&2
  exit 2
fi

exit 0
