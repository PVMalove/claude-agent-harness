#!/usr/bin/env python3
"""Regression tests for ledger.py's Value Objects and lock()."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ORCHESTRATION_ROOT = Path(__file__).resolve().parents[1] / "harness" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))

from ledger import (  # noqa: E402
    BatchRecord,
    CheckpointRecord,
    ContextPackageRecord,
    DispatchRecord,
    DispatchStatusRecord,
    LedgerError,
    LifecycleLedger,
    PlanRecord,
    RiskAssessmentRecord,
)


class ValueObjectRoundTripTests(unittest.TestCase):
    def test_batch_record_round_trips_with_unknown_keys_in_extra(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()
            generation = ledger.records_root()

            original = BatchRecord(
                batch_id="batch-1",
                state="planned",
                dispatches=[],
                coordinator_approval=None,
                extra={"ticket": "#194", "base_commit": "deadbeef"},
            )
            path = generation / "batches" / "batch-1.json"
            ledger.write_immutable(path, original.to_dict())

            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["ticket"], "#194")
            self.assertEqual(on_disk["base_commit"], "deadbeef")

            restored = BatchRecord.from_dict(on_disk)
            self.assertEqual(restored, original)

    def test_dispatch_record_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()
            generation = ledger.records_root()

            original = DispatchRecord(
                dispatch_id="dispatch-1",
                batch_id="batch-1",
                state="approved",
                coordinator_approval={"approved_by": "Malove", "approved_at": "2026-09-17T00:00:00Z"},
                extra={"role": "developer"},
            )
            path = generation / "dispatches" / "dispatch-1.json"
            ledger.write_immutable(path, original.to_dict())

            on_disk = json.loads(path.read_text(encoding="utf-8"))
            restored = DispatchRecord.from_dict(on_disk)
            self.assertEqual(restored, original)

    def test_remaining_kinds_round_trip(self) -> None:
        cases = [
            (PlanRecord(batch_id="batch-1", extra={"ticket": "#194"}), "plans/batch-1.json"),
            (
                DispatchStatusRecord(dispatch_id="dispatch-1", state="working", extra={"heartbeat_note": "none"}),
                "dispatch-status/dispatch-1.json",
            ),
            (
                RiskAssessmentRecord(risk_assessment_id="risk-1", extra={"risk_triggers": []}),
                "risk-assessments/risk-1.json",
            ),
            (
                ContextPackageRecord(context_package_id="context-package-1", extra={"estimated_tokens": 10}),
                "context-packages/context-package-1.json",
            ),
            (CheckpointRecord(checkpoint_id="checkpoint-1", extra={"note": "progress"}), "checkpoints/checkpoint-1.json"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()
            generation = ledger.records_root()

            for original, relative in cases:
                path = generation / relative
                ledger.write_immutable(path, original.to_dict())
                on_disk = json.loads(path.read_text(encoding="utf-8"))
                restored = type(original).from_dict(on_disk)
                self.assertEqual(restored, original)


class LockTests(unittest.TestCase):
    def test_lock_raises_ledger_error_on_contention_and_is_reacquirable_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            first = LifecycleLedger(state_root)
            second = LifecycleLedger(state_root)

            with first.lock():
                with self.assertRaises(LedgerError):
                    with second.lock():
                        pass

            with second.lock():
                pass

    def test_lock_releases_cleanly_when_body_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            lock_dir = state_root / ".coordinator.lock"

            with self.assertRaises(RuntimeError):
                with ledger.lock():
                    self.assertTrue(lock_dir.is_dir())
                    raise RuntimeError("boom")

            self.assertFalse(lock_dir.exists())

            with ledger.lock():
                pass


class StructuralValidationCharacterizationTests(unittest.TestCase):
    def test_illegal_batch_transition_still_raises_ledger_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()
            generation = ledger.records_root()

            batch = {
                "batch_id": "batch-1",
                "state": "planned",
                "dispatches": [],
            }
            ledger.write_immutable(generation / "plans" / "batch-1.json", {"batch_id": "batch-1"})
            batch_path = generation / "batches" / "batch-1.json"
            ledger.write_immutable(batch_path, batch)

            with self.assertRaises(LedgerError):
                ledger.replace(batch_path, {**batch, "state": "active"})

            with self.assertRaises(LedgerError):
                ledger.replace(batch_path, {**batch, "state": "awaiting-approval"})


if __name__ == "__main__":
    unittest.main()
