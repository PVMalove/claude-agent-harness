#!/usr/bin/env python3
"""Observable Claude Code adapter. Native permissions stay enabled; no approval bypass flags."""

from __future__ import annotations

import argparse
import hashlib
import threading
from typing import BinaryIO
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

# Reuse the coordinator's source/installed namespace bootstrap, without running its CLI.
if __package__ in {None, ""}:
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "coordinator.py"),
        run_name="adapter_bootstrap",
    )

from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _read_object,
    _canonical,
    _sanitise,
)
from harness.orchestration.runtime_permissions import stage_request
from harness.orchestration.operation_access import probe_worker_resources
from harness.orchestration.worker_observation import (
    PROTOCOL,
    process_identity,
    visible_events,
    redact_visible,
)
from harness.storage import storage_path


class WorkerOutcomeUnknown(CoordinatorError):
    """A supervisor was launched; a missing receipt cannot prove that no worker exists."""


def _network(args: argparse.Namespace, brief: JsonObject) -> JsonObject:
    endpoint = args.endpoint
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
    ):
        raise CoordinatorError(
            "invalid runtime probe endpoint",
            remedy="use a credential-free HTTP(S) endpoint; no authentication is sent",
        )
    plan = brief.get("runtime_access", {})
    if any(
        source != "legacy" for source in plan.get("sources", {}).values()
    ) and parsed.hostname not in plan.get("network", {}).get("hosts", []):
        raise CoordinatorError(
            "runtime endpoint host is outside the approved network plan",
            remedy="propose and approve the required network host before launch",
        )

    class NoRedirect(HTTPRedirectHandler):
        # Reachability never grants a redirect outside the approved host.
        max_redirections = 0
        max_repeats = 0

    try:
        with build_opener(NoRedirect()).open(
            Request(endpoint, method="HEAD"), timeout=5
        ) as response:
            status = response.status
    except HTTPError as exc:
        status = (
            exc.code
        )  # An HTTP refusal still proves reachability, never model readiness.
    except (URLError, OSError) as exc:
        raise CoordinatorError(
            "runtime endpoint is unreachable",
            remedy="prepare network access in this context before launch",
        ) from exc
    return {
        "host": parsed.hostname,
        "http_status": status,
        "authentication": "not_checked",
        "model_readiness": "unverified",
    }


def _write(path: Path, data: JsonObject) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _prompt(args: argparse.Namespace, brief: JsonObject) -> str:
    coordinator = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "coordinator.py"),
        "--repo",
        str(Path(args.repo).resolve()),
    ]
    if args.state_dir:
        coordinator += ["--state-dir", args.state_dir]
    cli = shlex.join(coordinator)
    probe = shlex.join(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "probe",
            "--repo",
            args.repo,
            "--brief",
            args.brief,
            "--endpoint",
            args.endpoint,
        ]
        + (["--state-dir", args.state_dir] if args.state_dir else [])
    )
    reason = brief.get("retry_reason")
    return f"""You are the {brief['role']} worker for dispatch {brief['dispatch_id']}.
Read {Path(args.repo).resolve() / '.harness/docs/technical-english.md'} before English coordination.
Read {Path(args.repo).resolve() / '.harness/orchestration/roles/_common.md'} and the role manifest in that directory.
Immutable brief: {Path(args.brief).resolve()}
Report staging path: {brief['report_staging_path']}
Expected worktree: {brief['worktree']}
Expected startup SHA: {brief.get('snapshot_commit')}
Before task work, run git rev-parse --show-toplevel, git branch --show-current, and git rev-parse HEAD.
Stop on a mismatch. Do not reset or modify the brief.
Run this access probe through your actual native Bash tool before task work:
{probe}
If ready is false, stop before implementation. Preserve its exact resource, errno/refusal and remedy.
A successful adapter-side probe does not prove your native sandbox access.
Confirm your actual model and probed Git worktree:
{cli} dispatch self-report --dispatch {brief['dispatch_id']} --model \"<actual active model>\" --worktree \"<actual Git top-level>\"
If self-report fails, stop before task work. Do not copy an expected model as evidence.
After successful self-report, run {cli} dispatch heartbeat --dispatch {brief['dispatch_id']}
Repeat heartbeat at least every {brief.get('liveness', {}).get('heartbeat_every_seconds')} seconds while working.
Use the pinned Context Package and work only inside the brief's scope and commit plan.
Architect retry reason, when present: {json.dumps(reason, ensure_ascii=False)}
Write one honest completion report to the exact staging path. Use report_language: ru,
outcome completed|blocked|failed, checks_run result pass|fail|not_run, and status/details objects for risks/blockers.
Then run {cli} report submit --file {shlex.quote(brief['report_staging_path'])}.
If submission fails, preserve the original file and return the exact command and error.
If the result includes completion, relay it for report complete; never submit again.
Do not produce extra progress messages for the monitor. Existing runtime events are sufficient.
"""


