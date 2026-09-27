#!/bin/bash
# SessionStart hook for Claude Code on the web: installs the agent harness into this clone.
# The environment Setup script runs only when a container is created, so warm/resumed
# containers never get it; this hook runs at the start of every session instead.
# Local sessions are left untouched.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

repo="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
here="$repo/scripts/cloud"
venv_bin="$repo/.harness/.venv/bin"
marker="$HOME/.harness-setup-ok"

python_bin=""
for candidate in python3.13 python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then
    python_bin="$candidate"
    break
  fi
done
if [ -z "$python_bin" ]; then
  echo "harness setup skipped: python3.12+ is not installed" >&2
  exit 0
fi

# Bootstraps only the gitignored runtime state of this clone (.harness/, .claude/ hooks) and never
# edits a tracked file or an existing project config: the AGENTS.md exception for this hook.
if [ ! -f "$repo/.harness/harness.lock" ]; then
  mkdir -p "$repo/.harness"
  if [ ! -f "$repo/.harness/orchestration.json" ]; then
    cp "$here/orchestration.json" "$repo/.harness/orchestration.json"
  fi
  # Cloud threads work on claude/... branches, so the pattern is set once, at install time.
  "$python_bin" "$repo/harness/bin/harness.py" init "$repo" --project-type software --stack python \
    --capability pvmalove-suite --capability backend-orchestration \
    --base-branch master --language ru \
    --pr-base-branch master --branch-pattern "^(feature/issue-[0-9]+-.+|claude/.+)" \
    --qa-gate-command "make verify" </dev/null >/dev/null
  echo "harness installed into $repo"
fi
if [ ! -x "$venv_bin/python" ]; then
  (cd "$repo" && make bootstrap PYTHON_BOOTSTRAP="$python_bin" >/dev/null)
fi

# Verification commands in orchestration.json call bare `python`; take it from .harness/.venv.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$venv_bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

date -u +%FT%TZ > "$marker"
