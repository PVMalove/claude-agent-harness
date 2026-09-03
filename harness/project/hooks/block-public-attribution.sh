#!/bin/bash
# PreToolUse(Bash): keeps commit messages and PR metadata project-only.
INPUT=$(cat)
# Decode only escaped quotes for reliable matching; keep the command intact so a quoted
# `git commit -m "..."` is not truncated before its message.
COMMAND="$(printf '%s\n' "$INPUT" | sed 's/\\\\\"/\"/g')"

FORBIDDEN='claude|claude\.ai/code/session|openai|chatgpt|gpt[-_ ]?[0-9]|copilot|gemini|codex|co-authored[- ]by|ai[-_ ]?(agent|assistant|generated)'

is_commit=0
is_pr=0
is_push=0
printf '%s\n' "$COMMAND" | grep -qiE 'git[[:space:]]+commit' && is_commit=1
printf '%s\n' "$COMMAND" | grep -qiE '(gh[[:space:]]+pr|glab[[:space:]]+mr)[[:space:]]+(create|edit)' && is_pr=1
printf '%s\n' "$COMMAND" | grep -qiE 'git[[:space:]]+push' && is_push=1

[ "$is_commit" -eq 0 ] && [ "$is_pr" -eq 0 ] && [ "$is_push" -eq 0 ] && exit 0

if printf '%s\n' "$COMMAND" | grep -qiE "$FORBIDDEN"; then
  echo "Публичные Git-метаданные должны содержать только сведения об изменении проекта: автоматическая атрибуция, имена моделей и session URL запрещены." >&2
  exit 2
fi

if [ "$is_push" -eq 1 ] || [ "$is_pr" -eq 1 ]; then
  LOCAL_MESSAGES="$(git -C "${CLAUDE_PROJECT_DIR:-.}" log --format=%B --no-merges --not --remotes 2>/dev/null)"
  if printf '%s\n' "$LOCAL_MESSAGES" | grep -qiE "$FORBIDDEN"; then
    echo "Публикация заблокирована: непереданный commit message содержит запрещённую автоматическую атрибуцию или ссылку на сессию." >&2
    exit 2
  fi
fi

extract_file() {
  local option="$1"
  if [[ "$COMMAND" =~ $option[[:space:]]+\"([^\"]+)\" ]]; then
    printf '%s' "${BASH_REMATCH[2]}"
  elif [[ "$COMMAND" =~ $option[[:space:]]+([^[:space:]\"]+) ]]; then
    printf '%s' "${BASH_REMATCH[2]}"
  elif [[ "$COMMAND" =~ $option=\"([^\"]+)\" ]]; then
    printf '%s' "${BASH_REMATCH[2]}"
  elif [[ "$COMMAND" =~ $option=([^[:space:]\"]+) ]]; then
    printf '%s' "${BASH_REMATCH[2]}"
  fi
}

MESSAGE_FILE=""
if [ "$is_commit" -eq 1 ]; then
  MESSAGE_FILE="$(extract_file '(-F|--file)')"
fi
if [ "$is_pr" -eq 1 ] && [ -z "$MESSAGE_FILE" ]; then
  MESSAGE_FILE="$(extract_file '(--body-file|--description-file)')"
fi

if [ -n "$MESSAGE_FILE" ] && [ -f "$MESSAGE_FILE" ] && grep -qiE "$FORBIDDEN" "$MESSAGE_FILE"; then
  echo "Публичные Git-метаданные в файле сообщения содержат запрещённую автоматическую атрибуцию или ссылку на сессию." >&2
  exit 2
fi

exit 0