def _stream_runtime(
    process: subprocess.Popen[bytes],
    root: Path,
    brief: JsonObject,
    launch_id: str,
    session: str,
    prompt: str,
    output: BinaryIO,
) -> None:
    """SDK-style control frames keep native permission answers outside model messages.

    Protocol reference: anthropics/claude-agent-sdk-python, _internal/query.py.
    No hook callbacks, SDK MCP servers or persistent permission changes are registered.
    """
    assert process.stdin is not None and process.stdout is not None
    stdin, stdout = process.stdin, process.stdout
    done, initialized = threading.Event(), threading.Event()
    write_lock = threading.Lock()
    output_lock = threading.Lock()

    def publish(event: JsonObject) -> None:
        with output_lock:
            if output.tell() >= 10 * 1024 * 1024:
                output.seek(0)
                output.truncate()
            output.write(
                (json.dumps({"type": "harness-event", **event}) + "\n").encode()
            )
            output.flush()

    def errors() -> None:
        assert process.stderr is not None
        while raw := process.stderr.readline(65536):
            publish(
                {
                    "kind": "stderr",
                    "text": redact_visible(raw.decode("utf-8", errors="replace")),
                }
            )

    error_reader = threading.Thread(target=errors, daemon=True)
    error_reader.start()
    control = root / "control"
    pending: dict[str, JsonObject] = {}
    pending_lock = threading.Lock()
    init_id = launch_id + "-initialize"

    def send(value: JsonObject) -> None:
        with write_lock:
            stdin.write((json.dumps(value) + "\n").encode())
            stdin.flush()

    def controls() -> None:
        while not done.wait(0.05):
            for path in (control / "responses").glob("*.json"):
                try:
                    value = json.loads(path.read_bytes())
                    request_id = value["request_id"]
                    with pending_lock:
                        request = pending.get(request_id)
                    original = control / "pending" / path.name
                    if (
                        request is None
                        or value.get("dispatch_id") != brief["dispatch_id"]
                        or value.get("launch_id") != launch_id
                        or hashlib.sha256(original.read_bytes()).hexdigest()
                        != value.get("request_sha256")
                    ):
                        continue
                    staged = json.loads(original.read_bytes())
                    if (
                        staged["request"].get("input_sha256")
                        != hashlib.sha256(
                            _canonical(request["request"].get("input", {})).encode()
                        ).hexdigest()
                    ):
                        continue
                    answer = value["response"]["response"]
                    choice = answer["response"]
                    if (
                        value["response"].get("type") != "control_response"
                        or answer.get("subtype") != "success"
                        or set(answer) != {"subtype", "request_id", "response"}
                        or set(choice)
                        != (
                            {"behavior"}
                            if choice.get("behavior") == "allow"
                            else {"behavior", "message"}
                        )
                        or answer.get("request_id") != request_id
                        or choice.get("behavior") not in {"allow", "deny"}
                        or (
                            choice["behavior"] == "allow"
                            and request["request"].get("tool_name")
                            not in brief.get("allowed_tools", [])
                        )
                    ):
                        continue
                    if choice["behavior"] == "allow":
                        choice["updatedInput"] = request["request"].get("input", {})
                    send(value["response"])
                    with (control / "decisions.jsonl").open(
                        "a", encoding="utf-8"
                    ) as audit:
                        audit.write(
                            json.dumps(
                                {
                                    "dispatch_id": brief["dispatch_id"],
                                    "launch_id": launch_id,
                                    "request_id": request_id,
                                    "request_sha256": value["request_sha256"],
                                    "behavior": choice["behavior"],
                                    "source": "console-operator",
                                }
                            )
                            + "\n"
                        )
                    with pending_lock:
                        pending.pop(request_id, None)
                    path.unlink(missing_ok=True)
                    original.unlink(missing_ok=True)
                except (OSError, ValueError, KeyError, TypeError):
                    continue

    responder = threading.Thread(target=controls, daemon=True)
    responder.start()
    send(
        {
            "type": "control_request",
            "request_id": init_id,
            "request": {"subtype": "initialize"},
        }
    )

    def initialization_watchdog() -> None:
        if not initialized.wait(30) and not done.is_set() and process.poll() is None:
            process.terminate()

    threading.Thread(target=initialization_watchdog, daemon=True).start()
    try:
        while raw := stdout.readline(1048576):
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            for event in visible_events(value):
                publish(event)
            if (
                value.get("type") == "control_response"
                and value.get("response", {}).get("request_id") == init_id
            ):
                if value["response"].get("subtype") != "success":
                    process.terminate()
                    break
                if not initialized.is_set():
                    initialized.set()
                    send(
                        {
                            "type": "user",
                            "session_id": session,
                            "message": {"role": "user", "content": prompt},
                        }
                    )
            elif value.get("type") == "control_request":
                if value.get("request", {}).get("subtype") == "can_use_tool":
                    stage_request(control, brief, launch_id, value)
                    with pending_lock:
                        pending[value["request_id"]] = value
                else:
                    send(
                        {
                            "type": "control_response",
                            "response": {
                                "subtype": "error",
                                "request_id": value.get("request_id"),
                                "error": "Unsupported native host request",
                            },
                        }
                    )
            elif value.get("type") == "control_cancel_request":
                request_id = str(value.get("request_id", ""))
                with pending_lock:
                    pending.pop(request_id, None)
                (
                    control
                    / "pending"
                    / (hashlib.sha256(request_id.encode()).hexdigest() + ".json")
                ).unlink(missing_ok=True)
            elif value.get("type") == "result":
                # This adapter runs one role, with no SDK background tasks. A final result ends
                # its input stream; an open stream would otherwise keep print-mode alive.
                with write_lock:
                    stdin.close()
    finally:
        done.set()
        responder.join(timeout=1)
        initialized.set()
        if not stdin.closed:
            stdin.close()
        process.wait(timeout=10)
        error_reader.join(timeout=1)


