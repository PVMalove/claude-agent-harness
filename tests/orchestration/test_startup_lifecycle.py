"""External startup through the public CLI, real Git, processes and ledger (#716)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.orchestration.core.utils import JsonObject

from tests.orchestration import test_recovery as recovery_tests
from tests.orchestration.test_coordinator import ORCHESTRATION_ROOT


@pytest.fixture
def launch() -> Iterator[recovery_tests.RecoveryTests]:
    fixture = recovery_tests.RecoveryTests()
    fixture.setUp()
    try:
        fixture._use_external_developer()
        path = fixture.repo / ".harness/orchestration.json"
        config = json.loads(path.read_text())
        config["assignment_plans"]["architect"]["transport"] = "external"
        path.write_text(json.dumps(config))
        yield fixture
    finally:
        fixture.tearDown()


def cli(launch: recovery_tests.RecoveryTests, *arguments: str) -> JsonObject:
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATION_ROOT / "coordinator.py"),
            "--repo",
            str(launch.repo),
            "--state-dir",
            str(launch.state_dir),
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def test_send_keeps_the_adapter_receipt_for_observation(
    launch: recovery_tests.RecoveryTests,
) -> None:
    batch = launch._create_batch()
    brief = launch._dispatch(batch["batch_id"], "architect")["brief"]
    adapter = launch.tmp / "adapter.py"
    adapter.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "brief=json.loads(Path(sys.argv[sys.argv.index('--brief')+1]).read_text())\n"
        "print(json.dumps({'dispatch_id':brief['dispatch_id'], 'worker_started':True,"
        "'launch_id':'launch-demo-716'}))\n"
    )
    sent = cli(
        launch,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
        "--adapter",
        str(adapter),
        "--worktree",
        str(launch.worktree),
    )
    assert sent["adapter_receipt"]["launch_id"] == "launch-demo-716"
    status = cli(launch, "dispatch", "status", "--dispatch", brief["dispatch_id"])
    assert status["dispatches"][0]["adapter_receipt"]["launch_id"] == "launch-demo-716"


def test_worker_can_self_report_before_adapter_returns(
    launch: recovery_tests.RecoveryTests,
) -> None:
    batch = launch._create_batch()
    brief = launch._dispatch(batch["batch_id"], "architect")["brief"]
    adapter = launch.tmp / "reporting-adapter.py"
    worker_command = [
        sys.executable,
        str(ORCHESTRATION_ROOT / "coordinator.py"),
        "--repo",
        str(launch.repo),
        "--state-dir",
        str(launch.state_dir),
        "dispatch",
        "self-report",
        "--dispatch",
        brief["dispatch_id"],
        "--model",
        "sonnet",
        "--worktree",
        str(launch.worktree),
    ]
    adapter.write_text(
        "import json,subprocess\n"
        f"result=subprocess.run({worker_command!r},capture_output=True,text=True)\n"
        "print(json.dumps({'worker_started':True,'launch_id':'worker-reporting',"
        "'self_report_exit':result.returncode}))\n"
    )
    cli(
        launch,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
        "--adapter",
        str(adapter),
        "--worktree",
        str(launch.worktree),
    )
    status = cli(launch, "dispatch", "status", "--dispatch", brief["dispatch_id"])
    assert status["dispatches"][0]["state"] == "working"
    assert status["dispatches"][0]["model_self_report"]["match"] is True


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="local /proc identity evidence"
)
@pytest.mark.parametrize("terminal_only", [False, True])
def test_dead_worker_is_observed_before_the_heartbeat_stale_deadline(
    launch: recovery_tests.RecoveryTests,
    terminal_only: bool,
) -> None:
    config_path = launch.repo / ".harness/orchestration.json"
    config = json.loads(config_path.read_text())
    config["approval_policy"] = "auto"
    config["worker_attestation_required"] = True
    config_path.write_text(json.dumps(config))
    batch = launch._create_batch()
    brief = launch._dispatch(batch["batch_id"], "architect")["brief"]
    adapter = launch.tmp / "dead-worker.py"
    adapter.write_text(
        "import json,subprocess,sys,socket,os\nfrom pathlib import Path\n"
        "brief=json.loads(Path(sys.argv[sys.argv.index('--brief')+1]).read_text())\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(.2)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "stat=Path('/proc/'+str(p.pid)+'/stat').read_text().rsplit(')',1)[1].split()\n"
        "identity={'pid':p.pid,'host':socket.gethostname(),'pid_namespace':os.readlink('/proc/self/ns/pid'),'start_time':stat[19],"
        "'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}\n"
        "receipt={'protocol':'harness-worker-v1','dispatch_id':brief['dispatch_id'],"
        "'worker_started':True,'launch_id':'launch-dead-716','process':identity}\n"
        "p.wait()\n"
        f"if {terminal_only!r}:\n"
        f" root=Path({str(launch.repo / '.harness/.sandboxes/logs/workers/launch-dead-716')!r});root.mkdir(parents=True)\n"
        " receipt.pop('process');receipt.update(log_path=str(root/'events.jsonl'),terminal_path=str(root/'terminal.json'))\n"
        " (root/'terminal.json').write_text(json.dumps({'dispatch_id':brief['dispatch_id'],'launch_id':'launch-dead-716','exit_code':p.returncode}))\n"
        "print(json.dumps(receipt))\n"
    )
    cli(
        launch,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
        "--adapter",
        str(adapter),
        "--worktree",
        str(launch.worktree),
    )
    status = cli(launch, "dispatch", "status", "--dispatch", brief["dispatch_id"])[
        "dispatches"
    ][0]
    assert status["stale"] is False
    assert status["startup"]["worker_state"] == "stopped"
    assert status["startup"]["classification"] == "runtime-crash"
    assert status["startup"]["restart_allowed"] is True
    event = cli(
        launch,
        "dispatch",
        "wait",
        "--dispatch",
        brief["dispatch_id"],
        "--timeout",
        "1",
        "--poll-interval",
        "1",
    )
    assert event["event"] == "startup_failed"
    assert cli(launch, "ledger", "validate")["valid"] is True

    observed = cli(launch, "batch", "auto-report", "--batch", batch["batch_id"])
    assert observed["observed_stop"]["reason"] == "runtime-unavailable"
    decided = cli(launch, "batch", "auto-decide", "--batch", batch["batch_id"])
    assert decided["batch_state"] == "paused"
    from harness.orchestration import coordinator

    recovery_args = launch._args(
        batch=batch["batch_id"],
        restart=True,
        note="restart the proven stopped worker under the same contract",
        **launch._approval(),
    )
    resumed = coordinator.resume_stopped_batch(recovery_args)
    renewed = resumed["restart_dispatch"]["brief"]
    assert renewed["dispatch_id"] != brief["dispatch_id"]
    assert renewed["snapshot_commit"] == brief["snapshot_commit"]
    assert renewed["write_paths"] == brief["write_paths"]
    assert (
        renewed["coordinator_approval"]["approved_by"]
        == launch._approval()["approved_by"]
    )
    repeated = coordinator.resume_stopped_batch(recovery_args)
    assert repeated["restart_dispatch_id"] == renewed["dispatch_id"]
    assert cli(launch, "ledger", "validate")["valid"] is True


def test_startup_timeout_never_proves_termination_for_legacy_adapter(
    launch: recovery_tests.RecoveryTests,
) -> None:
    import time

    config_path = launch.repo / ".harness/orchestration.json"
    config = json.loads(config_path.read_text())
    config["execution_policy"] = {"worker_startup_timeout_seconds": 1}
    config_path.write_text(json.dumps(config))
    batch = launch._create_batch()
    brief = launch._dispatch(batch["batch_id"], "architect")["brief"]
    adapter = launch.tmp / "legacy-adapter.py"
    adapter.write_text("pass\n")
    cli(
        launch,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
        "--adapter",
        str(adapter),
        "--worktree",
        str(launch.worktree),
    )
    time.sleep(1.05)
    status = cli(launch, "dispatch", "status", "--dispatch", brief["dispatch_id"])[
        "dispatches"
    ][0]
    assert status["startup"]["deadline_exceeded"] is True
    assert status["startup"]["restart_allowed"] is False
    assert status["state"] == "dispatched"
    event = cli(
        launch, "dispatch", "wait", "--dispatch", brief["dispatch_id"], "--timeout", "1"
    )
    assert event["event"] == "startup_timeout"
    assert event["startup"]["restart_allowed"] is False


def test_observe_reads_incremental_runtime_output_without_exposing_thinking(
    launch: recovery_tests.RecoveryTests,
) -> None:
    batch = launch._create_batch()
    brief = launch._dispatch(batch["batch_id"], "architect")["brief"]
    log = launch.repo / ".harness/.sandboxes/logs/workers/stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "private reasoning"},
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"secret": "must not appear"},
                        },
                        {"type": "text", "text": "Reading startup code"},
                    ]
                },
            }
        )
        + "\n"
    )
    adapter = launch.tmp / "stream-adapter.py"
    receipt = {
        "protocol": "harness-worker-v1",
        "dispatch_id": brief["dispatch_id"],
        "launch_id": "launch-stream-716",
        "worker_started": True,
        "log_path": str(log),
    }
    adapter.write_text("import json\nprint(json.dumps(" + repr(receipt) + "))\n")
    cli(
        launch,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
        "--adapter",
        str(adapter),
        "--worktree",
        str(launch.worktree),
    )
    first = cli(launch, "dispatch", "observe", "--dispatch", brief["dispatch_id"])
    assert [row["text"] for row in first["events"]] == ["Read", "Reading startup code"]
    assert first["runtime"] == brief["resolved_runtime"]
    assert first["effort"] == brief["resolved_effort"]
    assert first["snapshot_commit"] == brief["snapshot_commit"]
    assert "private reasoning" not in json.dumps(first)
    assert "must not appear" not in json.dumps(first)
    with log.open("a") as output:
        output.write(
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "result": "Native permission denied",
                }
            )
            + "\n"
        )
    second = cli(
        launch,
        "dispatch",
        "observe",
        "--dispatch",
        brief["dispatch_id"],
        "--offset",
        str(first["next_offset"]),
    )
    assert len(second["events"]) == 1
    assert "permission denied" in second["events"][0]["text"]
    assert second["dispatch_id"] == first["dispatch_id"]


def test_receipt_does_not_forward_unrelated_adapter_output(
    launch: recovery_tests.RecoveryTests,
) -> None:
    batch = launch._create_batch()
    brief = launch._dispatch(batch["batch_id"], "architect")["brief"]
    adapter = launch.tmp / "noisy-adapter.py"
    adapter.write_text(
        "import json\nprint(json.dumps({'launch_id':'visible-launch','worker_started':True,'private_setting':'must-not-be-shown'}))\n"
    )
    result = cli(
        launch,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
        "--adapter",
        str(adapter),
        "--worktree",
        str(launch.worktree),
    )
    assert result["adapter_receipt"]["launch_id"] == "visible-launch"
    assert "must-not-be-shown" not in json.dumps(result)


def test_native_handoff_can_record_worker_startup_without_holding_ledger_lock(
    launch: recovery_tests.RecoveryTests,
) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime
    from harness.orchestration import coordinator, extensions

    class NativeRuntime:
        def observe(
            self, plan: JsonObject, transport: str
        ) -> extensions.RuntimeAccessObservation:
            return extensions.RuntimeAccessObservation(
                plan_digest=plan["plan_digest"],
                supported_modes=("sandbox",),
                effective_mode="sandbox",
                mechanism="native-apply",
                environment_id="worker-context",
                launch_id="native-launch",
                source="native-runtime",
                observed_at=datetime.now(UTC).isoformat(),
                transport=transport,
                hosts=tuple(plan["network"]["hosts"]),
                filesystem=tuple(
                    (item["path"], item["access"]) for item in plan["requirements"]
                ),
            )

        def apply(
            self, brief: JsonObject, observation: extensions.RuntimeAccessObservation
        ) -> extensions.RuntimeAccessObservation:
            return replace(observation, applied=True)

        def handoff(
            self,
            brief: JsonObject,
            observation: extensions.RuntimeAccessObservation,
            command: tuple[str, ...] | None,
        ) -> extensions.RuntimeAccessObservation:
            coordinator.self_report_dispatch(
                launch._args(
                    dispatch=brief["dispatch_id"],
                    model="sonnet",
                    worktree=str(launch.worktree),
                )
            )
            return replace(
                observation, handed_off=True, dispatch_id=brief["dispatch_id"]
            )

    extensions.register("runtime_access", "test-native-unlocked", NativeRuntime())
    try:
        (launch.repo / ".harness/orchestration.json").write_text(
            json.dumps(
                {
                    "access_policy": {"defaults": {"mode": "sandbox"}},
                    "extensions": {"runtime_access": "test-native-unlocked"},
                }
            )
        )
        batch = launch._create_batch()
        brief = launch._dispatch(batch["batch_id"], "architect")["brief"]
        coordinator.send_dispatch(
            launch._args(
                dispatch=brief["dispatch_id"],
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )
        status = coordinator.dispatch_status(
            launch._args(dispatch=brief["dispatch_id"], batch=None, stale_after=None)
        )["dispatches"][0]
        assert status["state"] == "working"
        assert status["model_self_report"]["match"] is True
    finally:
        extensions.unregister("runtime_access", "test-native-unlocked")


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="local Linux process proof"
)
def test_process_in_another_pid_namespace_is_not_a_local_worker() -> None:
    import os
    from harness.orchestration.worker_observation import (
        observe_process,
        process_identity,
    )

    identity = process_identity(os.getpid())
    assert identity is not None
    receipt = {
        "protocol": "harness-worker-v1",
        "dispatch_id": "dispatch-local",
        "launch_id": "launch-local",
        "process": identity,
    }
    live = observe_process(receipt, "dispatch-local")
    assert live is not None and live["state"] == "live"
    receipt["process"] = {**identity, "pid_namespace": "foreign-namespace"}
    foreign = observe_process(receipt, "dispatch-local")
    assert foreign is not None and foreign["state"] == "unknown"


@pytest.mark.parametrize(
    "corruption", ["dispatch", "launch", "exit", "path", "protocol"]
)
def test_foreign_or_malformed_terminal_never_proves_exit(
    tmp_path: Path, corruption: str
) -> None:
    from harness.orchestration.worker_observation import observe_worker
    from harness.storage import storage_path

    root = storage_path(tmp_path, "logs", "workers") / "launch-bound"
    root.mkdir(parents=True)
    terminal = root / "terminal.json"
    record = {
        "dispatch_id": "dispatch-bound",
        "launch_id": "launch-bound",
        "exit_code": 0,
    }
    receipt = {
        "protocol": "harness-worker-v1",
        "dispatch_id": "dispatch-bound",
        "launch_id": "launch-bound",
        "log_path": str(root / "events.jsonl"),
        "terminal_path": str(terminal),
    }
    terminal.write_text(json.dumps(record))
    stopped = observe_worker(receipt, "dispatch-bound", repo=tmp_path)
    assert stopped is not None and stopped["state"] == "stopped"
    if corruption == "dispatch":
        record["dispatch_id"] = "dispatch-other"
    elif corruption == "launch":
        record["launch_id"] = "launch-other"
    elif corruption == "exit":
        record["exit_code"] = True
    elif corruption == "path":
        receipt["terminal_path"] = str(tmp_path / "terminal.json")
    else:
        receipt["protocol"] = "other-protocol"
    terminal.write_text(json.dumps(record))
    observed = observe_worker(receipt, "dispatch-bound", repo=tmp_path)
    assert observed is None or observed["state"] == "unknown"
