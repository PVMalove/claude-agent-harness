"""Strict lifecycle vocabulary at durable and coordinator boundaries."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import cast

from harness.orchestration import contract
from harness.orchestration.core import config as core_config
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
from harness.orchestration.workflow import decisions, history, reports


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


EXACT_SPELLING = "(case-sensitive, no surrounding spaces, no synonyms)"


class ExactCliValueHandlerTests(unittest.TestCase):
    """Flag values the parser cannot check are refused exactly by their handlers, with a remedy."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.roles = self.repo / ".harness" / "orchestration" / "roles"
        shutil.copytree(Path(contract.__file__).parent / "roles", self.roles)
        self.installed = sorted(
            path.stem for path in self.roles.glob("*.md") if path.stem != "_common"
        )

    def test_an_installed_role_name_loads_only_when_exact(self) -> None:
        for name in self.installed:
            self.assertEqual(core_config._role(self.repo, name)["name"], name)
        allowed = ", ".join(self.installed)
        for name, hint in (
            ("Developer", "write exactly 'developer'"),
            (" developer", "write exactly 'developer'"),
            ("code_review", "write exactly 'code-review'"),
            ("developer.md", "write one listed value exactly"),
            ("../roles/developer", "write one listed value exactly"),
            ("_common", "write one listed value exactly"),
            ("разработчик", "write one listed value exactly"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(CoordinatorError) as caught:
                    core_config._role(self.repo, name)
                self.assertEqual(
                    caught.exception.message,
                    f"role must be one of: {allowed} (got {name!r}; "
                    + caught.exception.message.split("; ", 1)[1],
                )
                self.assertIn(hint, caught.exception.message)
                self.assertEqual(
                    caught.exception.remedy,
                    f"set the role to exactly one of: {allowed} {EXACT_SPELLING}",
                )

    def test_an_empty_roles_directory_keeps_the_manifest_load_error(self) -> None:
        for manifest in self.roles.glob("*.md"):
            manifest.unlink()

        with self.assertRaises(CoordinatorError) as caught:
            core_config._role(self.repo, "developer")

        self.assertEqual(
            caught.exception.message, "role manifest 'developer.md' cannot be read"
        )

    def test_a_session_effort_is_exact_and_recorded_unchanged(self) -> None:
        resolved = core_config._resolve_assignment(
            self.repo,
            {},
            "developer",
            "",
            session_model="opus",
            session_effort="high",
        )
        self.assertEqual(resolved[4], "high")
        for effort in (" high", "high ", "High", "HIGH", "extra-high", "высокий"):
            with self.subTest(effort=effort):
                with self.assertRaises(CoordinatorError) as caught:
                    core_config._resolve_assignment(
                        self.repo,
                        {},
                        "developer",
                        "",
                        session_model="opus",
                        session_effort=effort,
                    )
                self.assertTrue(
                    caught.exception.message.startswith(
                        "--effort must be one of: high, low, max, medium, minimal, none, "
                        f"ultra, xhigh (got {effort!r}; "
                    )
                )
                self.assertEqual(
                    caught.exception.remedy,
                    "set --effort to exactly one of: high, low, max, medium, minimal, "
                    f"none, ultra, xhigh {EXACT_SPELLING}; report complete and an "
                    "infrastructure retry reuse the previous brief's resolved_effort, so "
                    "when that recorded value is not exact, create the next dispatch with "
                    "dispatch create --effort and an exact value",
                )

    def test_a_recorded_session_effort_replays_as_it_was_written(self) -> None:
        resolved = core_config._resolve_assignment(
            self.repo,
            {},
            "developer",
            "",
            session_model="opus",
            session_effort="turbo",
            replay=True,
        )

        self.assertEqual(resolved[4], "turbo")

    def test_a_termination_reason_variant_is_refused_before_the_ledger(self) -> None:
        missing = self.repo / "missing"
        for reason, exact in (
            ("RATE_LIMIT", "'rate-limit' or 'rate_limit'"),
            ("Rate-Limit", "'rate-limit' or 'rate_limit'"),
            ("rate limit", "'rate-limit' or 'rate_limit'"),
            (" 429", "'429'"),
            ("429 ", "'429'"),
        ):
            with self.subTest(reason=reason):
                args = argparse.Namespace(
                    repo=str(missing),
                    state_dir=str(missing / "state"),
                    dispatch="dispatch-1",
                    termination_reason=reason,
                    trigger=None,
                )
                with self.assertRaises(CoordinatorError) as caught:
                    reports.resume_dispatch(args)
                self.assertEqual(
                    caught.exception.message,
                    f"--termination-reason {reason!r} is not a recognized rate-limit "
                    f"reason; write exactly {exact}",
                )
                self.assertEqual(
                    caught.exception.remedy,
                    "pass --termination-reason as exactly one of: 429, rate-limit, "
                    f"rate_limit {EXACT_SPELLING}, or pass --trigger for a planned "
                    "continuation",
                )
                self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
