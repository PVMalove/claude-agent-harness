"""``ledger migrate`` stopped at every boundary of its intent and generation switch: the next run
finishes it, or stops on a pointer it cannot explain, and the source never changes."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.ledger.generation_migration import read_manifest
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
)
from tests.orchestration.test_ledger_migration import (
    BATCH_A,
    BATCH_B,
    DISPATCH_A,
    DISPATCH_B,
    PASSED_REPORT,
    Scene,
    make_scene,
    tree,
)
from tests.orchestration.test_runtime_reconcile_faults import Crash, crash_on

Stop = Callable[[pytest.MonkeyPatch], None]


def seeded(tmp_path: Path) -> Scene:
    """A ledger with one mapped value and one batch whose unknown state quarantines it."""
    scene = make_scene(tmp_path)
    scene.seed(BATCH_A, DISPATCH_A, report=PASSED_REPORT)
    batch = scene.seed(BATCH_B, DISPATCH_B)
    scene.put(f"batches/{BATCH_B}.json", {**batch, "state": "on-hold"})
    return scene


def outcome(scene: Scene) -> tuple[object, ...]:
    """What a finished migration leaves, without the names and times that differ between two
    ledgers: the overlay, the quarantine, the operational records and the store's verdicts."""
    found = read_manifest(scene.generation)
    assert found is not None
    manifest = found[0]
    quarantine = scene.generation / "migration" / "quarantine"
    evidence = {
        path.parent.name: json.loads(path.read_text("utf-8"))["findings"]
        for path in quarantine.glob("*/legacy-unknown.json")
    }
    records = {
        relative: data
        for relative, data in tree(scene.generation).items()
        if not relative.startswith(("audit/", "migration/"))
    }
    store = scene.store()
    scope, denied = store.read(None), store.read(BATCH_B)
    assert scope is not None and denied is not None
    return (
        manifest["mapped"],
        [(entry["batch_id"], entry["files"]) for entry in manifest["quarantine"]],
        evidence,
        {
            relative: data
            for relative, data in tree(quarantine).items()
            if not relative.endswith("legacy-unknown.json")
        },
        records,
        (scope["operation"]["kind"], scope["operation"]["phase"], scope["failure"]),
        (denied["state"], denied["failure"]["step"]),
    )


def reference(tmp_path: Path) -> tuple[object, ...]:
    scene = seeded(tmp_path / "reference")
    assert scene.run()["migrated"] is True
    return outcome(scene)


def stop_at(owner: type, name: str, call: int, *, after: bool = False) -> Stop:
    return lambda monkeypatch: crash_on(monkeypatch, owner, name, call, after=after)


# Store writes of a clean run: the ledger scope's first record, prepared, effect-observed,
# ledger-committed, the quarantined batch's first record and its denial, committed.
SEAMS: dict[str, Stop] = {
    "S1 while the staging is built": stop_at(LifecycleLedger, "_append_audit", 2),
    "S2 after the intent, before prepared": stop_at(RuntimeStateStore, "write", 2),
    "S3 after prepared, before the rename": stop_at(Path, "rename", 1),
    "S4 after the rename, before the pointer": stop_at(
        LifecycleLedger, "select_generation", 1
    ),
    "S6 after the pointer, before effect-observed": stop_at(
        RuntimeStateStore, "write", 3
    ),
    "S7 after ledger-committed, before the denial": stop_at(
        RuntimeStateStore, "write", 5
    ),
    "S8 after the denial, before committed": stop_at(RuntimeStateStore, "write", 7),
}


@pytest.mark.parametrize("seam", sorted(SEAMS))
def test_a_stop_at_any_boundary_is_finished_by_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    scene = seeded(tmp_path / "scene")
    source = scene.generation
    before = tree(source)
    SEAMS[seam](monkeypatch)

    with pytest.raises(Crash):
        scene.run()
    monkeypatch.undo()

    # The pointer selects the source or the checked target, and the source never changes.
    assert scene.generation == source or read_manifest(scene.generation) is not None
    assert tree(source) == before
    result = scene.run()

    assert result["migrated"] is True
    assert result["source_generation"] == source.name
    assert tree(source) == before
    assert outcome(scene) == reference(tmp_path)
    assert not list(scene.generation.parent.glob(".migrating-*"))
    generations = sorted(path.name for path in source.parent.iterdir())
    store = scene.store_bytes()

    assert scene.run()["migrated"] is False
    assert sorted(path.name for path in source.parent.iterdir()) == generations
    assert scene.store_bytes() == store


def test_a_failed_pointer_replace_suspends_and_keeps_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = seeded(tmp_path / "scene")
    pointer = scene.ledger.pointer_path.read_bytes()

    def refuse(source: Path, target: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(
        LifecycleLedger, "_replace_with_windows_retry", staticmethod(refuse)
    )
    with pytest.raises(RuntimeStateError, match="migrate:select") as caught:
        scene.run()
    monkeypatch.undo()

    assert scene.ledger.pointer_path.read_bytes() == pointer
    assert caught.value.durable_revision is not None
    scope = scene.store().read(None)
    assert scope is not None
    assert (scope["state"], scope["operation"]["phase"]) == ("suspended", "prepared")
    assert scene.run()["migrated"] is True
    assert outcome(scene) == reference(tmp_path)


def test_a_pointer_on_another_generation_stops_without_a_new_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = seeded(tmp_path / "scene")
    source = scene.generation
    SEAMS["S3 after prepared, before the rename"](monkeypatch)
    with pytest.raises(Crash):
        scene.run()
    monkeypatch.undo()
    foreign = source.parent / "generation-f0e1d2c3"
    shutil.copytree(source, foreign)
    stale: JsonObject = json.loads(scene.ledger.pointer_path.read_text("utf-8"))
    scene.ledger.pointer_path.write_text(
        json.dumps({**stale, "generation": foreign.name}), encoding="utf-8"
    )
    pointer = scene.ledger.pointer_path.read_bytes()

    with pytest.raises(RuntimeStateError, match="integrity-failure") as caught:
        scene.run()
    stopped = scene.store_bytes()
    with pytest.raises(RuntimeStateError, match="integrity-failure"):
        scene.run()

    assert caught.value.step == "reconcile:generation"
    assert scene.store_bytes() == stopped
    assert scene.ledger.pointer_path.read_bytes() == pointer
    scope = scene.store().read(None)
    assert scope is not None and scope["state"] == "suspended"
    assert scope["failure"]["step"] == "reconcile:generation"


def test_a_source_changed_after_a_stop_is_migrated_as_it_is_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = seeded(tmp_path / "scene")
    SEAMS["S3 after prepared, before the rename"](monkeypatch)
    with pytest.raises(Crash):
        scene.run()
    monkeypatch.undo()
    late = scene.seed("batch-0000000c", "dispatch-000000c1")
    scene.put("batches/batch-0000000c.json", {**late, "state": "Active"})

    result = scene.run()

    assert result["quarantined"] == [BATCH_B, "batch-0000000c"]
    assert (
        scene.generation
        / "migration/quarantine/batch-0000000c/batches/batch-0000000c.json"
    ).is_file()
