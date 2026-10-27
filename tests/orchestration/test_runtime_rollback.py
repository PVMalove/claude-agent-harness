"""``harness-runtime rollback --to-legacy`` on a state of the last 1.x release (ADR 0020, section 5).

The release itself writes the state (``release_1x``), ``harness-runtime migrate`` cuts it over, and
the coordinator of this tree, served by the real runtime, sends one approved dispatch and starts
another, so the store holds a dispatch in every ``DispatchState``. After the rollback, ``ledger
validate`` of the release accepts the new generation: the way back to 1.x needs no other step.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from .release_1x import STATE_REL, build_1x_state, cli_1x
from .runtime_store_support import (
    SHORT_TMP,
    ServingRuntime,
    coordinator_with,
    migrate,
    runtime_binary,
    tree,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)

EVERY_DISPATCH_STATE = {
    "proposed",
    "approved",
    "dispatched",
    "working",
    "blocked",
    "failed",
    "reported",
    "checkpointed",
    "rate_limited",
    "abandoned",
    "cancelled",
}
#: The SQLite sidecars come and go with every connection; they hold no row once it is closed.
SIDECARS = ("runtime.db-",)


def _send(repo: Path, runtime: ServingRuntime, dispatch_id: str) -> str:
    sent = coordinator_with(repo, runtime, "dispatch", "send", "--dispatch", dispatch_id)
    assert sent.returncode == 0, sent.stderr
    worktree: str = json.loads(sent.stdout)["worker_worktree"]
    return worktree


@pytest.fixture
def cut_over(release_1x: Path, tmp_path: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    """The release's state after the cutover, with one dispatch sent and one working, and no
    runtime serving it."""
    repo, ids = build_1x_state(release_1x, tmp_path)
    result = migrate(repo)
    assert result.returncode == 0, result.stderr
    runtime = ServingRuntime(repo)
    try:
        _send(repo, runtime, ids["send_dispatch"])
        worktree = _send(repo, runtime, ids["work_dispatch"])
        reported = coordinator_with(
            repo,
            runtime,
            "dispatch",
            "self-report",
            "--dispatch",
            ids["work_dispatch"],
            "--model",
            "sonnet",
            "--worktree",
            worktree,
        )
        assert reported.returncode == 0, reported.stderr
    finally:
        runtime.stop()
    yield repo, ids


def _runtime_dir() -> Path:
    """A runtime directory short enough for the socket path of the repository."""
    return Path(tempfile.mkdtemp(prefix="hr", dir=SHORT_TMP)).resolve()


def rollback(
    repo: Path, *, approved_by: str = "Malove", environ: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(runtime_binary()),
            "rollback",
            "--to-legacy",
            "--repo",
            str(repo),
            "--approved-by",
            approved_by,
            "--approved-at",
            "2026-10-27T00:00:00+00:00",
            "--reason",
            "return to the last 1.x release",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(environ or {"XDG_RUNTIME_DIR": str(_runtime_dir())})},
    )


def _store(repo: Path) -> Path:
    return repo / STATE_REL / "runtime.db"


def _read_only(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{database}?mode=ro", uri=True)


def _state(repo: Path) -> dict[str, bytes]:
    return tree(repo / STATE_REL, skip=SIDECARS)


def test_the_generation_of_the_rollback_passes_ledger_validate_of_the_release(
    cut_over: tuple[Path, dict[str, str]], release_1x: Path
) -> None:
    repo, _ = cut_over

    result = rollback(repo)

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["rolled_back"] is True
    validated = cli_1x(release_1x, repo, "ledger", "validate")
    assert validated.returncode == 0, validated.stderr
    assert json.loads(validated.stdout)["generation"] == outcome["generation"]
    generation = repo / STATE_REL / "generations" / outcome["generation"]
    states = {
        json.loads(path.read_text(encoding="utf-8"))["state"]
        for path in (generation / "dispatch-status").glob("*.json")
    }
    assert states == EVERY_DISPATCH_STATE


def test_the_archive_hash_is_in_the_audit_record_and_the_store_is_kept_read_only(
    cut_over: tuple[Path, dict[str, str]],
) -> None:
    repo, _ = cut_over

    result = rollback(repo)

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    archive = repo / STATE_REL / "runtime-export" / f"{outcome['store_id']}.tar"
    with _read_only(_store(repo)) as connection:
        action, details = connection.execute(
            "SELECT action, details_json FROM audit ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    assert action == "runtime-rollback"
    assert json.loads(details)["export_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert _store(repo).is_file()
    assert not os.access(_store(repo), os.W_OK)
    authority = json.loads((repo / STATE_REL / "AUTHORITY.json").read_text(encoding="utf-8"))
    assert authority["authority"] == "file-ledger"
    assert authority["rolled_back_from"] == outcome["store_id"]


def _assert_refused_without_a_change(
    repo: Path, result: subprocess.CompletedProcess[str], before: dict[str, bytes], code: str
) -> None:
    assert result.returncode == 2, result.stderr
    assert f"REMEDY: [{code}]" in result.stderr, result.stderr
    assert _state(repo) == before


def test_a_policy_approver_is_refused(cut_over: tuple[Path, dict[str, str]]) -> None:
    repo, _ = cut_over
    before = _state(repo)

    result = rollback(repo, approved_by="policy:auto")

    _assert_refused_without_a_change(repo, result, before, "policy_denied")


def test_a_running_serve_is_refused(cut_over: tuple[Path, dict[str, str]]) -> None:
    repo, _ = cut_over
    runtime = ServingRuntime(repo)
    try:
        before = _state(repo)
        result = rollback(repo, environ=runtime.environ)
        after = _state(repo)
    finally:
        runtime.stop()

    assert result.returncode == 2, result.stderr
    assert "REMEDY: [lease_held]" in result.stderr, result.stderr
    assert after == before


def test_a_qa_lease_in_the_store_is_refused(cut_over: tuple[Path, dict[str, str]]) -> None:
    repo, _ = cut_over
    with sqlite3.connect(_store(repo)) as connection:
        connection.execute("INSERT INTO qa_lane (id, body_json) VALUES (1, '{}')")
    before = _state(repo)

    result = rollback(repo)

    _assert_refused_without_a_change(repo, result, before, "lease_held")


def test_a_running_execution_is_refused(cut_over: tuple[Path, dict[str, str]]) -> None:
    repo, ids = cut_over
    with sqlite3.connect(_store(repo)) as connection:
        connection.execute(
            "UPDATE dispatch_status SET execution_state = 'running' WHERE id = ?",
            (ids["work_dispatch"],),
        )
    before = _state(repo)

    result = rollback(repo)

    _assert_refused_without_a_change(repo, result, before, "lease_held")
