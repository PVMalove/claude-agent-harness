#!/usr/bin/env python3
"""Regression tests for coordinator.py's migration to per-record Value Objects and
``LifecycleLedger.lock()`` (issues #195, #203).

These exercise the coordinator's own public functions against a real ``LifecycleLedger`` on a real
temporary git repository -- no mocks -- mirroring ``tests/test_qa_lane.py``'s pattern. They prove
the migration kept batch/dispatch persistence intact, and that ``qa_lane.py``'s bridge into
``coordinator.py`` (the ``ops`` parameter) carries only validation/loading helpers -- never a
raw-path builder or a bare ``Path``+``dict`` write adapter.
"""

from __future__ import annotations

import argparse
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ORCHESTRATION_ROOT = Path(__file__).resolve().parents[1] / "harness" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))

import coordinator  # noqa: E402
from ledger import LifecycleLedger  # noqa: E402


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr or result.stdout}")
    return result.stdout.strip()


def _ns(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


def _init_repo(tmp: Path) -> Path:
    """A minimal git repository with an 'origin' remote, a zero-config `.harness/project.json`, a
    real architect role manifest and the seed files the context builder needs -- everything
    ``create_batch``/``create_dispatch`` touch for real, with no mocked git or filesystem calls."""
    origin = tmp / "origin.git"
    _git(tmp, "init", "--bare", str(origin))

    repo = tmp / "work"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (repo / "README.md").write_text("# Readme\n", encoding="utf-8")
    (repo / ".harness").mkdir()
    (repo / ".harness" / "project.json").write_text(
        json.dumps({"qa_gate_commands": ["true"]}), encoding="utf-8",
    )
    roles_dst = repo / ".harness" / "orchestration" / "roles"
    roles_dst.mkdir(parents=True)
    architect_manifest = (ORCHESTRATION_ROOT / "roles" / "architect.md").read_text(encoding="utf-8")
    (roles_dst / "architect.md").write_text(architect_manifest, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "branch", "-M", "master")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "master")
    return repo


