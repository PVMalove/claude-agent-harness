#!/bin/bash
# Fail-closed activation check for the optional test-author agent.
#
# The script is invoked by the developer before spawning the in-session Claude Code subagent. It
# prints exactly one sanitized JSON line and always exits 0: a disabled result preserves the normal
# developer route and does not create orchestration or ledger state.

set -u

REASON_PATTERN='^[a-z_]+$'

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

# Claude Code supplies this identity to scoped hooks and its Bash tool.  A matching canonical
# path is required before this helper can enable the optional subagent.
CLAUDE_REPO="${CLAUDE_PROJECT_DIR:-}"
[ -n "$CLAUDE_REPO" ] || disabled project_untrusted
CLAUDE_REPO="$(cd "$CLAUDE_REPO" 2>/dev/null && pwd -P || true)"
[ "$CLAUDE_REPO" = "$REPO" ] || disabled project_untrusted
[ "${CLAUDE_CODE_SIMPLE:-0}" = "1" ] && disabled claude_code_unsupported
[ "${CLAUDE_CODE_SAFE_MODE:-0}" = "1" ] && disabled claude_code_unsupported

GIT_ROOT="$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null || true)"
[ "$GIT_ROOT" = "$REPO" ] || disabled project_untrusted
[ "$(git -C "$REPO" rev-parse --is-inside-work-tree 2>/dev/null || true)" = "true" ] || disabled project_untrusted

# Claude Code keeps workspace trust decisions in ~/.claude.json.  The native --print mode skips
# the trust dialog, so the preflight must verify the decision itself before enabling an agent whose
# safety depends on a frontmatter hook.
TRUSTED_PROJECT="$($PY -c '
import json
import os
from pathlib import Path

try:
    project = str(Path(os.environ["CLAUDE_PROJECT_DIR"]).resolve())
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    state_path = (
        Path(config_dir).expanduser() / ".claude.json"
        if config_dir
        else Path.home() / ".claude.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    entry = state.get("projects", {}).get(project)
except (AttributeError, OSError, TypeError, ValueError, RecursionError):
    entry = None
print("ok" if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True else "disabled")
' 2>/dev/null || true)"
[ "$TRUSTED_PROJECT" = "ok" ] || disabled project_untrusted

AGENT="$REPO/.claude/agents/test-author.md"
[ -f "$AGENT" ] && [ ! -L "$AGENT" ] || disabled agent_missing
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
  agent_missing|scoped_hook_unsupported|test_paths_missing|test_runner_missing|config_invalid|write_zone_rejected) disabled "$CONFIG_REASON" ;;
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

printf '{"status":"ok"}\n'
