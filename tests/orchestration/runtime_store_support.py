"""Shared helpers of the runtime-store seam tests: a 1.x state written by the real Python ledger,
and the real ``harness-runtime`` binary."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness.orchestration.core.constants import BatchState, DispatchState
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    CheckpointRecord,
    DispatchRecord,
    DispatchStatusRecord,
    JsonObject,
    JsonValue,
    LifecycleLedger,
    PlanRecord,
    _canonical,
)

ROOT = Path(__file__).resolve().parents[2]
STATE_REL = Path(".harness/orchestration/state")
BATCH_ID = "batch-aaaaaaaa-0000-4000-8000-000000000001"
SNAPSHOT_COMMIT = "4" * 40
DISPATCH_IDS = (
    "dispatch-11111111-0000-4000-8000-000000000001",
    "dispatch-22222222-0000-4000-8000-000000000002",
)


def runtime_binary() -> Path:
    configured = os.environ.get("HARNESS_RUNTIME_BIN")
    name = "harness-runtime.exe" if sys.platform == "win32" else "harness-runtime"
    binary = (
        Path(configured)
        if configured
        else ROOT / "native/harness-runtime/target/debug" / name
    )
    if binary.is_file():
        return binary
    message = (
        f"build harness-runtime first (cargo build in native/harness-runtime): {binary}"
    )
    if os.environ.get("HARNESS_REQUIRE_RUNTIME") == "1":
        pytest.fail(message)
    pytest.skip(message)


def plan_fields(batch_id: str = BATCH_ID) -> JsonObject:
    """The fields a batch and its immutable plan share, as the coordinator writes them: a state
    that lacks one is a batch ``ledger validate`` rejects."""
    return {
        "batch_id": batch_id,
        "created_at": "2026-10-01T00:00:00+00:00",
        "base_commit": "1" * 40,
        "integration_ref": "integration/example",
        "branch_start_commit": "2" * 40,
        "ticket": "#1",
        "branch": "feature/issue-1-example",
        "worktree": "/var/projects/example/.harness/.sandboxes/worktrees/issue-1-example",
        "zone": None,
        "allowed_paths": ["a/**"],
        "definition_of_done": ["The example is done."],
        "prohibited_changes": [],
        "developer_verification_commands": ["make test"],
        "verification_commands": ["make test"],
        "required_gates": ["review", "qa"],
        "dependencies": [],
        "approval_policy": "manual",
        "communication_policy": {
            "agent_to_agent_language": "en",
            "coordinator_report_language": "ru",
        },
        "scope_preflight": {"status": "clean", "outside_allowed_paths": []},
        "harness_runtime_sha256": "3" * 64,
        "write_paths": ["a/**"],
    }


def _digest(value: JsonObject) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def build_state(
    repo: Path,
    *,
    second_status: DispatchState = DispatchState.APPROVED,
    owes_reassessment_without_candidate: bool = False,
) -> LifecycleLedger:
    """A repository whose state directory a 1.x installation wrote: one batch of two dispatches,
    the first reported."""
    ledger = LifecycleLedger(repo / STATE_REL)
    ledger.ensure()
    generation = ledger.records_root()
    first, second = DISPATCH_IDS
    dispatches = [
        DispatchRecord(
            dispatch_id,
            BATCH_ID,
            DispatchState.APPROVED,
            {"approved_at": "2026-10-01T00:00:00+00:00"},
            {
                "role": role,
                "purpose": "work",
                "note": "café \U0001f600",
                "ratio": 1e-05,
                "snapshot_commit": SNAPSHOT_COMMIT,
                "definition_of_done": ["The example is done."],
                "dependencies": [],
                "write_paths": ["a/**"],
                "prohibited_changes": [],
            },
        )
        for dispatch_id, role in ((first, "developer"), (second, "qa"))
    ]
    report: JsonObject = {
        "dispatch_id": first,
        "outcome": "completed",
        "output": "done",
    }
    ledger.write_immutable(
        generation / "reports" / f"{first}.json", report, artifact=True
    )
    entries: list[JsonValue] = [
        {
            "dispatch_id": first,
            "state": "reported",
            "brief_sha256": _digest(dispatches[0].to_dict()),
            "report": f"reports/{first}.json",
            "report_sha256": _digest(report),
        },
        {
            "dispatch_id": second,
            "state": "approved",
            "brief_sha256": _digest(dispatches[1].to_dict()),
        },
    ]
    batch_fields = plan_fields()
    reassessment: JsonObject = (
        {"risk_reassessment_required": True}
        if owes_reassessment_without_candidate
        else {
            "risk_reassessment_required": True,
            "risk_reassessment_candidate": "a" * 40,
            "risk_reassessment_triggers": ["transactions"],
        }
    )
    ledger.write_record(PlanRecord(BATCH_ID, dict(batch_fields)))
    ledger.write_record(
        BatchRecord(
            BATCH_ID, BatchState.ACTIVE, entries, None, {**batch_fields, **reassessment}
        )
    )
    for record in dispatches:
        ledger.write_record(record)
    ledger.write_record(DispatchStatusRecord(first, DispatchState.REPORTED, {}))
    ledger.write_record(DispatchStatusRecord(second, second_status, {}))
    ledger.write_record(CheckpointRecord("checkpoint-0001", {"dispatch_id": first}))
    return ledger


def migrate(
    repo: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """``harness-runtime migrate --repo <repo>`` as its own process."""
    return subprocess.run(
        [str(runtime_binary()), "migrate", "--repo", str(repo)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def tree(root: Path, *, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    """Every file below ``root`` and its bytes, keyed by ``/`` path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.relative_to(root).as_posix().startswith(skip)
    }


