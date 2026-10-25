#!/usr/bin/env python3
"""Standalone external adapter for a detached Codex app-server worker.

Install through harness init/adopt/update with backend-orchestration. The coordinator invokes
``codex_worker.py dispatch --repo <main repo> --brief <immutable brief>`` in the worker checkout.
Only stdlib is required. Native permissions separate source access from lifecycle writes.
The worker receives its own thread/start identity and still submits its own self-report.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NotRequired, TypedDict, cast

STARTUP_CHECK_SECONDS = 10
PERMISSION_PROFILE = "harness-worker"


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
    resolved_transport: str
    purpose: str
    access: str


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
Allowed tools: {allowed_tools}. Follow the brief tool allowlist throughout the task.
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
If submission returns ledger_busy, retry after its interval. If submission otherwise fails before
recording, return the command and error as a blocker; keep the report file.
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
        allowed_tools=", ".join(brief["allowed_tools"]),
    )


def git_commit_permissions(worktree: Path) -> dict[str, str]:
    """Allow Git commits without opening shared hooks or repository configuration."""

    def git(*arguments: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(worktree), *arguments], text=True
        ).strip()

    gitdir = Path(git("rev-parse", "--absolute-git-dir"))
    common = (worktree / git("rev-parse", "--git-common-dir")).resolve()
    if not git("symbolic-ref", "--quiet", "HEAD").startswith("refs/heads/"):
        raise ValueError("a write-role worker requires a local branch")
    # Native writable roots must be directories (file roots fail bubblewrap setup).
    # Git locks and atomically renames refs, including packed-refs in the common root.
    permissions = {
        str(path): "write"
        for path in (common, common / "objects", common / "refs", common / "logs/refs")
    }
    if gitdir != common:
        # Protect existing shared metadata and sibling worktree administration, then reopen
        # the selected worktree's own administration and the shared ref logs.
        permissions.update(
            {
                str(path): "read"
                for path in common.iterdir()
                if path.name not in {"objects", "refs", "packed-refs"}
            }
        )
        permissions[str(gitdir)] = "write"
        permissions[str(common / "logs/refs")] = "write"
    permissions.update({str(common / name): "read" for name in ("config", "hooks")})
    return permissions


def worker_command(
    brief: Brief,
    repo: Path,
    worktree: Path,
    binary: str = "codex",
    *,
    network_access: bool = False,
) -> list[str]:
    access = brief.get("access")
    if access not in ("write", "read-only"):
        raise ValueError("access must be write or read-only")
    command = [
        binary,
        "app-server",
    ]
    filesystem = {
        ":root": "read",
        str(worktree): "write" if access == "write" else "read",
        str(repo / ".harness/orchestration/state"): "write",
        str(Path(brief["report_staging_path"]).parent): "write",
        str(repo / ".harness/.sandboxes/runs/qa"): "write",
        str(repo / ".harness/.sandboxes/runs/tests"): "write",
        str(repo / ".harness/.sandboxes/logs"): "write",
    }
    if access == "write":
        filesystem.update({":tmpdir": "write", ":slash_tmp": "write"})
        filesystem.update(
            {
                str(worktree / name): "read"
                for name in (".git", ".agents", ".codex", ".aws")
            }
        )
        filesystem.update(git_commit_permissions(worktree))
    entries = ", ".join(
        json.dumps(path) + " = " + json.dumps(mode) for path, mode in filesystem.items()
    )
    command += [
        "--config",
        'default_permissions="harness-worker"',
        "--config",
        'permissions={"harness-worker"={filesystem={'
        + entries
        + "},network={enabled="
        + str(network_access).lower()
        + "}}}",
    ]
    effort = brief.get("resolved_effort")
    if effort:
        command += ["--config", "model_reasoning_effort=" + json.dumps(effort)]
    return command


class AppServer:
    """One native worker connection; observations never replace ledger approval."""

    def __init__(self, command: list[str], cwd: Path) -> None:
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            encoding="utf-8",
        )
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.notifications: list[dict[str, object]] = []
        self.sequence = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.messages.put(line)
        self.messages.put(None)

    def send(self, message: dict[str, object]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def receive(self, timeout: float | None = None) -> dict[str, object]:
        try:
            line = self.messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise ValueError("native runtime startup timed out") from exc
        if line is None:
            code = self.process.wait(timeout=2)
            raise ValueError(f"native runtime exited with {code}")
        try:
            message = json.loads(line)
        except ValueError as exc:
            try:
                code = self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise ValueError(
                    "native runtime returned invalid JSON: " + line[:500]
                ) from exc
            raise ValueError(
                f"native runtime exited with {code}: {line[:500]}"
            ) from exc
        if not isinstance(message, dict):
            raise ValueError("native runtime message must be an object")
        if "method" in message and "id" in message:
            self.send(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Unattended worker grants no additional permissions",
                    },
                }
            )
        return cast(dict[str, object], message)

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + STARTUP_CHECK_SECONDS
        while True:
            message = self.receive(max(0, deadline - time.monotonic()))
            if message.get("id") != request_id:
                self.notifications.append(message)
                continue
            if "error" in message:
                raise ValueError(f"{method} refused: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise ValueError(f"{method} returned no result object")
            return cast(dict[str, object], result)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


def thread_params(brief: Brief, worktree: Path) -> dict[str, object]:
    params: dict[str, object] = {
        "model": brief["resolved_model"],
        "cwd": str(worktree),
        "approvalPolicy": "never",
        "permissions": PERMISSION_PROFILE,
        "ephemeral": True,
    }
    effort = brief.get("resolved_effort")
    if effort:
        params["config"] = {"model_reasoning_effort": effort}
    return params


def run_worker(args: argparse.Namespace) -> int:
    job = json.load(sys.stdin)
    receipt = Path(job["receipt"])
    server: AppServer | None = None
    try:
        brief = cast(Brief, job["brief"])
        worktree = Path(job["worktree"])
        server = AppServer(job["command"], worktree)
        server.request(
            "initialize",
            {
                "clientInfo": {"name": "harness-worker", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        server.send({"method": "initialized", "params": {}})
        effort = brief.get("resolved_effort")
        started = server.request("thread/start", thread_params(brief, worktree))
        expected = {"model": brief["resolved_model"], "cwd": str(worktree)}
        if effort:
            expected["reasoningEffort"] = effort
        for field, value in expected.items():
            if started.get(field) != value:
                raise ValueError(
                    f"native thread/start {field} mismatch: {started.get(field)!r}"
                )
        profile = started.get("activePermissionProfile")
        if not isinstance(profile, dict) or profile.get("id") != PERMISSION_PROFILE:
            raise ValueError(
                "native thread/start did not apply the worker permission profile"
            )
        thread = started.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise ValueError("native thread/start returned no thread identity")
        evidence = {
            "source": "thread/start",
            "thread_id": thread["id"],
            **{field: started[field] for field in expected},
        }
        prompt = job["prompt"] + (
            "\nNative runtime evidence for THIS worker session (not a separate probe):\n"
            + json.dumps(evidence)
            + "\nUse this observed model identity for your own mandatory "
            "self-report. Still probe Git in your actual CWD and stop on any mismatch.\n"
        )
        server.request(
            "turn/start",
            {
                "threadId": thread["id"],
                "input": [{"type": "text", "text": prompt}],
            },
        )
        receipt.write_text(
            json.dumps({"ready": True, "evidence": evidence}), encoding="utf-8"
        )
        while True:
            message = (
                server.notifications.pop(0)
                if server.notifications
                else server.receive()
            )
            method = message.get("method")
            payload = message.get("params")
            if not isinstance(payload, dict):
                continue
            if method == "item/agentMessage/delta":
                print(payload.get("delta", ""), end="", flush=True)
            elif method == "error":
                raise ValueError(f"native worker error: {payload}")
            elif method == "turn/completed":
                turn = payload.get("turn")
                if not isinstance(turn, dict) or turn.get("status") != "completed":
                    raise ValueError(f"native worker turn did not complete: {turn}")
                return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        if not receipt.exists():
            receipt.write_text(
                json.dumps({"ready": False, "error": str(exc)}), encoding="utf-8"
            )
        print(f"codex worker failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if server is not None:
            server.close()


def start(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    brief_path = Path(args.brief).resolve()
    brief = cast(Brief, json.loads(brief_path.read_text(encoding="utf-8")))
    if brief.get("role") == "qa":
        raise ValueError("role qa requires the coordinator QA runner")
    if brief.get("purpose") == "publish":
        raise ValueError("purpose publish requires the coordinator publish boundary")
    if brief.get("resolved_transport") != "external":
        raise ValueError("resolved_transport must be external")
    allowed = brief.get("allowed_tools")
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(not isinstance(tool, str) or not tool.strip() for tool in allowed)
    ):
        raise ValueError("allowed_tools must be a non-empty list of tool names")
    if (
        not isinstance(brief.get("resolved_model"), str)
        or not brief["resolved_model"].strip()
    ):
        raise ValueError("resolved_model must be a non-empty string")
    staging = Path(brief["report_staging_path"])
    incoming = repo / ".harness/.sandboxes/scratch/inbox"
    if not staging.is_absolute() or staging.parent.resolve() != incoming.resolve():
        raise ValueError(
            "report_staging_path must be in the incoming reports directory"
        )
    worktree = Path.cwd().resolve()
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    prompt = build_prompt(brief, brief_path, repo, worktree, sha)
    if args.extra_prompt_file:
        prompt += "\n" + Path(args.extra_prompt_file).read_text(encoding="utf-8")
    command = worker_command(
        brief,
        repo,
        worktree,
        args.codex_bin,
        network_access=args.network_access == "enabled",
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "cwd": str(worktree),
                    "command": command,
                    "prompt": prompt,
                    "thread_start": thread_params(brief, worktree),
                }
            )
        )
        return 0
    log = Path(brief["report_staging_path"]).parent.parent / "worker-logs"
    log.mkdir(parents=True, exist_ok=True)
    log_path = log / f"{brief['dispatch_id']}.log"
    receipt = log / f"{brief['dispatch_id']}.ready.json"
    receipt.unlink(missing_ok=True)
    with log_path.open("ab") as sink:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_run"],
            cwd=worktree,
            stdin=subprocess.PIPE,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    assert process.stdin is not None
    try:
        process.stdin.write(
            json.dumps(
                {
                    "command": command,
                    "prompt": prompt,
                    "brief": brief,
                    "worktree": str(worktree),
                    "receipt": str(receipt),
                }
            ).encode("utf-8")
        )
        process.stdin.close()
    except BrokenPipeError:
        process.wait(timeout=STARTUP_CHECK_SECONDS)
    deadline = time.monotonic() + STARTUP_CHECK_SECONDS + 2
    while time.monotonic() < deadline:
        if receipt.exists():
            try:
                result = json.loads(receipt.read_text(encoding="utf-8"))
            except ValueError:
                time.sleep(0.02)
                continue
            if result.get("ready") is True:
                return 0
            process.wait(timeout=3)
            print(result.get("error", "worker startup failed"), file=sys.stderr)
            return 1
        if process.poll() is not None:
            break
        time.sleep(0.02)
    if process.poll() is None:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=3)
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
    print(f"codex worker failed before native readiness: {tail}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    dispatch = sub.add_parser(
        "dispatch", help="start a worker for one approved dispatch"
    )
    dispatch.add_argument("--repo", required=True, help="main project root")
    dispatch.add_argument("--brief", required=True, help="immutable dispatch brief")
    dispatch.add_argument("--codex-bin", default="codex", help="Codex executable")
    dispatch.add_argument(
        "--network-access",
        choices=("disabled", "enabled"),
        default="disabled",
        help="native command network policy; enabled also permits local socketpair IPC",
    )
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
    worker = sub.add_parser("_run", help=argparse.SUPPRESS)
    worker.set_defaults(handler=run_worker)
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"codex worker could not start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
