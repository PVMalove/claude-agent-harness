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
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from harness.json_types import JsonObject
from harness.orchestration import operation_access, qa_lane
from harness.orchestration.core import state_view
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.ledger import ledger_ops
from harness.orchestration.ledger.lifecycle import BatchRecord, LifecycleLedger
from harness.orchestration.runtime_client import RuntimeClientError
from harness.orchestration.workflow import attention, reports

from .runtime_store_support import (
    ROOT,
    STATE_REL,
    Store,
    audited,
    coordinator_with,
    store_call,
    store_records,
    tree,
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


def test_the_writes_never_change_the_hash_of_the_pinned_runtime(store: Store) -> None:
    from harness.orchestration.core import workspace

    before = workspace._harness_runtime_sha256(store.repo)

    assert store.heartbeat().returncode == 0
    assert store.submit_report().returncode == 0

    assert workspace._harness_runtime_sha256(store.repo) == before


def test_a_write_for_a_linked_worktree_does_not_touch_the_store_of_the_main_checkout(
    store: Store, tmp_path: Path
) -> None:
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "linked-branch", str(linked)],
        cwd=store.repo,
        check=True,
        capture_output=True,
    )
    chain = store.audit_records()
    revisions = store.revisions(store.batch_path, store.status_path)

    result = coordinator_with(
        linked, store.runtime, "dispatch", "heartbeat", "--dispatch", store.dispatch
    )

    # A linked worktree has a state directory of its own (the state is ignored by Git): the
    # command finds no dispatch there and the store of the main checkout is as it was.
    assert result.returncode == 2
    assert store.audit_records() == chain
    assert store.revisions(store.batch_path, store.status_path) == revisions
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=linked, capture_output=True, text=True, check=True
    )
    assert status.stdout == ""


def test_a_write_with_another_state_dir_is_refused_and_writes_nothing(
    store: Store, tmp_path: Path
) -> None:
    chain = store.audit_records()
    other = tmp_path / "other-state"
    other.mkdir()

    result = store.run(
        "dispatch", "heartbeat", "--dispatch", store.dispatch, "--state-dir", str(other)
    )

    assert result.returncode == 2
    assert store.audit_records() == chain
    assert list(other.iterdir()) == []


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="a file mode does not keep the superuser from reading",
)
def test_an_unreadable_store_file_is_a_refusal_that_writes_nothing(store: Store) -> None:
    chain = store.audit_records()
    revisions = store.revisions(store.batch_path, store.status_path)
    store.database.chmod(0o000)
    try:
        result = store.heartbeat()
    finally:
        store.database.chmod(0o600)

    assert result.returncode == 2
    assert "REMEDY:" in result.stderr
    assert store.audit_records() == chain
    assert store.revisions(store.batch_path, store.status_path) == revisions


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
        self,
        batch_id: str | None,
        expected: int | None,
        mutations: list[JsonObject],
        /,
        *,
        guard: state_view.Guard | None = None,
        answer: list[JsonObject] | None = None,
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


# -- one fresh snapshot for each block of a command, writes that outlive a refusal ---------------------


def test_a_refreshed_view_reads_the_store_again_with_its_new_revisions(tmp_path: Path) -> None:
    path = "dispatch-status/d.json"
    store = _FakeStore({path: {"n": 1}}, {path: 1})
    view = state_view.StateView(tmp_path, store.fetch)
    assert view.read(path) == {"n": 1}
    store.records[path], store.revisions[path] = {"n": 2}, 2

    assert view.read(path) == {"n": 1}, "one snapshot until the view is refreshed"
    view.refresh()

    assert view.read(path) == {"n": 2}
    assert view.revisions[path] == 2


def test_a_row_written_by_two_requests_of_one_flush_carries_the_revision_the_first_returned(
    tmp_path: Path,
) -> None:
    a, b = "batches/batch-a.json", "batches/batch-b.json"
    store = _FakeStore(
        {a: {"batch_id": "batch-a"}, b: {"batch_id": "batch-b"}}, {a: 3, b: 7}
    )
    unit = _unit(tmp_path, store)
    assert unit.view.read(a) is not None and unit.view.read(b) is not None
    unit.replace(a, {"batch_id": "batch-a", "n": 1}, "a1\n")
    unit.replace(b, {"batch_id": "batch-b", "n": 1}, "b1\n")
    unit.replace(a, {"batch_id": "batch-a", "n": 2}, "a2\n")

    unit.flush()

    assert [(batch, expected) for batch, expected, _ in store.commits] == [
        ("batch-a", 3),
        ("batch-b", 7),
        ("batch-a", 4),
    ]
    assert store.commits[2][2][0]["expected_revision"] == 4


