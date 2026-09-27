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

if [ ! -f "$repo/.harness/project.json" ] || [ ! -x "$venv_bin/python" ]; then
  mkdir -p "$repo/.harness"
  if [ ! -f "$repo/.harness/orchestration.json" ]; then
    cp "$here/orchestration.json" "$repo/.harness/orchestration.json"
  fi
  "$python_bin" "$repo/harness/bin/harness" init "$repo" --project-type software --stack python \
    --capability pvmalove-suite --capability backend-orchestration \
    --base-branch master --language ru \
    --pr-base-branch integration/harness-console --qa-gate-command "make verify" >/dev/null
  (cd "$repo" && make bootstrap PYTHON_BOOTSTRAP="$python_bin" >/dev/null)
  echo "harness installed into $repo"
fi

# Cloud threads work on claude/... branches; the committed .claude/settings.json (this hook)
# is recorded in the integrations inventory so `harness health` stays clean.
"$python_bin" - "$repo" <<'EOF'
import hashlib, json, sys
from pathlib import Path

repo = Path(sys.argv[1])
project = repo / ".harness/project.json"
data = json.loads(project.read_text(encoding="utf-8"))
data["branch_pattern"] = "^(feature/issue-[0-9]+-.+|claude/.+)"
project.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

settings = Path(".claude/settings.json")
inventory = repo / ".harness/integrations.json"
if (repo / settings).is_file():
    doc = json.loads(inventory.read_text(encoding="utf-8")) if inventory.is_file() else {}
    entries = [e for e in doc.get("integrations") or [] if e.get("id") != "cloud-session-start"]
    entries.append({
        "id": "cloud-session-start",
        "kind": "hook",
        "runtimes": ["claude"],
        "config": settings.as_posix(),
        "sha256": hashlib.sha256((repo / settings).read_bytes()).hexdigest(),
        "secret_refs": [],
        "verify": "Start a Claude Code on the web session and confirm ~/.harness-setup-ok exists.",
    })
    inventory.write_text(
        json.dumps({"schema": 1, "integrations": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
EOF

# Verification commands in orchestration.json call bare `python`; take it from .harness/.venv.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$venv_bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

date -u +%FT%TZ > "$marker"
