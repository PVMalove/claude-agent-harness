#!/bin/bash
# PreToolUse(Skill): tally how often each skill is invoked, for future analysis.
# Never blocks - analytics only, best-effort.
INPUT=$(cat)

SKILL=$(echo "$INPUT" | grep -oE '"skill"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"skill"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')

if [ -n "$SKILL" ]; then
  COUNTS_FILE="${CLAUDE_PROJECT_DIR:-.}/.claude/.skill-usage.json"
  mkdir -p "$(dirname "$COUNTS_FILE")"
  [ -f "$COUNTS_FILE" ] || echo '{}' > "$COUNTS_FILE"

  CURRENT=$(grep -oE "\"$SKILL\"[[:space:]]*:[[:space:]]*[0-9]+" "$COUNTS_FILE" | grep -oE '[0-9]+$')
  CURRENT=${CURRENT:-0}
  NEXT=$((CURRENT + 1))

  if [ "$CURRENT" -eq 0 ] 2>/dev/null && ! grep -q "\"$SKILL\"" "$COUNTS_FILE"; then
    # First time this skill is seen: insert before the closing brace.
    sed -i "s/}[[:space:]]*\$/,\"$SKILL\":$NEXT}/" "$COUNTS_FILE"
    sed -i 's/^{,/{/' "$COUNTS_FILE"
  else
    sed -i -E "s/\"$SKILL\"[[:space:]]*:[[:space:]]*[0-9]+/\"$SKILL\":$NEXT/" "$COUNTS_FILE"
  fi
fi

exit 0
