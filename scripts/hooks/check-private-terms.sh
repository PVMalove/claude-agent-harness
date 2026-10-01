#!/bin/bash
# PreToolUse(Bash), repo-local and maintainer-only (#438): blocks Git commit/push and
# gh issue|pr create|edit|comment when the command text, its body files, the staged diff, unpublished
# commits or the branch name contain a private term. The logic lives in scripts/check_private_terms.py.
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "private-terms: Python 3 not found; cannot check publication commands." >&2
  exit 2
fi
exec "$PY" "$(dirname "$0")/../check_private_terms.py" --hook
