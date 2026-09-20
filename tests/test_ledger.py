#!/usr/bin/env python3
"""Regression tests for ledger.py's Value Objects and lock()."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.errors import HarnessError
from harness.orchestration.core import utils
from harness.orchestration.ledger import (
    BatchRecord,
    CheckpointRecord,
    ContextPackageRecord,
    DispatchRecord,
    DispatchStatusRecord,
    JsonObject,
    LedgerError,
    LedgerRecordVO,
    LifecycleLedger,
    PlanRecord,
    RiskAssessmentRecord,
)


class ValueObjectRoundTripTests(unittest.TestCase):
    def test_batch_record_round_trips_with_unknown_keys_in_extra(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
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

    def test_batch_record_round_trips_nested_json_dispatch_and_approval(self) -> None:
        """The ledger preserves nested JSON at its unvalidated record boundary."""
        original = BatchRecord(
            batch_id="batch-1",
            state="planned",
            dispatches=[{"dispatch_id": "dispatch-1", "metadata": {"attempt": 1, "tags": ["typed"]}}],
            coordinator_approval={"approved_by": "Malove", "approved_at": "2026-09-19T00:00:00Z"},
        )

        self.assertEqual(BatchRecord.from_dict(original.to_dict()), original)

    def test_dispatch_record_round_trip(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
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
        cases: list[
            tuple[
                PlanRecord | DispatchStatusRecord | RiskAssessmentRecord | ContextPackageRecord | CheckpointRecord,
                str,
            ]
        ] = [
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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
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


class RecordApiTests(unittest.TestCase):
    def test_each_value_object_exposes_its_directory_and_record_id(self) -> None:
        cases: list[tuple[LedgerRecordVO, str, str]] = [
            (BatchRecord(batch_id="batch-1", state="planned", dispatches=[], coordinator_approval=None), "batches", "batch-1"),
            (PlanRecord(batch_id="batch-1"), "plans", "batch-1"),
            (DispatchRecord(dispatch_id="dispatch-1", batch_id="batch-1", state="approved", coordinator_approval=None), "dispatches", "dispatch-1"),
            (DispatchStatusRecord(dispatch_id="dispatch-1", state="working"), "dispatch-status", "dispatch-1"),
            (RiskAssessmentRecord(risk_assessment_id="risk-1"), "risk-assessments", "risk-1"),
            (ContextPackageRecord(context_package_id="context-package-1"), "context-packages", "context-package-1"),
            (CheckpointRecord(checkpoint_id="checkpoint-1"), "checkpoints", "checkpoint-1"),
        ]
        for record, directory, record_id in cases:
            self.assertEqual(record.directory, directory)
            self.assertEqual(record.record_id, record_id)

    def test_write_record_persists_at_the_path_derived_from_the_record(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()

            record = DispatchStatusRecord(dispatch_id="dispatch-1", state="approved")
            ledger.write_record(record)

            path = ledger.records_root() / "dispatch-status" / "dispatch-1.json"
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record.to_dict())

    def test_replace_record_persists_a_transition_at_the_same_derived_path(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()

            ledger.write_record(DispatchStatusRecord(dispatch_id="dispatch-1", state="approved"))
            ledger.replace_record(DispatchStatusRecord(dispatch_id="dispatch-1", state="working"))

            path = ledger.records_root() / "dispatch-status" / "dispatch-1.json"
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "working")

    def test_replace_record_still_enforces_batch_transition_validation(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()

            ledger.write_record(PlanRecord(batch_id="batch-1"))
            ledger.write_record(BatchRecord(batch_id="batch-1", state="planned", dispatches=[], coordinator_approval=None))

            with self.assertRaises(LedgerError):
                ledger.replace_record(BatchRecord(batch_id="batch-1", state="not-a-real-state", dispatches=[], coordinator_approval=None))

    def test_write_record_rejects_a_record_id_that_is_not_a_safe_path_segment(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()

            for bad_id in ("../evil", "a/b", "a\\b", "", "."):
                with self.assertRaises(LedgerError):
                    ledger.write_record(DispatchStatusRecord(dispatch_id=bad_id, state="approved"))


class LockTests(unittest.TestCase):
    def test_ledger_error_is_a_harness_error_with_a_remedy(self) -> None:
        error = LedgerError("cannot persist", remedy="repair the lifecycle record")

        self.assertIsInstance(error, HarnessError)
        self.assertEqual(error.remedy, "repair the lifecycle record")

    def test_lock_raises_ledger_error_on_contention_and_is_reacquirable_after_release(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            first = LifecycleLedger(state_root)
            second = LifecycleLedger(state_root)

            with first.lock():
                with self.assertRaises(LedgerError) as raised:
                    with second.lock():
                        pass
                self.assertTrue(raised.exception.remedy)

            with second.lock():
                pass

    def test_lock_releases_cleanly_when_body_raises(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()
            generation = ledger.records_root()

            batch: JsonObject = {
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

    def test_abandoned_is_reachable_only_after_a_report_and_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            ledger = LifecycleLedger(Path(temporary) / "state")
            ledger.ensure()
            generation = ledger.records_root()
            batch: JsonObject = {"batch_id": "batch-1", "state": "planned", "dispatches": []}
            ledger.write_immutable(generation / "plans" / "batch-1.json", {"batch_id": "batch-1"})
            batch_path = generation / "batches" / "batch-1.json"
            ledger.write_immutable(batch_path, batch)

            with self.assertRaises(LedgerError):
                ledger.replace(batch_path, {**batch, "state": "abandoned"})  # nothing to abandon before approval

            approved: JsonObject = {**batch, "state": "awaiting-approval", "coordinator_approval": {"approved_by": "a", "approved_at": "b"}}
            ledger.replace(batch_path, approved)
            ledger.replace(batch_path, {**approved, "state": "abandoned"})

            for target in ("awaiting-approval", "active", "failed", "completed"):
                with self.assertRaises(LedgerError, msg=target):
                    ledger.replace(batch_path, {**approved, "state": target})


class RecordsRootLenientTests(unittest.TestCase):
    def test_returns_none_on_empty_root_with_no_pointer_and_no_legacy_records(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)

            self.assertIsNone(ledger.records_root_lenient())

    def test_returns_same_path_as_records_root_once_ensure_selected_a_generation(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()

            self.assertEqual(ledger.records_root_lenient(), ledger.records_root())

    def test_returns_none_when_pointer_is_garbage_but_records_root_still_raises(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()
            ledger.pointer_path.write_text("not json {{{", encoding="utf-8")

            self.assertIsNone(ledger.records_root_lenient())
            with self.assertRaises(LedgerError):
                ledger.records_root()

    def test_returns_none_when_pointer_references_a_missing_generation_directory(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()
            pointer = json.loads(ledger.pointer_path.read_text(encoding="utf-8"))
            pointer["generation"] = "generation-deadbeef"
            ledger.pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

            self.assertIsNone(ledger.records_root_lenient())

    def test_stale_version_pointer_still_resolves_under_lenient_but_records_root_raises(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()
            generation = ledger.records_root()
            pointer = json.loads(ledger.pointer_path.read_text(encoding="utf-8"))
            pointer["version"] = 1
            ledger.pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

            self.assertEqual(ledger.records_root_lenient(), generation)
            with self.assertRaises(LedgerError):
                ledger.records_root()


class ReadRecordLenientTests(unittest.TestCase):
    def test_returns_none_for_nonexistent_path(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            path = Path(temporary) / "missing.json"

            self.assertIsNone(LifecycleLedger.read_record_lenient(path))

    def test_returns_none_for_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text("not json {{{", encoding="utf-8")

            self.assertIsNone(LifecycleLedger.read_record_lenient(path))

    def test_returns_none_for_valid_json_that_is_not_an_object(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            path = Path(temporary) / "array.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")

            self.assertIsNone(LifecycleLedger.read_record_lenient(path))

    def test_read_rejects_json_array_with_a_remedy(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            path = Path(temporary) / "array.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")

            with self.assertRaises(LedgerError) as raised:
                ledger = LifecycleLedger(path.parent)
                path.replace(ledger.pointer_path)
                ledger.pointer()

            self.assertTrue(raised.exception.remedy)

    def test_returns_parsed_dict_for_a_valid_record(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            path = Path(temporary) / "record.json"
            path.write_text(json.dumps({"dispatch_id": "dispatch-1"}), encoding="utf-8")

            self.assertEqual(LifecycleLedger.read_record_lenient(path), {"dispatch_id": "dispatch-1"})

    def test_returns_none_for_non_utf8_bytes_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            path = Path(temporary) / "binary.json"
            path.write_bytes(b"\xff\xfe\x00\x01garbage")

            self.assertIsNone(LifecycleLedger.read_record_lenient(path))


class OperationalRecordMigrationTests(unittest.TestCase):
    """Issue #250: batch-level context pressure and attention records survive an explicit migration
    verbatim, keep passing the coordinator's own integrity validation, and need no schema bump."""

    def _pressure(self) -> JsonObject:
        import hashlib

        from harness.orchestration import coordinator

        record: JsonObject = {
            "pressure_id": "pressure-1", "dispatch_id": "dispatch-1", "observed_tokens": 160_000,
            "context_limit": 150_000, "warning_threshold": 120_000, "level": "critical",
            "recorded_at": "2026-09-20T00:00:00+00:00", "source": "provider-usage", "action_required": True,
            "required_worker_action": "Return a structured blocker now.",
        }
        record["record_sha256"] = hashlib.sha256(utils._canonical(record).encode("utf-8")).hexdigest()
        return record

    def test_a_pre_ledger_batch_with_the_operational_fields_migrates_and_stays_valid(self) -> None:
        from harness.orchestration import coordinator

        batch = {
            "batch_id": "batch-1", "state": "awaiting-approval", "dispatches": [],
            "coordinator_approval": {"approved_by": "Malove", "approved_at": "2026-09-20T00:00:00+00:00"},
            "context_pressure": [self._pressure()],
            "needs_attention": True, "attention_reason": "retry-queued-too-long",
            "attention_since": "2026-09-20T01:00:00+00:00", "last_safe_action": "evidence kept",
            "recommended_human_action": "confirm the retry is still wanted",
            "attention_events": [{"event": "raised", "at": "2026-09-20T01:00:00+00:00", "keys": ["retry-queued-too-long:d"]}],
            "attention_open_keys": ["retry-queued-too-long:d"],
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            for directory, record in (("batches", batch), ("plans", {"batch_id": "batch-1"})):
                (root / directory).mkdir()
                (root / directory / "batch-1.json").write_text(json.dumps(record), encoding="utf-8")
            ledger = LifecycleLedger(root)

            result = ledger.migrate()

            self.assertTrue(result["migrated"])
            migrated = json.loads((ledger.records_root() / "batches" / "batch-1.json").read_text(encoding="utf-8"))
            self.assertEqual(migrated, batch)
            coordinator._validate_operational_batch_fields(migrated)
            self.assertEqual(ledger.status()["version"], 3)
            self.assertFalse(ledger.migrate()["migrated"])  # already current: the schema version did not change


if __name__ == "__main__":
    unittest.main()
