"""Real maintenance facts and streaming at the console's public boundaries."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Thread
from typing import cast

import pytest

from harness import cleanup
from harness.console.maintenance import load_maintenance
from harness.console import process_tree
from harness.console.runner import HarnessRuns, capturing_runner
from harness.orchestration.runtime_client import (
    RuntimeClient,
    RuntimeClientError,
    RuntimeReply,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _real_tree(repo: Path) -> Path:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / ".gitignore").write_text("/.harness/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "test: fixture")
    remote = repo / ".harness/origin.git"
    remote.parent.mkdir()
    _git(repo, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    tree = repo / ".harness/.sandboxes/worktrees/issue-1-test"
    _git(repo, "worktree", "add", "-b", "feature/issue-1-test", str(tree))
    _git(repo, "push", "-u", "origin", "feature/issue-1-test")
    os.utime(tree, (1, 1))
    return tree


def test_sizes_count_only_planned_files_and_report_fresh_skips(tmp_path: Path) -> None:
    logs = tmp_path / ".harness/.sandboxes/logs"
    logs.mkdir(parents=True)
    old, fresh = logs / "old.log", logs / "fresh.log"
    old.write_bytes(b"1234567")
    fresh.write_bytes(b"fresh")
    os.utime(old, (1, 1))
    view = load_maintenance(tmp_path)
    assert view.soft.categories["logs"].bytes == 7
    assert view.soft.categories["logs"].files == 1
    assert any(item["path"] == str(fresh) for item in view.soft.plan["skipped"])


def test_streams_both_pipes_before_exit_and_retains_complete_result(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    code = (
        "import sys,time,pathlib; "
        "sys.stdout.write('FIRST');sys.stdout.flush(); "
        "sys.stderr.write('ERROR');sys.stderr.flush(); "
        f"p=pathlib.Path({str(release)!r}); "
        "exec('while not p.exists(): time.sleep(.01)'); "
        "print('LAST');sys.exit(7)"
    )
    runs = HarnessRuns()
    run = runs.reserve("probe", "probe", [sys.executable, "-u", "-c", code], {})
    assert run is not None
    worker = Thread(target=runs.execute, args=(run, capturing_runner, tmp_path))
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not ("FIRST" in run.stdout and "ERROR" in run.stderr):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert runs.busy and run.returncode is None
        runs.abandon_pending(run)  # Leaving the page cannot release a running command.
        assert runs.reserve("other", "other", [], {}) is None
    finally:
        release.touch()
        worker.join(5)
    assert not worker.is_alive()
    assert run.stdout == "FIRSTLAST\n" and run.stderr == "ERROR"
    assert run.returncode == 7 and not runs.busy


def test_approved_cleanup_digest_rejects_a_changed_plan(tmp_path: Path) -> None:
    plan = cleanup.plan_cleanup(tmp_path, "soft")
    digest = cleanup.plan_digest(plan)
    logs = tmp_path / ".harness/.sandboxes/logs"
    logs.mkdir(parents=True)
    log = logs / "old"
    log.write_text("keep")
    os.utime(log, (1, 1))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.bin.harness",
            "cleanup",
            str(tmp_path),
            "--apply",
            "--plan-digest",
            digest,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "plan changed" in result.stdout + result.stderr
    assert log.read_text() == "keep"


def test_worktree_removal_rechecks_active_and_dirty_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / ".harness/.sandboxes/worktrees/issue-1"
    tree.mkdir(parents=True)
    monkeypatch.setattr(
        cleanup, "_registered_worktrees", lambda _: {tree: "feature/issue-1-test"}
    )
    monkeypatch.setattr(cleanup, "_active_worktrees", lambda _: {tree})
    fact = cleanup.worktree_facts(tmp_path)[0]
    assert not fact.removable and "active batch" in fact.reason
    with pytest.raises(ValueError, match="active batch"):
        cleanup.remove_worktree(tmp_path, tree)


def test_real_worktree_removal_preserves_branch_and_rechecks_dirty_state(
    tmp_path: Path,
) -> None:
    tree = _real_tree(tmp_path)
    assert next(
        item for item in cleanup.worktree_facts(tmp_path) if item.path == tree
    ).removable
    (tree / "untracked").write_text("keep")
    os.utime(tree, (1, 1))
    fact = next(item for item in cleanup.worktree_facts(tmp_path) if item.path == tree)
    assert fact.dirty == 1 and not fact.removable
    with pytest.raises(ValueError, match="uncommitted"):
        cleanup.remove_worktree(tmp_path, tree)
    (tree / "untracked").unlink()
    os.utime(tree, (1, 1))
    cleanup.remove_worktree(tmp_path, tree)
    assert not tree.exists()
    assert _git(tmp_path, "branch", "--list", "feature/issue-1-test")


@pytest.mark.parametrize(
    "protection", ["fresh", "active", "unpushed", "ledger-unavailable"]
)
def test_real_worktree_protections_cannot_be_bypassed(
    tmp_path: Path, protection: str
) -> None:
    tree = _real_tree(tmp_path)
    if protection == "fresh":
        os.utime(tree, None)
    elif protection == "unpushed":
        (tree / "new").write_text("new")
        _git(tree, "add", "new")
        _git(tree, "commit", "-m", "test: local change")
        os.utime(tree, (1, 1))
    else:
        state = tmp_path / ".harness/orchestration/state/batches"
        state.mkdir(parents=True)
        (state / "batch-test.json").write_text(
            json.dumps({"state": "running", "worktree": str(tree)})
            if protection == "active"
            else "invalid"
        )
    with pytest.raises(ValueError):
        cleanup.remove_worktree(tmp_path, tree)
    assert tree.exists()


def test_missing_process_preserves_admission_until_failure_is_recorded(
    tmp_path: Path,
) -> None:
    runs = HarnessRuns()
    run = runs.reserve("bad", "bad", [str(tmp_path / "missing")], {})
    assert run is not None
    runs.execute(run, capturing_runner, tmp_path)
    assert run.phase == "completed" and run.error and not runs.busy


def test_cancelled_pending_run_cannot_start_later(tmp_path: Path) -> None:
    runs = HarnessRuns()
    run = runs.reserve("pending", "pending", ["missing"], {})
    assert run is not None
    runs.abandon_pending(run)
    replacement = runs.reserve("new", "new", [], {})
    assert replacement is not None
    runs.execute(run, capturing_runner, tmp_path)
    assert run.phase == "cancelled" and replacement.phase == "pending" and runs.busy


def test_streaming_decodes_split_utf8_and_preserves_text_newlines(
    tmp_path: Path,
) -> None:
    result = capturing_runner(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'\\xd0'); os.write(1, b'\\xaf\\r\\nend\\r'); os.write(2, b'\\xff')",
        ],
        cwd=tmp_path,
    )
    assert result.stdout == "Я\nend\n"
    assert result.stderr == "�" and result.returncode == 0


def test_capturing_runner_terminates_the_child_when_cancelled(tmp_path: Path) -> None:
    started = Event()
    cancelled = Event()
    raised: list[BaseException] = []

    def capture() -> None:
        try:
            capturing_runner(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time; print('ready', flush=True); time.sleep(60)",
                ],
                cwd=tmp_path,
                on_output=lambda channel, text: started.set()
                if channel == "stdout" and "ready" in text
                else None,
                cancel_event=cancelled,
            )
        except BaseException as exc:
            raised.append(exc)

    worker = Thread(target=capture)
    worker.start()
    assert started.wait(3)
    cancelled.set()
    worker.join(3)
    assert not worker.is_alive()
    assert len(raised) == 1 and isinstance(raised[0], process_tree.CommandCancelled)


def test_cancel_after_the_command_ended_keeps_its_exit_code() -> None:
    """A late cancellation must not report a finished command, and its changes, as cancelled."""
    with subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"]) as process:
        process.wait()
        cancelled = Event()
        cancelled.set()

        assert process_tree.wait_cancellable(process, cancelled, lambda: True) == 3


def test_windows_tree_stop_uses_job_object_after_root_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A returned root PID must not make Windows cancellation lose its descendants."""

    class FakeProcess:
        pid = 1234
        returncode = 0
        killed = False

        def kill(self) -> None:
            self.killed = True

    class FakeJob:
        terminated = False

        def terminate(self) -> bool:
            self.terminated = True
            return True

    taskkill_calls: list[object] = []

    def taskkill(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        taskkill_calls.append((args, kwargs))
        return subprocess.CompletedProcess([], 1)

    monkeypatch.setattr(process_tree, "is_windows", lambda: True)
    monkeypatch.setattr(subprocess, "run", taskkill)
    process = FakeProcess()
    job = FakeJob()

    process_tree.ProcessTree(
        cast("subprocess.Popen[bytes]", process),
        cast(process_tree.WindowsJobObject, job),
    ).stop(force=False)

    assert job.terminated and not process.killed
    assert taskkill_calls == []


def test_windows_tree_stop_kills_the_root_when_taskkill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a Job Object, a failed taskkill still leaves no live root behind."""

    class FakeProcess:
        pid = 1234
        killed = False

        def kill(self) -> None:
            self.killed = True

    taskkill_calls: list[object] = []

    def taskkill(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        taskkill_calls.append((args, kwargs))
        return subprocess.CompletedProcess([], 1)

    monkeypatch.setattr(process_tree, "is_windows", lambda: True)
    monkeypatch.setattr(subprocess, "run", taskkill)
    process = FakeProcess()

    process_tree.ProcessTree(cast("subprocess.Popen[bytes]", process), None).stop(
        force=True
    )

    assert len(taskkill_calls) == 1 and process.killed


@pytest.mark.parametrize("state", ["active", "completed", "unavailable"])
def test_worktree_protection_reads_the_runtime_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    tree = _real_tree(tmp_path)
    root = tmp_path / ".harness"
    ledger = root / "orchestration/state"
    ledger.mkdir(parents=True)
    (ledger / "ledger.json").write_text(json.dumps({"version": 4}))

    def call(self: RuntimeClient, operation: str, **kwargs: object) -> RuntimeReply:
        assert operation == "state.read"
        assert kwargs["payload"] == {"collections": ["batches"], "paths": []}
        if state == "unavailable":
            raise RuntimeClientError(
                "runtime_unavailable",
                "offline",
                remedy="start runtime",
                domain="infrastructure",
            )
        return RuntimeReply(
            {},
            {
                "records": {
                    "batches/batch-1.json": {"state": state, "worktree": str(tree)}
                }
            },
            1,
        )

    monkeypatch.setattr(RuntimeClient, "call", call)
    active = cleanup._active_worktrees(root)
    assert active == (
        None if state == "unavailable" else {tree} if state == "active" else set()
    )

    fact = next(item for item in cleanup.worktree_facts(tmp_path) if item.path == tree)
    assert fact.removable == (state == "completed")
