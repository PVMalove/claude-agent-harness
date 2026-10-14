"""Durable intent of a composite ledger write: the intent payload, its plan and the order in
which the runtime state store and the ledger are written."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration import runtime_intent
from harness.orchestration.core import workspace
from harness.orchestration.core.constants import STATE_REL
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    CheckpointRecord,
    DispatchStatusRecord,
    LifecycleLedger,
)
from harness.orchestration.runtime_intent import CompositeWrite, idempotency_key
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
)

BATCH = "batch-0a1b2c3d"
DISPATCH = "dispatch-4e5f6a7b"
CHECKPOINT = "checkpoint-8c9d0e1f"
OPERATION = "0f1e2d3c-4b5a-4968-8776-655443322110"


@dataclass
class Scene:
    """A Git checkout with a ledger whose batch has one dispatched role."""

    repo: Path
    root: Path
    ledger: LifecycleLedger

    @property
    def records(self) -> Path:
        return self.ledger.records_root()

    def store(self) -> RuntimeStateStore:
        return RuntimeStateStore(self.repo, self.root)

    def runtime(self) -> JsonObject | None:
        return self.store().read(BATCH)

    def read(self, relative: str) -> JsonObject:
        value: JsonObject = json.loads(
            (self.records / relative).read_text(encoding="utf-8")
        )
        return value

    def tree(self) -> dict[str, bytes]:
        """Every ledger file but the audit timestamps' volatile bytes."""
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }


def make_scene(tmp_path: Path) -> Scene:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    root = repo / STATE_REL
    ledger = LifecycleLedger(root)
    ledger.ensure()
    records = ledger.records_root()
    ledger.write_immutable(records / f"plans/{BATCH}.json", {"batch_id": BATCH})
    ledger.write_record(
        BatchRecord.from_dict(
            {
                "batch_id": BATCH,
                "state": "active",
                "dispatches": [{"dispatch_id": DISPATCH, "state": "dispatched"}],
            }
        )
    )
    ledger.write_record(
        DispatchStatusRecord.from_dict({"dispatch_id": DISPATCH, "state": "working"})
    )
    return Scene(repo.resolve(), root.resolve(), ledger)


def checkpoint_write(scene: Scene) -> CompositeWrite:
    """The steps ``checkpoint_dispatch`` plans: checkpoint, batch and status."""
    request: JsonObject = {"dispatch_id": DISPATCH, "commit_sha": "abc"}
    write = CompositeWrite(
        scene.ledger,
        scene.store(),
        batch_id=BATCH,
        dispatch_id=DISPATCH,
        kind="checkpoint",
        idempotency_key=idempotency_key(
            "checkpoint", DISPATCH, {"checkpoint": request}
        ),
    )
    write.write_record(
        CheckpointRecord.from_dict(
            {"checkpoint_id": CHECKPOINT, "batch_id": BATCH, **request}
        )
    )
    batch = scene.read(f"batches/{BATCH}.json")
    batch["dispatches"][0]["state"] = "checkpointed"
    batch["checkpoints"] = [{"checkpoint_id": CHECKPOINT, "dispatch_id": DISPATCH}]
    write.replace_record(BatchRecord.from_dict(batch))
    write.replace_record(
        DispatchStatusRecord.from_dict(
            {"dispatch_id": DISPATCH, "state": "checkpointed"}
        )
    )
    return write


