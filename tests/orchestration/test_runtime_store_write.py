"""The coordinator write commands in the runtime-store epoch (ADR 0020, sections 2 to 4).

The state of a real batch with one working architect dispatch is cut over with ``harness-runtime
migrate`` and served by a real ``harness-runtime serve``. ``dispatch heartbeat`` and ``report
submit`` then write through the coordinator CLI: the revision of every row they change advances by
one, the audit chain gains one record for each mutation, and the 1.x files stay as they were. The
coordinator never opens ``runtime.db``, never takes the file lock, and has no fallback writer.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from harness.json_types import JsonObject
from harness.orchestration.core import state_view
from harness.orchestration.ledger import ledger_ops
from harness.orchestration.runtime_client import RuntimeClientError
from harness.orchestration.workflow import reports

from .runtime_store_support import (
    ROOT,
    STATE_REL,
    ServingRuntime,
    architect_report_file,
    audited,
    coordinator_with,
    migrate,
    store_call,
    store_records,
    store_revisions,
    tree,
    write_coordinator_state,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)

LEGACY = ("ledger.json", "generations", "lock-audit")


def _legacy_files(repo: Path) -> dict[str, bytes]:
    """The 1.x files of the state directory: the cutover leaves them as the snapshot of the past."""
    return {
        path: data
        for path, data in tree(repo / STATE_REL).items()
        if path.startswith(LEGACY)
    }


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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path)
    try:
        yield store
    finally:
        store.runtime.stop()


def test_a_heartbeat_advances_the_dispatch_status_by_one_revision_and_nothing_else(
    store: Store,
) -> None:
    before = store.revisions(store.batch_path, store.status_path)
    chain = store.audit_records()

    result = store.heartbeat()

    assert result.returncode == 0, result.stderr
    after = store.revisions(store.batch_path, store.status_path)
    assert after[store.status_path] == before[store.status_path] + 1
    assert after[store.batch_path] == before[store.batch_path]
    assert store.audit_records() == chain + 1
    assert store.validate()["valid"] is True


def test_a_record_round_trips_revision_by_revision(store: Store) -> None:
    seen = []
    for _ in range(3):
        result = store.heartbeat()
        assert result.returncode == 0, result.stderr
        seen.append(store.revisions(store.status_path)[store.status_path])

    assert seen == [seen[0], seen[0] + 1, seen[0] + 2]
    record = store_records(store.repo, store.runtime, store.status_path)[store.status_path]
    assert record["dispatch_id"] == store.dispatch


def test_a_write_never_opens_the_file_ledger_the_store_or_the_lock(
    store: Store, tmp_path: Path
) -> None:
    result, events = audited(
        store.repo,
        store.runtime,
        ["dispatch", "heartbeat", "--dispatch", store.dispatch],
        tmp_path / "audit.json",
    )

    assert result.returncode == 0, result.stderr
    assert events == []
    assert not (store.repo / STATE_REL / ".coordinator.lock").exists()


def test_the_legacy_files_stay_byte_for_byte_after_every_write(store: Store) -> None:
    legacy = _legacy_files(store.repo)

    assert store.heartbeat().returncode == 0
    assert store.submit_report().returncode == 0

    assert _legacy_files(store.repo) == legacy


def test_the_checkout_stays_clean_after_the_writes(store: Store) -> None:
    store.heartbeat()

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=store.repo,
        capture_output=True,
        text=True,
        check=True,
    )

    # Only the ignored runtime state of the harness itself may be new; no tracked file changed.
    assert all(
        line.startswith("?? .harness/") for line in status.stdout.splitlines()
    ), status.stdout


def test_a_report_and_its_transitions_are_one_commit_with_one_audit_record_each(
    store: Store,
) -> None:
    before = store.revisions(store.batch_path, store.status_path)
    chain = store.audit_records()

    result = store.submit_report()

    assert result.returncode == 0, result.stderr
    after = store.revisions(store.batch_path, store.status_path)
    assert after[store.batch_path] > before[store.batch_path]
    assert after[store.status_path] > before[store.status_path]
    report = f"reports/{store.dispatch}.json"
    assert report in store_records(store.repo, store.runtime, report)
    assert store.audit_records() - chain >= 3
    assert store.validate()["valid"] is True
    status = store_records(store.repo, store.runtime, store.status_path)[store.status_path]
    assert status["state"] == "reported"


def test_a_refused_report_writes_nothing_to_the_store(store: Store) -> None:
    assert store.submit_report().returncode == 0
    revisions = store.revisions(store.batch_path, store.status_path)
    chain = store.audit_records()

    again = store.submit_report()

    assert again.returncode == 2
    assert store.revisions(store.batch_path, store.status_path) == revisions
    assert store.audit_records() == chain


def test_a_report_with_a_variant_of_a_vocabulary_value_is_refused_before_the_runtime(
    store: Store,
) -> None:
    path = store.report_file()
    report = json.loads(path.read_text(encoding="utf-8"))
    report["outcome"] = "Completed"
    path.write_text(json.dumps(report), encoding="utf-8")
    chain = store.audit_records()

    result = store.run("report", "submit", "--file", str(path))

    assert result.returncode == 2
    assert "completed" in result.stderr
    assert store.audit_records() == chain


def test_without_a_runtime_a_write_is_runtime_unavailable_and_touches_no_file(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.runtime.stop()
    monkeypatch.setattr(ledger_ops, "READ_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(ledger_ops, "WRITE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(store.repo / "no-runtime-here"))
    files = tree(store.repo / STATE_REL, skip=("runtime.db",))
    args = argparse.Namespace(
        repo=str(store.repo),
        state_dir=None,
        dispatch=store.dispatch,
        note=None,
        context_tokens=None,
        context_source=None,
    )

    with pytest.raises(RuntimeClientError) as refused:
        reports.heartbeat_dispatch(args)

    assert refused.value.code == "runtime_unavailable"
    assert refused.value.remedy.startswith("[runtime_unavailable] ")
    assert tree(store.repo / STATE_REL, skip=("runtime.db",)) == files
    assert not (store.repo / STATE_REL / ".coordinator.lock").exists()


def test_a_stale_revision_and_a_missing_runtime_have_different_remedies(store: Store) -> None:
    with pytest.raises(RuntimeClientError) as conflict:
        store_call(
            store.repo,
            store.runtime,
            "state.write",
            {
                "mutations": [
                    {
                        "op": "replace",
                        "path": store.status_path,
                        "content": "{}\n",
                        "expected_revision": 999,
                    }
                ]
            },
        )

    assert conflict.value.code == "revision_conflict"
    assert conflict.value.remedy.startswith("[revision_conflict] ")
    assert conflict.value.facts["expected_revision"] == 999


def test_no_module_of_the_coordinator_opens_sqlite() -> None:
    """Python never opens ``runtime.db``: it does not even import the SQLite module."""
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "harness" / "orchestration").rglob("*.py"))
        if "sqlite3" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


# -- the store unit of one command, without a runtime --------------------------------------------------


class _FakeStore:
    """What a ``StateView`` fetches, and what a ``StoreUnit`` commits."""

    def __init__(self, records: dict[str, JsonObject], revisions: dict[str, int]) -> None:
        self.records = records
        self.revisions = revisions
        self.commits: list[tuple[str | None, int | None, list[JsonObject]]] = []

    def fetch(
        self, collections: Sequence[str], paths: Sequence[str], revision: int | None
    ) -> tuple[dict[str, JsonObject], int | None, dict[str, int]]:
        found = {
            path: record
            for path, record in self.records.items()
            if path in paths or path.split("/")[0] in collections
        }
        return found, 5, {path: self.revisions[path] for path in found if path in self.revisions}

    def commit(
        self, batch_id: str | None, expected: int | None, mutations: list[JsonObject]
    ) -> dict[str, int]:
        self.commits.append((batch_id, expected, [dict(mutation) for mutation in mutations]))
        return {
            str(m["path"]): self.revisions.get(str(m["path"]), 0) + 1
            for m in mutations
            if m["op"] != "delete"
        }


def _unit(tmp_path: Path, store: _FakeStore) -> state_view.StoreUnit:
    view = state_view.StateView(tmp_path, store.fetch)
    return view.begin_unit(store.commit)


def test_staged_writes_are_read_back_and_reach_the_runtime_only_at_the_flush(
    tmp_path: Path,
) -> None:
    store = _FakeStore({}, {})
    unit = _unit(tmp_path, store)

    unit.create("reports/a.json", {"a": 1}, '{"a": 1}\n')

    assert unit.view.read("reports/a.json") == {"a": 1}
    assert unit.view.names("reports") == ["a.json"]
    assert unit.view.is_file("reports/a.json")
    assert store.commits == []
    unit.flush()
    assert store.commits == [
        (None, None, [{"op": "create", "path": "reports/a.json", "content": '{"a": 1}\n'}])
    ]
    assert unit.view.read("reports/a.json") == {"a": 1}
    assert unit.view.revisions["reports/a.json"] == 1


def test_the_first_touch_of_a_row_carries_the_revision_the_command_read(tmp_path: Path) -> None:
    path = "qa-lane/sequence.json"
    store = _FakeStore({path: {"n": 1}}, {path: 4})
    unit = _unit(tmp_path, store)
    assert unit.view.read(path) == {"n": 1}

    unit.replace(path, {"n": 2}, '{"n": 2}\n')
    unit.replace(path, {"n": 3}, '{"n": 3}\n', decision={"kind": "x"})
    unit.flush()

    (_, _, mutations), = store.commits
    assert mutations[0]["expected_revision"] == 4
    assert "expected_revision" not in mutations[1]
    assert mutations[1]["decision"] == {"kind": "x"}


def test_a_row_the_command_created_and_deleted_never_reaches_the_runtime(tmp_path: Path) -> None:
    store = _FakeStore({}, {})
    unit = _unit(tmp_path, store)
    unit.create("reports/a.json", {"a": 1}, '{"a": 1}\n')
    unit.create("reports/b.json", {"b": 1}, '{"b": 1}\n')

    unit.delete("reports/a.json", reason="discard incomplete immutable report")
    unit.flush()

    assert unit.view.read("reports/a.json") is None
    assert [m["path"] for m in store.commits[0][2]] == ["reports/b.json"]


def test_a_command_that_raises_sends_nothing(tmp_path: Path) -> None:
    store = _FakeStore({}, {})
    unit = _unit(tmp_path, store)
    unit.create("reports/a.json", {"a": 1}, '{"a": 1}\n')

    unit.abort()

    assert store.commits == []
    assert unit.view.unit is None
    assert unit.view.names("reports") == []


def test_the_mutations_are_cut_where_the_batch_changes_with_the_revision_each_read(
    tmp_path: Path,
) -> None:
    store = _FakeStore(
        {"batches/batch-a.json": {"batch_id": "batch-a"}}, {"batches/batch-a.json": 3}
    )
    unit = _unit(tmp_path, store)
    assert unit.view.read("batches/batch-a.json") is not None
    unit.replace("batches/batch-a.json", {"batch_id": "batch-a", "n": 1}, "a\n")
    unit.create("batches/batch-b.json", {"batch_id": "batch-b"}, "b\n")
    unit.create("reports/r.json", {"r": 1}, "r\n")

    unit.flush()

    assert [(batch, expected) for batch, expected, _ in store.commits] == [
        ("batch-a", 3),
        ("batch-b", 0),
    ]
    # A row of no batch goes with the batch that is open.
    assert [m["path"] for m in store.commits[1][2]] == ["batches/batch-b.json", "reports/r.json"]


class _FakeClient:
    calls: ClassVar[list[dict[str, object]]] = []

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def call(self, operation: str, **arguments: object) -> object:
        _FakeClient.calls.append({"operation": operation, **arguments})
        return SimpleNamespace(result={"revisions": {}}, revision=1)


def test_the_operation_id_is_the_digest_of_batch_revision_operation_and_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from harness.orchestration import runtime_client
    from harness.orchestration.operational_guards import _digest

    monkeypatch.setattr(runtime_client, "RuntimeClient", _FakeClient)
    _FakeClient.calls.clear()
    commit = ledger_ops._runtime_commit(tmp_path)
    mutations: list[JsonObject] = [{"op": "create", "path": "reports/a.json", "content": "{}\n"}]

    commit("batch-a", 3, mutations)
    commit("batch-a", 3, mutations)
    commit("batch-a", 4, mutations)
    commit("batch-a", 3, [{**mutations[0], "content": "{\"a\": 1}\n"}])

    first, repeat, other_revision, other_payload = (
        str(call["operation_id"]) for call in _FakeClient.calls
    )
    assert first == _digest(["batch-a", 3, "state.write", _digest({"mutations": mutations})])
    assert repeat == first
    assert len({first, other_revision, other_payload}) == 3
    assert _FakeClient.calls[0]["batch_id"] == "batch-a"
    assert _FakeClient.calls[0]["expected_revision"] == 3


def test_a_busy_store_is_the_ledger_busy_marker_and_a_stale_revision_is_its_own_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from harness.orchestration import runtime_client

    def refuse(code: str) -> type[_FakeClient]:
        class Refusing(_FakeClient):
            def call(self, operation: str, **arguments: object) -> object:
                raise RuntimeClientError(
                    code, f"{code} happened", remedy=f"fix {code}", domain="infrastructure"
                )

        return Refusing

    monkeypatch.setattr(runtime_client, "RuntimeClient", refuse("store_busy"))
    commit = ledger_ops._runtime_commit(tmp_path)
    with pytest.raises(ledger_ops.LedgerBusyError) as busy:
        commit(None, None, [])
    monkeypatch.setattr(runtime_client, "RuntimeClient", refuse("revision_conflict"))
    commit = ledger_ops._runtime_commit(tmp_path)
    with pytest.raises(RuntimeClientError) as conflict:
        commit(None, None, [])

    assert busy.value.remedy == "[store_busy] fix store_busy"
    assert not isinstance(conflict.value, ledger_ops.LedgerBusyError)
    assert conflict.value.remedy == "[revision_conflict] fix revision_conflict"
