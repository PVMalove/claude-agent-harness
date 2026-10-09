"""Hardening tests for the lifecycle ledger (`ledger/lifecycle.py`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.ledger.lifecycle import LedgerError, LifecycleLedger

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
            "dispatches": [{"dispatch_id": _LIVE}],
            "checkpoints": [{"checkpoint_id": "checkpoint-0001", "dispatch_id": _LIVE}],
        },
    )
    for directory in ("dispatches", "dispatch-status", "reports"):
        _write(root / directory / f"{_LIVE}.json", {"dispatch_id": _LIVE})
    _write(root / "checkpoints/checkpoint-0001.json", {"dispatch_id": _LIVE})


def test_clean_keeps_the_checkpoints_of_a_referenced_dispatch(tmp_path: Path) -> None:
    ledger, root = _generation(tmp_path)
    _live_batch(root)
    # A checkpoint written before its batch pointer, and one of an orphaned dispatch.
    _write(root / "checkpoints/checkpoint-0002.json", {"dispatch_id": _LIVE})
    _write(root / f"dispatches/{_ORPHAN}.json", {"dispatch_id": _ORPHAN})
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
            json.dumps({"batch_id": "batch-0002", "dispatches": "dispatch-0002"}),
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
    _write(root / f"dispatches/{_ORPHAN}.json", {"dispatch_id": _ORPHAN})
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
