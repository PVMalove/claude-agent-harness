#!/bin/bash
# PreToolUse(Bash): "Zero Direct Commits" from docs/agents/git-workflow.md, enforced deterministically.
INPUT=$(cat)
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Невозможно проверить запрет прямого коммита: Python 3.9+ не найден." >&2
  exit 2
fi

COMMAND="$(printf '%s' "$INPUT" | "$PY" -c '
import json, sys
try:
    data = json.load(open(0, encoding="utf-8", errors="ignore"))
    command = data.get("tool_input", {}).get("command", "")
    if isinstance(command, str):
        sys.stdout.write(command)
except Exception:
    pass
')"

REPO_DIR="${CLAUDE_PROJECT_DIR:-.}"
PROJECT_JSON="$REPO_DIR/.harness/project.json"

BASE_BRANCH="$( [ -f "$PROJECT_JSON" ] && "$PY" -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="ignore") as f:
        data = json.load(f)
        sys.stdout.write(str(data.get("base_branch", "")))
except Exception:
    pass
' "$PROJECT_JSON" )"

is_protected_branch() {
  BRANCH_NAME="$1"
  if [ "$BRANCH_NAME" = "master" ] || [ "$BRANCH_NAME" = "main" ]; then
    return 0
  fi
  if [ -n "$BASE_BRANCH" ] && [ "$BRANCH_NAME" = "$BASE_BRANCH" ]; then
    return 0
  fi
  echo "$BRANCH_NAME" | grep -qE '^integration/'
}

# Classify the integration/* refs of a push command against the remote. "exists <ref>",
# "unverified <remote> <ref>" and "unparsed <ref>" block; "create-only" means every push in the
# command only creates integration branches absent on the remote, as /to-spec does without
# switching the worktree, so a protected HEAD may run it.
INTEGRATION_STATUS="$(printf '%s' "$COMMAND" | "$PY" -c '
import os, re, shlex, subprocess, sys

command = sys.stdin.read()
push_count = len(re.findall(r"\bgit\s+push\b", command))
if not push_count:
    sys.exit()
# Refspecs never contain "<" or ">", so redirections such as "2>&1" are dropped before parsing.
parsable = re.sub(r"\d*[<>]+&?\s*[^\s;&|()<>]*", " ", command)
try:
    lexer = shlex.shlex(parsable, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    tokens = list(lexer)
except ValueError:
    tokens = parsable.split()

pushes = []
for i, token in enumerate(tokens[:-1]):
    if token != "git" or tokens[i + 1] != "push":
        continue
    remote, refspecs = None, []
    for arg in tokens[i + 2:]:
        if arg == "git" or re.fullmatch(r"[;&|()]+", arg):
            break
        if arg.startswith("-"):
            continue
        if remote is None:
            remote = arg
        else:
            refspecs.append(arg)
    pushes.append((remote, refspecs))

env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
created, create_only = set(), len(pushes) == push_count
for remote, refspecs in pushes:
    create_only = create_only and bool(refspecs)
    for refspec in refspecs:
        dest = re.sub(r"^refs/heads/", "", refspec.lstrip("+").rsplit(":", 1)[-1])
        if not dest.startswith("integration/"):
            create_only = False
            continue
        try:
            code = subprocess.run(
                ["git", "-C", sys.argv[1], "ls-remote", "--exit-code", remote, "refs/heads/" + dest],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=20,
            ).returncode
        except (OSError, subprocess.TimeoutExpired):
            code = None
        if code == 0:
            print("exists", dest)
            sys.exit()
        if code != 2:
            print("unverified", remote, dest)
            sys.exit()
        created.add(dest)
# Fail closed: every integration ref the command mentions must be a destination verified absent
# above; any other one sits in a push the parser cannot read (e.g. inside `bash -c "..."`).
mentioned = set(re.findall(r"(?<![\w./-])(?:refs/heads/)?(integration/[^\s\"\x27;&|()<>:]+)", command))
if mentioned - created:
    print("unparsed", min(mentioned - created))
elif create_only:
    print("create-only")
' "$REPO_DIR")"

case "$INTEGRATION_STATUS" in
  exists\ *)
    echo "Zero Direct Commits: push в существующую ветку '${INTEGRATION_STATUS#exists }' запрещён — работай на issue-ветке (docs/agents/git-workflow.md)." >&2
    exit 2
    ;;
  unverified\ *)
    read -r _ UNVERIFIED_REMOTE UNVERIFIED_REF <<<"$INTEGRATION_STATUS"
    echo "Zero Direct Commits: не удалось проверить на remote '$UNVERIFIED_REMOTE', существует ли '$UNVERIFIED_REF', — push заблокирован. Проверь доступ к remote и повтори (docs/agents/git-workflow.md)." >&2
    exit 2
    ;;
  unparsed\ *)
    echo "Zero Direct Commits: push с integration-рефом '${INTEGRATION_STATUS#unparsed }' запрещён — новую integration-ветку создавай отдельной командой git push -u origin integration/<name> (docs/agents/git-workflow.md)." >&2
    exit 2
    ;;
esac

if printf '%s\n' "$COMMAND" | grep -qE '\bgit commit\b' ||
  { printf '%s\n' "$COMMAND" | grep -qE '\bgit push\b' && [ "$INTEGRATION_STATUS" != "create-only" ]; }; then
  BRANCH=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)
  if is_protected_branch "$BRANCH"; then
    echo "Zero Direct Commits: коммит/push в защищённую ветку '$BRANCH' запрещён — работай на issue-ветке (docs/agents/git-workflow.md)." >&2
    exit 2
  fi
fi

# Also catch a push whose *target* refspec is a protected branch even from an issue branch
# (e.g. `git push origin HEAD:master`, `git push origin feature-x:master`, or bare `git push
# origin master`) — the current-branch check above only sees where HEAD is, not where the ref is
# going. Integration destinations are classified against the remote above.
if printf '%s\n' "$COMMAND" | grep -qE '\bgit push\b'; then
  for target in master main "$BASE_BRANCH"; do
    [ -n "$target" ] || continue
    if printf '%s\n' "$COMMAND" | grep -qE "(^|[\"[:space:]:])(refs/heads/)?$target([\"[:space:]]|\$)"; then
      echo "Zero Direct Commits: push с целевым рефом '$target' запрещён — работай на issue-ветке (docs/agents/git-workflow.md)." >&2
      exit 2
    fi
  done
fi

exit 0
