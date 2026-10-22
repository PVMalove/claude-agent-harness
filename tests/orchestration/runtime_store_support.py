"""Shared helpers of the runtime-store seam tests: a 1.x state written by the real Python ledger,
and the real ``harness-runtime`` binary."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
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
            {"role": role, "note": "café \U0001f600", "ratio": 1e-05},
        )
        for dispatch_id, role in ((first, "developer"), (second, "qa"))
    ]
    report: JsonObject = {"dispatch_id": first, "outcome": "completed", "output": "done"}
    ledger.write_immutable(generation / "reports" / f"{first}.json", report, artifact=True)
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
    batch_fields: JsonObject = {"ticket": "#1", "write_paths": ["a/**"]}
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


def migrate(repo: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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

    def __init__(self, repo: Path, step: str, *, env: dict[str, str] | None = None) -> None:
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


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def coordinator_repository(tmp: Path) -> Path:
    """A git repository with an origin, a project config and the architect role manifest: what
    ``create_batch`` and ``create_dispatch`` touch for real."""
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
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
    (repo / ".gitignore").write_text(".harness/orchestration/state/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "branch", "-M", "master")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "master")
    return repo


def write_coordinator_state(tmp: Path) -> tuple[Path, dict[str, str]]:
    """The state of a real batch: planned, approved, one architect dispatch created, sent,
    self-reported and reported. Returns the repository and the ids."""
    import argparse

    from harness.orchestration import coordinator
    from harness.orchestration.core import workspace

    repo = coordinator_repository(tmp)
    worktree = tmp / "worktree"
    branch = "feature/issue-797-seam"
    _git(repo, "worktree", "add", "-b", branch, str(worktree), "master")

    def args(**values: object) -> argparse.Namespace:
        return argparse.Namespace(repo=str(repo), state_dir=None, **values)

    batch = coordinator.create_batch(
        args(
            ticket="#797",
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
    fields = {
        "batch": batch_id,
        "role": "architect",
        "runtime": "claude",
        "purpose": "work",
        "candidate_commit": None,
        "delta_review_of": None,
        "model": "sonnet",
        "effort": "high",
    }
    proposal = coordinator.create_dispatch(args(propose=True, **fields))
    created = coordinator.create_dispatch(
        args(transition_digest=proposal["transition_digest"], **fields, **APPROVAL)
    )
    brief = created["brief"]
    dispatch_id = str(brief["dispatch_id"])
    launched = coordinator.send_dispatch(
        args(dispatch=dispatch_id, adapter=None, adapter_arg=None, checkout=None)
    )
    coordinator.self_report_dispatch(
        args(dispatch=dispatch_id, model="sonnet", worktree=launched["worker_worktree"])
    )
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
            for command in brief["verification_commands"]
        ],
        "risks": {"status": "none", "details": ""},
        "blockers": {"status": "none", "details": ""},
        "next_coordinator_action": "decide",
        "report_language": "ru",
    }
    path = workspace._prepare_agent_inbox(repo) / f"{dispatch_id}.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    coordinator.submit_report(args(file=str(path)))
    return repo, {"batch_id": batch_id, "dispatch_id": dispatch_id}


# -- a real runtime process serving one repository ----------------------------------------------------

SHORT_TMP = (
    "/tmp"
    if sys.platform != "win32" and os.access("/tmp", os.W_OK | os.X_OK)
    else tempfile.gettempdir()
)


class ServingRuntime:
    """``harness-runtime serve --repo <repo>``, with a runtime directory short enough for a socket."""

    def __init__(self, repo: Path) -> None:
        self.xdg = Path(tempfile.mkdtemp(prefix="hr", dir=SHORT_TMP)).resolve()
        self.environ = {"XDG_RUNTIME_DIR": str(self.xdg)}
        self.process = subprocess.Popen(
            [str(runtime_binary()), "serve", "--repo", str(repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.environ},
        )
        stdout = self.process.stdout
        assert stdout is not None
        lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: lines.put(stdout.readline()), daemon=True).start()
        try:
            text = lines.get(timeout=30)
        except queue.Empty:
            self.stop()
            pytest.fail("harness-runtime printed no readiness line within 30 seconds")
        assert json.loads(text)["status"] == "ready"

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=30)
        for stream in (self.process.stdout, self.process.stderr):
            if stream:
                stream.close()
        shutil.rmtree(self.xdg, ignore_errors=True)


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
