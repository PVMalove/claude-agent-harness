"""Reconciliation of a composite ledger write that stopped part-way, from its durable intent."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration import runtime_reconcile
from harness.orchestration.runtime_reconcile import settle
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
)
from harness.orchestration.workflow import reports
from tests.orchestration.test_runtime_intent import (
    BATCH,
    DISPATCH,
    WRITES,
    Scene,
    make_scene,
)


class Crash(BaseException):
    """The process stops here: nothing after this point runs, no handler sees it."""


def crash_on(
    monkeypatch: pytest.MonkeyPatch,
    owner: type,
    name: str,
    call: int,
    *,
    after: bool = False,
) -> None:
    """Stop the process at the ``call``-th call (from 1) of ``owner.name``, before its original
    runs or, with ``after``, once it returned."""
    original = owner.__dict__[name]
    function = original.__func__ if isinstance(original, staticmethod) else original
    calls = [0]

    def stopping(*arguments: object, **keywords: object) -> object:
        calls[0] += 1
        if calls[0] == call and not after:
            raise Crash(f"{name} #{call}")
        result = function(*arguments, **keywords)
        if calls[0] == call:
            raise Crash(f"{name} #{call} returned")
        return result

    monkeypatch.setattr(
        owner,
        name,
        staticmethod(stopping) if isinstance(original, staticmethod) else stopping,
    )


def settle_scene(scene: Scene) -> bool:
    return settle(scene.repo, scene.root, scene.ledger, BATCH)


def ledger_state(scene: Scene) -> tuple[dict[str, bytes], list[str]]:
    """The generation's records byte for byte, and its audit events without their IDs and
    timestamps, which differ between two runs of the same write."""
    records = scene.ledger.records_root()
    files: dict[str, bytes] = {}
    events: list[str] = []
    for path in sorted(records.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(records).as_posix()
        if relative.startswith("audit/"):
            event = json.loads(path.read_text(encoding="utf-8"))
            events.append(
                json.dumps([event["action"], event["details"]], sort_keys=True)
            )
        else:
            files[relative] = path.read_bytes()
    return files, sorted(events)


def store_bytes(scene: Scene) -> dict[str, bytes]:
    directory = scene.store().location.directory
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.suffix == ".json"
    }


def reference(tmp_path: Path, kind: str) -> tuple[dict[str, bytes], list[str]]:
    """The ledger a clean, uninterrupted run of the same write leaves."""
    scene = make_scene(tmp_path / f"reference-{kind}")
    WRITES[kind](scene).commit()
    return ledger_state(scene)


def interrupted(
    scene: Scene, kind: str, stop: Callable[[], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    write = WRITES[kind](scene)
    stop()
    with pytest.raises(Crash):
        write.commit()
    monkeypatch.undo()


def assert_reconciled(
    scene: Scene, expected: tuple[dict[str, bytes], list[str]]
) -> None:
    assert ledger_state(scene) == expected
    record = scene.runtime()
    assert record is not None
    assert (record["state"], record["operation"]["phase"]) == ("ready", "committed")
    assert record["failure"] is None
    store, ledger = store_bytes(scene), scene.tree()

    assert settle_scene(scene) is False

    assert store_bytes(scene) == store
    assert scene.tree() == ledger


@pytest.mark.parametrize("kind", sorted(WRITES))
@pytest.mark.parametrize("step", [1, 2, 3])
def test_a_stop_between_a_record_and_its_audit_is_rolled_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, step: int
) -> None:
    scene = make_scene(tmp_path)
    interrupted(
        scene,
        kind,
        lambda: crash_on(monkeypatch, LifecycleLedger, "_append_audit", step),
        monkeypatch,
    )
    stopped = scene.runtime()
    assert stopped is not None
    assert (stopped["state"], stopped["operation"]["phase"]) == ("running", "prepared")

    assert settle_scene(scene) is True

    assert_reconciled(scene, reference(tmp_path, kind))


@pytest.mark.parametrize("kind", sorted(WRITES))
@pytest.mark.parametrize("step", [1, 2])
def test_a_stop_between_two_ledger_records_is_rolled_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, step: int
) -> None:
    scene = make_scene(tmp_path)
    interrupted(
        scene,
        kind,
        lambda: crash_on(
            monkeypatch, LifecycleLedger, "_append_audit", step, after=True
        ),
        monkeypatch,
    )

    assert settle_scene(scene) is True

    assert_reconciled(scene, reference(tmp_path, kind))


@pytest.mark.parametrize("kind", sorted(WRITES))
def test_a_stop_before_the_store_commit_finishes_the_phases_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    scene = make_scene(tmp_path)
    # Writes: ready, prepared, effect-observed, ledger-committed, committed.
    interrupted(
        scene,
        kind,
        lambda: crash_on(monkeypatch, RuntimeStateStore, "write", 5),
        monkeypatch,
    )
    written = ledger_state(scene)

    assert settle_scene(scene) is True

    assert ledger_state(scene) == written
    assert_reconciled(scene, reference(tmp_path, kind))


def test_reconciliation_records_no_decision_and_writes_only_the_planned_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = make_scene(tmp_path)
    before = scene.tree()
    interrupted(
        scene,
        "report-submit",
        lambda: crash_on(monkeypatch, LifecycleLedger, "_append_audit", 1),
        monkeypatch,
    )

    settle_scene(scene)

    record = scene.runtime()
    assert record is not None
    planned = set(record["output_refs"])
    prefix = scene.ledger.records_root().relative_to(scene.root).as_posix()
    changed = {
        path.removeprefix(f"{prefix}/")
        for path, content in scene.tree().items()
        if before.get(path) != content
    }
    assert changed == planned
    for ref in planned:
        if ref.startswith("audit/"):
            event = json.loads(
                (scene.ledger.records_root() / ref).read_text(encoding="utf-8")
            )
            assert "decision" not in event["details"]
    assert scene.read(f"batches/{BATCH}.json").get("coordinator_approval") is None


def test_settling_without_an_unfinished_operation_changes_nothing(
    tmp_path: Path,
) -> None:
    scene = make_scene(tmp_path)
    assert settle_scene(scene) is False
    assert scene.runtime() is None

    WRITES["checkpoint"](scene).commit()
    store, ledger = store_bytes(scene), scene.tree()

    assert settle_scene(scene) is False
    assert (store_bytes(scene), scene.tree()) == (store, ledger)


def _report() -> JsonObject:
    return {
        "dispatch_id": DISPATCH,
        "ticket": "694",
        "role": "qa",
        "outcome": "completed",
        "output": "green",
        "commit_sha": "not applicable",
        "changed_files": [],
        "checks_run": [],
        "risks": {"status": "none", "details": ""},
        "blockers": {"status": "none", "details": ""},
        "next_coordinator_action": "accept",
    }


def test_a_report_whose_markdown_write_stopped_is_finished_by_the_next_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = make_scene(tmp_path)
    dispatch: JsonObject = {"dispatch_id": DISPATCH, "batch_id": BATCH}
    crash_on(monkeypatch, LifecycleLedger, "write_artifact", 1)
    with pytest.raises(Crash):
        reports._persist_report(
            scene.ledger,
            scene.root,
            scene.read(f"batches/{BATCH}.json"),
            dispatch,
            _report(),
            repo=scene.repo,
        )
    monkeypatch.undo()
    assert not (scene.records / f"reports/{DISPATCH}.md").exists()

    with pytest.raises(RuntimeStateError) as caught:
        reports._persist_report(
            scene.ledger,
            scene.root,
            scene.read(f"batches/{BATCH}.json"),
            dispatch,
            _report(),
            repo=scene.repo,
        )

    assert caught.value.code == "concurrency-conflict"
    assert "repeat the command" in caught.value.remedy
    assert (scene.records / f"reports/{DISPATCH}.md").is_file()
    assert scene.read(f"dispatch-status/{DISPATCH}.json")["state"] == "reported"
    batch = scene.read(f"batches/{BATCH}.json")
    assert (batch["state"], batch["dispatches"][0]["state"]) == (
        "awaiting-approval",
        "reported",
    )


# --- Controlled stops --------------------------------------------------------------------------


def stop_after_status(scene: Scene, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Interrupt a report write after its status replace, before that replace's audit; returns
    the replaced status record."""
    interrupted(
        scene,
        "report-submit",
        lambda: crash_on(monkeypatch, LifecycleLedger, "_append_audit", 3),
        monkeypatch,
    )
    return scene.records / f"dispatch-status/{DISPATCH}.json"


