#!/usr/bin/env python3
"""Integration accounting (issue #532): the record model, placement and Git helper.

Real ledger and real Git (a local bare remote); no mocks.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.orchestration.core import git_utils
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.ledger import (
    IntegrationEvidenceRecord,
    IntegrationRecord,
    LifecycleLedger,
)
from harness.orchestration.workflow import history

SHA_A = "a" * 40
SHA_B = "b" * 40
IDENTITY = {
    "ticket": "#532",
    "branch": "feature/issue-532-integration-accounting",
    "source_batch_id": "batch-0123",
    "candidate_sha": SHA_A,
    "target_sha": SHA_B,
}
EVIDENCE = {
    "integration_record_id": "integration-" + "0" * 32,
    "kind": "ci",
    "candidate_sha": SHA_A,
    "target_sha": SHA_B,
    "result": "passed",
    "reference": "https://ci.example.invalid/run/1",
    "artifact_sha256": None,
}


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)}: {result.stderr}")
    return result.stdout.strip()


class IntegrationRecordModelTests(unittest.TestCase):
    def test_record_id_is_deterministic_for_the_same_identity(self) -> None:
        first = IntegrationRecord.derive_id(IDENTITY)
        second = IntegrationRecord.derive_id(dict(reversed(list(IDENTITY.items()))))
        self.assertEqual(first, second)
        self.assertRegex(first, r"^integration-[0-9a-f]{32}$")

    def test_record_id_changes_when_any_identity_member_changes(self) -> None:
        base = IntegrationRecord.derive_id(IDENTITY)
        for key in IDENTITY:
            with self.subTest(key=key):
                changed = {**IDENTITY, key: IDENTITY[key] + "x"}
                self.assertNotEqual(base, IntegrationRecord.derive_id(changed))

    def test_evidence_id_is_deterministic_and_pair_sensitive(self) -> None:
        first = IntegrationEvidenceRecord.derive_id(EVIDENCE)
        self.assertEqual(first, IntegrationEvidenceRecord.derive_id(dict(EVIDENCE)))
        self.assertRegex(first, r"^evidence-[0-9a-f]{32}$")
        self.assertNotEqual(
            first,
            IntegrationEvidenceRecord.derive_id({**EVIDENCE, "target_sha": SHA_A}),
        )
        self.assertNotEqual(
            first,
            IntegrationEvidenceRecord.derive_id({**EVIDENCE, "result": "failed"}),
        )

    def test_value_objects_are_placed_under_reports_and_round_trip(self) -> None:
        record = IntegrationRecord(
            integration_record_id=IntegrationRecord.derive_id(IDENTITY),
            extra={"identity": dict(IDENTITY), "contract": 1},
        )
        evidence = IntegrationEvidenceRecord(
            evidence_id=IntegrationEvidenceRecord.derive_id(EVIDENCE),
            extra={"kind": "ci"},
        )
        self.assertEqual(record.directory, "reports/integration")
        self.assertEqual(evidence.directory, "reports/integration-evidence")
        self.assertEqual(record.record_id, record.integration_record_id)
        self.assertEqual(evidence.record_id, evidence.evidence_id)
        self.assertEqual(IntegrationRecord.from_dict(record.to_dict()), record)
        self.assertEqual(
            IntegrationEvidenceRecord.from_dict(evidence.to_dict()), evidence
        )

    def test_records_persist_immutably_inside_the_validated_reports_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            ledger = LifecycleLedger(Path(temporary) / "state")
            ledger.ensure()
            record = IntegrationRecord(
                integration_record_id=IntegrationRecord.derive_id(IDENTITY),
                extra={"identity": dict(IDENTITY)},
            )
            evidence = IntegrationEvidenceRecord(
                evidence_id=IntegrationEvidenceRecord.derive_id(EVIDENCE),
                extra={"kind": "ci"},
            )
            ledger.write_record(record)
            ledger.write_record(evidence)
            root = ledger.records_root()
            self.assertEqual(
                json.loads(
                    (
                        root / "reports/integration" / f"{record.record_id}.json"
                    ).read_text(encoding="utf-8")
                ),
                record.to_dict(),
            )
            self.assertTrue(
                (
                    root / "reports/integration-evidence" / f"{evidence.record_id}.json"
                ).is_file()
            )
            with self.assertRaises(Exception):
                ledger.write_record(record)
            # The generation still validates, so no schema bump or migration is required.
            self.assertEqual(ledger.records_root(), root)
            LifecycleLedger(ledger.root)._validate_generation(root)
            audited = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (root / "audit").glob("*.json")
            ]
            paths = {
                item["details"].get("path")
                for item in audited
                if item["action"] == "immutable-record"
            }
            self.assertIn(f"reports/integration/{record.record_id}.json", paths)

    def test_nested_records_survive_clean_and_migrate(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            ledger = LifecycleLedger(Path(temporary) / "state")
            ledger.ensure()
            record = IntegrationRecord(
                integration_record_id=IntegrationRecord.derive_id(IDENTITY),
                extra={"identity": dict(IDENTITY)},
            )
            ledger.write_record(record)
            self.assertEqual(ledger.clean()["cleaned"], 0)
            current = ledger.records_root()
            self.assertTrue(
                (current / "reports/integration" / f"{record.record_id}.json").is_file()
            )
            pointer = json.loads(ledger.pointer_path.read_text(encoding="utf-8"))
            pointer["version"] = 2
            ledger.pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
            self.assertTrue(ledger.migrate()["migrated"])
            migrated = ledger.records_root()
            self.assertNotEqual(migrated, current)
            self.assertTrue(
                (
                    migrated / "reports/integration" / f"{record.record_id}.json"
                ).is_file()
            )


class RemoteBranchTipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(self._tmp.name)
        origin = tmp / "origin.git"
        _git(tmp, "init", "--bare", str(origin))
        self.repo = tmp / "work"
        self.repo.mkdir()
        _git(self.repo, "init")
        _git(self.repo, "config", "user.email", "test@example.invalid")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "seed")
        _git(self.repo, "branch", "-M", "master")
        _git(self.repo, "remote", "add", "origin", str(origin))
        _git(self.repo, "push", "origin", "master")
        self.head = _git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_the_exact_remote_tip_without_touching_local_state(self) -> None:
        before = _git(self.repo, "for-each-ref")
        self.assertEqual(
            git_utils._remote_branch_tip(self.repo, "origin", "master"), self.head
        )
        self.assertEqual(_git(self.repo, "for-each-ref"), before)
        self.assertFalse((self.repo / ".git" / "FETCH_HEAD").exists())

    def test_absent_branch_is_none_and_names_match_exactly(self) -> None:
        _git(self.repo, "push", "origin", "master:refs/heads/team/master")
        self.assertIsNone(git_utils._remote_branch_tip(self.repo, "origin", "nope"))
        _git(self.repo, "push", "origin", "master:refs/heads/team/only")
        self.assertIsNone(git_utils._remote_branch_tip(self.repo, "origin", "only"))
        _git(self.repo, "push", "origin", "master:refs/heads/gone")
        _git(self.repo, "push", "origin", "--delete", "gone")
        self.assertIsNone(git_utils._remote_branch_tip(self.repo, "origin", "gone"))
        self.assertEqual(
            git_utils._remote_branch_tip(self.repo, "origin", "team/master"), self.head
        )

    def test_unreachable_remote_is_a_coordinator_error_with_a_remedy(self) -> None:
        with self.assertRaises(CoordinatorError) as raised:
            git_utils._remote_branch_tip(self.repo, "missing-remote", "master")
        self.assertTrue(raised.exception.remedy)

    def test_option_like_names_are_refused(self) -> None:
        for remote, ref in (("-x", "master"), ("origin", "--all"), ("origin", "")):
            with self.subTest(remote=remote, ref=ref):
                with self.assertRaises(CoordinatorError):
                    git_utils._remote_branch_tip(self.repo, remote, ref)


class BatchesForTicketBranchTests(unittest.TestCase):
    def test_missing_batches_directory_and_no_match_are_refused(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary) / "state"
            ledger = LifecycleLedger(root)
            ledger.ensure()
            with self.assertRaises(CoordinatorError) as raised:
                history._batches_for_ticket_branch(root, "#1", "feature/issue-1-x")
            self.assertRegex(raised.exception.message, re.compile("no orchestration"))


if __name__ == "__main__":
    unittest.main()
