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
import hashlib
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ORCHESTRATION_ROOT = Path(__file__).resolve().parents[1] / "harness" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))

import contract  # noqa: E402
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

    def _telemetry_payload(
        self, dispatch_id: str, *, max_context_tokens: int | None = None,
        recorded_at: str = "2026-09-18T00:00:00+00:00", session_kind: str = "worker",
    ) -> dict:
        return {
            "dispatch_id": dispatch_id, "session_kind": session_kind,
            "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "max_context_tokens": max_context_tokens, "tool_calls": 1, "tool_output_bytes": 10,
            "poll_turns": 1, "restart_reason": "none", "recorded_at": recorded_at,
        }

    def _record_telemetry(self, payload: dict) -> dict:
        path = self.tmp / f"telemetry-{uuid.uuid4()}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return coordinator.record_telemetry(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir), file=str(path),
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

    def test_adaptive_continuation_policy_resolves_context_warn_ratio(self) -> None:
        self.assertEqual(coordinator._adaptive_continuation_policy({})["context_warn_ratio"], 0.8)
        self.assertEqual(
            coordinator._adaptive_continuation_policy(
                {"adaptive_continuation_policy": {"context_warn_ratio": 0.5}}
            )["context_warn_ratio"],
            0.5,
        )
        self.assertEqual(
            coordinator._adaptive_continuation_policy(
                {"adaptive_continuation_policy": {"context_warn_ratio": 1}}
            )["context_warn_ratio"],
            1,
        )
        for invalid in (0, 1.5, "0.5", True, -0.1):
            resolved = coordinator._adaptive_continuation_policy(
                {"adaptive_continuation_policy": {"context_warn_ratio": invalid}}
            )
            self.assertEqual(resolved["context_warn_ratio"], 0.8, f"{invalid!r} should fall back to default")

    def test_record_telemetry_returns_context_advisory_levels(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]

        ok = self._record_telemetry(self._telemetry_payload(
            dispatch_id, max_context_tokens=50_000, recorded_at="2026-09-18T00:00:00+00:00",
        ))
        self.assertEqual(
            ok["context_advisory"], {"level": "ok", "limit": 150_000, "warn_at": 120_000, "observed": 50_000},
        )

        warn = self._record_telemetry(self._telemetry_payload(
            dispatch_id, max_context_tokens=130_000, recorded_at="2026-09-18T00:01:00+00:00",
        ))
        self.assertEqual(
            warn["context_advisory"], {"level": "warn", "limit": 150_000, "warn_at": 120_000, "observed": 130_000},
        )

        over = self._record_telemetry(self._telemetry_payload(
            dispatch_id, max_context_tokens=150_000, recorded_at="2026-09-18T00:02:00+00:00",
        ))
        self.assertEqual(over["context_advisory"]["level"], "over")

        null_observed = self._record_telemetry(self._telemetry_payload(
            dispatch_id, max_context_tokens=None, recorded_at="2026-09-18T00:03:00+00:00",
        ))
        self.assertEqual(
            null_observed["context_advisory"],
            {"level": "ok", "limit": 150_000, "warn_at": 120_000, "observed": None},
        )

    def test_record_telemetry_context_advisory_is_not_persisted(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]

        self._record_telemetry(self._telemetry_payload(dispatch_id, max_context_tokens=130_000))

        on_disk_batch = coordinator._read_object(
            self._records_root() / "batches" / f"{batch['batch_id']}.json", "batch",
        )
        telemetry_records = on_disk_batch.get("telemetry", [])
        self.assertEqual(len(telemetry_records), 1)
        self.assertNotIn("context_advisory", telemetry_records[0])
        self.assertEqual(
            set(telemetry_records[0]) - {"telemetry_id", "record_sha256"}, coordinator.TELEMETRY_FIELDS,
        )

    def test_dispatch_status_reports_latest_telemetry_and_context_advisory(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]

        self._record_telemetry(self._telemetry_payload(
            dispatch_id, max_context_tokens=50_000, recorded_at="2026-09-18T00:00:00+00:00",
        ))
        self._record_telemetry(self._telemetry_payload(
            dispatch_id, max_context_tokens=130_000, recorded_at="2026-09-18T00:05:00+00:00",
        ))

        status = coordinator.dispatch_status(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            dispatch=None, batch=batch["batch_id"], stale_after=900,
        ))
        entry = status["dispatches"][0]
        self.assertEqual(entry["telemetry"]["max_context_tokens"], 130_000)
        self.assertEqual(
            entry["context_advisory"], {"level": "warn", "limit": 150_000, "warn_at": 120_000, "observed": 130_000},
        )

    def test_dispatch_status_context_advisory_ok_when_no_telemetry(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        self._create_architect_dispatch(batch["batch_id"])

        status = coordinator.dispatch_status(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            dispatch=None, batch=batch["batch_id"], stale_after=900,
        ))
        entry = status["dispatches"][0]
        self.assertIsNone(entry["telemetry"])
        self.assertEqual(
            entry["context_advisory"], {"level": "ok", "limit": 150_000, "warn_at": 120_000, "observed": None},
        )

    def test_heartbeat_dispatch_records_context_tokens_probe_in_extra(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]
        coordinator.send_dispatch(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            dispatch=dispatch_id, adapter=None, adapter_arg=None, checkout=None,
        ))

        result = coordinator.heartbeat_dispatch(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir), dispatch=dispatch_id, note=None,
            context_tokens=142_000, context_source="probe",
        ))
        self.assertEqual(result["context_tokens"], 142_000)
        self.assertEqual(result["context_source"], "probe")
        on_disk = coordinator._read_object(
            self._records_root() / "dispatch-status" / f"{dispatch_id}.json", "status",
        )
        self.assertEqual(on_disk["context_tokens"], 142_000)
        self.assertEqual(on_disk["context_source"], "probe")

    def test_heartbeat_dispatch_without_context_tokens_omits_extra_fields(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]
        coordinator.send_dispatch(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            dispatch=dispatch_id, adapter=None, adapter_arg=None, checkout=None,
        ))

        result = coordinator.heartbeat_dispatch(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir), dispatch=dispatch_id, note=None,
            context_tokens=None, context_source=None,
        ))
        self.assertNotIn("context_tokens", result)
        self.assertNotIn("context_source", result)
        on_disk = coordinator._read_object(
            self._records_root() / "dispatch-status" / f"{dispatch_id}.json", "status",
        )
        self.assertNotIn("context_tokens", on_disk)
        self.assertNotIn("context_source", on_disk)

    def test_heartbeat_dispatch_requires_context_tokens_and_source_together(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])
        dispatch_id = dispatch["dispatch_id"]
        coordinator.send_dispatch(_ns(
            repo=str(self.repo), state_dir=str(self.state_dir),
            dispatch=dispatch_id, adapter=None, adapter_arg=None, checkout=None,
        ))

        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.heartbeat_dispatch(_ns(
                repo=str(self.repo), state_dir=str(self.state_dir), dispatch=dispatch_id, note=None,
                context_tokens=142_000, context_source=None,
            ))
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.heartbeat_dispatch(_ns(
                repo=str(self.repo), state_dir=str(self.state_dir), dispatch=dispatch_id, note=None,
                context_tokens=None, context_source="probe",
            ))

    def _configure_project(self, **extra: object) -> None:
        """Turn the zero-config test repository into a configured one: a real `.harness/orchestration.json`
        (architect + the mandatory code-review assignment) plus the role manifests it validates against."""
        roles_dir = self.repo / ".harness" / "orchestration" / "roles"
        (roles_dir / "code-review.md").write_text(
            (ORCHESTRATION_ROOT / "roles" / "code-review.md").read_text(encoding="utf-8"), encoding="utf-8",
        )
        runtime = {"claude": {"profiles": ["p"], "model": "sonnet", "effort": "high"}}
        config = {
            "provider_profiles": {"p": {
                "capabilities": ["architecture-analysis", "code-review"], "agent": "claude",
                "fallback": [], "known_limitations": ["none"],
            }},
            "assignment_plans": {
                "architect": {"zone": "repository", "runtimes": runtime},
                "code-review": {"zone": "repository", "runtimes": runtime},
            },
            "backend_zones": {"repository": {"paths": ["**"]}},
            "concurrency_budget": 1,
            "verification_commands": ["true"],
            **extra,
        }
        (self.repo / ".harness" / "orchestration.json").write_text(json.dumps(config), encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "configure orchestration")
        _git(self.repo, "push", "origin", "master")

    def _rewritten_brief_validation(
        self, batch_id: str, dispatch_id: str, drop: set[str], **replace: object,
    ) -> None:
        """Rewrite a stored brief the way an older coordinator wrote it (fields dropped or replaced, integrity
        hash recomputed) and run the real `_validate_dispatch` against it."""
        root = coordinator._state_root(_ns(repo=str(self.repo), state_dir=str(self.state_dir)), self.repo)
        record = coordinator._read_object(self._records_root() / "dispatches" / f"{dispatch_id}.json", "dispatch")
        brief = {key: value for key, value in record.items() if key not in drop}
        brief.update(replace)
        batch = coordinator._load_batch(root, batch_id)
        entry = next(item for item in batch["dispatches"] if item["dispatch_id"] == dispatch_id)
        entry["brief_sha256"] = hashlib.sha256(coordinator._canonical(brief).encode("utf-8")).hexdigest()
        coordinator._validate_dispatch(self.repo, coordinator._config(self.repo), root, batch, brief)

    def test_dispatch_brief_records_default_tool_policy_and_context_budget(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])

        dispatch = self._create_architect_dispatch(batch["batch_id"])

        expected_tools = list(contract.DEFAULT_ALLOWED_TOOLS["read-only"])
        self.assertEqual(dispatch["brief"]["allowed_tools"], expected_tools)
        self.assertEqual(dispatch["brief"]["context_budget"], 150_000)
        on_disk = coordinator._read_object(
            self._records_root() / "dispatches" / f"{dispatch['dispatch_id']}.json", "dispatch",
        )
        self.assertEqual(on_disk["allowed_tools"], expected_tools)
        self.assertEqual(on_disk["context_budget"], 150_000)

    def test_dispatch_brief_takes_tool_policy_and_context_budget_from_project_config(self) -> None:
        self._configure_project(
            adaptive_continuation_policy={"context_limit": 90_000},
            tool_policy={"roles": {"architect": ["Read", "Grep"]}},
        )
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])

        dispatch = self._create_architect_dispatch(batch["batch_id"])

        self.assertEqual(dispatch["brief"]["allowed_tools"], ["Read", "Grep"])
        self.assertEqual(dispatch["brief"]["context_budget"], 90_000)

    def test_only_write_mode_default_tools_include_edit_tools(self) -> None:
        for name in ("Edit", "Write"):
            self.assertNotIn(name, contract.DEFAULT_ALLOWED_TOOLS["read-only"])
            self.assertIn(name, contract.DEFAULT_ALLOWED_TOOLS["write"])

    def test_resolve_allowed_tools_prefers_role_then_mode_then_default(self) -> None:
        policy = {"tool_policy": {"modes": {"write": ["Read", "Edit"]}, "roles": {"qa": ["Read", "Bash"]}}}

        self.assertEqual(contract.resolve_allowed_tools(policy, "qa", "read-only"), ["Read", "Bash"])
        self.assertEqual(contract.resolve_allowed_tools(policy, "developer", "write"), ["Read", "Edit"])
        self.assertEqual(
            contract.resolve_allowed_tools(policy, "architect", "read-only"),
            list(contract.DEFAULT_ALLOWED_TOOLS["read-only"]),
        )
        self.assertEqual(
            contract.resolve_allowed_tools({}, "developer", "write"), list(contract.DEFAULT_ALLOWED_TOOLS["write"]),
        )

    def test_brief_created_before_tool_policy_fields_stays_valid(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])

        self._rewritten_brief_validation(
            batch["batch_id"], dispatch["dispatch_id"], {"allowed_tools", "context_budget"},
        )
        self._rewritten_brief_validation(
            batch["batch_id"], dispatch["dispatch_id"],
            {"allowed_tools", "context_budget", "report_staging_path"},
        )

    def test_brief_with_only_one_tool_policy_field_is_rejected(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])

        for dropped in ("allowed_tools", "context_budget"):
            with self.assertRaisesRegex(coordinator.CoordinatorError, "schema mismatch"):
                self._rewritten_brief_validation(batch["batch_id"], dispatch["dispatch_id"], {dropped})

    def test_brief_with_tools_outside_the_project_policy_is_rejected(self) -> None:
        batch = self._create_batch()
        self._approve_batch(batch["batch_id"])
        dispatch = self._create_architect_dispatch(batch["batch_id"])

        with self.assertRaisesRegex(coordinator.CoordinatorError, "allowed_tools"):
            self._rewritten_brief_validation(
                batch["batch_id"], dispatch["dispatch_id"], set(), allowed_tools=["Read", "Edit", "Write"],
            )
        with self.assertRaisesRegex(coordinator.CoordinatorError, "context_budget"):
            self._rewritten_brief_validation(
                batch["batch_id"], dispatch["dispatch_id"], set(), context_budget=1_000_000,
            )

    def _tool_policy_health(self, tool_policy: object) -> list[str]:
        path = self.tmp / "orchestration.json"
        path.write_text(json.dumps({
            "provider_profiles": {}, "assignment_plans": {}, "backend_zones": {}, "concurrency_budget": 1,
            "verification_commands": [], "tool_policy": tool_policy,
        }), encoding="utf-8")
        return contract.health_problems(path, ORCHESTRATION_ROOT / "roles")

    def test_health_accepts_a_valid_tool_policy(self) -> None:
        self.assertEqual(
            self._tool_policy_health({"modes": {"read-only": ["Read"]}, "roles": {"qa": ["Read", "Bash"]}}), [],
        )

    def test_health_rejects_an_invalid_tool_policy(self) -> None:
        for invalid in (
            ["Read"], {"other": {}}, {"modes": ["Read"]}, {"modes": {"admin": ["Read"]}},
            {"roles": {"no-such-role": ["Read"]}}, {"roles": {"qa": []}}, {"roles": {"qa": ["Read", ""]}},
            {"roles": {"qa": ["Read", "Read"]}}, {"roles": {"qa": "Read"}},
        ):
            with self.subTest(tool_policy=invalid):
                problems = self._tool_policy_health(invalid)
                self.assertTrue(
                    any(problem.startswith("orchestration tool_policy") for problem in problems), problems,
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