def coordinator(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """The 1.x coordinator CLI against ``repo``."""
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "harness/orchestration/coordinator.py"),
            "--repo",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def load(path: Path) -> JsonObject:
    loaded: JsonObject = json.loads(path.read_text(encoding="utf-8"))
    return loaded


class PausedMigrate:
    """A debug-build ``harness-runtime migrate`` that waits at one step boundary.

    ``HARNESS_RUNTIME_TEST_PAUSE_AT`` makes the process print ``PAUSED:<step>`` and wait for a line
    on stdin; a test kills it there, or changes the state first and then resumes it."""

    def __init__(
        self, repo: Path, step: str, *, env: dict[str, str] | None = None
    ) -> None:
        self.step = step
        self.process = subprocess.Popen(
            [str(runtime_binary()), "migrate", "--repo", str(repo)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "HARNESS_RUNTIME_TEST_PAUSE_AT": step, **(env or {})},
        )
        self._lines: list[str] = []
        self._paused = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        for line in stderr:
            self._lines.append(line)
            if line.strip() == f"PAUSED:{self.step}":
                self._paused.set()

    def wait_paused(self, timeout: float = 60.0) -> None:
        if not self._paused.wait(timeout):
            self.process.kill()
            pytest.fail(
                f"migrate did not pause at {self.step!r}: {''.join(self._lines)!r}"
            )

    def kill(self) -> None:
        """SIGKILL: the process cannot clean up anything."""
        self.process.kill()
        self.process.wait(timeout=30)
        self._reader.join(timeout=30)

    def resume(self) -> tuple[int, str, str]:
        """Let the process go on and return its exit code, stdout and stderr."""
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write("\n")
        stdin.flush()
        stdin.close()
        stdout = self.process.stdout
        assert stdout is not None
        text = stdout.read()
        code = self.process.wait(timeout=60)
        self._reader.join(timeout=30)
        return code, text, "".join(self._lines)


def authorities(repo: Path) -> dict[str, bool]:
    """Which store is the authority of the state directory: the pointer decides."""
    state = repo / STATE_REL
    pointer = load(state / "ledger.json")
    marker = state / "AUTHORITY.json"
    return {
        "file-ledger": pointer["version"] == 3,
        "runtime-store": pointer["version"] == 4
        and marker.is_file()
        and load(marker)["authority"] == "runtime-store"
        and (state / "runtime.db").is_file(),
    }


# -- a repository whose state the real coordinator wrote ---------------------------------------------

