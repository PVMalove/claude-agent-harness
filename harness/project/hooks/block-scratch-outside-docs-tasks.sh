#!/bin/bash
# PreToolUse(Write|Edit): docs/agents/artifacts.md forbids saving specs/scratchpads under system temp dirs.
INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | grep -oE '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"file_path"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')

if echo "$FILE_PATH" | grep -qiE '(AppData.(Local|Roaming).Temp|[\\/]tmp[\\/]|[\\/]scratchpad[\\/])'; then
  echo "artifacts.md: спецификации и скретчпады пишем в docs/tasks/, не в системный temp: $FILE_PATH" >&2
  exit 2
fi

exit 0
