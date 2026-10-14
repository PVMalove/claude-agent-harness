"""Hardening tests for the lifecycle ledger (`ledger/lifecycle.py`)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.ledger.lifecycle import (
    JsonObject,
    LedgerError,
    LedgerWriteConflict,
    LifecycleLedger,
    audit_details,
)

_LIVE = "dispatch-0001"
_ORPHAN = "dispatch-0002"


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _generation(tmp_path: Path) -> tuple[LifecycleLedger, Path]:
    ledger = LifecycleLedger(tmp_path / "state")
    ledger.ensure()
    return ledger, ledger.records_root()


def _live_batch(root: Path) -> None:
    _write(
        root / "batches/batch-0001.json",
        {
            "batch_id": "batch-0001",
            "state": "active",
            "dispatches": [{"dispatch_id": _LIVE}],
            "checkpoints": [{"checkpoint_id": "checkpoint-0001", "dispatch_id": _LIVE}],
        },
    )
    for directory in ("dispatches", "dispatch-status", "reports"):
        _write(
            root / directory / f"{_LIVE}.json",
            {"dispatch_id": _LIVE, "state": "working"},
        )
    _write(root / "checkpoints/checkpoint-0001.json", {"dispatch_id": _LIVE})


def test_clean_keeps_the_checkpoints_of_a_referenced_dispatch(tmp_path: Path) -> None:
    ledger, root = _generation(tmp_path)
    _live_batch(root)
    # A checkpoint written before its batch pointer, and one of an orphaned dispatch.
    _write(root / "checkpoints/checkpoint-0002.json", {"dispatch_id": _LIVE})
    _write(
        root / f"dispatches/{_ORPHAN}.json",
        {"dispatch_id": _ORPHAN, "state": "proposed"},
    )
    _write(root / "checkpoints/checkpoint-0003.json", {"dispatch_id": _ORPHAN})

    result = ledger.clean()

    assert result["cleaned"] == 2
    assert sorted(path.name for path in (root / "checkpoints").iterdir()) == [
        "checkpoint-0001.json",
        "checkpoint-0002.json",
    ]
    assert not (root / f"dispatches/{_ORPHAN}.json").exists()
    for directory in ("dispatches", "dispatch-status", "reports"):
        assert (root / directory / f"{_LIVE}.json").is_file()


@pytest.mark.parametrize(
    ("batch_text", "message"),
    [
        pytest.param("{not json", "batch record is not valid JSON", id="unreadable"),
        pytest.param(
            json.dumps(
                {
                    "batch_id": "batch-0002",
                    "state": "active",
                    "dispatches": "dispatch-0002",
                }
            ),
            "batch dispatches are invalid",
            id="dispatches-not-a-list",
        ),
    ],
)
def test_clean_refuses_to_delete_while_a_batch_cannot_name_its_evidence(
    tmp_path: Path, batch_text: str, message: str
) -> None:
    ledger, root = _generation(tmp_path)
    _live_batch(root)
    (root / "batches/batch-0002.json").write_text(batch_text, encoding="utf-8")
    _write(
        root / f"dispatches/{_ORPHAN}.json",
        {"dispatch_id": _ORPHAN, "state": "proposed"},
    )
    before = sorted(path.relative_to(root) for path in root.rglob("*.json"))

    with pytest.raises(LedgerError) as caught:
        ledger.clean()

    assert message in caught.value.message
    assert "batch-0002.json" in caught.value.remedy
    assert sorted(path.relative_to(root) for path in root.rglob("*.json")) == before


def test_a_pointer_without_a_string_generation_is_an_internal_invariant() -> None:
    with pytest.raises(LedgerError) as caught:
        LifecycleLedger._pointer_generation({"generation": 3})

    assert caught.value.remedy == INTERNAL_INVARIANT_REMEDY


@pytest.mark.parametrize(
    "generation", ["generation-../../outside", "generation-..\\..\\outside"]
)
def test_a_pointer_generation_never_leaves_the_state_root(
    tmp_path: Path, generation: str
) -> None:
    ledger, _ = _generation(tmp_path)
    _write(
        ledger.pointer_path,
        {"version": 3, "generation": generation, "selected_at": "2026-10-08"},
    )

    with pytest.raises(LedgerError) as caught:
        ledger.records_root()

    assert caught.value.message == "ledger pointer has an invalid generation"
    assert "one directory name" in caught.value.remedy


# --- Primitives for an intent-backed composite write (#694) -----------------------------------

_AUDIT_ID = "audit-6f1d1a4e-0000-5000-8000-000000000001"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _status(tmp_path: Path) -> tuple[LifecycleLedger, Path, Path]:
    ledger, root = _generation(tmp_path)
    path = root / f"dispatch-status/{_LIVE}.json"
    ledger.write_immutable(path, {"dispatch_id": _LIVE, "state": "working"})
    return ledger, root, path


def test_a_compare_and_swap_replace_refuses_a_record_another_writer_changed(
    tmp_path: Path,
) -> None:
    ledger, root, path = _status(tmp_path)
    planned = hashlib.sha256(path.read_bytes()).hexdigest()
    _write(path, {"dispatch_id": _LIVE, "state": "working", "note": "foreign"})
    before = _tree(root)

    with pytest.raises(LedgerWriteConflict) as caught:
        ledger.replace(
            path,
            {"dispatch_id": _LIVE, "state": "reported"},
            expected_before_sha256=planned,
        )

    assert "changed since its transition was planned" in caught.value.message
    assert _tree(root) == before


def test_a_compare_and_swap_replace_writes_over_the_planned_digest(
    tmp_path: Path,
) -> None:
    ledger, _, path = _status(tmp_path)
    planned = hashlib.sha256(path.read_bytes()).hexdigest()
    value: JsonObject = {"dispatch_id": _LIVE, "state": "reported"}

    ledger.replace(path, value, expected_before_sha256=planned, audit_id=_AUDIT_ID)

    event = ledger.read_audit(_AUDIT_ID)
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert event is not None
    assert (event["action"], event["details"]) == audit_details(
        "replace", f"dispatch-status/{_LIVE}.json", after, before_sha256=planned
    )


def test_a_given_audit_id_is_idempotent_for_the_same_event_and_refuses_another(
    tmp_path: Path,
) -> None:
    ledger, root, path = _status(tmp_path)
    details: JsonObject = {"path": f"dispatch-status/{_LIVE}.json", "sha256": "0" * 64}
    ledger.append_audit(path, "immutable-record", details, audit_id=_AUDIT_ID)
    first = (root / f"audit/{_AUDIT_ID}.json").read_bytes()

    ledger.append_audit(path, "immutable-record", details, audit_id=_AUDIT_ID)
    with pytest.raises(LedgerError) as caught:
        ledger.append_audit(
            path,
            "immutable-record",
            {**details, "sha256": "1" * 64},
            audit_id=_AUDIT_ID,
        )

    assert "already records a different write" in caught.value.message
    assert (root / f"audit/{_AUDIT_ID}.json").read_bytes() == first


def test_reading_an_audit_event_verifies_its_checksum(tmp_path: Path) -> None:
    ledger, root, path = _status(tmp_path)
    assert ledger.read_audit(_AUDIT_ID) is None
    ledger.append_audit(path, "immutable-record", {"path": "x"}, audit_id=_AUDIT_ID)
    event_path = root / f"audit/{_AUDIT_ID}.json"
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event["details"] = {"path": "y"}
    event_path.write_text(json.dumps(event), encoding="utf-8")

    with pytest.raises(LedgerError) as caught:
        ledger.read_audit(_AUDIT_ID)

    assert "failed immutable integrity check" in caught.value.message


@pytest.mark.parametrize(
    ("mode", "relative", "value", "error", "message"),
    [
        pytest.param(
            "create",
            f"dispatch-status/{_LIVE}.json",
            {"dispatch_id": _LIVE, "state": "working"},
            LedgerWriteConflict,
            "refusing to overwrite immutable record",
            id="create-over-an-existing-record",
        ),
        pytest.param(
            "create-artifact",
            f"dispatch-status/{_LIVE}.json",
            "text",
            LedgerWriteConflict,
            "refusing to overwrite immutable record",
            id="artifact-over-an-existing-file",
        ),
        pytest.param(
            "create",
            "dispatch-status/dispatch-0003.json",
            {"dispatch_id": "dispatch-0003", "state": "finished"},
            LedgerError,
            "invalid DispatchState value",
            id="create-with-an-unknown-state",
        ),
        pytest.param(
            "replace",
            "dispatch-status/dispatch-0003.json",
            {"dispatch_id": "dispatch-0003", "state": "working"},
            LedgerError,
            "transition targets a missing record",
            id="replace-a-missing-record",
        ),
        pytest.param(
            "replace",
            "batches/batch-0001.json",
            {
                "batch_id": "batch-0001",
                "state": "active",
                "dispatches": [{"dispatch_id": _LIVE}],
                "checkpoints": [],
                "recovery_events": [{"sequence": 1}],
            },
            LedgerError,
            "recovery event does not bind this transition",
            id="batch-recovery-event-without-a-decision",
        ),
        pytest.param(
            "replace",
            "audit/audit-0001.json",
            {"dispatch_id": _LIVE, "state": "working"},
            LedgerError,
            "append-only",
            id="replace-an-audit-event",
        ),
    ],
)
def test_checking_a_step_refuses_what_its_write_refuses_and_writes_nothing(
    tmp_path: Path,
    mode: str,
    relative: str,
    value: JsonObject | str,
    error: type[LedgerError],
    message: str,
) -> None:
    ledger, root, _ = _status(tmp_path)
    _live_batch(root)
    _write(root / "plans/batch-0001.json", {"batch_id": "batch-0001"})
    before = _tree(root)

    with pytest.raises(error) as caught:
        ledger.check_step(mode, root / relative, value)

    assert message in caught.value.message
    assert _tree(root) == before


def test_checking_an_allowed_step_writes_nothing(tmp_path: Path) -> None:
    ledger, root, path = _status(tmp_path)
    before = _tree(root)

    ledger.check_step("replace", path, {"dispatch_id": _LIVE, "state": "reported"})
    ledger.check_step("create", root / "reports/dispatch-0001.json", {"a": 1})
    ledger.check_step("create-artifact", root / "reports/dispatch-0001.md", "text")

    assert _tree(root) == before
