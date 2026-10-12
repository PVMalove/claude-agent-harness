"""Strict lifecycle vocabulary at durable and coordinator boundaries."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import cast

from harness.orchestration.core.constants import (
    BatchState,
    DispatchState,
    RecoveryRoute,
    RetryReason,
)
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.extensions import ExtensionError, ReasonHint
from harness.orchestration.ledger import (
    BatchRecord,
    DispatchRecord,
    DispatchStatusRecord,
    LedgerError,
    JsonValue,
    LifecycleLedger,
    ledger_ops,
)
from harness.orchestration.workflow import decisions, history


class MachineStateBoundaryTests(unittest.TestCase):
    def test_each_declared_state_round_trips_as_a_typed_value(self) -> None:
        for record, vocabulary in (
            (BatchRecord, BatchState),
            (DispatchRecord, DispatchState),
            (DispatchStatusRecord, DispatchState),
        ):
            for state in vocabulary:
                with self.subTest(record=record.__name__, state=state):
                    value: JsonObject = {"state": state.value, "dispatches": []}
                    restored = record.from_dict(value)
                    self.assertIs(restored.state, state)
                    self.assertEqual(restored.to_dict()["state"], state.value)

    def test_invalid_replace_preserves_record_and_audit(self) -> None:
        for directory, state in (
            ("batches", "planned"),
            ("dispatches", "proposed"),
            ("dispatch-status", "approved"),
        ):
            with self.subTest(directory=directory):
                with tempfile.TemporaryDirectory() as temporary:
                    ledger = LifecycleLedger(Path(temporary) / "state")
                    ledger.ensure()
                    root = ledger.records_root()
                    path = root / directory / "test.json"
                    ledger.write_immutable(path, {"state": state, "dispatches": []})
                    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                    with self.assertRaises(LedgerError):
                        ledger.replace(path, {"state": "работает"})
                    self.assertEqual(
                        before,
                        {p: p.read_bytes() for p in root.rglob("*") if p.is_file()},
                    )

    def test_invalid_state_is_refused_before_record_or_audit_write(self) -> None:
        invalid: tuple[JsonValue, ...] = (
            "нет",
            "Passed",
            "planned ",
            "",
            None,
            [],
            True,
        )
        for directory in ("batches", "dispatches", "dispatch-status"):
            for state in invalid:
                with self.subTest(directory=directory, state=state):
                    with tempfile.TemporaryDirectory() as temporary:
                        ledger = LifecycleLedger(Path(temporary) / "state")
                        ledger.ensure()
                        root = ledger.records_root()
                        before = {
                            p: p.read_bytes() for p in root.rglob("*") if p.is_file()
                        }
                        value: JsonObject = {"state": state}
                        with self.assertRaises(LedgerError):
                            ledger.write_immutable(
                                root / directory / "test.json", value
                            )
                        self.assertEqual(
                            before,
                            {p: p.read_bytes() for p in root.rglob("*") if p.is_file()},
                        )

    def test_value_objects_refuse_invalid_state(self) -> None:
        invalid: tuple[JsonValue, ...] = (
            "неизвестно",
            "Working",
            " working",
            None,
            {},
            1,
        )
        for record in (BatchRecord, DispatchRecord, DispatchStatusRecord):
            for state in invalid:
                with self.subTest(record=record.__name__, state=state):
                    with self.assertRaises(LedgerError):
                        record.from_dict({"state": state})

    def test_coordinator_refuses_corrupt_state_on_read(self) -> None:
        for directory, loader, record_id in (
            ("batches", ledger_ops._load_batch, "batch-1"),
            ("dispatches", ledger_ops._load_dispatch, "dispatch-1"),
            ("dispatch-status", ledger_ops._load_dispatch_status, "dispatch-1"),
        ):
            with self.subTest(directory=directory):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary) / "state"
                    ledger = LifecycleLedger(root)
                    ledger.ensure()
                    path = ledger.records_root() / directory / f"{record_id}.json"
                    path.write_text('{"state": "работает"}', encoding="utf-8")
                    with self.assertRaises(CoordinatorError):
                        loader(root, record_id)


class RetryVocabularyBoundaryTests(unittest.TestCase):
    def test_classifier_hints_use_the_same_strict_reason_enum(self) -> None:
        for reason in RetryReason:
            self.assertIs(ReasonHint(reason, "observed evidence").category, reason)
        for value in ("Transport", "transport ", "сеть"):
            with self.subTest(value=value):
                with self.assertRaises(ExtensionError):
                    ReasonHint(
                        category=cast(RetryReason, value), basis="observed evidence"
                    )

    def test_routes_are_parsed_without_aliases(self) -> None:
        for route in RecoveryRoute:
            self.assertIs(history._require_route(route.value), route)
        invalid: tuple[JsonValue, ...] = (
            "Developer-retry",
            "developer-retry ",
            "повтор",
            [],
            None,
            True,
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(CoordinatorError):
                    history._require_route(value)

    def test_retry_categories_are_parsed_before_routing(self) -> None:
        report: JsonObject = {
            "outcome": "blocked",
            "checks_run": [],
            "risks": {"status": "none", "details": ""},
            "blockers": {"status": "none", "details": ""},
        }
        for reason in RetryReason:
            with self.subTest(reason=reason):
                routed = decisions._retry_routing(
                    "code-review",
                    report,
                    dispatch_candidate="a" * 40,
                    current_candidate="a" * 40,
                    explicit_category=reason.value,
                )
                self.assertIsInstance(routed["reason_category"], RetryReason)
                self.assertIsInstance(routed["route"], RecoveryRoute)
        invalid: tuple[JsonValue, ...] = (
            "Transport",
            "transport ",
            "инфраструктура",
            [],
            True,
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(CoordinatorError):
                    decisions._retry_routing(
                        "code-review",
                        report,
                        dispatch_candidate="a" * 40,
                        current_candidate="a" * 40,
                        explicit_category=value,
                    )


if __name__ == "__main__":
    unittest.main()
