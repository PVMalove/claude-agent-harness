"""The ledger lock of an owner in another PID namespace (issue #759).

A worker sandboxed with ``--unshare-pid`` records a pid that means nothing in the coordinator's
namespace, so ``ledger release-lock`` must not judge that owner by its pid. The lock records the
owner's environment, ``ledger lock-status`` shows the lock without taking it, and an operator can
release any lock with ``release-lock --force`` behind an explicit human approval and an audit.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harness.orchestration import coordinator
from harness.orchestration.core import constants
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger import ledger_admin, ledger_ops, lifecycle
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from tests.orchestration.test_coordinator import _ns

STALE = constants.LEDGER_LOCK_STALE_SECONDS
HERE = {"pid_namespace": "pid:[1]", "boot_id": "boot-a"}
OWNER = {"pid": 4242, "host": "here", "acquired_at": "2026-01-01T00:00:00+00:00"}


def _verdict(
    owner: JsonObject | None, held: int, *, alive: bool, environment: JsonObject | None
) -> tuple[bool, str]:
    return ledger_admin._release_verdict(
        {"owner": owner, "held_seconds": held},
        host="here",
        pid_active=lambda _pid: alive,
        stale_after=STALE,
        environment=environment,
    )


class LockOwnerEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.state_dir = Path(self._tmp.name) / "state"
        self.ledger = LifecycleLedger(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @unittest.skipUnless(Path("/proc/self/ns/pid").exists(), "Linux PID namespaces")
    def test_the_lock_records_the_pid_namespace_and_boot_id_of_its_owner(
        self,
    ) -> None:
        with self.ledger.lock():
            owner = cast(JsonObject, self.ledger.lock_state())["owner"]

        self.assertEqual(owner["pid_namespace"], os.readlink("/proc/self/ns/pid"))
        self.assertEqual(
            owner["boot_id"],
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        )

    def test_an_owner_in_the_same_environment_is_judged_by_its_process(self) -> None:
        same = {**OWNER, **HERE}

        self.assertEqual(
            _verdict(same, 10 * STALE, alive=True, environment=HERE),
            (False, "owner-alive"),
        )
        self.assertEqual(
            _verdict(same, 0, alive=False, environment=HERE), (True, "owner-dead")
        )

    def test_an_owner_in_another_namespace_or_boot_is_released_only_once_stale(
        self,
    ) -> None:
        for name, other in (
            ("pid namespace", {**OWNER, **HERE, "pid_namespace": "pid:[2]"}),
            ("boot id", {**OWNER, **HERE, "boot_id": "boot-b"}),
        ):
            with self.subTest(differs=name):
                self.assertEqual(
                    _verdict(other, STALE - 1, alive=True, environment=HERE),
                    (False, "owner-in-other-environment"),
                )
                self.assertEqual(
                    _verdict(other, STALE, alive=True, environment=HERE),
                    (True, "owner-unknown-stale"),
                )

    def test_a_legacy_record_or_an_unknown_environment_keeps_the_pid_check(
        self,
    ) -> None:
        self.assertEqual(
            _verdict(OWNER, 10 * STALE, alive=True, environment=HERE),
            (False, "owner-alive"),
        )
        self.assertEqual(
            _verdict({**OWNER, **HERE}, 10 * STALE, alive=True, environment=None),
            (False, "owner-alive"),
        )


class LedgerLockCommandTests(unittest.TestCase):
    """`ledger lock-status` and `ledger release-lock --force` against the real ledger lock."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        self.lock_dir = self.state_dir / ".coordinator.lock"
        self.ledger = LifecycleLedger(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _args(self, **values: object) -> argparse.Namespace:
        return _ns(repo=str(self.tmp), state_dir=str(self.state_dir), **values)

    def _orphan(self, *, age: int = 0, **owner: object) -> None:
        """A lock left by a sandboxed worker: a live-looking pid in another PID namespace."""
        self.lock_dir.mkdir(parents=True)
        acquired = datetime.fromtimestamp(datetime.now(UTC).timestamp() - age, UTC)
        record = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": acquired.isoformat(),
            "pid_namespace": "pid:[1]",
            "boot_id": "boot-of-another-sandbox",
            **owner,
        }
        (self.lock_dir / "owner.json").write_text(json.dumps(record), encoding="utf-8")

    def _force(self, **overrides: object) -> JsonObject:
        values: dict[str, object] = {
            "force": True,
            "approved_by": "Operator",
            "approved_at": datetime.now(UTC).isoformat(),
            "reason": "worker sandbox was killed during report submit",
        }
        values.update(overrides)
        return coordinator.release_ledger_lock(self._args(**values))

    def test_lock_status_reports_no_lock(self) -> None:
        self.assertEqual(
            coordinator.ledger_lock_status(self._args()), {"locked": False}
        )

    def test_lock_status_reads_a_held_lock_without_taking_it(self) -> None:
        lifecycle.set_lock_operation("dispatch status")
        self.addCleanup(lifecycle.set_lock_operation, None)
        with self.ledger.lock():
            status = coordinator.ledger_lock_status(self._args())

            self.assertTrue(self.lock_dir.is_dir())

        self.assertTrue(status["locked"])
        self.assertEqual(status["verdict"], "owner-alive")
        self.assertFalse(status["release_available"])
        self.assertEqual(status["operation"], "dispatch status")
        self.assertEqual(status["stale_after_seconds"], STALE)
        self.assertEqual(
            status["remaining_seconds"], STALE - cast(int, status["held_seconds"])
        )
        owner = cast(JsonObject, status["owner"])
        self.assertEqual(owner["pid"], os.getpid())

    def test_lock_status_shows_an_orphaned_worker_and_its_timeout(self) -> None:
        self._orphan(age=STALE - 100)

        status = coordinator.ledger_lock_status(self._args())

        self.assertEqual(status["verdict"], "owner-in-other-environment")
        self.assertFalse(status["release_available"])
        self.assertLessEqual(cast(int, status["remaining_seconds"]), 100)
        self.assertGreater(cast(int, status["remaining_seconds"]), 0)

    def test_an_orphaned_worker_lock_is_released_once_stale(self) -> None:
        self._orphan(age=STALE - 100)
        with self.assertRaises(CoordinatorError) as refused:
            coordinator.release_ledger_lock(self._args())

        self.assertIn("owner-in-other-environment", refused.exception.message)
        self.assertIn("--force", refused.exception.remedy)
        self.assertTrue(self.lock_dir.is_dir())

        self.lock_dir.joinpath("owner.json").unlink()
        self.lock_dir.rmdir()
        self._orphan(age=STALE + 1)

        released = coordinator.release_ledger_lock(self._args())

        self.assertEqual(released["reason"], "owner-unknown-stale")
        self.assertFalse(self.lock_dir.exists())

    def test_force_needs_a_human_approval_and_a_reason(self) -> None:
        self._orphan()
        for case, overrides in (
            ("no approved-by", {"approved_by": None}),
            ("no approved-at", {"approved_at": None}),
            ("a policy approver", {"approved_by": "policy:auto"}),
            ("no reason", {"reason": None}),
            ("an empty reason", {"reason": "  "}),
        ):
            with self.subTest(case=case):
                with self.assertRaises(CoordinatorError):
                    self._force(**overrides)
                self.assertTrue(self.lock_dir.is_dir())

    def test_force_releases_a_live_owner_and_audits_the_decision(self) -> None:
        with self.ledger.lock():
            before = cast(JsonObject, self.ledger.lock_state())

            released = self._force()

            self.assertFalse(self.lock_dir.exists())

        self.assertTrue(released["released"])
        self.assertEqual(released["reason"], "forced")
        self.assertEqual(released["verdict"], "owner-alive")
        audits = sorted((self.state_dir / "lock-audit").glob("*.json"))
        self.assertEqual(len(audits), 1)
        audit = json.loads(audits[0].read_text(encoding="utf-8"))
        self.assertEqual(audit["audit_id"], released["audit_id"])
        self.assertEqual(audit["action"], "ledger-lock-force-release")
        details = audit["details"]
        self.assertEqual(details["approved_by"], "Operator")
        self.assertEqual(
            details["reason"], "worker sandbox was killed during report submit"
        )
        self.assertEqual(details["verdict"], "owner-alive")
        self.assertEqual(details["lock"]["owner"], before["owner"])
        self.assertIn("record_sha256", audit)

    def test_ledger_validate_passes_after_a_forced_release_and_guards_its_audit(
        self,
    ) -> None:
        with self.ledger.lock():
            self.ledger.status()
        self._orphan()
        self._force()

        coordinator.validate_ledger(self._args())

        audit = next((self.state_dir / "lock-audit").glob("*.json"))
        record = json.loads(audit.read_text(encoding="utf-8"))
        record["details"]["reason"] = "rewritten later"
        audit.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(CoordinatorError):
            coordinator.validate_ledger(self._args())

    def test_a_busy_ledger_points_at_lock_status_and_the_forced_release(
        self,
    ) -> None:
        with self.ledger.lock():
            with self.assertRaises(ledger_ops.LedgerBusyError) as busy:
                coordinator.ledger_status(self._args())

        self.assertIn("ledger lock-status", busy.exception.remedy)
        self.assertIn("release-lock --force", busy.exception.remedy)
        self.assertNotIn("remove", busy.exception.remedy.lower())
        self.assertNotIn(".coordinator.lock", busy.exception.remedy)

    def test_the_cli_wires_lock_status_and_the_forced_release(self) -> None:
        parse = coordinator.parser().parse_args
        status = parse(["ledger", "lock-status"])
        forced = parse(
            [
                "ledger",
                "release-lock",
                "--force",
                "--approved-by",
                "Operator",
                "--approved-at",
                "2026-10-18T00:00:00+00:00",
                "--reason",
                "orphaned worker",
            ]
        )
        plain = parse(["ledger", "release-lock"])

        self.assertIs(status.handler, coordinator.ledger_lock_status)
        self.assertIs(forced.handler, coordinator.release_ledger_lock)
        self.assertTrue(forced.force)
        self.assertEqual(forced.reason, "orphaned worker")
        self.assertFalse(plain.force)


if __name__ == "__main__":
    unittest.main()
