"""System-fields migration of the lifecycle ledger: a verified generation that keeps its source,
an overlay of explicitly mapped values and a quarantine of unknown or unreadable ones."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.core.constants import STATE_REL
from harness.orchestration.core.system_fields import load_registry
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.ledger import field_migration, generation_migration
from harness.orchestration.ledger.field_migration import MigrationRule, rules_sha256
from harness.orchestration.ledger.lifecycle import (
    RECORD_DIRECTORIES,
    LedgerError,
    LedgerWriteConflict,
    LifecycleLedger,
)
from harness.orchestration.ledger.ledger_admin import export_ledger, inspect_ledger
from harness.orchestration.ledger.ledger_ops import _load_batch
from harness.orchestration.runtime_intent import CompositeWrite
from harness.orchestration.runtime_migration import migrate_system_fields
from harness.orchestration.runtime_reconcile import settle
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
)

BATCH_A = "batch-0000000a"
BATCH_B = "batch-0000000b"
DISPATCH_A = "dispatch-000000a1"
DISPATCH_B = "dispatch-000000b1"
REPORT: JsonObject = {
    "outcome": "completed",
    "checks_run": [{"command": "make test", "result": "pass"}],
    "blockers": {"status": "none", "details": ""},
    "risks": {"status": "none", "details": ""},
}


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@dataclass
class Scene:
    """A ledger whose selected generation is written byte by byte, as history left it."""

    repo: Path
    ledger: LifecycleLedger

    @property
    def root(self) -> Path:
        return self.ledger.root

    @property
    def generation(self) -> Path:
        pointer: JsonObject = json.loads(self.ledger.pointer_path.read_text("utf-8"))
        return self.root / "generations" / str(pointer["generation"])

    def put(self, relative: str, value: object) -> bytes:
        data = value if isinstance(value, bytes) else canonical(value)
        path = self.generation / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def seed(
        self,
        batch_id: str,
        dispatch_id: str,
        *,
        report: JsonObject | None = None,
        status: object = "reported",
    ) -> JsonObject:
        """A batch with one reported dispatch whose graph the ledger accepts as complete."""
        dispatch: JsonObject = {
            "dispatch_id": dispatch_id,
            "batch_id": batch_id,
            "state": "approved",
            "role": "developer",
            "purpose": "work",
        }
        report_bytes = self.put(f"reports/{dispatch_id}.json", report or REPORT)
        self.put(f"reports/{dispatch_id}.md", b"# Report\n")
        self.put(
            f"dispatch-status/{dispatch_id}.json",
            {"dispatch_id": dispatch_id, "state": status},
        )
        batch: JsonObject = {
            "batch_id": batch_id,
            "ticket": "#696",
            "state": "awaiting-approval",
            "approval_policy": "auto",
            "dispatches": [
                {
                    "dispatch_id": dispatch_id,
                    "role": "developer",
                    "state": "reported",
                    "brief_sha256": sha(
                        self.put(f"dispatches/{dispatch_id}.json", dispatch)
                    ),
                    "report": f"reports/{dispatch_id}.json",
                    "report_sha256": sha(report_bytes),
                }
            ],
        }
        self.put(f"batches/{batch_id}.json", batch)
        self.put(f"plans/{batch_id}.json", {"batch_id": batch_id, "ticket": "#696"})
        return batch

    def pointer_sha256(self) -> str:
        return sha(self.ledger.pointer_path.read_bytes())

    def store(self) -> RuntimeStateStore:
        return RuntimeStateStore(self.repo, self.root)

    def store_bytes(self) -> dict[str, bytes]:
        directory = self.store().location.directory
        return tree(directory) if directory.is_dir() else {}

    def run(self) -> JsonObject:
        """``ledger migrate`` of the system fields, behind its durable intent."""
        return migrate_system_fields(self.repo, self.root, self.ledger)

    def migrate(self) -> tuple[str, JsonObject]:
        """Build the migration of the selected generation, then select it."""
        before = self.pointer_sha256()
        operation = str(uuid.uuid4())
        staging = self.root / "generations" / f".migrating-{operation}"
        manifest = generation_migration.build(
            self.ledger,
            self.generation.name,
            staging,
            source_pointer_sha256=before,
            audit_ids=(f"audit-{operation}-0", f"audit-{operation}-1"),
        )
        target = f"generation-{uuid.uuid4().hex[:8]}"
        staging.rename(self.root / "generations" / target)
        self.ledger.select_generation(target, expected_pointer_sha256=before)
        return target, manifest


def make_scene(tmp_path: Path) -> Scene:
    """A ledger in a Git checkout, whose runtime state store the migration reaches."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    ledger = LifecycleLedger(repo / STATE_REL)
    ledger.ensure()
    return Scene(repo.resolve(), ledger)


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return make_scene(tmp_path)