def test_persisted_writes_outlive_a_later_abort_and_the_unit_stays_open(tmp_path: Path) -> None:
    store = _FakeStore({}, {})
    unit = _unit(tmp_path, store)
    unit.create("reports/a.json", {"a": 1}, '{"a": 1}\n')

    unit.persist()
    unit.create("reports/b.json", {"b": 1}, '{"b": 1}\n')
    unit.abort()

    assert [m["path"] for _, _, mutations in store.commits for m in mutations] == [
        "reports/a.json"
    ]
    assert unit.view.read("reports/a.json") == {"a": 1}
    assert unit.view.revisions["reports/a.json"] == 1


def _wait(store: Store, *options: str) -> subprocess.Popen[str]:
    return store.start(
        "dispatch", "wait", "--dispatch", store.dispatch, "--poll-interval", "1", *options
    )


def test_a_dispatch_wait_poll_sees_the_report_written_while_it_waits(store: Store) -> None:
    waiting = _wait(store, "--timeout", "40", "--stale-after", "3600")
    time.sleep(3)
    assert waiting.poll() is None, "the wait ends before any report exists"

    assert store.submit_report().returncode == 0
    stdout, stderr = waiting.communicate(timeout=60)

    assert waiting.returncode == 0, stderr
    assert json.loads(stdout)["event"] == "reported"


def test_a_dispatch_wait_poll_sees_the_heartbeats_written_while_it_waits(store: Store) -> None:
    waiting = _wait(store, "--timeout", "14", "--stale-after", "6")
    for _ in range(5):
        time.sleep(2)
        assert store.heartbeat().returncode == 0
    stdout, stderr = waiting.communicate(timeout=60)

    assert waiting.returncode == 0, stderr
    assert json.loads(stdout)["event"] == "timeout", "a live dispatch is no stale dispatch"
    status = store_records(store.repo, store.runtime, store.status_path)[store.status_path]
    assert "stale" not in json.dumps(status)


def test_a_qa_access_refusal_keeps_its_attempt_record(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(store.runtime.xdg))
    ledger = LifecycleLedger(store.repo / STATE_REL)

    def refuse(*args: object, **kwargs: object) -> None:
        raise operation_access.OperationAccessError(
            "no access", remedy="grant it", evidence={"why": "test"}
        )

    monkeypatch.setattr(operation_access, "require", refuse)
    ops = SimpleNamespace(_config=lambda repo: {})

    with pytest.raises(operation_access.OperationAccessError), ledger_ops._ledger_lock(ledger):
        qa_lane._verify_access(ledger, store.repo, {"dispatch_id": store.dispatch}, ops)

    answer = store_call(store.repo, store.runtime, "state.read", {"collections": ["qa-lane"]})
    records = answer["records"]
    assert isinstance(records, dict)
    attempts = [
        record
        for path, record in records.items()
        if path.startswith("qa-lane/attempts/") and isinstance(record, dict)
    ]
    assert [(a["stage"], a["dispatch_id"]) for a in attempts] == [("access", store.dispatch)]


def test_a_batch_flagged_for_attention_keeps_the_flag_when_the_dispatch_is_refused(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(store.runtime.xdg))
    root = store.repo / STATE_REL
    ledger = LifecycleLedger(root)

    def flag(config: JsonObject, batch: JsonObject, findings: object, moment: str) -> bool:
        batch.update(
            needs_attention=True, attention_reason="test", recommended_human_action="look"
        )
        return True

    monkeypatch.setattr(attention, "_apply_attention", flag)
    monkeypatch.setattr(attention, "_attention_findings", lambda *args: [])

    with pytest.raises(CoordinatorError, match="needs human attention"), ledger_ops._ledger_lock(
        ledger
    ):
        batch = ledger_ops._load_batch(root, store.batch)
        attention._require_no_attention(ledger, store.repo, root, {}, batch)

    stored = store_records(store.repo, store.runtime, store.batch_path)[store.batch_path]
    assert stored["needs_attention"] is True


