#!/usr/bin/env python3
"""Runtime adapter: start a headless Claude Code worker for an approved dispatch.

Claude Code cannot start a subagent in an existing checkout: the Agent tool has no working-directory
parameter, a subagent starts in the main checkout, and `EnterWorktree` refuses it there. The
`external` transport can: `coordinator.py dispatch send --adapter <this file>` runs the adapter in
`send.worker_worktree` as
`claude_worker.py dispatch [--extra-prompt-file F] [--dry-run] --repo <main repo> --brief <brief>`.
The adapter starts a detached `claude -p` there and returns; the worker's self-report, heartbeat and
`report submit` prove its actual checkout. It runs on the brief's model and effort, with exactly the
brief's `allowed_tools` and `dontAsk`, so nothing outside the brief can prompt or run unattended.

The installed copy is `.harness/adapters/claude_worker.py`, outside the hashed runtime package, so
installing it never changes the pinned runtime of an open batch.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import NotRequired, TypedDict, cast

STARTUP_CHECK_SECONDS = 3


class Liveness(TypedDict):
    heartbeat_every_seconds: int


class Brief(TypedDict):
    """The brief fields the adapter reads; the coordinator owns the full contract."""

    dispatch_id: str
    role: str
    resolved_model: str
    resolved_effort: NotRequired[str | None]
    allowed_tools: list[str]
    report_staging_path: str
    context_package_id: str
    liveness: Liveness


PROMPT = """\
You are the {role} worker for dispatch {dispatch_id}.
Brief: {brief}
Report staging path: {staging}
Coordinator CLI: {cli}
Expected worker checkout: {worktree}
Expected startup SHA: {sha}
Before task work, run git rev-parse --show-toplevel, git branch --show-current and git rev-parse HEAD
in your runtime's current directory. Compare the top-level path and HEAD with the expected values
above, and the branch with the brief. Stop on any mismatch; do not reset Git history or claim
another checkout as your actual directory. Confirm your active model and the probed Git top-level:
{cli} dispatch self-report --dispatch {dispatch_id} --model "<actual active model>" --worktree "<probed Git top-level>"
Proceed only after a successful self-report; escalate a mismatch or unavailable model identity.
Immediately after self-report and at least every {heartbeat} seconds while working, run:
{cli} dispatch heartbeat --dispatch {dispatch_id}
Context Package {package}: start from its starting_files, symbol_graph and
related_tests. For a starting file with non-empty sections, read only the start_line-end_line
ranges the task needs.
Work within the brief; escalate a blocker for anything the brief and the package leave out.
Before your final reply, write the completion report JSON to the exact report staging path using
the common and role-specific report contract, with report_language: ru.
Use exact English enums: outcome = completed | blocked | failed; checks_run[].result = pass | fail | not_run.
Set blockers and risks (also in every review axis) to {{"status": "none", "details": ""}} when absent,
or {{"status": "present", "details": "<Russian description>"}} when present. A none status requires empty
details; present requires a non-empty description. Put other explanations in output. Strings, translated
statuses, passed and not-run are invalid; no aliases or old report formats are accepted.
Then run:
{cli} report submit --file "{staging}"
Completion means the report is recorded in the ledger. Include the submit result in your final reply.
If submission fails before recording, return the command and error as a blocker; keep the report file.
If the result includes completion, relay it to the coordinator for report complete; the report is
already recorded, so submit it only once. Chat text alone does not complete the dispatch.
"""


def build_prompt(
    brief: Brief, brief_path: Path, repo: Path, worktree: Path, sha: str
) -> str:
    cli = " ".join(
        shlex.quote(part)
        for part in (
            sys.executable,
            str(repo / ".harness" / "orchestration" / "coordinator.py"),
            "--repo",
            str(repo),
        )
    )
    return PROMPT.format(
        role=brief["role"],
        dispatch_id=brief["dispatch_id"],
        brief=brief_path,
        staging=brief["report_staging_path"],
        cli=cli,
        worktree=worktree,
        sha=sha,
        heartbeat=brief["liveness"]["heartbeat_every_seconds"],
        package=brief["context_package_id"],
    )


def worker_command(brief: Brief, repo: Path) -> list[str]:
    command = [
        "claude",
        "-p",
        "--model",
        brief["resolved_model"],
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ",".join(brief["allowed_tools"]),
        "--add-dir",
        str(repo),
        "--no-session-persistence",
        "--output-format",
        "text",
    ]
    effort = brief.get("resolved_effort")
    if effort:
        command += ["--effort", effort]
    return command


def start(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    brief_path = Path(args.brief).resolve()
    brief = cast(Brief, json.loads(brief_path.read_text(encoding="utf-8")))
    worktree = Path.cwd().resolve()
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    prompt = build_prompt(brief, brief_path, repo, worktree, sha)
    if args.extra_prompt_file:
        prompt += "\n" + Path(args.extra_prompt_file).read_text(encoding="utf-8")
    command = worker_command(brief, repo)
    if args.dry_run:
        print(json.dumps({"cwd": str(worktree), "command": command, "prompt": prompt}))
        return 0
    log = Path(brief["report_staging_path"]).parent.parent / "worker-logs"
    log.mkdir(parents=True, exist_ok=True)
    log_path = log / f"{brief['dispatch_id']}.log"
    with log_path.open("ab") as sink:
        process = subprocess.Popen(
            command,
            cwd=worktree,
            stdin=subprocess.PIPE,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    assert process.stdin is not None
    process.stdin.write(prompt.encode("utf-8"))
    process.stdin.close()
    time.sleep(STARTUP_CHECK_SECONDS)
    code = process.poll()
    if code not in (None, 0):
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
        print(f"claude worker exited with {code} at startup: {tail}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    dispatch = sub.add_parser(
        "dispatch", help="start a worker for one approved dispatch"
    )
    dispatch.add_argument("--repo", required=True, help="main project root")
    dispatch.add_argument("--brief", required=True, help="immutable dispatch brief")
    dispatch.add_argument(
        "--extra-prompt-file",
        help="text appended to the prompt (retry handoff lines)",
    )
    dispatch.add_argument(
        "--dry-run",
        action="store_true",
        help="print the command and prompt; start nothing",
    )
    dispatch.set_defaults(handler=start)
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