# --- A verified generation that keeps its source (DoD 2) --------------------------------------


def test_a_migration_selects_a_verified_copy_and_keeps_the_source_byte_for_byte(
    scene: Scene,
) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    scene.seed(BATCH_B, DISPATCH_B)
    source = scene.generation
    before = tree(source)

    target, manifest = scene.migrate()

    assert scene.generation.name == target != source.name
    assert tree(source) == before
    copied = tree(scene.generation)
    records = {k: v for k, v in before.items() if not k.startswith("audit/")}
    assert {k: copied[k] for k in records} == records
    assert {k: v for k, v in before.items() if k.startswith("audit/")}.items() <= (
        copied.items()
    )
    new_events = [
        json.loads(copied[k])
        for k in sorted(set(copied) - set(before))
        if k.startswith("audit/")
    ]
    assert sorted(event["action"] for event in new_events) == [
        "generation-created",
        "system-fields-migration",
    ]
    assert scene.ledger.validate()["batches"] == [BATCH_A, BATCH_B]
    assert scene.ledger.records_root() == scene.generation
    assert (manifest["source_generation"], manifest["quarantine"]) == (source.name, [])
    assert manifest["mapped"] == []


def test_a_mapped_check_result_keeps_the_report_bytes_and_its_hash(
    scene: Scene,
) -> None:
    report = {**REPORT, "checks_run": [{"command": "make test", "result": "passed"}]}
    batch = scene.seed(BATCH_A, DISPATCH_A, report=report)
    report_bytes = (scene.generation / f"reports/{DISPATCH_A}.json").read_bytes()

    _, manifest = scene.migrate()

    migrated = scene.generation / f"reports/{DISPATCH_A}.json"
    assert migrated.read_bytes() == report_bytes
    assert batch["dispatches"][0]["report_sha256"] == sha(report_bytes)
    assert manifest["mapped"] == [
        {
            "path": f"reports/{DISPATCH_A}.json",
            "sha256": sha(report_bytes),
            "location": "checks_run[0].result",
            "field_id": "report.check_result",
            "legacy_value": "passed",
            "canonical_value": "pass",
            "disposition": "mapped",
        }
    ]
    assert scene.ledger.validate()["valid"] is True


def test_the_generation_switch_is_a_compare_and_swap_on_the_pointer_bytes(
    scene: Scene,
) -> None:
    pointer = scene.ledger.pointer_path.read_bytes()
    other = scene.root / "generations" / "generation-0ther000"
    for directory in RECORD_DIRECTORIES:
        (other / directory).mkdir(parents=True)

    with pytest.raises(LedgerWriteConflict):
        scene.ledger.select_generation(other.name, expected_pointer_sha256="0" * 64)
    with pytest.raises(LedgerWriteConflict):
        scene.ledger.select_generation(other.name, expected_pointer_sha256=None)
    with pytest.raises(LedgerError, match="not a ledger generation name"):
        scene.ledger.select_generation("../x", expected_pointer_sha256=sha(pointer))
    assert scene.ledger.pointer_path.read_bytes() == pointer

    scene.ledger.select_generation(other.name, expected_pointer_sha256=sha(pointer))
    assert scene.generation == other


# --- Quarantine of unknown and unreadable values (DoD 5) ---------------------------------------


def _unknown_batch_state(scene: Scene) -> None:
    batch = scene.seed(BATCH_B, DISPATCH_B)
    scene.put(f"batches/{BATCH_B}.json", {**batch, "state": "on-hold"})


