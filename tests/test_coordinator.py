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
import subprocess
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

from harness.orchestration import (
    contract,
    coordinator,
    coordinator_cli,
    extensions,
    operational_guards,
    qa_lane,
)
from harness.orchestration.core import config, constants, git_utils, utils, workspace
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger import (
    BatchRecord,
    DispatchStatusRecord,
    LifecycleLedger,
    ledger_ops,
)
from harness.orchestration.workflow import approval, decisions, dispatch, reports

ORCHESTRATION_ROOT = Path(__file__).resolve().parents[1] / "harness" / "orchestration"


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
        self, ticket: str = "#195", branch: str = "feature/issue-195-thing"
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
            branch=branch,
            worktree=str(worktree_path),
            zone="repository",
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
            "checks_run": [{"command": "true", "result": "pass", "evidence": "n/a"}],
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

        coordinator._validate_report(report, dispatch, {"mode": "write", "name": "developer"})

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
                    "agent": "claude",
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
                "context_package_policy": {"min_starting_files": 11, "max_starting_files": 10}
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
        template = json.loads(
            (
                ORCHESTRATION_ROOT.parent / "project" / "orchestration.json.tmpl"
            ).read_text(encoding="utf-8")
        )
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
        self.assertEqual(config._execution_policy(template), template["execution_policy"])

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
        self.assertEqual(
            list(inspect.signature(coordinator._persist_report).parameters),
            ["ledger", "root", "batch", "dispatch", "report"],
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
            "zone": "repository",
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

    def test_create_batch_rejects_a_missing_ticket_or_zone(self) -> None:
        for overrides in ({"ticket": None}, {"zone": ""}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(coordinator.CoordinatorError) as caught:
                    coordinator.create_batch(self._create_batch_args(**overrides))
                self.assertEqual(
                    caught.exception.message,
                    "ticket and zone must be non-empty strings",
                )
                self.assertEqual(
                    caught.exception.remedy, "pass a non-empty --ticket and --zone"
                )

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
            "zone": "repository",
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

    def _create_batch(self) -> JsonObject:
        _git(
            self.repo,
            "worktree",
            "add",
            "-b",
            self.branch,
            str(self.worktree),
            "master",
        )
        batch = coordinator.create_batch(self._args(**self._batch_plan()))
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
            self._args(dispatch=dispatch_id, model="sonnet", worktree=None)
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
                note=None,
                **{**self._approval(), **extra},
            )
        )

    def _accepted_architect(self, batch_id: str) -> None:
        brief = self._dispatch(batch_id, "architect")["brief"]
        self._start(brief["dispatch_id"])
        self._submit(brief["dispatch_id"], self._base_report(brief, "architect"))
        self._decide(batch_id, "accept")

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
        self.assertEqual(stored["dispatches"][0]["decision"]["approved_by"], "policy:milestone")
        self.assertEqual(stored["next_action"], "developer")
        self.assertEqual(stored["dispatches"][-1]["role"], "developer")
        self.assertEqual(stored["dispatches"][-1]["state"], "approved")

    def test_milestone_developer_advances_to_qa_gate_and_qa_report_waits(self) -> None:
        self._patch_config(approval_policy="milestone")
        plan = self._batch_plan()
        plan["definition_of_done"] = ["add simple marker"]
        _git(self.repo, "worktree", "add", "-b", self.branch, str(self.worktree), "master")
        batch = coordinator.create_batch(self._args(**plan))
        coordinator.approve_batch(self._args(batch=batch["batch_id"], **self._approval()))
        self.batch_id = batch["batch_id"]
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(architect["dispatch_id"], self._base_report(architect, "architect"))
        developer_id = self._batch_record(batch["batch_id"])["dispatches"][-1]["dispatch_id"]
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
        with self.assertRaisesRegex(coordinator.CoordinatorError, "requires --approved-by"):
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
        self.assertNotIn("decision", self._batch_record(batch["batch_id"])["dispatches"][-1])

    def test_milestone_report_with_risk_trigger_waits_for_decision(self) -> None:
        self._patch_config(approval_policy="milestone")
        batch = self._create_batch()
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(architect["dispatch_id"], self._base_report(architect, "architect"))
        developer_id = self._batch_record(batch["batch_id"])["dispatches"][-1]["dispatch_id"]
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
        self._patch_config(approval_policy="low_risk", low_risk_zones=["repository"])
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
        self._patch_config(approval_policy="low_risk", low_risk_zones=["repository"])
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
        self._patch_config(approval_policy="low_risk", low_risk_zones=["repository"])
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
        self._patch_config(approval_policy="low_risk", low_risk_zones=["repository"])
        plan = self._batch_plan()
        plan["definition_of_done"] = ["add simple marker"]
        _git(self.repo, "worktree", "add", "-b", self.branch, str(self.worktree), "master")
        batch = coordinator.create_batch(self._args(**plan))
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )
        self.batch_id = batch["batch_id"]
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(architect["dispatch_id"], self._base_report(architect, "architect"))
        developer_id = self._batch_record(batch["batch_id"])["dispatches"][-1]["dispatch_id"]
        developer = coordinator._read_object(
            self._records() / "dispatches" / f"{developer_id}.json", "dispatch"
        )
        self._start(developer_id)
        candidate, changed = self._developer_commit("x")

        self._submit(developer_id, self._developer_report(developer, candidate, changed))

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

    def test_low_risk_review_with_findings_still_waits_for_decision(self) -> None:
        self._patch_config(approval_policy="low_risk", low_risk_zones=["repository"])
        batch = self._create_batch()
        architect = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(architect["dispatch_id"])
        self._submit(architect["dispatch_id"], self._base_report(architect, "architect"))
        developer_id = self._batch_record(batch["batch_id"])["dispatches"][-1]["dispatch_id"]
        developer = coordinator._read_object(
            self._records() / "dispatches" / f"{developer_id}.json", "dispatch"
        )
        self._start(developer_id)
        candidate, changed = self._developer_commit("x")
        self._submit(developer_id, self._developer_report(developer, candidate, changed))
        self.assertEqual(self._batch_record(batch["batch_id"])["next_action"], "code-review")
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
            item for item in stored["dispatches"] if item["dispatch_id"] == review["dispatch_id"]
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
    ) -> JsonObject:
        brief: JsonObject = self._dispatch(
            batch_id, "code-review", candidate=candidate
        )["brief"]
        self._start(brief["dispatch_id"], checkout=self.worktree)
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
                review={
                    "candidate_commit": candidate,
                    "scope": brief["review_scope"],
                    **self._axes(standards, spec),
                },
            ),
        )
        return brief

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
    ) -> JsonObject:
        routing = self._routing(batch)
        self.assertEqual(
            (
                routing["next_role"],
                routing["next_action"],
                routing["reason_category"],
                routing["candidate_commit"],
            ),
            (role, action, category, candidate),
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
        )
        self.assertNotIn("candidate_registrations", decided)

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

    def test_infrastructure_retry_still_enforces_the_base_commit_gate(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        self._infra_review(batch["batch_id"], candidate)
        self._decide(batch["batch_id"], "retry", reason_category="transport")
        (self.repo / "later.txt").write_text("integration moved\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "later")
        _git(self.repo, "push", "origin", "master")

        with self.assertRaises(coordinator.CoordinatorError) as caught:
            self._dispatch(batch["batch_id"], "code-review", candidate=candidate)

        self.assertIn("base", caught.exception.message.lower())

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
        )
        second = self._dispatch(batch["batch_id"], "qa", candidate=candidate)
        self.assertNotEqual(second["dispatch_id"], first["dispatch_id"])
        self.assertEqual(second["brief"]["candidate_commit"], candidate)

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
            spec=("blocker", [{"severity": "blocker", "summary": "wrong", "evidence": "x.py:1"}]),
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
                {"commit_sha": fixes[0], "plan_entry_id": retry["commit_plan"][0]["id"]},
                {"commit_sha": fixes[1], "plan_entry_id": retry["commit_plan"][1]["id"]},
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
        )
        self.assertEqual(routing["previous_role"], "developer")
        self.assertTrue(decided["retry_candidate_required"])

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
        self.assertEqual(set(transition), set(operational_guards.TRANSITION_FIELDS))
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

    def test_routing_table(self) -> None:
        finding = [{"severity": "warning", "summary": "s", "evidence": "e"}]
        infra, transport = "verification-infrastructure", "transport"
        review = self._report()
        no_review = self._report(standards=None)
        cases = [
            # stage, report, explicit category, candidate moved -> (reason category, next role, next action)
            (
                "code-review",
                review,
                infra,
                False,
                (infra, "code-review", "code-review"),
            ),
            (
                "code-review",
                review,
                transport,
                False,
                (transport, "code-review", "code-review"),
            ),
            (
                "code-review",
                review,
                None,
                False,
                ("unknown", "developer", "developer-retry"),
            ),
            (
                "code-review",
                review,
                "unknown",
                False,
                ("unknown", "developer", "developer-retry"),
            ),
            (
                "code-review",
                review,
                "code",
                False,
                ("code", "developer", "developer-retry"),
            ),
            (
                "code-review",
                review,
                "requirements",
                False,
                ("requirements", "developer", "developer-retry"),
            ),
            (
                "code-review",
                review,
                "candidate-change",
                False,
                ("candidate-change", "developer", "developer-retry"),
            ),
            (
                "code-review",
                self._report(standards=("warning", finding)),
                transport,
                False,
                ("code", "developer", "developer-retry"),
            ),
            (
                "code-review",
                self._report(spec=("blocker", finding)),
                transport,
                False,
                ("requirements", "developer", "developer-retry"),
            ),
            (
                "code-review",
                self._report(spec=("warning", [])),
                transport,
                False,
                ("requirements", "developer", "developer-retry"),
            ),
            (
                "code-review",
                self._report("completed"),
                transport,
                False,
                ("unknown", "developer", "developer-retry"),
            ),
            (
                "code-review",
                self._report("failed"),
                transport,
                False,
                ("unknown", "developer", "developer-retry"),
            ),
            (
                "code-review",
                review,
                infra,
                True,
                ("candidate-change", "developer", "developer-retry"),
            ),
            ("qa", no_review, infra, False, (infra, "qa", "qa")),
            ("qa", no_review, None, False, ("unknown", "developer", "developer-retry")),
            (
                "qa",
                self._report("failed", standards=None, failed_check=True),
                infra,
                False,
                ("code", "developer", "developer-retry"),
            ),
            ("publish", no_review, transport, False, (transport, "publish", "publish")),
            ("publish", no_review, infra, False, (infra, "publish", "publish")),
            (
                "publish",
                no_review,
                "candidate-change",
                False,
                ("candidate-change", "developer", "developer-retry"),
            ),
            (
                "publish",
                no_review,
                None,
                False,
                ("unknown", "developer", "developer-retry"),
            ),
            (
                "developer",
                no_review,
                transport,
                False,
                (transport, "developer", "developer-retry"),
            ),
            (
                "developer",
                self._report("completed", standards=None),
                transport,
                False,
                ("unknown", "developer", "developer-retry"),
            ),
            (
                "architect",
                no_review,
                transport,
                False,
                (transport, "architect", "architect"),
            ),
            (
                "architect",
                no_review,
                None,
                False,
                ("unknown", "architect", "architect"),
            ),
        ]
        for stage, report, category, moved, (reason, role, action) in cases:
            with self.subTest(
                stage=stage, category=category, moved=moved, outcome=report["outcome"]
            ):
                routing = self._route(stage, report, category, moved=moved)
                self.assertEqual(
                    (
                        routing["reason_category"],
                        routing["next_role"],
                        routing["next_action"],
                    ),
                    (reason, role, action),
                )
                self.assertEqual(routing["previous_role"], stage)
                self.assertTrue(routing["rationale"].strip())
                self.assertEqual(
                    routing["candidate_commit"], None if moved else self.CANDIDATE
                )

    def test_the_reason_categories_are_exactly_the_documented_seven(self) -> None:
        self.assertEqual(
            set(constants.RETRY_REASON_CATEGORIES),
            {
                "code",
                "requirements",
                "candidate-change",
                "verification-infrastructure",
                "transport",
                "context-pressure",
                "unknown",
            },
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

    def test_coordinator_parser_wires_the_same_handler(self) -> None:
        args = coordinator.parser().parse_args(["dispatch", "status"])

        self.assertIs(args.handler, coordinator.dispatch_status)

    def test_dispatch_preflight_exposes_purpose_like_other_dispatch_commands(self) -> None:
        parse = coordinator.parser().parse_args
        default = parse(["dispatch", "preflight", "--batch", "batch-1", "--role", "architect"])
        self.assertIs(default.handler, coordinator.preflight_dispatch)
        self.assertEqual(default.purpose, "work")
        publish = parse(
            ["dispatch", "preflight", "--batch", "batch-1", "--role", "developer", "--purpose", "publish"]
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


if __name__ == "__main__":
    unittest.main()
