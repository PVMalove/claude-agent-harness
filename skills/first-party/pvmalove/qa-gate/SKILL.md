---
name: qa-gate
description: Run this project's full local quality gate (lint/typecheck/test commands from .harness/project.json) and report pass/fail. Use before opening a PR, or whenever asked to run the full check/test suite.
context: fork
background: false
---

Run this project's quality gate and report the result. Nothing else — no restating the commands, no unrelated commentary.

1. Read `qa_gate_commands` from `.harness/project.json` — an ordered list of shell commands (e.g. `["make check", "make test"]` or `["ruff check .", "mypy .", "pytest"]`). If the file or field is missing, tell the user to fill it in (run `harness init`/`update` again, or edit `.harness/project.json` directly) and stop.
2. Run each command in order. On the first failure, stop and report the **complete, unabridged** output of the failing command — do not summarize or truncate it. Do not run the remaining commands.
3. If every command succeeds, record the pass marker yourself: run `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/record-qa-gate-pass.sh"`. Don't rely on the `mark-qa-gate-passed.sh` PostToolUse hook alone for this — this skill runs forked (see `context: fork` above), and the hook isn't guaranteed to fire for Bash calls made inside that fork.
4. Report a compact one-line PASS summary only — do not paste the passing output.