def report_write(scene: Scene) -> CompositeWrite:
    """The steps ``_persist_report`` plans: report JSON and Markdown, status and batch."""
    report: JsonObject = {"dispatch_id": DISPATCH, "outcome": "completed"}
    report_sha256 = hashlib.sha256(
        runtime_intent.content_bytes("create", report)
    ).hexdigest()
    write = CompositeWrite(
        scene.ledger,
        scene.store(),
        batch_id=BATCH,
        dispatch_id=DISPATCH,
        kind="report-submit",
        idempotency_key=idempotency_key(
            "report-submit", DISPATCH, {"report_sha256": report_sha256}
        ),
    )
    write.write_immutable(scene.records / f"reports/{DISPATCH}.json", report)
    write.write_artifact(scene.records / f"reports/{DISPATCH}.md", "# Report\n")
    write.replace_record(
        DispatchStatusRecord.from_dict({"dispatch_id": DISPATCH, "state": "reported"})
    )
    batch = scene.read(f"batches/{BATCH}.json")
    batch["dispatches"][0].update({"state": "reported", "report_sha256": report_sha256})
    batch["state"] = "awaiting-approval"
    write.replace_record(BatchRecord.from_dict(batch))
    return write


WRITES: dict[str, Callable[[Scene], CompositeWrite]] = {
    "checkpoint": checkpoint_write,
    "report-submit": report_write,
}


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return make_scene(tmp_path)


# --- Intent payload in the store ---------------------------------------------------------------


def test_an_intent_payload_round_trips_and_is_write_once(scene: Scene) -> None:
    store = scene.store()
    payload: JsonObject = {"operation_id": OPERATION, "steps": []}

    ref = store.write_intent(BATCH, OPERATION, payload)

    assert ref == f"intents/{BATCH}/{OPERATION}.json"
    assert store.read_intent(BATCH, ref) == payload
    assert store.write_intent(BATCH, OPERATION, copy.deepcopy(payload)) == ref
    with pytest.raises(RuntimeStateError) as caught:
        store.write_intent(BATCH, OPERATION, {**payload, "steps": [1]})
    assert caught.value.code == "integrity-failure"
    assert store.read_intent(BATCH, ref) == payload


@pytest.mark.parametrize(
    "ref",
    [
        f"intents/batch-0000/{OPERATION}.json",
        f"intents/{BATCH}/../{OPERATION}.json",
        f"intents/{BATCH}/not-an-operation.json",
        f"intents/{BATCH}/0f1e2d3c-4b5a-4968-8776-000000000000.json",
    ],
)
def test_reading_a_foreign_or_missing_intent_payload_stops(
    scene: Scene, ref: str
) -> None:
    scene.store().write_intent(BATCH, OPERATION, {"operation_id": OPERATION})

    with pytest.raises(RuntimeStateError) as caught:
        scene.store().read_intent(BATCH, ref)

    assert caught.value.code == "integrity-failure"


def test_the_idempotency_key_comes_from_the_request_only() -> None:
    first = idempotency_key("checkpoint", DISPATCH, {"checkpoint": {"a": 1}})

    assert first == idempotency_key("checkpoint", DISPATCH, {"checkpoint": {"a": 1}})
    assert first != idempotency_key("checkpoint", DISPATCH, {"checkpoint": {"a": 2}})
    assert first != idempotency_key("report-submit", DISPATCH, {"checkpoint": {"a": 1}})


