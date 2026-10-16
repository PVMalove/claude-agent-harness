"""Installed-example adapter: fail before handoff or retain real local runtime evidence."""

from __future__ import annotations

import json
from collections.abc import Iterator
import os
import subprocess
import sys
import time
import pytest
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from tests.orchestration import test_startup_lifecycle as startup_tests
from tests.orchestration import test_recovery as recovery_tests

external_worker = startup_tests.launch

# The fake runtimes below are shebang scripts the adapter executes directly.
posix_runtime = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake runtime is a POSIX shebang script"
)

ADAPTER = (
    Path(__file__).resolve().parents[2]
    / "harness/orchestration/adapters/claude_code.py"
)


@pytest.fixture
def reachable_endpoint() -> Iterator[str]:
    class Reachable(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Reachable)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_visible_launch_never_silently_becomes_a_hidden_process(
    external_worker: recovery_tests.RecoveryTests,
) -> None:
    batch = external_worker._create_batch()
    result = external_worker._dispatch(batch["batch_id"], "architect")
    env = dict(os.environ)
    env.pop("HARNESS_CONSOLE_ATTACHED", None)
    process = subprocess.run(
        [
            sys.executable,
            str(ADAPTER),
            "dispatch",
            "--repo",
            str(external_worker.repo),
            "--brief",
            str(
                external_worker._records()
                / "dispatches"
                / (result["dispatch_id"] + ".json")
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 2
    evidence = json.loads(process.stdout)
    assert evidence["worker_started"] is False
    assert "visible" in evidence["error"]


@posix_runtime
def test_background_choice_preserves_runtime_stream_and_exit_evidence(
    external_worker: recovery_tests.RecoveryTests,
    reachable_endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXAMPLE_API_KEY", "test-secret-716-value")
    batch = external_worker._create_batch()
    result = external_worker._dispatch(batch["batch_id"], "architect")
    endpoint = reachable_endpoint
    runtime = external_worker.tmp / "fake-claude"
    runtime.write_text(
        "#!/usr/bin/env python3\nimport json,sys,os\n"
        "if '--help' in sys.argv:\n print('--verbose --output-format --model --effort --permission-mode --session-id --tools manual --input-format --permission-prompts');sys.exit(0)\n"
        "init=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':init['request_id'],'response':{}}}),flush=True)\n"
        "sys.stdin.readline()\nprint(json.dumps({'type':'assistant','message':{'content':[{'type':'thinking','thinking':'hidden-analysis-716'},{'type':'text','text':'runtime output '+os.environ['EXAMPLE_API_KEY']}]}}),flush=True)\n"
        "print('stderr '+os.environ['EXAMPLE_API_KEY'],file=sys.stderr,flush=True)\n"
    )
    runtime.chmod(0o755)
    process = subprocess.run(
        [
            sys.executable,
            str(ADAPTER),
            "dispatch",
            "--repo",
            str(external_worker.repo),
            "--brief",
            str(
                external_worker._records()
                / "dispatches"
                / (result["dispatch_id"] + ".json")
            ),
            "--runtime-executable",
            str(runtime),
            "--visibility=background",
            "--endpoint",
            endpoint,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr or process.stdout
    receipt = json.loads(process.stdout)
    assert receipt["protocol"] == "harness-worker-v1"
    assert receipt["dispatch_id"] == result["brief"]["dispatch_id"]
    terminal = Path(receipt["terminal_path"])
    deadline = time.monotonic() + 5
    while not terminal.exists() and time.monotonic() < deadline:
        time.sleep(0.03)
    evidence = json.loads(terminal.read_text())
    assert evidence["exit_code"] == 0
    assert evidence["launch_id"] == receipt["launch_id"]
    assert "runtime output" in Path(receipt["log_path"]).read_text()
    assert receipt["preflight"]["worker_context"] == "pending-native-probe"
    persisted = b"".join(
        path.read_bytes() for path in terminal.parent.rglob("*") if path.is_file()
    )
    assert b"hidden-analysis-716" not in persisted
    assert b"test-secret-716-value" not in persisted


def test_preflight_refusal_is_recorded_as_no_worker_started(
    external_worker: recovery_tests.RecoveryTests,
) -> None:
    from tests.orchestration.test_coordinator import ORCHESTRATION_ROOT
    from tests.orchestration.test_startup_lifecycle import cli

    batch = external_worker._create_batch()
    brief = external_worker._dispatch(batch["batch_id"], "architect")["brief"]
    env = dict(os.environ)
    env.pop("HARNESS_CONSOLE_ATTACHED", None)
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATION_ROOT / "coordinator.py"),
            "--repo",
            str(external_worker.repo),
            "--state-dir",
            str(external_worker.state_dir),
            "dispatch",
            "send",
            "--dispatch",
            brief["dispatch_id"],
            "--adapter",
            str(ADAPTER),
            "--worktree",
            str(external_worker.worktree),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "visible" in result.stderr
    status = cli(
        external_worker, "dispatch", "status", "--dispatch", brief["dispatch_id"]
    )["dispatches"][0]
    observed = cli(
        external_worker, "dispatch", "observe", "--dispatch", brief["dispatch_id"]
    )
    assert "visible" in observed["failure"]["error"]
    assert status["startup"]["worker_state"] == "absent"
    assert status["startup"]["restart_allowed"] is True


def test_worker_context_probe_keeps_the_exact_filesystem_refusal(
    external_worker: recovery_tests.RecoveryTests,
) -> None:
    import pytest

    if sys.platform == "win32" or os.geteuid() == 0:
        pytest.skip("root can bypass the fixture directory mode")
    batch = external_worker._create_batch()
    result = external_worker._dispatch(batch["batch_id"], "architect")
    external_worker.state_dir.chmod(0o555)
    try:
        process = subprocess.run(
            [
                sys.executable,
                str(ADAPTER),
                "probe",
                "--repo",
                str(external_worker.repo),
                "--brief",
                str(
                    external_worker._records()
                    / "dispatches"
                    / (result["dispatch_id"] + ".json")
                ),
                "--endpoint",
                "http://127.0.0.1:9",
            ],
            capture_output=True,
            text=True,
        )
        assert process.returncode == 2
        evidence = json.loads(process.stdout)
        assert evidence["ready"] is False
        denied = next(
            check for check in evidence["checks"] if check["requirement"] == "ledger"
        )
        assert denied["state"] == "denied"
        assert denied["reason"] == "Permission denied"
    finally:
        external_worker.state_dir.chmod(0o755)


@posix_runtime
@pytest.mark.parametrize("panel_open", [False, True], ids=["panel-closed", "panel-open"])
def test_native_permission_uses_control_frames_without_extra_model_messages(
    external_worker: recovery_tests.RecoveryTests,
    reachable_endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
    panel_open: bool,
) -> None:
    monkeypatch.setenv("EXAMPLE_API_KEY", "native-secret-716")
    from tests.orchestration.test_startup_lifecycle import cli
    from harness.orchestration.workflow.delivery import respond_native_permission

    batch = external_worker._create_batch()
    brief = external_worker._dispatch(batch["batch_id"], "architect")["brief"]
    trace = external_worker.tmp / "model-messages.json"
    runtime = external_worker.tmp / "permission-runtime"
    received = external_worker.tmp / "runtime-stdin-kinds.json"
    runtime.write_text(
        "#!/usr/bin/env python3\nimport json,sys,signal\nfrom pathlib import Path\n"
        "if '--help' in sys.argv:\n print('--verbose --output-format --input-format --model --effort --permission-mode --permission-prompts --permission-prompt-tool --session-id --tools manual --input-format --permission-prompts');sys.exit(0)\n"
        "signal.alarm(30)\n"
        "kinds=[]\n"
        "def read():\n"
        " message=json.loads(sys.stdin.readline())\n"
        " body=message.get('request') or message.get('response') or {}\n"
        " kinds.append(str(message.get('type'))+':'+str(body.get('subtype','')))\n"
        f" Path({str(received)!r}).write_text(json.dumps(kinds))\n"
        " return message\n"
        "init=read()\n"
        "print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':init['request_id'],'response':{}}}),flush=True)\n"
        "user=read()\n"
        f"Path({str(trace)!r}).write_text(json.dumps([user]))\n"
        "print(json.dumps({'type':'control_request','request_id':'permission-716','request':{'subtype':'can_use_tool','tool_name':'Bash','input':{'command':'git status --short','password':'native-secret-716'}}}),flush=True)\n"
        "answer=read()\n"
        "assert answer['response']['response']['updatedInput']['password']=='native-secret-716'\n"
        "print(json.dumps({'type':'result','is_error':False,'result':answer['response']['response']['behavior']}),flush=True)\n"
    )
    runtime.chmod(0o755)
    cli(
        external_worker,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
        "--adapter",
        str(ADAPTER),
        "--worktree",
        str(external_worker.worktree),
        "--adapter-arg=--visibility=background",
        "--adapter-arg=--runtime-executable=" + str(runtime),
        "--adapter-arg=--endpoint=" + reachable_endpoint,
    )
    observed = {}
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        observed = cli(
            external_worker, "dispatch", "observe", "--dispatch", brief["dispatch_id"]
        )
        if observed["permission_requests"]:
            break
        time.sleep(0.03)
    assert len(observed["permission_requests"]) == 1
    before = trace.read_bytes()
    # The same pending fake runtime remains connected while the actual panel is opened,
    # refreshed, filtered, closed and re-opened. None of those operations may contact a model.
    if panel_open:
        import asyncio
        import socket
        from harness.console.app import HarnessConsoleApp
        from harness.console.screens.worker_observation import WorkerObservationScreen
        from textual.widgets import Select

        def no_network(_socket: socket.socket, address: object) -> None:
            raise AssertionError(f"observation attempted a network request: {address}")

        async def watch() -> None:
            app = HarnessConsoleApp(external_worker.repo)
            async with app.run_test(size=(150, 48)) as pilot:
                for _ in range(2):
                    screen = WorkerObservationScreen(
                        external_worker.repo, brief["dispatch_id"]
                    )
                    await app.push_screen(screen)
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    assert screen.snapshot["permission_requests"]
                    screen.query_one("#worker-filter", Select).value = "permission"
                    await pilot.pause()
                    await pilot.click("#worker-reconnect")
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    await pilot.press("escape")
                    await pilot.pause()

        with monkeypatch.context() as observation_scope:
            observation_scope.setattr(socket.socket, "connect", no_network)
            asyncio.run(watch())
        assert trace.read_bytes() == before
    for _ in range(3):
        cli(external_worker, "dispatch", "observe", "--dispatch", brief["dispatch_id"])
    pending = observed["permission_requests"][0]
    assert pending["tool_name"] == "Bash"
    assert "git status --short" in pending["input_preview"]
    log = Path(observed["log_path"])
    assert b"native-secret-716" not in b"".join(
        path.read_bytes() for path in log.parent.rglob("*") if path.is_file()
    )
    respond_native_permission(
        external_worker.repo,
        brief["dispatch_id"],
        pending["request_id"],
        pending["request_sha256"],
        allow=True,
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = cli(
            external_worker, "dispatch", "observe", "--dispatch", brief["dispatch_id"]
        )
        if result.get("runtime_exit") and any(
            event["text"] == "allow" for event in result["events"]
        ):
            break
        time.sleep(0.03)
    assert any(event["text"] == "allow" for event in result["events"])
    assert result["runtime_exit"]["exit_code"] == 0
    assert result["report_recorded"] is False
    assert trace.read_bytes() == before
    assert len(json.loads(before)) == 1
    # Whether or not a panel was ever opened, the runtime received exactly the protocol
    # handshake, one user message and the operator's permission answer: nothing for display.
    assert json.loads(received.read_text()) == [
        "control_request:initialize",
        "user:",
        "control_response:success",
    ]