def _unknown_status_state(scene: Scene) -> None:
    scene.seed(BATCH_B, DISPATCH_B, status="Reported")


def _non_string_state(scene: Scene) -> None:
    batch = scene.seed(BATCH_B, DISPATCH_B)
    scene.put(f"batches/{BATCH_B}.json", {**batch, "state": None})


def _unreadable_batch(scene: Scene) -> None:
    scene.seed(BATCH_B, DISPATCH_B)
    scene.put(f"batches/{BATCH_B}.json", b"{ not JSON")


DEFECTS: dict[str, tuple[Callable[[Scene], None], str, str, object]] = {
    "unknown batch.state": (
        _unknown_batch_state,
        "legacy-unknown",
        f"batches/{BATCH_B}.json",
        "on-hold",
    ),
    "unknown dispatch-status.state": (
        _unknown_status_state,
        "legacy-unknown",
        f"dispatch-status/{DISPATCH_B}.json",
        "Reported",
    ),
    "non-string batch.state": (
        _non_string_state,
        "legacy-unknown",
        f"batches/{BATCH_B}.json",
        None,
    ),
    "unreadable batch": (
        _unreadable_batch,
        "unreadable",
        f"batches/{BATCH_B}.json",
        None,
    ),
}


@pytest.mark.parametrize("defect", sorted(DEFECTS))
def test_an_unknown_or_unreadable_value_quarantines_only_its_batch(
    scene: Scene, defect: str
) -> None:
    make, disposition, path, value = DEFECTS[defect]
    scene.seed(BATCH_A, DISPATCH_A)
    make(scene)
    source = tree(scene.generation)
    with pytest.raises(LedgerError):
        scene.ledger.records_root()

    _, manifest = scene.migrate()

    quarantine = scene.generation / "migration" / "quarantine" / BATCH_B
    graph = [
        f"batches/{BATCH_B}.json",
        f"dispatch-status/{DISPATCH_B}.json",
        f"dispatches/{DISPATCH_B}.json",
        f"plans/{BATCH_B}.json",
    ]
    assert sorted(tree(quarantine)) == sorted([*graph, "legacy-unknown.json"])
    for relative in graph:
        assert (quarantine / relative).read_bytes() == source[relative]
        assert not (scene.generation / relative).exists()
    evidence = json.loads((quarantine / "legacy-unknown.json").read_text("utf-8"))
    [finding] = evidence["findings"]
    assert (finding["path"], finding["disposition"], finding["value"]) == (
        path,
        disposition,
        value,
    )
    assert finding["sha256"] == sha(source[path])
    [entry] = manifest["quarantine"]
    assert entry["batch_id"] == BATCH_B
    assert entry["files"] == {relative: sha(source[relative]) for relative in graph}
    # The reports of a quarantined batch stay where they were.
    assert (scene.generation / f"reports/{DISPATCH_B}.json").is_file()
    assert scene.ledger.records_root() == scene.generation
    assert scene.ledger.validate()["batches"] == [BATCH_A]