# --- The composite write -----------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(WRITES))
def test_the_intent_is_durable_before_the_first_ledger_write(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    seen: list[JsonObject | None] = []
    for name in ("write_immutable", "write_artifact", "_atomic_write"):
        original = getattr(LifecycleLedger, name)

        def spy(
            *arguments: object, _original: object = original, **keywords: object
        ) -> object:
            seen.append(scene.runtime())
            return _original(*arguments, **keywords)  # type: ignore[operator]

        # ``_atomic_write`` is a static method: keep it one, or ``self`` shifts its arguments.
        monkeypatch.setattr(
            LifecycleLedger, name, staticmethod(spy) if name == "_atomic_write" else spy
        )

    WRITES[kind](scene).commit()

    first = seen[0]
    assert first is not None
    assert (first["state"], first["operation"]["phase"]) == ("running", "prepared")
    assert first["operation"]["kind"] == kind
    payload = scene.store().read_intent(BATCH, first["input_refs"][0])
    assert payload["idempotency_key"] == first["operation"]["idempotency_key"]
    assert {step["path"]: step["after_sha256"] for step in payload["steps"]} == first[
        "ledger"
    ]["record_hashes"]


@pytest.mark.parametrize("kind", sorted(WRITES))
def test_the_store_commits_only_after_every_record_and_audit_is_verified(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    timeline: list[str] = []
    original_write = RuntimeStateStore.write
    original_audit = LifecycleLedger._append_audit

    def store_write(
        self: RuntimeStateStore, record: JsonObject, **keywords: int
    ) -> object:
        operation = record["operation"]
        timeline.append(f"store:{operation['phase'] if operation else record['state']}")
        return original_write(self, record, **keywords)

    def append_audit(
        self: LifecycleLedger, *arguments: object, **keywords: object
    ) -> None:
        timeline.append("audit")
        original_audit(self, *arguments, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(RuntimeStateStore, "write", store_write)
    monkeypatch.setattr(LifecycleLedger, "_append_audit", append_audit)

    WRITES[kind](scene).commit()

    steps = 3 if kind == "checkpoint" else 4
    assert timeline == [
        "store:ready",
        "store:prepared",
        *["audit"] * steps,
        "store:effect-observed",
        "store:ledger-committed",
        "store:committed",
    ]
    record = scene.runtime()
    assert record is not None
    assert record["state"] == "running"
    assert record["operation"]["effect"]["state"] == "confirmed"
    assert record["last_confirmed_boundary"]["revision"] == record["revision"]
    for path, sha256 in record["ledger"]["record_hashes"].items():
        assert hashlib.sha256((scene.records / path).read_bytes()).hexdigest() == sha256
    audits = [ref for ref in record["output_refs"] if ref.startswith("audit/")]
    assert len(audits) == steps
    assert all((scene.records / ref).is_file() for ref in audits)


def test_a_second_operation_follows_a_committed_one(scene: Scene) -> None:
    checkpoint_write(scene).commit()
    first = scene.runtime()

    report_write(scene).commit()

    second = scene.runtime()
    assert first is not None and second is not None
    assert second["operation"]["operation_id"] != first["operation"]["operation_id"]
    assert second["operation"]["phase"] == "committed"


def test_a_refused_plan_writes_nothing_to_either_store(scene: Scene) -> None:
    (scene.records / f"reports/{DISPATCH}.md").write_text("earlier", encoding="utf-8")
    before = scene.tree()
    write = report_write(scene)

    with pytest.raises(CoordinatorError) as caught:
        write.commit()

    assert "refusing to overwrite immutable record" in caught.value.message
    assert scene.tree() == before
    assert scene.runtime() is None


def test_an_unfinished_operation_blocks_the_next_composite_write(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        LifecycleLedger, "write_artifact", lambda *arguments, **keywords: _fail()
    )
    with pytest.raises(RuntimeStateError) as caught:
        report_write(scene).commit()
    assert (caught.value.code, caught.value.step) == (
        "execution-failure",
        "ledger:reports",
    )
    monkeypatch.undo()
    suspended = scene.runtime()
    assert suspended is not None
    assert suspended["state"] == "suspended"

    with pytest.raises(RuntimeStateError) as blocked:
        checkpoint_write(scene).commit()

    assert blocked.value.code == "concurrency-conflict"
    assert scene.runtime() == suspended


def _fail() -> None:
    raise OSError("disk full")


def test_runtime_state_progress_never_changes_the_pinned_runtime_hash(
    scene: Scene,
) -> None:
    (scene.repo / ".harness/orchestration/coordinator.py").write_text(
        "# installed runtime\n", encoding="utf-8"
    )
    pinned = workspace._harness_runtime_sha256(scene.repo)

    checkpoint_write(scene).commit()

    assert scene.store().location.directory.is_relative_to(
        scene.repo / ".harness/orchestration"
    )
    assert workspace._harness_runtime_sha256(scene.repo) == pinned
