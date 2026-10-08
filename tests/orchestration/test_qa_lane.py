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

from harness.gate_runner.gate_runner import (
    Diagnosis,
    GateResult,
    GateRunnerError,
    QAStagesResult,
    format_command_log,
)
from harness.orchestration import coordinator, operation_access, qa_lane
from harness.storage import storage_path
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.ledger import (
    BatchRecord,
    DispatchRecord,
    DispatchStatusRecord,
    JsonObject,
    JsonValue,
    LedgerError,
    LedgerRecordVO,
    LifecycleLedger,
    PlanRecord,
)
from harness.orchestration.qa_lane import CoordinatorOps
from harness.orchestration.workflow import reports

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
    def test_expired_local_lease_requires_explicit_owner_checked_clearance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary)
            ledger = LifecycleLedger(repo / coordinator.STATE_REL)
            ledger.ensure()
            identity = "local-qa-" + "c" * 32
            with ledger.lock():
                admitted = qa_lane.acquire(
                    ledger,
                    identity,
                    coordinator,
                    owner_kind="local-qa",
                    lease_seconds=1800,
                )
                expired = {
                    **admitted["lease"],
                    "expires_at": (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ).isoformat(),
                }
                ledger.replace(ledger.records_root() / "qa-lane/lease.json", expired)
                with self.assertRaises(CoordinatorError):
                    qa_lane.acquire(
                        ledger, DISPATCH_ID, coordinator, lease_seconds=1800
                    )
            result = qa_lane.clear_stale_lease(
                _ns(
                    repo=str(repo),
                    state_dir=None,
                    expected_host=expired["host"],
                    expected_pid=expired["pid"],
                    expected_expiry=expired["expires_at"],
                    approved_by="Test operator",
                    approved_at=datetime.now(UTC).isoformat(),
                    reason="Expired local attempt",
                ),
                coordinator,
            )
            self.assertEqual(result["owner_id"], identity)
            with ledger.lock():
                next_owner = qa_lane.acquire(
                    ledger, DISPATCH_ID, coordinator, lease_seconds=1800
                )
                self.assertEqual(next_owner["state"], "acquired")
                qa_lane.release(ledger, next_owner["lease"], coordinator)

    def test_local_requests_and_dispatches_share_fifo_and_exact_owner_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            ledger = LifecycleLedger(Path(temporary) / "state")
            ledger.ensure()
            with ledger.lock():
                first = qa_lane.acquire(
                    ledger,
                    "local-qa-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    coordinator,
                    owner_kind="local-qa",
                    lease_seconds=1800,
                )
                ordinary = qa_lane.acquire(
                    ledger, DISPATCH_ID, coordinator, lease_seconds=1800
                )
                second = qa_lane.acquire(
                    ledger,
                    "local-qa-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    coordinator,
                    owner_kind="local-qa",
                    lease_seconds=1800,
                )
                self.assertEqual(first["state"], "acquired")
                self.assertEqual(ordinary["position"], 2)
                self.assertEqual(second["position"], 3)
                self.assertEqual(
                    qa_lane.acquire(
                        ledger, DISPATCH_ID, coordinator, lease_seconds=1800
                    )["position"],
                    2,
                )
                with self.assertRaises(CoordinatorError):
                    qa_lane.release(ledger, {**first["lease"], "pid": -1}, coordinator)
                qa_lane.release(ledger, first["lease"], coordinator)
                self.assertEqual(
                    qa_lane.acquire(
                        ledger,
                        "local-qa-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        coordinator,
                        owner_kind="local-qa",
                        lease_seconds=1800,
                    )["state"],
                    "queued",
                )
                admitted = qa_lane.acquire(
                    ledger, DISPATCH_ID, coordinator, lease_seconds=1800
                )
                self.assertEqual(admitted["state"], "acquired")
                qa_lane.release(ledger, admitted["lease"], coordinator)

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
            item for item in batch["dispatches"] if item["dispatch_id"] == DISPATCH_ID
        )
        self.assertEqual(entry["state"], "approved")
        self.assertEqual(status["state"], "approved")
        self.assertIsNone(qa_lane._lease(self.ledger, coordinator))
        self.assertEqual(qa_lane._queue_entries(self.ledger, coordinator), [])
        attempts = list((self.lane / "attempts").glob("*.json"))
        self.assertEqual(len(attempts), 1)

    def _refusal(self) -> operation_access.OperationAccessError:
        return operation_access.OperationAccessError(
            "qa access is denied: shared Git metadata (write /repo/.git): Permission denied",
            remedy="grant write access to /repo/.git; then repeat the command",
            evidence={"operation": "qa", "status": "denied", "checks": []},
        )

    def test_an_access_refusal_is_recorded_before_the_lane_is_acquired(self) -> None:
        self._seed()
        refusal = self._refusal()

        with (
            mock.patch.object(operation_access, "require", side_effect=refusal),
            mock.patch.object(qa_lane, "run_gate") as gate,
            self.assertRaises(operation_access.OperationAccessError) as caught,
        ):
            qa_lane.run(self.args, self._ops())

        self.assertIs(caught.exception, refusal)
        gate.assert_not_called()
        self.assertIsNone(qa_lane._lease(self.ledger, coordinator))
        self.assertEqual(qa_lane._queue_entries(self.ledger, coordinator), [])
        status = coordinator._load_dispatch_status(self.root, DISPATCH_ID)
        self.assertEqual(status["state"], "approved")
        (attempt,) = (
            coordinator._read_object(path, "attempt")
            for path in (self.lane / "attempts").glob("*.json")
        )
        self.assertEqual(attempt["stage"], "access")
        self.assertEqual(attempt["evidence"]["status"], "denied")
        self.assertEqual(attempt["remedy"], refusal.remedy)

    def test_a_later_allowed_run_proceeds_and_keeps_the_refusal_evidence(self) -> None:
        self._seed()
        passing = GateResult(
            checks=[{"result": "pass", "command": "true"}],
            artifact="all green",
            duration_seconds=0.0,
        )
        with (
            mock.patch.object(operation_access, "require", side_effect=self._refusal()),
            self.assertRaises(operation_access.OperationAccessError),
        ):
            qa_lane.run(self.args, self._ops())

        with mock.patch.object(qa_lane, "run_gate", return_value=passing):
            result = qa_lane.run(self.args, self._ops([]))

        self.assertEqual(result["state"], "reported")
        self.assertEqual(len(list((self.lane / "attempts").glob("*.json"))), 1)

    def test_the_access_check_uses_the_pinned_brief_and_the_clean_room_directory(
        self,
    ) -> None:
        self._seed()
        gate = GateResult(
            checks=[{"result": "pass", "command": "true"}],
            artifact="ok",
            duration_seconds=0.0,
        )
        with (
            mock.patch.object(operation_access, "require") as require,
            mock.patch.object(qa_lane, "run_gate", return_value=gate),
        ):
            qa_lane.run(self.args, self._ops([]))

        (call,) = require.call_args_list
        self.assertEqual(call.args[2], "qa")
        self.assertEqual(call.kwargs["brief"]["dispatch_id"], DISPATCH_ID)
        self.assertEqual(call.kwargs["checkout"], storage_path(self.repo, "runs", "qa"))

    def test_an_unexpected_gate_failure_releases_the_lane_for_a_later_run(self) -> None:
        self._seed()
        with (
            mock.patch.object(
                qa_lane, "run_gate", side_effect=PermissionError("denied")
            ),
            self.assertRaises(coordinator.CoordinatorError) as caught,
        ):
            qa_lane.run(self.args, self._ops())

        self.assertRemedy(caught)
        self.assertIsInstance(caught.exception.__cause__, PermissionError)
        self._assert_transient_failure_released()

    def test_an_interrupted_gate_releases_the_lane_and_propagates(self) -> None:
        self._seed()
        with (
            mock.patch.object(qa_lane, "run_gate", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            qa_lane.run(self.args, self._ops())

        self._assert_transient_failure_released()

    def test_a_failure_while_marking_the_dispatch_running_releases_the_lane(
        self,
    ) -> None:
        self._seed()
        original = qa_lane._replace_record
        calls: list[object] = []

        def flaky(ledger: LifecycleLedger, ops: CoordinatorOps, record: object) -> None:
            calls.append(record)
            if len(calls) == 1:
                raise coordinator.CoordinatorError("ledger busy", remedy="retry")
            original(ledger, ops, cast(LedgerRecordVO, record))

        with (
            mock.patch.object(qa_lane, "_replace_record", side_effect=flaky),
            mock.patch.object(qa_lane, "run_gate") as gate,
            self.assertRaises(coordinator.CoordinatorError) as caught,
        ):
            qa_lane.run(self.args, self._ops())

        self.assertEqual(caught.exception.message, "ledger busy")
        gate.assert_not_called()
        self._assert_transient_failure_released()

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

    # ---- preparation and gate stages (issue #618) ----

    PREPARE = "uv sync --locked"
    PROBE = "curl -sI https://pypi.org"
    FILE_CHECK = "uv lock --check"
    GATE = ["make lint", "make test"]

    def _staged(
        self,
        *,
        prep_exit: int = 0,
        gate_exits: tuple[int, ...] = (0, 0),
        diagnosis: Diagnosis | None = None,
        probe_exit: int | None = None,
        check_exit: int | None = None,
    ) -> QAStagesResult:
        """A runner result consistent with its own artifact, as ``run_qa_stages`` builds it."""
        stages: list[dict[str, object]] = []
        blocks = format_command_log(self.PREPARE, prep_exit, "prep output")
        stages.append(
            {
                "stage": "preparation",
                "command": self.PREPARE,
                "result": "pass" if prep_exit == 0 else "fail",
                "exit_code": prep_exit,
                **({"diagnostics": "prep output"} if prep_exit else {}),
            }
        )
        for stage, command, code in (
            ("environment-probe", self.PROBE, probe_exit),
            ("project-file-check", self.FILE_CHECK, check_exit),
        ):
            if prep_exit and code is not None:
                blocks += format_command_log(command, code, "fact output")
                stages.append(
                    {
                        "stage": stage,
                        "command": command,
                        "result": "pass" if code == 0 else "fail",
                        "exit_code": code,
                        **({"diagnostics": "fact output"} if code else {}),
                    }
                )
        checks: list[dict[str, str]] = []
        if prep_exit == 0:
            for command, code in zip(self.GATE, gate_exits):
                blocks += format_command_log(command, code, "gate output")
                stages.append(
                    {
                        "stage": "gate",
                        "command": command,
                        "result": "pass" if code == 0 else "fail",
                        "exit_code": code,
                        **({"diagnostics": "gate output"} if code else {}),
                    }
                )
                checks.append(
                    {
                        "command": command,
                        "result": "pass" if code == 0 else "fail",
                        "evidence": f"exit {code}; gate output",
                    }
                )
        failed = next((s["stage"] for s in stages if s["result"] == "fail"), None)
        return QAStagesResult(
            stages=stages,
            gate_checks=checks,
            artifact=blocks,
            duration_seconds=0.0,
            failed_stage=cast("str | None", failed),
            code_checks_started="not_started" if prep_exit else "started",
            diagnosis=diagnosis,
        )

    def _run_staged(self, staged: QAStagesResult) -> dict[str, object]:
        self.dispatch["verification_commands"] = list(self.GATE)
        self._seed()
        persisted: list[dict[str, object]] = []

        def persist(
            ledger: LifecycleLedger,
            root: Path,
            batch: object,
            dispatch: object,
            report: dict[str, object],
        ) -> Path:
            persisted.append(report)
            return root / "report.json"

        ops = _Ops(
            _validate_batch_integrity=lambda root, batch: None,
            _validate_dispatch=lambda repo, config, root, batch, dispatch: None,
            _config=lambda repo: {"qa_preparation": [self.PREPARE]},
            _role=lambda repo, name: {},
            _validate_report=lambda *arguments, **keywords: None,
            _persist_report=persist,
        )
        with (
            mock.patch.object(qa_lane, "run_qa_stages", return_value=staged) as runner,
            mock.patch.object(qa_lane, "run_gate") as legacy,
        ):
            qa_lane.run(self.args, cast(CoordinatorOps, ops))
        legacy.assert_not_called()
        self.assertEqual(runner.call_args.args[0], [self.PREPARE])
        self.assertEqual(runner.call_args.args[1], self.GATE)
        self.assertIsNone(qa_lane._lease(self.ledger, coordinator))
        self.assertEqual(qa_lane._queue_entries(self.ledger, coordinator), [])
        report = persisted[0]
        reports._validate_qa_stages(report, self.dispatch)
        return report

    def test_a_run_with_preparation_records_stages_and_a_green_report(self) -> None:
        report = self._run_staged(self._staged())

        self.assertEqual(report["outcome"], "completed")
        stages = cast("dict[str, object]", report["qa_stages"])
        self.assertEqual(stages["code_checks_started"], "started")
        self.assertIsNone(stages["failed_stage"])
        self.assertEqual(
            [c["result"] for c in cast("list[dict[str, str]]", report["checks_run"])],
            ["pass", "pass"],
        )

    def test_an_infrastructure_preparation_failure_blocks_without_a_failed_code_check(
        self,
    ) -> None:
        diagnosis = Diagnosis("infrastructure", ("exit-code:6",), "outage")
        report = self._run_staged(
            self._staged(prep_exit=6, diagnosis=diagnosis, probe_exit=6, check_exit=0)
        )

        self.assertEqual(report["outcome"], "blocked")
        checks = cast("list[dict[str, str]]", report["checks_run"])
        self.assertEqual([c["result"] for c in checks], ["not-run", "not-run"])
        self.assertEqual([c["command"] for c in checks], self.GATE)
        self.assertIn("code was not verified", str(report["output"]))
        self.assertIn("no developer retry", str(report["blockers"]))
        stages = cast("dict[str, object]", report["qa_stages"])
        self.assertEqual(stages["code_checks_started"], "not_started")
        self.assertEqual(stages["failed_stage"], "preparation")

    def test_an_infrastructure_diagnosis_without_independent_facts_is_refused(
        self,
    ) -> None:
        self.dispatch["verification_commands"] = list(self.GATE)
        diagnosis = Diagnosis("infrastructure", ("exit-code:6",), "outage")
        for probe_exit, check_exit in (
            (None, None),
            (6, None),
            (0, 0),
            (6, 1),
        ):
            kwargs = {"probe_exit": probe_exit, "check_exit": check_exit}
            staged = self._staged(
                prep_exit=6,
                diagnosis=diagnosis,
                probe_exit=probe_exit,
                check_exit=check_exit,
            )
            report = qa_lane._qa_report(
                self.dispatch,
                staged.gate_checks,
                Path("a"),
                "0" * 64,
                staged.record(),
            )
            with self.subTest(kwargs), self.assertRaises(coordinator.CoordinatorError):
                reports._validate_qa_stages(report, self.dispatch)

    def test_facts_without_a_failed_preparation_are_refused(self) -> None:
        self.dispatch["verification_commands"] = list(self.GATE)
        staged = self._staged()
        staged.stages.append(
            {
                "stage": "environment-probe",
                "command": self.PROBE,
                "result": "pass",
                "exit_code": 0,
            }
        )
        report = qa_lane._qa_report(
            self.dispatch, staged.gate_checks, Path("a"), "0" * 64, staged.record()
        )
        with self.assertRaises(coordinator.CoordinatorError):
            reports._validate_qa_stages(report, self.dispatch)

    def test_a_confirmed_project_defect_fails_the_report_for_the_developer(
        self,
    ) -> None:
        diagnosis = Diagnosis("project-defect", ("project-file:uv.lock",), "lock")
        report = self._run_staged(self._staged(prep_exit=2, diagnosis=diagnosis))

        self.assertEqual(report["outcome"], "failed")
        self.assertIn("developer retry", str(report["blockers"]))

    def test_an_unknown_preparation_cause_blocks_for_triage(self) -> None:
        diagnosis = Diagnosis("unknown", ("exit-code:1",), "no signal")
        report = self._run_staged(self._staged(prep_exit=1, diagnosis=diagnosis))

        self.assertEqual(report["outcome"], "blocked")
        self.assertIn("triage", str(report["blockers"]))
        self.assertIn("no automatic retry", str(report["blockers"]))

    def test_a_failing_gate_stage_keeps_its_checks_and_pads_the_rest(self) -> None:
        report = self._run_staged(self._staged(gate_exits=(1,)))

        self.assertEqual(report["outcome"], "failed")
        checks = cast("list[dict[str, str]]", report["checks_run"])
        self.assertEqual([c["result"] for c in checks], ["fail", "not-run"])

    def test_stages_that_disagree_with_the_artifact_release_the_lane(self) -> None:
        self.dispatch["verification_commands"] = list(self.GATE)
        self._seed()
        staged = self._staged()
        tampered = QAStagesResult(
            **{**staged.__dict__, "artifact": format_command_log("other", 0, "x")}
        )
        with (
            mock.patch.object(qa_lane, "run_qa_stages", return_value=tampered),
            self.assertRaises(coordinator.CoordinatorError) as caught,
        ):
            qa_lane.run(
                self.args,
                cast(
                    CoordinatorOps,
                    _Ops(
                        _validate_batch_integrity=lambda root, batch: None,
                        _validate_dispatch=lambda *a, **k: None,
                        _config=lambda repo: {"qa_preparation": [self.PREPARE]},
                    ),
                ),
            )

        self.assertIn("does not match the immutable evidence", caught.exception.message)
        self._assert_transient_failure_released()

    def test_report_validation_refuses_inconsistent_stages(self) -> None:
        self.dispatch["verification_commands"] = list(self.GATE)
        staged = self._staged(
            prep_exit=6,
            diagnosis=Diagnosis("infrastructure", ("exit-code:6",), "outage"),
            probe_exit=6,
            check_exit=0,
        )
        artifact = Path(tempfile.mkdtemp()) / "a"
        good = qa_lane._qa_report(
            self.dispatch, staged.gate_checks, artifact, "0" * 64, staged.record()
        )
        reports._validate_qa_stages(good, self.dispatch)

        def refused(change: Callable[[dict[str, object]], object]) -> None:
            import copy

            report = copy.deepcopy(good)
            change(report)
            with self.assertRaises(coordinator.CoordinatorError):
                reports._validate_qa_stages(report, self.dispatch)

        def stages(report: dict[str, object]) -> dict[str, object]:
            return cast("dict[str, object]", report["qa_stages"])

        refused(lambda r: r.update(outcome="failed"))
        refused(lambda r: stages(r).update(code_checks_started="started"))
        refused(lambda r: stages(r).pop("diagnosis"))
        refused(lambda r: stages(r).update(failed_stage="gate"))
        refused(lambda r: stages(r).update(unexpected=1))
        refused(
            lambda r: cast("list[dict[str, str]]", r["checks_run"])[0].update(
                result="fail"
            )
        )
        refused(lambda r: r.update(role="qa", qa_stages="not a mapping"))

    def test_qa_stages_belong_only_to_a_qa_report(self) -> None:
        with self.assertRaises(coordinator.CoordinatorError):
            reports._validate_qa_stages(
                {"qa_stages": {}}, {**self.dispatch, "role": "developer"}
            )


if __name__ == "__main__":
    unittest.main()
