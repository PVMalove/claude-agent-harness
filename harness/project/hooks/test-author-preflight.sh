#!/bin/bash
# Fail-closed activation check for the optional test-author agent.
#
# The script is invoked by the developer before spawning the in-session Claude Code subagent. It
# prints exactly one sanitized JSON line and always exits 0: a disabled result preserves the normal
# developer route and does not create orchestration or ledger state.

set -u

REASON_PATTERN='^[a-z_]+$'
RUN_DIR=''

cleanup() {
  if [ -n "$RUN_DIR" ]; then
    rm -rf -- "$RUN_DIR"
  fi
}
trap cleanup EXIT

disabled() {
  local reason="$1"
  if [[ ! "$reason" =~ $REASON_PATTERN ]]; then
    reason='preflight_failed'
  fi
  printf '{"status":"disabled","reason":"%s"}\n' "$reason"
  exit 0
}

PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || disabled runner_unavailable

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd -P)"
[ -n "$REPO" ] || disabled project_untrusted

GIT_ROOT="$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null || true)"
[ "$GIT_ROOT" = "$REPO" ] || disabled project_untrusted

GUARD="$REPO/.claude/hooks/test-author-guard.py"
RUNNER="$REPO/.claude/hooks/run-test.sh"
[ -f "$GUARD" ] && [ -x "$GUARD" ] || disabled guard_hook_not_firing
[ -f "$RUNNER" ] && [ -x "$RUNNER" ] || disabled runner_unavailable

CONFIG="$($PY "$GUARD" --validate-config 2>/dev/null || true)"
CONFIG_REASON="$(printf '%s' "$CONFIG" | "$PY" -c '
import json
import sys

try:
    value = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError):
    raise SystemExit(1)
if value == {"status": "ok"}:
    print("ok")
elif isinstance(value, dict) and value.get("status") == "disabled" and isinstance(value.get("reason"), str):
    print(value["reason"])
else:
    raise SystemExit(1)
' 2>/dev/null || true)"
case "$CONFIG_REASON" in
  ok) ;;
  test_paths_missing|test_runner_missing|config_invalid|write_zone_rejected) disabled "$CONFIG_REASON" ;;
  *) disabled guard_hook_not_firing ;;
esac

PROBE="$($RUNNER --probe 2>/dev/null || true)"
PROBE_REASON="$(printf '%s' "$PROBE" | "$PY" -c '
import json
import sys

try:
    value = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError):
    raise SystemExit(1)
if value == {"status": "ok"}:
    print("ok")
elif isinstance(value, dict) and value.get("status") == "unavailable":
    reason = value.get("reason")
    print(reason if reason in {"isolation_unavailable", "runner_unavailable"} else "invalid")
else:
    print("invalid")
' 2>/dev/null || true)"
case "$PROBE_REASON" in
  ok) ;;
  isolation_unavailable|runner_unavailable) disabled "$PROBE_REASON" ;;
  *) disabled isolation_unavailable ;;
esac

RUNS="$REPO/.harness/.sandboxes/runs"
mkdir -p "$RUNS" 2>/dev/null || disabled guard_hook_not_firing
RUN_DIR="$(mktemp -d "$RUNS/test-author-preflight-XXXXXX" 2>/dev/null || true)"
[ -n "$RUN_DIR" ] || disabled guard_hook_not_firing
mkdir -p "$RUN_DIR/logs" 2>/dev/null || disabled guard_hook_not_firing

NONCE="$($PY -c 'import secrets; print(secrets.token_hex(12))' 2>/dev/null || true)"
[ -n "$NONCE" ] || disabled guard_hook_not_firing
NONCE_FILE="$RUN_DIR/logs/canary-nonce"
CANARY_LOG="$RUN_DIR/logs/canary.json"
printf '%s\n' "$NONCE" >"$NONCE_FILE" 2>/dev/null || disabled guard_hook_not_firing

CANARY_PAYLOAD='{"agent_type":"test-author","hook_event_name":"PreToolUse","tool_name":"Write","tool_input":{"file_path":"canary.txt"}}'
GUARD_OUTPUT="$(
  printf '%s' "$CANARY_PAYLOAD" |
    CLAUDE_PROJECT_DIR="$REPO" \
    TEST_AUTHOR_CANARY_NONCE_FILE="$NONCE_FILE" \
    TEST_AUTHOR_CANARY_LOG="$CANARY_LOG" \
    "$PY" "$GUARD" 2>/dev/null
)"

if ! GUARD_OUTPUT="$GUARD_OUTPUT" NONCE_FILE="$NONCE_FILE" CANARY_LOG="$CANARY_LOG" REPO="$REPO" "$PY" - <<'PY'
import json
import os
from pathlib import Path

try:
    answer = json.loads(os.environ["GUARD_OUTPUT"])
    output = answer["hookSpecificOutput"]
    if output["hookEventName"] != "PreToolUse" or output["permissionDecision"] != "deny":
        raise ValueError
    nonce = Path(os.environ["NONCE_FILE"]).read_text(encoding="utf-8").strip()
    evidence = json.loads(Path(os.environ["CANARY_LOG"]).read_text(encoding="utf-8"))
    if evidence != {"nonce": nonce, "decision": "deny"}:
        raise ValueError
    if (Path(os.environ["REPO"]) / "canary.txt").exists():
        raise ValueError
except (KeyError, OSError, ValueError, json.JSONDecodeError, TypeError):
    raise SystemExit(1)
PY
then
  disabled guard_hook_not_firing
fi

printf '{"status":"ok"}\n'