class CoordinatorLedgerMigrationTests(unittest.TestCase):
    """TDD seeds for the #195 migration, run against real git + a real LifecycleLedger."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.state_dir = self.tmp / "state"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _records_root(self) -> Path:
        root = coordinator._state_root(_ns(repo=str(self.repo), state_dir=str(self.state_dir)), self.repo)
        return LifecycleLedger(root).records_root()

    def _create_batch(self, ticket: str = "#195", branch: str = "feature/issue-195-thing") -> dict:
        worktree_path = self.tmp / "worktree"
        if not worktree_path.exists():
            _git(self.repo, "worktree", "add", "-b", branch, str(worktree_path), "master")
        args = _ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            ticket=ticket, branch=branch, worktree=str(worktree_path), zone="repository",
            integration_ref="master", definition_of_done=["do the thing"], prohibited_change=["secrets"],
            required_gate=None, dependency=None,
            expected_file=["services/x.py"], expected_service=["core"], expected_changed_lines=10,
            expected_context_tokens=None,
        )
        return coordinator.create_batch(args)

    def _approve_batch(self, batch_id: str) -> dict:
        return coordinator.approve_batch(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            batch=batch_id, approved_by="Malove", approved_at="2026-09-17T00:00:00+00:00",
        ))

    def _create_architect_dispatch(self, batch_id: str) -> dict:
        return coordinator.create_dispatch(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            batch=batch_id, role="architect", runtime="claude", purpose="work",
            candidate_commit=None, delta_review_of=None, model="sonnet", effort="high",
            approved_by="Malove", approved_at="2026-09-17T00:00:00+00:00",
        ))

    def test_batch_create_writes_immutable_batch_and_plan_records(self) -> None:
        batch = self._create_batch()
        records = self._records_root()

        batch_path = records / "batches" / f"{batch['batch_id']}.json"
        plan_path = records / "plans" / f"{batch['batch_id']}.json"
        self.assertTrue(batch_path.is_file())
        self.assertTrue(plan_path.is_file())
        on_disk = coordinator._read_object(batch_path, "batch")
        self.assertEqual(on_disk["state"], "planned")
        self.assertEqual(on_disk["dispatches"], [])

    def test_batch_approve_transitions_state_via_ledger_replace(self) -> None:
        batch = self._create_batch()

        approved = self._approve_batch(batch["batch_id"])

        self.assertEqual(approved["state"], "awaiting-approval")
        self.assertEqual(approved["coordinator_approval"]["approved_by"], "Malove")
        on_disk = coordinator._read_object(
            self._records_root() / "batches" / f"{batch['batch_id']}.json", "batch",
        )
        self.assertEqual(on_disk["state"], "awaiting-approval")

    def test_dispatch_create_writes_dispatch_and_dispatch_status_records(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])

        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]

        records = self._records_root()
        self.assertTrue((records / "dispatches" / f"{dispatch_id}.json").is_file())
        self.assertTrue((records / "dispatch-status" / f"{dispatch_id}.json").is_file())
        on_disk_dispatch = coordinator._read_object(records / "dispatches" / f"{dispatch_id}.json", "dispatch")
        self.assertEqual(on_disk_dispatch["batch_id"], batch["batch_id"])
        on_disk_status = coordinator._read_object(records / "dispatch-status" / f"{dispatch_id}.json", "status")
        self.assertEqual(on_disk_status["dispatch_id"], dispatch_id)
        self.assertEqual(on_disk_status["state"], "approved")

    def test_dispatch_report_and_decision_round_trip(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]

        coordinator.send_dispatch(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            dispatch=dispatch_id, adapter=None, adapter_arg=None, checkout=None,
        ))
        coordinator.self_report_dispatch(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            dispatch=dispatch_id, model="sonnet", worktree=None,
        ))

        report = {
            "dispatch_id": dispatch_id, "ticket": batch["ticket"], "role": "architect",
            "outcome": "completed", "output": "architecture decision recorded",
            "commit_sha": "not applicable — read-only role", "changed_files": [],
            "checks_run": [{"command": "true", "result": "pass", "evidence": "n/a"}],
            "risks": "none", "blockers": "none", "next_coordinator_action": "dispatch developer",
            "report_language": "ru",
        }
        # A report is staged inside the project, at the absolute path the brief names, so the
        # evidence a human later looks for is in the repository and not in a guessed folder.
        stray_file = self.tmp / "report.json"
        stray_file.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.submit_report(_ns(
                repo=str(self.repo), state_dir=str(self.state_dir), file=str(stray_file),
            ))

        report_file = coordinator._prepare_agent_inbox(self.repo) / f"{dispatch_id}.json"
        report_file.write_text(json.dumps(report), encoding="utf-8")

        submitted = coordinator.submit_report(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir), file=str(report_file),
        ))
        self.assertEqual(submitted["state"], "reported")

        decision = coordinator.decide_batch(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            batch=batch["batch_id"], decision="accept", approved_by="Malove",
            approved_at="2026-09-17T00:01:00+00:00", note=None,
        ))
        self.assertEqual(decision["next_action"], "developer")
        on_disk_status = coordinator._read_object(
            self._records_root() / "dispatch-status" / f"{dispatch_id}.json", "status",
        )
        self.assertEqual(on_disk_status["state"], "reported")

    def test_qa_lane_bridge_surface_has_no_path_builders(self) -> None:
        """``qa_lane.py`` constructs its own ``LifecycleLedger`` and Value Objects directly (issue
        #196); the only things it still reaches into ``coordinator.py`` (via the ``ops`` parameter)
        for are validation/loading helpers, field-set constants and ``_now()`` -- never a raw-path
        builder or a bare ``Path``+``dict`` write adapter. This is the corrected, narrower successor
        to the #195-era bridge-symbols test, which pinned a wider surface (including
        ``_batch_path``/``_dispatch_status_path``/``_replace``) that a later fix (issue #203) proved
        was never actually required to stay that wide."""
        required = (
            "CoordinatorError", "STATE_REL", "_read_object", "QA_QUEUE_FIELDS", "_safe_id",
            "_non_empty", "_now", "QA_LEASE_FIELDS", "_moment", "_repo", "_candidate_commit",
            "_batch_for_ticket_branch", "_accepted_qa_for_candidate", "_load_batch",
            "_validate_batch_integrity", "_validate_dispatch", "_config", "_load_dispatch_status",
            "_validate_report", "_role", "_persist_report", "_load_dispatch", "_approval",
        )
        for name in required:
            self.assertTrue(hasattr(coordinator, name), f"{name} must remain defined for qa_lane.py")
        # ``_write_exclusive``/``_write_text_exclusive`` still exist -- ``_persist_report`` keeps
        # using them for the one write path with no Value Object -- but qa_lane.py no longer reaches
        # them (confirmed above: neither name appears in `required`), and none of the four below
        # (path builders / the path-sniffing bare ``_replace``) survive at all.
        removed = ("_replace", "_ledger_for_path", "_batch_path", "_dispatch_status_path")
        for name in removed:
            self.assertNotIn(
                name, dir(coordinator),
                f"{name} was coordinator.py's own raw-path/bare-dict bridge for qa_lane.py and must "
                "stay deleted now that qa_lane.py builds Value Objects and calls "
                "LifecycleLedger.write_record/replace_record directly",
            )

    def test_persist_report_takes_an_explicit_ledger_instead_of_sniffing_the_path(self) -> None:
        """``_persist_report`` (the one write path with no Value Object -- no ``ReportRecord``
        exists) still uses the bare ``Path``+``dict`` primitives, but takes its ``LifecycleLedger``
        explicitly rather than rediscovering it by walking the filesystem for a ``ledger.json``
        marker (the now-deleted ``_ledger_for_path``)."""
        self.assertEqual(
            list(inspect.signature(coordinator._persist_report).parameters),
            ["ledger", "root", "batch", "dispatch", "report"],
        )


if __name__ == "__main__":
    unittest.main()
