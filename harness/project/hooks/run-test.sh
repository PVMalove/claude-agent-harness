#!/bin/bash
# Isolated runner of one selected test (docs/hooks/run-test.md): run-test.sh <test-id> | --probe.
# Not an event hook: the runner lives in .harness/health/isolated_runner.py, and this entry point
# only finds Python and the repository (two levels above .claude/hooks/). It never runs a test
# itself; without Python or the runner module it prints a runner_unavailable refusal and exits 3.
REFUSED='{"test_id":null,"result":"refused","reason":"runner_unavailable","detail":null,"exit_code":null,"run_id":null}'
if [ "$#" -eq 1 ] && [ "$1" = "--probe" ]; then
  REFUSED='{"status":"unavailable","reason":"runner_unavailable","detail":null}'
fi
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd -P)"
PY="$(command -v python3 || command -v python)"
if [ -z "$REPO" ] || [ -z "$PY" ]; then
  printf '%s\n' "$REFUSED"
  exit 3
fi

# -I -B: no PYTHON* variables, user site, current directory or bytecode writes. The packages are
# mapped onto <repo>/.harness without running harness/health/__init__.py, which would put the
# repository root on sys.path.
exec "$PY" -I -B -c '
import importlib, importlib.machinery, importlib.util, sys

def package(name, path):
    spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
    spec.submodule_search_locations = [path]
    sys.modules[name] = importlib.util.module_from_spec(spec)

refused, repo = sys.argv[1], sys.argv[2]
try:
    package("harness", repo + "/.harness")
    package("harness.health", repo + "/.harness/health")
    runner = importlib.import_module("harness.health.isolated_runner")
except Exception:
    print(refused)
    sys.exit(3)
sys.exit(runner.main(sys.argv[2:]))
' "$REFUSED" "$REPO" "$@" 2>/dev/null
