#!/usr/bin/env python3
"""Regression tests for the clean-room QA lane: queue bootstrap, error remedies and the run path."""

from __future__ import annotations

import argparse
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

from harness.gate_runner.gate_runner import GateResult, GateRunnerError
from harness.orchestration import coordinator, qa_lane
from harness.orchestration.ledger import (
    BatchRecord,
    DispatchRecord,
    DispatchStatusRecord,
    JsonObject,
    JsonValue,
    LedgerError,
    LifecycleLedger,
    PlanRecord,
)
from harness.orchestration.qa_lane import CoordinatorOps

DISPATCH_ID = "dispatch-0123456789abcdef"
OTHER_DISPATCH_ID = "dispatch-fedcba9876543210"
BATCH_ID = "batch-0123456789abcdef"


def _ns(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


class _Ops:
    """The coordinator module as ``ops``, with individual members swapped for the test."""

    def __init__(self, **overrides: object) -> None:
        self._overrides = overrides

    def __getattr__(self, name: str) -> object:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(coordinator, name)


class QaLaneBootstrapTests(unittest.TestCase):
    def test_first_enqueue_creates_the_ledger_sequence_record(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()

            queue_path, entry = qa_lane._enqueue(
                ledger, "dispatch-0123456789abcdef", coordinator
            )

            self.assertTrue(queue_path.is_file())
            self.assertEqual(entry["sequence"], 1)
            sequence_path = ledger.records_root() / "qa-lane" / "sequence.json"
            self.assertEqual(
                coordinator._read_object(sequence_path, "sequence"), {"next": 2}
            )

            _, second = qa_lane._enqueue(
                ledger, "dispatch-fedcba9876543210", coordinator
            )
            self.assertEqual(second["sequence"], 2)
            self.assertEqual(
                coordinator._read_object(sequence_path, "sequence"), {"next": 3}
            )


class QaLaneTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.root = self.repo / coordinator.STATE_REL
        self.ledger = LifecycleLedger(self.root)
        self.ledger.ensure()
        self.lane = self.ledger.records_root() / "qa-lane"

    def assertRemedy(
        self, caught: unittest.case._AssertRaisesContext[coordinator.CoordinatorError]
    ) -> None:
        self.assertIsInstance(caught.exception, coordinator.CoordinatorError)
        self.assertTrue(caught.exception.message.strip())
        self.assertTrue(caught.exception.remedy.strip())

    def _lease(self, **overrides: JsonValue) -> JsonObject:
        lease: JsonObject = {
            "dispatch_id": OTHER_DISPATCH_ID,
            "host": "host-a",
            "pid": 42,
            "acquired_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        }
        lease.update(overrides)
        return lease


class QaLaneLedgerTranslationTests(QaLaneTestCase):
    def test_ledger_failures_keep_their_message_and_remedy_as_coordinator_errors(
        self,
    ) -> None:
        existing = self.lane / "existing.json"
        self.ledger.write_immutable(existing, {"a": 1})
        artifact = self.lane / "existing.log"
        self.ledger.write_artifact(artifact, "one")
        missing = self.lane / "missing.json"
        status = DispatchStatusRecord.from_dict(
            {"dispatch_id": DISPATCH_ID, "state": "working", "updated_at": "now"}
        )

        def lock() -> None:
            held = self.root / ".coordinator.lock"
            held.mkdir()
            try:
                with qa_lane._lock(self.ledger, coordinator):
                    pass
            finally:
                held.rmdir()

        cases: dict[str, Callable[[], object]] = {
            "lock": lock,
            "write_immutable": lambda: qa_lane._write_immutable(
                self.ledger, coordinator, existing, {"a": 2}
            ),
            "write_artifact": lambda: qa_lane._write_artifact(
                self.ledger, coordinator, artifact, "two"
            ),
            "replace_path": lambda: qa_lane._replace_path(
                self.ledger, coordinator, missing, {"a": 1}
            ),
            "replace_record": lambda: qa_lane._replace_record(
                self.ledger, coordinator, status
            ),
            "delete_record": lambda: qa_lane._delete_record(
                self.ledger, coordinator, missing, reason="test"
            ),
        }
        for name, call in cases.items():
            with (
                self.subTest(name),
                self.assertRaises(coordinator.CoordinatorError) as caught,
            ):
                call()
            self.assertRemedy(caught)
            cause = caught.exception.__cause__
            self.assertIsInstance(cause, LedgerError)
            assert isinstance(cause, LedgerError)
            self.assertEqual(
                (caught.exception.message, caught.exception.remedy),
                (cause.message, cause.remedy),
            )

    def test_records_root_failure_carries_the_ledger_remedy(self) -> None:
        self.ledger.pointer_path.write_text("{", encoding="utf-8")

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            qa_lane._records_root(self.ledger, coordinator)

        self.assertRemedy(caught)
        self.assertIsInstance(caught.exception.__cause__, LedgerError)


class QaLaneValidationTests(QaLaneTestCase):
    def test_state_dir_is_rejected_with_a_remedy(self) -> None:
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            qa_lane._state_root(_ns(state_dir="elsewhere"), self.repo, coordinator)
        self.assertRemedy(caught)

    def test_state_root_defaults_to_the_repository_state_dir(self) -> None:
        self.assertEqual(
            qa_lane._state_root(_ns(state_dir=None), self.repo, coordinator), self.root
        )

    def test_queue_entries_reject_invalid_entries_with_a_remedy(self) -> None:
        queue = self.lane / "queue"
        good: JsonObject = {
            "dispatch_id": DISPATCH_ID,
            "sequence": 1,
            "queued_at": "now",
        }
        cases: dict[str, JsonObject] = {
            "schema": {"dispatch_id": DISPATCH_ID},
            "sequence": {**good, "sequence": 0},
            "queued_at": {**good, "queued_at": " "},
        }
        for name, entry in cases.items():
            path = queue / f"{name}.json"
            self.ledger.write_immutable(path, entry)
            with (
                self.subTest(name),
                self.assertRaises(coordinator.CoordinatorError) as caught,
            ):
                qa_lane._queue_entries(self.ledger, coordinator)
            self.assertRemedy(caught)
            self.ledger.delete(path, reason="test cleanup")

    def test_queue_entries_are_ordered_by_sequence(self) -> None:
        qa_lane._enqueue(self.ledger, DISPATCH_ID, coordinator)
        qa_lane._enqueue(self.ledger, OTHER_DISPATCH_ID, coordinator)

        entries = qa_lane._queue_entries(self.ledger, coordinator)

        self.assertEqual(
            [entry["dispatch_id"] for _, entry in entries],
            [DISPATCH_ID, OTHER_DISPATCH_ID],
        )

    def test_enqueue_is_idempotent_per_dispatch(self) -> None:
        first_path, first = qa_lane._enqueue(self.ledger, DISPATCH_ID, coordinator)
        again_path, again = qa_lane._enqueue(self.ledger, DISPATCH_ID, coordinator)

        self.assertEqual((first_path, first), (again_path, again))

    def test_enqueue_rejects_an_invalid_sequence_with_a_remedy(self) -> None:
        self.ledger.write_immutable(self.lane / "sequence.json", {"next": 0})

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            qa_lane._enqueue(self.ledger, DISPATCH_ID, coordinator)

        self.assertRemedy(caught)

    def test_lease_is_absent_by_default_and_round_trips(self) -> None:
        self.assertIsNone(qa_lane._lease(self.ledger, coordinator))
        lease = self._lease()
        self.ledger.write_immutable(self.lane / "lease.json", lease)

        self.assertEqual(qa_lane._lease(self.ledger, coordinator), lease)

    def test_lease_rejects_invalid_records_with_a_remedy(self) -> None:
        cases: dict[str, JsonObject] = {
            "schema": {"host": "h"},
            "acquired_at": self._lease(acquired_at=""),
            "expires_at": self._lease(expires_at=" "),
        }
        for name, lease in cases.items():
            path = self.lane / "lease.json"
            self.ledger.write_immutable(path, lease)
            with (
                self.subTest(name),
                self.assertRaises(coordinator.CoordinatorError) as caught,
            ):
                qa_lane._lease(self.ledger, coordinator)
            self.assertRemedy(caught)
            self.ledger.delete(path, reason="test cleanup")

    def test_lease_expiry_compares_against_now(self) -> None:
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()

        self.assertTrue(qa_lane._lease_expired(self._lease(), coordinator))
        self.assertFalse(
            qa_lane._lease_expired(self._lease(expires_at=future), coordinator)
        )

    def test_release_queue_drops_only_the_named_dispatches(self) -> None:
        qa_lane._enqueue(self.ledger, DISPATCH_ID, coordinator)
        qa_lane._enqueue(self.ledger, OTHER_DISPATCH_ID, coordinator)

        released = qa_lane.release_queue(self.ledger, [DISPATCH_ID], coordinator)

        self.assertEqual(released, [DISPATCH_ID])
        remaining = qa_lane._queue_entries(self.ledger, coordinator)
        self.assertEqual(
            [entry["dispatch_id"] for _, entry in remaining], [OTHER_DISPATCH_ID]
        )

    def test_qa_evidence_requires_ticket_and_branch_with_a_remedy(self) -> None:
        for ticket, branch in (("", "feature/x"), ("1", " "), (None, "feature/x")):
            args = _ns(
                repo=str(self.repo),
                state_dir=None,
                ticket=ticket,
                branch=branch,
                candidate_commit="abc1234",
            )
            with (
                self.subTest(ticket=ticket, branch=branch),
                self.assertRaises(coordinator.CoordinatorError) as caught,
            ):
                qa_lane.qa_evidence(args, coordinator)
            self.assertRemedy(caught)


class QaLaneStatusTests(QaLaneTestCase):
    def _args(self, **extra: object) -> argparse.Namespace:
        return _ns(**{"repo": str(self.repo), "state_dir": None, **extra})

    def test_status_reports_queue_and_lease(self) -> None:
        self.assertEqual(
            qa_lane.status(self._args(), coordinator),
            {"lease": None, "lease_stale": False, "queue": []},
        )
        qa_lane._enqueue(self.ledger, DISPATCH_ID, coordinator)
        lease = self._lease()
        self.ledger.write_immutable(self.lane / "lease.json", lease)

        status = qa_lane.status(self._args(), coordinator)

        self.assertEqual(status["lease"], lease)
        self.assertTrue(status["lease_stale"])
        self.assertEqual(
            [entry["dispatch_id"] for entry in status["queue"]], [DISPATCH_ID]
        )

    def test_status_rejects_state_dir(self) -> None:
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            qa_lane.status(self._args(state_dir="elsewhere"), coordinator)
        self.assertRemedy(caught)


class QaLaneClearStaleLeaseTests(QaLaneTestCase):
    def _args(self, lease: JsonObject, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "repo": str(self.repo),
            "state_dir": None,
            "expected_host": lease["host"],
            "expected_pid": lease["pid"],
            "expected_expiry": lease["expires_at"],
            "reason": " operator restart ",
        }
        values.update(overrides)
        return _ns(**values)

    def _ops(self) -> CoordinatorOps:
        return cast(
            CoordinatorOps,
            _Ops(
                _approval=lambda args: {"approved_by": "operator", "approved_at": "now"}
            ),
        )

    def test_missing_lease_is_rejected_with_a_remedy(self) -> None:
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            qa_lane.clear_stale_lease(self._args(self._lease()), self._ops())
        self.assertRemedy(caught)

    def test_live_lease_is_rejected_with_a_remedy(self) -> None:
        lease = self._lease(
            expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        )
        self.ledger.write_immutable(self.lane / "lease.json", lease)

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            qa_lane.clear_stale_lease(self._args(lease), self._ops())

        self.assertRemedy(caught)

    def test_changed_lease_is_rejected_with_a_remedy(self) -> None:
        lease = self._lease()
        self.ledger.write_immutable(self.lane / "lease.json", lease)

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            qa_lane.clear_stale_lease(self._args(lease, expected_pid=43), self._ops())

        self.assertRemedy(caught)

    def test_stale_lease_is_cleared_with_its_queue_entry_and_recorded(self) -> None:
        qa_lane._enqueue(self.ledger, OTHER_DISPATCH_ID, coordinator)
        lease = self._lease()
        self.ledger.write_immutable(self.lane / "lease.json", lease)

        result = qa_lane.clear_stale_lease(self._args(lease), self._ops())

        self.assertEqual(result, {"state": "cleared", "dispatch_id": OTHER_DISPATCH_ID})
        self.assertFalse((self.lane / "lease.json").exists())
        self.assertEqual(qa_lane._queue_entries(self.ledger, coordinator), [])
        recoveries = list((self.lane / "recoveries").glob("*.json"))
        self.assertEqual(len(recoveries), 1)
        recovery = coordinator._read_object(recoveries[0], "recovery")
        self.assertEqual(recovery["reason"], "operator restart")
        self.assertEqual(recovery["cleared_dispatch_id"], OTHER_DISPATCH_ID)


class QaLaneRunTests(QaLaneTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dispatch: JsonObject = {
            "dispatch_id": DISPATCH_ID,
            "batch_id": BATCH_ID,
            "ticket": "225",
            "role": "qa",
            "verification_commands": [["true"]],
            "candidate_commit": "abc1234",
        }
        self.args = _ns(
            repo=str(self.repo), state_dir=None, dispatch=DISPATCH_ID, lease_seconds=60
        )

    def _seed(
        self, *, batch_state: str = "approved", status_state: str = "approved"
    ) -> None:
        records = self.ledger.records_root()
        batch: JsonObject = {
            "batch_id": BATCH_ID,
            "state": "active",
            "coordinator_approval": None,
            "dispatches": [{"dispatch_id": DISPATCH_ID, "state": batch_state}],
        }
        self.ledger.write_immutable(
            records / PlanRecord.directory / f"{BATCH_ID}.json", {"batch_id": BATCH_ID}
        )
        self.ledger.write_immutable(
            records / BatchRecord.directory / f"{BATCH_ID}.json", batch
        )
        self.ledger.write_immutable(
            records / DispatchRecord.directory / f"{DISPATCH_ID}.json", self.dispatch
        )
        self.ledger.write_immutable(
            records / DispatchStatusRecord.directory / f"{DISPATCH_ID}.json",
            {"dispatch_id": DISPATCH_ID, "state": status_state, "updated_at": "now"},
        )

    def _ops(
        self,
        persisted: list[dict[str, object]] | None = None,
        report_error: coordinator.CoordinatorError | None = None,
    ) -> CoordinatorOps:
        def persist(
            ledger: LifecycleLedger,
            root: Path,
            batch: object,
            dispatch: object,
            report: dict[str, object],
        ) -> Path:
            if report_error is not None:
                raise report_error
            if persisted is not None:
                persisted.append(report)
            return root / "report.json"

        return cast(
            CoordinatorOps,
            _Ops(
                _validate_batch_integrity=lambda root, batch: None,
                _validate_dispatch=lambda repo, config, root, batch, dispatch: None,
                _config=lambda repo: {},
                _role=lambda repo, name: {},
                _validate_report=lambda *arguments, **keywords: None,
                _persist_report=persist,
            ),
        )

    def _assert_run_rejected(self) -> None:
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            qa_lane.run(self.args, self._ops())
        self.assertRemedy(caught)

    def test_invalid_lease_seconds_are_rejected_with_a_remedy(self) -> None:
        for value in (0, -1, True, "60", None):
            self.args.lease_seconds = value
            with self.subTest(value=value):
                self._assert_run_rejected()

    def test_non_qa_dispatch_is_rejected_with_a_remedy(self) -> None:
        self.dispatch["role"] = "developer"
        self._seed()
        self._assert_run_rejected()

    def test_dispatch_without_verification_commands_is_rejected_with_a_remedy(
        self,
    ) -> None:
        self.dispatch["verification_commands"] = []
        self._seed()
        self._assert_run_rejected()

    def test_unapproved_dispatch_is_rejected_with_a_remedy(self) -> None:
        self._seed(batch_state="dispatched", status_state="working")
        self._assert_run_rejected()

    def test_stale_foreign_lease_is_rejected_with_a_remedy(self) -> None:
        self._seed()
        self.ledger.write_immutable(self.lane / "lease.json", self._lease())
        self._assert_run_rejected()

    def test_live_foreign_lease_queues_the_dispatch(self) -> None:
        self._seed()
        live = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        self.ledger.write_immutable(
            self.lane / "lease.json", self._lease(expires_at=live)
        )

        result = qa_lane.run(self.args, self._ops())

        self.assertEqual(
            result, {"dispatch_id": DISPATCH_ID, "state": "queued", "position": 1}
        )

    def test_earlier_queue_entry_keeps_the_dispatch_queued(self) -> None:
        self._seed()
        qa_lane._enqueue(self.ledger, OTHER_DISPATCH_ID, coordinator)

        result = qa_lane.run(self.args, self._ops())

        self.assertEqual(
            result, {"dispatch_id": DISPATCH_ID, "state": "queued", "position": 2}
        )

    def test_gate_runner_failure_keeps_message_and_remedy(self) -> None:
        self._seed()
        failure = GateRunnerError("checkout failed", remedy="fix the candidate commit")

        with (
            mock.patch.object(qa_lane, "run_gate", side_effect=failure),
            self.assertRaises(coordinator.CoordinatorError) as caught,
        ):
            qa_lane.run(self.args, self._ops())

        self.assertRemedy(caught)
        self.assertEqual(
            (caught.exception.message, caught.exception.remedy),
            ("checkout failed", "fix the candidate commit"),
        )
        self._assert_transient_failure_released()

    def test_evidence_persist_failure_is_rejected_with_a_remedy(self) -> None:
        self._seed()
        gate = GateResult(
            checks=[{"result": "pass"}], artifact="ok", duration_seconds=0.0
        )
        broken = coordinator.CoordinatorError("disk full", remedy="free some space")

        with (
            mock.patch.object(qa_lane, "run_gate", return_value=gate),
            mock.patch.object(qa_lane, "_write_artifact", side_effect=broken),
            self.assertRaises(coordinator.CoordinatorError) as caught,
        ):
            qa_lane.run(self.args, self._ops())

        self.assertRemedy(caught)
        self.assertIs(caught.exception.__cause__, broken)
        self._assert_transient_failure_released()

    def test_retry_after_transient_gate_failure_reports_without_manual_state_edits(
        self,
    ) -> None:
        self._seed()
        failure = GateRunnerError("checkout failed", remedy="fix the candidate commit")
        gate = GateResult(
            checks=[{"result": "pass", "command": "true"}],
            artifact="all green",
            duration_seconds=0.0,
        )

        with (
            mock.patch.object(qa_lane, "run_gate", side_effect=failure),
            self.assertRaises(coordinator.CoordinatorError),
        ):
            qa_lane.run(self.args, self._ops())

        with mock.patch.object(qa_lane, "run_gate", return_value=gate):
            result = qa_lane.run(self.args, self._ops())

        self.assertEqual(result["state"], "reported")

    def test_report_persist_failure_releases_the_dispatch_for_retry(self) -> None:
        self._seed()
        gate = GateResult(
            checks=[{"result": "pass", "command": "true"}],
            artifact="all green",
            duration_seconds=0.0,
        )
        broken = coordinator.CoordinatorError("report disk full", remedy="free space")

        with (
            mock.patch.object(qa_lane, "run_gate", return_value=gate),
            self.assertRaises(coordinator.CoordinatorError) as caught,
        ):
            qa_lane.run(
                self.args,
                self._ops(report_error=broken),
            )

        self.assertRemedy(caught)
        self.assertIs(caught.exception, broken)
        self._assert_transient_failure_released()

    def _assert_transient_failure_released(self) -> None:
        batch = coordinator._load_batch(self.root, BATCH_ID)
        status = coordinator._load_dispatch_status(self.root, DISPATCH_ID)
        entry = next(
            item
            for item in batch["dispatches"]
            if item["dispatch_id"] == DISPATCH_ID
        )
        self.assertEqual(entry["state"], "approved")
        self.assertEqual(status["state"], "approved")
        self.assertIsNone(qa_lane._lease(self.ledger, coordinator))
        self.assertEqual(qa_lane._queue_entries(self.ledger, coordinator), [])
        attempts = list((self.lane / "attempts").glob("*.json"))
        self.assertEqual(len(attempts), 1)

    def test_report_requires_a_running_dispatch(self) -> None:
        self._seed(batch_state="approved", status_state="approved")

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            qa_lane._record_report(
                self.ledger, self.root, self.repo, self.dispatch, {}, self._ops()
            )

        self.assertRemedy(caught)

    def test_passing_gate_persists_evidence_and_releases_the_lane(self) -> None:
        self._seed()
        gate = GateResult(
            checks=[{"result": "pass", "command": "true"}],
            artifact="all green",
            duration_seconds=0.0,
        )
        persisted: list[dict[str, object]] = []

        with mock.patch.object(qa_lane, "run_gate", return_value=gate):
            result = qa_lane.run(self.args, self._ops(persisted))

        self.assertEqual(result["state"], "reported")
        self.assertEqual(
            Path(str(result["artifact"])).read_text(encoding="utf-8"), "all green"
        )
        self.assertEqual(persisted[0]["outcome"], "completed")
        self.assertEqual(qa_lane._queue_entries(self.ledger, coordinator), [])
        self.assertIsNone(qa_lane._lease(self.ledger, coordinator))

    def test_failing_gate_reports_a_failed_outcome(self) -> None:
        self._seed()
        gate = GateResult(
            checks=[{"result": "fail", "command": "true"}],
            artifact="broken",
            duration_seconds=0.0,
        )
        persisted: list[dict[str, object]] = []

        with mock.patch.object(qa_lane, "run_gate", return_value=gate):
            qa_lane.run(self.args, self._ops(persisted))

        self.assertEqual(persisted[0]["outcome"], "failed")
        self.assertEqual(
            persisted[0]["blockers"], "new approved developer retry required"
        )


if __name__ == "__main__":
    unittest.main()