def test_an_unreadable_report_moves_with_its_batch(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    scene.seed(BATCH_B, DISPATCH_B)
    broken = scene.put(f"reports/{DISPATCH_B}.json", b"[not a report")

    _, manifest = scene.migrate()

    [entry] = manifest["quarantine"]
    assert entry["files"][f"reports/{DISPATCH_B}.json"] == sha(broken)
    assert not (scene.generation / f"reports/{DISPATCH_B}.json").exists()
    assert scene.ledger.validate()["batches"] == [BATCH_A]


def test_a_finding_no_batch_owns_refuses_before_anything_changes(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    scene.put("dispatch-status/dispatch-0000ffff.json", {"state": "lost"})
    before = tree(scene.root)

    with pytest.raises(
        LedgerError, match="cannot assign dispatch-status/dispatch-0000ffff"
    ):
        scene.migrate()

    assert tree(scene.root) == before
    assert not list((scene.root / "generations").glob(".migrating-*"))


def test_a_quarantined_batch_id_must_be_a_coordinator_batch_id(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    scene.put("batches/batch-damaged.json", b"{ not JSON")
    before = tree(scene.root)

    with pytest.raises(LedgerError, match="cannot quarantine batch-damaged"):
        scene.migrate()

    assert tree(scene.root) == before


# --- The migration directory stays verified evidence ------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    ["quarantined-file", "evidence", "extra-file", "manifest"],
)
def test_a_changed_migration_file_is_refused(scene: Scene, tamper: str) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    _unknown_batch_state(scene)
    scene.migrate()
    migration = scene.generation / "migration"
    quarantine = migration / "quarantine" / BATCH_B
    path = {
        "quarantined-file": quarantine / f"batches/{BATCH_B}.json",
        "evidence": quarantine / "legacy-unknown.json",
        "extra-file": quarantine / "notes.txt",
        "manifest": migration / "manifest.json",
    }[tamper]
    path.write_bytes((path.read_bytes() if path.exists() else b"") + b" ")

    with pytest.raises(LedgerError, match="migration|manifest"):
        generation_migration.current_manifest(scene.ledger, scene.generation.name)


def test_a_schema_upgrade_and_ledger_clean_keep_the_quarantine(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    _unknown_batch_state(scene)
    target, manifest = scene.migrate()
    stale = json.loads(scene.ledger.pointer_path.read_text("utf-8"))
    scene.ledger.pointer_path.write_text(json.dumps({**stale, "version": 2}), "utf-8")

    upgraded = scene.ledger.migrate()
    scene.ledger.clean()

    assert upgraded["migrated"] is True and upgraded["generation"] != target
    found = generation_migration.read_manifest(scene.generation)
    assert found is not None and found[0] == manifest
    assert (scene.generation / f"reports/{DISPATCH_B}.json").is_file()


def test_a_later_migration_carries_an_earlier_quarantine(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    _unknown_batch_state(scene)
    first, manifest = scene.migrate()
    quarantine = tree(scene.generation / "migration" / "quarantine")

    second, again = scene.migrate()

    assert second != first
    assert again["quarantine"] == manifest["quarantine"]
    assert tree(scene.generation / "migration" / "quarantine") == quarantine
    assert again["source_generation"] == first


# --- ledger migrate behind the durable intent (DoD 3) -----------------------------------------

PASSED_REPORT: JsonObject = {
    **REPORT,
    "checks_run": [{"command": "make test", "result": "passed"}],
}


def test_ledger_migrate_selects_the_migration_and_denies_the_quarantined_batch(
    scene: Scene,
) -> None:
    scene.seed(BATCH_A, DISPATCH_A, report=PASSED_REPORT)
    _unknown_batch_state(scene)
    source = scene.generation.name

    result = scene.run()

    assert result == {
        "migrated": True,
        "rules_sha256": rules_sha256(load_registry()),
        "mapped": 1,
        "quarantined": [BATCH_B],
        "source_generation": source,
    }
    scope = scene.store().read(None)
    assert scope is not None
    assert (
        scope["state"],
        scope["operation"]["kind"],
        scope["operation"]["phase"],
    ) == (
        "running",
        "migrate",
        "committed",
    )
    assert scope["ledger"]["generation"] == scene.generation.name
    denied = scene.store().read(BATCH_B)
    assert denied is not None
    assert (denied["state"], denied["failure"]["code"], denied["failure"]["step"]) == (
        "suspended",
        "integrity-failure",
        "quarantine",
    )
    assert f"ledger inspect --batch {BATCH_B}" in denied["failure"]["message"]
    assert scene.store().read(BATCH_A) is None


def test_a_repeated_migrate_and_reconcile_change_nothing(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A, report=PASSED_REPORT)
    _unknown_batch_state(scene)
    first = scene.run()
    generations = scene.root / "generations"
    snapshot = (
        scene.ledger.pointer_path.read_bytes(),
        sorted(path.name for path in generations.iterdir()),
        scene.store_bytes(),
        tree(scene.root),
    )

    again = scene.run()
    with pytest.raises(RuntimeStateError, match="integrity-failure"):
        settle(scene.repo, scene.root, scene.ledger, BATCH_B)
    assert settle(scene.repo, scene.root, scene.ledger, BATCH_A) is False

    assert again == {**first, "migrated": False}
    assert snapshot == (
        scene.ledger.pointer_path.read_bytes(),
        sorted(path.name for path in generations.iterdir()),
        scene.store_bytes(),
        tree(scene.root),
    )


def test_a_migrated_ledger_migrates_again_only_under_changed_rules(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    first = scene.run()
    migrated = scene.generation.name
    extra = MigrationRule("report.outcome", "done", "completed", "a test rule")
    monkeypatch.setattr(
        field_migration, "MIGRATION_RULES", (*field_migration.MIGRATION_RULES, extra)
    )

    again = scene.run()
    third = scene.run()

    assert again["migrated"] is True and third["migrated"] is False
    assert again["rules_sha256"] != first["rules_sha256"]
    assert again["source_generation"] == migrated != scene.generation.name


def test_a_batch_with_an_unfinished_operation_holds_the_migration(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    store = scene.store()
    ready: JsonObject = {
        "contract": "runtimeState.v1",
        "identity": store.location.identity(BATCH_A),
        "revision": 1,
        "updated_at": "2026-10-15T09:00:00+00:00",
        "state": "ready",
        "stage": None,
        "dispatch_id": None,
        "operation": None,
        "ledger": {"generation": None, "record_hashes": {}},
        "policy_snapshot_sha256": None,
        "input_refs": [],
        "output_refs": [],
        "last_confirmed_boundary": None,
        "failure": None,
    }
    store.write(ready, expected_revision=0)
    operation: JsonObject = {
        "operation_id": str(uuid.uuid4()),
        "kind": "checkpoint",
        "phase": "prepared",
        "idempotency_key": "key",
        "expected_before_sha256": None,
        "intended_after_sha256": None,
        "effect": {"state": "not-started", "evidence_ref": None, "observed_at": None},
    }
    store.write(
        {**ready, "revision": 2, "state": "running", "operation": operation},
        expected_revision=1,
    )
    before = (tree(scene.root), scene.store_bytes())

    with pytest.raises(RuntimeStateError, match="concurrency-conflict") as caught:
        scene.run()

    assert BATCH_A in caught.value.message
    assert (tree(scene.root), scene.store_bytes()) == before


def test_an_empty_state_has_nothing_to_migrate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    ledger = LifecycleLedger(repo / STATE_REL)

    result = migrate_system_fields(repo, ledger.root, ledger)

    assert (result["migrated"], result["source_generation"], result["quarantined"]) == (
        False,
        None,
        [],
    )
    assert not ledger.root.exists()


# --- Safe inspection and export of quarantined and unreadable batches (DoD 6) -----------------


def _args(scene: Scene, **values: object) -> argparse.Namespace:
    return argparse.Namespace(repo=str(scene.repo), state_dir=str(scene.root), **values)


def _content(file: JsonObject) -> bytes:
    if file["encoding"] == "base64":
        return base64.b64decode(file["content"])
    return str(file["content"]).encode("utf-8")


@pytest.mark.parametrize("migrated", [False, True], ids=["unreadable", "quarantined"])
def test_inspect_and_export_read_a_denied_batch_without_writing(
    scene: Scene, migrated: bool
) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    _unknown_batch_state(scene)
    if migrated:
        scene.run()
    with pytest.raises(CoordinatorError):
        _load_batch(scene.root, BATCH_B)

    with scene.ledger.lock():
        before = (tree(scene.root), scene.store_bytes())
        report = inspect_ledger(_args(scene, batch=None))
        only = inspect_ledger(_args(scene, batch=BATCH_B))
        exported = export_ledger(_args(scene, batch=BATCH_B))
        after = (tree(scene.root), scene.store_bytes())

    assert after == before
    area = "quarantine" if migrated else "operational"
    assert [(b["batch_id"], b["area"]) for b in report["batches"]] == [
        (BATCH_A, "operational"),
        (BATCH_B, area),
    ]
    [shown] = only["batches"]
    [finding] = shown["findings"]
    assert (finding["disposition"], finding["field_id"], finding["value"]) == (
        "legacy-unknown",
        "batch.state",
        "'on-hold'",
    )
    if migrated:
        assert report["migration"]["current"] is True
    else:
        assert report["migration"] is None
    assert exported["area"] == area
    prefix = f"migration/quarantine/{BATCH_B}/" if migrated else ""
    paths = {file["path"] for file in exported["files"]}
    assert {
        f"{prefix}batches/{BATCH_B}.json",
        f"{prefix}dispatches/{DISPATCH_B}.json",
        f"reports/{DISPATCH_B}.json",
        f"reports/{DISPATCH_B}.md",
    } <= paths
    for file in exported["files"]:
        data = (scene.generation / str(file["path"])).read_bytes()
        assert _content(file) == data and file["sha256"] == sha(data)
    runtime = shown["runtime"]
    if migrated:
        assert runtime["state"] == "suspended"
        assert runtime["failure"]["step"] == "quarantine"
    else:
        assert runtime is None


def test_a_quarantined_batch_stays_denied_in_the_operational_store(
    scene: Scene,
) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    _unknown_batch_state(scene)
    scene.run()
    before = scene.store_bytes()
    write = CompositeWrite(
        scene.ledger,
        scene.store(),
        batch_id=BATCH_B,
        dispatch_id=DISPATCH_B,
        kind="checkpoint",
        idempotency_key="key",
    )
    write.write_immutable(scene.generation / "checkpoints/checkpoint-0b.json", {})

    with pytest.raises(RuntimeStateError, match="quarantine"):
        settle(scene.repo, scene.root, scene.ledger, BATCH_B)
    with pytest.raises(RuntimeStateError, match="concurrency-conflict"):
        write.commit()

    assert scene.store_bytes() == before


def test_inspect_and_export_read_a_legacy_ledger_no_migration_selected(
    scene: Scene,
) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    _unknown_batch_state(scene)
    # The pre-ledger layout: the records lie in the state directory, and no pointer selects a
    # generation.
    generation = scene.generation
    for directory in generation.iterdir():
        shutil.move(directory, scene.root / directory.name)
    scene.ledger.pointer_path.unlink()
    shutil.rmtree(scene.root / "generations")
    with pytest.raises(LedgerError, match="legacy"):
        scene.ledger.records_root()
    before = (tree(scene.root), scene.store_bytes())

    report = inspect_ledger(_args(scene, batch=None))
    exported = export_ledger(_args(scene, batch=BATCH_B))

    assert (tree(scene.root), scene.store_bytes()) == before
    assert report["generation"] is None and exported["generation"] is None
    assert [(b["batch_id"], b["area"]) for b in report["batches"]] == [
        (BATCH_A, "operational"),
        (BATCH_B, "operational"),
    ]
    [finding] = report["batches"][1]["findings"]
    assert (finding["disposition"], finding["value"]) == ("legacy-unknown", "'on-hold'")
    assert {f"batches/{BATCH_B}.json", f"reports/{DISPATCH_B}.json"} <= {
        file["path"] for file in exported["files"]
    }
    for file in exported["files"]:
        assert _content(file) == (scene.root / str(file["path"])).read_bytes()


def test_an_export_keeps_bytes_that_are_not_utf8(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    scene.seed(BATCH_B, DISPATCH_B)
    broken = scene.put(f"reports/{DISPATCH_B}.json", b"\xff\xfe not a report")
    scene.run()

    exported = export_ledger(_args(scene, batch=BATCH_B))

    [file] = [
        file
        for file in exported["files"]
        if file["path"] == f"migration/quarantine/{BATCH_B}/reports/{DISPATCH_B}.json"
    ]
    assert file["encoding"] == "base64" and _content(file) == broken


@pytest.mark.parametrize("batch", ["batch-00000fff", "../batch-0000000a"])
def test_an_unknown_batch_is_refused_by_inspect_and_export(
    scene: Scene, batch: str
) -> None:
    scene.seed(BATCH_A, DISPATCH_A)

    with pytest.raises(CoordinatorError):
        inspect_ledger(_args(scene, batch=batch))
    with pytest.raises(CoordinatorError):
        export_ledger(_args(scene, batch=batch))