def test_a_block_that_raises_without_persisting_writes_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(store.runtime.xdg))
    root = store.repo / STATE_REL
    ledger = LifecycleLedger(root)
    chain = store.audit_records()

    with pytest.raises(CoordinatorError), ledger_ops._ledger_lock(ledger):
        batch = ledger_ops._load_batch(root, store.batch)
        batch["needs_attention"] = True
        ledger_ops._replace_record(ledger, BatchRecord.from_dict(batch))
        raise CoordinatorError("refused", remedy="none")

    assert store.audit_records() == chain


# -- the read snapshot of a view and the guard of a decision over every batch ---------------------


class _SnapshotStore(_FakeStore):
    """A fake store that answers from snapshots: each one is opened by a read and ended by
    ``close``, with the generation revision of the moment it was opened."""

    def __init__(self, records: dict[str, JsonObject], revisions: dict[str, int]) -> None:
        super().__init__(records, revisions)
        self.generation = 4
        self.opened = 0
        self.closed = 0
        self._open: int | None = None
        self.guards: list[int | None] = []

    @property
    def generation_revision(self) -> int | None:
        return self._open

    def __call__(
        self, collections: Sequence[str], paths: Sequence[str], revision: int | None, /
    ) -> tuple[dict[str, JsonObject], int | None, dict[str, int]]:
        if self._open is None:
            self.opened += 1
            self._open = self.generation
        return self.fetch(collections, paths, revision)

    def close(self) -> None:
        if self._open is not None:
            self.closed += 1
        self._open = None

    def commit(
        self,
        batch_id: str | None,
        expected: int | None,
        mutations: list[JsonObject],
        /,
        *,
        guard: state_view.Guard | None = None,
        answer: list[JsonObject] | None = None,
    ) -> dict[str, int]:
        self.guards.append(None if guard is None else guard.expected)
        if guard is not None:
            self.generation += 1
            guard.expected = self.generation
        return super().commit(batch_id, expected, mutations)


def test_a_view_ends_its_snapshot_at_a_refresh_after_a_commit_and_when_unbound(
    tmp_path: Path,
) -> None:
    path = "dispatch-status/d.json"
    store = _SnapshotStore({path: {"n": 1}}, {path: 1})
    view = state_view.StateView(tmp_path, store)
    assert isinstance(store, state_view.SnapshotFetch)

    with state_view.bind(view):
        view.read(path)
        assert (store.opened, store.closed) == (1, 0)
        view.refresh()
        assert (store.opened, store.closed) == (1, 1)
        view.read(path)
        unit = view.begin_unit(store.commit)
        unit.replace(path, {"n": 2}, '{"n": 2}\n')
        unit.flush()
        assert (store.opened, store.closed) == (2, 2)
        view.read("dispatch-status/other.json")
    assert (store.opened, store.closed) == (3, 3)


def test_a_unit_that_decided_over_every_batch_carries_and_adopts_the_generation(
    tmp_path: Path,
) -> None:
    a, b = "batches/batch-a.json", "batches/batch-b.json"
    store = _SnapshotStore(
        {a: {"batch_id": "batch-a"}, b: {"batch_id": "batch-b"}}, {a: 3, b: 7}
    )
    view = state_view.StateView(tmp_path, store)
    with state_view.bind(view):
        view.names("batches")
        unit = view.begin_unit(store.commit)
        state_view.depend_on_shared()
        unit.replace(a, {"batch_id": "batch-a", "n": 1}, "{}\n")
        unit.replace(b, {"batch_id": "batch-b", "n": 1}, "{}\n")
        unit.flush()

    # Both requests are guarded: the first by the snapshot's revision, the second by the one the
    # first request left.
    assert store.guards == [4, 5]


def test_a_unit_without_a_decision_over_every_batch_carries_no_generation(
    tmp_path: Path,
) -> None:
    path = "batches/batch-a.json"
    store = _SnapshotStore({path: {"batch_id": "batch-a"}}, {path: 3})
    view = state_view.StateView(tmp_path, store)
    with state_view.bind(view):
        view.read(path)
        unit = view.begin_unit(store.commit)
        unit.replace(path, {"batch_id": "batch-a", "n": 1}, "{}\n")
        unit.flush()

    assert store.guards == [None]