def settle_error(scene: Scene) -> RuntimeStateError:
    with pytest.raises(RuntimeStateError) as caught:
        settle_scene(scene)
    return caught.value


def test_a_violated_record_hash_stops_with_evidence_and_no_new_revision_on_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = make_scene(tmp_path)
    status = stop_after_status(scene, monkeypatch)
    planned = status.read_bytes()
    status.write_text('{"dispatch_id": "dispatch-4e5f6a7b", "state": "failed"}\n')
    ledger = scene.tree()

    stopped = settle_error(scene)

    assert (stopped.code, stopped.step) == (
        "integrity-failure",
        "reconcile:dispatch-status",
    )
    evidence = stopped.diagnostics["evidence"]
    assert evidence["path"] == f"dispatch-status/{DISPATCH}.json"
    assert len(evidence["expected_before"]) == len(evidence["actual"]) == 12
    assert evidence["actual"] not in (
        evidence["expected_before"],
        evidence["expected_after"],
    )
    # The generation is still the planned one: the stop comes from the hash alone.
    record = scene.runtime()
    assert record is not None
    assert scene.ledger.pointer()["generation"] == record["ledger"]["generation"]  # type: ignore[index]
    assert (record["state"], record["failure"]["code"]) == (
        "suspended",
        "integrity-failure",
    )
    assert record["failure"]["step"] == "reconcile:dispatch-status"
    assert scene.tree() == ledger
    store = store_bytes(scene)

    again = settle_error(scene)

    assert (again.code, again.durable_revision) == (
        "integrity-failure",
        record["revision"],
    )
    assert (store_bytes(scene), scene.tree()) == (store, ledger)

    status.write_bytes(planned)
    assert settle_scene(scene) is True
    assert_reconciled(scene, reference(tmp_path, "report-submit"))


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(
            lambda steps: steps[0].update({"content": {"forged": True}}), id="content"
        ),
        pytest.param(
            lambda steps: steps[3]["audit"]["details"].update({"to": "completed"}),
            id="audit-details",
        ),
        pytest.param(
            lambda steps: steps[2].update(
                {"path": "dispatches/dispatch-4e5f6a7b.json"}
            ),
            id="path",
        ),
    ],
)
def test_a_tampered_intent_payload_stops_before_any_ledger_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Callable[[list[JsonObject]], None],
) -> None:
    scene = make_scene(tmp_path)
    stop_after_status(scene, monkeypatch)
    record = scene.runtime()
    assert record is not None
    path = scene.store().location.directory / record["input_refs"][0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    tamper(payload["steps"])
    path.write_text(json.dumps(payload), encoding="utf-8")
    ledger = scene.tree()

    stopped = settle_error(scene)

    assert (stopped.code, stopped.step) == ("integrity-failure", "reconcile:intent")
    assert scene.tree() == ledger


def test_a_tampered_audit_event_stops_the_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = make_scene(tmp_path)
    stop_after_status(scene, monkeypatch)
    record = scene.runtime()
    assert record is not None
    payload = scene.store().read_intent(BATCH, record["input_refs"][0])
    event = scene.records / f"audit/{payload['steps'][0]['audit']['audit_id']}.json"
    value = json.loads(event.read_text(encoding="utf-8"))
    value["details"]["sha256"] = "0" * 64
    event.write_text(json.dumps(value), encoding="utf-8")

    stopped = settle_error(scene)

    # The selected generation's own validation already refuses the forged event.
    assert (stopped.code, stopped.step) == ("integrity-failure", "reconcile:ledger")
    assert "failed immutable integrity check" in str(stopped)
    record = scene.runtime()
    assert record is not None
    assert (record["state"], record["failure"]["code"]) == (
        "suspended",
        "integrity-failure",
    )


def test_a_ready_state_with_an_unfinished_operation_is_unknown_and_stops_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = make_scene(tmp_path)
    stop_after_status(scene, monkeypatch)
    path = scene.store().path(BATCH)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["state"] = "ready"
    path.write_text(json.dumps(record), encoding="utf-8")
    store, ledger = store_bytes(scene), scene.tree()

    stopped = settle_error(scene)

    assert (stopped.code, stopped.step) == ("integrity-failure", "reconcile:state")
    assert (store_bytes(scene), scene.tree()) == (store, ledger)


def test_a_record_another_writer_changes_during_reconcile_is_a_concurrency_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = make_scene(tmp_path)
    status = stop_after_status(scene, monkeypatch)
    batch = scene.records / f"batches/{BATCH}.json"
    original = runtime_reconcile._reconciling

    def foreign_writer(store: RuntimeStateStore, record: JsonObject) -> JsonObject:
        reconciling = original(store, record)
        batch.write_text(batch.read_text(encoding="utf-8") + " ", encoding="utf-8")
        return reconciling

    monkeypatch.setattr(runtime_reconcile, "_reconciling", foreign_writer)
    planned_status = status.read_bytes()

    stopped = settle_error(scene)

    assert (stopped.code, stopped.step) == ("concurrency-conflict", "reconcile:batches")
    record = scene.runtime()
    assert record is not None
    assert (record["state"], record["failure"]["code"]) == (
        "suspended",
        "concurrency-conflict",
    )
    assert status.read_bytes() == planned_status
    monkeypatch.undo()
    # No blind retry: the foreign bytes are neither the planned before nor after content.
    assert settle_error(scene).code == "integrity-failure"


def test_a_record_another_writer_changes_during_the_write_suspends_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = make_scene(tmp_path)
    write = WRITES["report-submit"](scene)
    status = scene.records / f"dispatch-status/{DISPATCH}.json"
    original = RuntimeStateStore.write_intent

    def foreign_writer(
        self: RuntimeStateStore, batch_id: str, operation_id: str, payload: JsonObject
    ) -> str:
        ref = original(self, batch_id, operation_id, payload)
        status.write_text('{"dispatch_id": "dispatch-4e5f6a7b", "state": "working"} ')
        return ref

    monkeypatch.setattr(RuntimeStateStore, "write_intent", foreign_writer)

    with pytest.raises(RuntimeStateError) as caught:
        write.commit()

    assert (caught.value.code, caught.value.step) == (
        "concurrency-conflict",
        "ledger:dispatch-status",
    )
    record = scene.runtime()
    assert record is not None
    assert (record["state"], record["failure"]["code"]) == (
        "suspended",
        "concurrency-conflict",
    )
    monkeypatch.undo()
    assert settle_error(scene).code == "integrity-failure"
