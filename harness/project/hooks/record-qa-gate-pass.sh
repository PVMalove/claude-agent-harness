#!/bin/bash
# Called directly by the qa-gate skill (see SKILL.md) as its own last step, right after every
# qa_gate_commands entry has succeeded. The skill runs forked (context: fork) and cannot count on
# mark-qa-gate-passed.sh (PostToolUse on Bash) to fire reliably for tool calls made inside that
# fork, so it records the marker itself here instead of only relying on the hook.
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
STATE="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null):$(git -C "$PROJECT_DIR" diff HEAD 2>/dev/null | git -C "$PROJECT_DIR" hash-object --stdin 2>/dev/null)"
MARKER_DIR="$PROJECT_DIR/.claude/.qa-gate"
mkdir -p "$MARKER_DIR"
echo "$STATE" > "$MARKER_DIR/passed"