def supervise(args: argparse.Namespace, brief: JsonObject) -> JsonObject:
    root = Path(args.receipt).parent
    log = root / "events.jsonl"
    terminal = root / "terminal.json"
    launch_id = root.name
    session = str(uuid.uuid4())
    command = [
        args.runtime_executable,
        "-p",
        "--verbose",
        "--output-format",
        "stream-json",
        "--model",
        brief["resolved_model"],
        "--effort",
        brief["resolved_effort"],
        "--permission-mode",
        "manual",
        "--permission-prompts",
        "host",
        "--permission-prompt-tool",
        "stdio",
        "--input-format",
        "stream-json",
        "--session-id",
        session,
        "--tools",
        ",".join(brief.get("allowed_tools", [])),
    ]
    if args.worker_sandbox == "disabled":
        command += ["--settings", json.dumps({"sandbox": {"enabled": False}})]
    for directory in (root / "control/pending", root / "control/responses"):
        directory.mkdir(parents=True, mode=0o700)
    with log.open("xb") as output:
        log.chmod(0o600)
        process = subprocess.Popen(
            command,
            cwd=brief["worktree"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        receipt = {
            "protocol": PROTOCOL,
            "dispatch_id": brief["dispatch_id"],
            "launch_id": launch_id,
            "runtime_session_id": session,
            "worker_started": True,
            "visibility": args.visibility,
            "process": process_identity(process.pid),
            "log_path": str(log),
            "terminal_path": str(terminal),
            "preflight": {"worker_context": "pending-native-probe"},
        }
        _write(Path(args.receipt), receipt)
        _stream_runtime(
            process, root, brief, launch_id, session, _prompt(args, brief), output
        )
    _write(
        terminal,
        {
            "dispatch_id": brief["dispatch_id"],
            "launch_id": launch_id,
            "exit_code": process.returncode,
            "source": "runtime-supervisor",
        },
    )
    return receipt


def dispatch(args: argparse.Namespace, brief: JsonObject) -> JsonObject:
    if brief["role"] == "qa" or brief.get("purpose") == "publish":
        raise CoordinatorError(
            "QA and publish use their coordinator-owned boundaries",
            remedy="use qa run or dispatch publish, not the worker adapter",
        )
    if (
        args.visibility == "console"
        and os.environ.get("HARNESS_CONSOLE_ATTACHED") != "1"
    ):
        raise CoordinatorError(
            "a visible Harness console is required before worker launch",
            remedy="send from the Harness console, or explicitly choose --visibility=background",
        )
    if (
        args.worker_sandbox == "disabled"
        and brief.get("runtime_access", {}).get("mode") != "unsandboxed"
    ):
        raise CoordinatorError(
            "disabling the worker sandbox requires an approved unsandboxed plan",
            remedy="select the task's runtime access mode before propose/approval; global settings stay unchanged",
        )
    evidence = probe_worker_resources(
        Path(args.repo).resolve(),
        brief,
        state_dir=Path(args.state_dir) if args.state_dir else None,
    )
    if not evidence["ready"]:
        refused = [
            check for check in evidence["checks"] if check["state"] != "verified"
        ]
        raise CoordinatorError(
            "adapter access probe refused: " + json.dumps(refused),
            remedy="prepare the named resource in this execution context; do not repeat identical EROFS/EPERM launches",
        )
    executable = shutil.which(args.runtime_executable)
    if executable is None:
        raise CoordinatorError(
            "runtime executable is unavailable",
            remedy="install or select --runtime-executable; it is not the adapter executable",
        )
    help_result = subprocess.run(
        [executable, "--help"], capture_output=True, text=True, timeout=10
    )
    required = [
        "--verbose",
        "--output-format",
        "--model",
        "--effort",
        "--permission-mode",
        "--session-id",
        "--tools",
        "manual",
        "--input-format",
        "--permission-prompts",
    ]
    if help_result.returncode or any(
        flag not in help_result.stdout for flag in required
    ):
        raise CoordinatorError(
            "runtime lacks required streaming/session options",
            remedy="select a compatible runtime; no worker was launched",
        )
    network = _network(args, brief)
    root = storage_path(Path(args.repo), "logs", "workers") / (
        "launch-" + uuid.uuid4().hex
    )
    root.mkdir(parents=True, mode=0o700)
    receipt_file = root / "receipt.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "supervise",
        "--repo",
        args.repo,
        "--brief",
        args.brief,
        "--runtime-executable",
        executable,
        "--visibility",
        args.visibility,
        "--worker-sandbox",
        args.worker_sandbox,
        "--endpoint",
        args.endpoint,
        "--receipt",
        str(receipt_file),
    ]
    if args.state_dir:
        command += ["--state-dir", args.state_dir]
    with (root / "supervisor.log").open("xb") as errors:
        supervisor = subprocess.Popen(
            command,
            cwd=brief["worktree"],
            stdin=subprocess.DEVNULL,
            stdout=errors,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if receipt_file.is_file():
            receipt = _read_object(receipt_file, "launch receipt")
            receipt["preflight"]["adapter_network"] = network
            return receipt
        if supervisor.poll() is not None:
            raise WorkerOutcomeUnknown(
                "runtime supervisor exited before launch receipt",
                remedy="inspect the local supervisor log; do not infer worker termination from a missing receipt",
            )
        time.sleep(0.03)
    raise WorkerOutcomeUnknown(
        "runtime launch receipt timed out",
        remedy="inspect the same supervisor; do not start another worker",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("dispatch", "probe", "supervise"):
        command = commands.add_parser(name)
        command.add_argument("--repo", required=True)
        command.add_argument("--brief", required=True)
        command.add_argument("--state-dir")
        command.add_argument(
            "--endpoint",
            default=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        )
        command.add_argument("--runtime-executable", default="claude")
        command.add_argument(
            "--visibility", choices=("console", "background"), default="console"
        )
        command.add_argument(
            "--worker-sandbox", choices=("inherit", "disabled"), default="inherit"
        )
        if name == "supervise":
            command.add_argument("--receipt", required=True)
    args = parser.parse_args()
    try:
        brief = _read_object(Path(args.brief), "immutable brief")
        if args.command == "probe":
            evidence = probe_worker_resources(
                Path(args.repo).resolve(),
                brief,
                state_dir=Path(args.state_dir) if args.state_dir else None,
            )
            evidence["network"] = (
                _network(args, brief) if evidence["ready"] else {"state": "not_checked"}
            )
            print(json.dumps(evidence))
            return 0 if evidence["ready"] else 2
        if args.command == "supervise":
            supervise(args, brief)
        else:
            print(json.dumps(dispatch(args, brief)))
        return 0
    except (CoordinatorError, OSError, subprocess.TimeoutExpired) as exc:
        print(
            json.dumps(
                {
                    "protocol": PROTOCOL,
                    "worker_started": False
                    if args.command != "supervise"
                    and not isinstance(exc, WorkerOutcomeUnknown)
                    else None,
                    "error": _sanitise(str(exc)),
                    "remedy": getattr(
                        exc, "remedy", "inspect runtime access and native permissions"
                    ),
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