ORCHESTRATION_ROOT = ROOT / "harness" / "orchestration"
APPROVAL = {"approved_by": "Malove", "approved_at": "2026-09-17T00:00:00+00:00"}
# The architect dispatch of the seam batches, as ``dispatch create`` takes it.
ARCHITECT_DISPATCH = {
    "role": "architect",
    "runtime": "claude",
    "purpose": "work",
    "model": "sonnet",
    "effort": "high",
}


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def coordinator_repository(tmp: Path) -> Path:
    """A git repository with an origin, a project config and the architect role manifest: what
    ``create_batch`` and ``create_dispatch`` touch for real."""
    origin = tmp / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)], check=True, capture_output=True
    )
    repo = tmp / "work"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (repo / "README.md").write_text("# Readme\n", encoding="utf-8")
    (repo / ".harness").mkdir()
    (repo / ".harness" / "project.json").write_text(
        json.dumps({"qa_gate_commands": ["true"]}), encoding="utf-8"
    )
    roles = repo / ".harness" / "orchestration" / "roles"
    roles.mkdir(parents=True)
    for name in ("architect", "developer"):
        (roles / f"{name}.md").write_text(
            (ORCHESTRATION_ROOT / "roles" / f"{name}.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (repo / ".gitignore").write_text(
        ".harness/orchestration/state/\n.harness/orchestration/state-snapshots/\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "branch", "-M", "master")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "master")
    return repo


def write_coordinator_state(
    tmp: Path,
    *,
    report: bool = True,
    ticket: str = "#797",
    state_from: Path | None = None,
) -> tuple[Path, dict[str, str]]:
    """The state of a real batch: planned, approved, one architect dispatch created and, unless
    ``report`` is false, sent, self-reported and reported (with ``report`` false it stays approved,
    which the cutover accepts). Returns the repository and the ids.

    ``state_from`` is a state directory the batch is added to, such as a copy of the state of a
    real repository: the measurement runs on a ledger of that size."""
    import argparse

    from harness.orchestration import coordinator

    repo = coordinator_repository(tmp)
    if state_from is not None:
        shutil.copytree(state_from, repo / STATE_REL)
        # The copied ledger holds the batch that is open there; a new batch needs room beside it.
        (repo / ".harness" / "orchestration.json").write_text(
            json.dumps(
                {
                    "provider_profiles": {},
                    "assignment_plans": {},
                    "concurrency_budget": 99,
                    "verification_commands": [],
                }
            ),
            encoding="utf-8",
        )
    ids, brief = add_coordinator_batch(repo, tmp / "worktree", ticket=ticket)
    dispatch_id = ids["dispatch_id"]

    def args(**values: object) -> argparse.Namespace:
        return argparse.Namespace(repo=str(repo), state_dir=None, **values)

    if not report:
        # An approved dispatch is no live worker: the cutover accepts this state.
        return repo, ids
    launched = coordinator.send_dispatch(
        args(dispatch=dispatch_id, adapter=None, adapter_arg=None, checkout=None)
    )
    coordinator.self_report_dispatch(
        args(dispatch=dispatch_id, model="sonnet", worktree=launched["worker_worktree"])
    )
    path = architect_report_file(repo, dispatch_id, brief)
    coordinator.submit_report(args(file=str(path)))
    return repo, ids


def add_coordinator_batch(
    repo: Path, worktree: Path, *, ticket: str, dispatch: bool = True
) -> tuple[dict[str, str], JsonObject]:
    """A planned and approved batch of the file ledger of ``repo`` with one approved architect
    dispatch, in its own issue branch and ``worktree``: the ids and the brief. Without
    ``dispatch`` the batch stays planned, and the ids and the brief name no dispatch."""
    import argparse

    from harness.orchestration import coordinator

    branch = f"feature/issue-{ticket.removeprefix('#')}-seam"
    _git(repo, "worktree", "add", "-b", branch, str(worktree), "master")

    def args(**values: object) -> argparse.Namespace:
        return argparse.Namespace(repo=str(repo), state_dir=None, **values)

    batch = coordinator.create_batch(
        args(
            ticket=ticket,
            goal=None,
            branch=branch,
            worktree=str(worktree),
            allowed_path=["**"],
            integration_ref="master",
            definition_of_done=["read from the store"],
            prohibited_change=["secrets"],
            required_gate=None,
            dependency=None,
            expected_file=["services/x.py"],
            expected_service=["core"],
            expected_changed_lines=10,
            expected_context_tokens=None,
        )
    )
    batch_id = str(batch["batch_id"])
    coordinator.approve_batch(args(batch=batch_id, **APPROVAL))
    if not dispatch:
        return {"batch_id": batch_id}, {}
    fields = {
        "batch": batch_id,
        **ARCHITECT_DISPATCH,
        "candidate_commit": None,
        "delta_review_of": None,
    }
    proposal = coordinator.create_dispatch(args(propose=True, **fields))
    created = coordinator.create_dispatch(
        args(transition_digest=proposal["transition_digest"], **fields, **APPROVAL)
    )
    brief = created["brief"]
    assert isinstance(brief, dict)
    return {"batch_id": batch_id, "dispatch_id": str(brief["dispatch_id"])}, brief


def architect_report_file(repo: Path, dispatch_id: str, brief: JsonObject) -> Path:
    """The completion report of an architect dispatch, staged in the agent inbox."""
    from harness.orchestration.core import workspace

    commands = brief["verification_commands"]
    assert isinstance(commands, list)
    report = {
        "dispatch_id": dispatch_id,
        "ticket": brief["ticket"],
        "role": "architect",
        "outcome": "completed",
        "output": "done — café",
        "commit_sha": "not applicable — read-only role",
        "changed_files": [],
        "checks_run": [
            {"command": command, "result": "pass", "evidence": "n/a"}
            for command in commands
        ],
        "risks": {"status": "none", "details": ""},
        "blockers": {"status": "none", "details": ""},
        "next_coordinator_action": "decide",
        "report_language": "ru",
    }
    path = workspace._prepare_agent_inbox(repo) / f"{dispatch_id}.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


AUDITING = """
import json, os, runpy, sys
FORBIDDEN = (".coordinator.lock", "runtime.db", "/generations/")
CALLS = {"open", "os.scandir", "os.listdir", "os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.replace", "os.stat"}
seen = []

def hook(event, arguments):
    if event not in CALLS:
        return
    text = " ".join(str(a) for a in arguments if isinstance(a, (str, bytes, os.PathLike)))
    if any(marker in text for marker in FORBIDDEN):
        seen.append([event, text])

sys.addaudithook(hook)
script = sys.argv[1]
sys.argv = [script] + sys.argv[2:]
code = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exit_:
    code = exit_.code or 0
finally:
    with open(os.environ["AUDIT_LOG"], "w", encoding="utf-8") as out:
        json.dump(seen, out)
sys.exit(code)
"""


def audited(
    repo: Path, runtime: ServingRuntime | None, command: list[str], log: Path
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run one coordinator command with an audit hook that logs every use of the file ledger."""
    environ = {"XDG_RUNTIME_DIR": str(runtime.xdg)} if runtime else {}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            AUDITING,
            str(ROOT / "harness/orchestration/coordinator.py"),
            "--repo",
            str(repo),
            *command,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "AUDIT_LOG": str(log),
            **environ,
        },
    )
    events: list[list[str]] = json.loads(log.read_text(encoding="utf-8"))
    return result, events


# -- a real runtime process serving one repository ----------------------------------------------------

SHORT_TMP = (
    "/tmp"
    if sys.platform != "win32" and os.access("/tmp", os.W_OK | os.X_OK)
    else tempfile.gettempdir()
)


class ServingRuntime:
    """``harness-runtime serve --repo <repo>``, with a runtime directory short enough for a socket."""

    def __init__(
        self,
        repo: Path,
        *,
        env: dict[str, str] | None = None,
        xdg: Path | None = None,
    ) -> None:
        """``env`` sets the debug hooks of the runtime (``HARNESS_RUNTIME_TEST_*``); ``xdg`` is
        the runtime directory of an earlier runtime that a new one takes over after a kill."""
        self.xdg = xdg or Path(tempfile.mkdtemp(prefix="hr", dir=SHORT_TMP)).resolve()
        self.environ = {"XDG_RUNTIME_DIR": str(self.xdg)}
        self.process = subprocess.Popen(
            [str(runtime_binary()), "serve", "--repo", str(repo)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.environ, **(env or {})},
        )
        stdout = self.process.stdout
        assert stdout is not None
        lines: queue.Queue[str] = queue.Queue()
        threading.Thread(
            target=lambda: lines.put(stdout.readline()), daemon=True
        ).start()
        self._paused: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            text = lines.get(timeout=30)
        except queue.Empty:
            self.stop()
            pytest.fail("harness-runtime printed no readiness line within 30 seconds")
        assert json.loads(text)["status"] == "ready"

    def _read_stderr(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        for line in stderr:
            if line.startswith("PAUSED:"):
                self._paused.put(line.strip().removeprefix("PAUSED:"))

    def wait_paused(self, step: str, timeout: float = 30.0) -> None:
        """Wait until the runtime stops at the debug hook ``step`` (``HARNESS_RUNTIME_TEST_PAUSE_AT``)."""
        try:
            reached = self._paused.get(timeout=timeout)
        except queue.Empty:
            self.stop()
            pytest.fail(
                f"the runtime did not pause at {step!r} within {timeout} seconds"
            )
        assert reached == step

    def resume(self) -> None:
        """Let the paused request go on."""
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write("\n")
        stdin.flush()

    def kill(self) -> None:
        """SIGKILL: the runtime cannot clean up anything; its directory stays for a successor."""
        self.process.kill()
        self.process.wait(timeout=30)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=30)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
        shutil.rmtree(self.xdg, ignore_errors=True)


def store_call(
    repo: Path,
    runtime: ServingRuntime,
    operation: str,
    payload: JsonObject | None = None,
    *,
    operation_id: str | None = None,
    batch_id: str | None = None,
    expected_revision: int | None = None,
) -> JsonObject:
    """One request of a test to the runtime of ``repo``; the answer is the result of the call.

    A refusal is raised as ``RuntimeClientError``."""
    import uuid

    from harness.orchestration.runtime_client import RuntimeClient

    reply = RuntimeClient(repo, environ=runtime.environ).call(
        operation,
        operation_id=operation_id or f"{operation}-{uuid.uuid4().hex}",
        payload=payload,
        batch_id=batch_id,
        expected_revision=expected_revision,
        timeout_seconds=30.0,
    )
    return {**reply.result, "_revision": reply.revision}


def store_records(
    repo: Path, runtime: ServingRuntime, *paths: str
) -> dict[str, JsonObject]:
    """The records of the runtime store at ``paths``."""
    answer = store_call(repo, runtime, "state.read", {"paths": list(paths)})
    records = answer["records"]
    assert isinstance(records, dict)
    return {
        path: record for path, record in records.items() if isinstance(record, dict)
    }


def store_revisions(repo: Path, runtime: ServingRuntime, *paths: str) -> dict[str, int]:
    answer = store_call(repo, runtime, "state.read", {"paths": list(paths)})
    revisions = answer["revisions"]
    assert isinstance(revisions, dict)
    found: dict[str, int] = {}
    for path, revision in revisions.items():
        assert isinstance(revision, int)
        found[path] = revision
    return found


class Store:
    """A cut-over repository with a working dispatch and the runtime that serves it."""

    def __init__(self, tmp: Path) -> None:
        self.repo, ids = write_coordinator_state(tmp, report=False)
        self.batch = ids["batch_id"]
        self.dispatch = ids["dispatch_id"]
        result = migrate(self.repo)
        assert result.returncode == 0, result.stderr
        self.runtime = ServingRuntime(self.repo)
        # Both commands write through the runtime: the dispatch becomes dispatched, then working.
        sent = self.run("dispatch", "send", "--dispatch", self.dispatch)
        assert sent.returncode == 0, sent.stderr
        worktree = json.loads(sent.stdout)["worker_worktree"]
        reported = self.run(
            "dispatch",
            "self-report",
            "--dispatch",
            self.dispatch,
            "--model",
            "sonnet",
            "--worktree",
            worktree,
        )
        assert reported.returncode == 0, reported.stderr

    @property
    def batch_path(self) -> str:
        return f"batches/{self.batch}.json"

    @property
    def status_path(self) -> str:
        return f"dispatch-status/{self.dispatch}.json"

    @property
    def database(self) -> Path:
        return self.repo / STATE_REL / "runtime.db"

    def run(self, *command: str) -> subprocess.CompletedProcess[str]:
        return coordinator_with(self.repo, self.runtime, *command)

    def heartbeat(self) -> subprocess.CompletedProcess[str]:
        return self.run("dispatch", "heartbeat", "--dispatch", self.dispatch)

    def revisions(self, *paths: str) -> dict[str, int]:
        return store_revisions(self.repo, self.runtime, *paths)

    def audit_records(self) -> int:
        """The revision of the store: the sequence number of the last audit record."""
        revision = store_call(self.repo, self.runtime, "state.read")["_revision"]
        assert isinstance(revision, int)
        return revision

    def validate(self, *, full: bool = True) -> JsonObject:
        return store_call(self.repo, self.runtime, "state.validate", {"full": full})

    def report_file(self) -> Path:
        path = f"dispatches/{self.dispatch}.json"
        brief = store_records(self.repo, self.runtime, path)[path]
        return architect_report_file(self.repo, self.dispatch, brief)

    def submit_report(self) -> subprocess.CompletedProcess[str]:
        return self.run("report", "submit", "--file", str(self.report_file()))

    def stop_runtime(self) -> None:
        """Stop the runtime and remove its directory."""
        self.runtime.stop()

    def restart_runtime(self, env: dict[str, str] | None = None) -> None:
        """Kill the runtime and start another in the same runtime directory, with debug hooks
        ``env``: the hooks fire once for each process, so a setup that wrote already is past them."""
        xdg = self.runtime.xdg
        self.runtime.kill()
        self.runtime = ServingRuntime(self.repo, env=env, xdg=xdg)

    def start(self, *command: str) -> subprocess.Popen[str]:
        """A coordinator command that runs while the test goes on."""
        return coordinator_popen(self.repo, self.runtime, *command)


def coordinator_popen(
    repo: Path, runtime: ServingRuntime, *args: str
) -> subprocess.Popen[str]:
    """The coordinator CLI against ``repo`` as a process that the test waits for later."""
    return subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "harness/orchestration/coordinator.py"),
            "--repo",
            str(repo),
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT), **runtime.environ},
    )


def coordinator_with(
    repo: Path, runtime: ServingRuntime | None, *args: str, python: str | None = None
) -> subprocess.CompletedProcess[str]:
    """The coordinator CLI against ``repo``, reaching the runtime of ``runtime``."""
    environ = {"XDG_RUNTIME_DIR": str(runtime.xdg)} if runtime else {}
    command = (
        [sys.executable, "-c", python]
        if python
        else [sys.executable, str(ROOT / "harness/orchestration/coordinator.py")]
    )
    return subprocess.run(
        [*command, "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT), **environ},
    )


# -- an external dispatch the runtime supervises ------------------------------------------------------

# The fake adapter of the supervised seam: it logs its launch, ignores SIGTERM when asked (or counts
# it in ``terms.log``), starts a member of its group that waits for ``member_release`` when asked,
# waits in the foreground for ``release`` and exits with the code in ``exit_code``.
FAKE_ADAPTER = """
import json, os, signal, subprocess, sys, time
from pathlib import Path
control = Path({control!r})
if (control / "ignore_term").exists():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
def count_term(*_):
    with open(control / "terms.log", "a", encoding="utf-8") as log:
        log.write("TERM\\n")
if (control / "count_term").exists():
    signal.signal(signal.SIGTERM, count_term)
if (control / "member").exists():
    wait = "while [ ! -e member_release ]; do sleep 0.05; done"
    member = subprocess.Popen(["sh", "-c", wait], cwd=control, stdin=subprocess.DEVNULL)
    (control / "member.pid").write_text(str(member.pid), encoding="utf-8")
with open(control / "launches.log", "a", encoding="utf-8") as log:
    log.write(json.dumps({{"pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgid(0),
                          "operation_id": os.environ.get("HARNESS_OPERATION_ID"),
                          "argv": sys.argv[1:]}}) + "\\n")
while not (control / "release").exists():
    time.sleep(0.05)
code = control / "exit_code"
sys.exit(int(code.read_text()) if code.exists() else 0)
"""


def _supervised_config(execution_policy: JsonObject) -> JsonObject:
    runtime: JsonObject = {
        "claude": {"profiles": ["p"], "model": "sonnet", "effort": "high"}
    }
    return {
        "provider_profiles": {
            "p": {
                "capabilities": [
                    "architecture-analysis",
                    "backend-development",
                    "code-review",
                ],
                "fallback": [],
                "known_limitations": ["none"],
            }
        },
        "assignment_plans": {
            "architect": {
                "zone": "repository",
                "runtimes": runtime,
                "transport": "external",
            },
            "developer": {"zone": "repository", "runtimes": runtime},
            "code-review": {"zone": "repository", "runtimes": runtime},
        },
        "backend_zones": {"repository": {"paths": ["**"]}},
        "concurrency_budget": 1,
        "verification_commands": ["true"],
        "execution_policy": execution_policy,
    }


def labelled_groups(dispatch_id: str) -> set[int]:
    """The process groups whose leader carries the label of ``dispatch_id``."""
    groups: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes().split(b"\0")
            if f"HARNESS_DISPATCH_ID={dispatch_id}".encode() in environ:
                groups.add(os.getpgid(int(entry.name)))
        except OSError:
            continue
    return groups


@dataclass(frozen=True)
class Launch:
    """One start of the fake adapter, as it logged itself."""

    pid: int
    ppid: int
    pgid: int
    operation_id: str | None


class Supervised:
    """A cut-over repository whose external architect dispatch was created after the cutover, so
    its approved brief pins ``execution``; the runtime that serves it, and a fake foreground
    adapter driven through ``control``. ``close`` kills every process group the dispatch left."""

    def __init__(
        self,
        tmp: Path,
        *,
        timeout: int = 600,
        grace: int = 1,
        env: dict[str, str] | None = None,
    ) -> None:
        repo = coordinator_repository(tmp)
        role = repo / ".harness" / "orchestration" / "roles" / "code-review.md"
        role.write_text(
            (ORCHESTRATION_ROOT / "roles" / "code-review.md").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "the review role")
        (repo / ".harness" / "orchestration.json").write_text(
            json.dumps(
                _supervised_config(
                    {
                        "worker_execution_timeout_seconds": timeout,
                        "stop_grace_seconds": grace,
                        "dispatch_poll_interval_seconds": 1,
                    }
                )
            ),
            encoding="utf-8",
        )
        ids, _ = add_coordinator_batch(
            repo, tmp / "worktree", ticket="#805", dispatch=False
        )
        self.repo = repo
        self.batch = ids["batch_id"]
        assert migrate(repo).returncode == 0
        self.control = tmp / "control"
        self.control.mkdir()
        self.adapter = tmp / "adapter.py"
        self.adapter.write_text(
            FAKE_ADAPTER.format(control=str(self.control)), encoding="utf-8"
        )
        self.runtime = ServingRuntime(repo, env=env)
        self.dispatch = ""
        self.dispatch = self._create()

    def _create(self) -> str:
        fields = ["--batch", self.batch, "--role", "architect", "--purpose", "work"]
        proposal = self.run("dispatch", "propose", *fields)
        assert proposal.returncode == 0, proposal.stderr
        digest = json.loads(proposal.stdout)["transition_digest"]
        created = self.run(
            "dispatch",
            "create",
            *fields,
            "--transition-digest",
            digest,
            "--approved-by",
            APPROVAL["approved_by"],
            "--approved-at",
            APPROVAL["approved_at"],
        )
        assert created.returncode == 0, created.stderr
        brief = json.loads(created.stdout)["brief"]
        assert brief["execution"]["supervisor"] == "harness-runtime", brief
        return str(brief["dispatch_id"])

    def run(self, *command: str) -> subprocess.CompletedProcess[str]:
        return coordinator_with(self.repo, self.runtime, *command)

    def send_command(self) -> list[str]:
        return [
            "dispatch",
            "send",
            "--dispatch",
            self.dispatch,
            "--adapter",
            str(self.adapter),
        ]

    def send(self) -> subprocess.CompletedProcess[str]:
        return self.run(*self.send_command())

    def start_send(self) -> subprocess.Popen[str]:
        return coordinator_popen(self.repo, self.runtime, *self.send_command())

    @property
    def database(self) -> Path:
        return self.repo / STATE_REL / "runtime.db"

    def launches(self) -> list[Launch]:
        log = self.control / "launches.log"
        if not log.exists():
            return []
        found = []
        for line in log.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            found.append(
                Launch(
                    entry["pid"], entry["ppid"], entry["pgid"], entry["operation_id"]
                )
            )
        return found

    def wait_launches(self, count: int, timeout: float = 30.0) -> list[Launch]:
        deadline = time.monotonic() + timeout
        while len(self.launches()) < count and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.launches()

    def release(self, code: int = 0) -> None:
        (self.control / "exit_code").write_text(str(code), encoding="utf-8")
        (self.control / "release").write_text("", encoding="utf-8")

    def ignore_term(self) -> None:
        (self.control / "ignore_term").write_text("", encoding="utf-8")

    def count_term(self) -> None:
        """The adapter survives SIGTERM and logs each one it receives."""
        (self.control / "count_term").write_text("", encoding="utf-8")

    def terms(self) -> int:
        log = self.control / "terms.log"
        return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0

    def wait_terms(self, count: int, timeout: float = 30.0) -> int:
        deadline = time.monotonic() + timeout
        while self.terms() < count and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.terms()

    def add_member(self) -> None:
        """The adapter starts a member of its group that outlives it until ``release_member``."""
        (self.control / "member").write_text("", encoding="utf-8")

    def member(self, timeout: float = 30.0) -> int:
        path = self.control / "member.pid"
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return int(path.read_text(encoding="utf-8"))

    def release_member(self) -> None:
        (self.control / "member_release").write_text("", encoding="utf-8")

    def execution(self) -> JsonObject | None:
        answer = store_call(
            self.repo, self.runtime, "execution.observe", {"dispatch_id": self.dispatch}
        )
        execution = answer["execution"]
        return execution if isinstance(execution, dict) else None

    def wait_execution(self, state: str, timeout: float = 30.0) -> JsonObject:
        deadline = time.monotonic() + timeout
        while True:
            execution = self.execution()
            if (
                execution and execution["state"] == state
            ) or time.monotonic() > deadline:
                assert execution and execution["state"] == state, execution
                return execution
            time.sleep(0.05)

    def status(self) -> JsonObject:
        path = f"dispatch-status/{self.dispatch}.json"
        return store_records(self.repo, self.runtime, path)[path]

    def rows(self, sql: str, *params: object) -> list[tuple[object, ...]]:
        """Rows of the store, read directly: a test inspects what the runtime committed."""
        import sqlite3

        with contextlib.closing(
            sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        ) as db:
            return list(db.execute(sql, params))

    def execution_actions(self) -> list[str]:
        return [
            str(action)
            for (action,) in self.rows(
                "SELECT action FROM audit WHERE action LIKE 'execution-%' ORDER BY seq"
            )
        ]

    def restart_runtime(self, env: dict[str, str] | None = None) -> None:
        """SIGKILL the runtime and start another in its directory with the hooks ``env``."""
        xdg = self.runtime.xdg
        self.runtime.kill()
        self.runtime = ServingRuntime(self.repo, env=env, xdg=xdg)

    def close(self) -> None:
        self.release()
        self.release_member()
        for group in labelled_groups(self.dispatch) if self.dispatch else set():
            with contextlib.suppress(OSError):
                os.killpg(group, signal.SIGKILL)
        self.runtime.stop()
