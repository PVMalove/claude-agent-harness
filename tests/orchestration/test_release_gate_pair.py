"""The Release Gate pair record (ADR 0026, section 4): every pair the integration route checks
stores the tree of its candidate, written by ``dispatch publish``, ``integration prepare`` and
``integration refresh`` through one function.

Real ledger and real Git (a local bare remote) through the coordinator CLI functions; no mocks.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from harness.orchestration import coordinator
from harness.orchestration.core.utils import CoordinatorError, JsonObject, _canonical
from harness.orchestration.ledger import LifecycleLedger, ledger_ops
from harness.orchestration.workflow import release_gate
from tests.orchestration.test_integration_record import PublishedBranch, _git

PAIR_KEYS = {"pair_id", "contract", "candidate_sha", "target_sha", "candidate_tree_sha"}


def _pair_id(candidate: str, target: str) -> str:
    members = {"candidate_sha": candidate, "target_sha": target}
    digest = hashlib.sha256(_canonical(members).encode("utf-8")).hexdigest()
    return f"pair-{digest[:32]}"


class ReleaseGatePairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.addCleanup(self.branch.close)

    def pair_path(self, candidate: str, target: str) -> Path:
        return (
            self.branch.records()
            / "reports/integration-pair"
            / f"{_pair_id(candidate, target)}.json"
        )

    def stored_pair(self, candidate: str, target: str) -> JsonObject:
        stored: JsonObject = json.loads(
            self.pair_path(candidate, target).read_text(encoding="utf-8")
        )
        return stored

    def tree(self, commit: str) -> str:
        return _git(self.branch.repo, "rev-parse", f"{commit}^{{tree}}")

    def test_publish_records_the_pair_with_the_tree_of_its_candidate(self) -> None:
        published = self.branch.publish()
        target = _git(self.branch.repo, "rev-parse", "origin/master")

        pair = self.stored_pair(published["candidate"], target)

        self.assertEqual(set(pair), PAIR_KEYS)
        self.assertEqual(pair["pair_id"], _pair_id(published["candidate"], target))
        self.assertEqual(pair["candidate_sha"], published["candidate"])
        self.assertEqual(pair["target_sha"], target)
        self.assertEqual(pair["candidate_tree_sha"], self.tree(published["candidate"]))
        written = [
            item
            for item in self.branch.audit_paths()
            if "reports/integration-pair/" in item
        ]
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].startswith("immutable-record:"))

    def test_prepare_shows_the_candidate_tree_of_its_pair(self) -> None:
        published = self.branch.publish()

        prepared = self.branch.prepare()

        self.assertEqual(
            prepared["candidate_tree_sha"], self.tree(published["candidate"])
        )
        status = coordinator.integration_status(
            self.branch.args(
                record=prepared["integration_record_id"],
                ticket=None,
                branch=None,
                batch=None,
            )
        )
        self.assertEqual(status["candidate_tree_sha"], prepared["candidate_tree_sha"])

    def test_prepare_completes_the_pair_of_a_record_prepared_before_it_existed(
        self,
    ) -> None:
        published = self.branch.publish()
        target = _git(self.branch.repo, "rev-parse", "origin/master")
        path = self.pair_path(published["candidate"], target)
        self.branch.prepare()
        # A record prepared before the pair record existed: only the pair is missing.
        path.unlink()

        repeat = self.branch.prepare()

        self.assertFalse(repeat["created"])
        self.assertEqual(
            self.stored_pair(published["candidate"], target)["candidate_tree_sha"],
            self.tree(published["candidate"]),
        )
        self.assertEqual(
            repeat["candidate_tree_sha"], self.tree(published["candidate"])
        )

    def test_refresh_records_the_pair_of_the_new_candidate(self) -> None:
        self.branch.publish()
        record_id = self.branch.prepare()["integration_record_id"]
        moved = self.branch.advance_integration_ref()

        refreshed = coordinator.integration_refresh(
            self.branch.args(record=record_id, ticket=None, branch=None, batch=None)
        )

        new = refreshed["new_candidate_sha"]
        self.assertEqual(
            self.stored_pair(new, moved)["candidate_tree_sha"], self.tree(new)
        )
        status = coordinator.integration_status(
            self.branch.args(record=record_id, ticket=None, branch=None, batch=None)
        )
        self.assertEqual(status["candidate_tree_sha"], self.tree(new))

    def test_recording_a_pair_again_is_idempotent_and_other_content_is_refused(
        self,
    ) -> None:
        published = self.branch.publish()
        target = _git(self.branch.repo, "rev-parse", "origin/master")
        ledger = LifecycleLedger(self.branch.state_root())
        audit = self.branch.audit_paths()

        with ledger_ops._ledger_lock(ledger):
            again = release_gate.record_pair(
                ledger, self.branch.repo, published["candidate"], target
            )

        self.assertEqual(again, self.stored_pair(published["candidate"], target))
        self.assertEqual(self.branch.audit_paths(), audit)
        path = self.pair_path(published["candidate"], target)
        forged = {**again, "candidate_tree_sha": "0" * 40}
        path.write_text(_canonical(forged), encoding="utf-8")
        with self.assertRaises(CoordinatorError) as raised:
            with ledger_ops._ledger_lock(ledger):
                release_gate.record_pair(
                    ledger, self.branch.repo, published["candidate"], target
                )
        self.assertIn("candidate_tree_sha", raised.exception.message)
        self.assertTrue(raised.exception.remedy.strip())

    def test_an_unknown_candidate_is_refused_and_writes_nothing(self) -> None:
        self.branch.publish()
        target = _git(self.branch.repo, "rev-parse", "origin/master")
        ledger = LifecycleLedger(self.branch.state_root())
        missing = "1" * 40

        with self.assertRaises(CoordinatorError):
            with ledger_ops._ledger_lock(ledger):
                release_gate.record_pair(ledger, self.branch.repo, missing, target)

        self.assertFalse(self.pair_path(missing, target).exists())


if __name__ == "__main__":
    unittest.main()
