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
import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

from harness.orchestration import (
    contract,
    coordinator,
    coordinator_cli,
    extensions,
    operation_access,
    operational_guards,
    qa_lane,
)
from harness.orchestration.core import config, constants, git_utils, utils, workspace
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger import (
    BatchRecord,
    DispatchStatusRecord,
    LifecycleLedger,
    PlanRecord,
    ledger_admin,
    ledger_ops,
)
from harness.orchestration.workflow import (
    approval,
    carried_items,
    commit_plan,
    decisions,
    delta_review,
    dispatch,
    history,
    reports,
    supersede,
)
from harness.orchestration.workflow import batch as batch_module

ORCHESTRATION_ROOT = Path(__file__).resolve().parents[2] / "harness" / "orchestration"
# The structured evidence of a tool that blocked a legitimate role action (issue #500).
TOOLING_BLOCKER = {
    "tool": "PreToolUse:Bash hook block-scratch-outside-docs-tasks.sh",
    "command": "python -m pytest -q tests/orchestration/test_coordinator.py",
    "message": "Blocked: writes outside docs/tasks are not allowed",
}
# One brief item a read-only role left undone (issue #501).
INCOMPLETE_ITEM = {
    "brief_item": "Map the rollback path into the commit plan",
    "reason": "The rollback module was out of the Context Package",
    "target_role": "developer",
}


class ImmutableReportPersistenceTests(unittest.TestCase):
    def test_markdown_failure_discards_the_incomplete_json_report(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary) / "state"
            ledger = LifecycleLedger(root)
            ledger.ensure()
            dispatch: JsonObject = {
                "dispatch_id": "dispatch-0123456789abcdef",
                "batch_id": "batch-0123456789abcdef",
            }
            batch: JsonObject = {
                "batch_id": "batch-0123456789abcdef",
                "dispatches": [
                    {"dispatch_id": "dispatch-0123456789abcdef", "state": "dispatched"}
                ],
            }
            report: JsonObject = {
                "dispatch_id": "dispatch-0123456789abcdef",
                "ticket": "291",
                "role": "qa",
                "outcome": "completed",
                "output": "green",
                "commit_sha": "not applicable",
                "changed_files": [],
                "checks_run": [],
                "risks": "none",
                "blockers": "none",
                "next_coordinator_action": "accept",
            }
            failure = coordinator.CoordinatorError("disk full", remedy="free space")
            with (
                mock.patch.object(
                    reports,
                    "_write_text_exclusive",
                    side_effect=failure,
                ),
                self.assertRaises(coordinator.CoordinatorError),
            ):
                reports._persist_report(ledger, root, batch, dispatch, report)

            report_path = (
                ledger.records_root() / "reports" / "dispatch-0123456789abcdef.json"
            )
            self.assertFalse(report_path.exists())


class GitUtilsValidationTests(unittest.TestCase):
    def test_fetch_ref_tip_rejects_invalid_refs(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary)
            for invalid in ("-v", "--help", "", 123):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(coordinator.CoordinatorError) as raised:
                        git_utils._fetch_ref_tip(repo, cast(str, invalid))
                    self.assertIn(
                        "ref must be a non-empty string not starting with '-'",
                        raised.exception.message,
                    )


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(arguments)} failed: {result.stderr or result.stdout}"
        )
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
        json.dumps({"qa_gate_commands": ["true"]}),
        encoding="utf-8",
    )
    roles_dst = repo / ".harness" / "orchestration" / "roles"
    roles_dst.mkdir(parents=True)
    architect_manifest = (ORCHESTRATION_ROOT / "roles" / "architect.md").read_text(
        encoding="utf-8"
    )
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
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.state_dir = self.tmp / "state"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _records_root(self) -> Path:
        root = ledger_ops._state_root(
            _ns(repo=str(self.repo), state_dir=str(self.state_dir)), self.repo
        )
        return LifecycleLedger(root).records_root()

    def _create_batch(
        self,
        ticket: str = "#195",
        branch: str = "feature/issue-195-thing",
        goal: str | None = None,
    ) -> JsonObject:
        worktree_path = self.tmp / "worktree"
        if not worktree_path.exists():
            _git(
                self.repo, "worktree", "add", "-b", branch, str(worktree_path), "master"
            )
        args = _ns(
            repo=str(self.repo),
            state_dir=str(self.state_dir),
            ticket=ticket,
            goal=goal,
            branch=branch,
            worktree=str(worktree_path),
            allowed_path=["**"],
            integration_ref="master",
            definition_of_done=["do the thing"],
            prohibited_change=["secrets"],
            required_gate=None,
            dependency=None,
            expected_file=["services/x.py"],
            expected_service=["core"],
            expected_changed_lines=10,
            expected_context_tokens=None,
        )
        return coordinator.create_batch(args)

    def _approve_batch(self, batch_id: str) -> JsonObject:
        return coordinator.approve_batch(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                batch=batch_id,
                approved_by="Malove",
                approved_at="2026-09-17T00:00:00+00:00",
            )
        )

    def _create_architect_dispatch(self, batch_id: str) -> JsonObject:
        fields = {
            "repo": str(self.repo),
            "state_dir": str(self.state_dir),
            "batch": batch_id,
            "role": "architect",
            "runtime": "claude",
            "purpose": "work",
            "candidate_commit": None,
            "delta_review_of": None,
            "model": "sonnet",
            "effort": "high",
        }
        proposal = coordinator.create_dispatch(_ns(propose=True, **fields))
        return coordinator.create_dispatch(
            _ns(
                transition_digest=proposal["transition_digest"],
                approved_by="Malove",
                approved_at="2026-09-17T00:00:00+00:00",
                **fields,
            )
        )

    def _telemetry_payload(
        self,
        dispatch_id: str,
        *,
        max_context_tokens: int | None = None,
        recorded_at: str = "2026-09-18T00:00:00+00:00",
        session_kind: str = "worker",
    ) -> JsonObject:
        return {
            "dispatch_id": dispatch_id,
            "session_kind": session_kind,
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "max_context_tokens": max_context_tokens,
            "tool_calls": 1,
            "tool_output_bytes": 10,
            "poll_turns": 1,
            "restart_reason": "none",
            "recorded_at": recorded_at,
        }

    def _record_telemetry(self, payload: JsonObject) -> JsonObject:
        path = self.tmp / f"telemetry-{uuid.uuid4()}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return coordinator.record_telemetry(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                file=str(path),
            )
        )

    def test_batch_create_writes_immutable_batch_and_plan_records(self) -> None:
        batch = self._create_batch(goal="Freeze project memory")
        records = self._records_root()

        batch_path = records / "batches" / f"{batch['batch_id']}.json"
        plan_path = records / "plans" / f"{batch['batch_id']}.json"
        self.assertTrue(batch_path.is_file())
        self.assertTrue(plan_path.is_file())
        on_disk = coordinator._read_object(batch_path, "batch")
        self.assertEqual(on_disk["state"], "planned")
        self.assertEqual(on_disk["dispatches"], [])
        self.assertEqual(on_disk["goal"], "Freeze project memory")
        plan = coordinator._read_object(plan_path, "plan")
        self.assertEqual(plan["goal"], "Freeze project memory")
        on_disk["goal"] = "Changed goal"
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "goal does not match"
        ):
            coordinator._validate_batch_integrity(self.state_dir, on_disk)

    def test_dispatch_command_resolves_its_batch_pinned_runtime(self) -> None:
        (self.repo / ".harness" / "orchestration" / "coordinator.py").write_text(
            "# pinned entry point\n", encoding="utf-8"
        )
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch_id = self._create_architect_dispatch(batch["batch_id"])["dispatch_id"]
        args = _ns(
            repo=str(self.repo), state_dir=str(self.state_dir), dispatch=dispatch_id
        )
        self.assertIsNone(coordinator.pinned_runtime_command(args))

        (
            self.repo / ".harness" / "orchestration" / "roles" / "architect.md"
        ).write_text("reinstalled\n", encoding="utf-8")

        command = coordinator.pinned_runtime_command(args)
        assert command is not None
        self.assertEqual(
            Path(command[3])
            .relative_to(self.state_dir / workspace.RUNTIMES_DIR)
            .parts[1:],
            ("harness", "orchestration", "coordinator.py"),
        )

    def test_file_command_resolves_its_payload_dispatch_pinned_runtime(self) -> None:
        """report submit, dispatch checkpoint and dispatch telemetry name their dispatch only
        inside --file, so the batch is resolved from that payload (#377)."""
        (self.repo / ".harness" / "orchestration" / "coordinator.py").write_text(
            "# pinned entry point\n", encoding="utf-8"
        )
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch_id = self._create_architect_dispatch(batch["batch_id"])["dispatch_id"]
        payload = workspace._prepare_agent_inbox(self.repo) / f"{dispatch_id}.json"
        payload.write_text(json.dumps({"dispatch_id": dispatch_id}), encoding="utf-8")
        args = _ns(
            repo=str(self.repo), state_dir=str(self.state_dir), file=str(payload)
        )
        (
            self.repo / ".harness" / "orchestration" / "roles" / "architect.md"
        ).write_text("reinstalled\n", encoding="utf-8")

        self.assertIsNotNone(coordinator.pinned_runtime_command(args))
        # A payload naming no readable dispatch is the command's own error to report.
        for unreadable in (
            "not json",
            "[]",
            "{}",
            '{"dispatch_id": "dispatch-missing"}',
        ):
            with self.subTest(payload=unreadable):
                payload.write_text(unreadable, encoding="utf-8")
                self.assertIsNone(coordinator.pinned_runtime_command(args))

    def test_schema_upgrade_waits_for_batches_pinned_to_another_runtime(self) -> None:
        batch = self._create_batch()
        args = _ns(repo=str(self.repo), state_dir=str(self.state_dir))
        architect = self.repo / ".harness" / "orchestration" / "roles" / "architect.md"
        original = architect.read_text(encoding="utf-8")
        architect.write_text("reinstalled\n", encoding="utf-8")

        with mock.patch("harness.orchestration.ledger.lifecycle.LEDGER_VERSION", 99):
            with self.assertRaises(coordinator.CoordinatorError) as refused:
                coordinator.migrate_ledger(args)
            self.assertIn(batch["batch_id"], refused.exception.message)

            architect.write_text(original, encoding="utf-8")
            self.assertTrue(coordinator.migrate_ledger(args)["migrated"])

    def test_batch_approve_transitions_state_via_ledger_replace(self) -> None:
        batch = self._create_batch()

        approved = self._approve_batch(batch["batch_id"])

        self.assertEqual(approved["state"], "awaiting-approval")
        self.assertEqual(approved["coordinator_approval"]["approved_by"], "Malove")
        on_disk = coordinator._read_object(
            self._records_root() / "batches" / f"{batch['batch_id']}.json",
            "batch",
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
        on_disk_dispatch = coordinator._read_object(
            records / "dispatches" / f"{dispatch_id}.json", "dispatch"
        )
        self.assertEqual(on_disk_dispatch["batch_id"], batch["batch_id"])
        on_disk_status = coordinator._read_object(
            records / "dispatch-status" / f"{dispatch_id}.json", "status"
        )
        self.assertEqual(on_disk_status["dispatch_id"], dispatch_id)
        self.assertEqual(on_disk_status["state"], "approved")
        self.assertEqual(
            on_disk_dispatch["liveness"],
            {"heartbeat_every_seconds": 300, "stale_after_seconds": 3600},
        )

    def test_clean_base_context_package_starts_with_expected_scope_file(self) -> None:
        task_file = self.repo / "services" / "x.py"
        task_file.parent.mkdir()
        task_file.write_text("def target() -> None:\n    pass\n", encoding="utf-8")
        _git(self.repo, "add", "services/x.py")
        _git(self.repo, "commit", "-m", "Add target file")
        _git(self.repo, "push", "origin", "master")
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])

        dispatch = self._create_architect_dispatch(batch["batch_id"])

        package_id = dispatch["brief"]["context_package_id"]
        package = coordinator._read_object(
            self._records_root() / "context-packages" / f"{package_id}.json",
            "context package",
        )
        paths = [item["path"] for item in package["starting_files"]]
        self.assertIn("services/x.py", paths)

    def test_dispatch_send_returns_the_frozen_heartbeat_cadence(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])

        sent = coordinator.send_dispatch(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                dispatch=dispatch["dispatch_id"],
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )

        self.assertEqual(
            sent["heartbeat"],
            {
                "every_seconds": 300,
                "stale_after_seconds": 3600,
                "instruction": (
                    "after self-report, send dispatch heartbeat now and at least once per "
                    "300 seconds while working"
                ),
            },
        )

    def test_dispatch_report_and_decision_round_trip(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]

        coordinator.send_dispatch(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                dispatch=dispatch_id,
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )
        coordinator.self_report_dispatch(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                dispatch=dispatch_id,
                model="sonnet",
                worktree=None,
            )
        )

        report = {
            "dispatch_id": dispatch_id,
            "ticket": batch["ticket"],
            "role": "architect",
            "outcome": "completed",
            "output": "architecture decision recorded",
            "commit_sha": "not applicable — read-only role",
            "changed_files": [],
            # The architect owns no verification gate, so its brief approves no commands.
            "checks_run": [],
            "risks": "none",
            "blockers": "none",
            "next_coordinator_action": "dispatch developer",
            "report_language": "ru",
        }
        # A report is staged inside the project, at the absolute path the brief names, so the
        # evidence a human later looks for is in the repository and not in a guessed folder.
        stray_file = self.tmp / "report.json"
        stray_file.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.submit_report(
                _ns(
                    repo=str(self.repo),
                    state_dir=str(self.state_dir),
                    file=str(stray_file),
                )
            )

        report_file = workspace._prepare_agent_inbox(self.repo) / f"{dispatch_id}.json"
        report_file.write_text(json.dumps(report), encoding="utf-8")

        submitted = coordinator.submit_report(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                file=str(report_file),
            )
        )
        self.assertEqual(submitted["state"], "reported")

        decision = coordinator.decide_batch(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                batch=batch["batch_id"],
                decision="accept",
                approved_by="Malove",
                approved_at="2026-09-17T00:01:00+00:00",
                note=None,
            )
        )
        self.assertEqual(decision["next_action"], "developer")
        on_disk_status = coordinator._read_object(
            self._records_root() / "dispatch-status" / f"{dispatch_id}.json",
            "status",
        )
        self.assertEqual(on_disk_status["state"], "reported")

    def test_mark_batch_not_required_is_terminal_and_recommends_wontfix(self) -> None:
        batch = self._create_batch(ticket="#238")
        self._approve_batch(batch["batch_id"])

        resolved = coordinator.mark_batch_not_required(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                batch=batch["batch_id"],
                approved_by="Malove",
                approved_at="2026-09-17T00:01:00+00:00",
                reason="The pinned snapshot already satisfies every definition-of-done item.",
            )
        )

        self.assertEqual(resolved["state"], "not-required")
        self.assertEqual(resolved["tracker_resolution"], "resolution::wontfix")
        listed = coordinator.list_batches(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                ticket="#238",
                state=None,
                open=True,
            )
        )
        self.assertEqual(listed["batches"], [])

    def test_ordinary_write_report_still_rejects_an_empty_diff(self) -> None:
        report = {
            "dispatch_id": "dispatch-123",
            "ticket": "#238",
            "role": "developer",
            "outcome": "completed",
            "output": "no change",
            "commit_sha": "not applicable",
            "changed_files": [],
            "checks_run": [{"command": "true", "result": "pass", "evidence": "passed"}],
            "risks": "none",
            "blockers": "none",
            "next_coordinator_action": "accept",
            "report_language": "ru",
        }
        dispatch = {
            "dispatch_id": "dispatch-123",
            "ticket": "#238",
            "role": "developer",
            "verification_commands": ["true"],
            "write_paths": ["**"],
        }

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            coordinator._validate_report(report, dispatch, {"mode": "write"})

        self.assertEqual(
            caught.exception.message,
            "write-role completion reports require commit_sha and changed_files",
        )

    @staticmethod
    def _scoped_write_report(changed: list[str]) -> tuple[JsonObject, JsonObject]:
        report: JsonObject = {
            "dispatch_id": "dispatch-123",
            "ticket": "#633",
            "role": "developer",
            "outcome": "completed",
            "output": "done",
            "commit_sha": "a" * 40,
            "changed_files": changed,
            "checks_run": [{"command": "true", "result": "pass", "evidence": "passed"}],
            "risks": "none",
            "blockers": "none",
            "next_coordinator_action": "accept",
            "report_language": "ru",
        }
        dispatch: JsonObject = {
            "dispatch_id": "dispatch-123",
            "ticket": "#633",
            "role": "developer",
            "verification_commands": ["true"],
            "write_paths": ["services/a.py", "docs/**"],
        }
        return report, dispatch

    def test_only_a_developer_report_may_change_files_outside_write_paths(
        self,
    ) -> None:
        report, dispatch = self._scoped_write_report(["services/b.py"])

        coordinator._validate_report(
            report, dispatch, {"mode": "write", "name": "developer"}
        )
        self.assertEqual(
            reports.report_scope_warnings(report, dispatch), ["services/b.py"]
        )
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "must remain inside the approved scope"
        ):
            coordinator._validate_report(
                report, dispatch, {"mode": "write", "name": "conflict-resolver"}
            )

    def test_scope_matching_follows_the_glob_semantics_of_write_paths(self) -> None:
        report, dispatch = self._scoped_write_report(
            ["services/a.py", "docs/guide/intro.md"]
        )
        dispatch["write_paths"] = ["services/*.py", "docs/**"]

        self.assertEqual(reports.report_scope_warnings(report, dispatch), [])
        self.assertEqual(
            commit_plan.paths_outside_scope(
                [{"expected_paths": ["services/a.py", "tools/x.py", "tools/x.py"]}],
                ["services/*.py"],
            ),
            ["tools/x.py"],
        )
        self.assertEqual(
            commit_plan.paths_outside_scope([{"expected_paths": ["tools/x.py"]}], None),
            [],
        )

    def test_absolute_changed_files_are_refused_even_for_a_developer(self) -> None:
        report, dispatch = self._scoped_write_report(["/etc/passwd"])

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "must remain inside the repository"
        ):
            coordinator._validate_report(
                report, dispatch, {"mode": "write", "name": "developer"}
            )

    def test_checkpoint_changed_files_outside_write_paths_are_still_refused(
        self,
    ) -> None:
        _, dispatch = self._scoped_write_report([])
        checkpoint: JsonObject = {
            "dispatch_id": "dispatch-123",
            "commit_sha": "a" * 40,
            "changed_files": ["services/b.py"],
            "remaining_definition_of_done": "none",
            "passing_checks": "none",
            "risks": "none",
            "blockers": "none",
            "context_package_id": "pkg",
        }

        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            "checkpoint changed_files must remain inside the approved scope",
        ):
            reports._validate_checkpoint(
                checkpoint,
                dispatch,
                {"mode": "write", "name": "developer"},
                self.repo,
                None,
                self.state_dir,
                {},
            )

    def test_decision_packet_cli_accepts_a_commit_plan_file(self) -> None:
        parsed = coordinator.parser().parse_args(
            [
                "batch",
                "decision-packet",
                "--batch",
                "b1",
                "--commit-plan-file",
                "plan.json",
            ]
        )

        self.assertIs(parsed.handler, coordinator.decision_packet)
        self.assertEqual(parsed.commit_plan_file, "plan.json")

    def test_commit_plan_is_not_checked_against_git_without_a_repository(self) -> None:
        # Without a repository there is no history to map commits against: validation must not
        # reach the Git-backed commit_map check (it used to fail on an unbound commit).
        report = {
            "dispatch_id": "dispatch-123",
            "ticket": "#238",
            "role": "developer",
            "outcome": "completed",
            "output": "done",
            "commit_sha": "a" * 40,
            "changed_files": ["src/app.py"],
            "checks_run": [{"command": "true", "result": "pass", "evidence": "passed"}],
            "risks": "none",
            "blockers": "none",
            "next_coordinator_action": "accept",
            "report_language": "ru",
            "commit_map": [{"commit_sha": "a" * 40, "plan_entry_id": "step-1"}],
        }
        dispatch = {
            "dispatch_id": "dispatch-123",
            "ticket": "#238",
            "role": "developer",
            "verification_commands": ["true"],
            "write_paths": ["**"],
            "commit_plan": [{"id": "step-1"}],
        }

        coordinator._validate_report(
            report, dispatch, {"mode": "write", "name": "developer"}
        )

    def test_qa_lane_bridge_surface_has_no_path_builders(self) -> None:
        """``qa_lane.py`` constructs its own ``LifecycleLedger`` and Value Objects directly (issue
        #196); the only things it still reaches into ``coordinator.py`` (via the ``ops`` parameter)
        for are validation/loading helpers, field-set constants and ``_now()`` -- never a raw-path
        builder or a bare ``Path``+``dict`` write adapter. This is the corrected, narrower successor
        to the #195-era bridge-symbols test, which pinned a wider surface (including
        ``_batch_path``/``_dispatch_status_path``/``_replace``) that a later fix (issue #203) proved
        was never actually required to stay that wide."""
        required = (
            "CoordinatorError",
            "STATE_REL",
            "_read_object",
            "QA_QUEUE_FIELDS",
            "_safe_id",
            "_non_empty",
            "_now",
            "QA_LEASE_FIELDS",
            "_moment",
            "_repo",
            "_candidate_commit",
            "_batch_for_ticket_branch",
            "_accepted_qa_for_candidate",
            "_load_batch",
            "_validate_batch_integrity",
            "_validate_dispatch",
            "_config",
            "_load_dispatch_status",
            "_validate_report",
            "_role",
            "_persist_report",
            "_load_dispatch",
            "_approval",
        )
        for name in required:
            self.assertTrue(
                hasattr(coordinator, name), f"{name} must remain defined for qa_lane.py"
            )
        # ``_write_exclusive``/``_write_text_exclusive`` still exist -- ``_persist_report`` keeps
        # using them for the one write path with no Value Object -- but qa_lane.py no longer reaches
        # them (confirmed above: neither name appears in `required`), and none of the four below
        # (path builders / the path-sniffing bare ``_replace``) survive at all.
        removed = (
            "_replace",
            "_ledger_for_path",
            "_batch_path",
            "_dispatch_status_path",
        )
        for name in removed:
            self.assertNotIn(
                name,
                dir(coordinator),
                f"{name} was coordinator.py's own raw-path/bare-dict bridge for qa_lane.py and must "
                "stay deleted now that qa_lane.py builds Value Objects and calls "
                "LifecycleLedger.write_record/replace_record directly",
            )

    def test_adaptive_continuation_policy_resolves_context_warn_ratio(self) -> None:
        self.assertEqual(
            config._adaptive_continuation_policy({})["context_warn_ratio"], 0.8
        )
        self.assertEqual(
            config._adaptive_continuation_policy(
                {"adaptive_continuation_policy": {"context_warn_ratio": 0.5}}
            )["context_warn_ratio"],
            0.5,
        )
        self.assertEqual(
            config._adaptive_continuation_policy(
                {"adaptive_continuation_policy": {"context_warn_ratio": 1}}
            )["context_warn_ratio"],
            1,
        )
        for invalid in (0, 1.5, "0.5", True, -0.1):
            resolved = config._adaptive_continuation_policy(
                {"adaptive_continuation_policy": {"context_warn_ratio": invalid}}
            )
            self.assertEqual(
                resolved["context_warn_ratio"],
                0.8,
                f"{invalid!r} should fall back to default",
            )

    def test_record_telemetry_returns_context_advisory_levels(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]

        ok = self._record_telemetry(
            self._telemetry_payload(
                dispatch_id,
                max_context_tokens=50_000,
                recorded_at="2026-09-18T00:00:00+00:00",
            )
        )
        self.assertEqual(
            ok["context_advisory"],
            {"level": "ok", "limit": 150_000, "warn_at": 120_000, "observed": 50_000},
        )

        warn = self._record_telemetry(
            self._telemetry_payload(
                dispatch_id,
                max_context_tokens=130_000,
                recorded_at="2026-09-18T00:01:00+00:00",
            )
        )
        self.assertEqual(
            warn["context_advisory"],
            {
                "level": "warn",
                "limit": 150_000,
                "warn_at": 120_000,
                "observed": 130_000,
            },
        )

        over = self._record_telemetry(
            self._telemetry_payload(
                dispatch_id,
                max_context_tokens=150_000,
                recorded_at="2026-09-18T00:02:00+00:00",
            )
        )
        self.assertEqual(over["context_advisory"]["level"], "over")

        null_observed = self._record_telemetry(
            self._telemetry_payload(
                dispatch_id,
                max_context_tokens=None,
                recorded_at="2026-09-18T00:03:00+00:00",
            )
        )
        self.assertEqual(
            null_observed["context_advisory"],
            {"level": "ok", "limit": 150_000, "warn_at": 120_000, "observed": None},
        )

    def test_record_telemetry_context_advisory_is_not_persisted(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]

        self._record_telemetry(
            self._telemetry_payload(dispatch_id, max_context_tokens=130_000)
        )

        on_disk_batch = coordinator._read_object(
            self._records_root() / "batches" / f"{batch['batch_id']}.json",
            "batch",
        )
        telemetry_records = on_disk_batch.get("telemetry", [])
        self.assertEqual(len(telemetry_records), 1)
        self.assertNotIn("context_advisory", telemetry_records[0])
        self.assertEqual(
            set(telemetry_records[0]) - {"telemetry_id", "record_sha256"},
            constants.TELEMETRY_FIELDS,
        )

    def test_dispatch_status_reports_latest_telemetry_and_context_advisory(
        self,
    ) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]

        self._record_telemetry(
            self._telemetry_payload(
                dispatch_id,
                max_context_tokens=50_000,
                recorded_at="2026-09-18T00:00:00+00:00",
            )
        )
        self._record_telemetry(
            self._telemetry_payload(
                dispatch_id,
                max_context_tokens=130_000,
                recorded_at="2026-09-18T00:05:00+00:00",
            )
        )

        status = coordinator.dispatch_status(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                dispatch=None,
                batch=batch["batch_id"],
                stale_after=900,
            )
        )
        entry = status["dispatches"][0]
        self.assertEqual(entry["telemetry"]["max_context_tokens"], 130_000)
        self.assertEqual(
            entry["context_advisory"],
            {
                "level": "warn",
                "limit": 150_000,
                "warn_at": 120_000,
                "observed": 130_000,
            },
        )

    def test_dispatch_status_context_advisory_ok_when_no_telemetry(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        self._create_architect_dispatch(batch["batch_id"])

        status = coordinator.dispatch_status(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                dispatch=None,
                batch=batch["batch_id"],
                stale_after=900,
            )
        )
        entry = status["dispatches"][0]
        self.assertIsNone(entry["telemetry"])
        self.assertEqual(
            entry["context_advisory"],
            {"level": "ok", "limit": 150_000, "warn_at": 120_000, "observed": None},
        )

    def test_heartbeat_dispatch_records_context_tokens_probe_in_extra(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]
        coordinator.send_dispatch(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                dispatch=dispatch_id,
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )

        result = coordinator.heartbeat_dispatch(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                dispatch=dispatch_id,
                note=None,
                context_tokens=142_000,
                context_source="probe",
            )
        )
        self.assertEqual(result["context_tokens"], 142_000)
        self.assertEqual(result["context_source"], "probe")
        on_disk = coordinator._read_object(
            self._records_root() / "dispatch-status" / f"{dispatch_id}.json",
            "status",
        )
        self.assertEqual(on_disk["context_tokens"], 142_000)
        self.assertEqual(on_disk["context_source"], "probe")

    def test_heartbeat_dispatch_without_context_tokens_omits_extra_fields(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]
        coordinator.send_dispatch(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                dispatch=dispatch_id,
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )

        result = coordinator.heartbeat_dispatch(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                dispatch=dispatch_id,
                note=None,
                context_tokens=None,
                context_source=None,
            )
        )
        self.assertNotIn("context_tokens", result)
        self.assertNotIn("context_source", result)
        on_disk = coordinator._read_object(
            self._records_root() / "dispatch-status" / f"{dispatch_id}.json",
            "status",
        )
        self.assertNotIn("context_tokens", on_disk)
        self.assertNotIn("context_source", on_disk)

    def test_heartbeat_dispatch_requires_context_tokens_and_source_together(
        self,
    ) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]
        coordinator.send_dispatch(
            _ns(
                repo=str(self.repo),
                state_dir=str(self.state_dir),
                dispatch=dispatch_id,
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )

        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.heartbeat_dispatch(
                _ns(
                    repo=str(self.repo),
                    state_dir=str(self.state_dir),
                    dispatch=dispatch_id,
                    note=None,
                    context_tokens=142_000,
                    context_source=None,
                )
            )
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.heartbeat_dispatch(
                _ns(
                    repo=str(self.repo),
                    state_dir=str(self.state_dir),
                    dispatch=dispatch_id,
                    note=None,
                    context_tokens=None,
                    context_source="probe",
                )
            )

    def _configure_project(self, **extra: object) -> None:
        """Turn the zero-config test repository into a configured one: a real `.harness/orchestration.json`
        (architect + the mandatory code-review assignment) plus the role manifests it validates against."""
        roles_dir = self.repo / ".harness" / "orchestration" / "roles"
        (roles_dir / "code-review.md").write_text(
            (ORCHESTRATION_ROOT / "roles" / "code-review.md").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        runtime = {"claude": {"profiles": ["p"], "model": "sonnet", "effort": "high"}}
        config = {
            "provider_profiles": {
                "p": {
                    "capabilities": ["architecture-analysis", "code-review"],
                    "fallback": [],
                    "known_limitations": ["none"],
                }
            },
            "assignment_plans": {
                "architect": {"zone": "repository", "runtimes": runtime},
                "code-review": {"zone": "repository", "runtimes": runtime},
            },
            "backend_zones": {"repository": {"paths": ["**"]}},
            "concurrency_budget": 1,
            "verification_commands": ["true"],
            **extra,
        }
        (self.repo / ".harness" / "orchestration.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "configure orchestration")
        _git(self.repo, "push", "origin", "master")

    def _rewritten_brief_validation(
        self,
        batch_id: str,
        dispatch_id: str,
        drop: set[str],
        **replace: object,
    ) -> None:
        """Rewrite a stored brief the way an older coordinator wrote it (fields dropped or replaced, integrity
        hash recomputed) and run the real `_validate_dispatch` against it."""
        root = ledger_ops._state_root(
            _ns(repo=str(self.repo), state_dir=str(self.state_dir)), self.repo
        )
        record = coordinator._read_object(
            self._records_root() / "dispatches" / f"{dispatch_id}.json", "dispatch"
        )
        brief = {key: value for key, value in record.items() if key not in drop}
        brief.update(replace)
        batch = ledger_ops._load_batch(root, batch_id)
        entry = next(
            item for item in batch["dispatches"] if item["dispatch_id"] == dispatch_id
        )
        entry["brief_sha256"] = hashlib.sha256(
            utils._canonical(brief).encode("utf-8")
        ).hexdigest()
        coordinator._validate_dispatch(
            self.repo, coordinator._config(self.repo), root, batch, brief
        )

    def test_dispatch_brief_records_default_tool_policy_and_context_budget(
        self,
    ) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])

        dispatch = self._create_architect_dispatch(batch["batch_id"])

        expected_tools = list(contract.DEFAULT_ALLOWED_TOOLS["read-only"])
        self.assertEqual(dispatch["brief"]["allowed_tools"], expected_tools)
        self.assertEqual(dispatch["brief"]["context_budget"], 150_000)
        on_disk = coordinator._read_object(
            self._records_root() / "dispatches" / f"{dispatch['dispatch_id']}.json",
            "dispatch",
        )
        self.assertEqual(on_disk["allowed_tools"], expected_tools)
        self.assertEqual(on_disk["context_budget"], 150_000)

    def test_dispatch_brief_takes_tool_policy_and_context_budget_from_project_config(
        self,
    ) -> None:
        self._configure_project(
            adaptive_continuation_policy={"context_limit": 90_000},
            tool_policy={"roles": {"architect": ["Read", "Grep"]}},
        )
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])

        dispatch = self._create_architect_dispatch(batch["batch_id"])

        self.assertEqual(dispatch["brief"]["allowed_tools"], ["Read", "Grep"])
        self.assertEqual(dispatch["brief"]["context_budget"], 90_000)

    def _architect_dispatch_fields(self, batch_id: str) -> JsonObject:
        return {
            "repo": str(self.repo),
            "state_dir": str(self.state_dir),
            "batch": batch_id,
            "role": "architect",
            "runtime": "claude",
            "purpose": "work",
            "candidate_commit": None,
            "delta_review_of": None,
            "model": "sonnet",
            "effort": "high",
        }

    def test_dispatch_admission_rejects_a_context_package_poorer_than_the_configured_role_minimum(
        self,
    ) -> None:
        """With a repo-wide `min_tier: full`, the real (offline, minimal-tier) Repo Map Context
        Package used by these tests is rejected at admission -- both at `propose` (a dry run, no
        brief written) and at `create` -- with a reason naming the role and both tiers."""
        self._configure_project(repo_map_policy={"min_tier": "full"})
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        fields = self._architect_dispatch_fields(batch["batch_id"])

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            coordinator.create_dispatch(_ns(propose=True, **fields))

        self.assertEqual(
            caught.exception.message,
            "Repo Map tier 'minimal' for role 'architect' is below the configured minimum 'full'",
        )
        self.assertIn("repo_map_policy", caught.exception.remedy)

        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.create_dispatch(
                _ns(
                    transition_digest="irrelevant-because-the-gate-runs-first",
                    approved_by="Malove",
                    approved_at="2026-09-17T00:00:00+00:00",
                    **fields,
                )
            )

    def test_dispatch_admission_rejects_a_context_package_above_the_role_context_budget(
        self,
    ) -> None:
        """`context_package_policy.max_tokens` may exceed the role's `context_limit`; the package
        must still fit the brief's `context_budget`, at `propose` and at `create`."""
        self._configure_project(adaptive_continuation_policy={"context_limit": 1})
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        fields = self._architect_dispatch_fields(batch["batch_id"])

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            coordinator.create_dispatch(_ns(propose=True, **fields))

        self.assertRegex(
            caught.exception.message,
            r"^Context Package estimate \d+ tokens exceeds the architect context budget of 1 tokens",
        )
        self.assertIn("expected_files", caught.exception.remedy)
        self.assertIn("context_limit", caught.exception.remedy)

        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.create_dispatch(
                _ns(
                    transition_digest="irrelevant-because-the-gate-runs-first",
                    approved_by="Malove",
                    approved_at="2026-09-17T00:00:00+00:00",
                    **fields,
                )
            )

    def test_dispatch_admission_without_a_repo_map_policy_never_blocks_on_degradation(
        self,
    ) -> None:
        """AC1: with no `repo_map_policy` configured at all -- the zero-config default for these
        tests -- dispatch admission is never blocked by Repo Map degradation, even though the
        real Context Package built here is `minimal`. Only the existing non-blocking
        `context_package_quality_warning` (proven in a companion test class) may appear."""
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])

        dispatch = self._create_architect_dispatch(batch["batch_id"])

        self.assertEqual(dispatch["state"], "approved")

    def test_dispatch_admission_unlisted_role_keeps_portable_default_behaviour(
        self,
    ) -> None:
        """AC3: `min_tier_by_role` names only `developer`; `architect` is not listed and there is
        no repo-wide `min_tier`, so it keeps the default unblocked behaviour even though the real
        Context Package built here is `minimal`."""
        self._configure_project(
            repo_map_policy={"min_tier_by_role": {"developer": "full"}}
        )
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])

        dispatch = self._create_architect_dispatch(batch["batch_id"])

        self.assertEqual(dispatch["state"], "approved")

    def test_only_write_mode_default_tools_include_edit_tools(self) -> None:
        for name in ("Edit", "Write"):
            self.assertNotIn(name, contract.DEFAULT_ALLOWED_TOOLS["read-only"])
            self.assertIn(name, contract.DEFAULT_ALLOWED_TOOLS["write"])

    def test_resolve_allowed_tools_prefers_role_then_mode_then_default(self) -> None:
        policy = {
            "tool_policy": {
                "modes": {"write": ["Read", "Edit"]},
                "roles": {"qa": ["Read", "Bash"]},
            }
        }

        self.assertEqual(
            contract.resolve_allowed_tools(policy, "qa", "read-only"), ["Read", "Bash"]
        )
        self.assertEqual(
            contract.resolve_allowed_tools(policy, "developer", "write"),
            ["Read", "Edit"],
        )
        self.assertEqual(
            contract.resolve_allowed_tools(policy, "architect", "read-only"),
            list(contract.DEFAULT_ALLOWED_TOOLS["read-only"]),
        )
        self.assertEqual(
            contract.resolve_allowed_tools({}, "developer", "write"),
            list(contract.DEFAULT_ALLOWED_TOOLS["write"]),
        )

    def test_brief_created_before_tool_policy_fields_stays_valid(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])

        self._rewritten_brief_validation(
            batch["batch_id"],
            dispatch["dispatch_id"],
            {"allowed_tools", "context_budget"},
        )
        self._rewritten_brief_validation(
            batch["batch_id"],
            dispatch["dispatch_id"],
            {"allowed_tools", "context_budget", "report_staging_path"},
        )

    def test_brief_with_only_one_tool_policy_field_is_rejected(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])

        for dropped in ("allowed_tools", "context_budget"):
            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "schema mismatch"
            ):
                self._rewritten_brief_validation(
                    batch["batch_id"], dispatch["dispatch_id"], {dropped}
                )

    def test_brief_with_malformed_tool_policy_fields_is_rejected(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])

        for tools in ([], ["Read", "Read"], ["Read", ""], "Read"):
            with (
                self.subTest(allowed_tools=tools),
                self.assertRaisesRegex(coordinator.CoordinatorError, "allowed_tools"),
            ):
                self._rewritten_brief_validation(
                    batch["batch_id"],
                    dispatch["dispatch_id"],
                    set(),
                    allowed_tools=tools,
                )
        for budget in (0, -1, True, "big"):
            with (
                self.subTest(context_budget=budget),
                self.assertRaisesRegex(coordinator.CoordinatorError, "context_budget"),
            ):
                self._rewritten_brief_validation(
                    batch["batch_id"],
                    dispatch["dispatch_id"],
                    set(),
                    context_budget=budget,
                )

    def test_brief_stays_valid_when_the_project_edits_tool_policy_or_context_limit_in_flight(
        self,
    ) -> None:
        self._configure_project()
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        config_path = self.repo / ".harness" / "orchestration.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["adaptive_continuation_policy"] = {"context_limit": 500}
        config["tool_policy"] = {"roles": {"architect": ["Read"]}}
        config_path.write_text(json.dumps(config), encoding="utf-8")

        self._rewritten_brief_validation(
            batch["batch_id"], dispatch["dispatch_id"], set()
        )

    def _tool_policy_health(self, tool_policy: object) -> list[str]:
        path = self.tmp / "orchestration.json"
        path.write_text(
            json.dumps(
                {
                    "provider_profiles": {},
                    "assignment_plans": {},
                    "backend_zones": {},
                    "concurrency_budget": 1,
                    "verification_commands": [],
                    "tool_policy": tool_policy,
                }
            ),
            encoding="utf-8",
        )
        return contract.health_problems(path, ORCHESTRATION_ROOT / "roles")

    def test_health_accepts_a_valid_tool_policy(self) -> None:
        self.assertEqual(
            self._tool_policy_health(
                {"modes": {"read-only": ["Read"]}, "roles": {"qa": ["Read", "Bash"]}}
            ),
            [],
        )

    def test_health_rejects_an_invalid_tool_policy(self) -> None:
        invalid_policies: tuple[object, ...] = (
            ["Read"],
            {"other": {}},
            {"modes": ["Read"]},
            {"modes": {"admin": ["Read"]}},
            {"roles": {"no-such-role": ["Read"]}},
            {"roles": {"qa": []}},
            {"roles": {"qa": ["Read", ""]}},
            {"roles": {"qa": ["Read", "Read"]}},
            {"roles": {"qa": "Read"}},
        )
        for invalid in invalid_policies:
            with self.subTest(tool_policy=invalid):
                problems = self._tool_policy_health(invalid)
                self.assertTrue(
                    any(
                        problem.startswith("orchestration tool_policy")
                        for problem in problems
                    ),
                    problems,
                )

    def _operational_health(self, **sections: object) -> list[str]:
        path = self.tmp / "orchestration.json"
        path.write_text(
            json.dumps(
                {
                    "provider_profiles": {},
                    "assignment_plans": {},
                    "backend_zones": {},
                    "concurrency_budget": 1,
                    "verification_commands": [],
                    **sections,
                }
            ),
            encoding="utf-8",
        )
        return contract.health_problems(path, ORCHESTRATION_ROOT / "roles")

    def test_health_accepts_the_operational_policy_sections(self) -> None:
        self.assertEqual(
            self._operational_health(
                attention_policy={
                    "retry_queue_seconds": 60,
                    "max_infrastructure_retries": 0,
                    "stale_dispatch_seconds": 30,
                },
                approval_ttl_seconds=600,
                extensions={
                    "human_notifier": "my_pkg.notify:build",
                    "transport_health": "none",
                },
            ),
            [],
        )

    def test_health_rejects_invalid_operational_policy_sections(self) -> None:
        invalid: dict[str, JsonObject] = {
            "attention_policy_type": {"attention_policy": ["x"]},
            "attention_policy_field": {"attention_policy": {"retry_queue_seconds": 0}},
            "attention_policy_unknown": {"attention_policy": {"speed": 1}},
            "attention_policy_negative": {
                "attention_policy": {"max_infrastructure_retries": -1}
            },
            "heartbeat_interval_zero": {
                "attention_policy": {"heartbeat_interval_seconds": 0}
            },
            "preflight_estimate_zero": {
                "preflight_policy": {"estimated_tokens_per_file": 0}
            },
            "execution_timeout_zero": {
                "execution_policy": {"dispatch_wait_timeout_seconds": 0}
            },
            "starting_files_inverted": {
                "context_package_policy": {
                    "min_starting_files": 11,
                    "max_starting_files": 10,
                }
            },
            "ttl_zero": {"approval_ttl_seconds": 0},
            "ttl_bool": {"approval_ttl_seconds": True},
            "extensions_list": {"extensions": ["none"]},
            "extensions_kind": {"extensions": {"prompt_rewriter": "none"}},
            "extensions_name": {"extensions": {"human_notifier": "not a name"}},
            "extensions_type": {"extensions": {"human_notifier": 3}},
        }
        for label, sections in invalid.items():
            with self.subTest(label):
                self.assertTrue(self._operational_health(**sections), label)

    def test_the_seeded_project_template_is_healthy_and_selects_only_inert_extensions(
        self,
    ) -> None:
        example = ORCHESTRATION_ROOT.parent / "orchestration.example.json"
        # The whole example `harness init` seeds .harness/orchestration.json from is valid
        # against the shipped schema and role manifests, assignment plans included.
        self.assertEqual(
            contract.health_problems(example, ORCHESTRATION_ROOT / "roles"), []
        )
        template = json.loads(example.read_text(encoding="utf-8"))
        template.pop("$schema", None)
        self.assertEqual(
            self._operational_health(
                **{
                    k: v
                    for k, v in template.items()
                    if k
                    not in {
                        "provider_profiles",
                        "assignment_plans",
                        "backend_zones",
                        # names backend_zones, which this operational subset leaves out
                        "low_risk_zones",
                        "concurrency_budget",
                        "verification_commands",
                    }
                }
            ),
            [],
        )
        self.assertEqual(set(template["extensions"].values()), {"none"})
        self.assertEqual(
            config._attention_policy(template), template["attention_policy"]
        )
        self.assertEqual(
            config._execution_policy(template), template["execution_policy"]
        )

    def test_cli_uses_config_for_unspecified_execution_limits(self) -> None:
        parser = coordinator_cli.build_parser(coordinator, constants)
        waiting = parser.parse_args(["dispatch", "wait", "--dispatch", "dispatch-1"])
        qa = parser.parse_args(["qa", "run", "--dispatch", "dispatch-1"])
        self.assertIsNone(waiting.timeout)
        self.assertIsNone(waiting.poll_interval)
        self.assertIsNone(waiting.stale_after)
        self.assertIsNone(qa.lease_seconds)
        configured = config._execution_policy(
            {"execution_policy": {"dispatch_wait_timeout_seconds": 42}}
        )
        self.assertEqual(configured["dispatch_wait_timeout_seconds"], 42)
        self.assertEqual(configured["qa_lease_seconds"], 1800)

    def test_persist_report_takes_an_explicit_ledger_instead_of_sniffing_the_path(
        self,
    ) -> None:
        """``_persist_report`` (the one write path with no Value Object -- no ``ReportRecord``
        exists) still uses the bare ``Path``+``dict`` primitives, but takes its ``LifecycleLedger``
        explicitly rather than rediscovering it by walking the filesystem for a ``ledger.json``
        marker (the now-deleted ``_ledger_for_path``)."""
        parameters = inspect.signature(coordinator._persist_report).parameters
        self.assertEqual(
            list(parameters),
            ["ledger", "root", "batch", "dispatch", "report", "auto_accept_policy"],
        )
        self.assertIs(
            parameters["auto_accept_policy"].kind, inspect.Parameter.KEYWORD_ONLY
        )

    def _ledger(self) -> LifecycleLedger:
        return LifecycleLedger(
            ledger_ops._state_root(
                _ns(repo=str(self.repo), state_dir=str(self.state_dir)), self.repo
            )
        )

    def test_write_record_maps_a_ledger_refusal_to_a_coordinator_error(self) -> None:
        batch = self._create_batch()
        ledger = self._ledger()
        record = BatchRecord.from_dict(batch)

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            ledger_ops._write_record(ledger, record)

        self.assertEqual(
            caught.exception.message,
            f"refusing to overwrite immutable record: {batch['batch_id']}.json",
        )
        self.assertIn("use a different record id", caught.exception.remedy)

    def test_replace_record_maps_a_ledger_refusal_to_a_coordinator_error(self) -> None:
        self._create_batch()
        ledger = self._ledger()
        missing = BatchRecord(
            batch_id="batch-0000",
            state="planned",
            dispatches=[],
            coordinator_approval=None,
        )

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            ledger_ops._replace_record(ledger, missing)

        self.assertEqual(
            caught.exception.message,
            "ledger transition targets a missing record: batches/batch-0000.json",
        )
        self.assertIn("write the record at", caught.exception.remedy)

    def _create_batch_args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "repo": str(self.repo),
            "state_dir": str(self.state_dir),
            "ticket": "#195",
            "branch": "feature/issue-195-thing",
            "worktree": str(self.tmp / "worktree"),
            "allowed_path": ["**"],
            "integration_ref": "master",
            "definition_of_done": ["do the thing"],
            "prohibited_change": ["secrets"],
            "required_gate": None,
            "dependency": None,
            "expected_file": ["services/x.py"],
            "expected_service": ["core"],
            "expected_changed_lines": 10,
            "expected_context_tokens": None,
        }
        values.update(overrides)
        return _ns(**values)

    def test_preflight_uses_configured_context_estimate_weights(self) -> None:
        args = self._create_batch_args(expected_changed_lines=10)
        with mock.patch.object(
            config,
            "_config",
            return_value={
                **config._config(self.repo),
                "preflight_policy": {
                    "estimated_tokens_per_changed_line": 7,
                    "estimated_tokens_per_file": 300,
                },
            },
        ):
            result = coordinator.preflight_batch(args)
        self.assertEqual(result["expected_context_tokens"], 370)

    def test_create_batch_rejects_a_missing_branch_or_worktree(self) -> None:
        with self.assertRaises(coordinator.CoordinatorError) as no_branch:
            coordinator.create_batch(self._create_batch_args(branch=None))
        self.assertEqual(
            no_branch.exception.message, "branch must be a non-empty string"
        )
        self.assertEqual(no_branch.exception.remedy, "pass a non-empty branch name")

        with self.assertRaises(coordinator.CoordinatorError) as no_worktree:
            coordinator.create_batch(self._create_batch_args(worktree=None))
        self.assertEqual(
            no_worktree.exception.message, "worktree must be a non-empty string"
        )
        self.assertEqual(no_worktree.exception.remedy, "pass a non-empty worktree path")

    def test_create_batch_rejects_a_missing_ticket(self) -> None:
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            coordinator.create_batch(self._create_batch_args(ticket=None))
        self.assertEqual(caught.exception.message, "ticket must be a non-empty string")
        self.assertEqual(caught.exception.remedy, "pass a non-empty --ticket")

    def test_create_batch_requires_an_explicit_write_scope(self) -> None:
        scopes: list[list[str] | None] = [None, []]
        for allowed in scopes:
            with self.subTest(allowed=allowed):
                with self.assertRaises(coordinator.CoordinatorError) as caught:
                    coordinator.create_batch(
                        self._create_batch_args(allowed_path=allowed)
                    )
                self.assertEqual(
                    caught.exception.message,
                    "a batch must pin the explicit write scope of its writer",
                )
                self.assertIn("--allowed-path", caught.exception.remedy)

    def test_create_batch_rejects_a_scope_that_escapes_the_repository(self) -> None:
        for bad in ("/etc/**", "../other/**", "./src/**", "src//x/**"):
            with self.subTest(bad=bad):
                with self.assertRaises(coordinator.CoordinatorError) as caught:
                    coordinator.create_batch(
                        self._create_batch_args(allowed_path=[bad])
                    )
                self.assertIn("relative paths or globs", caught.exception.message)

    def test_prior_review_entry_returns_a_retried_code_review_entry(self) -> None:
        entry = {
            "dispatch_id": "dispatch-1",
            "role": "code-review",
            "state": "reported",
            "decision": {"decision": "retry"},
        }
        batch = {
            "dispatches": [{"dispatch_id": "dispatch-0", "role": "developer"}, entry]
        }

        self.assertIs(dispatch._prior_review_entry(batch, "dispatch-1"), entry)

    def test_prior_review_entry_rejects_a_missing_or_non_review_dispatch(self) -> None:
        batch = {"dispatches": [{"dispatch_id": "dispatch-0", "role": "developer"}]}
        for dispatch_id in ("dispatch-9", "dispatch-0"):
            with self.subTest(dispatch_id=dispatch_id):
                with self.assertRaises(coordinator.CoordinatorError) as caught:
                    dispatch._prior_review_entry(batch, dispatch_id)
                self.assertEqual(
                    caught.exception.message,
                    "delta-review-of must reference a code-review dispatch in this batch",
                )
                self.assertEqual(
                    caught.exception.remedy,
                    "pass --delta-review-of naming a code-review dispatch that belongs to this batch",
                )

    def test_prior_review_entry_rejects_a_review_that_was_not_retried(self) -> None:
        entry = {
            "dispatch_id": "dispatch-1",
            "role": "code-review",
            "state": "reported",
            "decision": {"decision": "accept"},
        }

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            dispatch._prior_review_entry({"dispatches": [entry]}, "dispatch-1")

        self.assertEqual(
            caught.exception.message,
            "delta-review-of must reference a retried code-review dispatch",
        )
        self.assertEqual(
            caught.exception.remedy,
            "pass --delta-review-of naming a code-review dispatch that was actually retried",
        )

    def _self_report(self, dispatch_id: str, **overrides: object) -> JsonObject:
        values: dict[str, object] = {
            "repo": str(self.repo),
            "state_dir": str(self.state_dir),
            "dispatch": dispatch_id,
            "model": "sonnet",
            "worktree": None,
        }
        values.update(overrides)
        return coordinator.self_report_dispatch(_ns(**values))

    def _approved_architect_dispatch(self) -> JsonObject:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        status_path = (
            self._records_root() / "dispatch-status" / f"{dispatch['dispatch_id']}.json"
        )
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["state"] = "dispatched"
        status_path.write_text(json.dumps(status), encoding="utf-8")
        return coordinator._read_object(
            self._records_root() / "dispatches" / f"{dispatch['dispatch_id']}.json",
            "dispatch",
        )

    def test_self_report_dispatch_blocks_a_model_mismatch(self) -> None:
        dispatch = self._approved_architect_dispatch()

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._self_report(dispatch["dispatch_id"], model="not-the-approved-model")

        self.assertEqual(
            caught.exception.message,
            f"dispatch running 'not-the-approved-model' but approved brief resolved {dispatch['resolved_model']!r}; "
            "the dispatch is blocked and needs a new coordinator decision",
        )
        self.assertEqual(
            caught.exception.remedy,
            "resolve the listed mismatch(es) and get a fresh coordinator decision before continuing this dispatch",
        )

    def test_self_report_dispatch_blocks_when_a_required_worktree_attestation_is_missing(
        self,
    ) -> None:
        dispatch = self._approved_architect_dispatch()
        path = self._records_root() / "dispatches" / f"{dispatch['dispatch_id']}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["worker_attestation_required"] = True
        path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._self_report(dispatch["dispatch_id"], model=dispatch["resolved_model"])

        self.assertEqual(
            caught.exception.message,
            "dispatch runtime worktree attestation is required; the dispatch is blocked and needs a new coordinator decision",
        )


class CoordinatorRetryRoutingTests(unittest.TestCase):
    """``batch decide --decision retry|abandon`` routing, against a real ledger and real git.

    Every stage is reached through the coordinator's public functions (architect -> developer ->
    risk assessment -> code-review -> QA -> publish). Only the clean-room QA lane and the publish
    boundary never run as a worker session here, so their reports are staged the way those two
    components persist them.
    """

    APPROVED_AT = "2026-09-17T00:00:00+00:00"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        roles = self.repo / ".harness" / "orchestration" / "roles"
        for name in ("developer", "verification", "code-review", "qa"):
            (roles / f"{name}.md").write_text(
                (ORCHESTRATION_ROOT / "roles" / f"{name}.md").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "roles")
        _git(self.repo, "push", "origin", "master")
        self.state_dir = self.tmp / "state"
        self.worktree = self.tmp / "worktree"
        self.branch = "feature/issue-244-routing"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reset(self) -> None:
        """A clean repository for the next ``subTest``."""
        self.tearDown()
        self.setUp()

    # -- plumbing -----------------------------------------------------------------------------

    def _args(self, **values: object) -> argparse.Namespace:
        return _ns(repo=str(self.repo), state_dir=str(self.state_dir), **values)

    def _approval(self) -> JsonObject:
        return {"approved_by": "Malove", "approved_at": self.APPROVED_AT}

    def _records(self) -> Path:
        return LifecycleLedger(
            ledger_ops._state_root(self._args(), self.repo)
        ).records_root()

    def _batch_record(self, batch_id: str) -> JsonObject:
        return coordinator._read_object(
            self._records() / "batches" / f"{batch_id}.json", "batch"
        )

    def _batch_plan(self) -> JsonObject:
        return {
            "ticket": "#244",
            "branch": self.branch,
            "worktree": str(self.worktree),
            "allowed_path": ["**"],
            "integration_ref": "master",
            "definition_of_done": ["route retries by cause"],
            "prohibited_change": ["secrets"],
            "required_gate": None,
            "dependency": None,
            "expected_file": ["services/x.py"],
            "expected_service": ["core"],
            "expected_changed_lines": 10,
            "expected_context_tokens": None,
        }

    def _create_batch(
        self, *, definition_of_done: list[str] | None = None
    ) -> JsonObject:
        _git(
            self.repo,
            "worktree",
            "add",
            "-b",
            self.branch,
            str(self.worktree),
            "master",
        )
        plan = self._batch_plan()
        if definition_of_done is not None:
            plan["definition_of_done"] = definition_of_done
        batch = coordinator.create_batch(self._args(**plan))
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )
        self.batch_id = batch["batch_id"]
        return batch

    def _proposal_fields(
        self, batch_id: str, role: str, purpose: str, candidate: str | None
    ) -> JsonObject:
        return {
            "batch": batch_id,
            "role": role,
            "runtime": "claude",
            "purpose": purpose,
            "candidate_commit": candidate,
            "delta_review_of": None,
            "model": "sonnet",
            "effort": "high",
        }

    def _propose(
        self,
        batch_id: str,
        role: str,
        *,
        purpose: str = "work",
        candidate: str | None = None,
    ) -> JsonObject:
        """The dry run a human approves: the canonical transition and its digest, no brief written."""
        return coordinator.create_dispatch(
            self._args(
                propose=True,
                **self._proposal_fields(batch_id, role, purpose, candidate),
            )
        )

    def _dispatch(
        self,
        batch_id: str,
        role: str,
        *,
        purpose: str = "work",
        candidate: str | None = None,
        digest: str | None = None,
    ) -> JsonObject:
        fields = self._proposal_fields(batch_id, role, purpose, candidate)
        if digest is None:
            digest = self._propose(
                batch_id, role, purpose=purpose, candidate=candidate
            )["transition_digest"]
        return coordinator.create_dispatch(
            self._args(transition_digest=digest, **fields, **self._approval())
        )

    def test_access_config_change_requires_a_new_approval_and_pins_the_plan(
        self,
    ) -> None:
        path = self.repo / ".harness/orchestration.json"
        path.write_text(
            json.dumps({"access_policy": {"defaults": {"mode": "sandbox"}}})
        )
        batch = self._create_batch()
        proposal = self._propose(batch["batch_id"], "architect")
        path.write_text(
            json.dumps({"access_policy": {"defaults": {"mode": "unsandboxed"}}})
        )
        with self.assertRaises(coordinator.CoordinatorError) as refused:
            self._dispatch(
                batch["batch_id"], "architect", digest=proposal["transition_digest"]
            )
        self.assertIn("digest", refused.exception.message)
        created = self._dispatch(batch["batch_id"], "architect")
        brief = created["brief"]
        self.assertEqual(brief["runtime_access"]["mode"], "unsandboxed")
        self.assertEqual(
            brief["transition"]["runtime_access_sha256"],
            brief["runtime_access"]["plan_digest"],
        )

    def test_brief_policy_rejects_unbound_or_malformed_pinned_access(self) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        malformed = json.loads(json.dumps(brief))
        malformed["runtime_access"]["network"] = {"hosts": ["https://github.com"]}
        plan = malformed["runtime_access"]
        plan["plan_digest"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in plan.items() if key != "plan_digest"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        malformed["transition"]["runtime_access_sha256"] = plan["plan_digest"]
        unbound = json.loads(json.dumps(brief))
        del unbound["transition"]["runtime_access_sha256"]
        missing = json.loads(json.dumps(brief))
        del missing["runtime_access"]
        for invalid in (malformed, unbound, missing):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(contract.ContractError) as refused,
            ):
                contract.validate_brief_policy(
                    invalid,
                    {},
                    config._config(self.repo),
                    self.repo / ".harness/orchestration/roles",
                )
            self.assertIn("access", refused.exception.message)

    def test_send_refuses_access_application_without_native_handoff_binding(
        self,
    ) -> None:
        from dataclasses import replace
        from harness.orchestration.runtime_access import AccessError

        class ApplyOnly:
            def observe(
                self, plan: JsonObject, transport: str
            ) -> extensions.RuntimeAccessObservation:
                return extensions.RuntimeAccessObservation(
                    plan_digest=plan["plan_digest"],
                    transport=transport,
                    supported_modes=("sandbox",),
                    effective_mode="sandbox",
                    mechanism="native-apply",
                    environment_id="worker",
                    launch_id="launch",
                    source="native-runtime",
                    observed_at=datetime.now(UTC).isoformat(),
                    hosts=(),
                    filesystem=tuple(
                        (item["path"], item["access"]) for item in plan["requirements"]
                    ),
                )

            def apply(
                self,
                brief: JsonObject,
                observation: extensions.RuntimeAccessObservation,
            ) -> extensions.RuntimeAccessObservation:
                return replace(observation, applied=True)

        extensions.register("runtime_access", "test-apply-only", ApplyOnly())
        self.addCleanup(extensions.unregister, "runtime_access", "test-apply-only")
        (self.repo / ".harness/orchestration.json").write_text(
            json.dumps(
                {
                    "access_policy": {"defaults": {"mode": "sandbox"}},
                    "extensions": {"runtime_access": "test-apply-only"},
                }
            )
        )
        brief = self._dispatch(self._create_batch()["batch_id"], "architect")["brief"]
        with self.assertRaises(AccessError) as refused:
            coordinator.send_dispatch(
                self._args(
                    dispatch=brief["dispatch_id"],
                    adapter=None,
                    adapter_arg=None,
                    checkout=None,
                )
            )
        self.assertIn("handoff", refused.exception.message)
        status = coordinator.dispatch_status(
            self._args(dispatch=brief["dispatch_id"], batch=None, stale_after=None)
        )
        self.assertEqual(status["dispatches"][0]["state"], "approved")

    def test_access_only_public_lifecycle_keeps_pinned_worker_proof(self) -> None:
        from dataclasses import replace

        launches: list[str] = []

        class ControlledNative:
            def observe(
                self, plan: JsonObject, transport: str
            ) -> extensions.RuntimeAccessObservation:
                return extensions.RuntimeAccessObservation(
                    plan_digest=plan["plan_digest"],
                    transport=transport,
                    supported_modes=("sandbox", "inherit"),
                    effective_mode="sandbox",
                    mechanism="native-apply",
                    environment_id="worker",
                    launch_id="reserved-launch",
                    source="native-runtime",
                    observed_at=datetime.now(UTC).isoformat(),
                    hosts=tuple(plan["network"]["hosts"]),
                    filesystem=tuple(
                        (item["path"], item["access"]) for item in plan["requirements"]
                    ),
                )

            def apply(
                self,
                brief: JsonObject,
                observation: extensions.RuntimeAccessObservation,
            ) -> extensions.RuntimeAccessObservation:
                return replace(observation, applied=True)

            def handoff(
                self,
                brief: JsonObject,
                observation: extensions.RuntimeAccessObservation,
                command: tuple[str, ...] | None,
            ) -> extensions.RuntimeAccessObservation:
                assert command is None
                launches.append(brief["dispatch_id"])
                return replace(
                    observation, dispatch_id=brief["dispatch_id"], handed_off=True
                )

        extensions.register("runtime_access", "test-worker-launch", ControlledNative())
        self.addCleanup(extensions.unregister, "runtime_access", "test-worker-launch")
        path = self.repo / ".harness/orchestration.json"
        authored: JsonObject = {
            "access_policy": {
                "defaults": {"mode": "sandbox", "network": {"hosts": ["github.com"]}}
            },
            "extensions": {"runtime_access": "test-worker-launch"},
        }
        path.write_text(json.dumps(authored))
        batch = self._create_batch()
        preview = coordinator.preflight_dispatch(
            self._args(
                batch=batch["batch_id"],
                role="architect",
                purpose="work",
                runtime="claude",
                candidate_commit=None,
            )
        )
        self.assertEqual(
            preview["decision_packet"]["runtime_access"]["verification"]["status"],
            "verified",
        )
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        authored["access_policy"]["defaults"]["network"]["hosts"].append("pypi.org")
        path.write_text(json.dumps(authored))
        self._start(brief["dispatch_id"])
        coordinator.heartbeat_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                note=None,
                context_tokens=None,
                context_source=None,
            )
        )
        status = coordinator.dispatch_status(
            self._args(dispatch=brief["dispatch_id"], batch=None, stale_after=None)
        )["dispatches"][0]
        proof = status["runtime_access"]
        self.assertEqual(proof["plan_digest"], brief["runtime_access"]["plan_digest"])
        self.assertEqual(proof["dispatch_id"], brief["dispatch_id"])
        self.assertTrue(proof["evidence"]["handed_off"])
        self.assertEqual(proof["evidence"]["hosts"], ["github.com"])
        self.assertEqual(launches, [brief["dispatch_id"]])

    def _start(self, dispatch_id: str, *, checkout: Path | None = None) -> None:
        coordinator.send_dispatch(
            self._args(
                dispatch=dispatch_id,
                adapter=None,
                adapter_arg=None,
                checkout=str(checkout) if checkout else None,
            )
        )
        coordinator.self_report_dispatch(
            self._args(
                dispatch=dispatch_id, model="sonnet", worktree=str(self.worktree)
            )
        )

    def _submit(self, dispatch_id: str, report: JsonObject) -> JsonObject:
        path = workspace._prepare_agent_inbox(self.repo) / f"{dispatch_id}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return coordinator.submit_report(self._args(file=str(path)))

    def _checks(self, brief: JsonObject, result: str = "pass") -> list[JsonObject]:
        return [
            {"command": command, "result": result, "evidence": "n/a"}
            for command in brief["verification_commands"]
        ]

    def _base_report(
        self, brief: JsonObject, role: str, **overrides: object
    ) -> JsonObject:
        report = {
            "dispatch_id": brief["dispatch_id"],
            "ticket": brief["ticket"],
            "role": role,
            "outcome": "completed",
            "output": "done",
            "commit_sha": "not applicable — read-only role",
            "changed_files": [],
            "checks_run": self._checks(brief),
            "risks": "none",
            "blockers": "none",
            "next_coordinator_action": "decide",
            "report_language": "ru",
        }
        report.update(overrides)
        return report

    def _decide(self, batch_id: str, decision: str, **extra: object) -> JsonObject:
        return coordinator.decide_batch(
            self._args(
                batch=batch_id,
                decision=decision,
                **{"note": None, **self._approval(), **extra},
            )
        )

    def _accepted_architect(self, batch_id: str) -> None:
        brief = self._dispatch(batch_id, "architect")["brief"]
        self._start(brief["dispatch_id"])
        self._submit(brief["dispatch_id"], self._base_report(brief, "architect"))
        self._decide(batch_id, "accept")

    def test_completion_report_retains_optional_memory_evidence(self) -> None:
        """Memory evidence survives submission but grants no authority to advance."""
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        report = self._base_report(
            brief,
            "architect",
            lessons=["Validate source authorization before retaining content hashes."],
            used_memory=["historical-hit-without-a-current-index"],
        )
        submitted = self._submit(brief["dispatch_id"], report)
        self.assertEqual(submitted["state"], "reported")
        stored = json.loads(Path(submitted["report"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["lessons"], report["lessons"])
        self.assertEqual(stored["used_memory"], report["used_memory"])
        markdown = (
            Path(submitted["report"]).with_suffix(".md").read_text(encoding="utf-8")
        )
        self.assertIn("Validate source authorization", markdown)
        self.assertIn("historical-hit-without-a-current-index", markdown)
        self.assertEqual(
            self._batch_record(batch["batch_id"])["state"], "awaiting-approval"
        )

    def test_completion_report_memory_fields_validate_shape_without_resolving_hits(
        self,
    ) -> None:
        """Lists are optional, but malformed evidence cannot enter an immutable report."""
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        for field in ("lessons", "used_memory"):
            for invalid in (None, "text", [1], ["   "], {"id": "hit"}):
                with self.subTest(field=field, invalid=invalid):
                    report = self._base_report(brief, "architect", **{field: invalid})
                    with self.assertRaisesRegex(coordinator.CoordinatorError, field):
                        self._submit(brief["dispatch_id"], report)
        report = self._base_report(brief, "architect", lessons=[], used_memory=[])
        self.assertEqual(
            self._submit(brief["dispatch_id"], report)["state"], "reported"
        )

    def test_milestone_clean_architect_report_prepares_developer(self) -> None:
        self._patch_config(approval_policy="milestone")
        batch = self._create_batch()
        architect = coordinator.create_dispatch(
            self._args(
                transition_digest=None,
                **self._proposal_fields(batch["batch_id"], "architect", "work", None),
            )
        )["brief"]
        self._start(architect["dispatch_id"])

        result = self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )

        stored = self._batch_record(batch["batch_id"])
        self.assertTrue(result["auto_accepted"])
        self.assertEqual(
            stored["dispatches"][0]["decision"]["approved_by"], "policy:milestone"
        )
        self.assertEqual(stored["next_action"], "developer")
        self.assertEqual(stored["dispatches"][-1]["role"], "developer")
        self.assertEqual(stored["dispatches"][-1]["state"], "approved")

    def test_milestone_developer_advances_to_qa_gate_and_qa_report_waits(self) -> None:
        self._patch_config(approval_policy="milestone")
        plan = self._batch_plan()
        plan["definition_of_done"] = ["add simple marker"]
        _git(
            self.repo,
            "worktree",
            "add",
            "-b",
            self.branch,
            str(self.worktree),
            "master",
        )
        batch = coordinator.create_batch(self._args(**plan))
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )
        self.batch_id = batch["batch_id"]
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        developer_id = self._batch_record(batch["batch_id"])["dispatches"][-1][
            "dispatch_id"
        ]
        developer = coordinator._read_object(
            self._records() / "dispatches" / f"{developer_id}.json", "dispatch"
        )
        self._start(developer_id)
        candidate, changed = self._developer_commit("x")

        result = self._submit(
            developer_id, self._developer_report(developer, candidate, changed)
        )

        stored = self._batch_record(batch["batch_id"])
        self.assertTrue(result["auto_accepted"])
        self.assertEqual(stored["next_action"], "qa")
        self.assertEqual(len(stored["dispatches"]), 2)
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "requires --approved-by"
        ):
            coordinator.create_dispatch(
                self._args(
                    transition_digest=None,
                    **self._proposal_fields(batch["batch_id"], "qa", "work", candidate),
                )
            )
        qa = self._reported_qa(batch["batch_id"], candidate)
        report_path = self._records() / "reports" / f"{qa['dispatch_id']}.json"
        with mock.patch.object(
            qa_lane,
            "run",
            return_value={"state": "reported", "report": str(report_path)},
        ):
            qa_result = coordinator.run_qa(self._args(dispatch=qa["dispatch_id"]))
        self.assertNotIn("auto_accepted", qa_result)
        self.assertNotIn(
            "decision", self._batch_record(batch["batch_id"])["dispatches"][-1]
        )

    def test_milestone_report_with_risk_trigger_waits_for_decision(self) -> None:
        self._patch_config(approval_policy="milestone")
        batch = self._create_batch()
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        developer_id = self._batch_record(batch["batch_id"])["dispatches"][-1][
            "dispatch_id"
        ]
        developer = coordinator._read_object(
            self._records() / "dispatches" / f"{developer_id}.json", "dispatch"
        )
        self._start(developer_id)
        candidate, changed = self._developer_commit("x")

        result = self._submit(
            developer_id,
            self._developer_report(
                developer, candidate, changed, risk_triggers=["transactions"]
            ),
        )

        self.assertNotIn("auto_accepted", result)
        stored = self._batch_record(batch["batch_id"])
        self.assertNotIn("decision", stored["dispatches"][-1])
        self.assertEqual(stored["state"], "awaiting-approval")

    def test_low_risk_clean_report_is_accepted_and_routes_to_developer(self) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])

        self._submit(
            brief["dispatch_id"],
            self._base_report(
                brief,
                "architect",
                blockers="none",
            ),
        )

        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(stored["dispatches"][0]["decision"]["decision"], "accept")
        self.assertEqual(stored["next_action"], "developer")
        self.assertEqual(
            stored["coordinator_decisions"][-1]["note"],
            "Auto-accepted due to low_risk policy and clean report",
        )
        self.assertEqual(
            stored["coordinator_decisions"][-1]["approved_by"], "policy:low_risk"
        )
        self.assertEqual(len(stored["dispatches"]), 2)
        self.assertEqual(stored["dispatches"][-1]["role"], "developer")
        self.assertEqual(stored["dispatches"][-1]["state"], "approved")

    def test_low_risk_report_disclosing_risk_waits_for_decision(self) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])

        result = self._submit(
            brief["dispatch_id"],
            self._base_report(brief, "architect", risks="Known alias limitation"),
        )

        self.assertNotIn("auto_accepted", result)
        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(stored["state"], "awaiting-approval")
        self.assertEqual(len(stored["dispatches"]), 1)
        self.assertNotIn("decision", stored["dispatches"][0])

    def test_milestone_report_disclosing_risk_waits_for_decision(self) -> None:
        self._patch_config(approval_policy="milestone")
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])

        result = self._submit(
            brief["dispatch_id"],
            self._base_report(brief, "architect", risks="Known alias limitation"),
        )

        self.assertNotIn("auto_accepted", result)
        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(stored["state"], "awaiting-approval")
        self.assertNotIn("decision", stored["dispatches"][0])

    def test_manual_all_clean_report_waits_for_a_decision(self) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])

        self._submit(brief["dispatch_id"], self._base_report(brief, "architect"))

        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(stored["state"], "awaiting-approval")
        self.assertNotIn("decision", stored["dispatches"][-1])
        self.assertNotIn("next_action", stored)

    def test_low_risk_developer_report_registers_risk_before_next_role(self) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        batch = self._create_batch()
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        developer_id = self._batch_record(batch["batch_id"])["dispatches"][-1][
            "dispatch_id"
        ]
        developer = coordinator._read_object(
            self._records() / "dispatches" / f"{developer_id}.json", "dispatch"
        )
        self._start(developer_id)
        candidate, changed = self._developer_commit("x")

        self._submit(
            developer_id,
            self._developer_report(developer, candidate, changed),
        )

        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(stored["dispatches"][1]["decision"]["decision"], "accept")
        self.assertEqual(len(stored["risk_assessments"]), 1)
        self.assertIn(stored["next_action"], {"code-review", "qa"})

    def test_low_risk_candidate_without_triggers_prepares_qa(self) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        plan = self._batch_plan()
        plan["definition_of_done"] = ["add simple marker"]
        _git(
            self.repo,
            "worktree",
            "add",
            "-b",
            self.branch,
            str(self.worktree),
            "master",
        )
        batch = coordinator.create_batch(self._args(**plan))
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )
        self.batch_id = batch["batch_id"]
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        developer_id = self._batch_record(batch["batch_id"])["dispatches"][-1][
            "dispatch_id"
        ]
        developer = coordinator._read_object(
            self._records() / "dispatches" / f"{developer_id}.json", "dispatch"
        )
        self._start(developer_id)
        candidate, changed = self._developer_commit("x")

        self._submit(
            developer_id, self._developer_report(developer, candidate, changed)
        )

        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(stored["risk_assessments"][0]["review_required"], False)
        self.assertEqual(stored["dispatches"][-1]["role"], "qa")
        self.assertEqual(stored["dispatches"][-1]["state"], "approved")

        qa_id = stored["dispatches"][-1]["dispatch_id"]
        qa = coordinator._read_object(
            self._records() / "dispatches" / f"{qa_id}.json", "dispatch"
        )
        self._stage_report(
            batch["batch_id"],
            qa_id,
            self._base_report(
                qa,
                "qa",
                checks_run=self._checks(qa, "pass"),
                blockers="none",
            ),
            via_qa_lane=True,
        )
        report_path = self._records() / "reports" / f"{qa_id}.json"
        with mock.patch.object(
            qa_lane,
            "run",
            return_value={"state": "reported", "report": str(report_path)},
        ):
            result = coordinator.run_qa(self._args(dispatch=qa_id))

        stored = self._batch_record(batch["batch_id"])
        self.assertTrue(result["auto_accepted"])
        self.assertEqual(stored["dispatches"][-1]["decision"]["decision"], "accept")
        self.assertEqual(stored["next_action"], "publish")

    def test_auto_clean_report_is_accepted_outside_the_low_risk_zones(self) -> None:
        self._patch_config(approval_policy="auto")
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])

        result = self._submit(
            brief["dispatch_id"], self._base_report(brief, "architect")
        )

        stored = self._batch_record(batch["batch_id"])
        self.assertTrue(result["auto_accepted"])
        self.assertEqual(
            stored["coordinator_decisions"][-1]["approved_by"], "policy:auto"
        )
        self.assertEqual(stored["next_action"], "developer")
        self.assertEqual(stored["dispatches"][-1]["state"], "approved")

    def test_low_risk_report_outside_the_low_risk_zones_waits_for_decision(
        self,
    ) -> None:
        self._patch_config(approval_policy="low_risk")
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])

        result = self._submit(
            brief["dispatch_id"], self._base_report(brief, "architect")
        )

        self.assertNotIn("auto_accepted", result)
        self.assertNotIn(
            "decision", self._batch_record(batch["batch_id"])["dispatches"][0]
        )

    def test_auto_accepts_a_clean_qa_report_outside_the_low_risk_zones(self) -> None:
        self._patch_config(approval_policy="auto")
        plan = self._batch_plan()
        plan["definition_of_done"] = ["add simple marker"]
        _git(
            self.repo,
            "worktree",
            "add",
            "-b",
            self.branch,
            str(self.worktree),
            "master",
        )
        batch = coordinator.create_batch(self._args(**plan))
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )
        self.batch_id = batch["batch_id"]
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        developer_id = self._batch_record(batch["batch_id"])["dispatches"][-1][
            "dispatch_id"
        ]
        developer = coordinator._read_object(
            self._records() / "dispatches" / f"{developer_id}.json", "dispatch"
        )
        self._start(developer_id)
        candidate, changed = self._developer_commit("x")

        self._submit(
            developer_id, self._developer_report(developer, candidate, changed)
        )

        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(stored["risk_assessments"][0]["review_required"], False)
        self.assertEqual(stored["dispatches"][-1]["role"], "qa")
        self.assertEqual(stored["dispatches"][-1]["state"], "approved")

        qa_id = stored["dispatches"][-1]["dispatch_id"]
        qa = coordinator._read_object(
            self._records() / "dispatches" / f"{qa_id}.json", "dispatch"
        )
        self._stage_report(
            batch["batch_id"],
            qa_id,
            self._base_report(
                qa,
                "qa",
                checks_run=self._checks(qa, "pass"),
                blockers="none",
            ),
            via_qa_lane=True,
        )
        report_path = self._records() / "reports" / f"{qa_id}.json"
        with mock.patch.object(
            qa_lane,
            "run",
            return_value={"state": "reported", "report": str(report_path)},
        ):
            result = coordinator.run_qa(self._args(dispatch=qa_id))

        stored = self._batch_record(batch["batch_id"])
        self.assertTrue(result["auto_accepted"])
        self.assertEqual(stored["dispatches"][-1]["decision"]["decision"], "accept")
        self.assertEqual(stored["next_action"], "publish")
        self.assertEqual(
            stored["dispatches"][-1]["decision"]["approved_by"], "policy:auto"
        )

    def test_low_risk_review_with_findings_still_waits_for_decision(self) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        batch = self._create_batch()
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        developer_id = self._batch_record(batch["batch_id"])["dispatches"][-1][
            "dispatch_id"
        ]
        developer = coordinator._read_object(
            self._records() / "dispatches" / f"{developer_id}.json", "dispatch"
        )
        self._start(developer_id)
        candidate, changed = self._developer_commit("x")
        self._submit(
            developer_id, self._developer_report(developer, candidate, changed)
        )
        self.assertEqual(
            self._batch_record(batch["batch_id"])["next_action"], "code-review"
        )
        review = self._reported_review(
            batch["batch_id"],
            candidate,
            standards=(
                "warning",
                [{"severity": "warning", "summary": "style", "evidence": "file line"}],
            ),
        )

        stored = self._batch_record(batch["batch_id"])
        entry = next(
            item
            for item in stored["dispatches"]
            if item["dispatch_id"] == review["dispatch_id"]
        )
        self.assertEqual(stored["state"], "awaiting-approval")
        self.assertNotIn("decision", entry)

    def test_resume_failed_developer_start_keeps_accepted_architect(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        failed = self._dispatch(batch["batch_id"], "developer")["brief"]
        coordinator.send_dispatch(
            self._args(
                dispatch=failed["dispatch_id"],
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.self_report_dispatch(
                self._args(
                    dispatch=failed["dispatch_id"],
                    model="wrong-model",
                    worktree=None,
                )
            )

        resumed = coordinator.resume_batch(
            self._args(batch=batch["batch_id"], reason="startup mismatch fixed")
        )
        retry = self._dispatch(batch["batch_id"], "developer")["brief"]

        self.assertEqual(resumed["next_action"], "developer")
        self.assertNotEqual(retry["dispatch_id"], failed["dispatch_id"])
        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(stored["dispatches"][0]["decision"]["decision"], "accept")
        self.assertEqual(
            [entry["role"] for entry in stored["dispatches"]],
            ["architect", "developer", "developer"],
        )

    def test_resume_stale_developer_dispatch_keeps_architect(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        stalled = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(stalled["dispatch_id"])
        self._age_heartbeat(stalled["dispatch_id"], 7200)
        event = coordinator.wait_dispatch(
            self._args(
                dispatch=stalled["dispatch_id"],
                timeout=1,
                poll_interval=1,
                stale_after=900,
            )
        )
        self.assertEqual(event["event"], "stale")

        resumed = coordinator.resume_batch(
            self._args(batch=batch["batch_id"], reason="worker timed out")
        )
        retry = self._dispatch(batch["batch_id"], "developer")["brief"]

        self.assertEqual(resumed["next_action"], "developer")
        self.assertNotEqual(retry["dispatch_id"], stalled["dispatch_id"])
        self.assertFalse(self._batch_record(batch["batch_id"]).get("needs_attention"))

    def _developer_commit(self, name: str) -> tuple[str, list[str]]:
        path = self.worktree / "services" / f"{name}.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"VALUE = {name!r}\n", encoding="utf-8")
        _git(self.worktree, "add", "-A")
        _git(self.worktree, "commit", "-m", f"feat: {name}")
        candidate = _git(self.worktree, "rev-parse", "HEAD")
        base = self._batch_record(self.batch_id)["base_commit"]
        return candidate, git_utils._changed_files_between(self.repo, base, candidate)

    def _developer_report(
        self, brief: JsonObject, candidate: str, changed: list[str], **overrides: object
    ) -> JsonObject:
        defaults: JsonObject = {
            "commit_sha": candidate,
            "changed_files": changed,
            "commit_map": [
                {"commit_sha": candidate, "plan_entry_id": entry["id"]}
                for entry in brief["commit_plan"]
            ],
        }
        carried = carried_items.section_item_ids(brief.get("carried_items") or {})
        if commit_plan.is_developer_retry(brief) and carried:
            # A developer-retry brief that carried items owes their closure (issue #503).
            defaults["carried_item_closure"] = [
                {"item_id": item_id, "commits": [candidate]} for item_id in carried
            ]
        defaults.update(overrides)
        return self._base_report(brief, "developer", **defaults)

    def _accepted_candidate(self, batch_id: str, name: str = "x") -> str:
        """A developer commit that was accepted and risk-assessed as review-required."""
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        candidate, changed = self._developer_commit(name)
        self._submit(
            brief["dispatch_id"], self._developer_report(brief, candidate, changed)
        )
        self._decide(batch_id, "accept")
        self._assess(batch_id, candidate, changed)
        return candidate

    def _retried_developer_candidate(
        self, batch_id: str, *names: str
    ) -> tuple[JsonObject, list[str], list[str]]:
        """A clean developer report, one commit per name, retried for a code defect, never accepted."""
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        commits = [self._developer_commit(name)[0] for name in names]
        changed = git_utils._changed_files_between(
            self.repo, self._batch_record(batch_id)["base_commit"], commits[-1]
        )
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                commits[-1],
                changed,
                commit_map=[
                    {"commit_sha": sha, "plan_entry_id": entry["id"]}
                    for sha, entry in zip(commits, brief["commit_plan"])
                ],
            ),
        )
        self._decide(batch_id, "retry", reason_category="code")
        return brief, commits, changed

    def _assess(self, batch_id: str, candidate: str, changed: list[str]) -> None:
        coordinator.assess_risk(
            self._args(
                batch=batch_id,
                candidate_commit=candidate,
                base_commit=None,
                changed_file=changed,
                developer_trigger=["transactions"],
            )
        )

    def _axes(
        self,
        standards: tuple[str, list[JsonObject]],
        spec: tuple[str, list[JsonObject]],
    ) -> dict[str, JsonObject]:
        return {
            axis: {
                "severity": severity,
                "findings": findings,
                "risks": "none",
                "blockers": "none",
            }
            for axis, (severity, findings) in (("standards", standards), ("spec", spec))
        }

    def _reported_review(
        self,
        batch_id: str,
        candidate: str,
        *,
        outcome: str = "completed",
        blockers: str = "none",
        standards: tuple[str, list[JsonObject]] = ("clean", []),
        spec: tuple[str, list[JsonObject]] = ("clean", []),
        carried: object = None,
        tooling_blocker: object = None,
        incomplete_items: object = None,
    ) -> JsonObject:
        """``carried``, a dict, maps a carried item id to the status the review gives it (issue
        #499). It is typed ``object`` so the axis keyword dicts other tests unpack still check.
        ``tooling_blocker``, a dict, is the structured tool evidence of the report (issue #500).
        ``incomplete_items``, a list, names the brief items the review left undone (issue #501)."""
        brief: JsonObject = self._dispatch(
            batch_id, "code-review", candidate=candidate
        )["brief"]
        self._start(brief["dispatch_id"], checkout=self.worktree)
        review: JsonObject = {
            "candidate_commit": candidate,
            "scope": brief["review_scope"],
            **self._axes(standards, spec),
        }
        if isinstance(carried, dict):
            review["carried_items"] = [
                {"item_id": item_id, "status": status, "evidence": "services/x.py:1"}
                for item_id, status in carried.items()
            ]
        self._submit(
            brief["dispatch_id"],
            self._base_report(
                brief,
                "code-review",
                outcome=outcome,
                blockers=blockers,
                checks_run=self._checks(
                    brief, "pass" if outcome == "completed" else "not-run"
                ),
                review=review,
                **(
                    {"tooling_blocker": tooling_blocker}
                    if isinstance(tooling_blocker, dict)
                    else {}
                ),
                **(
                    {"incomplete_items": incomplete_items}
                    if isinstance(incomplete_items, list)
                    else {}
                ),
            ),
        )
        return brief

    def _tooling_review(self, batch_id: str, candidate: str) -> JsonObject:
        return self._reported_review(
            batch_id,
            candidate,
            outcome="blocked",
            blockers="a hook blocked a legitimate check command",
            standards=("none", []),
            spec=("none", []),
            tooling_blocker=dict(TOOLING_BLOCKER),
        )

    def _infra_review(self, batch_id: str, candidate: str) -> JsonObject:
        return self._reported_review(
            batch_id,
            candidate,
            outcome="blocked",
            blockers="Bash/WSL wrapper unavailable; checks could not run",
            standards=("none", []),
            spec=("none", []),
        )

    def _stage_report(
        self, batch_id: str, dispatch_id: str, report: JsonObject, *, via_qa_lane: bool
    ) -> None:
        """Persist a report for a role that never runs as a worker session in this suite."""
        root = ledger_ops._state_root(self._args(), self.repo)
        ledger = LifecycleLedger(root)
        with ledger_ops._ledger_lock(ledger):
            batch = ledger_ops._load_batch(root, batch_id)
            entry = next(
                item
                for item in batch["dispatches"]
                if item["dispatch_id"] == dispatch_id
            )
            entry["state"] = "dispatched"
            ledger_ops._replace_record(ledger, BatchRecord.from_dict(batch))
            status = ledger_ops._load_dispatch_status(root, dispatch_id)
            status.update(
                {
                    "state": "working" if via_qa_lane else "dispatched",
                    "updated_at": coordinator._now(),
                }
            )
            ledger_ops._replace_record(ledger, DispatchStatusRecord.from_dict(status))
            dispatch = ledger_ops._load_dispatch(root, dispatch_id)
            if via_qa_lane:
                qa_lane._record_report(
                    ledger, root, self.repo, dispatch, report, coordinator
                )
            else:
                coordinator._persist_report(
                    ledger,
                    root,
                    ledger_ops._load_batch(root, batch_id),
                    dispatch,
                    report,
                )

    def _reported_qa(
        self,
        batch_id: str,
        candidate: str,
        *,
        outcome: str = "completed",
        check_result: str = "pass",
    ) -> JsonObject:
        brief: JsonObject = self._dispatch(batch_id, "qa", candidate=candidate)["brief"]
        self._stage_report(
            batch_id,
            brief["dispatch_id"],
            self._base_report(
                brief,
                "qa",
                outcome=outcome,
                checks_run=self._checks(brief, check_result),
                blockers="none" if outcome == "completed" else "QA could not complete",
            ),
            via_qa_lane=True,
        )
        return brief

    def _reported_publish(
        self, batch_id: str, candidate: str, blockers: str
    ) -> JsonObject:
        brief: JsonObject = self._dispatch(
            batch_id, "developer", purpose="publish", candidate=candidate
        )["brief"]
        base = self._batch_record(batch_id)["base_commit"]
        self._stage_report(
            batch_id,
            brief["dispatch_id"],
            self._developer_report(
                brief,
                candidate,
                git_utils._changed_files_between(self.repo, base, candidate),
                outcome="blocked",
                blockers=blockers,
                checks_run=self._checks(brief, "not-run"),
            ),
            via_qa_lane=False,
        )
        return brief

    def _accepted_review_and_qa(self, batch_id: str, candidate: str) -> None:
        self._reported_review(batch_id, candidate)
        self._decide(batch_id, "accept")
        self._reported_qa(batch_id, candidate)
        self._decide(batch_id, "accept")

    def _dispatch_ids(self, batch_id: str) -> list[str]:
        return [
            item["dispatch_id"] for item in self._batch_record(batch_id)["dispatches"]
        ]

    def _routing(self, batch: JsonObject) -> JsonObject:
        return cast(JsonObject, batch["coordinator_decisions"][-1]["routing"])

    def _assert_route(
        self,
        batch: JsonObject,
        *,
        role: str,
        action: str,
        category: str,
        candidate: str | None,
        route: str,
    ) -> JsonObject:
        routing = self._routing(batch)
        self.assertEqual(
            (
                routing["next_role"],
                routing["next_action"],
                routing["reason_category"],
                routing["candidate_commit"],
                routing["route"],
            ),
            (role, action, category, candidate, route),
        )
        self.assertEqual(batch["next_action"], action)
        self.assertTrue(routing["rationale"].strip())
        return routing

    # -- code-review ---------------------------------------------------------------------------

    def test_infrastructure_blocked_developer_registers_candidate_then_verifies_same_sha(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        candidate, changed = self._developer_commit("infrastructure")
        self._submit(
            developer["dispatch_id"],
            self._developer_report(
                developer,
                candidate,
                changed,
                outcome="blocked",
                blockers="verification environment unavailable",
            ),
        )

        decided = self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )

        self._assert_route(
            decided,
            role="verification",
            action="verification",
            category="verification-infrastructure",
            candidate=candidate,
            route="verification",
        )
        registration = decided["candidate_registrations"][-1]
        self.assertEqual(registration["candidate_commit"], candidate)
        self.assertEqual(registration["source_dispatch_id"], developer["dispatch_id"])
        self.assertIn("source_report_sha256", registration)
        verification = self._dispatch(
            batch["batch_id"], "verification", candidate=candidate
        )["brief"]
        self.assertEqual(verification["candidate_commit"], candidate)
        self.assertEqual(verification["snapshot_commit"], candidate)
        self.assertEqual(verification["access"], "read-only")
        self._start(verification["dispatch_id"], checkout=self.worktree)
        self._submit(
            verification["dispatch_id"],
            self._base_report(verification, "verification"),
        )
        accepted = self._decide(batch["batch_id"], "accept")

        self.assertEqual(accepted["next_action"], "risk-assessment")
        self._assess(batch["batch_id"], candidate, changed)

    def test_developer_code_failure_cannot_register_candidate_for_verification(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        candidate, changed = self._developer_commit("failed-check")
        self._submit(
            developer["dispatch_id"],
            self._developer_report(
                developer,
                candidate,
                changed,
                outcome="blocked",
                blockers="tests failed",
                checks_run=self._checks(developer, "fail"),
            ),
        )

        decided = self._decide(batch["batch_id"], "retry", reason_category="code")

        self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="code",
            candidate=None,
            route="developer-retry",
        )
        self.assertNotIn("candidate_registrations", decided)

    def test_early_blocked_developer_report_without_commits_accepted_and_retried_with_approval(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        snapshot = developer["snapshot_commit"]
        report = self._developer_report(
            developer,
            snapshot,
            [],
            outcome="blocked",
            output="stopped early because risk trigger requires gate",
            blockers="missing mandatory risk review gate",
            checks_run=self._checks(developer, "not_run"),
            commit_map=[],
        )
        submitted = self._submit(developer["dispatch_id"], report)
        self.assertEqual(submitted["state"], "reported")
        self.assertIn("decision_packet", submitted)
        packet = submitted["decision_packet"]
        self.assertEqual(packet["options"], ["retry", "block", "abandon"])
        self.assertNotIn("accept", packet["options"])
        self.assertIn("recovery_route", packet)
        self.assertEqual(
            packet["recovery_route"]["options"], ["retry", "block", "abandon"]
        )

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._decide(batch["batch_id"], "accept")
        self.assertIn("non-completed", caught.exception.message)

        decided = self._decide(
            batch["batch_id"], "retry", reason_category="requirements"
        )
        self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="requirements",
            candidate=None,
            route="developer-retry",
        )
        self.assertNotIn("candidate_registrations", decided)

    def test_completed_write_role_report_without_changes_rejected(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        snapshot = developer["snapshot_commit"]
        report = self._developer_report(
            developer,
            snapshot,
            [],
            outcome="completed",
            output="falsely claiming completion",
            checks_run=self._checks(developer, "pass"),
        )
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._submit(developer["dispatch_id"], report)
        self.assertIn("require commit_sha and changed_files", caught.exception.message)

    def test_early_blocked_developer_report_with_mismatched_commit_rejected(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        wrong_commit = "f" * 40
        report = self._developer_report(
            developer,
            wrong_commit,
            [],
            outcome="blocked",
            output="wrong checkout commit reported",
            blockers="some blocker",
            checks_run=self._checks(developer, "not_run"),
            commit_map=[],
        )
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._submit(developer["dispatch_id"], report)
        self.assertIn("commit_sha", caught.exception.message)

    def test_infrastructure_blocked_review_retries_a_new_review_on_the_same_candidate(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        first = self._infra_review(batch["batch_id"], candidate)
        evidence_files = [
            self._records() / "dispatches" / f"{first['dispatch_id']}.json",
            self._records() / "reports" / f"{first['dispatch_id']}.json",
            self._records() / "reports" / f"{first['dispatch_id']}.md",
        ]
        prior_evidence = [path.read_bytes() for path in evidence_files]
        freshness_checks = len(
            self._batch_record(batch["batch_id"])["context_package_freshness_checks"]
        )

        decided = self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )

        routing = self._assert_route(
            decided,
            role="code-review",
            action="code-review",
            category="verification-infrastructure",
            candidate=candidate,
            route="same-candidate-rerun",
        )
        self.assertEqual(routing["previous_role"], "code-review")
        self.assertFalse(decided.get("retry_candidate_required"))
        self.assertEqual(decided["state"], "awaiting-approval")
        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(batch["batch_id"], "developer")
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.create_dispatch(
                self._args(
                    batch=batch["batch_id"],
                    role="code-review",
                    runtime="claude",
                    purpose="work",
                    candidate_commit=candidate,
                    delta_review_of=None,
                    model="sonnet",
                    effort="high",
                )
            )  # manual_all: every dispatch needs its own fresh human approval

        second = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)

        self.assertNotEqual(second["dispatch_id"], first["dispatch_id"])
        self.assertEqual(second["brief"]["candidate_commit"], candidate)
        self.assertEqual(
            second["brief"]["risk_assessment_id"], first["risk_assessment_id"]
        )
        self.assertEqual(second["context_package_freshness"]["status"], "fresh")
        self.assertGreater(
            len(
                self._batch_record(batch["batch_id"])[
                    "context_package_freshness_checks"
                ]
            ),
            freshness_checks,
            "the new review must re-check Context Package freshness",
        )
        self.assertEqual([path.read_bytes() for path in evidence_files], prior_evidence)

    def test_infrastructure_retry_is_not_blocked_by_upstream_drift(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_review(batch["batch_id"], candidate)
        self._decide(batch["batch_id"], "retry", reason_category="transport")
        (self.repo / "later.txt").write_text("integration moved\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "later")
        _git(self.repo, "push", "origin", "master")

        dispatch = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)

        self.assertEqual(dispatch["brief"]["candidate_commit"], candidate)
        record = self._batch_record(batch["batch_id"])
        self.assertFalse(record.get("base_rebase_required"))
        self.assertNotIn("rebase_target_commit", record)

    def test_review_code_findings_route_to_developer_retry(self) -> None:
        warning = {"severity": "warning", "summary": "off-by-one", "evidence": "x.py:3"}
        blocker = {"severity": "blocker", "summary": "data loss", "evidence": "x.py:9"}
        info = {"severity": "info", "summary": "naming", "evidence": "x.py:1"}
        cases: dict[
            str, tuple[dict[str, tuple[str, list[JsonObject]]], str | None]
        ] = {  # the full matrix lives in CoordinatorRetryRoutingTableTests; these prove the wiring
            "standards blocker finding": (
                {"standards": ("blocker", [blocker])},
                "verification-infrastructure",
            ),
            "spec warning finding": ({"spec": ("warning", [warning])}, "transport"),
            "spec warning severity alone": ({"spec": ("warning", [])}, "transport"),
            "an info finding still counts as a finding": (
                {"spec": ("clean", [info])},
                None,
            ),
        }
        for name, (axes, category) in cases.items():
            with self.subTest(case=name, explicit=category):
                self._reset()
                batch = self._create_batch()
                self._accepted_architect(batch["batch_id"])
                candidate = self._accepted_candidate(batch["batch_id"])
                self._reported_review(
                    batch["batch_id"],
                    candidate,
                    outcome="blocked",
                    blockers="reviewer stopped",
                    **axes,
                )

                decided = self._decide(
                    batch["batch_id"], "retry", reason_category=category
                )

                routing = self._routing(decided)
                self.assertEqual(
                    (routing["next_role"], routing["next_action"]),
                    ("developer", "developer-retry"),
                )
                self.assertIn(routing["reason_category"], {"code", "requirements"})
                self.assertTrue(decided["retry_candidate_required"])

    def test_unknown_reason_cannot_bypass_developer_retry(self) -> None:
        for explicit in (None, "unknown"):
            with self.subTest(explicit=explicit):
                self._reset()
                batch = self._create_batch()
                self._accepted_architect(batch["batch_id"])
                candidate = self._accepted_candidate(batch["batch_id"])
                self._infra_review(batch["batch_id"], candidate)

                decided = self._decide(
                    batch["batch_id"], "retry", reason_category=explicit
                )

                routing = self._assert_route(
                    decided,
                    role="developer",
                    action="developer-retry",
                    category="unknown",
                    candidate=candidate,
                    route="developer-retry",
                )
                self.assertEqual(routing["previous_role"], "code-review")
                self.assertTrue(decided["retry_candidate_required"])

    def test_infrastructure_category_is_ignored_when_the_report_is_not_blocked(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._reported_review(
            batch["batch_id"], candidate
        )  # completed and clean: nothing was blocked

        decided = self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )

        routing = self._routing(decided)
        self.assertEqual(
            (routing["reason_category"], routing["next_action"]),
            ("unknown", "developer-retry"),
        )

    def test_changed_candidate_cannot_reuse_prior_review_evidence(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        first_candidate = self._accepted_candidate(batch["batch_id"], "a")
        blocker = {"severity": "blocker", "summary": "wrong", "evidence": "a.py:1"}
        first_review = self._reported_review(
            batch["batch_id"],
            first_candidate,
            outcome="blocked",
            blockers="fix needed",
            spec=("blocker", [blocker]),
        )
        self._decide(batch["batch_id"], "retry")
        retry = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(retry["dispatch_id"])
        second_candidate, changed = self._developer_commit("b")
        self._submit(
            retry["dispatch_id"],
            self._developer_report(retry, second_candidate, changed),
        )
        self._decide(batch["batch_id"], "accept")

        self.assertEqual(
            self._batch_record(batch["batch_id"])["next_action"], "risk-assessment"
        )
        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(
                batch["batch_id"], "code-review", candidate=second_candidate
            )  # no risk assessment yet
        self._assess(batch["batch_id"], second_candidate, changed)
        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(
                batch["batch_id"], "qa", candidate=second_candidate
            )  # first candidate's review does not count

        second = self._dispatch(
            batch["batch_id"], "code-review", candidate=second_candidate
        )["brief"]

        self.assertEqual(second["candidate_commit"], second_candidate)
        self.assertNotEqual(
            second["risk_assessment_id"], first_review["risk_assessment_id"]
        )
        self.assertNotEqual(second["dispatch_id"], first_review["dispatch_id"])

    def test_routing_rejects_infrastructure_reuse_when_the_candidate_moved(
        self,
    ) -> None:
        report = {
            "outcome": "blocked",
            "checks_run": [],
            "review": {
                "standards": {"severity": "none", "findings": []},
                "spec": {"severity": "none", "findings": []},
            },
        }

        routing = decisions._retry_routing(
            "code-review",
            report,
            dispatch_candidate="a" * 40,
            current_candidate="b" * 40,
            explicit_category="verification-infrastructure",
        )

        self.assertEqual(
            (
                routing["reason_category"],
                routing["next_role"],
                routing["next_action"],
                routing["candidate_commit"],
            ),
            ("candidate-change", "developer", "developer-retry", None),
        )

    # -- QA and publish ------------------------------------------------------------------------

    def test_qa_infrastructure_blocker_retries_a_new_qa_on_the_same_candidate(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._reported_review(batch["batch_id"], candidate)
        self._decide(batch["batch_id"], "accept")
        first = self._reported_qa(
            batch["batch_id"], candidate, outcome="blocked", check_result="not-run"
        )

        decided = self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )

        self._assert_route(
            decided,
            role="qa",
            action="qa",
            category="verification-infrastructure",
            candidate=candidate,
            route="same-candidate-rerun",
        )
        second = self._dispatch(batch["batch_id"], "qa", candidate=candidate)
        self.assertNotEqual(second["dispatch_id"], first["dispatch_id"])
        self.assertEqual(second["brief"]["candidate_commit"], candidate)

    def test_qa_and_publish_briefs_pin_the_operation_plan_and_ignore_role_overrides(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._reported_review(batch["batch_id"], candidate)
        self._decide(batch["batch_id"], "accept")
        (self.repo / ".harness/orchestration.json").write_text(
            json.dumps(
                {
                    "access_policy": {
                        "defaults": {"mode": "inherit"},
                        "roles": {
                            "qa": {"mode": "sandbox"},
                            "developer": {"mode": "unsandboxed"},
                        },
                        "operations": {
                            "publish": {"network": {"hosts": ["github.com"]}}
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        qa = self._reported_qa(batch["batch_id"], candidate)
        self._decide(batch["batch_id"], "accept")
        publish = self._dispatch(
            batch["batch_id"], "developer", purpose="publish", candidate=candidate
        )["brief"]
        for brief, hosts in ((qa, []), (publish, ["github.com"])):
            plan = brief["runtime_access"]
            self.assertEqual(plan["mode"], "inherit")
            self.assertEqual(plan["sources"]["mode"], "defaults")
            self.assertEqual(plan["network"], {"hosts": hosts})
            writes = {
                item["resource"]
                for item in plan["requirements"]
                if item["access"] == "write"
            }
            self.assertTrue({"git_common", "shared_storage"} <= writes)
            self.assertEqual(
                brief["transition"]["runtime_access_sha256"], plan["plan_digest"]
            )
        self.assertEqual(qa["access"], "read-only")

    def _publish_brief_under(
        self, access_policy: JsonObject | None
    ) -> tuple[str, JsonObject, str]:
        """An accepted QA candidate and an approved publish brief pinned under ``access_policy``."""
        batch = self._create_batch()
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)
        candidate = self._accepted_candidate(batch_id)
        self._accepted_review_and_qa(batch_id, candidate)
        if access_policy is not None:
            (self.repo / ".harness/orchestration.json").write_text(
                json.dumps({"access_policy": access_policy}), encoding="utf-8"
            )
        brief = self._dispatch(
            batch_id, "developer", purpose="publish", candidate=candidate
        )["brief"]
        return batch_id, brief, candidate

    def _assert_nothing_published(
        self, brief: JsonObject, remote: str, candidate: str
    ) -> None:
        self.assertEqual(
            coordinator._load_dispatch_status(
                ledger_ops._state_root(self._args(), self.repo),
                brief["dispatch_id"],
            )["state"],
            "approved",
        )
        self.assertNotIn(
            candidate,
            subprocess.run(
                ["git", "-C", str(self.repo), "ls-remote", "--heads", remote],
                capture_output=True,
                text=True,
                check=False,
            ).stdout,
        )

    def test_publish_verifies_the_pinned_plan_and_reports_the_evidence(self) -> None:
        _, brief, candidate = self._publish_brief_under(
            {"defaults": {"mode": "inherit"}}
        )

        published = coordinator.publish_dispatch(
            self._args(dispatch=brief["dispatch_id"], remote="origin")
        )

        self.assertEqual(published["candidate_commit"], candidate)
        self.assertEqual(published["access"]["status"], "verified")
        self.assertEqual(
            published["access"]["plan_digest"], brief["runtime_access"]["plan_digest"]
        )

    def test_publish_stops_before_the_push_when_the_remote_host_is_not_approved(
        self,
    ) -> None:
        _, brief, candidate = self._publish_brief_under(
            {"defaults": {"mode": "inherit", "network": {"hosts": ["github.com"]}}}
        )
        _git(self.repo, "remote", "add", "mirror", "git@example.invalid:o/r.git")

        with self.assertRaises(operation_access.OperationAccessError) as refused:
            coordinator.publish_dispatch(
                self._args(dispatch=brief["dispatch_id"], remote="mirror")
            )

        self.assertEqual(refused.exception.evidence["status"], "denied")
        self.assertIn("example.invalid", refused.exception.remedy)
        self._assert_nothing_published(brief, "origin", candidate)

    def test_publish_stops_before_the_push_when_the_remote_is_unreachable(self) -> None:
        _, brief, candidate = self._publish_brief_under(
            {"defaults": {"mode": "inherit"}}
        )
        _git(self.repo, "remote", "add", "gone", str(self.tmp / "gone.git"))

        with self.assertRaises(operation_access.OperationAccessError) as refused:
            coordinator.publish_dispatch(
                self._args(dispatch=brief["dispatch_id"], remote="gone")
            )

        self.assertEqual(refused.exception.evidence["status"], "unverified")
        self._assert_nothing_published(brief, "origin", candidate)

    def test_publish_uses_the_pinned_plan_not_a_later_config_edit(self) -> None:
        _, brief, candidate = self._publish_brief_under(
            {"defaults": {"mode": "inherit"}}
        )
        (self.repo / ".harness/orchestration.json").write_text(
            json.dumps({"access_policy": {"defaults": {"mode": "unsandboxed"}}}),
            encoding="utf-8",
        )

        published = coordinator.publish_dispatch(
            self._args(dispatch=brief["dispatch_id"], remote="origin")
        )

        self.assertEqual(published["access"]["mode"], "inherit")
        self.assertEqual(published["candidate_commit"], candidate)

    def test_publish_classifies_a_remote_that_refuses_the_push(self) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("a privileged process is not denied by file permissions")
        _, brief, candidate = self._publish_brief_under(None)
        origin = self.tmp / "origin.git"
        paths = [origin, *origin.rglob("*")]
        modes = {path: path.stat().st_mode & 0o7777 for path in paths}
        for path in paths:
            path.chmod(modes[path] & ~0o222)
        try:
            with self.assertRaises(git_utils.GitAccessError) as refused:
                coordinator.publish_dispatch(
                    self._args(dispatch=brief["dispatch_id"], remote="origin")
                )
        finally:
            for path in paths:
                path.chmod(modes[path])

        self.assertEqual(refused.exception.category, git_utils.REMOTE_DENIED)
        self._assert_nothing_published(brief, "origin", candidate)

    def test_qa_defect_routes_to_developer_retry_even_with_an_infrastructure_category(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._reported_review(batch["batch_id"], candidate)
        self._decide(batch["batch_id"], "accept")
        self._reported_qa(
            batch["batch_id"], candidate, outcome="failed", check_result="fail"
        )

        decided = self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )

        self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="code",
            candidate=candidate,
            route="developer-retry",
        )

    def test_publish_infrastructure_blocker_retries_a_new_publish_on_the_same_candidate(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._accepted_review_and_qa(batch["batch_id"], candidate)
        first = self._reported_publish(
            batch["batch_id"], candidate, "remote unreachable"
        )

        decided = self._decide(batch["batch_id"], "retry", reason_category="transport")

        routing = self._assert_route(
            decided,
            role="publish",
            action="publish",
            category="transport",
            candidate=candidate,
            route="same-candidate-rerun",
        )
        self.assertEqual(routing["previous_role"], "publish")
        self.assertFalse(decided.get("retry_candidate_required"))
        second = self._dispatch(
            batch["batch_id"], "developer", purpose="publish", candidate=candidate
        )
        self.assertNotEqual(second["dispatch_id"], first["dispatch_id"])
        self.assertEqual(second["brief"]["candidate_commit"], candidate)
        published = coordinator.publish_dispatch(
            self._args(dispatch=second["dispatch_id"], remote="origin")
        )
        self.assertEqual(published["candidate_commit"], candidate)
        self.assertEqual(
            self._decide(batch["batch_id"], "accept")["state"], "completed"
        )

    def test_publish_that_needs_a_new_candidate_routes_to_developer_retry(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._accepted_review_and_qa(batch["batch_id"], candidate)
        self._reported_publish(
            batch["batch_id"], candidate, "push rejected: candidate must be rebased"
        )

        decided = self._decide(
            batch["batch_id"], "retry", reason_category="candidate-change"
        )

        self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="candidate-change",
            candidate=candidate,
            route="developer-retry",
        )
        self.assertTrue(decided["retry_candidate_required"])

    # -- architect, developer and terminal decisions ------------------------------------------

    def test_architect_retry_starts_a_new_architect_and_never_a_developer(self) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        self._submit(
            brief["dispatch_id"],
            self._base_report(
                brief, "architect", outcome="blocked", blockers="unclear scope"
            ),
        )

        decided = self._decide(batch["batch_id"], "retry")

        self._assert_route(
            decided,
            role="architect",
            action="architect",
            category="unknown",
            candidate=None,
            route="architect-retry",
        )
        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(batch["batch_id"], "developer")
        self.assertTrue(
            decided["needs_attention"]
        )  # an unknown reason halts automatic dispatch (issue #250)
        self._resolve_attention(batch["batch_id"])
        self.assertNotEqual(
            self._dispatch(batch["batch_id"], "architect")["dispatch_id"],
            brief["dispatch_id"],
        )

    def test_developer_retry_after_review_needs_a_new_candidate_and_never_reuses_the_reviewed_one(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        blocker = {"severity": "blocker", "summary": "wrong", "evidence": "x.py:1"}
        self._reported_review(
            batch["batch_id"],
            candidate,
            outcome="blocked",
            blockers="fix needed",
            spec=("blocker", [blocker]),
        )

        decided = self._decide(batch["batch_id"], "retry")

        routing = self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="requirements",
            candidate=candidate,
            route="fix-forward",
        )
        self.assertEqual(routing["previous_role"], "code-review")
        retry = self._dispatch(batch["batch_id"], "developer")["brief"]
        self.assertEqual(retry["snapshot_commit"], candidate)
        self._start(retry["dispatch_id"])
        changed = git_utils._changed_files_between(
            self.repo, self._batch_record(batch["batch_id"])["base_commit"], candidate
        )
        with self.assertRaises(coordinator.CoordinatorError):
            self._submit(
                retry["dispatch_id"], self._developer_report(retry, candidate, changed)
            )  # no fictitious re-report

    def test_developer_retry_maps_only_new_fix_commits_to_plan_subset(self) -> None:
        plan = self._batch_plan()
        plan["definition_of_done"] = ["one", "two", "three"]
        with mock.patch.object(self, "_batch_plan", return_value=plan):
            batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        initial = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(initial["dispatch_id"])
        original_commits = [self._developer_commit(name)[0] for name in ("a", "b", "c")]
        candidate = original_commits[-1]
        base = self._batch_record(batch["batch_id"])["base_commit"]
        changed = git_utils._changed_files_between(self.repo, base, candidate)
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "each created commit"
        ):
            self._submit(
                initial["dispatch_id"],
                self._developer_report(
                    initial,
                    candidate,
                    changed,
                    commit_map=[
                        {"commit_sha": sha, "plan_entry_id": entry["id"]}
                        for sha, entry in zip(
                            original_commits[:2], initial["commit_plan"][:2]
                        )
                    ],
                ),
            )
        self._submit(
            initial["dispatch_id"],
            self._developer_report(
                initial,
                candidate,
                changed,
                commit_map=[
                    {"commit_sha": sha, "plan_entry_id": entry["id"]}
                    for sha, entry in zip(original_commits, initial["commit_plan"])
                ],
            ),
        )
        self._decide(batch["batch_id"], "accept")
        self._assess(batch["batch_id"], candidate, changed)
        self._reported_review(
            batch["batch_id"],
            candidate,
            outcome="blocked",
            blockers="fix needed",
            spec=(
                "blocker",
                [{"severity": "blocker", "summary": "wrong", "evidence": "x.py:1"}],
            ),
        )
        self._decide(batch["batch_id"], "retry")
        retry = self._dispatch(batch["batch_id"], "developer")["brief"]
        self.assertEqual(retry["transition"]["next_action"], "developer-retry")
        self._start(retry["dispatch_id"])
        fixes = [self._developer_commit(name)[0] for name in ("fix_a", "fix_b")]
        fixed = fixes[-1]
        fixed_files = git_utils._changed_files_between(self.repo, base, fixed)
        report = self._developer_report(
            retry,
            fixed,
            fixed_files,
            commit_map=[
                {
                    "commit_sha": fixes[0],
                    "plan_entry_id": retry["commit_plan"][0]["id"],
                },
                {
                    "commit_sha": fixes[1],
                    "plan_entry_id": retry["commit_plan"][1]["id"],
                },
            ],
        )
        self._submit(retry["dispatch_id"], report)

    def test_developer_report_rejects_a_missing_commit_map(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(brief["dispatch_id"])
        candidate, changed = self._developer_commit("commit-map")

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "requires commit_map"
        ):
            self._submit(
                brief["dispatch_id"],
                self._developer_report(brief, candidate, changed, commit_map=[]),
            )

    def test_developer_stage_retry_routes_to_a_developer_retry_with_no_reusable_candidate(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(brief["dispatch_id"])
        candidate, changed = self._developer_commit("x")
        self._submit(
            brief["dispatch_id"], self._developer_report(brief, candidate, changed)
        )

        decided = self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )

        routing = self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="unknown",
            candidate=None,
            route="developer-retry",
        )
        self.assertEqual(routing["previous_role"], "developer")
        self.assertTrue(decided["retry_candidate_required"])

    # -- tooling (issue #500) -----------------------------------------------------------------

    def test_a_tooling_blocker_is_validated_and_allowed_only_on_a_blocked_report(
        self,
    ) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        too_long = {**TOOLING_BLOCKER, "message": "x" * 1_601}
        for outcome, invalid in (
            ("completed", dict(TOOLING_BLOCKER)),
            ("failed", dict(TOOLING_BLOCKER)),
            ("blocked", None),
            ("blocked", "a hook blocked it"),
            ("blocked", {"tool": "hook", "command": "true"}),
            ("blocked", {**TOOLING_BLOCKER, "exit_code": "2"}),
            ("blocked", {**TOOLING_BLOCKER, "command": "  "}),
            ("blocked", {**TOOLING_BLOCKER, "tool": 7}),
            ("blocked", too_long),
            ("blocked", {**TOOLING_BLOCKER, "uncommitted_files": ["a.py"]}),
        ):
            with self.subTest(outcome=outcome, invalid=invalid):
                report = self._base_report(
                    brief, "architect", outcome=outcome, tooling_blocker=invalid
                )
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "tooling_blocker"
                ):
                    self._submit(brief["dispatch_id"], report)
        interrupted = {
            "tool": "safety-classifier",
            "command": "git push --force-with-lease origin feature/x",
            "message": "The action was interrupted by the safety classifier",
        }
        submitted = self._submit(
            brief["dispatch_id"],
            self._base_report(
                brief, "architect", outcome="blocked", tooling_blocker=interrupted
            ),
        )
        self.assertEqual(submitted["state"], "reported")
        stored = json.loads(Path(submitted["report"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["tooling_blocker"], interrupted)
        markdown = (
            Path(submitted["report"]).with_suffix(".md").read_text(encoding="utf-8")
        )
        self.assertIn("Tooling blocker: safety-classifier", markdown)

        decided = self._decide(batch["batch_id"], "retry")

        self._assert_route(
            decided,
            role="architect",
            action="architect",
            category="tooling",
            candidate=None,
            route="tooling-retry",
        )
        self.assertNotEqual(
            self._dispatch(batch["batch_id"], "architect")["dispatch_id"],
            brief["dispatch_id"],
        )

    def test_a_tooling_blocked_developer_restarts_from_its_last_commit(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        candidate, changed = self._developer_commit("tooled")
        self._submit(
            developer["dispatch_id"],
            self._developer_report(
                developer,
                candidate,
                changed,
                outcome="blocked",
                blockers="a hook blocked a legitimate check command",
                checks_run=self._checks(developer, "not-run"),
                tooling_blocker=dict(TOOLING_BLOCKER),
            ),
        )

        decided = self._decide(batch["batch_id"], "retry")

        self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="tooling",
            candidate=candidate,
            route="tooling-retry",
        )
        self.assertNotIn("candidate_registrations", decided)
        retry = self._dispatch(batch["batch_id"], "developer")["brief"]
        self.assertNotEqual(retry["dispatch_id"], developer["dispatch_id"])
        self.assertEqual(retry["snapshot_commit"], candidate)
        self.assertEqual(retry["transition"]["reason_category"], "tooling")
        self.assertEqual(retry["transition"]["next_action"], "developer-retry")

    def test_a_blocked_commit_lists_its_uncommitted_files_for_the_restart(
        self,
    ) -> None:
        # Issue #502: a hook blocked the developer's git commit; nothing is reverted, the report
        # lists the uncommitted files and the restart inherits exactly them.
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        candidate, changed = self._developer_commit("tooled")
        (self.worktree / "services" / "wip.py").write_text(
            "WIP = 1\n", encoding="utf-8"
        )
        blocker = {
            **TOOLING_BLOCKER,
            "command": "git commit -m 'feat: wip'",
            "uncommitted_files": ["services/wip.py"],
        }

        def report(tooling_blocker: object) -> JsonObject:
            return self._developer_report(
                developer,
                candidate,
                changed,
                outcome="blocked",
                blockers="a hook blocked git commit",
                checks_run=self._checks(developer, "not-run"),
                tooling_blocker=tooling_blocker,
            )

        for invalid in (
            "services/wip.py",
            [],
            ["services/wip.py", "services/wip.py"],
            [" "],
            [7],
            ["/services/wip.py"],
            ["services/../wip.py"],
            ["./services/wip.py"],
            ["services//wip.py"],
            ["services\\wip.py"],
        ):
            with self.subTest(uncommitted_files=invalid):
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "uncommitted_files"
                ):
                    self._submit(
                        developer["dispatch_id"],
                        report({**blocker, "uncommitted_files": invalid}),
                    )
        submitted = self._submit(developer["dispatch_id"], report(blocker))
        markdown = (
            Path(submitted["report"]).with_suffix(".md").read_text(encoding="utf-8")
        )
        self.assertIn("Uncommitted files: services/wip.py", markdown)
        decided = self._decide(batch["batch_id"], "retry")
        self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="tooling",
            candidate=candidate,
            route="tooling-retry",
        )

        start = self._developer_preflight(batch["batch_id"])["retry_start"]

        self.assertEqual(
            start["handoff"]["developer_report"]["tooling_blocker"],
            blocker,
        )
        (self.worktree / "services" / "extra.py").write_text(
            "X = 1\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            "not listed in the blocked report: services/extra.py",
        ):
            self._developer_preflight(batch["batch_id"])

    def test_a_tooling_blocked_review_retries_a_new_review_on_the_same_candidate(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        first = self._tooling_review(batch["batch_id"], candidate)

        decided = self._decide(batch["batch_id"], "retry")

        self._assert_route(
            decided,
            role="code-review",
            action="code-review",
            category="tooling",
            candidate=candidate,
            route="tooling-retry",
        )
        self.assertFalse(decided.get("retry_candidate_required"))
        self.assertFalse(decided.get("needs_attention", False))
        second = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)
        self.assertNotEqual(second["dispatch_id"], first["dispatch_id"])
        self.assertEqual(second["brief"]["candidate_commit"], candidate)

    def test_a_tooling_blocked_verification_reruns_on_the_registered_candidate(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        candidate, changed = self._developer_commit("infrastructure")
        self._submit(
            developer["dispatch_id"],
            self._developer_report(
                developer,
                candidate,
                changed,
                outcome="blocked",
                blockers="verification environment unavailable",
            ),
        )
        self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )
        verification = self._dispatch(
            batch["batch_id"], "verification", candidate=candidate
        )["brief"]
        self._start(verification["dispatch_id"], checkout=self.worktree)
        self._submit(
            verification["dispatch_id"],
            self._base_report(
                verification,
                "verification",
                outcome="blocked",
                blockers="a hook blocked a legitimate check command",
                checks_run=self._checks(verification, "not-run"),
                tooling_blocker=dict(TOOLING_BLOCKER),
            ),
        )

        decided = self._decide(batch["batch_id"], "retry")

        routing = self._routing(decided)
        self.assertEqual(
            (
                routing["next_role"],
                routing["next_action"],
                routing["reason_category"],
                routing["route"],
                routing["candidate_commit"],
            ),
            ("verification", "verification", "tooling", "tooling-retry", candidate),
        )
        self.assertEqual(
            len(decided["candidate_registrations"]),
            1,
            "a tooling re-run registers no new candidate",
        )
        again = self._dispatch(batch["batch_id"], "verification", candidate=candidate)
        self.assertNotEqual(again["dispatch_id"], verification["dispatch_id"])
        self.assertEqual(again["brief"]["candidate_commit"], candidate)

    def test_a_review_that_worked_around_a_block_reruns_without_a_new_candidate(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        finding = [{"severity": "warning", "summary": "s", "evidence": "e"}]
        first = self._reported_review(
            batch["batch_id"], candidate, standards=("warning", finding)
        )

        with self.assertRaises(coordinator.CoordinatorError) as unnoted:
            self._decide(batch["batch_id"], "retry", reason_category="block-bypass")
        self.assertIn("--note", unnoted.exception.remedy)
        decided = self._decide(
            batch["batch_id"],
            "retry",
            reason_category="block-bypass",
            note="the review ran the blocked check through a script file",
        )

        self._assert_route(
            decided,
            role="code-review",
            action="code-review",
            category="block-bypass",
            candidate=candidate,
            route="bypass-rerun",
        )
        self.assertEqual(
            decided["dispatches"][-1]["decision"]["decision"],
            "retry",
            "the bypassing report is neither accepted nor overridden",
        )
        self.assertEqual(decisions._developer_retry_count(decided), 0)
        second = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)
        self.assertNotEqual(second["dispatch_id"], first["dispatch_id"])
        self.assertEqual(second["brief"]["candidate_commit"], candidate)
        self.assertEqual(
            second["brief"]["transition"]["reason_category"], "block-bypass"
        )

    def test_a_verification_that_worked_around_a_block_reruns_on_the_registered_candidate(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        candidate, changed = self._developer_commit("infrastructure")
        self._submit(
            developer["dispatch_id"],
            self._developer_report(
                developer,
                candidate,
                changed,
                outcome="blocked",
                blockers="verification environment unavailable",
            ),
        )
        self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )
        verification = self._dispatch(
            batch["batch_id"], "verification", candidate=candidate
        )["brief"]
        self._start(verification["dispatch_id"], checkout=self.worktree)
        self._submit(
            verification["dispatch_id"], self._base_report(verification, "verification")
        )

        decided = self._decide(
            batch["batch_id"],
            "retry",
            reason_category="block-bypass",
            note="verification split a blocked command",
        )

        routing = self._routing(decided)
        self.assertEqual(
            (routing["next_action"], routing["route"], routing["candidate_commit"]),
            ("verification", "bypass-rerun", candidate),
        )
        self.assertEqual(len(decided["candidate_registrations"]), 1)
        again = self._dispatch(batch["batch_id"], "verification", candidate=candidate)
        self.assertNotEqual(again["dispatch_id"], verification["dispatch_id"])

    def test_a_bypass_rerun_dispatch_always_needs_an_explicit_approval(self) -> None:
        args = _ns(approved_by=None, approved_at=None)
        for policy in ("milestone", "low_risk"):
            for route, expected in (
                ("tooling-retry", f"policy:{policy}"),
                ("bypass-rerun", None),
            ):
                batch: JsonObject = {
                    "approval_policy": policy,
                    "allowed_paths": ["**"],
                    "dispatches": [
                        {
                            "dispatch_id": "dispatch-1",
                            "role": "code-review",
                            "decision": {
                                "decision": "retry",
                                "routing": {"route": route},
                            },
                        }
                    ],
                }
                config_ = {"low_risk_paths": ["**"]}
                with self.subTest(policy=policy, route=route):
                    if expected is not None:
                        self.assertEqual(
                            dispatch._dispatch_approval_mode(
                                args, batch, config_, "code-review", "work", None
                            ),
                            expected,
                        )
                        continue
                    with self.assertRaises(coordinator.CoordinatorError) as raised:
                        dispatch._dispatch_approval_mode(
                            args, batch, config_, "code-review", "work", None
                        )
                    self.assertIn("--approved-by", raised.exception.remedy)

    def _developer_preflight(self, batch_id: str) -> JsonObject:
        planned = config._config
        with (
            mock.patch.object(dispatch, "_configured", return_value=True),
            mock.patch.object(
                config,
                "_config",
                lambda repo: {
                    **planned(repo),
                    "assignment_plans": {"developer": {"runtimes": {"claude": {}}}},
                },
            ),
        ):
            return coordinator.preflight_dispatch(
                self._args(
                    batch=batch_id,
                    role="developer",
                    purpose="work",
                    runtime="claude",
                    candidate_commit=None,
                )
            )

    def test_developer_retry_preflight_starts_from_a_checkpoint_shaped_handoff(
        self,
    ) -> None:
        # Issue #524: the retry starts from the developer report, the retry decision with its
        # findings, the commit plan and the Context Package ID -- not the earlier session's history.
        batch_id = self._create_batch()["batch_id"]
        self._accepted_architect(batch_id)
        candidate = self._accepted_candidate(batch_id)
        developer = self._batch_record(batch_id)["dispatches"][-1]
        blocker = {"severity": "blocker", "summary": "wrong", "evidence": "a.py:1"}
        review = self._reported_review(
            batch_id,
            candidate,
            outcome="blocked",
            blockers="fix needed",
            spec=("blocker", [blocker]),
        )
        self._decide(batch_id, "retry")

        start = self._developer_preflight(batch_id)["retry_start"]

        handoff = start["handoff"]
        self.assertEqual(
            (
                handoff["developer_report"]["dispatch_id"],
                handoff["developer_report"]["commit_sha"],
            ),
            (developer["dispatch_id"], candidate),
        )
        self.assertEqual(
            handoff["commit_plan"],
            coordinator._read_object(
                self._records() / "dispatches" / f"{developer['dispatch_id']}.json",
                "dispatch",
            )["commit_plan"],
        )
        self.assertEqual(
            handoff["context_package_id"],
            self._batch_record(batch_id)["context_packages"][-1]["context_package_id"],
        )
        decision = handoff["retry_decision"]
        self.assertEqual(
            (decision["dispatch_id"], decision["route"]),
            (review["dispatch_id"], "fix-forward"),
        )
        self.assertEqual(decision["findings"], [{"axis": "spec", **blocker}])
        self.assertFalse(start["context_estimate"]["compacted"])
        self.assertIsNone(start["warning"])

    def test_developer_retry_of_an_unaccepted_report_continues_its_candidate_end_to_end(
        self,
    ) -> None:
        # Issue #477, the #443 batch: a clean developer report retried for a code defect without
        # an accept must hand its candidate to the next developer instead of rewinding to base.
        plan = self._batch_plan()
        plan["definition_of_done"] = ["one", "two", "three"]
        with mock.patch.object(self, "_batch_plan", return_value=plan):
            batch = self._create_batch()
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)
        returned, commits, _ = self._retried_developer_candidate(
            batch_id, "a", "b", "c"
        )
        candidate = commits[-1]
        self._patch_config(worker_attestation_required=True)
        prepared = self._developer_preflight(batch_id)
        self.assertEqual(prepared["preview_brief"]["snapshot_commit"], candidate)
        self.assertEqual(
            prepared["retry_start"]["handoff"]["developer_report"]["dispatch_id"],
            returned["dispatch_id"],
        )

        created = self._dispatch(batch_id, "developer")

        retry = created["brief"]
        self.assertEqual(retry["snapshot_commit"], candidate)
        self.assertIsNone(retry["candidate_commit"])
        self.assertEqual(retry["transition"]["next_action"], "developer-retry")
        self.assertEqual(created["context_package_freshness"]["status"], "fresh")
        coordinator.send_dispatch(
            self._args(
                dispatch=retry["dispatch_id"],
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )
        attested = coordinator.self_report_dispatch(
            self._args(
                dispatch=retry["dispatch_id"],
                model="sonnet",
                worktree=str(self.worktree),
            )
        )
        self.assertEqual(attested["state"], "working")
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), candidate)
        fix, fixed_files = self._developer_commit("d")
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "each created commit"
        ):
            self._submit(
                retry["dispatch_id"],
                self._developer_report(
                    retry,
                    fix,
                    fixed_files,
                    commit_map=[
                        {"commit_sha": sha, "plan_entry_id": entry["id"]}
                        for sha, entry in zip(commits, retry["commit_plan"])
                    ],
                ),
            )
        self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                fix,
                fixed_files,
                commit_map=[
                    {"commit_sha": fix, "plan_entry_id": retry["commit_plan"][0]["id"]}
                ],
            ),
        )
        self._decide(batch_id, "accept")
        self._assess(batch_id, fix, fixed_files)
        self.assertEqual(self._batch_record(batch_id)["next_action"], "code-review")

    def test_risk_assessment_registers_the_retry_pinned_candidate_without_unlocking_review(
        self,
    ) -> None:
        batch = self._create_batch()
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)
        _, (candidate,), changed = self._retried_developer_candidate(batch_id, "x")
        # Same diff, different commit: only the retried report's own candidate may be assessed.
        other = _git(
            self.repo,
            "commit-tree",
            f"{candidate}^{{tree}}",
            "-p",
            candidate,
            "-m",
            "y",
        )
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._assess(batch_id, other, changed)
        self.assertEqual(
            caught.exception.message,
            "candidate commit does not match the developer report the pending retry continues",
        )
        self.assertIn(f"--candidate-commit {candidate}", caught.exception.remedy)

        self._assess(batch_id, candidate, changed)

        record = self._batch_record(batch_id)
        self.assertEqual(
            (
                record["next_action"],
                record["required_next_role"],
                record["retry_candidate_required"],
            ),
            ("developer-retry", "developer", True),
        )
        self.assertEqual(
            [item["candidate_commit"] for item in record["risk_assessments"]],
            [candidate],
        )
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "coordinator-prepared next action"
        ):
            self._dispatch(batch_id, "code-review", candidate=candidate)
        retry = self._dispatch(batch_id, "developer", candidate=candidate)["brief"]
        self.assertEqual(retry["snapshot_commit"], candidate)
        self.assertEqual(
            retry["risk_assessment_id"],
            record["risk_assessments"][0]["risk_assessment_id"],
        )
        self._start(retry["dispatch_id"])
        with self.assertRaises(coordinator.CoordinatorError):
            self._submit(
                retry["dispatch_id"], self._developer_report(retry, candidate, changed)
            )  # the retry must produce a new candidate
        fix, fixed_files = self._developer_commit("fix")
        self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                fix,
                fixed_files,
                commit_map=[
                    {"commit_sha": fix, "plan_entry_id": retry["commit_plan"][0]["id"]}
                ],
            ),
        )

    def test_a_second_developer_retry_pins_the_newest_unaccepted_candidate_over_the_accepted_one(
        self,
    ) -> None:
        self._patch_config(retry_policy={"max_developer_retries": 2})
        batch = self._create_batch()
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)
        accepted = self._accepted_candidate(batch_id, "a")
        self._reported_review(
            batch_id,
            accepted,
            outcome="blocked",
            blockers="fix needed",
            spec=(
                "blocker",
                [{"severity": "blocker", "summary": "wrong", "evidence": "x.py:1"}],
            ),
        )
        self._decide(batch_id, "retry")
        first, (retried,), _ = self._retried_developer_candidate(batch_id, "b")
        self.assertEqual(first["snapshot_commit"], accepted)

        second = self._dispatch(batch_id, "developer")

        self.assertEqual(second["brief"]["snapshot_commit"], retried)
        self.assertEqual(second["context_package_freshness"]["status"], "fresh")
        self._start(second["brief"]["dispatch_id"])

    def test_developer_retry_still_refuses_a_candidate_without_risk_assessment(
        self,
    ) -> None:
        batch = self._create_batch()
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)

        def assert_refused(candidate: str) -> None:
            before = self._batch_record(batch_id)
            written = {
                kind: sorted(path.name for path in (self._records() / kind).iterdir())
                for kind in ("dispatches", "dispatch-status")
            }
            with self.assertRaises(coordinator.CoordinatorError) as caught:
                self._dispatch(batch_id, "developer", candidate=candidate)
            self.assertEqual(
                caught.exception.message,
                "dispatch candidate is not linked to its immutable risk assessment",
            )
            self.assertIn(
                f"risk assess --batch {batch_id} --candidate-commit {candidate}",
                caught.exception.remedy,
            )
            after = self._batch_record(batch_id)
            self.assertEqual(after["dispatches"], before["dispatches"])
            self.assertEqual(after["state"], "awaiting-approval")
            self.assertEqual(
                {
                    kind: sorted(
                        path.name for path in (self._records() / kind).iterdir()
                    )
                    for kind in written
                },
                written,
            )

        _, (candidate,), changed = self._retried_developer_candidate(batch_id, "x")
        with self.subTest("a developer retry pinned without its risk assessment"):
            assert_refused(candidate)

        self._assess(batch_id, candidate, changed)
        retry = self._dispatch(batch_id, "developer", candidate=candidate)["brief"]

        self.assertIsNotNone(retry["risk_assessment_id"])
        self._start(retry["dispatch_id"])

    def test_block_and_fail_never_create_a_dispatch_automatically(self) -> None:
        for decision, state in (("block", "blocked"), ("fail", "failed")):
            with self.subTest(decision=decision):
                self._reset()
                batch = self._create_batch()
                self._accepted_architect(batch["batch_id"])
                candidate = self._accepted_candidate(batch["batch_id"])
                self._infra_review(batch["batch_id"], candidate)
                before = self._dispatch_ids(batch["batch_id"])

                decided = self._decide(
                    batch["batch_id"],
                    decision,
                    reason_category="verification-infrastructure",
                )

                self.assertEqual(decided["state"], state)
                self.assertNotIn(
                    "abandoned", decided
                )  # abandon is only ever an explicit, reasoned decision
                self.assertNotIn("next_action", decided)
                self.assertNotIn("required_next_role", decided)
                self.assertNotIn("routing", decided["coordinator_decisions"][-1])
                self.assertEqual(self._dispatch_ids(batch["batch_id"]), before)
                with self.assertRaises(coordinator.CoordinatorError):
                    self._dispatch(
                        batch["batch_id"], "code-review", candidate=candidate
                    )

    def test_developer_retry_budget_counts_only_developer_retries(self) -> None:
        legacy = {"decision": "retry", "next_role": "developer"}
        same_candidate = {"decision": "retry", "next_role": "code-review"}
        publish = {"decision": "retry", "next_role": "publish"}

        count = decisions._developer_retry_count(
            {"coordinator_decisions": [legacy, same_candidate, publish]}
        )

        self.assertEqual(count, 1)

    def test_a_tooling_retry_spends_no_developer_retry(self) -> None:
        tooling = {
            "decision": "retry",
            "next_role": "developer",
            "routing": {"route": "tooling-retry"},
        }
        spent = {
            "decision": "retry",
            "next_role": "developer",
            "routing": {"route": "developer-retry"},
        }

        count = decisions._developer_retry_count(
            {"coordinator_decisions": [tooling, spent, tooling]}
        )

        self.assertEqual(count, 1)

    def test_an_exhausted_budget_never_refuses_a_developer_tooling_retry(
        self,
    ) -> None:
        for blocker, expected in (
            (dict(TOOLING_BLOCKER), "tooling-retry"),
            (None, None),
        ):
            with self.subTest(route=expected):
                self._reset()
                batch = self._create_batch()
                self._accepted_architect(batch["batch_id"])
                developer = self._dispatch(batch["batch_id"], "developer")["brief"]
                self._start(developer["dispatch_id"])
                candidate, changed = self._developer_commit("tooled")
                extra = {} if blocker is None else {"tooling_blocker": blocker}
                self._submit(
                    developer["dispatch_id"],
                    self._developer_report(
                        developer,
                        candidate,
                        changed,
                        outcome="blocked",
                        blockers="a hook blocked a legitimate check command",
                        checks_run=self._checks(developer, "not-run"),
                        **extra,
                    ),
                )
                budget = {"max_developer_retries": 0}
                with mock.patch.object(decisions, "_retry_policy", return_value=budget):
                    if expected is None:
                        with self.assertRaisesRegex(
                            coordinator.CoordinatorError, "budget is exhausted"
                        ):
                            self._decide(batch["batch_id"], "retry")
                        continue
                    decided = self._decide(batch["batch_id"], "retry")
                self.assertEqual(self._routing(decided)["route"], expected)
                self.assertEqual(decided["next_action"], "developer-retry")
                self.assertEqual(decisions._developer_retry_count(decided), 0)

    def test_review_blocker_can_be_blocked_only_once_the_developer_retry_budget_is_exhausted(
        self,
    ) -> None:
        blocker = {"severity": "blocker", "summary": "data loss", "evidence": "x.py:9"}
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                self._reset()
                batch = self._create_batch()
                self._accepted_architect(batch["batch_id"])
                candidate = self._accepted_candidate(batch["batch_id"])
                self._reported_review(
                    batch["batch_id"], candidate, standards=("blocker", [blocker])
                )
                budget = {"max_developer_retries": 0 if exhausted else 1}
                with mock.patch.object(decisions, "_retry_policy", return_value=budget):
                    refused = "retry" if exhausted else "block"
                    with self.assertRaises(coordinator.CoordinatorError) as caught:
                        self._decide(batch["batch_id"], refused)
                    self.assertIn("abandon", caught.exception.remedy)
                    self.assertEqual(
                        self._batch_record(batch["batch_id"])["state"],
                        "awaiting-approval",
                    )
                    if exhausted:
                        self.assertIn("block", caught.exception.remedy)
                        decided = self._decide(batch["batch_id"], "block")
                        self.assertEqual(decided["state"], "blocked")

    # -- abandon -------------------------------------------------------------------------------

    def test_abandon_requires_explicit_approval_and_a_reason(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_review(batch["batch_id"], candidate)

        for label, extra in (
            ("no reason", {}),
            ("blank reason", {"reason": "   "}),
            ("no approver", {"reason": "superseded", "approved_by": ""}),
            ("no approval time", {"reason": "superseded", "approved_at": None}),
        ):
            with self.subTest(label):
                with self.assertRaises(coordinator.CoordinatorError):
                    self._decide(batch["batch_id"], "abandon", **extra)
                self.assertEqual(
                    self._batch_record(batch["batch_id"])["state"], "awaiting-approval"
                )

    def test_abandon_is_terminal_keeps_all_evidence_and_allows_a_fresh_batch(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        review = self._infra_review(batch["batch_id"], candidate)
        immutable_before = {
            path: path.read_bytes()
            for path in self._records().rglob("*")
            if path.is_file() and path.parent.name not in {"audit", "batches"}
        }
        staged = workspace._agent_inbox(self.repo) / f"{review['dispatch_id']}.json"
        self.assertTrue(staged.is_file())

        decided = self._decide(
            batch["batch_id"], "abandon", reason="superseded by a fresh plan"
        )

        self.assertEqual(decided["state"], "abandoned")
        self.assertNotIn("next_action", decided)
        self.assertNotIn("required_next_role", decided)
        entry = decided["coordinator_decisions"][-1]
        self.assertEqual(
            (entry["decision"], entry["note"]),
            ("abandon", "superseded by a fresh plan"),
        )
        self.assertEqual(entry["approved_by"], "Malove")
        routing = entry["routing"]
        self.assertEqual(
            {key: value for key, value in routing.items() if key != "rationale"},
            {
                "route": "abandon",
                "previous_role": "code-review",
                "reason_category": None,
                "next_role": None,
                "next_action": None,
                "candidate_commit": None,
                "decided_at": routing["decided_at"],
            },
        )
        self.assertTrue(routing["decided_at"])
        self.assertNotIn("superseded", routing["rationale"])
        self.assertEqual(decided["dispatches"][-1]["decision"]["routing"], routing)
        self.assertEqual(decided["abandoned"]["reason"], "superseded by a fresh plan")
        self.assertEqual(
            decided["abandoned"]["last_accepted"]["candidate_commit"], candidate
        )
        for path, content in immutable_before.items():
            self.assertEqual(path.read_bytes(), content, path.name)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), candidate)
        self.assertTrue(
            (self._records() / "reports" / f"{review['dispatch_id']}.json").is_file()
        )
        self.assertFalse(
            staged.exists(),
            "the staged copy is cleaned up; the immutable report is evidence and stays",
        )
        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(batch["batch_id"], "code-review", candidate=candidate)
        self.assertEqual(
            coordinator.list_batches(self._args(ticket="#244", state=None, open=True))[
                "batches"
            ],
            [],
        )
        fresh = coordinator.create_batch(self._args(**self._batch_plan()))
        self.assertNotEqual(fresh["batch_id"], batch["batch_id"])
        self.assertEqual(fresh["state"], "planned")

    # -- supersede (issue #506) ------------------------------------------------------------------

    def _abandoned_batch(self, *, review: bool = True) -> tuple[str, str]:
        """A batch abandoned after a dead end: architect and developer accepted, a code-review
        that cannot be decided otherwise. Returns the batch and its last accepted candidate."""
        batch_id = cast(str, self._create_batch()["batch_id"])
        self._accepted_architect(batch_id)
        candidate = self._accepted_candidate(batch_id)
        if review:
            self._infra_review(batch_id, candidate)
        self._decide(batch_id, "abandon", reason="review dead end")
        return batch_id, candidate

    def _superseding_plan(self, source: str | None, **overrides: object) -> JsonObject:
        return {
            **self._batch_plan(),
            "supersedes": source,
            **self._approval(),
            **overrides,
        }

    def _supersede(self, source: str, **overrides: object) -> JsonObject:
        batch = coordinator.create_batch(
            self._args(**self._superseding_plan(source, **overrides))
        )
        self.batch_id = batch["batch_id"]
        return batch

    def _ledger_bytes(self) -> dict[Path, bytes]:
        return {
            path: path.read_bytes()
            for path in self._records().rglob("*")
            if path.is_file() and path.parent.name != "audit"
        }

    def test_batch_create_supersedes_requires_a_human_approval_and_writes_nothing(
        self,
    ) -> None:
        source, _ = self._abandoned_batch()
        before = self._ledger_bytes()
        for label, plan, message in (
            (
                "no approval",
                self._superseding_plan(source, approved_by=None, approved_at=None),
                "requires a human approval",
            ),
            (
                "half an approval",
                self._superseding_plan(source, approved_at=None),
                "requires a human approval",
            ),
            (
                "a policy approver",
                self._superseding_plan(source, approved_by="policy:auto"),
                "never by a policy",
            ),
            (
                "approval flags without --supersedes",
                self._superseding_plan(None),
                "approve only --supersedes",
            ),
        ):
            with self.subTest(label):
                with self.assertRaises(coordinator.CoordinatorError) as refused:
                    coordinator.create_batch(self._args(**plan))
                self.assertIn(message, refused.exception.message)
                self.assertTrue(refused.exception.remedy)
                self.assertEqual(self._ledger_bytes(), before)

    def test_batch_create_supersedes_refuses_a_source_it_cannot_resume(self) -> None:
        failed = cast(str, self._create_batch()["batch_id"])
        coordinator.abandon_batch(
            self._args(batch=failed, reason="worker died", **self._approval())
        )
        unaccepted = cast(
            str, coordinator.create_batch(self._args(**self._batch_plan()))["batch_id"]
        )
        coordinator.approve_batch(self._args(batch=unaccepted, **self._approval()))
        self._reported_architect(unaccepted)
        self._decide(unaccepted, "abandon", reason="architect dead end")
        source = cast(
            str, coordinator.create_batch(self._args(**self._batch_plan()))["batch_id"]
        )
        coordinator.approve_batch(self._args(batch=source, **self._approval()))
        self.batch_id = source
        self._accepted_architect(source)
        self._decide_abandon_after_candidate(source)
        before = self._ledger_bytes()
        for label, plan, message, remedy in (
            (
                "a batch closed with batch abandon",
                self._superseding_plan(failed),
                "'failed', not abandoned",
                "batch decide",
            ),
            (
                "nothing accepted",
                self._superseding_plan(unaccepted),
                "no abandoned.last_accepted record",
                "ordinary batch",
            ),
            (
                "another ticket",
                self._superseding_plan(source, ticket="#245"),
                "belongs to ticket '#244'",
                "--ticket #244",
            ),
            (
                "another issue branch",
                self._superseding_plan(source, branch="feature/issue-244-other"),
                f"belongs to branch {self.branch!r}",
                f"--branch {self.branch}",
            ),
            (
                "a missing batch",
                self._superseding_plan(f"batch-{uuid.uuid4()}"),
                "does not exist",
                "batch list",
            ),
        ):
            with self.subTest(label):
                with self.assertRaises(coordinator.CoordinatorError) as refused:
                    coordinator.create_batch(self._args(**plan))
                self.assertIn(message, refused.exception.message)
                self.assertIn(remedy, refused.exception.remedy)
                self.assertEqual(self._ledger_bytes(), before)

    def _decide_abandon_after_candidate(self, batch_id: str) -> str:
        candidate = self._accepted_candidate(batch_id)
        self._infra_review(batch_id, candidate)
        self._decide(batch_id, "abandon", reason="review dead end")
        return candidate

    def _abandon_after_a_blocked_developer(self, batch_id: str) -> JsonObject:
        """Abandon ``batch_id`` after its next developer stops before any commit: nothing of
        that developer is accepted. Returns the blocked developer brief."""
        brief: JsonObject = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                brief["snapshot_commit"],
                [],
                commit_map=[],
                outcome="blocked",
                blockers="the developer cannot continue",
            ),
        )
        self._decide(batch_id, "abandon", reason="developer dead end")
        return brief

    def test_a_superseding_source_without_an_accept_names_the_batch_it_superseded(
        self,
    ) -> None:
        first, candidate = self._abandoned_batch()
        second = cast(str, self._supersede(first)["batch_id"])
        coordinator.approve_batch(self._args(batch=second, **self._approval()))
        self._abandon_after_a_blocked_developer(second)
        self.assertIsNone(self._batch_record(second)["abandoned"]["last_accepted"])
        before = self._ledger_bytes()

        with self.assertRaises(coordinator.CoordinatorError) as refused:
            coordinator.create_batch(self._args(**self._superseding_plan(second)))

        self.assertIn("no abandoned.last_accepted record", refused.exception.message)
        self.assertIn(f"--supersedes {first}", refused.exception.remedy)
        self.assertIn("ordinary batch", refused.exception.remedy)
        self.assertEqual(self._ledger_bytes(), before)
        again = self._supersede(first)
        self.assertEqual(
            (again["supersedes"]["batch_id"], again["supersedes"]["start_commit"]),
            (first, candidate),
        )

    def test_a_superseding_batch_links_its_abandoned_batch_and_records_the_route(
        self,
    ) -> None:
        source, candidate = self._abandoned_batch()
        source_bytes = (self._records() / "batches" / f"{source}.json").read_bytes()
        abandoned = self._batch_record(source)["abandoned"]

        batch = self._supersede(source)

        link = batch["supersedes"]
        self.assertEqual(
            (
                link["batch_id"],
                link["approved_by"],
                link["approved_at"],
                link["last_accepted"],
                link["definition_of_done_matches"],
            ),
            (
                source,
                "Malove",
                self.APPROVED_AT,
                abandoned["last_accepted"],
                True,
            ),
        )
        self.assertEqual(abandoned["last_accepted"]["candidate_commit"], candidate)
        plan = coordinator._read_object(
            self._records() / "plans" / f"{batch['batch_id']}.json", "plan"
        )
        self.assertEqual(plan["supersedes"], link)
        (decision,) = batch["coordinator_decisions"]
        self.assertEqual(
            (
                decision["decision"],
                decision["approved_by"],
                decision["routing"]["route"],
            ),
            ("supersede", "Malove", "supersede"),
        )
        self.assertEqual(decision["routing"]["superseded_batch_id"], source)
        (audit,) = self._decision_audits(batch["batch_id"])
        self.assertEqual(
            (audit["decision"], audit["route"], audit["approver"]),
            ("supersede", "supersede", {"kind": "human", "name": "Malove"}),
        )
        self.assertEqual(audit["evidence"]["batch_id"], source)
        self.assertEqual(audit["evidence"]["last_accepted"], abandoned["last_accepted"])
        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(
            (stored["supersedes"], stored["coordinator_decisions"]),
            (link, batch["coordinator_decisions"]),
        )
        self.assertEqual(
            (stored["state"], stored["dispatches"], stored["risk_assessments"]),
            ("planned", [], []),
        )
        for field in ("carried_items", "candidate_registrations", "abandoned"):
            self.assertNotIn(field, stored)
        self.assertEqual(
            (self._records() / "batches" / f"{source}.json").read_bytes(), source_bytes
        )
        root = ledger_ops._state_root(self._args(), self.repo)
        tampered = {**stored, "supersedes": {**link, "batch_id": "batch-other"}}
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "supersedes link does not match"
        ):
            history._validate_batch_integrity(root, tampered)
        approved = coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )
        self.assertEqual(approved["supersedes"], link)

    def _abandoned_with_pinned_plan(self) -> tuple[str, str, list[JsonObject]]:
        """An abandoned batch whose architect accept pinned a commit plan (#478)."""
        batch_id = cast(str, self._create_batch()["batch_id"])
        self._reported_architect(batch_id)
        entries = [self._plan_entry("route-retries", [1])]
        self._decide(batch_id, "accept", commit_plan_file=self._plan_file(entries))
        candidate = self._accepted_candidate(batch_id)
        self._infra_review(batch_id, candidate)
        self._decide(batch_id, "abandon", reason="review dead end")
        return batch_id, candidate, entries

    def test_the_same_definition_of_done_carries_the_architect_and_its_pinned_plan(
        self,
    ) -> None:
        source, _, entries = self._abandoned_with_pinned_plan()
        architect = next(
            item
            for item in self._batch_record(source)["dispatches"]
            if item["role"] == "architect"
        )
        source_bytes = (self._records() / "batches" / f"{source}.json").read_bytes()

        batch = self._supersede(source)
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )

        digest = commit_plan.plan_sha256(entries)
        reference = {
            "batch_id": source,
            "dispatch_id": architect["dispatch_id"],
            "report": architect["report"],
            "report_sha256": architect["report_sha256"],
            "commit_plan_sha256": digest,
        }
        self.assertEqual(batch["supersedes"]["architect"], reference)
        self.assertEqual(
            (batch["commit_plan"], batch["next_action"]), (entries, "developer")
        )
        routing = batch["coordinator_decisions"][0]["routing"]
        self.assertEqual(
            (routing["next_role"], routing["next_action"]), ("developer", "developer")
        )
        (audit,) = self._decision_audits(batch["batch_id"])
        self.assertEqual(audit["evidence"]["architect"], reference)
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "coordinator-prepared next action"
        ):
            self._propose(batch["batch_id"], "architect")
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self.assertEqual(brief["commit_plan"], entries)
        stored = self._batch_record(batch["batch_id"])
        self.assertTrue(history._accepted_architect(stored))
        self.assertEqual(commit_plan.accepted_plan_sha256(stored), digest)
        self.assertEqual(
            [item["role"] for item in stored["dispatches"]],
            ["developer"],
            "no architect dispatch runs in the superseding batch",
        )
        self.assertEqual(
            (self._records() / "batches" / f"{source}.json").read_bytes(), source_bytes
        )

    def test_an_accepted_architect_alone_is_carried_and_the_developer_starts_at_the_base(
        self,
    ) -> None:
        source = cast(str, self._create_batch()["batch_id"])
        self._reported_architect(source)
        entries = [self._plan_entry("route-retries", [1])]
        self._decide(source, "accept", commit_plan_file=self._plan_file(entries))
        self._abandon_after_a_blocked_developer(source)
        architect = next(
            item
            for item in self._batch_record(source)["dispatches"]
            if item["role"] == "architect"
        )
        self.assertEqual(
            self._batch_record(source)["abandoned"]["last_accepted"],
            {
                "dispatch_id": architect["dispatch_id"],
                "role": "architect",
                "candidate_commit": None,
            },
        )

        batch = self._supersede(source)
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )

        link = batch["supersedes"]
        self.assertEqual(
            (
                link["architect"]["dispatch_id"],
                link["architect"]["commit_plan_sha256"],
                link["start_commit"],
                link["rebase_target_commit"],
            ),
            (architect["dispatch_id"], commit_plan.plan_sha256(entries), None, None),
        )
        self.assertEqual(
            (batch["commit_plan"], batch["next_action"]), (entries, "developer")
        )
        routing = batch["coordinator_decisions"][0]["routing"]
        self.assertEqual(
            (routing["next_role"], routing["candidate_commit"]), ("developer", None)
        )
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(
            (
                brief["snapshot_commit"],
                brief["rebase_target_commit"],
                brief["transition"]["next_action"],
                brief["commit_plan"],
            ),
            (stored["base_commit"], None, "developer", entries),
        )
        self.assertEqual([item["role"] for item in stored["dispatches"]], ["developer"])

    def test_another_definition_of_done_carries_nothing_and_needs_a_new_architect(
        self,
    ) -> None:
        source, _, _ = self._abandoned_with_pinned_plan()
        batch = self._supersede(
            source,
            definition_of_done=["route retries by cause", "log the chosen route"],
        )
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )

        link = batch["supersedes"]
        self.assertEqual(
            (link["definition_of_done_matches"], link["architect"]), (False, None)
        )
        for field in ("commit_plan", "next_action"):
            self.assertNotIn(field, batch)
        routing = batch["coordinator_decisions"][0]["routing"]
        self.assertEqual(
            (routing["next_role"], routing["next_action"]), ("architect", None)
        )
        stored = self._batch_record(batch["batch_id"])
        self.assertFalse(history._accepted_architect(stored))
        self.assertIsNone(commit_plan.accepted_plan_sha256(stored))
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "requires an accepted architect report"
        ):
            self._propose(batch["batch_id"], "developer")
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self.assertEqual(brief["role"], "architect")

    def test_a_carried_architect_reference_is_handed_on_by_a_superseding_source(
        self,
    ) -> None:
        reference = {
            "batch_id": "batch-first",
            "dispatch_id": "dispatch-architect",
            "report": "reports/dispatch-architect.json",
            "report_sha256": "0" * 64,
            "commit_plan_sha256": None,
        }
        root = ledger_ops._state_root(self._args(), self.repo)
        source: JsonObject = {
            "batch_id": "batch-second",
            "dispatches": [],
            "supersedes": {"architect": reference},
        }

        carried = supersede._architect_reference(root, source)

        self.assertEqual(carried, reference)
        self.assertIsNone(
            supersede._architect_reference(root, {**source, "supersedes": None})
        )

    def _superseding_initial_report(
        self, brief: JsonObject, candidate: str, fix: str, changed: list[str]
    ) -> JsonObject:
        """The initial report of a superseding batch's first developer: the map covers every
        commit after the integration base, the start commit's too (startup recovery)."""
        entry = brief["commit_plan"][0]["id"]
        return self._developer_report(
            brief,
            fix,
            changed,
            commit_map=[
                {"commit_sha": candidate, "plan_entry_id": entry},
                {"commit_sha": fix, "plan_entry_id": entry},
            ],
            dod_coverage=[{"dod_item": 1, "commits": [candidate, fix]}],
            divergence_justification="the start commit and the fix both close the one entry",
        )

    def test_a_start_commit_on_the_integration_base_starts_an_initial_developer(
        self,
    ) -> None:
        source, candidate = self._abandoned_batch()

        batch = self._supersede(source)
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )

        link = batch["supersedes"]
        self.assertEqual(
            (link["start_commit"], link["rebase_target_commit"], batch["next_action"]),
            (candidate, None, "developer"),
        )
        routing = batch["coordinator_decisions"][0]["routing"]
        self.assertEqual(
            (routing["candidate_commit"], routing["rebase_target_commit"]),
            (candidate, None),
        )
        preflight = self._developer_preflight(batch["batch_id"])
        self.assertEqual(
            (preflight["candidate_sha"], preflight["preview_brief"]["snapshot_commit"]),
            (candidate, candidate),
        )
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self.assertEqual(
            (
                brief["snapshot_commit"],
                brief["candidate_commit"],
                brief["rebase_target_commit"],
                brief["transition"]["next_action"],
            ),
            (candidate, None, None, "developer"),
        )
        freshness = history._context_package_freshness(
            self.repo,
            ledger_ops._state_root(self._args(), self.repo),
            self._batch_record(batch["batch_id"]),
            context_package_id=brief["context_package_id"],
        )
        self.assertEqual((freshness or {}).get("current_candidate_commit"), candidate)
        self.assertEqual((freshness or {}).get("status"), "fresh")
        self._start(brief["dispatch_id"])
        fix, changed = self._developer_commit("fix")
        self._submit(
            brief["dispatch_id"],
            self._superseding_initial_report(brief, candidate, fix, changed),
        )
        accepted = self._decide(batch["batch_id"], "accept")

        self.assertEqual(accepted["next_action"], "risk-assessment")
        self.assertTrue(git_utils._git_is_ancestor(self.repo, candidate, fix))

    def test_a_start_commit_off_the_moved_base_takes_the_in_retry_rebase_route(
        self,
    ) -> None:
        source, candidate = self._abandoned_batch()
        old_base = self._batch_record(source)["integration_base_commit"]
        upstream = self._push_upstream()

        batch = self._supersede(source)
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )

        link = batch["supersedes"]
        self.assertEqual(
            (
                batch["integration_base_commit"],
                link["start_commit"],
                link["rebase_target_commit"],
                batch["next_action"],
            ),
            (upstream, candidate, upstream, "developer-retry"),
        )
        proposal = self._propose(batch["batch_id"], "developer")
        self.assertEqual(proposal["transition"]["rebase_target_sha"], upstream)
        with self.assertRaises(coordinator.CoordinatorError) as policy:
            coordinator.create_dispatch(
                self._args(
                    transition_digest=None,
                    **self._proposal_fields(
                        batch["batch_id"], "developer", "work", None
                    ),
                    approved_by=None,
                    approved_at=None,
                )
            )
        self.assertIn("--approved-by", policy.exception.remedy)
        retry = self._dispatch(
            batch["batch_id"], "developer", digest=proposal["transition_digest"]
        )["brief"]
        self.assertEqual(
            (
                retry["snapshot_commit"],
                retry["rebase_target_commit"],
                retry["transition"]["next_action"],
                retry["carried_items"],
            ),
            (candidate, upstream, "developer-retry", {}),
        )
        self._start(retry["dispatch_id"])
        originals = _git(
            self.worktree, "rev-list", "--reverse", f"{old_base}..{candidate}"
        ).splitlines()
        copies = self._rebase_onto(candidate, upstream)
        fix, _ = self._developer_commit("fix")
        changed = git_utils._changed_files_between(self.repo, upstream, fix)
        submitted = self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                fix,
                changed,
                commit_map=[
                    *self._rebased_map(originals, copies),
                    {"commit_sha": fix, "plan_entry_id": retry["commit_plan"][0]["id"]},
                ],
            ),
        )
        check = submitted["rebase_check"]
        self.assertEqual(
            (
                check["rebase_target_commit"],
                check["previous_base_commit"],
                [pair["patch_id_match"] for pair in check["rebased"]],
            ),
            (upstream, old_base, [True] * len(originals)),
        )
        accepted = self._decide(batch["batch_id"], "accept")
        self.assertEqual(
            (accepted["integration_base_commit"], accepted["next_action"]),
            (upstream, "risk-assessment"),
        )
        self.assertEqual(decisions._developer_retry_count(accepted), 0)
        self._assess(batch["batch_id"], fix, changed)
        review = self._dispatch(batch["batch_id"], "code-review", candidate=fix)[
            "brief"
        ]
        self.assertEqual(
            (review["candidate_commit"], review["delta_review_scope"]), (fix, None)
        )

    def test_a_superseding_rebase_target_always_needs_an_explicit_approval(
        self,
    ) -> None:
        args = _ns(approved_by=None, approved_at=None)
        batch: JsonObject = {"allowed_paths": ["**"], "dispatches": []}
        for policy in ("milestone", "low_risk", "auto"):
            with self.subTest(policy=policy):
                settings = {**batch, "approval_policy": policy}
                config_ = {"low_risk_paths": ["**"]}
                self.assertEqual(
                    dispatch._dispatch_approval_mode(
                        args, settings, config_, "developer", "work", None, None
                    ),
                    f"policy:{policy}",
                )
                with self.assertRaises(coordinator.CoordinatorError) as raised:
                    dispatch._dispatch_approval_mode(
                        args, settings, config_, "developer", "work", None, "a" * 40
                    )
                self.assertIn("--approved-by", raised.exception.remedy)

    def test_a_retry_before_the_rebase_keeps_the_superseding_rebase_target(
        self,
    ) -> None:
        source, candidate = self._abandoned_batch()
        upstream = self._push_upstream()
        batch_id = cast(str, self._supersede(source)["batch_id"])
        coordinator.approve_batch(self._args(batch=batch_id, **self._approval()))
        first = self._dispatch(batch_id, "developer")["brief"]
        self._start(first["dispatch_id"])
        self._submit(
            first["dispatch_id"],
            self._developer_report(
                first,
                candidate,
                [],
                commit_map=[],
                outcome="blocked",
                blockers="the rebase needs a decision on a conflict",
            ),
        )
        self._decide(batch_id, "retry", reason_category="code")

        second = self._dispatch(batch_id, "developer")["brief"]

        self.assertEqual(
            (
                second["snapshot_commit"],
                second["rebase_target_commit"],
                second["transition"]["next_action"],
            ),
            (candidate, upstream, "developer-retry"),
        )
        self.assertEqual(
            supersede.developer_next_action(self._batch_record(batch_id)), "developer"
        )

    def test_an_architect_accept_in_a_superseding_batch_routes_to_the_rebase(
        self,
    ) -> None:
        source, candidate = self._abandoned_batch()
        upstream = self._push_upstream()
        batch_id = cast(
            str,
            self._supersede(
                source,
                definition_of_done=["route retries by cause", "log the chosen route"],
            )["batch_id"],
        )
        coordinator.approve_batch(self._args(batch=batch_id, **self._approval()))
        architect = self._dispatch(batch_id, "architect")["brief"]
        self.assertEqual(architect["snapshot_commit"], candidate)
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )

        accepted = self._decide(batch_id, "accept")

        self.assertEqual(accepted["next_action"], "developer-retry")
        retry = self._dispatch(batch_id, "developer")["brief"]
        self.assertEqual(
            (retry["snapshot_commit"], retry["rebase_target_commit"]),
            (candidate, upstream),
        )
        self.assertEqual(
            [entry["id"] for entry in retry["commit_plan"]],
            [
                entry["id"]
                for entry in commit_plan.default_plan(
                    accepted["definition_of_done"], ["**"]
                )
            ],
        )

    def test_a_superseding_batch_superseded_again_keeps_the_first_architect(
        self,
    ) -> None:
        first, candidate, entries = self._abandoned_with_pinned_plan()
        reference = self._supersede(first)["supersedes"]["architect"]
        second = self.batch_id
        coordinator.approve_batch(self._args(batch=second, **self._approval()))
        brief = self._dispatch(second, "developer")["brief"]
        self._start(brief["dispatch_id"])
        fix, changed = self._developer_commit("second")
        self._submit(
            brief["dispatch_id"],
            self._superseding_initial_report(brief, candidate, fix, changed),
        )
        self._decide(second, "accept")
        self._assess(second, fix, changed)
        self._infra_review(second, fix)
        self._decide(second, "abandon", reason="review dead end again")

        third = self._supersede(second)

        self.assertEqual(third["supersedes"]["architect"], reference)
        self.assertEqual(reference["batch_id"], first)
        self.assertEqual(
            (third["supersedes"]["start_commit"], third["commit_plan"]),
            (fix, entries),
        )
        self.assertEqual(third["next_action"], "developer")

    def test_a_superseding_batch_with_only_its_architect_accepted_hands_on_its_start_commit(
        self,
    ) -> None:
        first, candidate = self._abandoned_batch()
        other = ["route retries by cause", "log the chosen route"]
        second = cast(str, self._supersede(first, definition_of_done=other)["batch_id"])
        coordinator.approve_batch(self._args(batch=second, **self._approval()))
        self._accepted_architect(second)
        blocked = self._abandon_after_a_blocked_developer(second)
        self.assertEqual(blocked["snapshot_commit"], candidate)
        architect = next(
            item
            for item in self._batch_record(second)["dispatches"]
            if item["role"] == "architect"
        )
        self.assertEqual(
            self._batch_record(second)["abandoned"]["last_accepted"],
            {
                "dispatch_id": architect["dispatch_id"],
                "role": "architect",
                "candidate_commit": candidate,
            },
        )

        third = self._supersede(second, definition_of_done=other)
        coordinator.approve_batch(
            self._args(batch=third["batch_id"], **self._approval())
        )

        link = third["supersedes"]
        self.assertEqual(
            (
                link["start_commit"],
                link["rebase_target_commit"],
                link["architect"]["batch_id"],
                third["next_action"],
            ),
            (candidate, None, second, "developer"),
        )
        brief = self._dispatch(third["batch_id"], "developer")["brief"]
        self.assertEqual(
            (brief["snapshot_commit"], brief["transition"]["next_action"]),
            (candidate, "developer"),
        )

    def test_issue_443_an_abandoned_dead_end_resumes_in_a_superseding_batch(
        self,
    ) -> None:
        """Regression for #443 (issue #506): a review dead end forced an abandon, and the work
        restarted with a new architect and commits cherry-picked by hand. A superseding batch
        resumes from abandoned.last_accepted: no architect dispatch, no cherry-pick, and risk
        assessment, code-review and QA run again on the new candidate."""
        blocker = {
            "severity": "blocker",
            "summary": "data loss",
            "evidence": "services/x.py:1",
        }
        source = cast(str, self._create_batch()["batch_id"])
        self._reported_architect(source)
        entries = [self._plan_entry("route-retries", [1])]
        self._decide(source, "accept", commit_plan_file=self._plan_file(entries))
        candidate = self._accepted_candidate(source)
        self._reported_review(source, candidate, standards=("blocker", [blocker]))
        exhausted = {"max_developer_retries": 0}
        with mock.patch.object(decisions, "_retry_policy", return_value=exhausted):
            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "budget is exhausted"
            ):
                self._decide(source, "retry")
            self._decide(source, "abandon", reason="review blocker, no retry left")
        abandoned = self._batch_record(source)
        owned = {
            source,
            *(item["dispatch_id"] for item in abandoned["dispatches"]),
            *(item["risk_assessment_id"] for item in abandoned["risk_assessments"]),
        }
        source_files = {
            path: path.read_bytes()
            for path in self._records().rglob("*")
            if path.is_file() and any(owned_id in path.name for owned_id in owned)
        }
        audit_files = {
            path: path.read_bytes() for path in (self._records() / "audit").glob("*")
        }

        batch_id = cast(str, self._supersede(source)["batch_id"])
        coordinator.approve_batch(self._args(batch=batch_id, **self._approval()))
        developer = self._dispatch(batch_id, "developer")["brief"]
        self._start(developer["dispatch_id"])
        fix, changed = self._developer_commit("fix")
        self._submit(
            developer["dispatch_id"],
            self._superseding_initial_report(developer, candidate, fix, changed),
        )
        self._decide(batch_id, "accept")
        self._assess(batch_id, fix, changed)
        review = self._reported_review(batch_id, fix)
        self._decide(batch_id, "accept")
        self._reported_qa(batch_id, fix)
        resumed = self._decide(batch_id, "accept")

        self.assertEqual(
            (
                developer["snapshot_commit"],
                developer["worktree"],
                developer["commit_plan"],
            ),
            (candidate, str(self.worktree), entries),
        )
        self.assertEqual(
            [item["role"] for item in resumed["dispatches"]],
            ["developer", "code-review", "qa"],
            "no architect dispatch runs again",
        )
        self.assertEqual(resumed["next_action"], "publish")
        self.assertEqual(
            _git(self.worktree, "rev-list", f"{candidate}..HEAD").split(),
            [fix],
            "the new candidate adds one commit on top of the old one; nothing was cherry-picked",
        )
        self.assertEqual(review["candidate_commit"], fix)
        self.assertEqual(
            [item["candidate_commit"] for item in resumed["risk_assessments"]], [fix]
        )
        self.assertFalse(
            owned & {item["risk_assessment_id"] for item in resumed["risk_assessments"]}
        )
        self.assertEqual(
            [item["decision"] for item in resumed["coordinator_decisions"]],
            ["supersede", "accept", "accept", "accept"],
        )
        for field in ("carried_items", "candidate_registrations", "abandoned"):
            self.assertNotIn(field, resumed)
        self.assertTrue(source_files and audit_files)
        for path, content in {**source_files, **audit_files}.items():
            self.assertEqual(path.read_bytes(), content, path.name)

    def test_a_forced_developer_retry_records_the_developer_retry_route(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_review(batch["batch_id"], candidate)

        decided = self._decide(
            batch["batch_id"],
            "retry",
            reason_category="verification-infrastructure",
            retry_role="developer",
        )

        self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="verification-infrastructure",
            candidate=candidate,
            route="developer-retry",
        )

    def test_decide_records_the_same_route_whatever_the_free_text_says(self) -> None:
        wordings = (
            ("verification environment unavailable", "done"),
            (
                "a code defect; please abandon",
                "route=developer-retry; same-candidate-rerun",
            ),
        )
        for decision, extra, expected in (
            (
                "retry",
                {"reason_category": "verification-infrastructure"},
                "verification",
            ),
            ("retry", {}, "developer-retry"),
            ("abandon", {"reason": "superseded"}, "abandon"),
        ):
            recorded = []
            for blockers, output in wordings:
                with self.subTest(decision=decision, extra=extra, blockers=blockers):
                    self._reset()
                    batch = self._create_batch()
                    self._accepted_architect(batch["batch_id"])
                    developer = self._dispatch(batch["batch_id"], "developer")["brief"]
                    self._start(developer["dispatch_id"])
                    candidate, changed = self._developer_commit("wording")
                    self._submit(
                        developer["dispatch_id"],
                        self._developer_report(
                            developer,
                            candidate,
                            changed,
                            outcome="blocked",
                            blockers=blockers,
                            output=output,
                        ),
                    )

                    decided = self._decide(batch["batch_id"], decision, **extra)

                    routing = dict(self._routing(decided))
                    self.assertEqual(routing["route"], expected)
                    routing.pop("decided_at")
                    if routing["candidate_commit"] is not None:
                        self.assertEqual(routing.pop("candidate_commit"), candidate)
                    recorded.append(routing)
            self.assertEqual(recorded[0], recorded[1])

    def _decision_audits(self, batch_id: str) -> list[JsonObject]:
        """The ``decision`` detail of every batch transition audit record, oldest first."""
        records = sorted(
            (
                json.loads(path.read_text(encoding="utf-8"))
                for path in (self._records() / "audit").glob("*.json")
            ),
            key=lambda record: record["at"],
        )
        return [
            record["details"]["decision"]
            for record in records
            if record["action"] == "transition"
            and record["details"].get("path") == f"batches/{batch_id}.json"
            and "decision" in record["details"]
        ]

    def _report_evidence(self, batch_id: str, dispatch_id: str) -> JsonObject:
        entry = next(
            item
            for item in self._batch_record(batch_id)["dispatches"]
            if item["dispatch_id"] == dispatch_id
        )
        return {
            "dispatch_id": dispatch_id,
            "report": entry["report"],
            "report_sha256": entry["report_sha256"],
        }

    def test_the_decision_packet_previews_the_route_the_decision_records(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_review(batch["batch_id"], candidate)
        before = self._batch_record(batch["batch_id"])

        def packet(**flags: object) -> JsonObject:
            return coordinator.decision_packet(
                self._args(batch=batch["batch_id"], dispatch=None, **flags)
            )

        unflagged = packet()
        forced = packet(
            reason_category="verification-infrastructure", retry_role="developer"
        )
        preview = packet(reason_category="verification-infrastructure")

        self.assertEqual(unflagged["action"], "decide completion report")
        self.assertEqual(
            unflagged["route_preview"]["retry"]["route"], "developer-retry"
        )
        self.assertEqual(unflagged["route_preview"]["abandon"], {"route": "abandon"})
        self.assertEqual(forced["route_preview"]["retry"]["route"], "developer-retry")
        retry = preview["route_preview"]["retry"]
        self.assertEqual(retry["route"], "same-candidate-rerun")
        self.assertNotIn("decided_at", retry)
        self.assertEqual(
            self._batch_record(batch["batch_id"]), before, "a preview writes nothing"
        )

        decided = self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )

        recorded = dict(self._routing(decided))
        recorded.pop("decided_at")
        self.assertEqual(retry, recorded)
        self.assertIsNone(packet()["route_preview"])

    def test_the_decision_packet_previews_the_tooling_retry_route(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._tooling_review(batch["batch_id"], candidate)

        preview = coordinator.decision_packet(
            self._args(batch=batch["batch_id"], dispatch=None)
        )["route_preview"]["retry"]

        self.assertEqual(
            (preview["route"], preview["reason_category"], preview["next_action"]),
            ("tooling-retry", "tooling", "code-review"),
        )
        decided = self._decide(batch["batch_id"], "retry")
        recorded = dict(self._routing(decided))
        recorded.pop("decided_at")
        self.assertEqual(preview, recorded)

    def test_a_failing_classifier_still_renders_the_packet_and_refuses_the_retry(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_review(batch["batch_id"], candidate)
        failure = extensions.ExtensionError(
            "classifier failed", remedy="fix the classifier"
        )

        with mock.patch.object(
            extensions, "retry_reason_classifier", side_effect=failure
        ):
            packet = coordinator.decision_packet(
                self._args(batch=batch["batch_id"], dispatch=None)
            )
            with self.assertRaises(coordinator.CoordinatorError) as refused:
                self._decide(batch["batch_id"], "retry")

        self.assertEqual(packet["action"], "decide completion report")
        self.assertEqual(
            packet["route_preview"],
            {
                "retry": {
                    "route": None,
                    "refused": "classifier failed",
                    "remedy": "fix the classifier",
                },
                "abandon": {"route": "abandon"},
            },
        )
        self.assertEqual(refused.exception.message, "classifier failed")

    def test_a_routed_decision_audits_its_route_evidence_and_human_approver(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        review = self._infra_review(batch["batch_id"], candidate)

        self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )

        audits = self._decision_audits(batch["batch_id"])
        human = {"kind": "human", "name": "Malove"}
        self.assertEqual(
            [(item["decision"], item["route"]) for item in audits],
            [("accept", None), ("accept", None), ("retry", "same-candidate-rerun")],
        )
        self.assertEqual(
            audits[-1],
            {
                "dispatch_id": review["dispatch_id"],
                "decision": "retry",
                "route": "same-candidate-rerun",
                "evidence": self._report_evidence(
                    batch["batch_id"], review["dispatch_id"]
                ),
                "approver": human,
                "approved_at": self.APPROVED_AT,
            },
        )
        self.assertTrue(all(item["approver"] == human for item in audits))

    def test_an_abandon_decision_audits_the_abandon_route(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        review = self._infra_review(batch["batch_id"], candidate)
        evidence = self._report_evidence(batch["batch_id"], review["dispatch_id"])

        self._decide(batch["batch_id"], "abandon", reason="superseded")

        audit = self._decision_audits(batch["batch_id"])[-1]
        self.assertEqual(
            (audit["decision"], audit["route"], audit["evidence"], audit["approver"]),
            ("abandon", "abandon", evidence, {"kind": "human", "name": "Malove"}),
        )

    def test_a_policy_auto_accept_audits_a_policy_approver(self) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])

        self._submit(
            brief["dispatch_id"], self._base_report(brief, "architect", blockers="none")
        )

        audit = self._decision_audits(batch["batch_id"])[-1]
        self.assertEqual(
            audit,
            {
                "dispatch_id": brief["dispatch_id"],
                "decision": "accept",
                "route": None,
                "evidence": self._report_evidence(
                    batch["batch_id"], brief["dispatch_id"]
                ),
                "approver": {"kind": "policy", "name": "low_risk"},
                "approved_at": audit["approved_at"],
            },
        )
        self.assertTrue(audit["approved_at"])

    def test_a_computed_route_outside_the_enum_is_refused_before_anything_is_written(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_review(batch["batch_id"], candidate)
        before = self._batch_record(batch["batch_id"])
        real = decisions._retry_routing

        def unknown_route(*args: object, **kwargs: object) -> JsonObject:
            return {**real(*args, **kwargs), "route": "rebase"}  # type: ignore[arg-type]

        with mock.patch.object(decisions, "_retry_routing", unknown_route):
            with self.assertRaises(coordinator.CoordinatorError) as raised:
                self._decide(
                    batch["batch_id"],
                    "retry",
                    reason_category="verification-infrastructure",
                )

        self.assertIn("same-candidate-rerun", raised.exception.remedy)
        self.assertEqual(self._batch_record(batch["batch_id"]), before)

    def test_a_tampered_route_is_refused_when_the_batch_is_read_back(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_review(batch["batch_id"], candidate)
        self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )
        root = ledger_ops._state_root(self._args(), self.repo)
        ledger = LifecycleLedger(root)
        with ledger_ops._ledger_lock(ledger):
            record = ledger_ops._load_batch(root, batch["batch_id"])
            record["coordinator_decisions"][-1]["routing"]["route"] = "rebase"
            ledger_ops._replace_record(ledger, BatchRecord.from_dict(record))

        with self.assertRaises(coordinator.CoordinatorError) as raised:
            coordinator.decision_packet(
                self._args(batch=batch["batch_id"], dispatch=None)
            )

        self.assertIn("rebase", str(raised.exception))
        self.assertIn("same-candidate-rerun", raised.exception.remedy)

    def test_abandon_closes_unreported_dispatches_and_names_them(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        review = self._infra_review(batch["batch_id"], candidate)
        root = ledger_ops._state_root(self._args(), self.repo)
        ledger = LifecycleLedger(root)
        open_id = f"dispatch-{uuid.uuid4()}"
        with ledger_ops._ledger_lock(ledger):
            qa_lane._enqueue(ledger, open_id, coordinator)
            record = ledger_ops._load_batch(root, batch["batch_id"])
            record["dispatches"].append(
                {
                    "dispatch_id": open_id,
                    "role": "qa",
                    "state": "approved",
                    "brief_sha256": "0" * 64,
                }
            )
            ledger_ops._replace_record(ledger, BatchRecord.from_dict(record))
            ledger_ops._write_record(
                ledger,
                DispatchStatusRecord.from_dict(
                    {
                        "dispatch_id": open_id,
                        "state": "approved",
                        "updated_at": coordinator._now(),
                    },
                ),
            )

        decided = self._decide(batch["batch_id"], "abandon", reason="operator restart")

        states = {item["dispatch_id"]: item["state"] for item in decided["dispatches"]}
        self.assertEqual(states[open_id], "abandoned")
        self.assertEqual(states[review["dispatch_id"]], "reported")
        status = coordinator._read_object(
            self._records() / "dispatch-status" / f"{open_id}.json", "status"
        )
        self.assertEqual(status["state"], "abandoned")
        self.assertEqual(decided["abandoned"]["open_dispatches"], [open_id])
        self.assertEqual(
            list((self._records() / "qa-lane" / "queue").glob("*.json")),
            [],
            "a dead QA queue entry must not hold the lane",
        )

    def test_legacy_batch_abandon_still_ends_in_failed_and_closes_open_dispatches(
        self,
    ) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]

        result = coordinator.abandon_batch(
            self._args(
                batch=batch["batch_id"],
                reason="worker died before confirming its model",
                **self._approval(),
            )
        )

        self.assertEqual(
            (result["state"], result["abandoned_dispatches"]),
            ("failed", [brief["dispatch_id"]]),
        )
        record = self._batch_record(batch["batch_id"])
        self.assertEqual(record["dispatches"][0]["state"], "abandoned")
        self.assertEqual(
            record["abandoned"]["reason"], "worker died before confirming its model"
        )

    # -- parallel batches with explicit scope (issue #531) --------------------------------------

    def _second_batch(
        self,
        slug: str,
        *,
        ticket: str,
        allowed: list[str] | None = None,
        approve: bool = True,
    ) -> JsonObject:
        """A second batch in its own issue branch and worktree, planned beside the first."""
        worktree = self.tmp / slug
        branch = f"feature/issue-{ticket.lstrip('#')}-{slug}"
        _git(self.repo, "worktree", "add", "-b", branch, str(worktree), "master")
        plan = {
            **self._batch_plan(),
            "ticket": ticket,
            "branch": branch,
            "worktree": str(worktree),
            "allowed_path": allowed or ["**"],
        }
        batch = coordinator.create_batch(self._args(**plan))
        if approve:
            coordinator.approve_batch(
                self._args(batch=batch["batch_id"], **self._approval())
            )
        return batch

    def test_a_zone_free_batch_records_its_scope_and_no_zone(self) -> None:
        batch = self._create_batch()
        self.assertEqual(batch["allowed_paths"], ["**"])
        self.assertIsNone(batch["zone"])
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self.assertIsNone(brief["zone"])
        self.assertEqual(brief["write_paths"], [])

    def test_batches_with_overlapping_scope_run_in_parallel_worktrees(self) -> None:
        self._patch_config(concurrency_budget=2)
        first = self._create_batch()
        second = self._second_batch(
            "other", ticket="#245", allowed=["services/**", "docs/**"]
        )
        first_brief = self._dispatch(first["batch_id"], "architect")["brief"]
        second_brief = self._dispatch(second["batch_id"], "architect")["brief"]
        self.assertNotEqual(first_brief["worktree"], second_brief["worktree"])
        self.assertNotEqual(first_brief["branch"], second_brief["branch"])
        self.assertEqual(self._batch_record(first["batch_id"])["zone"], None)
        self.assertEqual(self._batch_record(second["batch_id"])["zone"], None)

    def test_a_writer_brief_pins_the_batch_scope(self) -> None:
        self._patch_config(concurrency_budget=2)
        first = self._create_batch()
        second = self._second_batch("narrow", ticket="#245", allowed=["services/**"])
        self._accepted_architect(second["batch_id"])
        brief = self._dispatch(second["batch_id"], "developer")["brief"]
        self.assertEqual(brief["write_paths"], ["services/**"])
        self.assertEqual(
            {path for step in brief["commit_plan"] for path in step["expected_paths"]},
            {"services/**"},
        )
        self.assertEqual(first["allowed_paths"], ["**"])

    def test_a_batch_beyond_the_concurrency_budget_is_rejected_with_a_remedy(
        self,
    ) -> None:
        first = self._create_batch()
        self._dispatch(first["batch_id"], "architect")
        second = self._second_batch("over", ticket="#245")
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._dispatch(second["batch_id"], "architect")
        self.assertIn("concurrency_budget (1)", caught.exception.message)
        self.assertIn("raise concurrency_budget", caught.exception.remedy)

    def test_a_second_batch_for_unfinished_work_is_rejected(self) -> None:
        first = self._create_batch()
        root = ledger_ops._state_root(self._args(), self.repo)
        for label, ticket, branch, worktree in (
            ("ticket", "#244", "feature/issue-900-x", str(self.tmp / "unused")),
            ("branch", "#900", self.branch, str(self.tmp / "unused")),
            ("worktree", "#900", "feature/issue-900-x", str(self.worktree)),
        ):
            with self.subTest(label=label):
                with self.assertRaises(coordinator.CoordinatorError) as caught:
                    batch_module._reject_duplicate_work(root, ticket, branch, worktree)
                self.assertIn(first["batch_id"], caught.exception.message)
                self.assertIn(f"this {label}", caught.exception.message)
                self.assertIn("batch abandon", caught.exception.remedy)
        # Created through the public path, the same ticket is refused before any record exists.
        twin = self.tmp / "twin"
        _git(
            self.repo,
            "worktree",
            "add",
            "-b",
            "feature/issue-244-twin",
            str(twin),
            "master",
        )
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            coordinator.create_batch(
                self._args(
                    **{
                        **self._batch_plan(),
                        "branch": "feature/issue-244-twin",
                        "worktree": str(twin),
                    }
                )
            )
        self.assertIn("this ticket", caught.exception.message)

    def test_a_finished_batch_does_not_block_a_fresh_attempt(self) -> None:
        first = self._create_batch()
        coordinator.abandon_batch(
            self._args(
                batch=first["batch_id"],
                reason="requirements withdrawn",
                **self._approval(),
            )
        )
        retry = self._second_batch("again", ticket="#244")
        self.assertNotEqual(retry["batch_id"], first["batch_id"])

    def test_a_decision_blocked_batch_does_not_hold_the_ticket_for_ever(self) -> None:
        first = self._create_batch()
        self._accepted_architect(first["batch_id"])
        candidate = self._accepted_candidate(first["batch_id"])
        self._infra_review(first["batch_id"], candidate)
        self._decide(
            first["batch_id"], "block", reason_category="verification-infrastructure"
        )
        self.assertEqual(self._batch_record(first["batch_id"])["state"], "blocked")
        retry = self._second_batch("after-block", ticket="#244")
        self.assertNotEqual(retry["batch_id"], first["batch_id"])

    def test_a_blocked_batch_with_an_open_dispatch_still_holds_its_work(self) -> None:
        first = self._create_batch()
        brief = self._dispatch(first["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        root = ledger_ops._state_root(self._args(), self.repo)
        record = self._batch_record(first["batch_id"])
        record["state"] = "blocked"
        ledger = LifecycleLedger(root)
        with ledger_ops._ledger_lock(ledger):
            ledger_ops._replace_record(ledger, BatchRecord.from_dict(record))
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._second_batch("twin", ticket="#244")
        self.assertIn("unfinished work", caught.exception.message)

    def test_parallel_work_is_allowed_while_the_qa_lane_is_occupied(self) -> None:
        self._patch_config(concurrency_budget=2)
        first = self._create_batch()
        second = self._second_batch("busy", ticket="#245")
        lease = self._records() / "qa-lane" / "lease.json"
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text(
            json.dumps(
                {
                    "dispatch_id": "dispatch-" + "a" * 8,
                    "host": "qa-host",
                    "pid": 1,
                    "acquired_at": self._later(0),
                    "expires_at": self._later(3600),
                }
            ),
            encoding="utf-8",
        )
        self._accepted_architect(first["batch_id"])
        brief = self._dispatch(first["batch_id"], "developer")["brief"]
        other = self._dispatch(second["batch_id"], "architect")["brief"]
        self.assertEqual(brief["role"], "developer")
        self.assertEqual(other["role"], "architect")

    def _legacy_twin(self, batch: JsonObject) -> JsonObject:
        """The same batch as an earlier runtime wrote it: a zone label and no explicit scope."""
        legacy = {key: value for key, value in batch.items() if key != "allowed_paths"}
        legacy.update(
            batch_id=f"batch-{uuid.uuid4()}", ticket="#246", zone="repository"
        )
        ledger = LifecycleLedger(ledger_ops._state_root(self._args(), self.repo))
        with ledger_ops._ledger_lock(ledger):
            ledger_ops._write_record(
                ledger,
                PlanRecord.from_dict(
                    {
                        **{
                            field: legacy[field]
                            for field in constants.PRE_SCOPE_PLAN_FIELDS
                        },
                        "goal": legacy["goal"],
                    }
                ),
            )
            ledger_ops._write_record(ledger, BatchRecord.from_dict(legacy))
        coordinator.approve_batch(
            self._args(batch=legacy["batch_id"], **self._approval())
        )
        return legacy

    def test_a_batch_planned_before_scopes_existed_stays_valid(self) -> None:
        self._patch_config(concurrency_budget=2)
        legacy = self._legacy_twin(self._create_batch())
        brief = self._dispatch(legacy["batch_id"], "architect")["brief"]
        self.assertEqual(brief["zone"], "repository")
        self._start(brief["dispatch_id"])
        self._submit(brief["dispatch_id"], self._base_report(brief, "architect"))
        self._decide(legacy["batch_id"], "accept")
        developer = self._dispatch(legacy["batch_id"], "developer")["brief"]
        self.assertEqual(developer["write_paths"], ["**"])
        self.assertNotIn("allowed_paths", self._batch_record(legacy["batch_id"]))

    def test_a_legacy_zone_batch_keeps_its_low_risk_authority_by_zone(self) -> None:
        legacy = self._legacy_twin(self._create_batch())
        zones = {"repository": {"paths": ["**"]}}
        for extra, expected in (
            ({"low_risk_zones": ["repository"]}, True),
            ({"low_risk_paths": ["**"]}, False),
            ({}, False),
        ):
            with self.subTest(extra=extra):
                self.assertIs(
                    contract.low_risk_eligible(
                        {"backend_zones": zones, **extra}, legacy
                    ),
                    expected,
                )

    def test_a_scope_added_to_only_one_record_is_refused(self) -> None:
        batch = self._create_batch()
        path = self._records() / "plans" / f"{batch['batch_id']}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        del record["allowed_paths"]
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._dispatch(batch["batch_id"], "architect")
        self.assertEqual(caught.exception.message, "batch record is incomplete")

    def test_abandon_is_valid_before_any_candidate_exists(self) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        self._submit(brief["dispatch_id"], self._base_report(brief, "architect"))

        decided = self._decide(
            batch["batch_id"], "abandon", reason="requirements withdrawn"
        )

        self.assertEqual(decided["state"], "abandoned")
        self.assertIsNone(decided["abandoned"]["last_accepted"])

    # -- operational guards (issue #250) -------------------------------------------------------

    def _patch_config(self, **policy: object) -> None:
        """Layer project policy over the zero-config defaults for the rest of the test."""
        original = config._config
        patcher = mock.patch.object(
            config, "_config", lambda repo: {**original(repo), **policy}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _later(seconds: int) -> str:
        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()

    def _attention(self, batch_id: str, *, after: int = 0) -> JsonObject:
        with mock.patch.object(utils, "_now", return_value=self._later(after)):
            return coordinator.attention_check(self._args(batch=batch_id))

    def _resolve_attention(self, batch_id: str) -> JsonObject:
        return coordinator.attention_resolve(
            self._args(
                batch=batch_id, note="reviewed by the operator", **self._approval()
            )
        )

    def _evidence(self, dispatch_id: str) -> list[bytes]:
        records = self._records()
        return [
            (records / "dispatches" / f"{dispatch_id}.json").read_bytes(),
            (records / "reports" / f"{dispatch_id}.json").read_bytes(),
            (records / "reports" / f"{dispatch_id}.md").read_bytes(),
        ]

    def _pressure(
        self, dispatch_id: str, observed: int, source: str = "provider-usage"
    ) -> JsonObject:
        return coordinator.record_context_pressure(
            self._args(dispatch=dispatch_id, observed_tokens=observed, source=source)
        )

    def _infra_blocked_and_retried(
        self,
        batch_id: str,
        candidate: str,
        category: str = "verification-infrastructure",
    ) -> JsonObject:
        first = self._infra_review(batch_id, candidate)
        self._decide(batch_id, "retry", reason_category=category)
        return first

    def _edit_batch(self, batch_id: str, **fields: object) -> None:
        root = ledger_ops._state_root(self._args(), self.repo)
        ledger = LifecycleLedger(root)
        with ledger_ops._ledger_lock(ledger):
            batch = ledger_ops._load_batch(root, batch_id)
            batch.update(fields)
            ledger_ops._replace_record(ledger, BatchRecord.from_dict(batch))

    def _age_heartbeat(self, dispatch_id: str, seconds: int) -> None:
        root = ledger_ops._state_root(self._args(), self.repo)
        ledger = LifecycleLedger(root)
        with ledger_ops._ledger_lock(ledger):
            status = ledger_ops._load_dispatch_status(root, dispatch_id)
            old = self._later(-seconds)
            status.update({"heartbeat_at": old, "updated_at": old})
            ledger_ops._replace_record(ledger, DispatchStatusRecord.from_dict(status))

    def _live_architect(self, batch_id: str) -> JsonObject:
        brief: JsonObject = self._dispatch(batch_id, "architect")["brief"]
        self._start(brief["dispatch_id"])
        return brief

    # 1. context pressure

    def test_critical_context_pressure_asks_for_a_checkpoint_but_never_changes_routing(
        self,
    ) -> None:
        batch = self._create_batch()
        brief = self._live_architect(batch["batch_id"])
        before = self._batch_record(batch["batch_id"])
        brief_bytes = (
            self._records() / "dispatches" / f"{brief['dispatch_id']}.json"
        ).read_bytes()

        ok = self._pressure(brief["dispatch_id"], 100_000)
        warning = self._pressure(brief["dispatch_id"], 130_000)
        critical = self._pressure(brief["dispatch_id"], 160_000)

        self.assertEqual(
            [item["level"] for item in (ok, warning, critical)],
            ["ok", "warning", "critical"],
        )
        self.assertEqual(
            {
                key: critical[key]
                for key in (
                    "observed_tokens",
                    "context_limit",
                    "warning_threshold",
                    "level",
                )
            },
            {
                "observed_tokens": 160_000,
                "context_limit": brief["context_budget"],
                "warning_threshold": 120_000,
                "level": "critical",
            },
        )
        self.assertTrue(critical["recorded_at"].strip())
        self.assertTrue(critical["action_required"])
        self.assertFalse(ok["action_required"])
        after = self._batch_record(batch["batch_id"])
        self.assertEqual(len(after["context_pressure"]), 3)
        for field in (
            "state",
            "next_action",
            "required_next_role",
            "coordinator_decisions",
            "dispatches",
            "coordinator_approval",
        ):
            self.assertEqual(after.get(field), before.get(field), field)
        self.assertFalse(after.get("needs_attention", False))
        self.assertEqual(
            (
                self._records() / "dispatches" / f"{brief['dispatch_id']}.json"
            ).read_bytes(),
            brief_bytes,
        )

    def test_context_pressure_never_accepts_a_model_self_report_as_telemetry(
        self,
    ) -> None:
        batch = self._create_batch()
        brief = self._live_architect(batch["batch_id"])

        for source in ("model-self-report", "self-report", "estimate", ""):
            with (
                self.subTest(source=source),
                self.assertRaises(coordinator.CoordinatorError),
            ):
                self._pressure(brief["dispatch_id"], 160_000, source)

        self.assertNotIn("context_pressure", self._batch_record(batch["batch_id"]))

    def test_context_pressure_is_read_from_the_configured_telemetry_provider_when_no_count_is_given(
        self,
    ) -> None:
        batch = self._create_batch()
        brief = self._live_architect(batch["batch_id"])

        class Provider:
            def observe(self, dispatch_id: str) -> extensions.ContextObservation | None:
                return extensions.ContextObservation(155_000, "runtime-adapter")

        extensions.register("context_telemetry_provider", "test-provider", Provider())
        self.addCleanup(
            extensions.unregister, "context_telemetry_provider", "test-provider"
        )
        self._patch_config(extensions={"context_telemetry_provider": "test-provider"})

        entry = coordinator.record_context_pressure(
            self._args(dispatch=brief["dispatch_id"], observed_tokens=None, source=None)
        )

        self.assertEqual(
            (entry["observed_tokens"], entry["level"], entry["source"]),
            (155_000, "critical", "runtime-adapter"),
        )

    def _checkpoint_writer(
        self, brief: JsonObject, candidate: str, changed: list[str]
    ) -> JsonObject:
        package_id = self._batch_record(brief["batch_id"])["context_packages"][-1][
            "context_package_id"
        ]
        checkpoint: JsonObject = {
            "dispatch_id": brief["dispatch_id"],
            "commit_sha": candidate,
            "changed_files": changed,
            "remaining_definition_of_done": [brief["definition_of_done"][-1]],
            "passing_checks": self._checks(brief),
            "risks": "preserve prior commit evidence",
            "blockers": "none",
            "context_package_id": package_id,
        }
        path = workspace._prepare_agent_inbox(self.repo) / "checkpoint.json"
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
        coordinator.checkpoint_dispatch(self._args(file=str(path)))
        return checkpoint

    def _resume_writer(self, brief: JsonObject, checkpoint: JsonObject) -> None:
        facts = {
            "dispatch_id": brief["dispatch_id"],
            "remaining_definition_of_done": checkpoint["remaining_definition_of_done"],
            "risks": checkpoint["risks"],
            "dependencies": brief["dependencies"],
        }
        path = workspace._prepare_agent_inbox(self.repo) / "continuation-facts.json"
        path.write_text(json.dumps(facts), encoding="utf-8")
        coordinator.resume_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                termination_reason=None,
                trigger="vertical-slice",
                measured_value=None,
                file=str(path),
                note="resume the recorded green slice",
                **self._approval(),
            )
        )

    def test_committed_checkpoint_continuation_attests_exact_progress_and_completes_plan(
        self,
    ) -> None:
        self._patch_config(worker_attestation_required=True)
        batch = self._create_batch(
            definition_of_done=["first slice", "remaining slice"]
        )
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(brief["dispatch_id"])
        brief_path = self._records() / "dispatches" / f"{brief['dispatch_id']}.json"
        original_brief = brief_path.read_bytes()
        first, changed = self._developer_commit("first")
        checkpoint = self._checkpoint_writer(brief, first, changed)
        self._resume_writer(brief, checkpoint)

        attested = coordinator.self_report_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                model="sonnet",
                worktree=str(self.worktree),
            )
        )

        self.assertEqual(attested["state"], "working")
        self.assertEqual(brief_path.read_bytes(), original_brief)
        final, changed = self._developer_commit("remaining")
        report = self._developer_report(
            brief,
            final,
            changed,
            commit_map=[
                {"commit_sha": first, "plan_entry_id": brief["commit_plan"][0]["id"]},
                {"commit_sha": final, "plan_entry_id": brief["commit_plan"][1]["id"]},
            ],
        )
        self.assertEqual(
            self._submit(brief["dispatch_id"], report)["state"], "reported"
        )
        self.assertEqual(
            self._decide(batch["batch_id"], "accept")["next_action"], "risk-assessment"
        )

    def test_startup_recovery_preserves_checkpoint_commits_before_first_completion(
        self,
    ) -> None:
        self._patch_config(worker_attestation_required=True)
        batch = self._create_batch(
            definition_of_done=["first slice", "remaining slice"]
        )
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)
        original = self._dispatch(batch_id, "developer")["brief"]
        self._start(original["dispatch_id"])
        first, changed = self._developer_commit("first")
        checkpoint = self._checkpoint_writer(original, first, changed)
        self._resume_writer(original, checkpoint)
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "approved brief resolved"
        ):
            coordinator.self_report_dispatch(
                self._args(
                    dispatch=original["dispatch_id"],
                    model="wrong-model",
                    worktree=str(self.worktree),
                )
            )
        coordinator.resume_batch(
            self._args(batch=batch_id, reason="restore approved model")
        )

        replacement = self._dispatch(batch_id, "developer", candidate=first)["brief"]

        self.assertEqual(replacement["snapshot_commit"], first)
        self.assertEqual(replacement["commit_plan"], original["commit_plan"])
        self.assertIsNone(replacement["risk_assessment_id"])
        self.assertEqual(
            self._batch_record(batch_id)["dispatches"][-2]["state"], "abandoned"
        )
        self._start(replacement["dispatch_id"])
        final, changed = self._developer_commit("remaining")
        full_map = [
            {"commit_sha": first, "plan_entry_id": replacement["commit_plan"][0]["id"]},
            {"commit_sha": final, "plan_entry_id": replacement["commit_plan"][1]["id"]},
        ]
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "each created commit"
        ):
            self._submit(
                replacement["dispatch_id"],
                self._developer_report(
                    replacement, final, changed, commit_map=full_map[1:]
                ),
            )
        self.assertEqual(
            self._submit(
                replacement["dispatch_id"],
                self._developer_report(
                    replacement, final, changed, commit_map=full_map
                ),
            )["state"],
            "reported",
        )
        self.assertEqual(
            self._decide(batch_id, "accept")["next_action"], "risk-assessment"
        )
        with self.assertRaisesRegex(coordinator.CoordinatorError, "risk assessment"):
            self._dispatch(batch_id, "qa", candidate=final)
        self._assess(batch_id, final, changed)
        self.assertEqual(self._batch_record(batch_id)["next_action"], "code-review")

    def _attested_checkpoint(self) -> tuple[JsonObject, JsonObject]:
        self._patch_config(worker_attestation_required=True)
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(brief["dispatch_id"])
        candidate, changed = self._developer_commit("checkpoint")
        return brief, self._checkpoint_writer(brief, candidate, changed)

    def test_checkpoint_does_not_authorize_a_new_start_without_resume(self) -> None:
        brief, _ = self._attested_checkpoint()
        with self.assertRaisesRegex(coordinator.CoordinatorError, "dispatched role"):
            coordinator.self_report_dispatch(
                self._args(
                    dispatch=brief["dispatch_id"],
                    model="sonnet",
                    worktree=str(self.worktree),
                )
            )

    def test_resumed_writer_rejects_progress_after_the_recorded_checkpoint(
        self,
    ) -> None:
        brief, checkpoint = self._attested_checkpoint()
        self._resume_writer(brief, checkpoint)
        self._developer_commit("uncheckpointed")
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, checkpoint["commit_sha"]
        ):
            coordinator.self_report_dispatch(
                self._args(
                    dispatch=brief["dispatch_id"],
                    model="sonnet",
                    worktree=str(self.worktree),
                )
            )

    def test_resumed_writer_rejects_a_tampered_checkpoint_record(self) -> None:
        brief, checkpoint = self._attested_checkpoint()
        self._resume_writer(brief, checkpoint)
        entry = self._batch_record(brief["batch_id"])["checkpoints"][-1]
        path = self._records() / "checkpoints" / f"{entry['checkpoint_id']}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["commit_sha"] = brief["snapshot_commit"]
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(coordinator.CoordinatorError, "integrity check"):
            coordinator.self_report_dispatch(
                self._args(
                    dispatch=brief["dispatch_id"],
                    model="sonnet",
                    worktree=str(self.worktree),
                )
            )

    def test_committed_checkpoint_still_requires_unchanged_continuation_facts(
        self,
    ) -> None:
        brief, checkpoint = self._attested_checkpoint()
        checkpoint["risks"] = "changed risk scope"
        with self.assertRaisesRegex(coordinator.CoordinatorError, "facts differ"):
            self._resume_writer(brief, checkpoint)

    def test_committed_checkpoint_still_obeys_the_continuation_budget(self) -> None:
        self._patch_config(continuation_policy={"max_continuations": 1})
        brief, checkpoint = self._attested_checkpoint()
        self._resume_writer(brief, checkpoint)
        coordinator.self_report_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                model="sonnet",
                worktree=str(self.worktree),
            )
        )
        checkpoint = self._checkpoint_writer(
            brief, checkpoint["commit_sha"], checkpoint["changed_files"]
        )
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "budget is exhausted"
        ):
            self._resume_writer(brief, checkpoint)

    def test_initial_writer_cannot_pin_history_outside_the_batch_base(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        unrelated = _git(
            self.worktree, "commit-tree", "HEAD^{tree}", "-m", "unrelated history"
        )
        # Simulate an orphaned issue branch in this disposable Git fixture.
        _git(self.worktree, "update-ref", f"refs/heads/{self.branch}", unrelated)
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "contain the batch base"
        ):
            self._dispatch(batch["batch_id"], "developer", candidate=unrelated)

    def test_architect_starts_from_progress_preserved_by_a_replacement_batch(
        self,
    ) -> None:
        batch = self._create_batch()
        batch_id = batch["batch_id"]
        progress, _ = self._developer_commit("progress")

        architect = self._dispatch(batch_id, "architect", candidate=progress)["brief"]

        self.assertEqual(architect["snapshot_commit"], progress)
        self.assertIsNone(architect["risk_assessment_id"])
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        self._decide(batch_id, "accept")
        developer = self._dispatch(batch_id, "developer", candidate=progress)["brief"]
        self.assertEqual(developer["snapshot_commit"], progress)

    def test_architect_cannot_pin_history_outside_the_batch_base(self) -> None:
        batch = self._create_batch()
        unrelated = _git(
            self.worktree, "commit-tree", "HEAD^{tree}", "-m", "unrelated history"
        )
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "contain the batch base"
        ):
            self._dispatch(batch["batch_id"], "architect", candidate=unrelated)

    def test_continuation_after_critical_pressure_needs_a_checkpoint_and_a_new_model_attestation(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(brief["dispatch_id"])
        self._pressure(brief["dispatch_id"], 170_000)
        resume = {
            "dispatch": brief["dispatch_id"],
            "termination_reason": "rate_limit",
            "trigger": None,
            "measured_value": None,
            "file": None,
            "note": None,
            "approved_by": None,
            "approved_at": None,
        }

        with self.assertRaises(
            coordinator.CoordinatorError
        ):  # pressure alone is not a continuation
            coordinator.resume_dispatch(self._args(**resume))

        candidate, changed = self._developer_commit("pressure")
        package_id = self._batch_record(batch["batch_id"])["context_packages"][-1][
            "context_package_id"
        ]
        checkpoint = (
            workspace._prepare_agent_inbox(self.repo)
            / f"checkpoint-{brief['dispatch_id']}.json"
        )
        checkpoint.write_text(
            json.dumps(
                {
                    "dispatch_id": brief["dispatch_id"],
                    "commit_sha": candidate,
                    "changed_files": changed,
                    "remaining_definition_of_done": [],
                    "passing_checks": self._checks(brief),
                    "risks": "none",
                    "blockers": "none",
                    "context_package_id": package_id,
                }
            ),
            encoding="utf-8",
        )
        coordinator.checkpoint_dispatch(self._args(file=str(checkpoint)))
        coordinator.resume_dispatch(self._args(**resume))

        status = ledger_ops._load_dispatch_status(
            ledger_ops._state_root(self._args(), self.repo), brief["dispatch_id"]
        )
        self.assertNotIn(
            "model_self_report", status
        )  # a new session must attest its model again
        path = (
            workspace._prepare_agent_inbox(self.repo) / f"{brief['dispatch_id']}.json"
        )
        path.write_text(
            json.dumps(self._developer_report(brief, candidate, changed)),
            encoding="utf-8",
        )
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.submit_report(self._args(file=str(path)))
        coordinator.self_report_dispatch(
            self._args(dispatch=brief["dispatch_id"], model="sonnet", worktree=None)
        )
        self.assertEqual(
            self._submit(
                brief["dispatch_id"], self._developer_report(brief, candidate, changed)
            )["state"],
            "reported",
        )

    def test_context_pressure_retry_needs_a_recorded_critical_observation(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        review = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)[
            "brief"
        ]
        self._start(review["dispatch_id"], checkout=self.worktree)
        self._pressure(review["dispatch_id"], 152_000)
        self._submit(
            review["dispatch_id"],
            self._base_report(
                review,
                "code-review",
                outcome="blocked",
                blockers="context window exhausted",
                checks_run=self._checks(review, "not-run"),
                review={
                    "candidate_commit": candidate,
                    "scope": review["review_scope"],
                    **self._axes(("none", []), ("none", [])),
                },
            ),
        )

        decided = self._decide(
            batch["batch_id"], "retry", reason_category="context-pressure"
        )

        self._assert_route(
            decided,
            role="code-review",
            action="code-review",
            category="context-pressure",
            candidate=candidate,
            route="same-candidate-rerun",
        )
        self.assertFalse(decided.get("needs_attention", False))

    # 2. attention state

    def test_a_long_queued_infrastructure_retry_sets_needs_attention_and_blocks_dispatch(
        self,
    ) -> None:
        self._patch_config(attention_policy={"retry_queue_seconds": 3600})
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        first = self._infra_blocked_and_retried(batch["batch_id"], candidate)
        evidence = self._evidence(first["dispatch_id"])
        routed = self._routing(self._batch_record(batch["batch_id"]))

        self.assertFalse(self._attention(batch["batch_id"])["needs_attention"])
        flagged = self._attention(batch["batch_id"], after=7200)

        self.assertTrue(flagged["needs_attention"])
        record = self._batch_record(batch["batch_id"])
        self.assertEqual(record["attention_reason"], "retry-queued-too-long")
        for field in (
            "attention_since",
            "last_safe_action",
            "recommended_human_action",
        ):
            self.assertTrue(record[field].strip(), field)
        self.assertEqual(
            (record["state"], record["next_action"]),
            ("awaiting-approval", "code-review"),
        )
        self.assertEqual(
            self._routing(record), routed
        )  # the candidate and the route are untouched
        self.assertEqual(self._evidence(first["dispatch_id"]), evidence)
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._dispatch(batch["batch_id"], "code-review", candidate=candidate)
        self.assertIn("attention", caught.exception.message.lower())
        self.assertEqual(
            len(self._batch_record(batch["batch_id"])["dispatches"]),
            len(record["dispatches"]),
        )

        self._resolve_attention(batch["batch_id"])

        self.assertFalse(self._batch_record(batch["batch_id"])["needs_attention"])
        self.assertFalse(
            self._attention(batch["batch_id"], after=7200)["needs_attention"]
        )  # acknowledged, not re-raised
        self.assertNotEqual(
            self._dispatch(batch["batch_id"], "code-review", candidate=candidate)[
                "dispatch_id"
            ],
            first["dispatch_id"],
        )
        self.assertEqual(self._evidence(first["dispatch_id"]), evidence)

    def test_repeated_infrastructure_retries_set_needs_attention(self) -> None:
        self._patch_config(attention_policy={"max_infrastructure_retries": 1})
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_blocked_and_retried(batch["batch_id"], candidate, "transport")
        self.assertFalse(
            self._batch_record(batch["batch_id"]).get("needs_attention", False)
        )

        self._infra_blocked_and_retried(batch["batch_id"], candidate, "transport")

        record = self._batch_record(batch["batch_id"])
        self.assertTrue(record["needs_attention"])
        self.assertEqual(record["attention_reason"], "infrastructure-retry-repeated")
        self.assertEqual(record["state"], "awaiting-approval")
        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(batch["batch_id"], "code-review", candidate=candidate)

    def test_the_third_consecutive_tooling_retry_sets_needs_attention(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        for _ in range(2):
            self._tooling_review(batch["batch_id"], candidate)
            self._decide(batch["batch_id"], "retry")
            self.assertFalse(
                self._batch_record(batch["batch_id"]).get("needs_attention", False)
            )
        third = self._tooling_review(batch["batch_id"], candidate)

        self._decide(batch["batch_id"], "retry")

        record = self._batch_record(batch["batch_id"])
        self.assertTrue(record["needs_attention"])
        self.assertEqual(record["attention_reason"], "tooling-retry-repeated")
        self.assertIn(
            "3 consecutive tooling retries", record["recommended_human_action"]
        )
        self.assertEqual(
            (record["state"], record["next_action"]),
            ("awaiting-approval", "code-review"),
        )
        self.assertEqual(self._routing(record)["route"], "tooling-retry")
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._dispatch(batch["batch_id"], "code-review", candidate=candidate)
        self.assertIn("attention", caught.exception.message.lower())
        self.assertEqual(record["dispatches"][-1]["dispatch_id"], third["dispatch_id"])

    def test_an_unknown_retry_reason_sets_needs_attention_and_notifies_the_human_adapter(
        self,
    ) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.events: list[extensions.AttentionEvent] = []

            def notify(self, event: extensions.AttentionEvent) -> None:
                self.events.append(event)

        recorder = Recorder()
        extensions.register("human_notifier", "test-recorder", recorder)
        self.addCleanup(extensions.unregister, "human_notifier", "test-recorder")
        self._patch_config(extensions={"human_notifier": "test-recorder"})
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_review(batch["batch_id"], candidate)

        decided = self._decide(batch["batch_id"], "retry")

        self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="unknown",
            candidate=candidate,
            route="developer-retry",
        )
        self.assertTrue(decided["needs_attention"])
        self.assertEqual(decided["attention_reason"], "unknown-reason")
        self.assertEqual(
            [(event.batch_id, event.reason) for event in recorder.events],
            [(batch["batch_id"], "unknown-reason")],
        )
        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(batch["batch_id"], "developer")
        self._resolve_attention(batch["batch_id"])
        self.assertEqual(
            self._dispatch(batch["batch_id"], "developer")["brief"]["role"], "developer"
        )

    def test_a_broken_notification_adapter_never_blocks_the_attention_state(
        self,
    ) -> None:
        class Broken:
            def notify(self, event: extensions.AttentionEvent) -> None:
                raise RuntimeError("chat is down")

        extensions.register("human_notifier", "test-broken", Broken())
        self.addCleanup(extensions.unregister, "human_notifier", "test-broken")
        self._patch_config(extensions={"human_notifier": "test-broken"})
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_review(batch["batch_id"], candidate)

        decided = self._decide(batch["batch_id"], "retry")

        self.assertTrue(decided["needs_attention"])
        self.assertEqual(
            decided["attention_events"][-1]["notification"]["status"], "failed"
        )

    def test_a_stale_dispatch_sets_needs_attention_without_touching_its_brief_or_report(
        self,
    ) -> None:
        batch = self._create_batch()
        brief = self._live_architect(batch["batch_id"])
        brief_bytes = (
            self._records() / "dispatches" / f"{brief['dispatch_id']}.json"
        ).read_bytes()
        self._age_heartbeat(brief["dispatch_id"], 7200)

        flagged = self._attention(batch["batch_id"])

        self.assertTrue(flagged["needs_attention"])
        record = self._batch_record(batch["batch_id"])
        self.assertEqual(record["attention_reason"], "stale-dispatch")
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["dispatches"][0]["state"], "dispatched")
        self.assertEqual(
            (
                self._records() / "dispatches" / f"{brief['dispatch_id']}.json"
            ).read_bytes(),
            brief_bytes,
        )
        self._submit(
            brief["dispatch_id"], self._base_report(brief, "architect")
        )  # the worker may still report
        self.assertEqual(
            self._batch_record(batch["batch_id"])["dispatches"][0]["state"], "reported"
        )

    def test_a_pinned_context_package_that_went_stale_sets_needs_attention(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._dispatch(
            batch["batch_id"], "code-review", candidate=candidate
        )  # approved, not yet sent
        self.assertFalse(self._attention(batch["batch_id"])["needs_attention"])

        self._edit_batch(batch["batch_id"], integration_base_commit="f" * 40)
        flagged = self._attention(batch["batch_id"])

        self.assertTrue(flagged["needs_attention"])
        self.assertEqual(
            self._batch_record(batch["batch_id"])["attention_reason"], "stale-evidence"
        )

    def test_a_stale_wait_event_also_sets_needs_attention(self) -> None:
        batch = self._create_batch()
        brief = self._live_architect(batch["batch_id"])
        self._age_heartbeat(brief["dispatch_id"], 7200)

        event = coordinator.wait_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                timeout=1,
                poll_interval=1,
                stale_after=900,
            )
        )

        self.assertEqual(event["event"], "stale")
        record = self._batch_record(batch["batch_id"])
        self.assertEqual(
            (record["needs_attention"], record["attention_reason"], record["state"]),
            (True, "stale-dispatch", "active"),
        )

    # 2b. a busy ledger lock while the coordinator polls (#498)

    @contextlib.contextmanager
    def _ledger_held(self, release: threading.Event) -> Iterator[None]:
        """Hold the real ledger lock from a concurrent thread until ``release`` is set."""
        holding = threading.Event()

        def hold() -> None:
            with LifecycleLedger(self.state_dir).lock():
                holding.set()
                release.wait(30)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(holding.wait(10))
            yield
        finally:
            release.set()
            holder.join()

    def _architect_reported(self) -> JsonObject:
        batch = self._create_batch()
        brief = self._live_architect(batch["batch_id"])
        self._submit(brief["dispatch_id"], self._base_report(brief, "architect"))
        return brief

    def _wait_busy(self, dispatch_id: str, timeout: int) -> JsonObject:
        return coordinator.wait_dispatch(
            self._args(
                dispatch=dispatch_id, timeout=timeout, poll_interval=1, stale_after=900
            )
        )

    def test_dispatch_wait_polls_through_a_busy_ledger_and_returns_the_report_once_released(
        self,
    ) -> None:
        brief = self._architect_reported()
        release = threading.Event()

        with self._ledger_held(release):
            timer = threading.Timer(1.5, release.set)
            timer.start()
            started = time.monotonic()
            event = self._wait_busy(brief["dispatch_id"], timeout=10)
            elapsed = time.monotonic() - started
            timer.join()

        self.assertEqual(
            event, {"dispatch_id": brief["dispatch_id"], "event": "reported"}
        )
        self.assertGreaterEqual(elapsed, 1.5)

    def test_dispatch_wait_times_out_normally_while_the_ledger_stays_busy(
        self,
    ) -> None:
        brief = self._architect_reported()

        with self._ledger_held(threading.Event()):
            event = self._wait_busy(brief["dispatch_id"], timeout=2)

        self.assertEqual(
            event, {"dispatch_id": brief["dispatch_id"], "event": "timeout"}
        )

    def test_dispatch_status_answers_a_busy_ledger_with_a_structured_retry(
        self,
    ) -> None:
        brief = self._architect_reported()
        args = self._args(dispatch=None, batch=brief["batch_id"], stale_after=900)

        with LifecycleLedger(self.state_dir).lock():
            busy = coordinator.dispatch_status(args)

        self.assertTrue(busy["ledger_busy"])
        self.assertEqual(
            busy["retry_after_seconds"],
            config._execution_policy(config._config(self.repo))[
                "dispatch_poll_interval_seconds"
            ],
        )
        self.assertEqual(busy["lock"]["owner"]["pid"], os.getpid())
        self.assertIn("repeat dispatch status", busy["remedy"])
        self.assertIn("ledger release-lock", busy["remedy"])
        self.assertNotIn("dispatches", busy)
        after = coordinator.dispatch_status(args)
        self.assertNotIn("ledger_busy", after)
        self.assertEqual(
            [entry["dispatch_id"] for entry in after["dispatches"]],
            [brief["dispatch_id"]],
        )

    def test_the_dispatch_status_cli_exits_zero_on_a_busy_ledger(self) -> None:
        brief = self._architect_reported()

        with self._ledger_held(threading.Event()):
            result = subprocess.run(
                [
                    sys.executable,
                    str(ORCHESTRATION_ROOT / "coordinator.py"),
                    "--repo",
                    str(self.repo),
                    "--state-dir",
                    str(self.state_dir),
                    "dispatch",
                    "status",
                    "--batch",
                    brief["batch_id"],
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        answer = json.loads(result.stdout)
        self.assertTrue(answer["ledger_busy"])
        self.assertEqual(answer["lock"]["owner"]["pid"], os.getpid())

    # 2c. completing a recorded report's stopped policy chain (#498)

    def _ledger_files(self) -> dict[str, bytes]:
        records = self._records()
        return {
            path.relative_to(records).as_posix(): path.read_bytes()
            for path in sorted(records.rglob("*"))
            if path.is_file()
        }

    def _complete(self, dispatch_id: str) -> JsonObject:
        return coordinator.complete_report(self._args(dispatch=dispatch_id))

    def _completion_command(self, dispatch_id: str) -> str:
        return (
            "python .harness/orchestration/coordinator.py --repo . report complete "
            f"--dispatch {dispatch_id} --state-dir {shlex.quote(str(self.state_dir))}"
        )

    def _architect_report_under_attention(self) -> tuple[JsonObject, JsonObject]:
        """A low_risk architect report whose batch raised attention before it was submitted:
        the policy decision is recorded, then the next dispatch is refused on attention."""
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        batch = self._create_batch()
        brief = self._live_architect(batch["batch_id"])
        self._age_heartbeat(brief["dispatch_id"], 7200)
        self.assertEqual(
            self._wait_busy(brief["dispatch_id"], timeout=1)["event"], "stale"
        )
        submitted = self._submit(
            brief["dispatch_id"], self._base_report(brief, "architect")
        )
        return brief, submitted

    def test_a_stopped_policy_chain_reports_the_recorded_report_and_its_completion(
        self,
    ) -> None:
        brief, submitted = self._architect_report_under_attention()

        stored = self._batch_record(brief["batch_id"])
        self.assertEqual(submitted["state"], "reported")
        self.assertEqual(submitted["dispatch_id"], brief["dispatch_id"])
        self.assertTrue(Path(submitted["report"]).is_file())
        self.assertEqual(
            submitted["report_sha256"], stored["dispatches"][0]["report_sha256"]
        )
        self.assertTrue(submitted["auto_accepted"])
        self.assertEqual(submitted["next_action"], "developer")
        self.assertIsNone(submitted["next_dispatch_id"])
        completion = submitted["completion"]
        self.assertEqual(completion["route"], "report-completion")
        self.assertEqual(completion["failed_step"], "next-dispatch")
        self.assertEqual(
            completion["steps"],
            {
                "policy-decide": "done",
                "risk-assess": "not-applicable",
                "next-dispatch": "failed",
            },
        )
        self.assertIn("attention", completion["error"]["message"])
        self.assertEqual(
            completion["command"], self._completion_command(brief["dispatch_id"])
        )
        self.assertEqual(completion["run_by"], "coordinator")
        self.assertIn("never submit it again", completion["remedy"])
        self.assertEqual(len(stored["coordinator_decisions"]), 1)
        self.assertEqual(
            stored["coordinator_decisions"][0]["approved_by"], "policy:low_risk"
        )
        self.assertEqual(len(stored["dispatches"]), 1)
        status = ledger_ops._load_dispatch_status(
            ledger_ops._state_root(self._args(), self.repo), brief["dispatch_id"]
        )
        self.assertEqual(status["auto_accept_policy"], "low_risk")

    def test_report_complete_finishes_the_chain_once_and_a_rerun_changes_nothing(
        self,
    ) -> None:
        brief, submitted = self._architect_report_under_attention()
        self._resolve_attention(brief["batch_id"])

        completed = self._complete(brief["dispatch_id"])

        stored = self._batch_record(brief["batch_id"])
        self.assertEqual(
            completed["steps"],
            {
                "policy-decide": "already-done",
                "risk-assess": "not-applicable",
                "next-dispatch": "done",
            },
        )
        self.assertEqual(completed["route"], "report-completion")
        self.assertEqual(completed["report"], submitted["report"])
        self.assertEqual(completed["report_sha256"], submitted["report_sha256"])
        self.assertEqual(completed["auto_accept_policy"], "low_risk")
        self.assertEqual(completed["next_action"], "developer")
        self.assertEqual(
            [entry["role"] for entry in stored["dispatches"]],
            ["architect", "developer"],
        )
        self.assertEqual(
            completed["next_dispatch_id"], stored["dispatches"][1]["dispatch_id"]
        )
        self.assertEqual(stored["dispatches"][1]["state"], "approved")
        self.assertEqual(len(stored["coordinator_decisions"]), 1)
        before = self._ledger_files()

        repeated = self._complete(brief["dispatch_id"])

        self.assertEqual(
            repeated["steps"],
            {
                "policy-decide": "already-done",
                "risk-assess": "not-applicable",
                "next-dispatch": "already-done",
            },
        )
        self.assertEqual(repeated["next_dispatch_id"], completed["next_dispatch_id"])
        self.assertEqual(self._ledger_files(), before)

    def test_report_complete_on_a_busy_ledger_names_the_step_and_writes_nothing(
        self,
    ) -> None:
        brief, _ = self._architect_report_under_attention()
        self._resolve_attention(brief["batch_id"])
        before = self._ledger_files()

        with LifecycleLedger(self.state_dir).lock():
            with self.assertRaises(coordinator.CoordinatorError) as stopped:
                self._complete(brief["dispatch_id"])

        self.assertIn("policy-decide", stopped.exception.message)
        self.assertIn(
            "ledger is locked by another operation", stopped.exception.message
        )
        self.assertIn(
            self._completion_command(brief["dispatch_id"]), stopped.exception.remedy
        )
        self.assertEqual(self._ledger_files(), before)

    def test_report_complete_has_nothing_to_run_for_a_report_left_for_a_human(
        self,
    ) -> None:
        brief = self._architect_reported()  # manual_all: no policy decides it
        before = self._ledger_files()

        completed = self._complete(brief["dispatch_id"])

        self.assertEqual(
            completed["steps"],
            dict.fromkeys(
                ("policy-decide", "risk-assess", "next-dispatch"), "not-applicable"
            ),
        )
        self.assertIsNone(completed["auto_accept_policy"])
        self.assertIsNone(completed["next_dispatch_id"])
        self.assertEqual(self._ledger_files(), before)

    def test_a_completed_policy_chain_keeps_the_submit_response(self) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        batch = self._create_batch()
        brief = self._live_architect(batch["batch_id"])

        submitted = self._submit(
            brief["dispatch_id"], self._base_report(brief, "architect")
        )

        self.assertEqual(
            set(submitted),
            {
                "dispatch_id",
                "state",
                "report",
                "auto_accepted",
                "next_action",
                "next_dispatch_id",
            },
        )
        self.assertTrue(submitted["auto_accepted"])
        self.assertEqual(
            submitted["next_dispatch_id"],
            self._batch_record(batch["batch_id"])["dispatches"][1]["dispatch_id"],
        )

    def test_an_auto_accepted_verification_report_is_assessed_on_its_pinned_candidate(
        self,
    ) -> None:
        """A verification report is read-only and has no commit of its own: its policy chain
        assesses the candidate its dispatch pinned and hands that candidate to QA, so the chain
        completes and report complete has nothing left to run."""
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        plan = self._batch_plan()
        plan["definition_of_done"] = ["add simple marker"]
        with mock.patch.object(self, "_batch_plan", return_value=plan):
            batch = self._create_batch()
        architect = self._live_architect(batch["batch_id"])
        developer_id = self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )["next_dispatch_id"]
        developer = coordinator._read_object(
            self._records() / "dispatches" / f"{developer_id}.json", "dispatch"
        )
        self._start(developer_id)
        candidate, changed = self._developer_commit("marker")
        self._submit(
            developer_id,
            self._developer_report(
                developer,
                candidate,
                changed,
                outcome="blocked",
                blockers="verification environment unavailable",
            ),
        )
        self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )
        verification = self._dispatch(
            batch["batch_id"], "verification", candidate=candidate
        )["brief"]
        self._start(verification["dispatch_id"], checkout=self.worktree)

        submitted = self._submit(
            verification["dispatch_id"],
            self._base_report(verification, "verification"),
        )

        self.assertTrue(submitted["auto_accepted"])
        self.assertNotIn("completion", submitted)
        stored = self._batch_record(batch["batch_id"])
        self.assertEqual(
            [item["candidate_commit"] for item in stored["risk_assessments"]],
            [candidate],
        )
        self.assertEqual(submitted["next_action"], "qa")
        qa = stored["dispatches"][-1]
        self.assertEqual(
            (qa["role"], qa["dispatch_id"]), ("qa", submitted["next_dispatch_id"])
        )
        self.assertEqual(
            coordinator._read_object(
                self._records() / "dispatches" / f"{qa['dispatch_id']}.json",
                "dispatch",
            )["candidate_commit"],
            candidate,
        )
        before = self._ledger_files()

        repeated = self._complete(verification["dispatch_id"])

        self.assertEqual(
            repeated["steps"],
            dict.fromkeys(
                ("policy-decide", "risk-assess", "next-dispatch"), "already-done"
            ),
        )
        self.assertEqual(self._ledger_files(), before)

    def test_risk_assessment_for_a_completion_is_refused_once_the_batch_moved_on(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        candidate, changed = self._developer_commit("x")
        self._submit(
            developer["dispatch_id"],
            self._developer_report(developer, candidate, changed),
        )
        self._decide(batch["batch_id"], "accept")
        before = self._ledger_files()

        def assess(expected: str) -> JsonObject:
            return coordinator.assess_risk(
                self._args(
                    batch=batch["batch_id"],
                    candidate_commit=candidate,
                    base_commit=None,
                    changed_file=changed,
                    developer_trigger=[],
                    _expected_next_action=expected,
                )
            )

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "no longer awaits the risk assessment"
        ):
            assess("code-review")
        self.assertEqual(self._ledger_files(), before)
        assess("risk-assessment")
        self.assertEqual(
            len(self._batch_record(batch["batch_id"])["risk_assessments"]), 1
        )

    def test_the_cli_wires_report_complete(self) -> None:
        args = coordinator.parser().parse_args(
            ["report", "complete", "--dispatch", "dispatch-1"]
        )

        self.assertIs(args.handler, coordinator.complete_report)
        self.assertEqual(args.dispatch, "dispatch-1")

    # 3. approvals bound to the transition digest

    def test_the_proposal_is_a_dry_run_that_binds_the_canonical_transition(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        dispatches = self._dispatch_ids(batch["batch_id"])

        proposal = self._propose(batch["batch_id"], "code-review", candidate=candidate)

        self.assertEqual(
            self._dispatch_ids(batch["batch_id"]), dispatches
        )  # nothing was created
        transition = proposal["transition"]
        self.assertEqual(
            set(transition),
            set(operational_guards.TRANSITION_FIELDS) | {"runtime_access_sha256"},
        )
        self.assertEqual(
            proposal["transition_digest"],
            operational_guards.transition_digest(transition),
        )
        self.assertEqual(
            (
                transition["batch_id"],
                transition["previous_role"],
                transition["next_role"],
                transition["candidate_sha"],
            ),
            (batch["batch_id"], "developer", "code-review", candidate),
        )
        created = self._dispatch(
            batch["batch_id"],
            "code-review",
            candidate=candidate,
            digest=proposal["transition_digest"],
        )
        brief = created["brief"]
        self.assertEqual(brief["transition_digest"], proposal["transition_digest"])
        self.assertEqual(
            brief["coordinator_approval"]["transition_digest"],
            proposal["transition_digest"],
        )
        self.assertEqual(brief["transition"], transition)
        self.assertEqual(
            brief["transition"]["context_package_id"], brief["context_package_id"]
        )

    def _memory_dispatch_fixture(self) -> tuple[JsonObject, Path]:
        from harness.memory import build

        project_path = self.repo / ".harness/project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project.update(
            {
                "memory": {"enabled": True},
                "memory_policy": {
                    "source_types": ["glossary"],
                    "allow_paths": ["CONTEXT.md"],
                    "redact_rules": [],
                    "min_similarity": 0,
                    "top_k": 5,
                    "max_tokens": 1000,
                },
            }
        )
        project_path.write_text(json.dumps(project))
        source_path = self.repo / "CONTEXT.md"
        source_path.write_text("# Memory\nroute retries")
        build(self.repo)
        batch = self._create_batch()
        return self._proposal_fields(
            batch["batch_id"], "architect", "work", None
        ), source_path

    def test_a_changed_memory_source_registers_a_new_package_instead_of_reuse(
        self,
    ) -> None:
        from harness.memory import build

        fields, source_path = self._memory_dispatch_fixture()
        root = ledger_ops._state_root(self._args(), self.repo)
        enabled = coordinator.create_dispatch(
            self._args(propose=True, no_memory=False, **fields)
        )
        coordinator.create_dispatch(self._args(propose=True, no_memory=True, **fields))
        stale_id = enabled["transition"]["context_package_id"]
        stale_package = ledger_ops._load_context_package(root, stale_id)
        source_path.write_text("# Changed source\nroute retries")
        build(self.repo)
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "approval digest does not match"
        ):
            coordinator.create_dispatch(
                self._args(
                    no_memory=False,
                    transition_digest=enabled["transition_digest"],
                    **fields,
                    **self._approval(),
                )
            )

        proposal = coordinator.create_dispatch(
            self._args(propose=True, no_memory=False, **fields)
        )
        freshness = proposal["context_package_freshness"]
        self.assertNotEqual(freshness["context_package_id"], stale_id)
        self.assertEqual(freshness["status"], "fresh")
        created = coordinator.create_dispatch(
            self._args(
                no_memory=False,
                transition_digest=proposal["transition_digest"],
                **fields,
                **self._approval(),
            )
        )

        package = ledger_ops._load_context_package(
            root, created["brief"]["context_package_id"]
        )
        self.assertEqual(package["context_package_id"], freshness["context_package_id"])
        self.assertNotEqual(
            package["memory"]["pointers"][0]["source_hash"],
            stale_package["memory"]["pointers"][0]["source_hash"],
        )
        self.assertEqual(
            ledger_ops._load_context_package(root, stale_id), stale_package
        )

    def test_developer_dispatch_survives_a_memory_source_change_after_architect(
        self,
    ) -> None:
        """#636: the architect's shared package must not block the developer forever."""
        from harness.memory import build

        _, source_path = self._memory_dispatch_fixture()
        root = ledger_ops._state_root(self._args(), self.repo)
        architect = self._dispatch(self.batch_id, "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        self._decide(self.batch_id, "accept")
        architect_package = ledger_ops._load_context_package(
            root, architect["context_package_id"]
        )
        source_path.write_text("# Memory\nroute retries, revised")
        build(self.repo)

        developer = self._dispatch(self.batch_id, "developer")["brief"]

        self.assertNotEqual(
            developer["context_package_id"], architect["context_package_id"]
        )
        self.assertEqual(
            ledger_ops._load_context_package(root, architect["context_package_id"]),
            architect_package,
        )

    def test_unchanged_memory_sources_keep_reusing_the_shared_package(self) -> None:
        _, _ = self._memory_dispatch_fixture()
        architect = self._dispatch(self.batch_id, "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        self._decide(self.batch_id, "accept")

        developer = self._dispatch(self.batch_id, "developer")["brief"]

        self.assertEqual(
            developer["context_package_id"], architect["context_package_id"]
        )

    def test_a_stale_fresh_registration_names_the_source_hashes_and_remedy(
        self,
    ) -> None:
        fields, _ = self._memory_dispatch_fixture()
        cases = {
            "changed": ("b" * 64, "CONTEXT.md has source_hash " + "b" * 64),
            "unavailable": (None, "CONTEXT.md is unavailable or revoked"),
        }
        for case, (actual, expected) in cases.items():
            stale = {
                "status": "stale",
                "memory_mismatch": {
                    "path": "CONTEXT.md",
                    "expected_source_hash": "a" * 64,
                    "actual_source_hash": actual,
                },
            }
            with self.subTest(case=case):
                with (
                    mock.patch.object(
                        dispatch, "_context_package_freshness", return_value=stale
                    ),
                    self.assertRaises(coordinator.CoordinatorError) as raised,
                ):
                    coordinator.create_dispatch(
                        self._args(propose=True, no_memory=False, **fields)
                    )
                message = str(raised.exception)
                self.assertIn(expected, message)
                self.assertIn("a" * 64, message)
                self.assertIn("harness memory rebuild", raised.exception.remedy)
                self.assertIn("--no-memory", raised.exception.remedy)

    def test_bypass_reuse_is_not_blocked_by_a_later_stale_enabled_package(self) -> None:
        import sqlite3

        fields, source_path = self._memory_dispatch_fixture()
        with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connects:
            bypass = coordinator.create_dispatch(
                self._args(propose=True, no_memory=True, **fields)
            )
            coordinator.create_dispatch(
                self._args(propose=True, no_memory=False, **fields)
            )
            source_path.write_text("# Changed source")
            proposal = coordinator.create_dispatch(
                self._args(propose=True, no_memory=True, **fields)
            )
            self.assertEqual(
                proposal["context_package_freshness"]["context_package_id"],
                bypass["transition"]["context_package_id"],
            )
            self.assertEqual(proposal["context_package_freshness"]["status"], "fresh")
            created = coordinator.create_dispatch(
                self._args(
                    no_memory=True,
                    transition_digest=proposal["transition_digest"],
                    **fields,
                    **self._approval(),
                )
            )
            self.assertEqual(
                created["brief"]["context_package_id"],
                bypass["transition"]["context_package_id"],
            )
            self.assertEqual(
                connects.call_count,
                1,
                "bypass reuse cannot refresh a stale enabled package",
            )

    def test_selected_enabled_identity_stays_fresh_after_index_only_write(self) -> None:
        import sqlite3
        from harness.memory import build

        fields, _ = self._memory_dispatch_fixture()
        enabled = coordinator.create_dispatch(
            self._args(propose=True, no_memory=False, **fields)
        )
        coordinator.create_dispatch(self._args(propose=True, no_memory=True, **fields))
        build(self.repo)
        with mock.patch.object(
            sqlite3,
            "connect",
            side_effect=AssertionError("reuse freshness cannot open index"),
        ):
            proposal = coordinator.create_dispatch(
                self._args(propose=True, no_memory=False, **fields)
            )
        self.assertEqual(
            proposal["context_package_freshness"]["context_package_id"],
            enabled["transition"]["context_package_id"],
        )
        self.assertEqual(proposal["context_package_freshness"]["status"], "fresh")

    def test_proposal_warns_about_minimal_repo_map_without_blocking_dispatch(
        self,
    ) -> None:
        batch = self._create_batch()

        proposal = self._propose(batch["batch_id"], "architect")

        warning = proposal["context_package_quality_warning"]
        self.assertEqual(warning["tier"], "minimal")
        self.assertTrue(warning["degradation_reason"])
        self.assertIsInstance(warning["parser_provenance"], dict)
        created = self._dispatch(
            batch["batch_id"], "architect", digest=proposal["transition_digest"]
        )
        self.assertEqual(created["state"], "approved")

    def test_proposal_omits_warning_for_full_repo_map(self) -> None:
        batch = self._create_batch()
        full_package: JsonObject = {
            "context_package_id": "context-package-full",
            "parser_provenance": {
                "tier": "full",
                "degradation_reason": "parser bundle applied",
                "parser_provenance": {},
            },
        }

        with (
            mock.patch.object(
                dispatch, "_persist_context_package", return_value=full_package
            ),
            mock.patch.object(
                dispatch,
                "_context_package_freshness",
                return_value={"status": "fresh"},
            ),
        ):
            proposal = self._propose(batch["batch_id"], "architect")

        self.assertNotIn("context_package_quality_warning", proposal)

    def test_an_approval_is_valid_only_for_the_exact_transition_digest(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        proposal = self._propose(batch["batch_id"], "code-review", candidate=candidate)
        changes = {
            "batch_id": "batch-other",
            "previous_dispatch_id": "dispatch-other",
            "previous_role": "qa",
            "reason_category": "transport",
            "next_role": "qa",
            "next_action": "qa",
            "purpose": "publish",
            "candidate_sha": "e" * 40,
            "base_sha": "d" * 40,
            "review_scope": ["services/other.py"],
            "verification_commands": ["make anything-else"],
            "context_package_id": "context-package-other",
            "required_gates": ["everything"],
        }

        for field, value in changes.items():
            altered = operational_guards.transition_digest(
                {**proposal["transition"], field: value}
            )
            with (
                self.subTest(field=field),
                self.assertRaises(coordinator.CoordinatorError) as caught,
            ):
                self._dispatch(
                    batch["batch_id"],
                    "code-review",
                    candidate=candidate,
                    digest=altered,
                )
            self.assertIn("digest", caught.exception.message.lower())

        self.assertEqual(
            len(self._batch_record(batch["batch_id"])["dispatches"]), 2
        )  # architect + developer only

    def test_an_explicit_approval_without_a_digest_is_rejected(self) -> None:
        batch = self._create_batch()
        fields = self._proposal_fields(batch["batch_id"], "architect", "work", None)

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            coordinator.create_dispatch(
                self._args(transition_digest=None, **fields, **self._approval())
            )

        self.assertIn("digest", caught.exception.message.lower())
        self.assertEqual(self._dispatch_ids(batch["batch_id"]), [])

    def test_a_policy_approval_is_bound_to_its_own_transition_digest(self) -> None:
        self._patch_config(approval_policy="milestone")
        batch = self._create_batch()
        fields = self._proposal_fields(batch["batch_id"], "architect", "work", None)

        brief = coordinator.create_dispatch(
            self._args(transition_digest=None, **fields)
        )["brief"]

        self.assertEqual(
            brief["coordinator_approval"]["approved_by"], "policy:milestone"
        )
        self.assertEqual(
            brief["coordinator_approval"]["transition_digest"],
            brief["transition_digest"],
        )

    def test_an_expired_or_future_dated_approval_fails_closed(self) -> None:
        batch = (
            self._create_batch()
        )  # its own approval predates the TTL policy set below
        self._patch_config(approval_ttl_seconds=600)
        proposal = self._propose(batch["batch_id"], "architect")
        fields = self._proposal_fields(batch["batch_id"], "architect", "work", None)

        for label, approved_at in (
            ("expired", self.APPROVED_AT),
            ("future", self._later(7200)),
        ):
            with (
                self.subTest(label),
                self.assertRaises(coordinator.CoordinatorError) as caught,
            ):
                coordinator.create_dispatch(
                    self._args(
                        transition_digest=proposal["transition_digest"],
                        approved_by="Malove",
                        approved_at=approved_at,
                        **fields,
                    )
                )
            self.assertIn("approval", caught.exception.message.lower())

        self.assertEqual(self._dispatch_ids(batch["batch_id"]), [])
        fresh = coordinator.create_dispatch(
            self._args(
                transition_digest=proposal["transition_digest"],
                approved_by="Malove",
                approved_at=self._later(-30),
                **fields,
            )
        )
        self.assertEqual(fresh["state"], "approved")

    def test_a_denied_terminal_approval_is_not_retried_and_advances_nothing(
        self,
    ) -> None:
        batch = self._create_batch()
        self._patch_config(
            human_approval_gate="tty"
        )  # after the batch: approving it would prompt a real terminal
        proposal = self._propose(batch["batch_id"], "architect")
        fields = self._proposal_fields(batch["batch_id"], "architect", "work", None)
        denied = coordinator.CoordinatorError(
            "human approval was not confirmed on the terminal", remedy="ask again"
        )

        with (
            mock.patch.object(
                approval, "_confirm_on_terminal", side_effect=denied
            ) as confirm,
            self.assertRaises(coordinator.CoordinatorError),
        ):
            coordinator.create_dispatch(
                self._args(
                    transition_digest=proposal["transition_digest"],
                    **fields,
                    **self._approval(),
                )
            )

        self.assertEqual(confirm.call_count, 1)
        self.assertEqual(self._dispatch_ids(batch["batch_id"]), [])

    def test_a_new_candidate_cannot_reuse_an_old_approval_context_package_or_review_evidence(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        first_candidate = self._accepted_candidate(batch["batch_id"], "a")
        first_proposal = self._propose(
            batch["batch_id"], "code-review", candidate=first_candidate
        )
        blocker = {"severity": "blocker", "summary": "wrong", "evidence": "a.py:1"}
        first = self._dispatch(
            batch["batch_id"],
            "code-review",
            candidate=first_candidate,
            digest=first_proposal["transition_digest"],
        )["brief"]
        self._start(first["dispatch_id"], checkout=self.worktree)
        self._submit(
            first["dispatch_id"],
            self._base_report(
                first,
                "code-review",
                outcome="blocked",
                blockers="fix needed",
                checks_run=self._checks(first, "not-run"),
                review={
                    "candidate_commit": first_candidate,
                    "scope": first["review_scope"],
                    **self._axes(("none", []), ("blocker", [blocker])),
                },
            ),
        )
        self._decide(batch["batch_id"], "retry")
        retry = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(retry["dispatch_id"])
        second_candidate, changed = self._developer_commit("b")
        self._submit(
            retry["dispatch_id"],
            self._developer_report(retry, second_candidate, changed),
        )
        self._decide(batch["batch_id"], "accept")
        self._assess(batch["batch_id"], second_candidate, changed)

        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(
                batch["batch_id"],
                "code-review",
                candidate=second_candidate,
                digest=first_proposal["transition_digest"],
            )
        second_proposal = self._propose(
            batch["batch_id"], "code-review", candidate=second_candidate
        )
        second = self._dispatch(
            batch["batch_id"],
            "code-review",
            candidate=second_candidate,
            digest=second_proposal["transition_digest"],
        )["brief"]

        self.assertNotEqual(
            second_proposal["transition_digest"], first_proposal["transition_digest"]
        )
        self.assertNotEqual(second["context_package_id"], first["context_package_id"])
        self.assertNotEqual(second["risk_assessment_id"], first["risk_assessment_id"])
        self.assertNotEqual(
            second["retry_idempotency_key"], first["retry_idempotency_key"]
        )
        self.assertEqual(
            (
                second["transition"]["previous_role"],
                second["transition"]["reason_category"],
            ),
            ("developer", None),
        )

    # 4. read-only retry idempotency

    def test_the_brief_records_an_idempotency_key_only_for_read_only_roles_and_publish(
        self,
    ) -> None:
        batch = self._create_batch()
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        self._decide(batch["batch_id"], "accept")
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]

        self.assertRegex(architect["retry_idempotency_key"], r"^[0-9a-f]{64}$")
        self.assertIsNone(developer["retry_idempotency_key"])

    def test_an_active_read_only_dispatch_with_the_same_idempotency_key_is_rejected(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_blocked_and_retried(batch["batch_id"], candidate)
        active = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)[
            "brief"
        ]
        self._edit_batch(
            batch["batch_id"], state="awaiting-approval"
        )  # e.g. a coordinator that lost track of it
        dispatches = self._dispatch_ids(batch["batch_id"])

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._dispatch(batch["batch_id"], "code-review", candidate=candidate)

        self.assertIn("idempotency", caught.exception.message.lower())
        self.assertIn(active["dispatch_id"], caught.exception.message)
        self.assertEqual(self._dispatch_ids(batch["batch_id"]), dispatches)

    def test_a_completed_infrastructure_retry_allows_a_new_dispatch_with_a_new_immutable_id(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        first = self._infra_blocked_and_retried(
            batch["batch_id"], candidate, "transport"
        )
        evidence = self._evidence(first["dispatch_id"])

        second = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)[
            "brief"
        ]

        self.assertNotEqual(second["dispatch_id"], first["dispatch_id"])
        self.assertEqual(second["candidate_commit"], candidate)
        self.assertEqual(
            second["transition"]["previous_dispatch_id"], first["dispatch_id"]
        )
        self.assertEqual(second["transition"]["reason_category"], "transport")
        self.assertEqual(
            self._evidence(first["dispatch_id"]), evidence
        )  # never edited, never replaced

    # 6. the narrow waist: the selected policy is frozen in the brief

    def test_the_brief_freezes_the_selected_policy_and_adds_no_model_tool(self) -> None:
        batch = self._create_batch()
        self._patch_config(
            attention_policy={
                "retry_queue_seconds": 1800,
                "max_infrastructure_retries": 3,
                "stale_dispatch_seconds": 600,
                "heartbeat_interval_seconds": 90,
            },
            approval_ttl_seconds=900,
            extensions={"transport_health": "none"},
        )
        proposal = self._propose(batch["batch_id"], "architect")

        brief = coordinator.create_dispatch(
            self._args(
                transition_digest=proposal["transition_digest"],
                approved_by="Malove",
                approved_at=self._later(-5),
                **self._proposal_fields(batch["batch_id"], "architect", "work", None),
            )
        )["brief"]

        policy = brief["orchestration_policy"]
        self.assertEqual(
            policy["attention"],
            {
                "retry_queue_seconds": 1800,
                "max_infrastructure_retries": 3,
                "stale_dispatch_seconds": 600,
                "heartbeat_interval_seconds": 90,
            },
        )
        self.assertEqual(brief["liveness"]["heartbeat_every_seconds"], 90)
        self.assertEqual(policy["approval_ttl_seconds"], 900)
        self.assertEqual(
            policy["context_pressure"], {"context_limit": 150_000, "warning_ratio": 0.8}
        )
        self.assertEqual(
            policy["extensions"], {kind: "none" for kind in extensions.EXTENSION_KINDS}
        )
        self.assertEqual(
            brief["allowed_tools"], ["Read", "Grep", "Glob", "Bash"]
        )  # no capability adds a model tool

    # 9. ledger integrity of every new record

    def test_new_records_pass_ledger_and_batch_integrity_validation(self) -> None:
        self._patch_config(attention_policy={"retry_queue_seconds": 3600})
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        review = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)[
            "brief"
        ]
        self._start(review["dispatch_id"], checkout=self.worktree)
        self._pressure(review["dispatch_id"], 160_000)
        self._age_heartbeat(review["dispatch_id"], 7200)
        self._attention(batch["batch_id"])
        root = ledger_ops._state_root(self._args(), self.repo)

        LifecycleLedger(root).records_root()  # full generation validation
        current = ledger_ops._load_batch(root, batch["batch_id"])
        coordinator._validate_batch_integrity(root, current)
        coordinator._validate_dispatch(
            self.repo,
            coordinator._config(self.repo),
            root,
            current,
            ledger_ops._load_dispatch(root, review["dispatch_id"]),
        )

        tampered: dict[str, JsonObject] = {
            "context level does not match its numbers": {
                "context_pressure": [{**current["context_pressure"][0], "level": "ok"}]
            },
            "context record was edited": {
                "context_pressure": [
                    {**current["context_pressure"][0], "observed_tokens": 1}
                ]
            },
            "attention without a reason": {
                "needs_attention": True,
                "attention_reason": "",
            },
            "attention flag is not a boolean": {"needs_attention": "yes"},
        }
        for label, fields in tampered.items():
            with self.subTest(label), self.assertRaises(coordinator.CoordinatorError):
                coordinator._validate_batch_integrity(root, {**current, **fields})

    def test_a_brief_whose_digest_does_not_match_its_transition_is_rejected(
        self,
    ) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        root = ledger_ops._state_root(self._args(), self.repo)
        current = ledger_ops._load_batch(root, batch["batch_id"])
        forged = {
            **ledger_ops._load_dispatch(root, brief["dispatch_id"]),
            "transition_digest": "0" * 64,
        }
        current["dispatches"][0]["brief_sha256"] = hashlib.sha256(
            utils._canonical(forged).encode("utf-8")
        ).hexdigest()

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            coordinator._validate_dispatch(
                self.repo, coordinator._config(self.repo), root, current, forged
            )

        self.assertIn("digest", caught.exception.message.lower())

    # -- commit plan pinning and divergence (issue #478) --------------------------------------

    def _plan_batch(
        self, items: list[str], allowed: list[str] | None = None
    ) -> JsonObject:
        plan = self._batch_plan()
        plan["definition_of_done"] = items
        if allowed is not None:
            plan["allowed_path"] = allowed
        with mock.patch.object(self, "_batch_plan", return_value=plan):
            return self._create_batch()

    def _reported_architect(self, batch_id: str) -> None:
        brief = self._dispatch(batch_id, "architect")["brief"]
        self._start(brief["dispatch_id"])
        self._submit(brief["dispatch_id"], self._base_report(brief, "architect"))

    @staticmethod
    def _plan_entry(entry_id: str, covers: list[int]) -> JsonObject:
        return {
            "id": entry_id,
            "summary": f"implement {entry_id}",
            "expected_paths": ["services/**"],
            "covers": covers,
        }

    def _plan_file(self, entries: list[JsonObject]) -> str:
        path = workspace._prepare_agent_inbox(self.repo) / "commit-plan.json"
        path.write_text(json.dumps({"commit_plan": entries}), encoding="utf-8")
        return str(path)

    def _pinned(self, items: list[str], entries: list[JsonObject]) -> str:
        batch_id = cast(str, self._plan_batch(items)["batch_id"])
        self._reported_architect(batch_id)
        self._decide(batch_id, "accept", commit_plan_file=self._plan_file(entries))
        return batch_id

    def _commit_map(self, pairs: list[tuple[str, JsonObject]]) -> list[JsonObject]:
        return [
            {"commit_sha": sha, "plan_entry_id": entry["id"]} for sha, entry in pairs
        ]

    def test_architect_accept_pins_the_commit_plan_into_the_developer_brief(
        self,
    ) -> None:
        entries = [self._plan_entry("first", [1, 2]), self._plan_entry("second", [3])]
        batch_id = self._pinned(["one", "two", "three"], entries)

        record = self._batch_record(batch_id)
        self.assertEqual(record["commit_plan"], entries)
        self.assertEqual(
            record["coordinator_decisions"][-1]["commit_plan_sha256"],
            commit_plan.plan_sha256(entries),
        )
        brief = self._dispatch(batch_id, "developer")["brief"]
        self.assertEqual(brief["commit_plan"], entries)

        self._start(brief["dispatch_id"])
        commits = [self._developer_commit(name)[0] for name in ("a", "b")]
        changed = git_utils._changed_files_between(
            self.repo, record["base_commit"], commits[-1]
        )
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                commits[-1],
                changed,
                commit_map=self._commit_map(list(zip(commits, entries))),
            ),
        )
        self._decide(batch_id, "retry", reason_category="code")
        retry = self._dispatch(batch_id, "developer")["brief"]
        self.assertEqual(retry["transition"]["next_action"], "developer-retry")
        self.assertEqual(retry["commit_plan"], entries)

    def _assert_plan_refused(
        self, entries: list[JsonObject], message: str, remedy: str
    ) -> None:
        batch_id = self._plan_batch(["one", "two", "three"])["batch_id"]
        self._reported_architect(batch_id)

        with self.assertRaisesRegex(coordinator.CoordinatorError, message) as caught:
            self._decide(batch_id, "accept", commit_plan_file=self._plan_file(entries))

        self.assertIn(remedy, caught.exception.remedy)
        record = self._batch_record(batch_id)
        self.assertNotIn("commit_plan", record)
        self.assertNotIn("decision", record["dispatches"][-1])
        self.assertEqual(record["dispatches"][-1]["state"], "reported")

    def test_architect_accept_refuses_a_plan_that_leaves_an_item_uncovered(
        self,
    ) -> None:
        self._assert_plan_refused(
            [self._plan_entry("first", [1]), self._plan_entry("second", [3])],
            r"no entry covers definition-of-done items \[2\]",
            "so every definition-of-done item 1..3 is covered",
        )

    def test_architect_accept_refuses_a_plan_naming_an_unknown_item(self) -> None:
        self._assert_plan_refused(
            [self._plan_entry("first", [1, 2]), self._plan_entry("second", [3, 4])],
            r"covers unknown definition-of-done items \[4\]",
            "name only definition-of-done items 1..3",
        )

    def test_developer_brief_without_a_pinned_plan_keeps_one_entry_per_item(
        self,
    ) -> None:
        items = ["one", "two", "three"]
        batch_id = self._plan_batch(items)["batch_id"]
        self._accepted_architect(batch_id)

        brief = self._dispatch(batch_id, "developer")["brief"]

        self.assertNotIn("commit_plan", self._batch_record(batch_id))
        self.assertEqual(
            brief["commit_plan"],
            [
                {
                    "id": f"step-{index}",
                    "summary": item,
                    "expected_paths": brief["write_paths"],
                    "covers": [index],
                }
                for index, item in enumerate(items, start=1)
            ],
        )

    def test_commit_plan_file_is_refused_outside_an_architect_accept(self) -> None:
        batch_id = self._plan_batch(["one"])["batch_id"]
        self._reported_architect(batch_id)
        plan_file = self._plan_file([self._plan_entry("only", [1])])
        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            "only valid when accepting an architect report",
        ):
            self._decide(
                batch_id, "retry", reason_category="code", commit_plan_file=plan_file
            )
        self._decide(batch_id, "accept")
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        candidate, changed = self._developer_commit("only")
        self._submit(
            brief["dispatch_id"], self._developer_report(brief, candidate, changed)
        )

        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            "only valid when accepting an architect report",
        ):
            self._decide(batch_id, "accept", commit_plan_file=plan_file)

        self.assertNotIn("commit_plan", self._batch_record(batch_id))

    def test_tampered_pinned_plan_is_refused(self) -> None:
        entries = [self._plan_entry("first", [1]), self._plan_entry("second", [2])]
        forged = {
            "edited after the accept": [{**entries[0], "covers": [1, 2]}],
            "never pinned": commit_plan.default_plan(["one", "two"], ["**"]),
        }
        for label, plan in forged.items():
            with self.subTest(label):
                self._reset()
                if label == "never pinned":
                    batch_id = self._plan_batch(["one", "two"])["batch_id"]
                    self._accepted_architect(batch_id)
                else:
                    batch_id = self._pinned(["one", "two"], entries)
                self._edit_batch(batch_id, commit_plan=plan)

                with self.assertRaisesRegex(
                    coordinator.CoordinatorError,
                    "does not match the plan pinned on the architect accept",
                ):
                    self._dispatch(batch_id, "developer")

    FIVE_ITEMS = ["one", "two", "three", "four", "five"]

    def _commits(self, *names: str) -> tuple[list[str], list[str]]:
        commits = [self._developer_commit(name)[0] for name in names]
        changed = git_utils._changed_files_between(
            self.repo, self._batch_record(self.batch_id)["base_commit"], commits[-1]
        )
        return commits, changed

    def _history(self) -> str:
        return _git(self.worktree, "rev-list", "HEAD")

    def _issue_443_divergence(
        self, brief: JsonObject, commits: list[str]
    ) -> JsonObject:
        """Three commits for the five default plan entries: two commits each close two entries."""
        first, second, third = commits
        owners = [first, first, second, third, third]
        return {
            "commit_map": self._commit_map(list(zip(owners, brief["commit_plan"]))),
            "dod_coverage": [
                {"dod_item": item, "commits": [sha]}
                for item, sha in enumerate(owners, start=1)
            ],
            "divergence_justification": (
                "step-1 and step-2 share one schema change; step-4 and step-5 share one "
                "validator, so splitting them would leave a commit that does not pass on its own"
            ),
        }

    def test_issue_443_three_commits_with_a_pinned_three_entry_plan_are_accepted(
        self,
    ) -> None:
        entries = [
            self._plan_entry("contract", [1, 2]),
            self._plan_entry("rules", [3]),
            self._plan_entry("evidence", [4, 5]),
        ]
        batch_id = self._pinned(self.FIVE_ITEMS, entries)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        commits, changed = self._commits("a", "b", "c")
        history = self._history()

        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                commits[-1],
                changed,
                commit_map=self._commit_map(list(zip(commits, entries))),
            ),
        )
        decided = self._decide(batch_id, "accept")

        self.assertEqual(decided["next_action"], "risk-assessment")
        self.assertEqual(self._history(), history)

    def test_issue_443_three_commits_with_the_default_plan_and_a_justified_divergence_are_accepted(
        self,
    ) -> None:
        batch_id = self._plan_batch(self.FIVE_ITEMS)["batch_id"]
        self._accepted_architect(batch_id)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self.assertEqual(len(brief["commit_plan"]), 5)
        self._start(brief["dispatch_id"])
        commits, changed = self._commits("a", "b", "c")
        history = self._history()

        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                commits[-1],
                changed,
                **self._issue_443_divergence(brief, commits),
            ),
        )
        decided = self._decide(batch_id, "accept")

        self.assertEqual(decided["next_action"], "risk-assessment")
        self.assertEqual(self._history(), history)

    def test_divergent_report_without_justification_is_rejected_with_its_remedy(
        self,
    ) -> None:
        batch_id = self._plan_batch(self.FIVE_ITEMS)["batch_id"]
        self._accepted_architect(batch_id)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        commits, changed = self._commits("a", "b", "c")
        divergent = self._issue_443_divergence(brief, commits)
        unjustified = {
            key: value
            for key, value in divergent.items()
            if key != "divergence_justification"
        }

        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            f"commit {commits[0]} merges step-1, step-2.*without a divergence_justification",
        ) as caught:
            self._submit(
                brief["dispatch_id"],
                self._developer_report(brief, commits[-1], changed, **unjustified),
            )

        self.assertIn("merged, split or added and why", caught.exception.remedy)
        submitted = self._submit(
            brief["dispatch_id"],
            self._developer_report(brief, commits[-1], changed, **divergent),
        )
        self.assertEqual(submitted["state"], "reported")

    def test_coverage_claim_contradicting_commit_map_is_rejected_at_submit(
        self,
    ) -> None:
        batch_id = self._plan_batch(self.FIVE_ITEMS)["batch_id"]
        self._accepted_architect(batch_id)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        commits, changed = self._commits("a", "b", "c")
        first, second, third = commits
        plan = brief["commit_plan"]
        divergent = self._issue_443_divergence(brief, commits)
        divergent["commit_map"] = self._commit_map(
            [(first, plan[0]), (first, plan[1]), (second, plan[3]), (third, plan[4])]
        )
        divergent["dod_coverage"][2] = {"dod_item": 3, "commits": [first]}
        divergent["dod_coverage"][3] = {"dod_item": 4, "commits": [second]}

        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            "dod_coverage item 3 claims commits .* covering item 3 \\(step-3\\)",
        ) as caught:
            self._submit(
                brief["dispatch_id"],
                self._developer_report(brief, commits[-1], changed, **divergent),
            )

        self.assertIn("not_covered", caught.exception.remedy)
        divergent["dod_coverage"][2] = {"dod_item": 3, "not_covered": "step-3 deferred"}
        submitted = self._submit(
            brief["dispatch_id"],
            self._developer_report(brief, commits[-1], changed, **divergent),
        )
        self.assertEqual(submitted["state"], "reported")

    def test_developer_retry_rejects_coverage_fields_and_keeps_distinct_entries(
        self,
    ) -> None:
        batch_id = self._plan_batch(["one", "two", "three"])["batch_id"]
        self._accepted_architect(batch_id)
        self._retried_developer_candidate(batch_id, "a", "b", "c")
        retry = self._dispatch(batch_id, "developer")["brief"]
        self.assertEqual(retry["transition"]["next_action"], "developer-retry")
        self._start(retry["dispatch_id"])
        fix, _ = self._developer_commit("fix")
        changed = git_utils._changed_files_between(
            self.repo, self._batch_record(batch_id)["base_commit"], fix
        )
        plan = retry["commit_plan"]

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "belong only to an initial or rebase"
        ):
            self._submit(
                retry["dispatch_id"],
                self._developer_report(
                    retry,
                    fix,
                    changed,
                    commit_map=self._commit_map([(fix, plan[0])]),
                    dod_coverage=[{"dod_item": 1, "commits": [fix]}],
                ),
            )
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "one distinct immutable plan entry"
        ):
            self._submit(
                retry["dispatch_id"],
                self._developer_report(
                    retry,
                    fix,
                    changed,
                    commit_map=self._commit_map([(fix, plan[0]), (fix, plan[1])]),
                ),
            )
        submitted = self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry, fix, changed, commit_map=self._commit_map([(fix, plan[0])])
            ),
        )
        self.assertEqual(submitted["state"], "reported")

    def test_rebase_report_applies_initial_rules_from_the_rebase_target(self) -> None:
        batch_id = self._plan_batch(["one", "two"])["batch_id"]
        self._accepted_architect(batch_id)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        commits, changed = self._commits("a", "b")
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                commits[-1],
                changed,
                commit_map=self._commit_map(list(zip(commits, brief["commit_plan"]))),
            ),
        )
        self._decide(batch_id, "accept")
        self._assess(batch_id, commits[-1], changed)
        (self.repo / "upstream.txt").write_text("upstream\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "upstream")
        _git(self.repo, "push", "origin", "master")
        upstream = _git(self.repo, "rev-parse", "HEAD")
        self._edit_batch(
            batch_id,
            next_action="developer",
            required_next_role="developer",
            retry_candidate_required=True,
            base_rebase_required=True,
            rebase_target_commit=upstream,
        )

        rebase = self._dispatch(batch_id, "developer")["brief"]
        self.assertEqual(rebase["transition"]["next_action"], "developer")
        self._start(rebase["dispatch_id"])
        _git(self.worktree, "rebase", "master")
        rebased = _git(self.worktree, "rev-list", "--reverse", f"{upstream}..HEAD")
        first, second = rebased.splitlines()
        changed = git_utils._changed_files_between(self.repo, upstream, second)
        plan = rebase["commit_plan"]
        merged = self._commit_map(
            [(first, plan[0]), (first, plan[1]), (second, plan[1])]
        )
        divergence = {
            "dod_coverage": [
                {"dod_item": 1, "commits": [first]},
                {"dod_item": 2, "commits": [first, second]},
            ],
            "divergence_justification": "the rebase kept both commits; the first one touches both items",
        }

        with self.assertRaisesRegex(coordinator.CoordinatorError, "no dod_coverage"):
            self._submit(
                rebase["dispatch_id"],
                self._developer_report(rebase, second, changed, commit_map=merged),
            )
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "names commits this dispatch did not create"
        ):
            self._submit(
                rebase["dispatch_id"],
                self._developer_report(
                    rebase,
                    second,
                    changed,
                    commit_map=[*merged, *self._commit_map([(upstream, plan[0])])],
                    **divergence,
                ),
            )
        self._submit(
            rebase["dispatch_id"],
            self._developer_report(
                rebase, second, changed, commit_map=merged, **divergence
            ),
        )
        decided = self._decide(batch_id, "accept")

        self.assertEqual(decided["integration_base_commit"], upstream)
        self.assertFalse(decided["base_rebase_required"])

    def _auto_developer(self, items: list[str]) -> JsonObject:
        """A batch under the patched policy whose clean architect report prepared the developer."""
        batch_id = self._plan_batch(items)["batch_id"]
        architect = self._dispatch(batch_id, "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(
            architect["dispatch_id"], self._base_report(architect, "architect")
        )
        developer_id = self._batch_record(batch_id)["dispatches"][-1]["dispatch_id"]
        brief = coordinator._read_object(
            self._records() / "dispatches" / f"{developer_id}.json", "dispatch"
        )
        self.assertEqual(brief["role"], "developer")
        self._start(developer_id)
        return brief

    def _manual_developer(
        self, items: list[str], allowed: list[str] | None = None
    ) -> JsonObject:
        batch_id = self._plan_batch(items, allowed)["batch_id"]
        self._accepted_architect(batch_id)
        brief: JsonObject = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        return brief

    def _override(self, note: str | None) -> JsonObject:
        return coordinator.decide_batch(
            self._args(
                batch=self.batch_id,
                decision="override-warning",
                note=note,
                **self._approval(),
            )
        )

    def _not_covered_report(self, brief: JsonObject) -> JsonObject:
        """Two commits for three plan entries: the third item is reported as not covered."""
        commits, changed = self._commits("a", "b")
        plan = brief["commit_plan"]
        return self._developer_report(
            brief,
            commits[-1],
            changed,
            commit_map=self._commit_map(list(zip(commits, plan))),
            dod_coverage=[
                {"dod_item": 1, "commits": [commits[0]]},
                {"dod_item": 2, "commits": [commits[1]]},
                {"dod_item": 3, "not_covered": "needs a product decision first"},
            ],
            divergence_justification="step-3 waits for a product decision",
        )

    def _divergent_report(self, brief: JsonObject) -> tuple[JsonObject, list[str]]:
        commits, changed = self._commits("a", "b", "c")
        return (
            self._developer_report(
                brief,
                commits[-1],
                changed,
                **self._issue_443_divergence(brief, commits),
            ),
            commits,
        )

    def _expected_divergence(self, brief: JsonObject, commits: list[str]) -> JsonObject:
        first, _, third = commits
        return {
            "developer_dispatch_id": brief["dispatch_id"],
            "justification": self._issue_443_divergence(brief, commits)[
                "divergence_justification"
            ],
            "merged_commits": [
                {"commit_sha": first, "plan_entry_ids": ["step-1", "step-2"]},
                {"commit_sha": third, "plan_entry_ids": ["step-4", "step-5"]},
            ],
            "split_entries": [],
            "unclosed_entries": [],
        }

    def test_low_risk_divergent_report_with_full_coverage_is_auto_accepted_with_an_audit_record(
        self,
    ) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        brief = self._auto_developer(self.FIVE_ITEMS)
        report, commits = self._divergent_report(brief)

        result = self._submit(brief["dispatch_id"], report)

        self.assertTrue(result["auto_accepted"])
        stored = self._batch_record(self.batch_id)
        decision = stored["coordinator_decisions"][-1]
        self.assertEqual(decision["dispatch_id"], brief["dispatch_id"])
        self.assertEqual(decision["approved_by"], "policy:low_risk")
        self.assertEqual(decision["note"], decisions.AUTO_ACCEPT_RATIONALE)
        self.assertEqual(
            decision["commit_plan_divergence"],
            self._expected_divergence(brief, commits),
        )
        self.assertEqual(
            stored["dispatches"][1]["decision"]["commit_plan_divergence"],
            decision["commit_plan_divergence"],
        )

    def test_low_risk_report_with_a_not_covered_item_waits_for_a_manual_decision(
        self,
    ) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        brief = self._auto_developer(["one", "two", "three"])

        result = self._submit(brief["dispatch_id"], self._not_covered_report(brief))

        self.assertNotIn("auto_accepted", result)
        self.assertNotIn(
            "decision", self._batch_record(self.batch_id)["dispatches"][-1]
        )

    def test_milestone_report_with_a_not_covered_item_is_not_auto_accepted(
        self,
    ) -> None:
        self._patch_config(approval_policy="milestone")
        brief = self._auto_developer(["one", "two", "three"])

        result = self._submit(brief["dispatch_id"], self._not_covered_report(brief))

        self.assertNotIn("auto_accepted", result)
        self.assertNotIn(
            "decision", self._batch_record(self.batch_id)["dispatches"][-1]
        )

    def test_manual_all_divergent_report_still_waits_for_a_human(self) -> None:
        brief = self._manual_developer(self.FIVE_ITEMS)
        report, commits = self._divergent_report(brief)

        result = self._submit(brief["dispatch_id"], report)

        self.assertNotIn("auto_accepted", result)
        self.assertNotIn(
            "decision", self._batch_record(self.batch_id)["dispatches"][-1]
        )
        decided = self._decide(self.batch_id, "accept")
        decision = decided["coordinator_decisions"][-1]
        self.assertEqual(decision["approved_by"], "Malove")
        self.assertEqual(
            decision["commit_plan_divergence"],
            self._expected_divergence(brief, commits),
        )
        self.assertNotIn("dod_not_covered", decision)

    def test_not_covered_report_refuses_accept_and_takes_override_with_a_note(
        self,
    ) -> None:
        brief = self._manual_developer(["one", "two", "three"])
        self._submit(brief["dispatch_id"], self._not_covered_report(brief))

        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            r"definition-of-done items \[3\] are not covered",
        ) as caught:
            self._decide(self.batch_id, "accept")
        self.assertIn("override-warning", caught.exception.remedy)
        self.assertIn("retry", caught.exception.remedy)
        for note in (None, "none", " "):
            with self.subTest(note=note):
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "requires a recorded note"
                ):
                    self._override(note)

        decided = self._override("item 3 is split into a follow-up issue")

        decision = decided["coordinator_decisions"][-1]
        self.assertEqual(decision["decision"], "override-warning")
        self.assertEqual(decision["note"], "item 3 is split into a follow-up issue")
        self.assertEqual(
            decision["dod_not_covered"],
            [{"dod_item": 3, "reason": "needs a product decision first"}],
        )
        self.assertEqual(
            decision["commit_plan_divergence"]["unclosed_entries"], ["step-3"]
        )
        self.assertEqual(decided["next_action"], "risk-assessment")

    def test_not_covered_report_can_be_retried_as_developer_retry(self) -> None:
        brief = self._manual_developer(["one", "two", "three"])
        self._submit(brief["dispatch_id"], self._not_covered_report(brief))

        decided = self._decide(self.batch_id, "retry", reason_category="requirements")

        self.assertEqual(decided["next_action"], "developer-retry")
        self.assertNotIn("commit_plan_divergence", decided["coordinator_decisions"][-1])

    def test_override_warning_without_a_warning_or_an_uncovered_item_is_refused(
        self,
    ) -> None:
        brief = self._manual_developer(self.FIVE_ITEMS)
        report, _ = self._divergent_report(brief)
        self._submit(brief["dispatch_id"], report)

        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            "only a recorded review warning, a not-covered definition-of-done item",
        ):
            self._override("looks fine")

    def _out_of_scope_report(self, brief: JsonObject) -> tuple[JsonObject, str]:
        """A clean one-commit report whose change, services/b.py, lies outside services/a.py."""
        commits, changed = self._commits("b")
        self.assertEqual(changed, ["services/b.py"])
        return self._developer_report(brief, commits[-1], changed), commits[-1]

    def test_out_of_scope_developer_report_is_recorded_and_shown_as_a_scope_warning(
        self,
    ) -> None:
        brief = self._manual_developer(["one"], ["services/a.py"])
        report, _ = self._out_of_scope_report(brief)

        submitted = self._submit(brief["dispatch_id"], report)

        self.assertEqual(submitted["state"], "reported")
        packet = coordinator.decision_packet(
            self._args(batch=self.batch_id, dispatch=None)
        )
        self.assertEqual(packet["scope_warnings"], ["services/b.py"])

    def test_in_scope_developer_report_has_no_scope_warning(self) -> None:
        brief = self._manual_developer(["one"], ["services/**"])
        report, _ = self._out_of_scope_report(brief)
        self._submit(brief["dispatch_id"], report)

        packet = coordinator.decision_packet(
            self._args(batch=self.batch_id, dispatch=None)
        )
        decided = self._decide(self.batch_id, "accept")

        self.assertEqual(packet["scope_warnings"], [])
        self.assertNotIn("scope_warnings", decided["coordinator_decisions"][-1])

    def test_out_of_scope_report_refuses_accept_and_takes_override_with_a_note(
        self,
    ) -> None:
        brief = self._manual_developer(["one"], ["services/a.py"])
        report, candidate = self._out_of_scope_report(brief)
        self._submit(brief["dispatch_id"], report)

        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            r"outside the approved scope \['services/b.py'\]",
        ) as caught:
            self._decide(self.batch_id, "accept")
        self.assertIn("override-warning", caught.exception.remedy)
        for note in (None, "none", " "):
            with self.subTest(note=note):
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "requires a recorded note"
                ):
                    self._override(note)
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "requires approved-by"
        ):
            coordinator.decide_batch(
                self._args(
                    batch=self.batch_id,
                    decision="override-warning",
                    note="b.py is the shared helper",
                    approved_by=None,
                    approved_at=None,
                )
            )

        decided = self._override("b.py is the shared helper")

        decision = decided["coordinator_decisions"][-1]
        self.assertEqual(decision["decision"], "override-warning")
        self.assertEqual(decision["scope_warnings"], ["services/b.py"])
        self.assertEqual(decided["next_action"], "risk-assessment")
        [record] = decided["carried_items"]
        self.assertEqual(record["files"], ["services/b.py"])
        self._assess(self.batch_id, candidate, ["services/b.py"])
        review = self._dispatch(self.batch_id, "code-review", candidate=candidate)
        [item] = review["brief"]["carried_items"]["coordinator-finding"]
        self.assertEqual(item["files"], ["services/b.py"])

    def test_policy_never_auto_accepts_an_out_of_scope_developer_report(self) -> None:
        for policy in ("low_risk", "milestone", "auto"):
            with self.subTest(policy=policy):
                self._reset()
                self._patch_config(approval_policy=policy, low_risk_paths=["**"])
                batch_id = self._plan_batch(["one"], ["services/a.py"])["batch_id"]
                architect = self._dispatch(batch_id, "architect")["brief"]
                self._start(architect["dispatch_id"])
                self._submit(
                    architect["dispatch_id"],
                    self._base_report(architect, "architect"),
                )
                developer_id = self._batch_record(batch_id)["dispatches"][-1][
                    "dispatch_id"
                ]
                brief = coordinator._read_object(
                    self._records() / "dispatches" / f"{developer_id}.json",
                    "dispatch",
                )
                self._start(developer_id)
                report, _ = self._out_of_scope_report(brief)

                result = self._submit(developer_id, report)

                self.assertNotIn("auto_accepted", result)
                self.assertNotIn(
                    "decision", self._batch_record(batch_id)["dispatches"][-1]
                )

    def test_absolute_or_parent_changed_files_are_still_refused_on_submit(self) -> None:
        brief = self._manual_developer(["one"], ["**"])
        report, _ = self._out_of_scope_report(brief)
        report["changed_files"] = ["../escape.py"]

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "must remain inside the repository"
        ):
            self._submit(brief["dispatch_id"], report)

    def _architect_accept_with_plan(
        self, allowed: list[str], entries: list[JsonObject]
    ) -> tuple[JsonObject, JsonObject]:
        batch_id = self._plan_batch(["one", "two", "three"], allowed)["batch_id"]
        self._reported_architect(batch_id)
        packet = coordinator.decision_packet(
            self._args(
                batch=batch_id, dispatch=None, commit_plan_file=self._plan_file(entries)
            )
        )
        decided = self._decide(
            batch_id, "accept", commit_plan_file=self._plan_file(entries)
        )
        return packet, decided

    def test_architect_plan_outside_allowed_paths_is_accepted_with_a_scope_warning(
        self,
    ) -> None:
        entries = [self._plan_entry("first", [1, 2]), self._plan_entry("second", [3])]

        packet, decided = self._architect_accept_with_plan(["docs/**"], entries)

        self.assertEqual(packet["scope_warnings"], ["services/**"])
        self.assertEqual(
            decided["coordinator_decisions"][-1]["scope_warnings"], ["services/**"]
        )
        self.assertEqual(decided["commit_plan"], entries)

    def test_architect_plan_inside_allowed_paths_has_no_scope_warning(self) -> None:
        entries = [self._plan_entry("first", [1, 2]), self._plan_entry("second", [3])]

        packet, decided = self._architect_accept_with_plan(["services/**"], entries)

        self.assertEqual(packet["scope_warnings"], [])
        self.assertNotIn("scope_warnings", decided["coordinator_decisions"][-1])

    def test_decision_packet_shows_dod_coverage_and_divergence(self) -> None:
        brief = self._manual_developer(self.FIVE_ITEMS)
        report, commits = self._divergent_report(brief)
        self._submit(brief["dispatch_id"], report)

        packet = coordinator.decision_packet(
            self._args(batch=self.batch_id, dispatch=None)
        )

        self.assertEqual(packet["dod_coverage"], report["dod_coverage"])
        self.assertEqual(packet["dod_coverage_source"], "report")
        self.assertEqual(
            packet["commit_plan_divergence"], self._expected_divergence(brief, commits)
        )

        self._reset()
        entries = [self._plan_entry("first", [1, 2]), self._plan_entry("second", [3])]
        batch_id = self._pinned(["one", "two", "three"], entries)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        commits, changed = self._commits("a", "b")
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                commits[-1],
                changed,
                commit_map=self._commit_map(list(zip(commits, entries))),
            ),
        )

        packet = coordinator.decision_packet(self._args(batch=batch_id, dispatch=None))

        self.assertEqual(
            packet["dod_coverage"],
            [
                {"dod_item": 1, "commits": [commits[0]]},
                {"dod_item": 2, "commits": [commits[0]]},
                {"dod_item": 3, "commits": [commits[1]]},
            ],
        )
        self.assertEqual(packet["dod_coverage_source"], "derived")
        self.assertIsNone(packet["commit_plan_divergence"])

    def test_code_review_brief_carries_the_accepted_divergence(self) -> None:
        brief = self._manual_developer(self.FIVE_ITEMS)
        self.assertIsNone(brief["commit_plan_divergence"])
        report, commits = self._divergent_report(brief)
        self._submit(brief["dispatch_id"], report)
        self._decide(self.batch_id, "accept")
        changed = report["changed_files"]
        self._assess(self.batch_id, commits[-1], changed)

        review = self._dispatch(self.batch_id, "code-review", candidate=commits[-1])

        expected = self._expected_divergence(brief, commits)
        self.assertEqual(review["brief"]["commit_plan_divergence"], expected)
        packet = coordinator.decision_packet(
            self._args(batch=self.batch_id, dispatch=review["dispatch_id"])
        )
        self.assertEqual(packet["commit_plan_divergence"], expected)
        self.assertIsNone(packet["dod_coverage"])

        self._reset()
        self._create_batch()
        self._accepted_architect(self.batch_id)
        candidate = self._accepted_candidate(self.batch_id)

        review = self._dispatch(self.batch_id, "code-review", candidate=candidate)

        self.assertIsNone(review["brief"]["commit_plan_divergence"])

    def test_code_review_brief_skips_a_developer_retry_for_the_initial_divergence(
        self,
    ) -> None:
        brief = self._manual_developer(self.FIVE_ITEMS)
        report, commits = self._divergent_report(brief)
        self._submit(brief["dispatch_id"], report)
        self._decide(self.batch_id, "accept")
        self._assess(self.batch_id, commits[-1], report["changed_files"])
        self._reported_review(
            self.batch_id,
            commits[-1],
            outcome="blocked",
            blockers="fix needed",
            spec=(
                "blocker",
                [{"severity": "blocker", "summary": "wrong", "evidence": "x.py:1"}],
            ),
        )
        self._decide(self.batch_id, "retry")
        retry = self._dispatch(self.batch_id, "developer")["brief"]
        self._start(retry["dispatch_id"])
        fix, _ = self._developer_commit("fix")
        changed = git_utils._changed_files_between(
            self.repo, self._batch_record(self.batch_id)["base_commit"], fix
        )
        self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                fix,
                changed,
                commit_map=self._commit_map([(fix, retry["commit_plan"][0])]),
            ),
        )
        self._decide(self.batch_id, "accept")
        self._assess(self.batch_id, fix, changed)

        review = self._dispatch(self.batch_id, "code-review", candidate=fix)

        self.assertEqual(
            review["brief"]["commit_plan_divergence"],
            self._expected_divergence(brief, commits),
        )

    def test_brief_without_commit_plan_divergence_still_validates(self) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        root = ledger_ops._state_root(self._args(), self.repo)
        stored = ledger_ops._load_dispatch(root, brief["dispatch_id"])
        legacy = {
            key: value
            for key, value in stored.items()
            if key != "commit_plan_divergence"
        }
        forged = {**stored, "commit_plan_divergence": {"split_entries": []}}

        def validate(record: JsonObject) -> None:
            current = ledger_ops._load_batch(root, batch["batch_id"])
            current["dispatches"][0]["brief_sha256"] = hashlib.sha256(
                utils._canonical(record).encode("utf-8")
            ).hexdigest()
            coordinator._validate_dispatch(
                self.repo, coordinator._config(self.repo), root, current, record
            )

        validate(legacy)
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "commit_plan_divergence must be null"
        ):
            validate(forged)

    # -- carried items (issue #499) -----------------------------------------------------------

    FINDING: JsonObject = {
        "summary": "the retry counter is never reset after a clean run",
        "files": ["services/x.py"],
        "expected_evidence": "a test that runs twice and sees the counter at zero",
    }

    def _findings_file(self, *findings: JsonObject) -> str:
        path = (
            workspace._prepare_agent_inbox(self.repo)
            / f"findings-{uuid.uuid4().hex[:8]}.json"
        )
        document = {"findings": list(findings) or [self.FINDING]}
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def _reported_developer(
        self, batch_id: str, name: str = "x"
    ) -> tuple[JsonObject, str, list[str]]:
        brief: JsonObject = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        candidate, changed = self._developer_commit(name)
        self._submit(
            brief["dispatch_id"], self._developer_report(brief, candidate, changed)
        )
        return brief, candidate, changed

    def _assess_without_triggers(
        self, batch_id: str, candidate: str, changed: list[str]
    ) -> None:
        coordinator.assess_risk(
            self._args(
                batch=batch_id,
                candidate_commit=candidate,
                base_commit=None,
                changed_file=changed,
                developer_trigger=[],
            )
        )

    def _carried(self, brief: JsonObject) -> list[JsonObject]:
        """The flattened items of a brief's carried-items section, in channel order."""
        return [item for items in brief["carried_items"].values() for item in items]

    def test_accept_with_findings_carries_them_into_the_code_review_brief(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer, candidate, changed = self._reported_developer(batch["batch_id"])
        report_sha256 = self._report_evidence(
            batch["batch_id"], developer["dispatch_id"]
        )["report_sha256"]

        decided = self._decide(
            batch["batch_id"], "accept", findings_file=self._findings_file()
        )

        [record] = decided["carried_items"]
        self.assertEqual(record["item_id"], "coordinator-finding-1")
        self.assertEqual(
            record["source"],
            {
                "kind": "coordinator-finding",
                "dispatch_id": developer["dispatch_id"],
                "report_sha256": report_sha256,
                "candidate_commit": candidate,
            },
        )
        self.assertEqual(
            {key: record[key] for key in ("summary", "files", "expected_evidence")},
            self.FINDING,
        )
        self.assertEqual(record["attached_by"], "Malove")
        self.assertEqual(decided["next_action"], "risk-assessment")
        self._assess(batch["batch_id"], candidate, changed)

        review = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)

        item = {
            "item_id": "coordinator-finding-1",
            "source": record["source"],
            **self.FINDING,
        }
        brief = review["brief"]
        self.assertEqual(brief["carried_items"], {"coordinator-finding": [item]})
        self.assertEqual(
            brief["transition"]["carried_items_sha256"],
            operational_guards.carried_items_digest(brief["carried_items"]),
        )
        architect = self._batch_record(batch["batch_id"])["dispatches"][0]
        architect_brief = ledger_ops._load_dispatch(
            ledger_ops._state_root(self._args(), self.repo), architect["dispatch_id"]
        )
        self.assertEqual(architect_brief["carried_items"], {})
        self.assertNotIn("carried_items_sha256", architect_brief["transition"])

    def test_a_findings_file_is_refused_outside_a_developer_accept(self) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        self._submit(brief["dispatch_id"], self._base_report(brief, "architect"))

        with self.assertRaises(coordinator.CoordinatorError) as architect:
            self._decide(
                batch["batch_id"], "accept", findings_file=self._findings_file()
            )
        self._decide(batch["batch_id"], "accept")
        self._reported_developer(batch["batch_id"])
        with self.assertRaises(coordinator.CoordinatorError) as retry:
            self._decide(
                batch["batch_id"],
                "retry",
                reason_category="code",
                findings_file=self._findings_file(),
            )

        for refused in (architect, retry):
            self.assertIn("--findings-file", refused.exception.message)
            self.assertIn("batch carry-over", refused.exception.remedy)
        self.assertNotIn("carried_items", self._batch_record(batch["batch_id"]))

    def test_a_malformed_findings_file_is_refused_before_anything_is_written(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        self._reported_developer(batch["batch_id"])
        before = self._batch_record(batch["batch_id"])

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "findings file is invalid"
        ):
            self._decide(
                batch["batch_id"],
                "accept",
                findings_file=self._findings_file({**self.FINDING, "files": []}),
            )

        self.assertEqual(self._batch_record(batch["batch_id"]), before)

    def test_an_open_coordinator_finding_sends_a_trigger_free_candidate_to_review(
        self,
    ) -> None:
        for carried in (False, True):
            with self.subTest(carried=carried):
                self._reset()
                batch_id = cast(
                    str, self._plan_batch(["add simple marker"])["batch_id"]
                )
                self._accepted_architect(batch_id)
                _, candidate, changed = self._reported_developer(batch_id)
                extra = {"findings_file": self._findings_file()} if carried else {}
                self._decide(batch_id, "accept", **extra)

                self._assess_without_triggers(batch_id, candidate, changed)

                stored = self._batch_record(batch_id)
                self.assertFalse(stored["risk_assessments"][-1]["review_required"])
                self.assertEqual(
                    stored["next_action"], "code-review" if carried else "qa"
                )

    def test_a_carried_items_record_edited_after_it_was_attached_is_refused(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        self._reported_developer(batch["batch_id"])
        self._decide(batch["batch_id"], "accept", findings_file=self._findings_file())
        stored = self._batch_record(batch["batch_id"])
        stored["carried_items"][0]["summary"] = "nothing to see"

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "carried_items record failed"
        ):
            history._validate_operational_batch_fields(stored)

    def test_a_brief_whose_carried_items_left_its_transition_is_refused(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        _, candidate, changed = self._reported_developer(batch["batch_id"])
        self._decide(batch["batch_id"], "accept", findings_file=self._findings_file())
        self._assess(batch["batch_id"], candidate, changed)
        review = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)
        root = ledger_ops._state_root(self._args(), self.repo)
        stored = ledger_ops._load_dispatch(root, review["dispatch_id"])
        legacy = {key: value for key, value in stored.items() if key != "carried_items"}
        legacy["transition"] = {
            key: value
            for key, value in stored["transition"].items()
            if key != "carried_items_sha256"
        }
        legacy["transition_digest"] = operational_guards.transition_digest(
            legacy["transition"]
        )
        legacy["coordinator_approval"] = {
            **stored["coordinator_approval"],
            "transition_digest": legacy["transition_digest"],
        }
        emptied = {**stored, "carried_items": {}}

        def validate(record: JsonObject) -> None:
            current = ledger_ops._load_batch(root, batch["batch_id"])
            current["dispatches"][-1]["brief_sha256"] = hashlib.sha256(
                utils._canonical(record).encode("utf-8")
            ).hexdigest()
            coordinator._validate_dispatch(
                self.repo, coordinator._config(self.repo), root, current, record
            )

        validate(legacy)
        with self.assertRaisesRegex(coordinator.CoordinatorError, "carried items"):
            validate(emptied)

    def test_a_carry_over_after_a_proposal_invalidates_its_approved_digest(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        _, candidate, changed = self._reported_developer(batch["batch_id"])
        self._decide(batch["batch_id"], "accept")
        self._assess(batch["batch_id"], candidate, changed)
        approved = self._propose(batch["batch_id"], "code-review", candidate=candidate)
        self.assertNotIn("carried_items_sha256", approved["transition"])

        self._carry_over(batch["batch_id"])

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "approval digest does not match"
        ):
            self._dispatch(
                batch["batch_id"],
                "code-review",
                candidate=candidate,
                digest=approved["transition_digest"],
            )
        self.assertEqual(
            [
                item["role"]
                for item in self._batch_record(batch["batch_id"])["dispatches"]
            ],
            ["architect", "developer"],
        )
        brief = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)[
            "brief"
        ]
        self.assertNotEqual(brief["transition_digest"], approved["transition_digest"])
        self.assertEqual(
            [item["item_id"] for item in self._carried(brief)],
            ["coordinator-finding-1"],
        )
        self.assertEqual(
            brief["transition"]["carried_items_sha256"],
            operational_guards.carried_items_digest(brief["carried_items"]),
        )

    def _carry_over(self, batch_id: str) -> JsonObject:
        return coordinator.carry_over_findings(
            self._args(batch=batch_id, findings_file=self._findings_file())
        )

    def _auto_accepted_developer(self) -> tuple[str, JsonObject, str]:
        """A trigger-free developer report the patched policy accepted and risk-assessed."""
        brief = self._auto_developer(["add simple marker"])
        candidate, changed = self._developer_commit("x")
        self._submit(
            brief["dispatch_id"], self._developer_report(brief, candidate, changed)
        )
        stored = self._batch_record(self.batch_id)
        developer = next(
            item
            for item in stored["dispatches"]
            if item["dispatch_id"] == brief["dispatch_id"]
        )
        self.assertEqual(
            developer["decision"]["approved_by"], f"policy:{stored['approval_policy']}"
        )
        return self.batch_id, brief, candidate

    def test_carry_over_attaches_findings_after_a_policy_auto_accept(self) -> None:
        self._patch_config(approval_policy="milestone")
        batch_id, developer, candidate = self._auto_accepted_developer()
        self.assertEqual(self._batch_record(batch_id)["next_action"], "qa")

        carried = self._carry_over(batch_id)

        stored = self._batch_record(batch_id)
        self.assertEqual(carried["carried_item_ids"], ["coordinator-finding-1"])
        self.assertEqual(stored["next_action"], "code-review")
        [record] = stored["carried_items"]
        self.assertEqual(record["source"]["dispatch_id"], developer["dispatch_id"])
        self.assertEqual(record["source"]["candidate_commit"], candidate)
        self.assertEqual(record["attached_by"], "policy:carry-over")
        decision = stored["coordinator_decisions"][-1]
        self.assertEqual(
            (decision["decision"], decision["dispatch_id"], decision["approved_by"]),
            ("carry-over", developer["dispatch_id"], "policy:carry-over"),
        )
        self.assertEqual(decisions._developer_retry_count(stored), 0)
        review = self._dispatch(batch_id, "code-review", candidate=candidate)
        self.assertEqual(
            [item["item_id"] for item in self._carried(review["brief"])],
            ["coordinator-finding-1"],
        )

    def test_carry_over_names_a_chain_created_qa_dispatch_until_it_is_cancelled(
        self,
    ) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        batch_id, _, _ = self._auto_accepted_developer()
        qa = self._batch_record(batch_id)["dispatches"][-1]
        self.assertEqual((qa["role"], qa["state"]), ("qa", "approved"))
        before = self._batch_record(batch_id)

        with self.assertRaises(coordinator.CoordinatorError) as refused:
            self._carry_over(batch_id)

        self.assertIn(qa["dispatch_id"], refused.exception.message)
        self.assertIn("dispatch cancel", refused.exception.remedy)
        self.assertEqual(self._batch_record(batch_id), before)
        coordinator.cancel_dispatch(
            self._args(
                dispatch=qa["dispatch_id"],
                reason="carry a coordinator finding into review",
                **self._approval(),
            )
        )

        self._carry_over(batch_id)

        self.assertEqual(self._batch_record(batch_id)["next_action"], "code-review")

    def test_carry_over_is_refused_once_the_code_review_dispatch_exists(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        _, candidate, changed = self._reported_developer(batch["batch_id"])
        self._decide(batch["batch_id"], "accept")
        self._assess(batch["batch_id"], candidate, changed)
        review = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)
        before = self._batch_record(batch["batch_id"])

        with self.assertRaises(coordinator.CoordinatorError) as unsent:
            self._carry_over(batch["batch_id"])
        self._start(review["dispatch_id"], checkout=self.worktree)
        with self.assertRaises(coordinator.CoordinatorError) as running:
            self._carry_over(batch["batch_id"])

        for refused in (unsent, running):
            self.assertIn(review["dispatch_id"], refused.exception.message)
            self.assertIn(candidate, refused.exception.message)
        self.assertIn("dispatch cancel", unsent.exception.remedy)
        self.assertIn("retry", running.exception.remedy)
        self.assertNotIn("dispatch cancel", running.exception.remedy)
        self.assertNotIn("carried_items", self._batch_record(batch["batch_id"]))
        self.assertEqual(
            before["coordinator_decisions"],
            self._batch_record(batch["batch_id"])["coordinator_decisions"],
        )

    def test_carry_over_after_a_batch_resume_reaches_the_next_review_brief(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        _, candidate, changed = self._reported_developer(batch["batch_id"])
        self._decide(batch["batch_id"], "accept")
        self._assess(batch["batch_id"], candidate, changed)
        stalled = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)
        self._start(stalled["dispatch_id"], checkout=self.worktree)
        self._age_heartbeat(stalled["dispatch_id"], 7200)
        event = coordinator.wait_dispatch(
            self._args(
                dispatch=stalled["dispatch_id"],
                timeout=1,
                poll_interval=1,
                stale_after=900,
            )
        )
        self.assertEqual(event["event"], "stale")
        coordinator.resume_batch(
            self._args(batch=batch["batch_id"], reason="worker timed out")
        )
        self.assertEqual(
            self._batch_record(batch["batch_id"])["dispatches"][-1]["state"],
            "abandoned",
        )

        carried = self._carry_over(batch["batch_id"])

        self.assertEqual(carried["carried_item_ids"], ["coordinator-finding-1"])
        review = self._dispatch(batch["batch_id"], "code-review", candidate=candidate)
        self.assertNotEqual(review["dispatch_id"], stalled["dispatch_id"])
        self.assertEqual(
            [item["item_id"] for item in self._carried(review["brief"])],
            ["coordinator-finding-1"],
        )

    def test_carry_over_after_a_decided_review_points_at_the_next_developer_accept(
        self,
    ) -> None:
        candidate = self._carried_review_candidate()
        review = self._reported_review(
            self.batch_id, candidate, carried={"coordinator-finding-1": "open"}
        )
        self._decide(self.batch_id, "retry")
        before = self._batch_record(self.batch_id)

        with self.assertRaises(coordinator.CoordinatorError) as refused:
            self._carry_over(self.batch_id)

        self.assertIn(review["dispatch_id"], refused.exception.message)
        self.assertIn("batch decide --findings-file", refused.exception.remedy)
        self.assertIn("next developer report", refused.exception.remedy)
        self.assertNotIn("deciding that dispatch", refused.exception.remedy)
        self.assertNotIn("dispatch cancel", refused.exception.remedy)
        self.assertEqual(self._batch_record(self.batch_id), before)

    def test_carry_over_needs_an_accepted_developer_report_and_no_pending_one(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])

        with self.assertRaises(coordinator.CoordinatorError) as unaccepted:
            self._carry_over(batch["batch_id"])
        self._reported_developer(batch["batch_id"])
        with self.assertRaises(coordinator.CoordinatorError) as pending:
            self._carry_over(batch["batch_id"])

        self.assertIn("accepted developer", unaccepted.exception.message)
        self.assertIn("--findings-file", pending.exception.remedy)
        self.assertNotIn("carried_items", self._batch_record(batch["batch_id"]))

    def _carried_review_candidate(self) -> str:
        """A developer report accepted by hand with one finding, assessed for review."""
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        _, candidate, changed = self._reported_developer(batch["batch_id"])
        self._decide(batch["batch_id"], "accept", findings_file=self._findings_file())
        self._assess(batch["batch_id"], candidate, changed)
        return candidate

    def test_a_review_that_leaves_a_carried_item_unclosed_is_not_clean(self) -> None:
        for carried in (
            None,
            {"coordinator-finding-1": "unverified"},
            {"coordinator-finding-1": "open"},
        ):
            with self.subTest(carried=carried):
                self._reset()
                candidate = self._carried_review_candidate()
                self._reported_review(self.batch_id, candidate, carried=carried)

                packet = coordinator.decision_packet(
                    self._args(batch=self.batch_id, dispatch=None)
                )
                status = (
                    "omitted" if carried is None else carried["coordinator-finding-1"]
                )
                self.assertEqual(
                    [
                        (row["item_id"], row["status"])
                        for row in packet["carried_items"]
                    ],
                    [("coordinator-finding-1", status)],
                )
                self.assertEqual(packet["carried_items_gap"], ["coordinator-finding-1"])
                with self.assertRaises(coordinator.CoordinatorError) as plain:
                    self._decide(self.batch_id, "accept")
                self.assertIn("coordinator-finding-1", plain.exception.message)
                self.assertIn("override-warning", plain.exception.remedy)
                for note in (None, "none"):
                    with self.assertRaisesRegex(
                        coordinator.CoordinatorError, "requires a recorded note"
                    ):
                        self._override(note)

                decided = self._override("verified by hand against services/x.py")

                decision = decided["coordinator_decisions"][-1]
                self.assertEqual(decision["decision"], "override-warning")
                self.assertEqual(
                    decision["carried_items_gap"], ["coordinator-finding-1"]
                )
                self.assertEqual(decided["next_action"], "qa")
                root = ledger_ops._state_root(self._args(), self.repo)
                self.assertEqual(
                    carried_items.open_coordinator_findings(root, decided), []
                )

    def test_a_review_closing_every_carried_item_is_clean(self) -> None:
        candidate = self._carried_review_candidate()
        self._reported_review(
            self.batch_id, candidate, carried={"coordinator-finding-1": "closed"}
        )

        packet = coordinator.decision_packet(
            self._args(batch=self.batch_id, dispatch=None)
        )
        decided = self._decide(self.batch_id, "accept")

        self.assertEqual(packet["carried_items_gap"], [])
        self.assertEqual(packet["carried_items"][0]["status"], "closed")
        self.assertEqual(decided["next_action"], "qa")
        self.assertNotIn("carried_items_gap", decided["coordinator_decisions"][-1])

    def test_only_a_review_closing_every_carried_item_is_auto_accepted(self) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_paths=["**"])
        for carried, auto in (
            (None, False),
            ({"coordinator-finding-1": "unverified"}, False),
            ({"coordinator-finding-1": "closed"}, True),
        ):
            with self.subTest(carried=carried):
                self._reset()
                brief = self._auto_developer(["route retries by cause"])
                candidate, changed = self._developer_commit("x")
                self._submit(
                    brief["dispatch_id"],
                    self._developer_report(brief, candidate, changed),
                )
                self._carry_over(self.batch_id)

                review = self._reported_review(
                    self.batch_id, candidate, carried=carried
                )

                entry = self._batch_record(self.batch_id)["dispatches"][-1]
                self.assertEqual(entry["dispatch_id"], review["dispatch_id"])
                if auto:
                    self.assertEqual(
                        entry["decision"]["approved_by"], "policy:low_risk"
                    )
                else:
                    self.assertNotIn("decision", entry)

    def test_a_developer_retry_after_review_carries_both_kinds_for_one_retry(
        self,
    ) -> None:
        candidate = self._carried_review_candidate()
        self.assertEqual(
            decisions._developer_retry_count(self._batch_record(self.batch_id)), 0
        )
        finding = {
            "severity": "warning",
            "summary": "the reset path has no test",
            "evidence": "tests/test_x.py:1",
        }
        review = self._reported_review(
            self.batch_id,
            candidate,
            standards=("warning", [finding]),
            carried={"coordinator-finding-1": "open"},
        )

        decided = self._decide(self.batch_id, "retry")
        retry = self._dispatch(self.batch_id, "developer")["brief"]

        self.assertEqual(self._routing(decided)["route"], "fix-forward")
        self.assertEqual(decisions._developer_retry_count(decided), 1)
        section = retry["carried_items"]
        self.assertEqual(
            [item["item_id"] for item in section["coordinator-finding"]],
            ["coordinator-finding-1"],
        )
        self.assertEqual(
            section["review-finding"],
            [
                {
                    "item_id": "review-finding-1",
                    "source": {
                        "kind": "review-finding",
                        "dispatch_id": review["dispatch_id"],
                        "report_sha256": self._report_evidence(
                            self.batch_id, review["dispatch_id"]
                        )["report_sha256"],
                        "axis": "standards",
                        "severity": "warning",
                    },
                    "summary": "the reset path has no test",
                    "files": [],
                    "expected_evidence": "tests/test_x.py:1",
                }
            ],
        )
        self.assertEqual(
            retry["transition"]["carried_items_sha256"],
            operational_guards.carried_items_digest(section),
        )

    def test_a_developer_retry_before_review_carries_no_review_findings(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        self._retried_developer_candidate(batch["batch_id"], "x")

        retry = self._dispatch(batch["batch_id"], "developer")["brief"]

        self.assertEqual(retry["carried_items"], {})

    # -- fix-forward: the closed list of a developer-retry (issue #503) ------------------------

    REVIEW_WARNING = {
        "severity": "warning",
        "summary": "the reset path has no test",
        "evidence": "tests/test_x.py:1",
    }
    RETRY_ITEMS = ["coordinator-finding-1", "review-finding-1"]

    def _reported_review_with_items(self) -> str:
        """A review of a candidate carrying one coordinator finding, which it leaves open, with
        one Standards warning: a retry routes both items to the developer. Returns the candidate."""
        candidate = self._carried_review_candidate()
        self._reported_review(
            self.batch_id,
            candidate,
            standards=("warning", [dict(self.REVIEW_WARNING)]),
            carried={"coordinator-finding-1": "open"},
        )
        return candidate

    def _fix_forward_brief(self) -> tuple[str, JsonObject]:
        """The developer-retry brief of a retried review with items, and the reviewed candidate."""
        candidate = self._reported_review_with_items()
        self._decide(self.batch_id, "retry")
        return candidate, self._dispatch(self.batch_id, "developer")["brief"]

    def test_a_review_retry_records_the_closed_item_list_its_developer_brief_carries(
        self,
    ) -> None:
        self._reported_review_with_items()
        preview = self._packet()["route_preview"]["retry"]

        decided = self._decide(self.batch_id, "retry")
        retry = self._dispatch(self.batch_id, "developer")["brief"]

        routing = dict(self._routing(decided))
        routing.pop("decided_at")
        self.assertEqual(routing, preview)
        self.assertEqual(routing["retry_item_ids"], self.RETRY_ITEMS)
        self.assertEqual(
            carried_items.section_item_ids(retry["carried_items"]), self.RETRY_ITEMS
        )
        self.assertEqual(
            [item["source"]["kind"] for item in self._carried(retry)],
            ["coordinator-finding", "review-finding"],
        )

    def test_a_retry_with_carried_items_records_the_fix_forward_route(self) -> None:
        candidate = self._reported_review_with_items()
        preview = self._packet()["route_preview"]["retry"]

        decided = self._decide(self.batch_id, "retry")

        routing = self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="code",
            candidate=candidate,
            route="fix-forward",
        )
        self.assertEqual(preview["route"], "fix-forward")
        self.assertIn(
            "fix-forward: new commits on top of the candidate close the carried items "
            "coordinator-finding-1, review-finding-1 without rewriting history",
            routing["rationale"],
        )
        self.assertEqual(decided["required_next_role"], "developer")
        self.assertEqual(decisions._developer_retry_count(decided), 1)
        self.assertEqual(
            self._decision_audits(self.batch_id)[-1]["route"], "fix-forward"
        )

    def test_a_retry_without_carried_items_keeps_the_developer_retry_route(
        self,
    ) -> None:
        for stage in ("developer", "code-review"):
            with self.subTest(stage=stage):
                self._reset()
                batch_id = cast(str, self._create_batch()["batch_id"])
                self._accepted_architect(batch_id)
                if stage == "developer":
                    self._reported_developer(batch_id)
                else:
                    reviewed = self._accepted_candidate(batch_id)
                    self._reported_review(batch_id, reviewed)

                decided = self._decide(batch_id, "retry", reason_category="code")

                routing = self._routing(decided)
                self.assertEqual(
                    (routing["route"], routing["retry_item_ids"]),
                    ("developer-retry", []),
                )
                self.assertNotIn("fix-forward", routing["rationale"])

    def test_a_retried_developer_report_hands_the_same_closed_list_to_the_next_retry(
        self,
    ) -> None:
        self._patch_config(retry_policy={"max_developer_retries": 2})
        reviewed, first = self._fix_forward_brief()
        self._start(first["dispatch_id"])
        fix, _ = self._developer_commit("fix")
        base = self._batch_record(self.batch_id)["base_commit"]
        self._submit(
            first["dispatch_id"],
            self._developer_report(
                first,
                fix,
                git_utils._changed_files_between(self.repo, base, fix),
                commit_map=self._commit_map([(fix, first["commit_plan"][0])]),
            ),
        )

        decided = self._decide(self.batch_id, "retry", reason_category="code")
        second = self._dispatch(self.batch_id, "developer")["brief"]

        self.assertEqual(self._routing(decided)["retry_item_ids"], self.RETRY_ITEMS)
        self.assertEqual(decisions._developer_retry_count(decided), 2)
        self.assertEqual(second["carried_items"], first["carried_items"])
        self.assertEqual(second["snapshot_commit"], fix)

        # The next attempt maps an item the retried attempt closed to that attempt's commit.
        self._start(second["dispatch_id"])
        again, changed = self._developer_commit("again")
        report = self._developer_report(
            second,
            again,
            changed,
            commit_map=self._commit_map([(again, second["commit_plan"][0])]),
            carried_item_closure=[
                {"item_id": "coordinator-finding-1", "commits": [fix]},
                {"item_id": "review-finding-1", "commits": [again]},
            ],
        )
        root = ledger_ops._state_root(self._args(), self.repo)
        batch = self._batch_record(self.batch_id)
        self.assertEqual(
            reports._closure_base(self.repo, root, batch, second), reviewed
        )
        with mock.patch.object(
            carried_items, "closure_snapshots", return_value=[again, fix]
        ):
            # A chain snapshot the dispatch's own snapshot does not descend from is skipped.
            self.assertEqual(reports._closure_base(self.repo, root, batch, second), fix)
        reviewed_closure = [
            {"item_id": "coordinator-finding-1", "commits": [reviewed]},
            {"item_id": "review-finding-1", "commits": [again]},
        ]
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "nor an earlier attempt of its retry chain"
        ):
            self._submit(
                second["dispatch_id"],
                {**report, "carried_item_closure": reviewed_closure},
            )
        self._submit(second["dispatch_id"], report)
        self.assertEqual(self._packet()["carried_items_gap"], [])
        accepted = self._decide(self.batch_id, "accept")
        self.assertEqual(accepted["next_action"], "risk-assessment")

    def test_a_developer_tooling_retry_restarts_with_the_closed_list_of_the_blocked_brief(
        self,
    ) -> None:
        _, first = self._fix_forward_brief()
        self._start(first["dispatch_id"])
        fix, _ = self._developer_commit("fix")
        base = self._batch_record(self.batch_id)["base_commit"]
        self._submit(
            first["dispatch_id"],
            self._developer_report(
                first,
                fix,
                git_utils._changed_files_between(self.repo, base, fix),
                commit_map=self._commit_map([(fix, first["commit_plan"][0])]),
                outcome="blocked",
                blockers="a hook blocked a legitimate check command",
                checks_run=self._checks(first, "not-run"),
                tooling_blocker=dict(TOOLING_BLOCKER),
            ),
        )

        decided = self._decide(self.batch_id, "retry")
        restart = self._dispatch(self.batch_id, "developer")["brief"]

        routing = self._routing(decided)
        self.assertEqual(
            (routing["route"], routing["retry_item_ids"]),
            ("tooling-retry", self.RETRY_ITEMS),
        )
        self.assertEqual(decisions._developer_retry_count(decided), 1)
        self.assertEqual(restart["carried_items"], first["carried_items"])

        # The restart maps an item the blocked attempt closed to that attempt's commit.
        self._start(restart["dispatch_id"])
        again, changed = self._developer_commit("again")
        submitted = self._submit(
            restart["dispatch_id"],
            self._developer_report(
                restart,
                again,
                changed,
                commit_map=self._commit_map([(again, restart["commit_plan"][0])]),
                carried_item_closure=[
                    {"item_id": "coordinator-finding-1", "commits": [fix]},
                    {"item_id": "review-finding-1", "commits": [fix, again]},
                ],
            ),
        )
        self.assertEqual(submitted["state"], "reported")
        self.assertEqual(self._packet()["carried_items_gap"], [])

    def _fix_report(
        self, brief: JsonObject, **overrides: object
    ) -> tuple[str, JsonObject]:
        """One fix commit on top of the brief's snapshot and the developer-retry report mapping it
        to the first plan entry; by default it closes every carried item on that commit."""
        fix, _ = self._developer_commit("fix")
        base = self._batch_record(self.batch_id)["base_commit"]
        return fix, self._developer_report(
            brief,
            fix,
            git_utils._changed_files_between(self.repo, base, fix),
            commit_map=self._commit_map([(fix, brief["commit_plan"][0])]),
            **overrides,
        )

    def _pending_auto_policy(self) -> str | None:
        """The policy an ``auto`` approval policy would accept the pending report under."""
        root = ledger_ops._state_root(self._args(), self.repo)
        batch = self._batch_record(self.batch_id)
        entry = batch["dispatches"][-1]
        return decisions._auto_accept_policy(
            {"approval_policy": "auto"},
            {**batch, "approval_policy": "auto"},
            ledger_ops._load_dispatch(root, entry["dispatch_id"]),
            history._pending_report(root, batch, entry),
        )

    def test_a_retry_report_closing_every_carried_item_is_clean(self) -> None:
        _, brief = self._fix_forward_brief()
        self._start(brief["dispatch_id"])
        fix, report = self._fix_report(brief)

        self._submit(brief["dispatch_id"], report)
        packet = self._packet()

        self.assertEqual(packet["carried_items_gap"], [])
        self.assertEqual(
            [
                (row["item_id"], row["status"], row["evidence"])
                for row in packet["carried_items"]
            ],
            [(item_id, "closed", fix) for item_id in self.RETRY_ITEMS],
        )
        self.assertEqual(self._pending_auto_policy(), "auto")
        decided = self._decide(self.batch_id, "accept")
        self.assertNotIn("carried_items_gap", decided["coordinator_decisions"][-1])

    def test_a_retry_report_must_map_each_carried_item_once_to_its_own_commits(
        self,
    ) -> None:
        reviewed, brief = self._fix_forward_brief()
        self._start(brief["dispatch_id"])
        fix, report = self._fix_report(brief)
        closed = {"item_id": "coordinator-finding-1", "commits": [fix]}
        review_closed = {"item_id": "review-finding-1", "commits": [fix]}
        for closure, refusal in (
            (None, "carried_item_closure is missing"),
            ({"item_id": "review-finding-1"}, "must be a list"),
            ([closed, {"item_id": "review-finding-1"}], "must be {item_id, commits}"),
            (
                [
                    closed,
                    review_closed,
                    {"item_id": "review-finding-2", "commits": [fix]},
                ],
                "'review-finding-2', which the brief did not carry",
            ),
            ([closed, review_closed, closed], "or the report already mapped"),
            ([closed], r"no record for carried items \['review-finding-1'\]"),
            (
                [closed, {"item_id": "review-finding-1", "not_closed": " "}],
                "review-finding-1 is not_closed without a reason",
            ),
            (
                [closed, {"item_id": "review-finding-1", "commits": []}],
                "non-empty list of commit SHAs",
            ),
            (
                [closed, {"item_id": "review-finding-1", "commits": ["f" * 40]}],
                "not the hexadecimal SHA of a commit",
            ),
            (
                [closed, {"item_id": "review-finding-1", "commits": [reviewed]}],
                "names commits that neither this dispatch nor an earlier attempt",
            ),
        ):
            with self.subTest(refusal=refusal):
                invalid = {**report, "carried_item_closure": closure}
                if closure is None:
                    del invalid["carried_item_closure"]
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, refusal
                ) as raised:
                    self._submit(brief["dispatch_id"], invalid)
                self.assertTrue(raised.exception.remedy.strip())
                if closure == [closed]:
                    self.assertIn("review-finding-1", raised.exception.remedy)

        self.assertEqual(
            self._submit(brief["dispatch_id"], report)["state"], "reported"
        )

    def test_a_closure_belongs_only_to_a_retry_report_whose_brief_carried_items(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        initial = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(initial["dispatch_id"])
        candidate, changed = self._developer_commit("x")
        closure = [{"item_id": "coordinator-finding-1", "commits": [candidate]}]
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "belongs only to a developer-retry report"
        ):
            self._submit(
                initial["dispatch_id"],
                self._developer_report(
                    initial, candidate, changed, carried_item_closure=closure
                ),
            )
        self._submit(
            initial["dispatch_id"], self._developer_report(initial, candidate, changed)
        )
        self._decide(batch["batch_id"], "retry", reason_category="code")
        retry = self._dispatch(batch["batch_id"], "developer")["brief"]
        self.assertEqual(retry["carried_items"], {})
        self._start(retry["dispatch_id"])
        fix, report = self._fix_report(retry)

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "belongs only to a developer-retry report"
        ):
            self._submit(
                retry["dispatch_id"],
                {**report, "carried_item_closure": []},
            )
        self.assertEqual(
            self._submit(retry["dispatch_id"], report)["state"], "reported"
        )

    def test_a_retry_report_recorded_without_its_closure_is_decided_as_a_carried_gap(
        self,
    ) -> None:
        """A completed developer-retry report recorded before #503 has no carried_item_closure.
        A new submit of it is refused, but the recorded report is still decided: every carried item
        is an omitted gap, so accept is refused while override-warning and abandon are not."""
        for decision, extra in (
            ("override-warning", {"note": "the items are closed by the fix commit"}),
            ("abandon", {"reason": "superseded by a fresh plan"}),
        ):
            with self.subTest(decision=decision):
                self._reset()
                _, brief = self._fix_forward_brief()
                self._start(brief["dispatch_id"])
                _, report = self._fix_report(brief)
                del report["carried_item_closure"]
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "carried_item_closure is missing"
                ):
                    self._submit(brief["dispatch_id"], report)
                with mock.patch.object(carried_items, "require_closure"):
                    self._submit(brief["dispatch_id"], report)

                packet = self._packet()
                self.assertEqual(packet["carried_items_gap"], self.RETRY_ITEMS)
                self.assertEqual(
                    [
                        (row["status"], row["evidence"])
                        for row in packet["carried_items"]
                    ],
                    [("omitted", None)] * len(self.RETRY_ITEMS),
                )
                self.assertIsNone(self._pending_auto_policy())
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "are not closed"
                ):
                    self._decide(self.batch_id, "accept")
                decided = self._decide(self.batch_id, decision, **extra)

                self.assertEqual(
                    decided["coordinator_decisions"][-1]["decision"], decision
                )
                if decision == "override-warning":
                    self.assertEqual(
                        decided["coordinator_decisions"][-1]["carried_items_gap"],
                        self.RETRY_ITEMS,
                    )
                else:
                    self.assertEqual(decided["state"], "abandoned")

    def test_a_closure_names_only_commits_of_its_retry_chain_without_a_commit_plan(
        self,
    ) -> None:
        """A developer-retry brief without a commit_plan (before #478) still has every closure
        commit resolved and checked against the commits its retry chain created."""
        reviewed, brief = self._fix_forward_brief()
        self._start(brief["dispatch_id"])
        fix, report = self._fix_report(brief)
        legacy = {key: value for key, value in brief.items() if key != "commit_plan"}
        del report["commit_map"]
        base = self._batch_record(self.batch_id)["base_commit"]
        role = {"mode": "write", "name": "developer"}
        for commits, refusal in (
            ([reviewed], "nor an earlier attempt of its retry chain"),
            (["f" * 40], "not the hexadecimal SHA of a commit"),
        ):
            with self.subTest(refusal=refusal):
                foreign = {
                    **report,
                    "carried_item_closure": [
                        {"item_id": "coordinator-finding-1", "commits": commits},
                        {"item_id": "review-finding-1", "commits": [fix]},
                    ],
                }
                with self.assertRaisesRegex(coordinator.CoordinatorError, refusal):
                    reports._validate_report(foreign, legacy, role, self.repo, base)

        reports._validate_report(report, legacy, role, self.repo, base)

    def test_a_carried_item_left_not_closed_keeps_the_retry_report_unclean(
        self,
    ) -> None:
        _, brief = self._fix_forward_brief()
        self._start(brief["dispatch_id"])
        reason = "the reset path lies outside the approved zone"
        fix, report = self._fix_report(brief)
        report["carried_item_closure"] = [
            {"item_id": "coordinator-finding-1", "commits": [fix]},
            {"item_id": "review-finding-1", "not_closed": reason},
        ]

        self._submit(brief["dispatch_id"], report)
        packet = self._packet()

        self.assertEqual(packet["carried_items_gap"], ["review-finding-1"])
        self.assertEqual(
            [(row["status"], row["evidence"]) for row in packet["carried_items"]],
            [("closed", fix), ("open", reason)],
        )
        self.assertIsNone(self._pending_auto_policy())
        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            r"carried items \['review-finding-1'\] are not closed",
        ):
            self._decide(self.batch_id, "accept")
        for note in (None, "none"):
            with self.subTest(note=note):
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "requires a recorded note"
                ):
                    self._decide(self.batch_id, "override-warning", note=note)

        decided = self._decide(
            self.batch_id, "override-warning", note="the reset path moves to #999"
        )

        decision = decided["coordinator_decisions"][-1]
        self.assertEqual(decision["carried_items_gap"], ["review-finding-1"])
        self.assertEqual(decided["next_action"], "risk-assessment")

    def test_a_retry_that_rewrites_the_snapshot_history_is_refused(self) -> None:
        for rewrite in ("amend", "reset"):
            with self.subTest(rewrite=rewrite):
                self._reset()
                snapshot, brief = self._fix_forward_brief()
                self.assertEqual(brief["snapshot_commit"], snapshot)
                self._start(brief["dispatch_id"])
                if rewrite == "amend":
                    (self.worktree / "services" / "x.py").write_text(
                        "VALUE = 'amended'\n", encoding="utf-8"
                    )
                    _git(self.worktree, "add", "-A")
                    _git(self.worktree, "commit", "--amend", "--no-edit")
                    head = _git(self.worktree, "rev-parse", "HEAD")
                    base = self._batch_record(self.batch_id)["base_commit"]
                    report = self._developer_report(
                        brief,
                        head,
                        git_utils._changed_files_between(self.repo, base, head),
                        commit_map=self._commit_map([(head, brief["commit_plan"][0])]),
                    )
                else:
                    _git(self.worktree, "reset", "--hard", "HEAD~1")
                    head, report = self._fix_report(brief)

                with self.assertRaises(coordinator.CoordinatorError) as raised:
                    self._submit(brief["dispatch_id"], report)

                self.assertIn(
                    f"candidate {head} does not descend from snapshot_commit {snapshot}",
                    raised.exception.message,
                )
                self.assertIn("without amend or squash", raised.exception.remedy)
                self.assertIn("git reflog", raised.exception.remedy)

    def test_a_retry_of_a_rebase_report_may_rebuild_history_onto_the_rebase_target(
        self,
    ) -> None:
        batch_id = self._plan_batch(["one", "two"])["batch_id"]
        self._accepted_architect(batch_id)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        commits, changed = self._commits("a", "b")
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                commits[-1],
                changed,
                commit_map=self._commit_map(list(zip(commits, brief["commit_plan"]))),
            ),
        )
        self._decide(batch_id, "accept")
        self._assess(batch_id, commits[-1], changed)
        (self.repo / "upstream.txt").write_text("upstream\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "upstream")
        _git(self.repo, "push", "origin", "master")
        upstream = _git(self.repo, "rev-parse", "HEAD")
        self._edit_batch(
            batch_id,
            next_action="developer",
            required_next_role="developer",
            retry_candidate_required=True,
            base_rebase_required=True,
            rebase_target_commit=upstream,
        )
        rebase = self._dispatch(batch_id, "developer")["brief"]
        self._start(rebase["dispatch_id"])
        _git(self.worktree, "rebase", "master")
        first, second = _git(
            self.worktree, "rev-list", "--reverse", f"{upstream}..HEAD"
        ).splitlines()
        plan = rebase["commit_plan"]
        self._submit(
            rebase["dispatch_id"],
            self._developer_report(
                rebase,
                second,
                git_utils._changed_files_between(self.repo, upstream, second),
                commit_map=self._commit_map([(first, plan[0]), (second, plan[1])]),
            ),
        )
        self._decide(batch_id, "retry", reason_category="code")
        retry = self._dispatch(batch_id, "developer")["brief"]
        self.assertEqual(retry["snapshot_commit"], second)
        self._start(retry["dispatch_id"])
        (self.worktree / "services" / "b.py").write_text(
            "VALUE = 'rebuilt'\n", encoding="utf-8"
        )
        _git(self.worktree, "add", "-A")
        _git(self.worktree, "commit", "--amend", "--no-edit")
        rebuilt = _git(self.worktree, "rev-parse", "HEAD")
        self.assertFalse(git_utils._git_is_ancestor(self.repo, second, rebuilt))

        submitted = self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                rebuilt,
                git_utils._changed_files_between(self.repo, upstream, rebuilt),
                commit_map=self._commit_map([(first, plan[0]), (rebuilt, plan[1])]),
            ),
        )

        self.assertEqual(submitted["state"], "reported")

    # -- legacy stale-base records (base_rebase_required, before ADR 0014 removed the check) ----

    def _push_upstream(self, name: str = "upstream") -> str:
        """One commit pushed to origin/master after the batch pinned its integration base."""
        (self.repo / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", name)
        _git(self.repo, "push", "origin", "master")
        return _git(self.repo, "rev-parse", "HEAD")

    def _stale_base_rebase_report(self) -> tuple[str, str, JsonObject, list[str]]:
        """An accepted two-commit candidate on a legacy stale-base record (written before ADR 0014
        removed the base freshness check), and the developer rebase report onto the recorded tip,
        not decided yet. Returns the batch, the tip, the rebase brief and the rebased commits."""
        batch_id = cast(str, self._plan_batch(["one", "two"])["batch_id"])
        self._accepted_architect(batch_id)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        commits, changed = self._commits("a", "b")
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                commits[-1],
                changed,
                commit_map=self._commit_map(list(zip(commits, brief["commit_plan"]))),
            ),
        )
        self._decide(batch_id, "accept")
        self._assess(batch_id, commits[-1], changed)
        upstream = self._push_upstream()
        self._edit_batch(
            batch_id,
            next_action="developer",
            required_next_role="developer",
            retry_candidate_required=True,
            base_rebase_required=True,
            rebase_target_commit=upstream,
        )
        rebase = self._dispatch(batch_id, "developer")["brief"]
        self._start(rebase["dispatch_id"])
        _git(self.worktree, "rebase", "master")
        rebased = _git(
            self.worktree, "rev-list", "--reverse", f"{upstream}..HEAD"
        ).splitlines()
        plan = rebase["commit_plan"]
        self._submit(
            rebase["dispatch_id"],
            self._developer_report(
                rebase,
                rebased[-1],
                git_utils._changed_files_between(self.repo, upstream, rebased[-1]),
                commit_map=self._commit_map(list(zip(rebased, plan))),
            ),
        )
        return batch_id, upstream, rebase, rebased

    def test_a_retry_of_a_rebase_report_is_measured_from_the_target_and_accept_pins_it(
        self,
    ) -> None:
        batch_id, upstream, rebase, rebased = self._stale_base_rebase_report()
        # The legacy stale-base rebase is measured from the batch target, never from a brief target.
        self.assertIsNone(rebase["rebase_target_commit"])
        self.assertNotIn("rebase_target_sha", rebase["transition"])
        pinned = self._batch_record(batch_id)["integration_base_commit"]
        decided = self._decide(batch_id, "retry", reason_category="code")
        retry = self._dispatch(batch_id, "developer")["brief"]
        # The rebased candidate already contains the tip: no rebase target is proposed.
        self.assertEqual(self._routing(decided)["route"], "developer-retry")
        self.assertIsNone(retry["rebase_target_commit"])
        self._start(retry["dispatch_id"])
        (self.worktree / "services" / "b.py").write_text(
            "VALUE = 'rebuilt'\n", encoding="utf-8"
        )
        _git(self.worktree, "add", "-A")
        _git(self.worktree, "commit", "--amend", "--no-edit")
        rebuilt = _git(self.worktree, "rev-parse", "HEAD")
        plan = retry["commit_plan"]
        commit_map = self._commit_map([(rebased[0], plan[0]), (rebuilt, plan[1])])

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "changed_files must exactly match commit_sha"
        ):
            self._submit(
                retry["dispatch_id"],
                self._developer_report(
                    retry,
                    rebuilt,
                    git_utils._changed_files_between(self.repo, pinned, rebuilt),
                    commit_map=commit_map,
                ),
            )
        changed = git_utils._changed_files_between(self.repo, upstream, rebuilt)
        self._submit(
            retry["dispatch_id"],
            self._developer_report(retry, rebuilt, changed, commit_map=commit_map),
        )
        accepted = self._decide(batch_id, "accept")

        self.assertEqual(accepted["integration_base_commit"], upstream)
        self.assertFalse(accepted["base_rebase_required"])
        self.assertNotIn("rebase_target_commit", accepted)
        self._assess(batch_id, rebuilt, changed)
        review = self._dispatch(batch_id, "code-review", candidate=rebuilt)["brief"]
        self.assertEqual(review["candidate_commit"], rebuilt)

    # -- the rebase-fix-forward route (issue #504) --------------------------------------------

    def _moved_base_retry(self) -> tuple[str, str, str, str]:
        """A two-commit developer report retried after origin/master moved past the batch base,
        with no developer-retry dispatch yet. Returns the batch, the retried candidate, the pinned
        integration base and the tip."""
        batch_id = cast(str, self._plan_batch(["one", "two"])["batch_id"])
        self._accepted_architect(batch_id)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        commits, changed = self._commits("a", "b")
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                commits[-1],
                changed,
                commit_map=self._commit_map(list(zip(commits, brief["commit_plan"]))),
            ),
        )
        pinned = self._batch_record(batch_id)["integration_base_commit"]
        upstream = self._push_upstream()
        self._decide(batch_id, "retry", reason_category="code")
        return batch_id, commits[-1], pinned, upstream

    def test_a_developer_retry_on_a_moved_base_proposes_the_tip_as_its_rebase_target(
        self,
    ) -> None:
        batch_id = cast(str, self._create_batch()["batch_id"])
        self._accepted_architect(batch_id)
        self._reported_developer(batch_id)
        pinned = self._batch_record(batch_id)["integration_base_commit"]
        unmoved = self._packet(reason_category="code")["route_preview"]["retry"]
        upstream = self._push_upstream()

        preview = self._packet(reason_category="code")["route_preview"]["retry"]
        decided = self._decide(batch_id, "retry", reason_category="code")

        self.assertEqual(unmoved["route"], "developer-retry")
        self.assertNotIn("rebase_target_commit", unmoved)
        routing = self._assert_route(
            decided,
            role="developer",
            action="developer-retry",
            category="code",
            candidate=None,
            route="rebase-fix-forward",
        )
        recorded = dict(routing)
        recorded.pop("decided_at")
        self.assertEqual(recorded, preview)
        self.assertEqual(
            (
                routing["rebase_target_commit"],
                routing["integration_base_commit"],
                routing["retry_item_ids"],
            ),
            (upstream, pinned, []),
        )
        self.assertIn(
            f"rebase-fix-forward: origin/master moved from {pinned} to {upstream}",
            routing["rationale"],
        )
        # The decision only proposes the target: the batch base stays pinned until an accept.
        self.assertEqual(decided["integration_base_commit"], pinned)
        self.assertEqual(decisions._developer_retry_count(decided), 1)
        self.assertEqual(
            self._decision_audits(batch_id)[-1]["route"], "rebase-fix-forward"
        )

    def test_a_review_retry_on_a_moved_base_rebases_the_reviewed_candidate_with_its_items(
        self,
    ) -> None:
        self._reported_review_with_items()
        upstream = self._push_upstream()

        decided = self._decide(self.batch_id, "retry")

        routing = self._routing(decided)
        self.assertEqual(
            (
                routing["route"],
                routing["retry_item_ids"],
                routing["rebase_target_commit"],
            ),
            ("rebase-fix-forward", self.RETRY_ITEMS, upstream),
        )
        self.assertIn("It is a fix-forward", routing["rationale"])

    def test_a_developer_tooling_retry_on_a_moved_base_proposes_no_rebase_target(
        self,
    ) -> None:
        batch_id = cast(str, self._create_batch()["batch_id"])
        self._accepted_architect(batch_id)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        candidate, changed = self._developer_commit("x")
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                candidate,
                changed,
                outcome="blocked",
                blockers="a hook blocked a legitimate check command",
                checks_run=self._checks(brief, "not-run"),
                tooling_blocker=dict(TOOLING_BLOCKER),
            ),
        )
        self._push_upstream()

        decided = self._decide(batch_id, "retry")
        restart = self._dispatch(batch_id, "developer")["brief"]

        routing = self._routing(decided)
        self.assertEqual(routing["route"], "tooling-retry")
        self.assertNotIn("rebase_target_commit", routing)
        self.assertIsNone(restart["rebase_target_commit"])

    def test_a_retry_that_cannot_fetch_the_integration_ref_is_refused_with_a_remedy(
        self,
    ) -> None:
        batch_id = cast(str, self._create_batch()["batch_id"])
        self._accepted_architect(batch_id)
        self._reported_developer(batch_id)
        _git(self.repo, "remote", "set-url", "origin", str(self.tmp / "missing.git"))

        preview = self._packet(reason_category="code")["route_preview"]["retry"]
        with self.assertRaises(coordinator.CoordinatorError) as raised:
            self._decide(batch_id, "retry", reason_category="code")

        self.assertIsNone(preview["route"])
        self.assertIn("must know whether origin/master moved", preview["refused"])
        self.assertEqual(preview["refused"], raised.exception.message)
        self.assertIn("restore access to origin/master", raised.exception.remedy)
        self.assertNotIn("decision", self._batch_record(batch_id)["dispatches"][-1])

    def test_the_rebase_fix_forward_dispatch_binds_its_target_into_an_explicit_approval(
        self,
    ) -> None:
        batch_id, candidate, _, upstream = self._moved_base_retry()

        proposal = self._propose(batch_id, "developer")
        unbound = {
            key: value
            for key, value in proposal["transition"].items()
            if key != "rebase_target_sha"
        }
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "approval digest does not match"
        ):
            self._dispatch(
                batch_id,
                "developer",
                digest=operational_guards.transition_digest(unbound),
            )
        retry = self._dispatch(
            batch_id, "developer", digest=proposal["transition_digest"]
        )["brief"]

        self.assertEqual(proposal["transition"]["rebase_target_sha"], upstream)
        self.assertEqual(
            (
                retry["rebase_target_commit"],
                retry["transition"]["rebase_target_sha"],
                retry["transition"]["next_action"],
                retry["snapshot_commit"],
                retry["coordinator_approval"]["approved_by"],
            ),
            (upstream, upstream, "developer-retry", candidate, "Malove"),
        )

    def test_a_rebase_fix_forward_dispatch_always_needs_an_explicit_approval(
        self,
    ) -> None:
        args = _ns(approved_by=None, approved_at=None)
        for policy in ("milestone", "low_risk", "auto"):
            for route, expected in (
                ("fix-forward", f"policy:{policy}"),
                ("rebase-fix-forward", None),
            ):
                batch: JsonObject = {
                    "approval_policy": policy,
                    "allowed_paths": ["**"],
                    "dispatches": [
                        {
                            "dispatch_id": "dispatch-1",
                            "role": "developer",
                            "decision": {
                                "decision": "retry",
                                "routing": {"route": route},
                            },
                        }
                    ],
                }
                config_ = {"low_risk_paths": ["**"]}
                with self.subTest(policy=policy, route=route):
                    if expected is not None:
                        self.assertEqual(
                            dispatch._dispatch_approval_mode(
                                args, batch, config_, "developer", "work", None
                            ),
                            expected,
                        )
                        continue
                    with self.assertRaises(coordinator.CoordinatorError) as raised:
                        dispatch._dispatch_approval_mode(
                            args, batch, config_, "developer", "work", None
                        )
                    self.assertIn("--approved-by", raised.exception.remedy)

    def test_a_brief_whose_rebase_target_left_its_transition_is_refused(self) -> None:
        batch_id, _, _, upstream = self._moved_base_retry()
        retry = self._dispatch(batch_id, "developer")["brief"]
        root = ledger_ops._state_root(self._args(), self.repo)
        stored = ledger_ops._load_dispatch(root, retry["dispatch_id"])

        def rebound(record: JsonObject, transition: JsonObject) -> JsonObject:
            digest = operational_guards.transition_digest(transition)
            return {
                **record,
                "transition": transition,
                "transition_digest": digest,
                "coordinator_approval": {
                    **record["coordinator_approval"],
                    "transition_digest": digest,
                },
            }

        def validate(record: JsonObject) -> None:
            current = ledger_ops._load_batch(root, batch_id)
            current["dispatches"][-1]["brief_sha256"] = hashlib.sha256(
                utils._canonical(record).encode("utf-8")
            ).hexdigest()
            coordinator._validate_dispatch(
                self.repo, coordinator._config(self.repo), root, current, record
            )

        legacy = rebound(
            {k: v for k, v in stored.items() if k != "rebase_target_commit"},
            {k: v for k, v in stored["transition"].items() if k != "rebase_target_sha"},
        )
        validate(legacy)
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "does not match its rebase target"
        ):
            validate({**stored, "rebase_target_commit": None})
        initial = {**stored["transition"], "next_action": "developer"}
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "rebase_target_commit must be null"
        ):
            validate(rebound(stored, initial))
        self.assertEqual(stored["rebase_target_commit"], upstream)

    def _rebase_onto(self, snapshot: str, target: str) -> list[str]:
        """Rebase the worktree's commits above the old base onto ``target``, as the developer
        contract prescribes, and return the rebased copies."""
        old_base = _git(self.worktree, "merge-base", snapshot, target)
        _git(self.worktree, "rebase", "--onto", target, old_base)
        return _git(
            self.worktree, "rev-list", "--reverse", f"{target}..HEAD"
        ).splitlines()

    @staticmethod
    def _rebased_map(originals: list[str], copies: list[str]) -> list[JsonObject]:
        return [
            {"commit_sha": copy, "rebased_from": original}
            for original, copy in zip(originals, copies, strict=True)
        ]

    def _developer_briefs(self, batch_id: str) -> list[JsonObject]:
        root = ledger_ops._state_root(self._args(), self.repo)
        return [
            ledger_ops._load_dispatch(root, item["dispatch_id"])
            for item in self._batch_record(batch_id)["dispatches"]
            if item["role"] == "developer"
        ]

    def test_issue_443_a_moved_base_is_rebased_and_fixed_in_one_developer_retry(
        self,
    ) -> None:
        """Regression for #443 (issue #504): origin/master moved while the batch ran. One
        developer-retry rebases the candidate onto the approved tip and fixes on top of it; its
        accept pins the tip, so code-review starts without a separate stale-base rebase."""
        batch_id, candidate, pinned, upstream = self._moved_base_retry()
        originals = _git(
            self.worktree, "rev-list", "--reverse", f"{pinned}..{candidate}"
        ).splitlines()
        retry = self._dispatch(batch_id, "developer")["brief"]
        self._start(retry["dispatch_id"])
        plan = retry["commit_plan"]
        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            f"rebase candidate does not contain the integration tip {upstream}",
        ):
            self._submit(
                retry["dispatch_id"],
                self._developer_report(retry, candidate, ["services/b.py"]),
            )
        copies = self._rebase_onto(candidate, upstream)
        fix, _ = self._developer_commit("fix")
        changed = git_utils._changed_files_between(self.repo, upstream, fix)
        rebased = self._rebased_map(originals, copies)
        fixed = self._commit_map([(fix, plan[1])])

        with self.assertRaises(coordinator.CoordinatorError) as missing:
            self._submit(
                retry["dispatch_id"],
                self._developer_report(
                    retry, fix, changed, commit_map=[rebased[0], *fixed]
                ),
            )
        submitted = self._submit(
            retry["dispatch_id"],
            self._developer_report(retry, fix, changed, commit_map=[*rebased, *fixed]),
        )
        packet = self._packet()
        accepted = self._decide(batch_id, "accept")
        self._assess(batch_id, fix, changed)
        review = self._dispatch(batch_id, "code-review", candidate=fix)["brief"]

        self.assertIn(
            f"does not account for previous-candidate commits ['{originals[1]}']",
            missing.exception.message,
        )
        self.assertIn("dropped", missing.exception.remedy)
        check = submitted["rebase_check"]
        self.assertEqual(
            (
                check["rebase_target_commit"],
                check["previous_base_commit"],
                [pair["patch_id_match"] for pair in check["rebased"]],
                check["dropped"],
                check["patch_id_mismatches"],
            ),
            (upstream, pinned, [True, True], [], []),
        )
        self.assertEqual(packet["rebase_check"], check)
        self.assertEqual(accepted["coordinator_decisions"][-1]["rebase_check"], check)
        self.assertEqual(accepted["integration_base_commit"], upstream)
        self.assertEqual(review["candidate_commit"], fix)
        self.assertEqual(decisions._developer_retry_count(accepted), 1)
        self.assertEqual(
            [
                commit_plan.is_developer_retry(brief)
                for brief in self._developer_briefs(batch_id)
            ],
            [False, True],
        )

    def test_a_rebased_copy_changed_by_a_conflict_is_flagged_by_patch_id_only(
        self,
    ) -> None:
        batch_id = cast(str, self._plan_batch(["one", "two"])["batch_id"])
        self._accepted_architect(batch_id)
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        originals, changed = self._commits("a", "b")
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                originals[-1],
                changed,
                commit_map=self._commit_map(list(zip(originals, brief["commit_plan"]))),
            ),
        )
        (self.repo / "services").mkdir(exist_ok=True)
        (self.repo / "services" / "b.py").write_text(
            "VALUE = 'upstream'\n", encoding="utf-8"
        )
        upstream = self._push_upstream()
        self._decide(batch_id, "retry", reason_category="code")
        retry = self._dispatch(batch_id, "developer")["brief"]
        self._start(retry["dispatch_id"])
        # services/b.py now exists on both sides: the rebase of the second commit conflicts.
        with self.assertRaisesRegex(RuntimeError, f"git rebase {upstream} failed"):
            _git(self.worktree, "rebase", upstream)
        (self.worktree / "services" / "b.py").write_text(
            "VALUE = 'b'\nUPSTREAM = 'upstream'\n", encoding="utf-8"
        )
        _git(self.worktree, "add", "services/b.py")
        _git(self.worktree, "-c", "core.editor=true", "rebase", "--continue")
        copies = _git(
            self.worktree, "rev-list", "--reverse", f"{upstream}..HEAD"
        ).splitlines()

        submitted = self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                copies[-1],
                git_utils._changed_files_between(self.repo, upstream, copies[-1]),
                commit_map=self._rebased_map(originals, copies),
            ),
        )
        packet = self._packet()
        accepted = self._decide(batch_id, "accept")

        mismatch = [{"rebased_from": originals[1], "commit_sha": copies[1]}]
        self.assertEqual(submitted["state"], "reported")
        self.assertEqual(
            [pair["patch_id_match"] for pair in submitted["rebase_check"]["rebased"]],
            [True, False],
        )
        self.assertEqual(submitted["rebase_check"]["patch_id_mismatches"], mismatch)
        self.assertEqual(packet["rebase_check"]["patch_id_mismatches"], mismatch)
        decision = accepted["coordinator_decisions"][-1]
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["rebase_check"]["patch_id_mismatches"], mismatch)
        self.assertEqual(accepted["integration_base_commit"], upstream)

    def test_a_patch_id_ignores_the_diff_configuration_and_skips_every_merge(
        self,
    ) -> None:
        def commit(name: str, text: str | None = None) -> str:
            (self.repo / f"{name}.txt").write_text(
                text or f"{name}\n", encoding="utf-8"
            )
            _git(self.repo, "add", "-A")
            _git(self.repo, "commit", "-m", name)
            return _git(self.repo, "rev-parse", "HEAD")

        base = _git(self.repo, "rev-parse", "HEAD")
        change = commit("change")
        _git(self.repo, "checkout", "-b", "copy", base)
        _git(self.repo, "commit", "--allow-empty", "-m", "empty")
        empty = _git(self.repo, "rev-parse", "HEAD")
        copy = commit("change")
        _git(self.repo, "merge", "--no-commit", "--no-ff", change)
        evil = commit("change", "change\nevil\n")

        plain = git_utils._patch_id(self.repo, change)
        _git(self.repo, "config", "diff.noprefix", "true")

        self.assertIsNotNone(plain)
        self.assertEqual(
            (
                git_utils._patch_id(self.repo, change),
                git_utils._patch_id(self.repo, copy),
            ),
            (plain, plain),
        )
        self.assertIsNone(git_utils._patch_id(self.repo, empty))
        # An evil merge carries its own change, yet a merge never gets a patch-id.
        self.assertEqual(
            len(_git(self.repo, "show", "--format=%P", "-s", evil).split()), 2
        )
        self.assertIsNone(git_utils._patch_id(self.repo, evil))

    def test_a_retry_of_a_rebased_report_is_measured_from_its_approved_target(
        self,
    ) -> None:
        self._patch_config(retry_policy={"max_developer_retries": 2})
        batch_id, candidate, pinned, upstream = self._moved_base_retry()
        originals = _git(
            self.worktree, "rev-list", "--reverse", f"{pinned}..{candidate}"
        ).splitlines()
        first = self._dispatch(batch_id, "developer")["brief"]
        self._start(first["dispatch_id"])
        copies = self._rebase_onto(candidate, upstream)
        self._submit(
            first["dispatch_id"],
            self._developer_report(
                first,
                copies[-1],
                git_utils._changed_files_between(self.repo, upstream, copies[-1]),
                commit_map=self._rebased_map(originals, copies),
            ),
        )
        decided = self._decide(batch_id, "retry", reason_category="code")
        second = self._dispatch(batch_id, "developer")["brief"]
        self._start(second["dispatch_id"])
        fix, _ = self._developer_commit("fix")
        commit_map = self._commit_map([(fix, second["commit_plan"][0])])

        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "changed_files must exactly match commit_sha"
        ):
            self._submit(
                second["dispatch_id"],
                self._developer_report(
                    second,
                    fix,
                    git_utils._changed_files_between(self.repo, pinned, fix),
                    commit_map=commit_map,
                ),
            )
        submitted = self._submit(
            second["dispatch_id"],
            self._developer_report(
                second,
                fix,
                git_utils._changed_files_between(self.repo, upstream, fix),
                commit_map=commit_map,
            ),
        )
        accepted = self._decide(batch_id, "accept")

        self.assertEqual(self._routing(decided)["route"], "developer-retry")
        self.assertEqual(
            (second["rebase_target_commit"], second["snapshot_commit"]),
            (None, copies[-1]),
        )
        self.assertNotIn("rebase_check", submitted)
        self.assertEqual(accepted["integration_base_commit"], upstream)

    def test_a_carried_item_chain_keeps_the_commits_of_its_rebase_fix_forward_attempt(
        self,
    ) -> None:
        """A retry of a not yet accepted rebase-fix-forward report that carried items may name the
        rebased copies and fix commits of that attempt: its closure, like its changed_files, is
        measured from the approved target, never from the pre-rebase snapshot or the old base."""
        self._patch_config(retry_policy={"max_developer_retries": 2})
        reviewed = self._reported_review_with_items()
        pinned = self._batch_record(self.batch_id)["integration_base_commit"]
        originals = _git(
            self.worktree, "rev-list", "--reverse", f"{pinned}..{reviewed}"
        ).splitlines()
        upstream = self._push_upstream()
        self._decide(self.batch_id, "retry")
        first = self._dispatch(self.batch_id, "developer")["brief"]
        self._start(first["dispatch_id"])
        copies = self._rebase_onto(reviewed, upstream)
        fix, _ = self._developer_commit("fix")
        self._submit(
            first["dispatch_id"],
            self._developer_report(
                first,
                fix,
                git_utils._changed_files_between(self.repo, upstream, fix),
                commit_map=[
                    *self._rebased_map(originals, copies),
                    *self._commit_map([(fix, first["commit_plan"][0])]),
                ],
            ),
        )
        decided = self._decide(self.batch_id, "retry", reason_category="code")
        second = self._dispatch(self.batch_id, "developer")["brief"]
        self._start(second["dispatch_id"])
        again, _ = self._developer_commit("again")
        report = self._developer_report(
            second,
            again,
            git_utils._changed_files_between(self.repo, upstream, again),
            commit_map=self._commit_map([(again, second["commit_plan"][0])]),
            carried_item_closure=[
                {"item_id": "coordinator-finding-1", "commits": [fix]},
                {"item_id": "review-finding-1", "commits": [copies[-1], again]},
            ],
        )
        root = ledger_ops._state_root(self._args(), self.repo)
        batch = self._batch_record(self.batch_id)

        self.assertEqual(
            (self._routing(decided)["route"], second["rebase_target_commit"]),
            ("fix-forward", None),
        )
        self.assertEqual(second["carried_items"], first["carried_items"])
        self.assertEqual(
            carried_items.closure_snapshots(root, batch, second), [reviewed, fix]
        )
        self.assertEqual(
            reports._closure_base(self.repo, root, batch, second), upstream
        )
        for foreign in (reviewed, upstream):
            with self.subTest(foreign=foreign):
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError,
                    "nor an earlier attempt of its retry chain",
                ):
                    self._submit(
                        second["dispatch_id"],
                        {
                            **report,
                            "carried_item_closure": [
                                {"item_id": "coordinator-finding-1", "commits": [fix]},
                                {"item_id": "review-finding-1", "commits": [foreign]},
                            ],
                        },
                    )
        self._submit(second["dispatch_id"], report)
        self.assertEqual(self._packet()["carried_items_gap"], [])
        accepted = self._decide(self.batch_id, "accept")
        self.assertEqual(accepted["integration_base_commit"], upstream)

    def _packet(self, **flags: object) -> JsonObject:
        return coordinator.decision_packet(
            self._args(batch=self.batch_id, dispatch=None, **flags)
        )

    def test_an_accept_with_findings_records_the_carry_over_route_it_previewed(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer, candidate, _ = self._reported_developer(batch["batch_id"])
        findings_file = self._findings_file()
        before = self._batch_record(self.batch_id)

        plain = self._packet()
        preview = self._packet(findings_file=findings_file)["route_preview"]

        self.assertEqual(set(plain["route_preview"]), {"retry", "abandon"})
        self.assertEqual(self._batch_record(self.batch_id), before)
        self.assertEqual(
            preview["carry-over"],
            {
                "route": "carry-over",
                "previous_role": "developer",
                "reason_category": None,
                "next_role": "code-review",
                "next_action": "code-review",
                "candidate_commit": candidate,
                "carried_item_ids": ["coordinator-finding-1"],
                "rationale": preview["carry-over"]["rationale"],
            },
        )
        self.assertEqual(preview["retry"], plain["route_preview"]["retry"])

        decided = self._decide(self.batch_id, "accept", findings_file=findings_file)

        decision = decided["coordinator_decisions"][-1]
        self.assertEqual(decision["routing"], preview["carry-over"])
        self.assertNotIn("next_role", decision)
        self.assertEqual(decided["next_action"], "risk-assessment")
        audit = self._decision_audits(self.batch_id)[-1]
        self.assertEqual(
            (audit["decision"], audit["route"], audit["dispatch_id"]),
            ("accept", "carry-over", developer["dispatch_id"]),
        )

    def test_a_carry_over_records_the_route_it_previewed_with_a_policy_approver(
        self,
    ) -> None:
        self._patch_config(approval_policy="milestone")
        batch_id, developer, candidate = self._auto_accepted_developer()
        findings_file = self._findings_file()

        preview = self._packet(findings_file=findings_file)["route_preview"]
        coordinator.carry_over_findings(
            self._args(batch=batch_id, findings_file=findings_file)
        )

        self.assertEqual(set(preview), {"carry-over"})
        self.assertEqual(preview["carry-over"]["candidate_commit"], candidate)
        decision = self._batch_record(batch_id)["coordinator_decisions"][-1]
        self.assertEqual(decision["routing"], preview["carry-over"])
        audit = self._decision_audits(batch_id)[-1]
        self.assertEqual(
            (audit["decision"], audit["route"], audit["approver"]),
            ("carry-over", "carry-over", {"kind": "policy", "name": "carry-over"}),
        )
        self.assertEqual(
            audit["evidence"], self._report_evidence(batch_id, developer["dispatch_id"])
        )

    def test_a_carry_over_preview_that_would_be_refused_says_why(self) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        self._submit(brief["dispatch_id"], self._base_report(brief, "architect"))
        findings_file = self._findings_file()

        architect = self._packet(findings_file=findings_file)["route_preview"]
        self._decide(self.batch_id, "accept")
        _, candidate, changed = self._reported_developer(self.batch_id)
        self._decide(self.batch_id, "accept")
        self._assess(self.batch_id, candidate, changed)
        review = self._dispatch(self.batch_id, "code-review", candidate=candidate)
        created = self._packet(findings_file=findings_file)["route_preview"]

        refusals = (architect["carry-over"], created["carry-over"])
        for refused in refusals:
            self.assertIsNone(refused["route"])
            self.assertTrue(refused["remedy"].strip())
        self.assertIn("--findings-file", refusals[0]["refused"])
        self.assertIn(review["dispatch_id"], refusals[1]["refused"])
        self.assertIn("route", architect["retry"])

    def test_issue_443_a_defect_in_a_clean_developer_report_is_carried_into_review(
        self,
    ) -> None:
        """Regression for #443: the coordinator found a defect in a clean developer report and
        retried it before review, so the only developer retry was gone when the review found more
        and the batch had to be abandoned. Now the defect rides into review as a carried item and
        the single retry answers the review and the finding together."""
        batch_id = cast(str, self._plan_batch(["add simple marker"])["batch_id"])
        self._accepted_architect(batch_id)
        _, candidate, changed = self._reported_developer(batch_id)

        self._decide(batch_id, "accept", findings_file=self._findings_file())
        self._assess_without_triggers(batch_id, candidate, changed)

        assessed = self._batch_record(batch_id)
        self.assertFalse(assessed["risk_assessments"][-1]["review_required"])
        self.assertEqual(assessed["next_action"], "code-review")
        self.assertEqual(decisions._developer_retry_count(assessed), 0)
        finding = {
            "severity": "warning",
            "summary": "the reset path has no test",
            "evidence": "tests/test_x.py:1",
        }
        review = self._reported_review(
            batch_id,
            candidate,
            standards=("warning", [finding]),
            carried={"coordinator-finding-1": "open"},
        )
        self.assertEqual(
            [item["item_id"] for item in self._carried(review)],
            ["coordinator-finding-1"],
        )

        retried = self._decide(batch_id, "retry")
        retry = self._dispatch(batch_id, "developer")["brief"]

        self.assertEqual(self._routing(retried)["route"], "fix-forward")
        self.assertEqual(
            [item["item_id"] for item in self._carried(retry)],
            ["coordinator-finding-1", "review-finding-1"],
        )
        self._start(retry["dispatch_id"])
        fix, _ = self._developer_commit("fix")
        fixed = git_utils._changed_files_between(
            self.repo, self._batch_record(batch_id)["base_commit"], fix
        )
        self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                fix,
                fixed,
                commit_map=self._commit_map([(fix, retry["commit_plan"][0])]),
            ),
        )
        self._decide(batch_id, "accept")
        self._assess_without_triggers(batch_id, fix, fixed)
        self.assertEqual(self._batch_record(batch_id)["next_action"], "code-review")
        closing = self._reported_review(
            batch_id, fix, carried={"coordinator-finding-1": "closed"}
        )

        done = self._decide(batch_id, "accept")

        self.assertEqual(
            [item["item_id"] for item in self._carried(closing)],
            ["coordinator-finding-1"],
        )
        self.assertEqual(
            (done["state"], done["next_action"]), ("awaiting-approval", "qa")
        )
        self.assertNotIn("abandoned", done)
        self.assertEqual(decisions._developer_retry_count(done), 1)
        root = ledger_ops._state_root(self._args(), self.repo)
        self.assertEqual(carried_items.open_coordinator_findings(root, done), [])

    def test_issue_443_one_fix_commit_on_top_of_the_candidate_closes_the_items_by_fix_forward(
        self,
    ) -> None:
        """Regression for #443 (issue #503): the review's finding and the open coordinator finding
        need one more commit. The retry is a fix-forward in the same batch: one fix commit on top
        of the reviewed candidate closes both items, with no new batch, cherry-pick or rewrite."""
        batch_id = cast(str, self._plan_batch(["add simple marker"])["batch_id"])
        self._accepted_architect(batch_id)
        _, candidate, changed = self._reported_developer(batch_id)
        self._decide(batch_id, "accept", findings_file=self._findings_file())
        self._assess_without_triggers(batch_id, candidate, changed)
        self._reported_review(
            batch_id,
            candidate,
            standards=("warning", [dict(self.REVIEW_WARNING)]),
            carried={"coordinator-finding-1": "open"},
        )

        retried = self._decide(batch_id, "retry")
        retry = self._dispatch(batch_id, "developer")["brief"]
        self._start(retry["dispatch_id"])
        fix, report = self._fix_report(retry)
        self._submit(retry["dispatch_id"], report)
        accepted = self._decide(batch_id, "accept")
        self._assess_without_triggers(
            batch_id,
            fix,
            git_utils._changed_files_between(
                self.repo, self._batch_record(batch_id)["base_commit"], fix
            ),
        )
        closing = self._reported_review(
            batch_id, fix, carried={"coordinator-finding-1": "closed"}
        )
        done = self._decide(batch_id, "accept")

        routing = self._routing(retried)
        self.assertEqual(
            (routing["route"], routing["retry_item_ids"]),
            ("fix-forward", self.RETRY_ITEMS),
        )
        self.assertEqual(
            carried_items.section_item_ids(retry["carried_items"]), self.RETRY_ITEMS
        )
        self.assertEqual(retry["snapshot_commit"], candidate)
        self.assertEqual(_git(self.worktree, "rev-parse", f"{fix}^"), candidate)
        self.assertEqual(
            report["carried_item_closure"],
            [{"item_id": item_id, "commits": [fix]} for item_id in self.RETRY_ITEMS],
        )
        self.assertNotIn("carried_items_gap", accepted["coordinator_decisions"][-1])
        self.assertEqual(closing["candidate_commit"], fix)
        self.assertEqual(
            (done["batch_id"], done["state"], done["next_action"]),
            (batch_id, "awaiting-approval", "qa"),
        )
        self.assertNotIn("abandoned", done)
        self.assertEqual(decisions._developer_retry_count(done), 1)
        tickets = [
            coordinator._read_object(path, "batch")["ticket"]
            for path in (self._records() / "batches").glob("batch-*.json")
        ]
        self.assertEqual(tickets, [done["ticket"]])

    # -- delta-review after a fix-forward (issue #625) -----------------------------------------

    SPEC_WARNING = {
        "severity": "warning",
        "summary": "the marker value is not pinned by a test",
        "evidence": "services/x.py:1",
    }

    def _commit_file(self, path: str, message: str, line: str = "FIXED = 1\n") -> str:
        """One commit that appends ``line`` to ``path`` in the worktree; returns its SHA."""
        target = self.worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
        _git(self.worktree, "add", "-A")
        _git(self.worktree, "commit", "-m", message)
        return _git(self.worktree, "rev-parse", "HEAD")

    def _fix_forward_candidate(
        self,
        path: str,
        message: str,
        *,
        standards: tuple[str, list[JsonObject]] = ("clean", []),
    ) -> tuple[JsonObject, JsonObject, str, str]:
        """A reviewed candidate whose Spec warning was retried as a fix-forward, and the accepted,
        assessed developer-retry with one fix commit on ``path``. Returns the retried review brief,
        the developer-retry brief, the reviewed candidate and the fix."""
        batch_id = cast(str, self._plan_batch(["add simple marker"])["batch_id"])
        self._accepted_architect(batch_id)
        _, candidate, changed = self._reported_developer(batch_id)
        self._decide(batch_id, "accept")
        self._assess_without_triggers(batch_id, candidate, changed)
        review = self._reported_review(
            batch_id,
            candidate,
            standards=standards,
            spec=("warning", [dict(self.SPEC_WARNING)]),
        )
        self.assertEqual(
            self._routing(self._decide(batch_id, "retry"))["route"], "fix-forward"
        )
        retry = self._dispatch(batch_id, "developer")["brief"]
        self._start(retry["dispatch_id"])
        fix = self._commit_file(path, message)
        base = self._batch_record(batch_id)["base_commit"]
        fixed = git_utils._changed_files_between(self.repo, base, fix)
        self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                fix,
                fixed,
                commit_map=self._commit_map([(fix, retry["commit_plan"][0])]),
            ),
        )
        self._decide(batch_id, "accept")
        self._assess_without_triggers(batch_id, fix, fixed)
        return review, retry, candidate, fix

    def _review_fields(self, candidate: str, delta_review_of: str | None) -> JsonObject:
        return {
            **self._proposal_fields(self.batch_id, "code-review", "work", candidate),
            "delta_review_of": delta_review_of,
        }

    def _explicit_delta_review(self, candidate: str, prior: str) -> JsonObject:
        """The code-review brief ``dispatch create --delta-review-of <prior>`` creates."""
        fields = self._review_fields(candidate, prior)
        digest = coordinator.create_dispatch(self._args(propose=True, **fields))[
            "transition_digest"
        ]
        return cast(
            JsonObject,
            coordinator.create_dispatch(
                self._args(transition_digest=digest, **fields, **self._approval())
            )["brief"],
        )

    def test_an_explicit_delta_review_of_a_test_only_fix_after_a_fix_forward_is_unchanged(
        self,
    ) -> None:
        """Characterization (issue #625): ``--delta-review-of`` on a test-only fix after a
        fix-forward keeps the test-only delta-review: Spec is re-checked, the Clean Standards axis
        is inherited, and every refusal of that path still applies."""
        review, retry, candidate, fix = self._fix_forward_candidate(
            "tests/test_marker.py", "test: pin the marker value"
        )
        with self.assertRaisesRegex(
            coordinator.CoordinatorError,
            "delta-review-of must reference a code-review dispatch in this batch",
        ):
            self._explicit_delta_review(fix, retry["dispatch_id"])

        brief = self._explicit_delta_review(fix, review["dispatch_id"])

        self.assertEqual(
            (
                brief["delta_review_of"],
                brief["delta_review_axis"],
                brief.get("delta_review_scope"),
                brief["review_scope"],
                brief["carried_items"],
            ),
            (
                review["dispatch_id"],
                "spec",
                None,
                ["services/x.py", "tests/test_marker.py"],
                {},
            ),
        )
        self.assertNotIn("delta_review_sha256", brief["transition"])
        self._start(brief["dispatch_id"], checkout=self.worktree)
        axes = self._axes(("clean", []), ("clean", []))
        inherited = {**axes["standards"], "inherited_from": review["dispatch_id"]}
        report = self._base_report(
            brief,
            "code-review",
            review={
                "candidate_commit": fix,
                "scope": brief["review_scope"],
                **axes,
            },
        )
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "standards evidence has an invalid schema"
        ):
            self._submit(brief["dispatch_id"], report)
        report["review"]["standards"] = inherited
        self._submit(brief["dispatch_id"], report)
        accepted = self._decide(self.batch_id, "accept")
        qa = self._dispatch(self.batch_id, "qa", candidate=fix)["brief"]
        self.assertEqual(accepted["next_action"], "qa")
        self.assertEqual(
            (qa["candidate_commit"], qa["verification_commands"]),
            (fix, accepted["verification_commands"]),
        )

        root = ledger_ops._state_root(self._args(), self.repo)
        batch = self._batch_record(self.batch_id)
        prior = ledger_ops._load_dispatch(root, review["dispatch_id"])
        prior_entry = next(
            item
            for item in batch["dispatches"]
            if item["dispatch_id"] == review["dispatch_id"]
        )
        prior_report = history._pending_report(root, batch, prior_entry)

        def eligible(report_: JsonObject, commit: str) -> str:
            return dispatch._delta_review_eligibility(
                self.repo,
                coordinator._config(self.repo),
                ["transactions"],
                prior,
                report_,
                commit,
            )

        self.assertEqual(eligible(prior_report, fix), "spec")
        warned = json.loads(json.dumps(prior_report))
        warned["review"]["standards"]["severity"] = "warning"
        triggered = self._commit_file(
            "tests/test_marker.py", "test: wrap the marker in a transaction"
        )
        production = self._commit_file("services/x.py", "fix: tighten the marker")
        for report_, commit, refusal in (
            (warned, fix, "requires prior Standards=Clean"),
            (prior_report, candidate, "descended from the prior reviewed candidate"),
            (prior_report, production, r"touches non-test file\(s\): services/x.py"),
            (prior_report, triggered, r"matches risk trigger\(s\): transactions"),
        ):
            with self.subTest(refusal=refusal):
                with self.assertRaisesRegex(coordinator.CoordinatorError, refusal):
                    eligible(report_, commit)

    def _entry(self, dispatch_id: str) -> JsonObject:
        return cast(
            JsonObject,
            next(
                item
                for item in self._batch_record(self.batch_id)["dispatches"]
                if item["dispatch_id"] == dispatch_id
            ),
        )

    def _review_report(
        self, brief: JsonObject, carried: dict[str, str] | None = None
    ) -> JsonObject:
        """A clean composite review of ``brief``; ``carried`` maps an item id to its status."""
        review: JsonObject = {
            "candidate_commit": brief["candidate_commit"],
            "scope": brief["review_scope"],
            **self._axes(("clean", []), ("clean", [])),
        }
        if carried is not None:
            review["carried_items"] = [
                {"item_id": item_id, "status": status, "evidence": "services/x.py:2"}
                for item_id, status in carried.items()
            ]
        return self._base_report(brief, "code-review", review=review)

    def _accept_review_then_qa(
        self, brief: JsonObject, carried: dict[str, str] | None = None
    ) -> tuple[JsonObject, JsonObject]:
        """Accept a clean review of ``brief``; return the batch and the QA brief that follows."""
        self._start(brief["dispatch_id"], checkout=self.worktree)
        self._submit(brief["dispatch_id"], self._review_report(brief, carried))
        accepted = self._decide(self.batch_id, "accept")
        qa = self._dispatch(self.batch_id, "qa", candidate=brief["candidate_commit"])
        return accepted, cast(JsonObject, qa["brief"])

    def test_a_fix_inside_the_reviewed_files_after_a_fix_forward_gets_a_delta_review(
        self,
    ) -> None:
        """Issue #625: the fix-forward added one commit inside the files the review judged, so the
        coordinator scopes the next review to that commit and the closure of the review's finding,
        with the prior report as evidence. QA then runs on the new SHA."""
        review, retry, candidate, fix = self._fix_forward_candidate(
            "services/x.py", "fix: pin the marker value"
        )
        proposal = coordinator.create_dispatch(
            self._args(propose=True, **self._review_fields(fix, None))
        )
        brief = self._dispatch(
            self.batch_id,
            "code-review",
            candidate=fix,
            digest=proposal["transition_digest"],
        )["brief"]

        scope = brief["delta_review_scope"]
        self.assertEqual(
            scope,
            {
                "mode": "delta",
                "route": "fix-forward",
                "prior_review": {
                    "dispatch_id": review["dispatch_id"],
                    "report_sha256": self._entry(review["dispatch_id"])[
                        "report_sha256"
                    ],
                    "candidate_commit": candidate,
                    "review_base": review["review_base"],
                    "risk_assessment_id": review["risk_assessment_id"],
                },
                "developer_dispatch_id": retry["dispatch_id"],
                "delta_base": candidate,
                "delta_commits": [fix],
                "reviewed_copies": [],
                "closure": [{"item_id": "review-finding-1", "commits": [fix]}],
                "escalations": [],
            },
        )
        self.assertEqual(proposal["delta_review_scope"], scope)
        self.assertEqual(
            brief["transition"]["delta_review_sha256"],
            operational_guards.delta_review_digest(scope),
        )
        self.assertEqual(
            (
                brief["delta_review_of"],
                brief["delta_review_axis"],
                brief["review_scope"],
            ),
            (None, None, ["services/x.py"]),
        )
        self.assertEqual(brief["carried_items"], retry["carried_items"])
        self.assertEqual(
            carried_items.section_item_ids(brief["carried_items"]), ["review-finding-1"]
        )
        self.assertEqual(
            carried_items.carried_gap(self._review_report(brief), brief),
            ["review-finding-1"],
        )

        accepted, qa = self._accept_review_then_qa(
            brief, {"review-finding-1": "closed"}
        )

        self.assertEqual(accepted["next_action"], "qa")
        self.assertEqual(
            (
                qa["candidate_commit"],
                qa["verification_commands"],
                qa["delta_review_scope"],
            ),
            (fix, accepted["verification_commands"], None),
        )

    def test_a_brief_binds_its_delta_review_scope_and_keeps_the_legacy_shape(
        self,
    ) -> None:
        _, _, _, fix = self._fix_forward_candidate(
            "services/x.py", "fix: pin the marker value"
        )
        brief = self._dispatch(self.batch_id, "code-review", candidate=fix)["brief"]
        root = ledger_ops._state_root(self._args(), self.repo)
        stored = ledger_ops._load_dispatch(root, brief["dispatch_id"])
        scope = stored["delta_review_scope"]

        def rebound(record: JsonObject, transition: JsonObject) -> JsonObject:
            digest = operational_guards.transition_digest(transition)
            return {
                **record,
                "transition": transition,
                "transition_digest": digest,
                "coordinator_approval": {
                    **record["coordinator_approval"],
                    "transition_digest": digest,
                },
            }

        def validate(record: JsonObject) -> None:
            current = ledger_ops._load_batch(root, self.batch_id)
            current["dispatches"][-1]["brief_sha256"] = hashlib.sha256(
                utils._canonical(record).encode("utf-8")
            ).hexdigest()
            coordinator._validate_dispatch(
                self.repo, coordinator._config(self.repo), root, current, record
            )

        def rescoped(value: JsonObject) -> JsonObject:
            transition = {
                **stored["transition"],
                "delta_review_sha256": operational_guards.delta_review_digest(value),
            }
            return rebound({**stored, "delta_review_scope": value}, transition)

        validate(stored)
        validate(
            rebound(
                {k: v for k, v in stored.items() if k != "delta_review_scope"},
                {
                    k: v
                    for k, v in stored["transition"].items()
                    if k != "delta_review_sha256"
                },
            )
        )
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "does not match its delta-review scope"
        ):
            validate({**stored, "delta_review_scope": None})
        escalation = {"reason": "no-new-commits", "evidence": [f"{fix}..{fix}"]}
        for malformed in (
            {**scope, "escalations": [escalation]},
            {**scope, "mode": "full"},
            {**scope, "mode": "partial"},
            {**scope, "mode": "full", "escalations": [{**escalation, "reason": "x"}]},
            {**scope, "mode": "full", "escalations": [{**escalation, "evidence": []}]},
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "delta_review_scope must be null"
                ):
                    validate(rescoped(malformed))
        validate(rescoped({**scope, "mode": "full", "escalations": [escalation]}))
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "delta_review_scope must be null"
        ):
            validate(
                {
                    **rescoped(scope),
                    "delta_review_of": stored["dispatch_id"],
                    "delta_review_axis": "spec",
                }
            )

    def test_a_fix_forward_chain_of_retried_attempts_is_delta_reviewed_as_one(
        self,
    ) -> None:
        """Issue #625: the first fix-forward attempt was retried and the second, which continued
        it, was accepted. The delta-review spans the commits of both attempts from the reviewed
        candidate and names the accepted attempt and its closure. QA then runs on the new SHA."""
        self._patch_config(retry_policy={"max_developer_retries": 2})
        batch_id = cast(str, self._plan_batch(["add simple marker"])["batch_id"])
        self._accepted_architect(batch_id)
        _, candidate, changed = self._reported_developer(batch_id)
        self._decide(batch_id, "accept")
        self._assess_without_triggers(batch_id, candidate, changed)
        review = self._reported_review(
            batch_id, candidate, spec=("warning", [dict(self.SPEC_WARNING)])
        )
        self._decide(batch_id, "retry")
        base = self._batch_record(batch_id)["base_commit"]
        attempts: list[JsonObject] = []
        fixes: list[str] = []
        for message in ("fix: pin the marker value", "fix: pin the marker value again"):
            if attempts:
                retried = self._decide(batch_id, "retry", reason_category="code")
                self.assertEqual(self._routing(retried)["route"], "fix-forward")
            attempt = self._dispatch(batch_id, "developer")["brief"]
            self._start(attempt["dispatch_id"])
            fix = self._commit_file("services/x.py", message)
            fixed = git_utils._changed_files_between(self.repo, base, fix)
            self._submit(
                attempt["dispatch_id"],
                self._developer_report(
                    attempt,
                    fix,
                    fixed,
                    commit_map=self._commit_map([(fix, attempt["commit_plan"][0])]),
                ),
            )
            attempts.append(attempt)
            fixes.append(fix)
        self._decide(batch_id, "accept")
        self._assess_without_triggers(batch_id, fixes[-1], fixed)

        brief = self._dispatch(batch_id, "code-review", candidate=fixes[-1])["brief"]

        scope = brief["delta_review_scope"]
        self.assertEqual(attempts[1]["snapshot_commit"], fixes[0])
        self.assertEqual(
            (
                scope["mode"],
                scope["route"],
                scope["prior_review"]["dispatch_id"],
                scope["developer_dispatch_id"],
                scope["delta_base"],
                scope["delta_commits"],
                scope["closure"],
                scope["escalations"],
            ),
            (
                "delta",
                "fix-forward",
                review["dispatch_id"],
                attempts[1]["dispatch_id"],
                candidate,
                fixes,
                [{"item_id": "review-finding-1", "commits": [fixes[1]]}],
                [],
            ),
        )
        self.assertEqual(
            carried_items.section_item_ids(brief["carried_items"]), ["review-finding-1"]
        )
        accepted, qa = self._accept_review_then_qa(
            brief, {"review-finding-1": "closed"}
        )
        self.assertEqual(
            (accepted["next_action"], qa["candidate_commit"]), ("qa", fixes[-1])
        )

    def test_a_qa_retry_after_an_accepted_review_carries_nothing_to_delta_review(
        self,
    ) -> None:
        """Issue #625: a QA retry after an accepted review hands the developer no carried item.
        It is a plain developer-retry, so the next review is an ordinary full review with no
        delta_review_scope; when the integration base moved, it is a rebase-fix-forward whose fix
        lies outside the (empty) carried items, so the coordinator escalates to a full review on
        its own. QA then runs on the new SHA either way."""
        for moved in (False, True):
            with self.subTest(moved=moved):
                self._reset()
                batch_id = cast(
                    str, self._plan_batch(["add simple marker"])["batch_id"]
                )
                self._accepted_architect(batch_id)
                _, candidate, changed = self._reported_developer(batch_id)
                self._decide(batch_id, "accept")
                self._assess_without_triggers(batch_id, candidate, changed)
                review = self._reported_review(batch_id, candidate)
                self._decide(batch_id, "accept")
                self._reported_qa(
                    batch_id, candidate, outcome="failed", check_result="fail"
                )
                origin = (
                    self._push_upstream()
                    if moved
                    else self._batch_record(batch_id)["base_commit"]
                )
                routing = self._routing(
                    self._decide(batch_id, "retry", reason_category="code")
                )
                retry = self._dispatch(batch_id, "developer")["brief"]
                self._start(retry["dispatch_id"])
                copies = self._rebase_onto(candidate, origin) if moved else []
                fix = self._commit_file("services/x.py", "fix: make the QA check pass")
                fixed = git_utils._changed_files_between(self.repo, origin, fix)
                self._submit(
                    retry["dispatch_id"],
                    self._developer_report(
                        retry,
                        fix,
                        fixed,
                        commit_map=[
                            *self._rebased_map([candidate] if moved else [], copies),
                            *self._commit_map([(fix, retry["commit_plan"][0])]),
                        ],
                    ),
                )
                self._decide(batch_id, "accept")
                self._assess_without_triggers(batch_id, fix, fixed)

                brief = self._dispatch(batch_id, "code-review", candidate=fix)["brief"]

                self.assertEqual(
                    (routing["previous_role"], routing["retry_item_ids"]), ("qa", [])
                )
                self.assertEqual(retry["carried_items"], {})
                scope = brief["delta_review_scope"]
                if moved:
                    self.assertEqual(routing["route"], "rebase-fix-forward")
                    self.assertEqual(
                        (
                            scope["mode"],
                            scope["route"],
                            scope["prior_review"]["dispatch_id"],
                            scope["delta_base"],
                            scope["delta_commits"],
                            scope["reviewed_copies"],
                            scope["closure"],
                            scope["escalations"],
                        ),
                        (
                            "full",
                            "rebase-fix-forward",
                            review["dispatch_id"],
                            copies[-1],
                            [fix],
                            [{"commit_sha": copies[0], "rebased_from": candidate}],
                            [],
                            [
                                {
                                    "reason": "file-outside-carried-items",
                                    "evidence": ["services/x.py"],
                                }
                            ],
                        ),
                    )
                else:
                    self.assertEqual(routing["route"], "developer-retry")
                    self.assertIsNone(scope)
                    self.assertNotIn("delta_review_sha256", brief["transition"])
                self.assertEqual(brief["carried_items"], {})
                accepted, qa = self._accept_review_then_qa(brief)
                self.assertEqual(
                    (accepted["next_action"], qa["candidate_commit"]), ("qa", fix)
                )

    def test_a_new_risk_trigger_or_file_after_a_fix_forward_escalates_to_a_full_review(
        self,
    ) -> None:
        """Issue #625: with no manual choice, a fix-forward commit that matches a risk trigger the
        prior review did not see, or changes a file outside the carried items, sends the candidate
        to an ordinary full review; the section stays as audit evidence. QA then runs on the new
        SHA. A review finding names no files, so its files are its review's review_scope: a new
        file, even a new test file that pins the finding, lies outside them and escalates too."""
        for path, message, reason, evidence in (
            (
                "services/x.py",
                "fix: wrap the marker in a transaction",
                "new-risk-trigger",
                ["transactions"],
            ),
            (
                "services/y.py",
                "fix: pin the marker value",
                "file-outside-carried-items",
                ["services/y.py"],
            ),
            (
                "tests/test_marker.py",
                "test: pin the marker value",
                "file-outside-carried-items",
                ["tests/test_marker.py"],
            ),
        ):
            with self.subTest(reason=reason, path=path):
                self._reset()
                _, _, candidate, fix = self._fix_forward_candidate(path, message)
                brief = self._dispatch(self.batch_id, "code-review", candidate=fix)[
                    "brief"
                ]
                scope = brief["delta_review_scope"]
                base = self._batch_record(self.batch_id)["base_commit"]

                self.assertEqual(
                    (
                        scope["mode"],
                        scope["delta_base"],
                        scope["delta_commits"],
                        scope["escalations"],
                    ),
                    (
                        "full",
                        candidate,
                        [fix],
                        [{"reason": reason, "evidence": evidence}],
                    ),
                )
                self.assertEqual(
                    brief["transition"]["delta_review_sha256"],
                    operational_guards.delta_review_digest(scope),
                )
                self.assertEqual(
                    (
                        brief["carried_items"],
                        brief["delta_review_of"],
                        brief["review_scope"],
                    ),
                    ({}, None, git_utils._changed_files_between(self.repo, base, fix)),
                )
                accepted, qa = self._accept_review_then_qa(brief)
                self.assertEqual(
                    (
                        accepted["next_action"],
                        qa["candidate_commit"],
                        qa["verification_commands"],
                    ),
                    ("qa", fix, accepted["verification_commands"]),
                )

    def _rebased_fix_forward(
        self, *, conflict: bool = False, fixed: bool = True, dropped: bool = False
    ) -> tuple[JsonObject, str, str, list[str], str | None]:
        """A reviewed one-commit candidate whose Spec warning was retried as a rebase-fix-forward
        after origin/master moved, rebased (with ``conflict``, through a conflict resolved with
        changes; with ``dropped``, without carrying the reviewed commit, which the report maps as
        dropped) and, with ``fixed``, fixed by one commit inside the reviewed file; accepted and
        assessed. Returns the review brief, the reviewed candidate, the tip, the rebased copies and
        the fix (``None`` without one)."""
        batch_id = cast(str, self._plan_batch(["add simple marker"])["batch_id"])
        self._accepted_architect(batch_id)
        _, candidate, changed = self._reported_developer(batch_id)
        self._decide(batch_id, "accept")
        self._assess_without_triggers(batch_id, candidate, changed)
        review = self._reported_review(
            batch_id, candidate, spec=("warning", [dict(self.SPEC_WARNING)])
        )
        if conflict:
            (self.repo / "services").mkdir(exist_ok=True)
            (self.repo / "services" / "x.py").write_text(
                "VALUE = 'upstream'\n", encoding="utf-8"
            )
        upstream = self._push_upstream()
        self.assertEqual(
            self._routing(self._decide(batch_id, "retry"))["route"],
            "rebase-fix-forward",
        )
        retry = self._dispatch(batch_id, "developer")["brief"]
        self._start(retry["dispatch_id"])
        if conflict:
            with self.assertRaises(RuntimeError):
                _git(self.worktree, "rebase", upstream)
            (self.worktree / "services" / "x.py").write_text(
                "VALUE = 'x'\nUPSTREAM = 'upstream'\n", encoding="utf-8"
            )
            _git(self.worktree, "add", "services/x.py")
            _git(self.worktree, "-c", "core.editor=true", "rebase", "--continue")
            copies = _git(
                self.worktree, "rev-list", "--reverse", f"{upstream}..HEAD"
            ).splitlines()
        elif dropped:
            _git(self.worktree, "reset", "--hard", upstream)
            copies = []
        else:
            copies = self._rebase_onto(candidate, upstream)
        fix = (
            self._commit_file("services/x.py", "fix: pin the marker value")
            if fixed
            else None
        )
        head = fix or copies[-1]
        commit_map = (
            [{"rebased_from": candidate, "dropped": "the fix replaces the marker"}]
            if dropped
            else self._rebased_map([candidate], copies)
        )
        closure: JsonObject = {
            "item_id": "review-finding-1",
            "not_closed": "the finding needs a product decision",
        }
        if fix is not None:
            commit_map += self._commit_map([(fix, retry["commit_plan"][0])])
            closure = {"item_id": "review-finding-1", "commits": [fix]}
        head_changed = git_utils._changed_files_between(self.repo, upstream, head)
        self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                head,
                head_changed,
                commit_map=commit_map,
                carried_item_closure=[closure],
            ),
        )
        if fix is not None:
            self._decide(batch_id, "accept")
        else:
            self._decide(batch_id, "override-warning", note=closure["not_closed"])
        self._assess_without_triggers(batch_id, head, head_changed)
        return review, candidate, upstream, copies, fix

    def test_a_rebased_copy_with_a_matching_patch_id_stays_out_of_the_delta_review(
        self,
    ) -> None:
        """Issue #625: after a rebase-fix-forward, the rebased copy of the reviewed commit has the
        patch-id of its original, so it counts as reviewed and only the fix is delta-reviewed."""
        review, candidate, upstream, copies, fix = self._rebased_fix_forward()
        brief = self._dispatch(self.batch_id, "code-review", candidate=fix)["brief"]
        scope = brief["delta_review_scope"]

        self.assertEqual(
            (
                scope["mode"],
                scope["route"],
                scope["prior_review"]["dispatch_id"],
                scope["delta_base"],
                scope["delta_commits"],
                scope["reviewed_copies"],
                scope["escalations"],
            ),
            (
                "delta",
                "rebase-fix-forward",
                review["dispatch_id"],
                copies[-1],
                [fix],
                [{"commit_sha": copies[0], "rebased_from": candidate}],
                [],
            ),
        )
        self.assertEqual(
            self._batch_record(self.batch_id)["integration_base_commit"], upstream
        )
        self.assertEqual(
            carried_items.section_item_ids(brief["carried_items"]), ["review-finding-1"]
        )
        accepted, qa = self._accept_review_then_qa(
            brief, {"review-finding-1": "closed"}
        )
        self.assertEqual((accepted["next_action"], qa["candidate_commit"]), ("qa", fix))

    def test_a_rebase_without_a_new_commit_or_with_a_patch_id_mismatch_gets_a_full_review(
        self,
    ) -> None:
        """Issue #625: a rebase-fix-forward that adds no commit leaves nothing to delta-review, and
        a rebased copy changed by a conflict differs from its original by patch-id; both escalate
        to a full review without a manual choice."""
        with self.subTest(reason="no-new-commits"):
            _, candidate, upstream, copies, _ = self._rebased_fix_forward(fixed=False)
            brief = self._dispatch(self.batch_id, "code-review", candidate=copies[-1])[
                "brief"
            ]
            scope = brief["delta_review_scope"]
            self.assertEqual(
                (
                    scope["mode"],
                    scope["delta_base"],
                    scope["delta_commits"],
                    scope["reviewed_copies"],
                    scope["escalations"],
                    brief["carried_items"],
                ),
                (
                    "full",
                    None,
                    [],
                    [{"commit_sha": copies[0], "rebased_from": candidate}],
                    [
                        {
                            "reason": "no-new-commits",
                            "evidence": [f"{upstream}..{copies[-1]}"],
                        }
                    ],
                    {},
                ),
            )
        with self.subTest(reason="patch-id-mismatch"):
            self._reset()
            _, candidate, upstream, copies, fix = self._rebased_fix_forward(
                conflict=True
            )
            brief = self._dispatch(self.batch_id, "code-review", candidate=fix)["brief"]
            scope = brief["delta_review_scope"]
            mismatch = {"rebased_from": candidate, "commit_sha": copies[0]}
            self.assertEqual(
                (
                    scope["mode"],
                    scope["delta_base"],
                    scope["delta_commits"],
                    scope["reviewed_copies"],
                    scope["escalations"],
                    brief["carried_items"],
                ),
                (
                    "full",
                    upstream,
                    [copies[0], fix],
                    [],
                    [{"reason": "patch-id-mismatch", "evidence": [mismatch]}],
                    {},
                ),
            )

    def test_a_previous_candidate_commit_the_rebase_dropped_gets_a_full_review(
        self,
    ) -> None:
        """Issue #625: a rebase-fix-forward that drops a commit of the reviewed candidate leaves the
        candidate without a change the prior review judged, though the fix alone stays inside the
        carried items; the dropped commit escalates to a full review without a manual choice."""
        _, candidate, upstream, copies, fix = self._rebased_fix_forward(dropped=True)
        brief = self._dispatch(self.batch_id, "code-review", candidate=fix)["brief"]
        scope = brief["delta_review_scope"]

        self.assertEqual(
            (
                copies,
                scope["mode"],
                scope["delta_base"],
                scope["delta_commits"],
                scope["reviewed_copies"],
                scope["escalations"],
                brief["carried_items"],
            ),
            (
                [],
                "full",
                upstream,
                [fix],
                [],
                [
                    {
                        "reason": "dropped-commit",
                        "evidence": [
                            {
                                "rebased_from": candidate,
                                "dropped": "the fix replaces the marker",
                            }
                        ],
                    }
                ],
                {},
            ),
        )
        accepted, qa = self._accept_review_then_qa(brief)
        self.assertEqual((accepted["next_action"], qa["candidate_commit"]), ("qa", fix))

    def test_a_merge_of_the_target_mapped_as_its_own_rebased_copy_is_refused(
        self,
    ) -> None:
        """Issue #625: a rebase-fix-forward that merges the target instead of rebasing keeps the
        reviewed commit after the target. Mapped as its own rebased copy, it would count as
        reviewed by patch-id and loop the delta-review's origin walk inside the ledger lock, so
        report submit refuses it with the rebase as the remedy."""
        batch_id = cast(str, self._plan_batch(["add simple marker"])["batch_id"])
        self._accepted_architect(batch_id)
        _, candidate, changed = self._reported_developer(batch_id)
        self._decide(batch_id, "accept")
        self._assess_without_triggers(batch_id, candidate, changed)
        self._reported_review(
            batch_id, candidate, spec=("warning", [dict(self.SPEC_WARNING)])
        )
        upstream = self._push_upstream()
        self.assertEqual(
            self._routing(self._decide(batch_id, "retry"))["route"],
            "rebase-fix-forward",
        )
        retry = self._dispatch(batch_id, "developer")["brief"]
        self._start(retry["dispatch_id"])
        _git(self.worktree, "merge", "--no-ff", "--no-edit", upstream)
        merge = _git(self.worktree, "rev-parse", "HEAD")

        with self.assertRaises(coordinator.CoordinatorError) as refused:
            self._submit(
                retry["dispatch_id"],
                self._developer_report(
                    retry,
                    merge,
                    git_utils._changed_files_between(self.repo, upstream, merge),
                    commit_map=[
                        *self._rebased_map([candidate], [candidate]),
                        *self._commit_map([(merge, retry["commit_plan"][0])]),
                    ],
                ),
            )

        self.assertIn(
            f"rebased copies of themselves: ['{candidate}']", refused.exception.message
        )
        self.assertIn("git rebase --onto", refused.exception.remedy)
        self.assertNotIn("report", self._entry(retry["dispatch_id"]))

    def test_a_retried_delta_review_hands_its_open_findings_to_the_next_fix_forward(
        self,
    ) -> None:
        """Issue #625: a delta-review that finds the prior review's finding still open hands it on
        with its own finding, numbered after it, to the next developer-retry; the review after that
        fix-forward is a delta-review against this one and carries both items."""
        self._patch_config(retry_policy={"max_developer_retries": 2})
        review, _, _, fix = self._fix_forward_candidate(
            "services/x.py", "fix: pin the marker value"
        )
        delta = self._dispatch(self.batch_id, "code-review", candidate=fix)["brief"]
        self._start(delta["dispatch_id"], checkout=self.worktree)
        report = self._review_report(delta, {"review-finding-1": "open"})
        report["review"]["standards"].update(
            severity="warning", findings=[dict(self.REVIEW_WARNING)]
        )
        self._submit(delta["dispatch_id"], report)

        retried = self._decide(self.batch_id, "retry")
        retry = self._dispatch(self.batch_id, "developer")["brief"]

        routing = self._routing(retried)
        self.assertEqual(
            (routing["route"], routing["retry_item_ids"]),
            ("fix-forward", ["review-finding-1", "review-finding-2"]),
        )
        self.assertEqual(
            carried_items.section_item_ids(retry["carried_items"]),
            routing["retry_item_ids"],
        )
        self.assertEqual(
            [
                (item["source"]["dispatch_id"], item["summary"])
                for item in retry["carried_items"]["review-finding"]
            ],
            [
                (review["dispatch_id"], self.SPEC_WARNING["summary"]),
                (delta["dispatch_id"], self.REVIEW_WARNING["summary"]),
            ],
        )

        self._start(retry["dispatch_id"])
        again = self._commit_file("services/x.py", "fix: pin the marker value again")
        base = self._batch_record(self.batch_id)["base_commit"]
        changed = git_utils._changed_files_between(self.repo, base, again)
        self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                again,
                changed,
                commit_map=self._commit_map([(again, retry["commit_plan"][0])]),
            ),
        )
        self._decide(self.batch_id, "accept")
        self._assess_without_triggers(self.batch_id, again, changed)
        follow_up = self._dispatch(self.batch_id, "code-review", candidate=again)[
            "brief"
        ]

        scope = follow_up["delta_review_scope"]
        self.assertEqual(
            (
                scope["mode"],
                scope["prior_review"]["dispatch_id"],
                scope["delta_base"],
                scope["delta_commits"],
            ),
            ("delta", delta["dispatch_id"], fix, [again]),
        )
        self.assertEqual(
            carried_items.section_item_ids(follow_up["carried_items"]),
            ["review-finding-1", "review-finding-2"],
        )

    # -- incomplete items of a read-only role (issue #501) -------------------------------------

    def _reported_architect_with_items(
        self, *items: JsonObject, batch_id: str | None = None
    ) -> JsonObject:
        """An architect report that left ``items`` (default: one developer item) undone."""
        if batch_id is None:
            batch_id = cast(str, self._create_batch()["batch_id"])
        brief: JsonObject = self._dispatch(batch_id, "architect")["brief"]
        self._start(brief["dispatch_id"])
        self._submit(
            brief["dispatch_id"],
            self._base_report(
                brief,
                "architect",
                incomplete_items=list(items) or [dict(INCOMPLETE_ITEM)],
            ),
        )
        return brief

    def test_incomplete_items_are_validated_with_a_remedy(self) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        item = dict(INCOMPLETE_ITEM)
        for invalid, fragment in (
            ("one item", "must be a list"),
            (["text"], "invalid schema"),
            ([{"brief_item": "b", "target_role": "developer"}], "invalid schema"),
            ([{**item, "severity": "high"}], "invalid schema"),
            ([{**item, "reason": "  "}], "reason must be a non-empty string"),
            ([{**item, "brief_item": "x" * 1_601}], "brief_item exceeds"),
            ([{**item, "reason": "не успел"}], "must be written in English"),
            ([{**item, "target_role": "designer"}], "target_role 'designer'"),
            ([{**item, "target_role": "verification"}], "target_role 'verification'"),
            (
                [{**item, "tooling_blocker": {"tool": "hook"}}],
                "entry 1 tooling_blocker must carry exactly",
            ),
            (
                [{**item, "tooling_blocker": {**TOOLING_BLOCKER, "command": " "}}],
                "entry 1 tooling_blocker command must be a non-empty string",
            ),
        ):
            with self.subTest(invalid=invalid):
                report = self._base_report(brief, "architect", incomplete_items=invalid)
                with self.assertRaises(coordinator.CoordinatorError) as raised:
                    self._submit(brief["dispatch_id"], report)
                self.assertIn("incomplete_items", raised.exception.message)
                self.assertIn(fragment, raised.exception.message)
                self.assertTrue(raised.exception.remedy.strip())
        with self.assertRaises(coordinator.CoordinatorError) as unknown:
            self._submit(
                brief["dispatch_id"],
                self._base_report(
                    brief,
                    "architect",
                    incomplete_items=[{**item, "target_role": "designer"}],
                ),
            )
        self.assertIn("architect, developer, code-review, qa", unknown.exception.remedy)
        tooled = {
            **item,
            "target_role": "architect",
            "tooling_blocker": {
                "tool": "safety-classifier",
                "command": "rg -n rollback services/",
                "message": "The action was interrupted by the safety classifier",
            },
        }

        submitted = self._submit(
            brief["dispatch_id"],
            self._base_report(brief, "architect", incomplete_items=[item, tooled]),
        )

        self.assertEqual(submitted["state"], "reported")
        stored = json.loads(Path(submitted["report"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["incomplete_items"], [item, tooled])
        markdown = (
            Path(submitted["report"]).with_suffix(".md").read_text(encoding="utf-8")
        )
        self.assertIn(f"[developer] {item['brief_item']}: {item['reason']}", markdown)
        self.assertIn("tooling blocker: safety-classifier", markdown)

    def test_incomplete_items_are_limited_to_read_only_stages_and_their_targets(
        self,
    ) -> None:
        read_only = {"mode": "read-only"}
        for stage, role, target, allowed in (
            ("architect", read_only, "qa", True),
            ("verification", read_only, "code-review", True),
            ("code-review", read_only, "qa", True),
            ("qa", read_only, "qa", True),
            ("qa", read_only, "developer", False),
            ("code-review", read_only, "architect", False),
            ("verification", read_only, "developer", False),
            ("developer", {"mode": "write"}, "developer", False),
        ):
            report = {
                "role": stage,
                "incomplete_items": [{**INCOMPLETE_ITEM, "target_role": target}],
            }
            with self.subTest(stage=stage, target=target):
                if allowed:
                    reports._validate_incomplete_items(report, role)
                    continue
                with self.assertRaises(coordinator.CoordinatorError) as raised:
                    reports._validate_incomplete_items(report, role)
                self.assertIn("incomplete_items", raised.exception.message)
                self.assertTrue(raised.exception.remedy.strip())

    def test_a_report_with_incomplete_items_is_never_auto_accepted(self) -> None:
        clean: JsonObject = {
            "outcome": "completed",
            "blockers": "none",
            "risks": "none",
            "checks_run": [],
        }
        for policy in ("low_risk", "milestone", "auto"):
            with self.subTest(policy=policy, path="policy"):
                config_ = {"approval_policy": policy, "low_risk_paths": ["**"]}
                batch = {"approval_policy": policy, "allowed_paths": ["services/a.py"]}
                architect = {"role": "architect", "purpose": "work"}
                self.assertEqual(
                    decisions._auto_accept_policy(config_, batch, architect, clean),
                    policy,
                )
                self.assertIsNone(
                    decisions._auto_accept_policy(
                        config_,
                        batch,
                        architect,
                        {**clean, "incomplete_items": [dict(INCOMPLETE_ITEM)]},
                    )
                )
            with self.subTest(policy=policy, path="report submit"):
                self._reset()
                self._patch_config(approval_policy=policy, low_risk_paths=["**"])
                brief = self._reported_architect_with_items()
                stored = self._batch_record(self.batch_id)
                self.assertNotIn("decision", stored["dispatches"][0])
                self.assertEqual(stored["state"], "awaiting-approval")
                self.assertEqual(
                    stored["dispatches"][-1]["dispatch_id"], brief["dispatch_id"]
                )

    def _root(self) -> Path:
        return ledger_ops._state_root(self._args(), self.repo)

    def _incomplete(self, brief: JsonObject, *items: JsonObject) -> JsonObject:
        """The incomplete-item section a decision recorded for ``brief``'s report, as a later
        brief carries it; ``items`` pairs each item id with its ``source`` overrides."""
        report_sha256 = self._report_evidence(self.batch_id, brief["dispatch_id"])[
            "report_sha256"
        ]
        rows = []
        for item in items:
            target = item["target_role"]
            rows.append(
                {
                    "item_id": item["item_id"],
                    "source": {
                        "kind": "incomplete-item",
                        "dispatch_id": brief["dispatch_id"],
                        "report_sha256": report_sha256,
                        "role": brief["role"],
                        "target_role": target,
                        "route": item["route"],
                        "reason": INCOMPLETE_ITEM["reason"],
                        "reason_category": item.get("reason_category"),
                    },
                    "summary": INCOMPLETE_ITEM["brief_item"],
                    "files": [],
                    "expected_evidence": f"The {target} completion report shows this "
                    "brief item done.",
                }
            )
        return {"incomplete-item": rows}

    def test_a_plain_accept_of_a_report_with_incomplete_items_is_refused(
        self,
    ) -> None:
        self._reported_architect_with_items()
        before = self._batch_record(self.batch_id)

        with self.assertRaises(coordinator.CoordinatorError) as raised:
            self._decide(self.batch_id, "accept")

        self.assertIn("1 brief item(s) undone", raised.exception.message)
        self.assertIn("--carry-incomplete", raised.exception.remedy)
        self.assertIn("--narrowed", raised.exception.remedy)
        self.assertEqual(self._batch_record(self.batch_id), before)

    def test_carry_incomplete_needs_items_an_accept_and_a_later_target_role(
        self,
    ) -> None:
        batch_id = cast(str, self._create_batch()["batch_id"])
        self._reported_architect(batch_id)
        with self.assertRaises(coordinator.CoordinatorError) as clean:
            self._decide(batch_id, "accept", carry_incomplete=True)
        self._reset()
        self._reported_architect_with_items()
        with self.assertRaises(coordinator.CoordinatorError) as retried:
            self._decide(self.batch_id, "retry", carry_incomplete=True)
        self._reset()
        self._reported_architect_with_items(
            dict(INCOMPLETE_ITEM), {**INCOMPLETE_ITEM, "target_role": "architect"}
        )
        before = self._batch_record(self.batch_id)

        with self.assertRaises(coordinator.CoordinatorError) as own:
            self._decide(self.batch_id, "accept", carry_incomplete=True)

        for refused in (clean, retried):
            self.assertIn("--carry-incomplete", refused.exception.message)
        self.assertIn(
            "incomplete items [2] target the architect", own.exception.message
        )
        self.assertIn("--decision retry --narrowed", own.exception.remedy)
        self.assertEqual(self._batch_record(self.batch_id), before)

    def test_an_accept_with_carry_incomplete_hands_items_on_until_their_role_is_accepted(
        self,
    ) -> None:
        architect = self._reported_architect_with_items()

        decided = self._decide(self.batch_id, "accept", carry_incomplete=True)

        decision = decided["coordinator_decisions"][-1]
        routing = dict(decision["routing"])
        self.assertTrue(routing.pop("rationale").strip())
        self.assertEqual(
            routing,
            {
                "route": "carry-over",
                "previous_role": "architect",
                "reason_category": None,
                "next_role": "developer",
                "next_action": "developer",
                "candidate_commit": None,
                "carried_item_ids": ["incomplete-item-1"],
            },
        )
        self.assertNotIn("next_role", decision)
        self.assertEqual(decided["next_action"], "developer")
        self.assertNotIn("carried_items", decided, "nothing is stored on the batch")
        audit = self._decision_audits(self.batch_id)[-1]
        self.assertEqual((audit["decision"], audit["route"]), ("accept", "carry-over"))
        expected = self._incomplete(
            architect,
            {
                "item_id": "incomplete-item-1",
                "target_role": "developer",
                "route": "carry-over",
            },
        )

        developer = self._dispatch(self.batch_id, "developer")["brief"]

        self.assertEqual(developer["carried_items"], expected)
        self.assertEqual(
            developer["transition"]["carried_items_sha256"],
            operational_guards.carried_items_digest(expected),
        )
        self._start(developer["dispatch_id"])
        candidate, changed = self._developer_commit("x")
        self._submit(
            developer["dispatch_id"],
            self._developer_report(developer, candidate, changed),
        )
        self._decide(self.batch_id, "retry", reason_category="code")
        retry = self._dispatch(self.batch_id, "developer")["brief"]
        self.assertEqual(retry["carried_items"], expected, "a retry leaves it open")
        self._start(retry["dispatch_id"])
        fix, _ = self._developer_commit("fix")
        fixed = git_utils._changed_files_between(
            self.repo, self._batch_record(self.batch_id)["base_commit"], fix
        )
        self._submit(
            retry["dispatch_id"],
            self._developer_report(
                retry,
                fix,
                fixed,
                commit_map=self._commit_map([(fix, retry["commit_plan"][0])]),
            ),
        )

        done = self._decide(self.batch_id, "accept")

        self.assertEqual(
            carried_items.open_incomplete_items(self._root(), done, "developer"), []
        )

    def test_an_incomplete_item_for_code_review_sends_a_trigger_free_candidate_to_review(
        self,
    ) -> None:
        batch_id = cast(str, self._plan_batch(["add simple marker"])["batch_id"])
        self._reported_architect_with_items(
            {**INCOMPLETE_ITEM, "target_role": "code-review"}, batch_id=batch_id
        )
        self._decide(batch_id, "accept", carry_incomplete=True)
        developer, candidate, changed = self._reported_developer(batch_id)
        self.assertEqual(developer["carried_items"], {})
        self._decide(batch_id, "accept")

        self._assess_without_triggers(batch_id, candidate, changed)

        assessed = self._batch_record(batch_id)
        self.assertFalse(assessed["risk_assessments"][-1]["review_required"])
        self.assertEqual(assessed["next_action"], "code-review")
        review = self._reported_review(
            batch_id, candidate, carried={"incomplete-item-1": "closed"}
        )
        self.assertEqual(
            [item["item_id"] for item in self._carried(review)], ["incomplete-item-1"]
        )
        done = self._decide(batch_id, "accept")
        self.assertEqual(done["next_action"], "qa")
        self.assertEqual(
            carried_items.open_incomplete_items(self._root(), done, "code-review"), []
        )

    def test_the_decision_packet_shows_incomplete_items_and_previews_their_carry_over(
        self,
    ) -> None:
        self._reported_architect_with_items()
        before = self._batch_record(self.batch_id)

        packet = self._packet()

        self.assertEqual(packet["incomplete_items"], [INCOMPLETE_ITEM])
        self.assertEqual(
            set(packet["route_preview"]), {"retry", "abandon", "carry-over"}
        )
        self.assertEqual(self._batch_record(self.batch_id), before)
        decided = self._decide(self.batch_id, "accept", carry_incomplete=True)
        self.assertEqual(
            decided["coordinator_decisions"][-1]["routing"],
            packet["route_preview"]["carry-over"],
        )
        self._reset()
        self._reported_architect_with_items(
            {**INCOMPLETE_ITEM, "target_role": "architect"}
        )

        refused = self._packet()["route_preview"]["carry-over"]

        self.assertIsNone(refused["route"])
        self.assertIn("target the architect role itself", refused["refused"])
        self.assertIn("--narrowed", refused["remedy"])

    def test_a_narrowed_review_retry_reruns_on_the_same_candidate_with_its_items_only(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        review = self._reported_review(
            batch["batch_id"],
            candidate,
            incomplete_items=[{**INCOMPLETE_ITEM, "target_role": "code-review"}],
        )
        before = self._batch_record(self.batch_id)

        plain = self._packet()["route_preview"]["retry"]
        preview = self._packet(narrowed=True)["route_preview"]["retry"]
        self.assertEqual(self._batch_record(self.batch_id), before)

        decided = self._decide(self.batch_id, "retry", narrowed=True)

        self.assertEqual(plain["route"], "developer-retry", "unflagged, as before")
        routing = self._routing(decided)
        self.assertEqual(
            (
                routing["next_role"],
                routing["next_action"],
                routing["reason_category"],
                routing["candidate_commit"],
                routing["route"],
                routing["carried_item_ids"],
            ),
            (
                "code-review",
                "code-review",
                None,
                candidate,
                "narrowed-retry",
                ["incomplete-item-1"],
            ),
        )
        self.assertEqual(decided["next_action"], "code-review")
        recorded = dict(routing)
        recorded.pop("decided_at")
        self.assertEqual(recorded, preview)
        self.assertEqual(decisions._developer_retry_count(decided), 0)
        self.assertFalse(decided.get("needs_attention", False))
        expected = self._incomplete(
            review,
            {
                "item_id": "incomplete-item-1",
                "target_role": "code-review",
                "route": "narrowed-retry",
            },
        )
        narrowed = self._reported_review(
            self.batch_id, candidate, carried={"incomplete-item-1": "closed"}
        )
        self.assertEqual(narrowed["candidate_commit"], candidate)
        self.assertEqual(narrowed["carried_items"], expected)

        done = self._decide(self.batch_id, "accept")

        self.assertEqual(done["next_action"], "qa")
        self.assertEqual(
            carried_items.brief_section(self._root(), done, "code-review", "work"),
            {},
            "the narrowed items reach only the retry's brief",
        )

    def test_a_narrowed_verification_retry_reruns_on_the_registered_candidate(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        candidate, changed = self._developer_commit("infrastructure")
        self._submit(
            developer["dispatch_id"],
            self._developer_report(
                developer,
                candidate,
                changed,
                outcome="blocked",
                blockers="verification environment unavailable",
            ),
        )
        self._decide(
            batch["batch_id"], "retry", reason_category="verification-infrastructure"
        )
        verification = self._dispatch(
            batch["batch_id"], "verification", candidate=candidate
        )["brief"]
        self._start(verification["dispatch_id"], checkout=self.worktree)
        self._submit(
            verification["dispatch_id"],
            self._base_report(
                verification,
                "verification",
                incomplete_items=[{**INCOMPLETE_ITEM, "target_role": "verification"}],
            ),
        )

        decided = self._decide(batch["batch_id"], "retry", narrowed=True)

        routing = self._routing(decided)
        self.assertEqual(
            (
                routing["next_action"],
                routing["reason_category"],
                routing["route"],
                routing["candidate_commit"],
            ),
            ("verification", None, "narrowed-retry", candidate),
        )
        self.assertEqual(len(decided["candidate_registrations"]), 1)
        again = self._dispatch(batch["batch_id"], "verification", candidate=candidate)
        self.assertEqual(again["brief"]["candidate_commit"], candidate)
        self.assertEqual(
            [item["item_id"] for item in self._carried(again["brief"])],
            ["incomplete-item-1"],
        )

    def test_narrowed_needs_a_retry_of_a_report_with_items_and_no_other_route_flag(
        self,
    ) -> None:
        self._reported_architect_with_items()
        before = self._batch_record(self.batch_id)
        refusals = []
        for decision, extra in (
            ("accept", {"narrowed": True}),
            ("retry", {"narrowed": True, "retry_role": "developer"}),
            ("retry", {"narrowed": True, "reason_category": "transport"}),
        ):
            with self.assertRaises(coordinator.CoordinatorError) as raised:
                self._decide(self.batch_id, decision, **extra)
            refusals.append(raised.exception)
        self.assertIn("only valid with --decision retry", refusals[0].message)
        self.assertIn("cannot force a developer retry", refusals[1].message)
        self.assertIn("takes no --reason-category", refusals[2].message)
        for refused in refusals:
            self.assertTrue(refused.remedy.strip())
        self.assertEqual(self._batch_record(self.batch_id), before)
        self._reset()
        batch_id = cast(str, self._create_batch()["batch_id"])
        self._reported_architect(batch_id)

        with self.assertRaises(coordinator.CoordinatorError) as clean:
            self._decide(batch_id, "retry", narrowed=True)

        self.assertIn("lists incomplete_items", clean.exception.message)

    def test_issue_443_an_architect_that_left_one_item_undone_reruns_on_that_item_only(
        self,
    ) -> None:
        """Regression for #443 (situation 6 of #479): the safety classifier interrupted the
        architect on one brief item, and the only way forward was to re-run the whole architect
        assignment. Now the report lists that item, a narrowed retry re-runs the architect on it
        alone as a tooling-retry, and no developer retry is spent."""
        interrupted = {
            **INCOMPLETE_ITEM,
            "target_role": "architect",
            "tooling_blocker": {
                "tool": "safety-classifier",
                "command": "rg -n rollback services/",
                "message": "The action was interrupted by the safety classifier",
            },
        }
        first = self._reported_architect_with_items(interrupted)

        retried = self._decide(self.batch_id, "retry", narrowed=True)

        routing = self._assert_route(
            retried,
            role="architect",
            action="architect",
            category="tooling",
            candidate=None,
            route="tooling-retry",
        )
        self.assertEqual(routing["carried_item_ids"], ["incomplete-item-1"])
        self.assertEqual(decisions._developer_retry_count(retried), 0)
        self.assertFalse(retried.get("needs_attention", False))
        narrowed = self._dispatch(self.batch_id, "architect")["brief"]
        self.assertEqual(
            narrowed["carried_items"],
            self._incomplete(
                first,
                {
                    "item_id": "incomplete-item-1",
                    "target_role": "architect",
                    "route": "tooling-retry",
                    "reason_category": "tooling",
                },
            ),
        )
        self.assertEqual(narrowed["definition_of_done"], first["definition_of_done"])
        self._start(narrowed["dispatch_id"])
        self._submit(narrowed["dispatch_id"], self._base_report(narrowed, "architect"))

        accepted = self._decide(self.batch_id, "accept")

        self.assertEqual(accepted["next_action"], "developer")
        self.assertEqual(
            [entry["role"] for entry in accepted["dispatches"]],
            ["architect", "architect"],
        )
        developer = self._dispatch(self.batch_id, "developer")["brief"]
        self.assertEqual(developer["carried_items"], {})


class CoordinatorRetryRoutingTableTests(unittest.TestCase):
    """The pure routing table: structured evidence in, one routing record out (no I/O)."""

    CANDIDATE = "c" * 40

    def _report(
        self,
        outcome: str = "blocked",
        *,
        standards: tuple[str, list[JsonObject]] | None = ("none", []),
        spec: tuple[str, list[JsonObject]] = ("none", []),
        failed_check: bool = False,
        tooling: bool = False,
    ) -> JsonObject:
        report: JsonObject = {
            "outcome": outcome,
            "checks_run": [
                {
                    "command": "true",
                    "result": "fail" if failed_check else "not-run",
                    "evidence": "e",
                }
            ],
            "blockers": "Bash/WSL wrapper unavailable",
        }
        if tooling:
            report["tooling_blocker"] = dict(TOOLING_BLOCKER)
        if standards is not None:
            report["review"] = {
                "standards": {"severity": standards[0], "findings": standards[1]},
                "spec": {"severity": spec[0], "findings": spec[1]},
            }
        return report

    def _route(
        self,
        stage: str,
        report: JsonObject,
        category: str | None,
        *,
        moved: bool = False,
    ) -> JsonObject:
        return decisions._retry_routing(
            stage,
            report,
            dispatch_candidate=self.CANDIDATE,
            current_candidate="d" * 40 if moved else self.CANDIDATE,
            explicit_category=category,
        )

    def _routing_cases(
        self,
    ) -> list[tuple[str, JsonObject, str | None, bool, tuple[str, str, str, str]]]:
        """Every routing-table row: stage, report, explicit category, candidate moved -> expectation."""
        finding = [{"severity": "warning", "summary": "s", "evidence": "e"}]
        infra, transport = "verification-infrastructure", "transport"
        review = self._report()
        no_review = self._report(standards=None)
        tooled = self._report(tooling=True)
        tooled_no_review = self._report(standards=None, tooling=True)
        return [
            # stage, report, explicit category, candidate moved -> (reason category, next role, next action, route)
            # A blocked report's tooling_blocker re-runs the same stage; a developer continues.
            *(
                (
                    stage,
                    tooled if stage == "code-review" else tooled_no_review,
                    category,
                    False,
                    ("tooling", role, action, "tooling-retry"),
                )
                for stage, role, action in (
                    ("architect", "architect", "architect"),
                    ("developer", "developer", "developer-retry"),
                    ("verification", "verification", "verification"),
                    ("code-review", "code-review", "code-review"),
                    ("qa", "qa", "qa"),
                    ("publish", "publish", "publish"),
                )
                for category in (None, "tooling")
            ),
            # A read-only role that worked around a block re-runs on the same SHA: its findings,
            # failed checks and outcome are no evidence, only a moved candidate still counts.
            *(
                (
                    stage,
                    report,
                    "block-bypass",
                    False,
                    ("block-bypass", stage, stage, "bypass-rerun"),
                )
                for stage, report in (
                    (
                        "code-review",
                        self._report("completed", standards=("warning", finding)),
                    ),
                    ("qa", self._report("failed", standards=None, failed_check=True)),
                    ("verification", self._report("completed", standards=None)),
                )
            ),
            (
                "code-review",
                review,
                "block-bypass",
                True,
                ("candidate-change", "developer", "developer-retry", "developer-retry"),
            ),
            # tooling named without the structured field stays unknown.
            (
                "code-review",
                review,
                "tooling",
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "developer",
                no_review,
                "tooling",
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                self._report("completed", tooling=True),
                None,
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            # Structured evidence and a developer category outrank the tooling blocker.
            (
                "qa",
                self._report(standards=None, failed_check=True, tooling=True),
                None,
                False,
                ("code", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                self._report(spec=("warning", finding), tooling=True),
                "tooling",
                False,
                ("requirements", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                tooled,
                None,
                True,
                ("candidate-change", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                tooled,
                "code",
                False,
                ("code", "developer", "developer-retry", "developer-retry"),
            ),
            # Another named operational category keeps its own route.
            (
                "code-review",
                tooled,
                transport,
                False,
                (transport, "code-review", "code-review", "same-candidate-rerun"),
            ),
            (
                "code-review",
                review,
                infra,
                False,
                (infra, "code-review", "code-review", "same-candidate-rerun"),
            ),
            (
                "code-review",
                review,
                transport,
                False,
                (transport, "code-review", "code-review", "same-candidate-rerun"),
            ),
            (
                "code-review",
                review,
                None,
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                review,
                "unknown",
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                review,
                "code",
                False,
                ("code", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                review,
                "requirements",
                False,
                ("requirements", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                review,
                "candidate-change",
                False,
                ("candidate-change", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                self._report(standards=("warning", finding)),
                transport,
                False,
                ("code", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                self._report(spec=("blocker", finding)),
                transport,
                False,
                ("requirements", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                self._report(spec=("warning", [])),
                transport,
                False,
                ("requirements", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                self._report("completed"),
                transport,
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                self._report("failed"),
                transport,
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "code-review",
                review,
                infra,
                True,
                ("candidate-change", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "qa",
                no_review,
                infra,
                False,
                (infra, "qa", "qa", "same-candidate-rerun"),
            ),
            (
                "qa",
                no_review,
                None,
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "qa",
                self._report("failed", standards=None, failed_check=True),
                infra,
                False,
                ("code", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "publish",
                no_review,
                transport,
                False,
                (transport, "publish", "publish", "same-candidate-rerun"),
            ),
            (
                "publish",
                no_review,
                infra,
                False,
                (infra, "publish", "publish", "same-candidate-rerun"),
            ),
            (
                "publish",
                no_review,
                "candidate-change",
                False,
                ("candidate-change", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "publish",
                no_review,
                None,
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "developer",
                no_review,
                transport,
                False,
                (transport, "developer", "developer-retry", "developer-retry"),
            ),
            (
                "developer",
                self._report("completed", standards=None),
                transport,
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "verification",
                no_review,
                transport,
                False,
                (transport, "developer", "developer-retry", "developer-retry"),
            ),
            (
                "verification",
                self._report("completed", standards=None),
                infra,
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "verification",
                no_review,
                None,
                False,
                ("unknown", "developer", "developer-retry", "developer-retry"),
            ),
            (
                "architect",
                no_review,
                transport,
                False,
                (transport, "architect", "architect", "architect-retry"),
            ),
            (
                "architect",
                no_review,
                None,
                False,
                ("unknown", "architect", "architect", "architect-retry"),
            ),
        ]

    def test_routing_table(self) -> None:
        for stage, report, category, moved, (
            reason,
            role,
            action,
            route,
        ) in self._routing_cases():
            with self.subTest(
                stage=stage, category=category, moved=moved, outcome=report["outcome"]
            ):
                routing = self._route(stage, report, category, moved=moved)
                self.assertEqual(
                    (
                        routing["reason_category"],
                        routing["next_role"],
                        routing["next_action"],
                        routing["route"],
                    ),
                    (reason, role, action, route),
                )
                self.assertEqual(routing["previous_role"], stage)
                self.assertTrue(routing["rationale"].strip())
                self.assertEqual(
                    routing["candidate_commit"], None if moved else self.CANDIDATE
                )

    def test_the_reason_categories_are_exactly_the_documented_nine(self) -> None:
        self.assertEqual(
            set(constants.RETRY_REASON_CATEGORIES),
            {
                "code",
                "requirements",
                "candidate-change",
                "verification-infrastructure",
                "transport",
                "context-pressure",
                "tooling",
                "block-bypass",
                "unknown",
            },
        )
        # tooling and block-bypass have their own routes, never the operational ones.
        self.assertNotIn("tooling", constants.OPERATIONAL_REASON_CATEGORIES)
        self.assertNotIn("block-bypass", constants.OPERATIONAL_REASON_CATEGORIES)

    def test_block_bypass_is_refused_for_a_writing_role(self) -> None:
        for stage in ("architect", "developer", "publish"):
            with self.subTest(stage=stage):
                with self.assertRaises(coordinator.CoordinatorError) as raised:
                    self._route(stage, self._report(standards=None), "block-bypass")
                self.assertIn("developer reason category", raised.exception.remedy)

    def test_the_recovery_routes_are_exactly_the_documented_thirteen(self) -> None:
        self.assertEqual(
            constants.RECOVERY_ROUTES,
            (
                "developer-retry",
                "same-candidate-rerun",
                "verification",
                "architect-retry",
                "abandon",
                "report-completion",
                "carry-over",
                "tooling-retry",
                "bypass-rerun",
                "narrowed-retry",
                "fix-forward",
                "rebase-fix-forward",
                "supersede",
            ),
        )
        for route in constants.RECOVERY_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(history._require_route(route), route)
                self.assertEqual(history._require_route(route, recorded=True), route)

    def test_a_route_outside_the_enum_is_rejected_with_the_allowed_routes(
        self,
    ) -> None:
        for value, recorded in (
            ("rebase", False),
            ("rebase", True),
            (None, False),
            ("", True),
        ):
            with self.subTest(value=value, recorded=recorded):
                with self.assertRaises(coordinator.CoordinatorError) as raised:
                    history._require_route(value, recorded=recorded)
                for route in constants.RECOVERY_ROUTES:
                    self.assertIn(route, raised.exception.remedy)

    def test_a_recorded_route_outside_the_enum_is_rejected_on_read_back(
        self,
    ) -> None:
        routing = {"route": "rebase", "next_action": "developer-retry"}
        for batch in (
            {"dispatches": [{"decision": {"decision": "retry", "routing": routing}}]},
            {"coordinator_decisions": [{"decision": "retry", "routing": routing}]},
        ):
            with self.subTest(batch=sorted(batch)):
                with self.assertRaises(coordinator.CoordinatorError) as raised:
                    history._validate_operational_batch_fields(batch)
                self.assertIn("same-candidate-rerun", raised.exception.remedy)
        # A routing record written before the route field existed carries no route and stays valid.
        history._validate_operational_batch_fields(
            {
                "dispatches": [
                    {
                        "decision": {
                            "decision": "retry",
                            "routing": {"next_action": "qa"},
                        }
                    }
                ],
                "coordinator_decisions": [
                    {"decision": "retry", "routing": {"next_action": "qa"}},
                    {"decision": "abandon"},
                ],
            }
        )

    def test_context_pressure_needs_a_recorded_observation_and_routes_like_an_operational_cause(
        self,
    ) -> None:
        finding = [{"severity": "warning", "summary": "s", "evidence": "e"}]
        for stage in ("code-review", "qa", "publish"):
            report = self._report(
                standards=("none", []) if stage == "code-review" else None
            )
            with self.subTest(stage=stage):
                proven = decisions._retry_routing(
                    stage,
                    report,
                    dispatch_candidate=self.CANDIDATE,
                    current_candidate=self.CANDIDATE,
                    explicit_category="context-pressure",
                    pressure_recorded=True,
                )
                unproven = decisions._retry_routing(
                    stage,
                    report,
                    dispatch_candidate=self.CANDIDATE,
                    current_candidate=self.CANDIDATE,
                    explicit_category="context-pressure",
                    pressure_recorded=False,
                )
                self.assertEqual(
                    (
                        proven["reason_category"],
                        proven["next_role"],
                        proven["next_action"],
                    ),
                    ("context-pressure", stage, stage),
                )
                self.assertEqual(
                    (
                        unproven["reason_category"],
                        unproven["next_role"],
                        unproven["next_action"],
                    ),
                    ("unknown", "developer", "developer-retry"),
                )
        flagged = decisions._retry_routing(
            "code-review",
            self._report(standards=("warning", finding)),
            dispatch_candidate=self.CANDIDATE,
            current_candidate=self.CANDIDATE,
            explicit_category="context-pressure",
            pressure_recorded=True,
        )
        self.assertEqual(
            (flagged["reason_category"], flagged["next_action"]),
            ("code", "developer-retry"),
        )
        moved = decisions._retry_routing(
            "qa",
            self._report(standards=None),
            dispatch_candidate=self.CANDIDATE,
            current_candidate="d" * 40,
            explicit_category="context-pressure",
            pressure_recorded=True,
        )
        self.assertEqual(
            (moved["reason_category"], moved["next_action"]),
            ("candidate-change", "developer-retry"),
        )

    def test_an_unlisted_category_is_rejected(self) -> None:
        with self.assertRaises(coordinator.CoordinatorError):
            self._route("code-review", self._report(), "flaky-network")

    def test_the_reason_is_never_derived_from_free_text(self) -> None:
        report = self._report()
        report["blockers"] = "verification-infrastructure: the wrapper is down"

        routing = self._route("code-review", report, None)

        self.assertEqual(
            (routing["reason_category"], routing["next_action"]),
            ("unknown", "developer-retry"),
        )

    def test_free_text_wording_never_changes_the_route(self) -> None:
        wordings = (
            {"blockers": "none", "output": "done"},
            {
                "blockers": "verification-infrastructure: the wrapper is down; transport lost; context limit hit",
                "output": "same-candidate-rerun please; route=verification; abandon",
            },
            {
                "blockers": "a code defect and unclear requirements",
                "output": "the candidate changed; needs a developer-retry",
            },
        )
        for stage, report, category, moved, expected in self._routing_cases():
            routes = [
                self._route(stage, {**report, **wording}, category, moved=moved)
                for wording in wordings
            ]
            with self.subTest(stage=stage, category=category, moved=moved):
                self.assertEqual(routes[0]["route"], expected[3])
                for routing in routes[1:]:
                    self.assertEqual(routing, routes[0])

    def _narrowed(
        self,
        stage: str,
        report: JsonObject,
        category: str | None = None,
        *,
        moved: bool = False,
    ) -> JsonObject:
        return decisions._retry_routing(
            stage,
            report,
            dispatch_candidate=self.CANDIDATE,
            current_candidate="d" * 40 if moved else self.CANDIDATE,
            explicit_category=category,
            narrowed=True,
        )

    def _itemised(self, stage: str, *items: JsonObject, **report: object) -> JsonObject:
        """A completed ``stage`` report (a clean review for code-review) listing ``items``."""
        base = self._report(
            "completed",
            standards=("clean", []) if stage == "code-review" else None,
            spec=("clean", []),
        )
        return {**base, "incomplete_items": list(items), **report}

    def test_a_narrowed_retry_reruns_the_read_only_stage_on_its_items(self) -> None:
        tooled = {**INCOMPLETE_ITEM, "tooling_blocker": dict(TOOLING_BLOCKER)}
        for stage in ("architect", "verification", "code-review", "qa"):
            for items, (category, route) in (
                ([INCOMPLETE_ITEM], (None, "narrowed-retry")),
                ([INCOMPLETE_ITEM, tooled], ("tooling", "tooling-retry")),
            ):
                with self.subTest(stage=stage, route=route):
                    routing = self._narrowed(stage, self._itemised(stage, *items))
                    self.assertEqual(
                        (
                            routing["route"],
                            routing["reason_category"],
                            routing["previous_role"],
                            routing["next_role"],
                            routing["next_action"],
                            routing["candidate_commit"],
                        ),
                        (route, category, stage, stage, stage, self.CANDIDATE),
                    )
                    self.assertTrue(routing["rationale"].strip())

    def test_a_narrowed_retry_never_sets_aside_a_route_or_structured_evidence(
        self,
    ) -> None:
        finding = [{"severity": "warning", "summary": "s", "evidence": "e"}]
        failed = [{"command": "true", "result": "fail", "evidence": "e"}]
        open_item = {
            "candidate_commit": self.CANDIDATE,
            "standards": {"severity": "clean", "findings": []},
            "spec": {"severity": "clean", "findings": []},
            "carried_items": [
                {"item_id": "coordinator-finding-1", "status": "open", "evidence": "e"}
            ],
        }
        for label, stage, report, category, moved, fragment in (
            (
                "developer",
                "developer",
                self._itemised("developer", INCOMPLETE_ITEM),
                None,
                False,
                "re-runs only",
            ),
            (
                "publish",
                "publish",
                self._itemised("publish", INCOMPLETE_ITEM),
                None,
                False,
                "re-runs only",
            ),
            (
                "no items",
                "architect",
                self._itemised("architect"),
                None,
                False,
                "lists",
            ),
            (
                "category",
                "qa",
                self._itemised("qa", INCOMPLETE_ITEM),
                "transport",
                False,
                "takes no --reason-category",
            ),
            (
                "finding",
                "code-review",
                {
                    **self._report("completed", standards=("warning", finding)),
                    "incomplete_items": [INCOMPLETE_ITEM],
                },
                None,
                False,
                "standards axis",
            ),
            (
                "open carried item",
                "code-review",
                self._itemised("code-review", INCOMPLETE_ITEM, review=open_item),
                None,
                False,
                "carried item is still open",
            ),
            (
                "failed check",
                "qa",
                self._itemised("qa", INCOMPLETE_ITEM, checks_run=failed),
                None,
                False,
                "check failed",
            ),
            (
                "moved candidate",
                "qa",
                self._itemised("qa", INCOMPLETE_ITEM),
                None,
                True,
                "candidate changed",
            ),
        ):
            with self.subTest(label):
                with self.assertRaises(coordinator.CoordinatorError) as raised:
                    self._narrowed(stage, report, category, moved=moved)
                self.assertIn(fragment, raised.exception.message)
                self.assertIn("drop", raised.exception.remedy)


class CarriedItemsFindingsFileTests(unittest.TestCase):
    """The coordinator findings file (issue #499): validated as plain data, no ledger."""

    FINDING: JsonObject = {
        "summary": "the retry counter is never reset",
        "files": ["services/x.py", "tests/test_x.py"],
        "expected_evidence": "a test that sees the counter at zero",
    }

    def test_a_well_formed_file_yields_its_findings_in_order(self) -> None:
        second = {**self.FINDING, "summary": "a second defect"}

        findings = carried_items.parse_findings({"findings": [self.FINDING, second]})

        self.assertEqual(findings, [self.FINDING, second])

    def test_a_malformed_file_is_refused_with_the_file_shape_as_remedy(
        self,
    ) -> None:
        cases: dict[str, object] = {
            "not an object": ["findings"],
            "an extra key": {"findings": [self.FINDING], "note": "x"},
            "no findings": {"findings": []},
            "a missing field": {"findings": [{"summary": "s", "files": ["a.py"]}]},
            "an extra field": {"findings": [{**self.FINDING, "severity": "x"}]},
            "an empty summary": {"findings": [{**self.FINDING, "summary": " "}]},
            "no expected evidence": {
                "findings": [{**self.FINDING, "expected_evidence": ""}]
            },
            "no files": {"findings": [{**self.FINDING, "files": []}]},
            "an absolute file": {"findings": [{**self.FINDING, "files": ["/x.py"]}]},
            "a parent segment": {
                "findings": [{**self.FINDING, "files": ["a/../../x.py"]}]
            },
            "a backslash": {"findings": [{**self.FINDING, "files": ["a\\x.py"]}]},
            "a repeated file": {
                "findings": [{**self.FINDING, "files": ["x.py", "x.py"]}]
            },
            "non-English text": {
                "findings": [{**self.FINDING, "summary": "счётчик не сброшен"}]
            },
        }
        for label, document in cases.items():
            with self.subTest(label):
                with self.assertRaises(coordinator.CoordinatorError) as refused:
                    carried_items.parse_findings(document)
                self.assertIn("findings", refused.exception.message)
                self.assertIn("--findings-file", refused.exception.remedy)


class CarriedItemsReviewAccountingTests(unittest.TestCase):
    """How a code-review report accounts for the items its brief carried (issue #499)."""

    CANDIDATE = "c" * 40
    ITEM: JsonObject = {
        "item_id": "coordinator-finding-1",
        "source": {"kind": "coordinator-finding"},
        "summary": "the retry counter is never reset",
        "files": ["services/x.py"],
        "expected_evidence": "a test that sees the counter at zero",
    }
    CLOSED: JsonObject = {
        "item_id": "coordinator-finding-1",
        "status": "closed",
        "evidence": "services/x.py:1",
    }

    def _brief(self, *, carried: bool = True) -> JsonObject:
        return {
            "candidate_commit": self.CANDIDATE,
            "review_scope": ["services/x.py"],
            "delta_review_of": None,
            "delta_review_axis": None,
            "carried_items": {"coordinator-finding": [self.ITEM]} if carried else {},
        }

    def _review(self, accounting: object = None) -> JsonObject:
        axis = {"severity": "none", "findings": [], "risks": "none", "blockers": "none"}
        review: JsonObject = {
            "candidate_commit": self.CANDIDATE,
            "scope": ["services/x.py"],
            "standards": dict(axis),
            "spec": dict(axis),
        }
        if accounting is not None:
            review["carried_items"] = accounting
        return review

    def test_the_accounting_is_checked_against_the_brief(self) -> None:
        reports._validate_review(self._review([self.CLOSED]), self._brief())
        reports._validate_review(self._review(), self._brief())
        reports._validate_review(self._review([]), self._brief(carried=False))
        cases: dict[str, tuple[object, bool]] = {
            "not a list": ("closed", True),
            "an unknown item": ([{**self.CLOSED, "item_id": "x-9"}], True),
            "a repeated item": ([self.CLOSED, self.CLOSED], True),
            "an unknown status": ([{**self.CLOSED, "status": "fixed"}], True),
            "no evidence": ([{**self.CLOSED, "evidence": " "}], True),
            "an extra field": ([{**self.CLOSED, "severity": "info"}], True),
            "an item the brief never carried": ([self.CLOSED], False),
        }
        for label, (accounting, carried) in cases.items():
            with self.subTest(label):
                with self.assertRaises(coordinator.CoordinatorError) as refused:
                    reports._validate_review(
                        self._review(accounting), self._brief(carried=carried)
                    )
                self.assertIn("carried_items", refused.exception.message)

    def test_only_a_closed_item_closes_the_gap(self) -> None:
        for accounting, gap in (
            (None, ["coordinator-finding-1"]),
            ([{**self.CLOSED, "status": "unverified"}], ["coordinator-finding-1"]),
            ([{**self.CLOSED, "status": "open"}], ["coordinator-finding-1"]),
            ([self.CLOSED], []),
        ):
            with self.subTest(accounting=accounting):
                report = {"role": "code-review", "review": self._review(accounting)}
                self.assertEqual(carried_items.carried_gap(report, self._brief()), gap)

    def test_an_open_carried_item_is_structured_developer_evidence(self) -> None:
        for status, expected in (
            ("open", ("code", "developer-retry")),
            ("unverified", ("transport", "same-candidate-rerun")),
        ):
            with self.subTest(status=status):
                report = {
                    "outcome": "blocked",
                    "checks_run": [],
                    "review": self._review([{**self.CLOSED, "status": status}]),
                }
                routing = decisions._retry_routing(
                    "code-review",
                    report,
                    dispatch_candidate=self.CANDIDATE,
                    current_candidate=self.CANDIDATE,
                    explicit_category="transport",
                )
                self.assertEqual(
                    (routing["reason_category"], routing["route"]), expected
                )


class CarriedItemsBriefRolesTests(unittest.TestCase):
    """Which work briefs may carry each kind of carried item (issues #499, #501), no ledger."""

    ITEM: JsonObject = {
        "item_id": "item-1",
        "source": {},
        "summary": "s",
        "files": [],
        "expected_evidence": "e",
    }

    def test_a_brief_carries_each_kind_only_on_the_roles_it_reaches(self) -> None:
        for role, purpose, kind, valid in (
            ("architect", "work", "incomplete-item", True),
            ("developer", "work", "incomplete-item", True),
            ("verification", "work", "incomplete-item", True),
            ("code-review", "work", "incomplete-item", True),
            ("qa", "work", "incomplete-item", True),
            ("developer", "work", "coordinator-finding", True),
            ("code-review", "work", "review-finding", True),
            ("qa", "work", "coordinator-finding", False),
            ("architect", "work", "review-finding", False),
            ("developer", "publish", "incomplete-item", False),
        ):
            dispatch = {
                "role": role,
                "purpose": purpose,
                "carried_items": {kind: [dict(self.ITEM)]},
            }
            with self.subTest(role=role, purpose=purpose, kind=kind):
                if valid:
                    history._validate_carried_section(dispatch)
                    continue
                with self.assertRaises(coordinator.CoordinatorError):
                    history._validate_carried_section(dispatch)


class DeltaReviewHelperTests(unittest.TestCase):
    """The delta-review helpers after a fix-forward (issue #625), with no ledger."""

    A, B, C = "a" * 40, "b" * 40, "c" * 40

    def test_a_rebased_copy_resolves_through_the_chain_and_a_cycle_ends_the_walk(
        self,
    ) -> None:
        a, b, c = self.A, self.B, self.C
        chain = {c: (b, True), b: (a, True)}
        self.assertEqual(delta_review._reviewed_origin(c, chain, {a}), a)
        self.assertIsNone(delta_review._reviewed_origin(c, {c: (b, False)}, {b}))
        self.assertIsNone(delta_review._reviewed_origin(c, chain, {b}))
        for name, pairs in {
            "own original": {a: (a, True)},
            "two-step cycle": {a: (b, True), b: (a, True)},
        }.items():
            with self.subTest(name):
                self.assertIsNone(delta_review._reviewed_origin(a, pairs, {a, b}))

    @staticmethod
    def _item(item_id: str, **source: object) -> JsonObject:
        return {
            "item_id": item_id,
            "source": {"kind": item_id.rsplit("-", 1)[0], **source},
            "summary": f"{item_id} summary",
            "files": [],
            "expected_evidence": f"{item_id} evidence",
        }

    def test_a_delta_brief_adds_the_review_findings_and_developer_items_once(
        self,
    ) -> None:
        """A delta-review carries, on top of its own section, the developer-retry's review
        findings and its incomplete items for the developer, each once; an incomplete item for
        another role and a coordinator finding are not added, and a full or ordinary brief keeps
        its own section."""
        coordinator_finding = self._item("coordinator-finding-1")
        finding = self._item("review-finding-1")
        developer_item = self._item("incomplete-item-1", target_role="developer")
        own = {
            "coordinator-finding": [coordinator_finding],
            "review-finding": [finding],
        }
        retried = {
            "coordinator-finding": [
                coordinator_finding,
                self._item("coordinator-finding-2"),
            ],
            "review-finding": [finding, self._item("review-finding-2")],
            "incomplete-item": [
                developer_item,
                self._item("incomplete-item-2", target_role="qa"),
            ],
        }
        delta = {"mode": "delta", "developer_dispatch_id": "dispatch-retry"}
        with mock.patch.object(
            delta_review, "_load_dispatch", return_value={"carried_items": retried}
        ) as load:
            merged = delta_review.with_closure_items(own, Path("root"), delta)
            for scope in (None, {**delta, "mode": "full"}):
                with self.subTest(scope=scope):
                    self.assertEqual(
                        delta_review.with_closure_items(own, Path("root"), scope), own
                    )

        load.assert_called_once_with(Path("root"), "dispatch-retry")
        self.assertEqual(
            merged,
            {
                "coordinator-finding": [coordinator_finding],
                "review-finding": [finding, retried["review-finding"][1]],
                "incomplete-item": [developer_item],
            },
        )

    def _accounted(self, *statuses: tuple[str, str]) -> JsonObject:
        """A code-review report whose review accounts for each ``(item_id, status)`` pair and
        found one Standards warning of its own."""
        axis = {"severity": "none", "findings": [], "risks": "none", "blockers": "none"}
        warning = {"severity": "warning", "summary": "new", "evidence": "x.py:1"}
        return {
            "role": "code-review",
            "review": {
                "standards": {**axis, "severity": "warning", "findings": [warning]},
                "spec": dict(axis),
                "carried_items": [
                    {"item_id": item_id, "status": status, "evidence": "x.py:2"}
                    for item_id, status in statuses
                ],
            },
        }

    def test_a_retried_delta_review_hands_on_the_developer_items_it_did_not_close(
        self,
    ) -> None:
        """A retried delta-review hands the next developer-retry the review findings and the
        developer's incomplete items its brief carried and its review did not mark closed, each
        once next to the batch's open developer items; its own findings are numbered after the
        highest carried review finding. An incomplete item for another role stays behind."""
        open_item = self._item("incomplete-item-1", target_role="developer")
        closed_item = self._item("incomplete-item-2", target_role="developer")
        other_role = self._item("incomplete-item-3", target_role="qa")
        batch_item = self._item("incomplete-item-4", target_role="developer")
        findings = [self._item("review-finding-1"), self._item("review-finding-3")]
        dispatch = {
            "carried_items": {
                "review-finding": findings,
                "incomplete-item": [open_item, closed_item, other_role],
            }
        }
        report = self._accounted(
            ("review-finding-1", "closed"),
            ("review-finding-3", "unverified"),
            ("incomplete-item-1", "open"),
            ("incomplete-item-2", "closed"),
        )
        entry = {
            "role": "code-review",
            "dispatch_id": "dispatch-review",
            "report_sha256": "f" * 64,
        }

        self.assertEqual(
            carried_items._review_handoff(dispatch, report),
            ([findings[1]], [open_item], 3),
        )
        self.assertEqual(carried_items._review_handoff({}, report), ([], [], 0))
        with (
            mock.patch.object(
                carried_items, "open_coordinator_findings", return_value=[]
            ),
            mock.patch.object(
                carried_items,
                "open_incomplete_items",
                return_value=[batch_item, open_item],
            ),
            mock.patch.object(carried_items, "_pending_report", return_value=report),
            mock.patch.object(carried_items, "_load_dispatch", return_value=dispatch),
        ):
            section = carried_items.retry_section(Path("root"), {}, entry)

        self.assertEqual(
            [
                (item["item_id"], item["source"]["dispatch_id"])
                for item in section["review-finding"][1:]
            ],
            [("review-finding-4", "dispatch-review")],
        )
        self.assertEqual(
            (section["review-finding"][0], section["incomplete-item"]),
            (findings[1], [batch_item, open_item]),
        )


class CoordinatorGuardHelperTests(unittest.TestCase):
    """Direct-call pins for the config/brief guards whose parameters accept arbitrary JSON."""

    def test_reject_sensitive_accepts_plain_nested_json(self) -> None:
        config._reject_sensitive({"a": [{"b": 1}, "text", None]}, "config")

    def test_reject_sensitive_rejects_a_non_string_key(self) -> None:
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            config._reject_sensitive({1: "x"}, "config")

        self.assertEqual(caught.exception.message, "config contains a non-string key")
        self.assertEqual(caught.exception.remedy, "use only string keys in config")

    def test_reject_sensitive_rejects_a_secret_shaped_key(self) -> None:
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            config._reject_sensitive({"api_key": "x"}, "config")

        self.assertEqual(
            caught.exception.message, "config contains secret-shaped field 'api_key'"
        )
        self.assertIn(
            "remove the secret-shaped field 'api_key' from config",
            caught.exception.remedy,
        )

    def test_reject_sensitive_descends_into_lists_with_an_indexed_location(
        self,
    ) -> None:
        with self.assertRaises(coordinator.CoordinatorError) as caught:
            config._reject_sensitive(
                {"items": [{"ok": 1}, {"password": "x"}]}, "config"
            )

        self.assertEqual(
            caught.exception.message,
            "config.items[1] contains secret-shaped field 'password'",
        )

    def test_reject_non_english_accepts_english_scalars_and_sequences(self) -> None:
        workspace._reject_non_english("plain text", "field")
        workspace._reject_non_english(["plain", "text"], "field")
        workspace._reject_non_english(("plain",), "field")
        workspace._reject_non_english(7, "field")

    def test_reject_non_english_rejects_cyrillic_in_a_string_or_a_sequence(
        self,
    ) -> None:
        for value in ("привет", ["ok", "привет"], ("привет",)):
            with self.subTest(value=value):
                with self.assertRaises(coordinator.CoordinatorError) as caught:
                    workspace._reject_non_english(value, "purpose")

                self.assertIn(
                    "purpose is handed to a role as agent-to-agent protocol text",
                    caught.exception.message,
                )
                self.assertEqual(
                    caught.exception.remedy,
                    "rewrite purpose in English, keeping commands, paths, IDs and quoted evidence verbatim",
                )


class LedgerLockReleaseTests(unittest.TestCase):
    """``ledger release-lock`` against the real ledger lock (#498): the lock records its owner,
    and a stuck lock is released only after that owner has been checked."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        self.lock_dir = self.state_dir / ".coordinator.lock"
        self.ledger = LifecycleLedger(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _release(self) -> JsonObject:
        return coordinator.release_ledger_lock(
            _ns(repo=str(self.tmp), state_dir=str(self.state_dir))
        )

    def _lock_files(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.lock_dir.iterdir()}

    def _age(self, path: Path, seconds: float) -> None:
        """Set ``path``'s mtime ``seconds`` in the past."""
        then = datetime.now(UTC).timestamp() - seconds
        os.utime(path, (then, then))

    def _assert_remedy_names_remaining_time(
        self, refused: coordinator.CoordinatorError
    ) -> None:
        held = re.search(r"for (\d+) seconds", refused.message)
        remaining = re.search(r" in (\d+) seconds", refused.remedy)
        assert held is not None and remaining is not None
        self.assertEqual(
            int(remaining[1]), constants.LEDGER_LOCK_STALE_SECONDS - int(held[1])
        )

    def test_a_lock_whose_owner_is_alive_is_never_released(self) -> None:
        with self.ledger.lock():
            before = self._lock_files()

            with self.assertRaises(coordinator.CoordinatorError) as refused:
                self._release()

            self.assertIn("owner-alive", refused.exception.message)
            self.assertIn(str(os.getpid()), refused.exception.message)
            self.assertIn("never released", refused.exception.remedy)
            self.assertEqual(self._lock_files(), before)

    def test_a_lock_left_by_a_dead_owner_is_released(self) -> None:
        holder = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, sys\n"
                "from pathlib import Path\n"
                "from harness.orchestration.ledger.lifecycle import LifecycleLedger\n"
                "held = LifecycleLedger(Path(sys.argv[1])).lock()\n"
                "held.__enter__()\n"
                "print(os.getpid(), flush=True)\n"
                "os._exit(0)\n",
                str(self.state_dir),
            ],
            cwd=ORCHESTRATION_ROOT.parents[1],
            capture_output=True,
            text=True,
            check=True,
        )
        with self.assertRaises(ledger_ops.LedgerBusyError):
            with ledger_ops._ledger_lock(self.ledger):
                pass

        released = self._release()

        self.assertTrue(released["released"])
        self.assertEqual(released["reason"], "owner-dead")
        lock = cast(JsonObject, released["lock"])
        self.assertEqual(lock["owner"]["pid"], int(holder.stdout))
        self.assertFalse(self.lock_dir.exists())
        with self.ledger.lock():
            pass

    def test_a_lock_without_an_owner_record_is_released_only_once_stale(
        self,
    ) -> None:
        self.state_dir.mkdir()
        self.lock_dir.mkdir()  # the shape an older runtime's lock has: no owner record

        with self.assertRaises(coordinator.CoordinatorError) as refused:
            self._release()

        self.assertIn("owner-unknown-recent", refused.exception.message)
        self._assert_remedy_names_remaining_time(refused.exception)
        self.assertTrue(self.lock_dir.is_dir())
        self._age(self.lock_dir, constants.LEDGER_LOCK_STALE_SECONDS + 60)

        released = self._release()

        self.assertEqual(released["reason"], "owner-unknown-stale")
        lock = cast(JsonObject, released["lock"])
        self.assertIsNone(lock["owner"])
        self.assertEqual(lock["owner_record"], "absent")
        self.assertFalse(self.lock_dir.exists())
        with self.ledger.lock():
            pass

    def test_two_releases_never_overlap(self) -> None:
        """A second release-lock that starts while one is releasing is refused before it reads the
        lock (#498), so it can never remove a lock that was taken again in the meantime."""
        self.state_dir.mkdir()
        self.lock_dir.mkdir()
        self._age(self.lock_dir, constants.LEDGER_LOCK_STALE_SECONDS + 60)

        with self.ledger._release_guard():
            with self.assertRaises(coordinator.CoordinatorError) as refused:
                self._release()

            self.assertIn("another ledger release-lock", refused.exception.message)
            self.assertIn("ledger release-lock", refused.exception.remedy)
            self.assertNotIn("remove", refused.exception.remedy.lower())
            self.assertTrue(self.lock_dir.is_dir())

        self.assertEqual(self._release()["reason"], "owner-unknown-stale")
        self.assertFalse(self.lock_dir.exists())

    def test_a_lock_whose_owner_record_cannot_be_read_is_released_only_once_stale(
        self,
    ) -> None:
        """An owner that died between creating its record and writing it (#525) leaves an empty
        or unreadable record; its age is the record's own mtime, not the lock directory's."""
        for content in (b"", b'{"pid": 4'):
            with self.subTest(content=content):
                self.lock_dir.mkdir(parents=True)
                owner = self.lock_dir / "owner.json"
                owner.write_bytes(content)
                self._age(self.lock_dir, constants.LEDGER_LOCK_STALE_SECONDS + 60)
                self._age(owner, constants.LEDGER_LOCK_STALE_SECONDS - 60)
                before = self._lock_files()

                with self.assertRaises(coordinator.CoordinatorError) as refused:
                    self._release()

                self.assertIn("owner-unknown-recent", refused.exception.message)
                self._assert_remedy_names_remaining_time(refused.exception)
                self.assertNotIn("remove", refused.exception.remedy.lower())
                self.assertEqual(self._lock_files(), before)
                self._age(owner, constants.LEDGER_LOCK_STALE_SECONDS + 60)

                released = self._release()

                self.assertEqual(released["reason"], "owner-unknown-stale")
                lock = cast(JsonObject, released["lock"])
                self.assertIsNone(lock["owner"])
                self.assertEqual(lock["owner_record"], "unreadable")
                self.assertFalse(self.lock_dir.exists())
                with self.ledger.lock():
                    pass

    def test_no_lock_means_nothing_to_release(self) -> None:
        self.assertEqual(self._release(), {"released": False, "lock": None})

    def test_the_verdict_never_releases_a_live_or_foreign_owner(self) -> None:
        owner = {
            "pid": 4242,
            "host": "here",
            "acquired_at": "2026-01-01T00:00:00+00:00",
        }
        ancient = 10 * constants.LEDGER_LOCK_STALE_SECONDS
        cases = (
            ({"owner": owner, "held_seconds": ancient}, True, (False, "owner-alive")),
            ({"owner": owner, "held_seconds": 0}, False, (True, "owner-dead")),
            (
                {"owner": {**owner, "host": "elsewhere"}, "held_seconds": ancient},
                False,
                (False, "owner-on-another-host"),
            ),
            (
                {"owner": None, "held_seconds": 5},
                False,
                (False, "owner-unknown-recent"),
            ),
            (
                {"owner": {"pid": "x"}, "held_seconds": ancient},
                True,
                (True, "owner-unknown-stale"),
            ),
        )
        for state, alive, expected in cases:

            def probe(pid: int, alive: bool = alive) -> bool:
                return alive

            with self.subTest(state=state, alive=alive):
                self.assertEqual(
                    ledger_admin._release_verdict(
                        state,
                        host="here",
                        pid_active=probe,
                        stale_after=constants.LEDGER_LOCK_STALE_SECONDS,
                    ),
                    expected,
                )

    def test_a_busy_ledger_names_release_lock_instead_of_manual_removal(self) -> None:
        with self.ledger.lock():
            with self.assertRaises(ledger_ops.LedgerBusyError) as busy:
                coordinator.ledger_status(
                    _ns(repo=str(self.tmp), state_dir=str(self.state_dir))
                )

        self.assertIsInstance(busy.exception, coordinator.CoordinatorError)
        self.assertEqual(
            busy.exception.message, "ledger is locked by another operation"
        )
        self.assertIn("ledger release-lock", busy.exception.remedy)
        self.assertNotIn("remove", busy.exception.remedy.lower())
        self.assertNotIn(".coordinator.lock", busy.exception.remedy)

    def test_the_cli_wires_release_lock(self) -> None:
        args = coordinator.parser().parse_args(["ledger", "release-lock"])

        self.assertIs(args.handler, coordinator.release_ledger_lock)


class CoordinatorCliParserTests(unittest.TestCase):
    """coordinator_cli.py has no test coverage of its own (issue #219): a working ArgumentParser
    that resolves real subcommands to the right handler, seeded here before narrowing
    build_parser's ``handlers``/``defaults`` parameters off ``Any``.  ``handlers`` is the
    coordinator facade; ``defaults`` is ``core.constants``, the fixed vocabulary the CLI offers
    as choices."""

    def test_build_parser_resolves_dispatch_status_to_its_handler(self) -> None:
        parser = coordinator_cli.build_parser(coordinator, constants)
        self.assertIsInstance(parser, argparse.ArgumentParser)

        args = parser.parse_args(["dispatch", "status"])

        self.assertIs(args.handler, coordinator.dispatch_status)

    def test_batch_decide_accepts_a_commit_plan_file(self) -> None:
        decide = [
            "batch",
            "decide",
            "--batch",
            "batch-1",
            "--decision",
            "accept",
            "--approved-by",
            "Malove",
            "--approved-at",
            "2026-09-17T00:00:00+00:00",
        ]
        parse = coordinator.parser().parse_args

        pinned = parse([*decide, "--commit-plan-file", "plan.json"])

        self.assertIs(pinned.handler, coordinator.decide_batch)
        self.assertEqual(pinned.commit_plan_file, "plan.json")
        self.assertIsNone(parse(decide).commit_plan_file)

    def test_batch_create_accepts_supersedes_with_its_approval(self) -> None:
        create = [
            "batch",
            "create",
            "--ticket",
            "#506",
            "--branch",
            "feature/issue-506-x",
            "--worktree",
            "wt",
            "--definition-of-done",
            "resume",
            "--prohibited-change",
            "secrets",
        ]
        parse = coordinator.parser().parse_args

        superseding = parse(
            [
                *create,
                "--supersedes",
                "batch-1",
                "--approved-by",
                "Malove",
                "--approved-at",
                "2026-09-17T00:00:00+00:00",
            ]
        )
        ordinary = parse(create)

        self.assertIs(superseding.handler, coordinator.create_batch)
        self.assertEqual(
            (
                superseding.supersedes,
                superseding.approved_by,
                superseding.approved_at,
            ),
            ("batch-1", "Malove", "2026-09-17T00:00:00+00:00"),
        )
        self.assertEqual(
            (ordinary.supersedes, ordinary.approved_by, ordinary.approved_at),
            (None, None, None),
        )

    def test_batch_decide_accepts_a_findings_file(self) -> None:
        decide = [
            "batch",
            "decide",
            "--batch",
            "batch-1",
            "--decision",
            "accept",
            "--approved-by",
            "Malove",
            "--approved-at",
            "2026-09-17T00:00:00+00:00",
        ]
        parse = coordinator.parser().parse_args

        carried = parse([*decide, "--findings-file", "findings.json"])

        self.assertIs(carried.handler, coordinator.decide_batch)
        self.assertEqual(carried.findings_file, "findings.json")
        self.assertIsNone(parse(decide).findings_file)

    def test_batch_decide_accepts_carry_incomplete(self) -> None:
        decide = [
            "batch",
            "decide",
            "--batch",
            "batch-1",
            "--decision",
            "accept",
            "--approved-by",
            "Malove",
            "--approved-at",
            "2026-09-17T00:00:00+00:00",
        ]
        parse = coordinator.parser().parse_args

        carried = parse([*decide, "--carry-incomplete"])

        self.assertIs(carried.handler, coordinator.decide_batch)
        self.assertTrue(carried.carry_incomplete)
        self.assertFalse(parse(decide).carry_incomplete)

    def test_batch_decide_and_decision_packet_accept_narrowed(self) -> None:
        decide = [
            "batch",
            "decide",
            "--batch",
            "batch-1",
            "--decision",
            "retry",
            "--approved-by",
            "Malove",
            "--approved-at",
            "2026-09-17T00:00:00+00:00",
        ]
        packet = ["batch", "decision-packet", "--batch", "batch-1"]
        parse = coordinator.parser().parse_args

        narrowed = parse([*decide, "--narrowed"])
        previewed = parse([*packet, "--narrowed"])

        self.assertIs(narrowed.handler, coordinator.decide_batch)
        self.assertIs(previewed.handler, coordinator.decision_packet)
        self.assertTrue(narrowed.narrowed and previewed.narrowed)
        self.assertFalse(parse(decide).narrowed or parse(packet).narrowed)

    def test_batch_decision_packet_previews_a_findings_file(self) -> None:
        parse = coordinator.parser().parse_args
        common = ["batch", "decision-packet", "--batch", "batch-1"]

        previewed = parse([*common, "--findings-file", "findings.json"])

        self.assertIs(previewed.handler, coordinator.decision_packet)
        self.assertEqual(previewed.findings_file, "findings.json")
        self.assertIsNone(parse(common).findings_file)

    def test_batch_carry_over_requires_a_batch_and_a_findings_file(self) -> None:
        parse = coordinator.parser().parse_args

        carried = parse(
            ["batch", "carry-over", "--batch", "batch-1", "--findings-file", "f.json"]
        )

        self.assertIs(carried.handler, coordinator.carry_over_findings)
        self.assertEqual((carried.batch, carried.findings_file), ("batch-1", "f.json"))
        for missing in (
            ["batch", "carry-over", "--batch", "batch-1"],
            ["batch", "carry-over", "--findings-file", "f.json"],
        ):
            with self.subTest(missing=missing):
                with (
                    self.assertRaises(SystemExit),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    parse(missing)

    def test_coordinator_parser_wires_the_same_handler(self) -> None:
        args = coordinator.parser().parse_args(["dispatch", "status"])

        self.assertIs(args.handler, coordinator.dispatch_status)

    def test_no_memory_is_available_for_propose_create_and_register(self) -> None:
        parse = coordinator.parser().parse_args
        for command in ("propose", "create"):
            args = parse(
                [
                    "dispatch",
                    command,
                    "--batch",
                    "batch-1",
                    "--role",
                    "architect",
                    "--no-memory",
                ]
            )
            self.assertTrue(args.no_memory)
        args = parse(
            [
                "context-package",
                "register",
                "--batch",
                "batch-1",
                "--candidate-commit",
                "a" * 40,
                "--no-memory",
            ]
        )
        self.assertTrue(args.no_memory)

    def test_dispatch_preflight_exposes_purpose_like_other_dispatch_commands(
        self,
    ) -> None:
        parse = coordinator.parser().parse_args
        default = parse(
            ["dispatch", "preflight", "--batch", "batch-1", "--role", "architect"]
        )
        self.assertIs(default.handler, coordinator.preflight_dispatch)
        self.assertEqual(default.purpose, "work")
        publish = parse(
            [
                "dispatch",
                "preflight",
                "--batch",
                "batch-1",
                "--role",
                "developer",
                "--purpose",
                "publish",
            ]
        )
        self.assertEqual(publish.purpose, "publish")

    def test_operational_guard_commands_resolve_to_their_handlers(self) -> None:
        parse = coordinator.parser().parse_args
        approval = [
            "--approved-by",
            "Malove",
            "--approved-at",
            "2026-09-17T00:01:00+00:00",
        ]

        propose = parse(
            [
                "dispatch",
                "propose",
                "--batch",
                "batch-1",
                "--role",
                "code-review",
                "--candidate-commit",
                "abc1234",
            ]
        )
        create = parse(
            [
                "dispatch",
                "create",
                "--batch",
                "batch-1",
                "--transition-digest",
                "f" * 64,
                *approval,
            ]
        )
        pressure = parse(
            [
                "dispatch",
                "context-pressure",
                "--dispatch",
                "dispatch-1",
                "--observed-tokens",
                "5",
                "--source",
                "provider-usage",
            ]
        )

        self.assertIs(propose.handler, coordinator.create_dispatch)
        self.assertTrue(propose.propose)
        self.assertFalse(create.propose)
        self.assertEqual(create.transition_digest, "f" * 64)
        self.assertIs(pressure.handler, coordinator.record_context_pressure)
        self.assertIs(
            parse(["batch", "attention", "check", "--batch", "batch-1"]).handler,
            coordinator.attention_check,
        )
        self.assertIs(
            parse(
                [
                    "batch",
                    "attention",
                    "resolve",
                    "--batch",
                    "batch-1",
                    "--note",
                    "reviewed",
                    *approval,
                ]
            ).handler,
            coordinator.attention_resolve,
        )
        with self.assertRaises(
            SystemExit
        ):  # a role's own claim is never a telemetry source
            parse(
                [
                    "dispatch",
                    "context-pressure",
                    "--dispatch",
                    "dispatch-1",
                    "--observed-tokens",
                    "5",
                    "--source",
                    "self-report",
                ]
            )
        self.assertIn(
            "context-pressure",
            parse(
                [
                    "batch",
                    "decide",
                    "--batch",
                    "b",
                    "--decision",
                    "retry",
                    "--reason-category",
                    "context-pressure",
                    *approval,
                ]
            ).reason_category,
        )

    def test_batch_not_required_resolves_to_its_handler(self) -> None:
        args = coordinator.parser().parse_args(
            [
                "batch",
                "not-required",
                "--batch",
                "batch-123",
                "--approved-by",
                "Malove",
                "--approved-at",
                "2026-09-17T00:01:00+00:00",
                "--reason",
                "already satisfied",
            ]
        )

        self.assertIs(args.handler, coordinator.mark_batch_not_required)

    def test_batch_decide_accepts_abandon_with_a_reason_and_a_reason_category(
        self,
    ) -> None:
        common = [
            "batch",
            "decide",
            "--batch",
            "batch-123",
            "--approved-by",
            "Malove",
            "--approved-at",
            "2026-09-17T00:01:00+00:00",
        ]

        abandon = coordinator.parser().parse_args(
            [*common, "--decision", "abandon", "--reason", "superseded"]
        )
        retry = coordinator.parser().parse_args(
            [*common, "--decision", "retry", "--reason-category", "transport"]
        )
        default = coordinator.parser().parse_args([*common, "--decision", "retry"])

        self.assertEqual((abandon.decision, abandon.reason), ("abandon", "superseded"))
        self.assertEqual(retry.reason_category, "transport")
        self.assertIsNone(default.reason_category)
        self.assertIsNone(default.retry_role)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            coordinator.parser().parse_args(
                [*common, "--decision", "retry", "--reason-category", "flaky-network"]
            )

    def test_batch_decision_packet_takes_the_retry_flags_of_batch_decide(
        self,
    ) -> None:
        common = ["batch", "decision-packet", "--batch", "batch-123"]

        flagged = coordinator.parser().parse_args(
            [*common, "--reason-category", "transport", "--retry-role", "developer"]
        )
        default = coordinator.parser().parse_args(common)

        self.assertEqual(
            (flagged.reason_category, flagged.retry_role), ("transport", "developer")
        )
        self.assertEqual((default.reason_category, default.retry_role), (None, None))
        self.assertIs(default.handler, coordinator.decision_packet)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            coordinator.parser().parse_args([*common, "--retry-role", "qa"])


class PinnedRuntimeSnapshotTests(unittest.TestCase):
    """A batch finishes on the runtime it was planned under after the checkout's runtime is
    reinstalled for another task (#369), driven through the installed coordinator CLI."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        (self.repo / ".git" / "info" / "exclude").write_text(
            ".harness/\n", encoding="utf-8"
        )
        installed = self.repo / ".harness"
        ignore = shutil.ignore_patterns("state", "__pycache__", "*.pyc")
        for item in ORCHESTRATION_ROOT.parent.iterdir():
            if item.is_file() and item.suffix == ".py":
                shutil.copy2(item, installed / item.name)
            elif item.is_dir() and (item / "__init__.py").is_file():
                shutil.copytree(
                    item, installed / item.name, ignore=ignore, dirs_exist_ok=True
                )
        # A short state root keeps ledger temp files under Windows MAX_PATH in deep test roots.
        self.state_dir = self.tmp / "s"
        self.runtimes = self.state_dir / workspace.RUNTIMES_DIR

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _coordinator(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(self.repo / ".harness" / "orchestration" / "coordinator.py"),
                "--repo",
                str(self.repo),
                "--state-dir",
                str(self.state_dir),
                *arguments,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def _ok(self, *arguments: str) -> JsonObject:
        result = self._coordinator(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return cast(JsonObject, json.loads(result.stdout))

    def _create_batch(self, slug: str) -> str:
        branch = f"feature/issue-369-{slug}"
        worktree = self.tmp / slug
        # One unfinished batch per ticket: every batch of a test plans its own ticket.
        self._planned = getattr(self, "_planned", 0) + 1
        _git(self.repo, "worktree", "add", "-b", branch, str(worktree), "master")
        batch = self._ok(
            "batch",
            "create",
            "--ticket",
            f"#369{self._planned}",
            "--branch",
            branch,
            "--worktree",
            str(worktree),
            "--allowed-path",
            "**",
            "--integration-ref",
            "master",
            "--definition-of-done",
            "do the thing",
            "--prohibited-change",
            "secrets",
            "--expected-file",
            "services/x.py",
            "--expected-service",
            "core",
            "--expected-changed-lines",
            "10",
        )
        return cast(str, batch["batch_id"])

    def _approve(self, batch_id: str) -> subprocess.CompletedProcess[str]:
        return self._coordinator(
            "batch",
            "approve",
            "--batch",
            batch_id,
            "--approved-by",
            "Malove",
            "--approved-at",
            datetime.now(UTC).isoformat(),
        )

    def _reinstall_runtime(self) -> None:
        """Stand in for 'harness update' from another branch: the installed approve differs."""
        installed = self.repo / ".harness" / "orchestration" / "workflow" / "batch.py"
        with installed.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n\ndef approve_batch(args: argparse.Namespace) -> JsonObject:\n"
                "    return {'executed_by': 'reinstalled runtime'}\n"
            )

    def test_batch_finishes_on_its_pinned_runtime_after_a_reinstall(self) -> None:
        planned_before = self._create_batch("before")
        self._reinstall_runtime()

        approved = self._approve(planned_before)
        self.assertEqual(approved.returncode, 0, approved.stderr or approved.stdout)
        self.assertEqual(json.loads(approved.stdout)["batch_id"], planned_before)

        planned_after = self._create_batch("after")
        self.assertEqual(
            json.loads(self._approve(planned_after).stdout),
            {"executed_by": "reinstalled runtime"},
        )
        self.assertEqual(len(list(self.runtimes.iterdir())), 2)

    def _architect_dispatch(self, batch_id: str) -> JsonObject:
        shape = (
            "--batch",
            batch_id,
            "--role",
            "architect",
            "--runtime",
            "claude",
            "--model",
            "sonnet",
            "--effort",
            "high",
        )
        proposal = self._ok("dispatch", "propose", *shape)
        return self._ok(
            "dispatch",
            "create",
            *shape,
            "--transition-digest",
            cast(str, proposal["transition_digest"]),
            "--approved-by",
            "Malove",
            "--approved-at",
            datetime.now(UTC).isoformat(),
        )

    def test_runtime_hash_check_passes_inside_the_pinned_snapshot(self) -> None:
        batch_id = self._create_batch("dispatch")
        self.assertEqual(self._approve(batch_id).returncode, 0)
        dispatch = self._architect_dispatch(batch_id)
        self._reinstall_runtime()

        sent = self._coordinator(
            "dispatch", "send", "--dispatch", cast(str, dispatch["dispatch_id"])
        )

        self.assertEqual(sent.returncode, 0, sent.stderr or sent.stdout)

    def test_report_submitted_by_file_runs_on_the_pinned_snapshot(self) -> None:
        """report submit names its dispatch only inside --file (#377)."""
        batch_id = self._create_batch("report")
        self.assertEqual(self._approve(batch_id).returncode, 0)
        brief = cast(JsonObject, self._architect_dispatch(batch_id)["brief"])
        dispatch_id = cast(str, brief["dispatch_id"])
        self._reinstall_runtime()
        self._ok("dispatch", "send", "--dispatch", dispatch_id)
        self._ok(
            "dispatch", "self-report", "--dispatch", dispatch_id, "--model", "sonnet"
        )
        report = workspace._prepare_agent_inbox(self.repo) / f"{dispatch_id}.json"
        report.write_text(
            json.dumps(
                {
                    "dispatch_id": dispatch_id,
                    "ticket": brief["ticket"],
                    "role": "architect",
                    "outcome": "completed",
                    "output": "architecture decision recorded",
                    "commit_sha": "not applicable — read-only role",
                    "changed_files": [],
                    "checks_run": [],
                    "risks": "none",
                    "blockers": "none",
                    "next_coordinator_action": "dispatch developer",
                    "report_language": "ru",
                }
            ),
            encoding="utf-8",
        )

        submitted = self._ok("report", "submit", "--file", str(report))

        self.assertEqual(submitted["state"], "reported")

    def test_a_tampered_snapshot_is_refused(self) -> None:
        batch_id = self._create_batch("tampered")
        self._reinstall_runtime()
        (snapshot,) = self.runtimes.iterdir()
        (snapshot / "harness" / "orchestration" / "playbook.md").write_text(
            "tampered\n", encoding="utf-8"
        )

        refused = self._approve(batch_id)

        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("no longer matches", refused.stdout + refused.stderr)

    def test_restore_runtime_recovers_a_batch_planned_without_a_snapshot(self) -> None:
        batch_id = self._create_batch("legacy")
        pinned = self.tmp / "pinned-harness"
        shutil.copytree(
            self.repo / ".harness",
            pinned,
            ignore=shutil.ignore_patterns("state", "__pycache__", "*.pyc"),
        )
        shutil.rmtree(self.runtimes)
        self._reinstall_runtime()

        mismatch = self._coordinator(
            "batch",
            "restore-runtime",
            "--batch",
            batch_id,
            "--from",
            str(self.repo / ".harness"),
        )
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn(
            "does not match the pinned hash", mismatch.stdout + mismatch.stderr
        )

        self._ok("batch", "restore-runtime", "--batch", batch_id, "--from", str(pinned))
        approved = self._approve(batch_id)
        self.assertEqual(approved.returncode, 0, approved.stderr or approved.stdout)
        self.assertEqual(json.loads(approved.stdout)["batch_id"], batch_id)


if __name__ == "__main__":
    unittest.main()
