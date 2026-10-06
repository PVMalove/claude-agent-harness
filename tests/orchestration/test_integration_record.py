#!/usr/bin/env python3
"""Integration accounting (issue #532): the record model, placement and Git helper.

Real ledger and real Git (a local bare remote); no mocks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.orchestration import coordinator
from harness.orchestration.core import git_utils
from harness.orchestration.core.utils import CoordinatorError, JsonObject, _canonical
from harness.orchestration.ledger import (
    IntegrationEvidenceRecord,
    IntegrationRecord,
    LifecycleLedger,
    ledger_ops,
)
from harness.orchestration.ledger import JsonObject as LedgerObject
from harness.orchestration.workflow import history, integration
from tests.orchestration import test_coordinator as coordinator_tests

SHA_A = "a" * 40
SHA_B = "b" * 40
IDENTITY: LedgerObject = {
    "ticket": "#532",
    "branch": "feature/issue-532-integration-accounting",
    "source_batch_id": "batch-0123",
    "candidate_sha": SHA_A,
    "target_sha": SHA_B,
}
EVIDENCE: LedgerObject = {
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
                changed = {**IDENTITY, key: f"{IDENTITY[key]}x"}
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


class PublishedBranch:
    """A ticket branch taken through architect, developer, review, QA and an accepted publish.

    Composition, not inheritance: ``CoordinatorRetryRoutingTests`` already drives a real ledger and
    a real local bare remote through every role, and subclassing it would run its whole suite again.
    """

    def __init__(self) -> None:
        self.fixture = coordinator_tests.CoordinatorRetryRoutingTests()
        self.fixture.setUp()
        self.repo = self.fixture.repo
        self.branch = self.fixture.branch
        self.ticket = "#244"

    def close(self) -> None:
        self.fixture.tearDown()

    def args(self, **values: object) -> argparse.Namespace:
        return self.fixture._args(**values)

    def state_root(self) -> Path:
        return ledger_ops._state_root(self.args(), self.repo)

    def records(self) -> Path:
        return self.fixture._records()

    def publish(self, name: str = "x", *, landed: str | None = None) -> JsonObject:
        """Run one batch to a completed publish.  ``landed`` is an earlier published candidate:
        it is merged into the integration ref first, and a second batch is planned for the same
        ticket and branch (the worktree already exists) on top of it."""
        fx = self.fixture
        if landed is None:
            batch = fx._create_batch()
        else:
            _git(self.repo, "push", "origin", f"{landed}:refs/heads/master")
            batch = coordinator.create_batch(self.args(**fx._batch_plan()))
            coordinator.approve_batch(
                self.args(batch=batch["batch_id"], **fx._approval())
            )
            fx.batch_id = batch["batch_id"]
        batch_id = batch["batch_id"]
        fx._accepted_architect(batch_id)
        candidate = fx._accepted_candidate(batch_id, name)
        fx._accepted_review_and_qa(batch_id, candidate)
        brief = fx._dispatch(
            batch_id, "developer", purpose="publish", candidate=candidate
        )["brief"]
        coordinator.publish_dispatch(
            self.args(dispatch=brief["dispatch_id"], remote="origin")
        )
        fx._decide(batch_id, "accept")
        return {
            "batch_id": batch_id,
            "candidate": candidate,
            "publish_dispatch_id": brief["dispatch_id"],
        }

    def prepare(self, **overrides: object) -> JsonObject:
        values: JsonObject = {
            "ticket": self.ticket,
            "branch": self.branch,
            "batch": None,
            "candidate_commit": None,
            "remote": "origin",
        }
        values.update(overrides)
        return coordinator.integration_prepare(self.args(**values))

    def advance_integration_ref(self) -> str:
        """Another change lands on the integration ref in the remote."""
        _git(self.repo, "checkout", "-q", "master")
        (self.repo / "landed.txt").write_text("landed\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "landed")
        _git(self.repo, "push", "origin", "master")
        return _git(self.repo, "rev-parse", "HEAD")

    def snapshot(self) -> JsonObject:
        """Everything the integration operations must leave exactly as it was: the batch, plan,
        dispatch and report records, and the Git state of the checkout, worktree and remote."""
        root = self.records()
        files: JsonObject = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if not path.is_file() or relative.startswith(
                ("audit/", "reports/integration")
            ):
                continue
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        git: JsonObject = {
            "work_head": _git(self.repo, "rev-parse", "HEAD"),
            "work_status": _git(self.repo, "status", "--porcelain"),
            "work_refs": _git(self.repo, "for-each-ref"),
            "tree_head": _git(self.fixture.worktree, "rev-parse", "HEAD"),
            "tree_status": _git(self.fixture.worktree, "status", "--porcelain"),
            "remote_refs": _git(self.repo, "ls-remote", "origin"),
        }
        return {"ledger_files": files, "git": git}

    def audit_paths(self) -> list[str]:
        paths = []
        for path in sorted((self.records() / "audit").glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            details = item["details"]
            if details.get("path"):
                paths.append(f"{item['action']}:{details['path']}")
        return paths

    def record_files(self) -> list[str]:
        directory = self.records() / "reports" / "integration"
        return sorted(path.name for path in directory.glob("*.json"))


class IntegrationPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.addCleanup(self.branch.close)

    def test_prepare_links_ticket_branch_batch_candidate_and_target(self) -> None:
        published = self.branch.publish()
        target = _git(self.branch.repo, "rev-parse", "origin/master")

        result = self.branch.prepare()

        self.assertTrue(result["created"])
        self.assertEqual(result["ticket"], "#244")
        self.assertEqual(result["branch"], self.branch.branch)
        self.assertEqual(result["source_batch_id"], published["batch_id"])
        self.assertEqual(result["candidate_sha"], published["candidate"])
        self.assertEqual(result["published_sha"], published["candidate"])
        self.assertEqual(result["target_sha"], target)
        self.assertEqual(result["integration_ref"], "master")
        self.assertEqual(result["status"]["state"], "current")
        self.assertEqual(result["evidence_links"], [])
        source = result["source_evidence"]
        self.assertEqual(
            source["pair"],
            {"candidate_sha": published["candidate"], "target_sha": target},
        )
        self.assertEqual(source["qa"]["candidate_commit"], published["candidate"])
        self.assertEqual(source["qa"]["outcome"], "completed")
        self.assertTrue(source["qa"]["checks_run"])
        self.assertEqual(
            source["publish"]["dispatch_id"], published["publish_dispatch_id"]
        )
        stored = json.loads(
            (
                self.branch.records()
                / "reports/integration"
                / f"{result['integration_record_id']}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(stored["source"], source)
        self.assertEqual(
            stored["integration_record_id"],
            IntegrationRecord.derive_id(stored["identity"]),
        )
        for reference in (source["publish"], source["qa"]):
            report = json.loads(
                (self.branch.records() / reference["report_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                reference["report_sha256"],
                hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest(),
            )

    def test_the_public_cli_group_reaches_prepare(self) -> None:
        published = self.branch.publish()
        parsed = coordinator.parser().parse_args(
            [
                "--repo",
                str(self.branch.repo),
                "--state-dir",
                str(self.branch.state_root()),
                "integration",
                "prepare",
                "--ticket",
                self.branch.ticket,
                "--branch",
                self.branch.branch,
                "--candidate-commit",
                published["candidate"],
            ]
        )
        self.assertIs(parsed.handler, coordinator.integration_prepare)
        self.assertEqual(parsed.remote, "origin")
        self.assertEqual(
            parsed.handler(parsed)["source_batch_id"], published["batch_id"]
        )

    def test_prepare_is_idempotent_and_never_duplicates_work(self) -> None:
        self.branch.publish()
        first = self.branch.prepare()
        files, audit = self.branch.record_files(), self.branch.audit_paths()

        second = self.branch.prepare()

        self.assertFalse(second["created"])
        self.assertEqual(
            second["integration_record_id"], first["integration_record_id"]
        )
        self.assertEqual(second["source_evidence"], first["source_evidence"])
        self.assertEqual(self.branch.record_files(), files)
        self.assertEqual(self.branch.audit_paths(), audit)

    def test_prepare_never_rewrites_history_or_touches_git(self) -> None:
        self.branch.publish()
        before = self.branch.snapshot()

        self.branch.prepare()
        self.branch.prepare()

        self.assertEqual(self.branch.snapshot(), before)
        written = [
            item for item in self.branch.audit_paths() if "reports/integration/" in item
        ]
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].startswith("immutable-record:"))

    def test_a_repeat_after_the_ref_moved_returns_the_record_and_reports_stale(
        self,
    ) -> None:
        self.branch.publish()
        first = self.branch.prepare()
        moved = self.branch.advance_integration_ref()

        repeat = self.branch.prepare()

        self.assertFalse(repeat["created"])
        self.assertEqual(
            repeat["integration_record_id"], first["integration_record_id"]
        )
        self.assertEqual(repeat["status"]["state"], "stale")
        self.assertEqual(repeat["status"]["integration_tip"], moved)
        self.assertEqual(repeat["target_sha"], first["target_sha"])

    def _refused(self, expected: str, **overrides: object) -> None:
        with self.assertRaises(CoordinatorError) as raised:
            self.branch.prepare(**overrides)
        self.assertTrue(raised.exception.remedy.strip())
        self.assertIn(expected, raised.exception.remedy + raised.exception.message)

    def test_prepare_refuses_empty_ticket_and_branch(self) -> None:
        self.branch.publish()
        for key in ("ticket", "branch"):
            with self.subTest(key=key):
                self._refused("non-empty", **{key: "  "})

    def test_prepare_refuses_a_different_ticket_or_branch(self) -> None:
        self.branch.publish()
        self._refused("existing orchestration batch", ticket="#999")
        self._refused("existing orchestration batch", branch="feature/issue-1-other")
        self._refused("belongs to this --ticket", batch="batch-0123456789abcdef")
        self.assertEqual(self.branch.record_files(), [])

    def test_prepare_refuses_a_candidate_that_is_not_the_published_commit(
        self,
    ) -> None:
        self.branch.publish()
        other = _git(self.branch.repo, "rev-parse", "origin/master")
        self._refused("accepted publish report", candidate_commit=other)
        self.assertEqual(self.branch.record_files(), [])

    def test_prepare_refuses_a_branch_that_was_never_published(self) -> None:
        fixture = self.branch.fixture
        batch = fixture._create_batch()
        fixture._accepted_architect(batch["batch_id"])
        candidate = fixture._accepted_candidate(batch["batch_id"])
        fixture._accepted_review_and_qa(batch["batch_id"], candidate)

        self._refused("integration prepare publishes nothing")
        self.assertEqual(self.branch.record_files(), [])

    def test_prepare_refuses_when_the_remote_branch_is_gone_or_different(self) -> None:
        self.branch.publish()
        _git(self.branch.repo, "push", "origin", "--delete", self.branch.branch)
        self._refused("before the branch is merged or deleted")
        _git(
            self.branch.repo,
            "push",
            "origin",
            f"master:refs/heads/{self.branch.branch}",
        )
        self._refused("re-publish the accepted candidate")
        self.assertEqual(self.branch.record_files(), [])

    def test_prepare_refuses_a_moved_integration_ref_when_no_record_exists(
        self,
    ) -> None:
        self.branch.publish()
        self.branch.advance_integration_ref()
        before = self.branch.snapshot()

        self._refused("new developer rebase dispatch")

        self.assertEqual(self.branch.record_files(), [])
        self.assertEqual(self.branch.snapshot(), before)

    def test_prepare_refuses_an_unconfigured_remote(self) -> None:
        self.branch.publish()
        self._refused("git remote add", remote="elsewhere")

    def test_prepare_detects_a_rewritten_publish_report(self) -> None:
        published = self.branch.publish()
        batch = coordinator._load_batch(self.branch.state_root(), published["batch_id"])
        entry = next(
            item
            for item in batch["dispatches"]
            if item["dispatch_id"] == published["publish_dispatch_id"]
        )
        report = self.branch.records() / entry["report"]
        document = json.loads(report.read_text(encoding="utf-8"))
        document["output"] = "rewritten"
        report.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaises(CoordinatorError):
            self.branch.prepare()
        self.assertEqual(self.branch.record_files(), [])

    def test_missing_accepted_qa_is_refused_with_a_remedy(self) -> None:
        published = self.branch.publish()
        root = self.branch.state_root()
        batch = coordinator._load_batch(root, published["batch_id"])
        proof = integration._accepted_publish(root, batch)
        assert proof is not None
        batch["dispatches"] = [
            item for item in batch["dispatches"] if item.get("role") != "qa"
        ]
        with self.assertRaises(CoordinatorError) as raised:
            integration._source_facts(root, batch, proof, "t" * 40)
        self.assertIn("accept green QA", raised.exception.remedy)

    def test_two_published_batches_need_an_explicit_batch(self) -> None:
        first = self.branch.publish("x")
        second = self.branch.publish("y", landed=first["candidate"])
        self.assertNotEqual(first["batch_id"], second["batch_id"])

        self._refused("--batch")
        chosen = self.branch.prepare(batch=second["batch_id"])
        self.assertEqual(chosen["source_batch_id"], second["batch_id"])
        self.assertEqual(chosen["candidate_sha"], second["candidate"])
        # The first batch's candidate is no longer the remote branch: it is refused, and the
        # second batch is never substituted for it.
        self._refused("not the published candidate", batch=first["batch_id"])
        narrowed = self.branch.prepare(candidate_commit=second["candidate"])
        self.assertEqual(
            narrowed["integration_record_id"], chosen["integration_record_id"]
        )
        self.assertEqual(len(self.branch.record_files()), 1)


class IntegrationEvidenceLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.addCleanup(self.branch.close)
        self.published = self.branch.publish()
        self.prepared = self.branch.prepare()
        self.record_id = self.prepared["integration_record_id"]
        self.pair = {
            "candidate_commit": self.prepared["candidate_sha"],
            "target_commit": "c" * 40,
        }

    def link(self, **overrides: object) -> JsonObject:
        values: JsonObject = {
            "record": self.record_id,
            "kind": "ci",
            "result": "passed",
            "reference": "https://ci.example.invalid/runs/7",
            "artifact_sha256": None,
            **self.pair,
        }
        values.update(overrides)
        return coordinator.integration_link_evidence(self.branch.args(**values))

    def evidence_files(self) -> list[str]:
        directory = self.branch.records() / "reports" / "integration-evidence"
        return sorted(path.name for path in directory.glob("*.json"))

    def test_link_registers_a_pair_check_as_unverified_evidence(self) -> None:
        linked = self.link()

        self.assertTrue(linked["linked"])
        evidence = linked["evidence"]
        self.assertEqual(evidence["integration_record_id"], self.record_id)
        self.assertEqual(evidence["scope"], "pair-check")
        self.assertEqual(evidence["kind"], "ci")
        self.assertEqual(evidence["result"], "passed")
        self.assertEqual(evidence["candidate_sha"], self.pair["candidate_commit"])
        self.assertEqual(evidence["target_sha"], self.pair["target_commit"])
        self.assertEqual(evidence["verification"], "unverified")
        self.assertEqual(self.evidence_files(), [f"{linked['evidence_id']}.json"])
        self.assertEqual(
            self.branch.prepare()["evidence_links"], [linked["evidence_id"]]
        )

    def test_linking_leaves_the_record_and_its_initial_evidence_untouched(
        self,
    ) -> None:
        record = (
            self.branch.records() / "reports/integration" / f"{self.record_id}.json"
        )
        before_record = record.read_bytes()
        before = self.branch.snapshot()

        self.link()
        self.link(kind="resolver", result="failed")

        self.assertEqual(record.read_bytes(), before_record)
        self.assertEqual(self.branch.snapshot(), before)
        stored = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["source"]["pair"]["target_sha"], self.prepared["target_sha"]
        )
        self.assertNotIn("pair_checks", stored)

    def test_the_same_evidence_is_idempotent_and_new_evidence_is_added(self) -> None:
        first = self.link()
        again = self.link()

        self.assertFalse(again["linked"])
        self.assertEqual(again["evidence_id"], first["evidence_id"])
        self.assertEqual(again["evidence"], first["evidence"])
        self.assertEqual(len(self.evidence_files()), 1)
        for change in (
            {"kind": "local-qa"},
            {"result": "failed"},
            {"reference": "https://ci.example.invalid/runs/8"},
            {"target_commit": "d" * 40},
            {"artifact_sha256": "e" * 64},
        ):
            with self.subTest(change=change):
                self.assertTrue(self.link(**change)["linked"])
        self.assertEqual(len(self.evidence_files()), 6)

    def test_every_future_route_links_through_the_same_operation(self) -> None:
        for kind in ("ci", "local-qa", "resolver"):
            with self.subTest(kind=kind):
                self.assertEqual(self.link(kind=kind)["evidence"]["kind"], kind)

    def test_invalid_input_is_refused_with_a_remedy_and_writes_nothing(self) -> None:
        invalid: list[JsonObject] = [
            {"kind": "manual"},
            {"result": "ok"},
            {"candidate_commit": "abc123"},
            {"candidate_commit": "A" * 40},
            {"target_commit": "g" * 40},
            {"artifact_sha256": "abc"},
            {"reference": "   "},
            {"reference": "x" * 2049},
            {"record": "integration-nothex"},
            {"record": "integration-" + "0" * 32},
        ]
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises(CoordinatorError) as raised:
                    self.link(**change)
                self.assertTrue(raised.exception.remedy.strip())
        self.assertEqual(self.evidence_files(), [])

    def test_a_modified_record_is_refused(self) -> None:
        record = (
            self.branch.records() / "reports/integration" / f"{self.record_id}.json"
        )
        document = json.loads(record.read_text(encoding="utf-8"))
        document["source"]["qa"]["outcome"] = "rewritten"
        record.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(CoordinatorError) as raised:
            self.link()
        self.assertIn("integrity", raised.exception.message)
        self.assertEqual(self.evidence_files(), [])

    def test_the_public_cli_group_reaches_link_evidence(self) -> None:
        parsed = coordinator.parser().parse_args(
            [
                "--repo",
                str(self.branch.repo),
                "--state-dir",
                str(self.branch.state_root()),
                "integration",
                "link-evidence",
                "--record",
                self.record_id,
                "--kind",
                "resolver",
                "--candidate-commit",
                self.pair["candidate_commit"],
                "--target-commit",
                self.pair["target_commit"],
                "--result",
                "passed",
                "--reference",
                "resolver-run-1",
                "--artifact-sha256",
                "f" * 64,
            ]
        )
        self.assertIs(parsed.handler, coordinator.integration_link_evidence)
        self.assertTrue(parsed.handler(parsed)["linked"])
        with self.assertRaises(SystemExit):
            coordinator.parser().parse_args(
                [
                    "integration",
                    "link-evidence",
                    "--record",
                    self.record_id,
                    "--kind",
                    "x",
                ]
            )


class IntegrationStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.addCleanup(self.branch.close)
        self.published = self.branch.publish()
        self.prepared = self.branch.prepare()
        self.record_id = self.prepared["integration_record_id"]

    def status(self, **overrides: object) -> JsonObject:
        values: JsonObject = {
            "record": self.record_id,
            "ticket": None,
            "branch": None,
            "batch": None,
        }
        values.update(overrides)
        return coordinator.integration_status(self.branch.args(**values))

    def dispatch_ids(self) -> list[str]:
        batch = coordinator._load_batch(
            self.branch.state_root(), self.published["batch_id"]
        )
        return [item["dispatch_id"] for item in batch["dispatches"]]

    def link(self, **overrides: object) -> JsonObject:
        values: JsonObject = {
            "record": self.record_id,
            "kind": "resolver",
            "result": "passed",
            "reference": "resolver-run-1",
            "artifact_sha256": None,
            "candidate_commit": "1" * 40,
            "target_commit": "2" * 40,
        }
        values.update(overrides)
        return coordinator.integration_link_evidence(self.branch.args(**values))

    def test_status_of_an_unmoved_ref_is_current(self) -> None:
        status = self.status()

        self.assertEqual(status["state"], "current")
        self.assertFalse(status["refresh_required"])
        self.assertEqual(status["integration_tip"], self.prepared["target_sha"])
        self.assertTrue(status["source_evidence"]["applies_to_current_pair"])
        self.assertEqual(status["pair_checks"], [])
        self.assertEqual(
            status["source_evidence"]["pair"], self.prepared["source_evidence"]["pair"]
        )

    def test_status_by_ticket_and_branch_finds_the_record(self) -> None:
        status = self.status(
            record=None, ticket=self.branch.ticket, branch=self.branch.branch
        )
        self.assertEqual(status["integration_record_id"], self.record_id)
        for missing in (
            {"record": None},
            {"record": None, "ticket": self.branch.ticket},
            {"record": None, "ticket": "#1", "branch": self.branch.branch},
        ):
            with self.subTest(missing=missing):
                with self.assertRaises(CoordinatorError) as raised:
                    self.status(**missing)
                self.assertTrue(raised.exception.remedy.strip())

    def test_a_moved_ref_is_stale_and_never_inherits_the_old_qa(self) -> None:
        moved = self.branch.advance_integration_ref()
        before = self.branch.snapshot()
        audit = self.branch.audit_paths()
        dispatches = self.dispatch_ids()

        status = self.status()

        self.assertEqual(status["state"], "stale")
        self.assertTrue(status["refresh_required"])
        self.assertEqual(status["integration_tip"], moved)
        self.assertEqual(status["target_sha"], self.prepared["target_sha"])
        self.assertFalse(status["source_evidence"]["applies_to_current_pair"])
        self.assertNotEqual(
            status["integration_tip"], status["source_evidence"]["pair"]["target_sha"]
        )
        self.assertIn("new check", status["notice"])
        # Observing is not acting: nothing is dispatched, rewritten or even audited.
        self.assertEqual(self.dispatch_ids(), dispatches)
        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(self.branch.audit_paths(), audit)
        self.assertEqual(self.status(), status)

    def test_a_check_of_the_new_pair_applies_only_to_that_pair(self) -> None:
        moved = self.branch.advance_integration_ref()
        old_pair = self.link(
            kind="ci",
            candidate_commit=self.prepared["candidate_sha"],
            target_commit=self.prepared["target_sha"],
            reference="ci-old",
        )
        new_pair = self.link(
            candidate_commit="3" * 40, target_commit=moved, reference="resolver-new"
        )

        checks = {item["evidence_id"]: item for item in self.status()["pair_checks"]}

        self.assertEqual(len(checks), 2)
        self.assertFalse(checks[old_pair["evidence_id"]]["applies_to_current_pair"])
        self.assertTrue(checks[new_pair["evidence_id"]]["applies_to_current_pair"])
        self.assertEqual(checks[new_pair["evidence_id"]]["verification"], "unverified")
        self.assertFalse(self.status()["source_evidence"]["applies_to_current_pair"])

    def test_an_unreachable_remote_is_unavailable_not_current(self) -> None:
        _git(self.branch.repo, "remote", "set-url", "origin", "/nonexistent/origin.git")
        status = self.status()
        self.assertEqual(status["state"], "unavailable")
        self.assertTrue(status["refresh_required"])
        self.assertIsNone(status["integration_tip"])
        self.assertFalse(status["source_evidence"]["applies_to_current_pair"])

    def test_status_detects_rewritten_batch_history(self) -> None:
        batch_path = (
            self.branch.records() / "batches" / f"{self.published['batch_id']}.json"
        )
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        for entry in batch["dispatches"]:
            if (
                entry["dispatch_id"]
                == self.prepared["source_evidence"]["qa"]["dispatch_id"]
            ):
                entry["report_sha256"] = "0" * 64
        batch_path.write_text(json.dumps(batch), encoding="utf-8")
        with self.assertRaises(CoordinatorError):
            self.status()

    def test_status_never_reaches_the_dispatch_machinery(self) -> None:
        source = Path(integration.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "workflow.dispatch",
            "_enforce_base_freshness",
            "create_dispatch",
        ):
            self.assertNotIn(forbidden, source)

    def test_the_public_cli_group_reaches_status(self) -> None:
        parsed = coordinator.parser().parse_args(
            [
                "--repo",
                str(self.branch.repo),
                "--state-dir",
                str(self.branch.state_root()),
                "integration",
                "status",
                "--record",
                self.record_id,
            ]
        )
        self.assertIs(parsed.handler, coordinator.integration_status)
        self.assertEqual(parsed.handler(parsed)["state"], "current")


class IntegrationStatusAfterRefreshTests(unittest.TestCase):
    """After a PR-preparation refresh the new pair needs integration verification, not review."""

    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.addCleanup(self.branch.close)
        self.published = self.branch.publish()
        self.prepared = self.branch.prepare()
        self.record_id = self.prepared["integration_record_id"]
        self.moved = self.branch.advance_integration_ref()
        self.refreshed = coordinator.integration_refresh(
            self.branch.args(
                record=self.record_id, ticket=None, branch=None, batch=None
            )
        )

    def status(self) -> JsonObject:
        return coordinator.integration_status(
            self.branch.args(
                record=self.record_id, ticket=None, branch=None, batch=None
            )
        )

    def link(self, **overrides: object) -> JsonObject:
        values: JsonObject = {
            "record": self.record_id,
            "kind": "ci",
            "result": "passed",
            "reference": "https://ci.example.invalid/runs/9",
            "artifact_sha256": None,
            "candidate_commit": self.refreshed["new_candidate_sha"],
            "target_commit": self.moved,
        }
        values.update(overrides)
        return coordinator.integration_link_evidence(self.branch.args(**values))

    def test_the_refreshed_pair_is_current_but_unverified_and_old_qa_is_historical(
        self,
    ) -> None:
        status = self.status()

        self.assertEqual(status["state"], "current")
        self.assertEqual(status["candidate_sha"], self.refreshed["new_candidate_sha"])
        self.assertEqual(status["original_candidate_sha"], self.published["candidate"])
        self.assertEqual(status["target_sha"], self.moved)
        self.assertFalse(status["source_evidence"]["applies_to_current_pair"])
        self.assertEqual(
            [item["refresh_id"] for item in status["refreshes"]],
            [self.refreshed["refresh_id"]],
        )
        verification = status["verification"]
        self.assertTrue(verification["required"])
        self.assertFalse(verification["satisfied"])
        self.assertFalse(verification["re_review_required"])
        self.assertIn("integration", status["notice"])

    def test_only_a_passed_ci_or_local_qa_check_of_the_new_pair_satisfies_it(
        self,
    ) -> None:
        self.link(kind="resolver")
        self.link(kind="ci", result="failed", reference="ci-failed")
        self.link(
            kind="ci", candidate_commit=self.published["candidate"], reference="old"
        )
        self.assertFalse(self.status()["verification"]["satisfied"])

        # Hand-linked CI or local QA stays unverified evidence (issues #535, #536): only
        # collected CI or generated local QA closes the verification.
        self.link(kind="local-qa", reference="local-qa-run")
        self.link(kind="ci", reference="manual-ci-run")

        status = self.status()
        self.assertFalse(status["verification"]["satisfied"])
        self.assertFalse(status["verification"]["re_review_required"])
        self.assertIn("No re-review", status["notice"])

    def test_an_unrefreshed_record_needs_no_new_verification(self) -> None:
        other = PublishedBranch()
        self.addCleanup(other.close)
        other.publish()
        record = other.prepare()["integration_record_id"]
        status = coordinator.integration_status(
            other.args(record=record, ticket=None, branch=None, batch=None)
        )
        self.assertFalse(status["verification"]["required"])
        self.assertEqual(status["refreshes"], [])


REPO = Path(__file__).resolve().parents[2]


class IntegrationGuidanceTests(unittest.TestCase):
    """The guide, playbook, skills, their Russian descriptions and the glossary stay consistent
    with the public command group."""

    MENTIONS = {
        "harness/docs/backend-orchestration.md": (
            "integration prepare",
            "integration status",
            "integration link-evidence",
            "reports/integration",
            "integration next",
            "verification-failure",
        ),
        "harness/orchestration/playbook.md": (
            "integration prepare",
            "integration status",
            "integration link-evidence",
            "integration next",
        ),
        "harness/orchestration/README.md": (
            "integration prepare",
            "integration status",
        ),
        "skills/first-party/pvmalove/implement/SKILL.md": ("integration prepare",),
        "skills/first-party/pvmalove/to-pull-requests/SKILL.md": (
            "integration status",
            "integration next",
            "integration collect-ci",
            "integration local-qa",
            "candidate_sha",
            "target_sha",
            "qa_source",
        ),
        "docs/skills/implement.md": ("integration prepare",),
        "docs/skills/to-pull-requests.md": (
            "integration status",
            "integration next",
            "integration collect-ci",
            "integration local-qa",
            "qa_source",
        ),
        "docs/adr/0017-pr-continuation-routing.md": (
            "integration next",
            "verification-failure",
            "collector-failed",
        ),
        "docs/skills/coordinator.md": (
            "integration prepare",
            "integration status",
            "integration link-evidence",
            "integration next",
        ),
        "CONTEXT.md": ("**Integration record**", "**Stale integration record**"),
    }

    def test_every_guidance_file_names_its_integration_commands(self) -> None:
        for relative, needles in self.MENTIONS.items():
            text = (REPO / relative).read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(file=relative, needle=needle):
                    self.assertIn(needle, text)

    def test_the_skills_never_dispatch_or_rewrite_on_stale(self) -> None:
        text = (
            REPO / "skills/first-party/pvmalove/to-pull-requests/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("do not create a dispatch", text)

    def test_the_pr_skill_binds_confirmation_to_the_pair_and_never_merges(self) -> None:
        text = (
            REPO / "skills/first-party/pvmalove/to-pull-requests/SKILL.md"
        ).read_text(encoding="utf-8")
        confirmation = text.index("separate confirmation")
        opening = text.index("gh pr create")
        verification = text.index("6a.")
        handoff = text.index("`handoff`", verification)
        self.assertLess(confirmation, opening)
        self.assertLess(opening, verification)
        self.assertLess(verification, handoff)
        self.assertIn("Never merge it", text)
        self.assertIn("merge queue", text)
        # The old QA only permits entering PR preparation; a project without the opt-in keeps /qa-gate.
        self.assertIn("is not QA of the new candidate", text)
        self.assertIn("run `/qa-gate`", text)


if __name__ == "__main__":
    unittest.main()
