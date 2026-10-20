"""Operator recovery through the public coordinator, with real Git and ledger evidence."""

from __future__ import annotations
import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, tzinfo
from typing import cast
from unittest import mock
from pathlib import Path
from harness.orchestration import coordinator
from harness.orchestration.core import git_utils, workspace
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger import ledger_ops
from harness.orchestration.ledger.lifecycle import LifecycleLedger, BatchRecord
from harness.orchestration.workflow import (
    carried_items,
    commit_plan,
    decisions,
    recovery,
    risk as risk_policy,
)
from tests.orchestration.test_coordinator import (
    _init_repo,
    _git,
    _ns,
    ORCHESTRATION_ROOT,
)


class RecoveryTests(unittest.TestCase):
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
        self.state_dir = self.repo / ".harness/orchestration/state"
        self.worktree = self.tmp / "worktree"
        self.branch = "feature/issue-662-recovery"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_policy_blocks_wrong_or_missing_worker_checkout_without_retry_cost(
        self,
    ) -> None:
        for role, reported_path in (
            ("architect", "main"),
            ("developer", "main"),
            ("developer", None),
            ("code-review", "main"),
        ):
            with self.subTest(role=role, reported_path=reported_path):
                batch = self._create_batch()
                candidate = None
                if role != "architect":
                    self._accepted_architect(batch["batch_id"])
                if role == "code-review":
                    candidate = self._accepted_candidate(batch["batch_id"])
                brief = self._dispatch(batch["batch_id"], role, candidate=candidate)[
                    "brief"
                ]
                self.assertFalse(brief["worker_attestation_required"])
                coordinator.send_dispatch(
                    self._args(
                        dispatch=brief["dispatch_id"],
                        adapter=None,
                        adapter_arg=None,
                        checkout=str(self.worktree) if role == "code-review" else None,
                        worktree=str(self.worktree),
                    )
                )
                with self.assertRaisesRegex(coordinator.CoordinatorError, "blocked"):
                    coordinator.self_report_dispatch(
                        self._args(
                            dispatch=brief["dispatch_id"],
                            model="sonnet",
                            worktree=str(self.repo) if reported_path else None,
                        )
                    )
                stored = self._batch_record(batch["batch_id"])
                self.assertEqual(stored["state"], "blocked")
                self.assertEqual(decisions._developer_retry_count(stored), 0)
                self.assertTrue(coordinator.validate_ledger(self._args())["valid"])
                # Each case needs a fresh issue branch/worktree in this fixture.
            self.tearDown()
            self.setUp()

    def test_auto_review_threshold_routes_through_public_decision_guards(self) -> None:
        for threshold, severity, recommendation, expected in (
            ("warning", "info", "accept", "accept"),
            ("warning", "warning", "accept", "retry"),
            ("error", "warning", "accept", "retry"),
            ("blocker", "warning", "accept", "retry"),
            ("info", None, "retry", "retry"),
        ):
            with self.subTest(
                threshold=threshold, severity=severity, recommendation=recommendation
            ):
                (self.repo / ".harness/orchestration.json").write_text(
                    json.dumps(
                        {
                            "provider_profiles": {},
                            "assignment_plans": {},
                            "concurrency_budget": 1,
                            "verification_commands": ["true"],
                            "worker_attestation_required": True,
                            "approval_policy": "auto",
                            "auto_retry_min_severity": threshold,
                        }
                    ),
                    encoding="utf-8",
                )
                batch = self._create_batch()
                self._accepted_architect(batch["batch_id"])
                candidate = self._accepted_candidate(batch["batch_id"])
                brief = self._dispatch(
                    batch["batch_id"], "code-review", candidate=candidate
                )["brief"]
                self._start(brief["dispatch_id"], checkout=self.worktree)
                findings = (
                    []
                    if severity is None
                    else [
                        {
                            "severity": severity,
                            "summary": "review context",
                            "evidence": "services/x.py:1",
                        }
                    ]
                )
                review = {
                    "candidate_commit": candidate,
                    "scope": brief["review_scope"],
                    "recommendation": recommendation,
                    **self._axes(
                        ("warning" if severity == "warning" else "clean", findings),
                        ("clean", []),
                    ),
                }
                self._submit(
                    brief["dispatch_id"],
                    self._base_report(brief, "code-review", review=review),
                )
                coordinator.auto_decide(self._args(batch=batch["batch_id"]))
                stored = self._batch_record(batch["batch_id"])
                entry = stored["dispatches"][-1]
                self.assertEqual(entry["decision"]["decision"], expected)
                self.assertEqual(
                    decisions._developer_retry_count(stored), int(expected == "retry")
                )
                persisted = coordinator._read_object(
                    self._records() / "reports" / f"{brief['dispatch_id']}.json",
                    "report",
                )
                self.assertEqual(persisted["review"]["standards"]["findings"], findings)
                self.assertTrue(coordinator.validate_ledger(self._args())["valid"])
            self.tearDown()
            self.setUp()

    def _args(self, **values: object) -> argparse.Namespace:
        return _ns(repo=str(self.repo), state_dir=str(self.state_dir), **values)

    def test_repeated_environment_failures_keep_original_stop_and_bounded_attempts(
        self,
    ) -> None:
        batch, _ = self._failed_launch(retry=True)
        coordinator.auto_decide(self._args(batch=batch["batch_id"]))
        original = self._batch_record(batch["batch_id"])["auto_stop"]
        for attempt in range(2):
            coordinator.resume_stopped_batch(
                self._args(
                    batch=batch["batch_id"],
                    note=f"repair runtime launch {attempt}",
                    **self._approval(),
                )
            )
            brief = self._dispatch(batch["batch_id"], "developer")["brief"]
            coordinator.send_dispatch(
                self._args(
                    dispatch=brief["dispatch_id"],
                    adapter=None,
                    adapter_arg=None,
                    checkout=None,
                    worktree=str(self.worktree),
                )
            )
            with self.assertRaises(coordinator.CoordinatorError):
                coordinator.self_report_dispatch(
                    self._args(
                        dispatch=brief["dispatch_id"],
                        model="sonnet",
                        worktree=str(self.repo),
                    )
                )
        before = self._ledger_bytes()
        stopped_state = self._batch_record(batch["batch_id"])["state"]
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "budget exhausted"
        ) as caught:
            coordinator.resume_stopped_batch(
                self._args(
                    batch=batch["batch_id"],
                    note="third launch failure",
                    **self._approval(),
                )
            )
        # Exhaustion is a pause for a human choice: no abandon, no new attempt, no reset counter.
        self.assertEqual(before, self._ledger_bytes())
        self.assertEqual(stopped_state, self._batch_record(batch["batch_id"])["state"])
        self.assertIn(f"stays {stopped_state}", caught.exception.remedy)
        self.assertIn("separate abandon decision", caught.exception.remedy)
        self.assertIn("does not reset counters", caught.exception.remedy)
        self.assertEqual(original, self._batch_record(batch["batch_id"])["auto_stop"])
        coordinator.validate_ledger(self._args())

    def test_resume_stop_after_a_refused_self_report_keeps_its_human_approved_route(
        self,
    ) -> None:
        batch, old = self._failed_launch()
        coordinator.auto_decide(self._args(batch=batch["batch_id"]))

        result = coordinator.resume_stopped_batch(
            self._args(
                batch=batch["batch_id"], note="checkout fixed", **self._approval()
            )
        )

        assessment = result["event"]["evidence"]["failure"]["startup_assessment"]
        self.assertEqual(assessment["worker_state"], "rejected-at-startup")
        self.assertEqual(assessment["classification"], "worktree-mismatch")
        # The retired dispatch can no longer write to the ledger.
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.heartbeat_dispatch(
                self._args(
                    dispatch=old["dispatch_id"],
                    note="late",
                    context_tokens=None,
                    context_source=None,
                )
            )

    def test_resume_stop_records_the_operator_stop_proof(self) -> None:
        batch, _ = self._failed_launch()
        coordinator.auto_decide(self._args(batch=batch["batch_id"]))

        result = coordinator.resume_stopped_batch(
            self._args(
                batch=batch["batch_id"],
                note="runtime session stopped",
                runtime_stopped=True,
                **self._approval(),
            )
        )

        assessment = result["event"]["evidence"]["failure"]["startup_assessment"]
        self.assertEqual(assessment["worker_state"], "stopped")

    def test_a_worker_with_an_unknown_outcome_is_restarted_only_after_runtime_stopped(
        self,
    ) -> None:
        batch, _ = self._failed_launch()
        stored = self._batch_record(batch["batch_id"])
        entry = stored["dispatches"][-1]
        root = ledger_ops._state_root(self._args(), self.repo)
        unknown = {
            "state": "blocked",
            "runtime_failure": {"error": "provider is at capacity"},
        }

        with mock.patch.object(
            ledger_ops, "_load_dispatch_status", return_value=unknown
        ):
            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "outcome is unknown"
            ) as caught:
                recovery._confirmed_failure(root, stored, entry)
            evidence = recovery._confirmed_failure(
                root, stored, entry, runtime_stopped=True
            )

        self.assertIn("--runtime-stopped", caught.exception.remedy)
        self.assertIn("batch decision-packet", caught.exception.remedy)
        self.assertEqual(evidence["startup_assessment"]["worker_state"], "stopped")
        self.assertEqual(evidence["classification"]["category"], "capacity")

    def test_blocked_and_failed_stages_rewind_to_fresh_architecture(self) -> None:
        batch = self._create_batch()
        for decision in ("block", "fail"):
            with self.subTest(decision=decision):
                brief = self._dispatch(batch["batch_id"], "architect")["brief"]
                self._start(brief["dispatch_id"])
                self._submit(
                    brief["dispatch_id"],
                    self._base_report(
                        brief,
                        "architect",
                        outcome="blocked",
                        blockers={
                            "status": "present",
                            "details": "operator review required",
                        },
                    ),
                )
                self._decide(batch["batch_id"], decision)
                self.assertEqual(
                    self._batch_record(batch["batch_id"])["state"],
                    "blocked" if decision == "block" else "failed",
                )
                coordinator.validate_ledger(self._args())
                coordinator.rewind_batch(
                    self._args(
                        batch=batch["batch_id"],
                        to="architect",
                        note="repeat preparation after operator decision",
                        **self._approval(),
                    )
                )
                coordinator.validate_ledger(self._args())
        self._accepted_architect(batch["batch_id"])
        developer = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(developer["dispatch_id"])
        coordinator.validate_ledger(self._args())

    def test_resealed_recovery_event_cannot_replace_its_original_audit(self) -> None:
        from harness.orchestration.workflow.approval import sealed

        batch, _ = self._failed_launch()
        coordinator.auto_decide(self._args(batch=batch["batch_id"]))
        coordinator.resume_stopped_batch(
            self._args(
                batch=batch["batch_id"],
                note="fixed checkout",
                **self._approval(),
            )
        )
        stored = self._batch_record(batch["batch_id"])
        event = stored["recovery_events"][-1]
        event["note"] = "substituted approval"
        stored["recovery_events"][-1] = sealed(
            {k: v for k, v in event.items() if k != "record_sha256"}
        )
        path = self._records() / "batches" / f"{batch['batch_id']}.json"
        path.write_text(json.dumps(stored), encoding="utf-8")
        before = self._ledger_bytes()
        with self.assertRaisesRegex(coordinator.CoordinatorError, "audit"):
            coordinator.validate_ledger(self._args())
        self.assertEqual(before, self._ledger_bytes())

    def test_dead_session_before_first_checkpoint_needs_human_startup_proof(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        coordinator.send_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )
        facts = {
            "dispatch_id": brief["dispatch_id"],
            **{
                key: brief[key]
                for key in (
                    "definition_of_done",
                    "dependencies",
                    "write_paths",
                    "prohibited_changes",
                )
            },
        }
        path = workspace._prepare_agent_inbox(self.repo) / "startup-facts.json"
        path.write_text(json.dumps(facts), encoding="utf-8")
        fields = {
            "dispatch": brief["dispatch_id"],
            "termination_reason": "worker-exited",
            "trigger": "startup-failure",
            "file": str(path),
            "note": "runtime confirms old session stopped",
        }
        before = self._ledger_bytes()
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.resume_dispatch(self._args(**fields, **self._approval()))
        self.assertEqual(before, self._ledger_bytes())
        coordinator.resume_dispatch(
            self._args(**fields, runtime_stopped=True, **self._approval())
        )
        recorded = self._batch_record(batch["batch_id"])
        self.assertEqual(recorded.get("checkpoints", []), [])
        self.assertEqual(
            recorded["startup_evidence"][-1]["commit_sha"], brief["snapshot_commit"]
        )
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.resume_dispatch(
                self._args(**fields, runtime_stopped=True, **self._approval())
            )
        coordinator.self_report_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                model="sonnet",
                worktree=str(self.worktree),
            )
        )
        coordinator.heartbeat_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                note="new worker session",
                context_tokens=None,
                context_source=None,
            )
        )
        coordinator.validate_ledger(self._args())

    def test_rewind_preserves_committed_checkpoint_without_accepting_it(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(brief["dispatch_id"])
        candidate, changed = self._developer_commit("checkpoint")
        package = self._batch_record(batch["batch_id"])["context_packages"][-1]
        path = workspace._prepare_agent_inbox(self.repo) / "checkpoint.json"
        path.write_text(
            json.dumps(
                {
                    "dispatch_id": brief["dispatch_id"],
                    "commit_sha": candidate,
                    "changed_files": changed,
                    "remaining_definition_of_done": brief["definition_of_done"],
                    "passing_checks": self._checks(brief),
                    "risks": {"status": "none", "details": ""},
                    "blockers": {"status": "none", "details": ""},
                    "context_package_id": package["context_package_id"],
                }
            ),
            encoding="utf-8",
        )
        coordinator.checkpoint_dispatch(self._args(file=str(path)))
        checkpoint = self._batch_record(batch["batch_id"])["checkpoints"][-1]
        immutable = (
            self._records() / "checkpoints" / f"{checkpoint['checkpoint_id']}.json"
        )
        original = immutable.read_bytes()
        result = coordinator.rewind_batch(
            self._args(
                batch=batch["batch_id"],
                to="developer",
                note="continue known green progress",
                **self._approval(),
            )
        )
        self.assertEqual(result["event"]["evidence"]["writer_start_commit"], candidate)
        self.assertEqual(immutable.read_bytes(), original)
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), candidate)
        renewed = self._dispatch(batch["batch_id"], "developer")["brief"]
        self.assertEqual(renewed["snapshot_commit"], candidate)
        self._start(renewed["dispatch_id"])
        self.assertFalse(
            any(
                e.get("decision", {}).get("decision") == "accept"
                and e["role"] == "developer"
                for e in self._batch_record(batch["batch_id"])["dispatches"]
            )
        )
        coordinator.validate_ledger(self._args())

    def test_cli_rewind_preserves_unreported_writer_commits_and_pending_files(
        self,
    ) -> None:
        self._configure_preserved_recovery(approval_policy="auto")
        batch = self._create_batch(allowed_paths=["services/*.py"])
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(brief["dispatch_id"])
        candidate, _ = self._developer_commit("preserved")
        pending = self.worktree / "services" / "pending.py"
        pending.write_text("VALUE = 'pending'\n", encoding="utf-8")
        original = pending.read_bytes()
        tracked = self.worktree / "services/preserved.py"
        tracked.write_text("VALUE = 'staged'\n", encoding="utf-8")
        _git(self.worktree, "add", "services/preserved.py")
        tracked.write_text("VALUE = 'unstaged'\n", encoding="utf-8")
        original_status = _git(self.worktree, "status", "--porcelain")
        # Elapse time at the public clock boundary; never edit ledger status to manufacture stale.
        later = datetime.now().astimezone() + timedelta(hours=2)

        class Later(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> Later:
                return cls.fromtimestamp(later.timestamp(), tz)

        with mock.patch("harness.orchestration.core.utils.datetime", Later):
            paused = coordinator.auto_decide(self._args(batch=batch["batch_id"]))
        self.assertEqual(paused["batch_state"], "paused")
        self.assertEqual(
            self._batch_record(batch["batch_id"])["auto_stop"]["reason"], "stale"
        )
        coordinator.cancel_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                runtime_stopped=True,
                reason="operator stopped the runtime",
                **self._approval(),
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATION_ROOT / "coordinator.py"),
                "--repo",
                str(self.repo),
                "batch",
                "rewind",
                "--batch",
                batch["batch_id"],
                "--to",
                "developer",
                "--approved-by",
                "Malove",
                "--approved-at",
                self.APPROVED_AT,
                "--note",
                "continue preserved writer progress without accepting it",
                "--preserved-commit",
                candidate,
                "--preserved-file",
                "services/pending.py",
                "--preserved-file",
                "services/preserved.py",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), candidate)
        self.assertEqual(pending.read_bytes(), original)
        renewed = self._dispatch(batch["batch_id"], "developer")["brief"]
        self.assertEqual(renewed["snapshot_commit"], candidate)
        handoff = renewed["preserved_progress"]
        self.assertEqual(handoff["source_dispatch_id"], brief["dispatch_id"])
        self.assertEqual(
            set(handoff["files"]), {"services/pending.py", "services/preserved.py"}
        )
        self.assertNotEqual(renewed["transition_digest"], brief["transition_digest"])
        self._start(renewed["dispatch_id"])
        self.assertEqual(pending.read_bytes(), original)
        self.assertEqual(_git(self.worktree, "status", "--porcelain"), original_status)
        self.assertEqual(
            decisions._developer_retry_count(self._batch_record(batch["batch_id"])), 1
        )
        stored = self._batch_record(batch["batch_id"])
        self.assertTrue(stored["manual_recovery"])
        self.assertFalse(
            any(
                e.get("decision", {}).get("decision") == "accept"
                and e["role"] == "developer"
                for e in stored["dispatches"]
            )
        )
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])
        # The historical handoff must not freeze legitimate post-startup writer progress.
        self._developer_commit("continued")
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_preserved_handoff_refuses_drift_at_each_launch_boundary(self) -> None:
        for boundary, mutation in (
            ("preflight", "bytes"),
            ("propose", "bytes"),
            ("create", "index"),
            ("send", "bytes"),
            ("self-report", "extra"),
        ):
            with self.subTest(boundary=boundary, mutation=mutation):
                self._configure_preserved_recovery()
                batch = self._create_batch()
                self._accepted_architect(batch["batch_id"])
                source = self._dispatch(batch["batch_id"], "developer")["brief"]
                self._start(source["dispatch_id"])
                candidate, _ = self._developer_commit("preserved")
                pending = self.worktree / "services/pending.py"
                pending.write_text("pending\n", encoding="utf-8")
                coordinator.cancel_dispatch(
                    self._args(
                        dispatch=source["dispatch_id"],
                        runtime_stopped=True,
                        reason="runtime stopped",
                        **self._approval(),
                    )
                )
                coordinator.rewind_batch(
                    self._args(
                        batch=batch["batch_id"],
                        to="developer",
                        note="preserve unfinished work",
                        preserved_commit=candidate,
                        preserved_file=["services/pending.py"],
                        **self._approval(),
                    )
                )
                proposal = self._propose(batch["batch_id"], "developer")
                renewed: JsonObject = {}
                if boundary in {"send", "self-report"}:
                    renewed = self._dispatch(batch["batch_id"], "developer")["brief"]
                if boundary == "self-report":
                    coordinator.send_dispatch(
                        self._args(
                            dispatch=renewed["dispatch_id"],
                            adapter=None,
                            adapter_arg=None,
                            checkout=None,
                            worktree=str(self.worktree),
                        )
                    )
                if mutation == "index":
                    _git(self.worktree, "add", "services/pending.py")
                elif mutation == "extra":
                    (self.worktree / "services/foreign.py").write_text("foreign\n")
                else:
                    pending.write_text("changed after approval\n", encoding="utf-8")
                before = self._ledger_bytes()
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "changed after human approval"
                ):
                    if boundary == "preflight":
                        coordinator.preflight_dispatch(
                            self._args(
                                **self._proposal_fields(
                                    batch["batch_id"], "developer", "work", None
                                )
                            )
                        )
                    elif boundary == "propose":
                        self._propose(batch["batch_id"], "developer")
                    elif boundary == "create":
                        coordinator.create_dispatch(
                            self._args(
                                transition_digest=proposal["transition_digest"],
                                **self._proposal_fields(
                                    batch["batch_id"], "developer", "work", None
                                ),
                                **self._approval(),
                            )
                        )
                    elif boundary == "send":
                        coordinator.send_dispatch(
                            self._args(
                                dispatch=renewed["dispatch_id"],
                                adapter=None,
                                adapter_arg=None,
                                checkout=None,
                                worktree=str(self.worktree),
                            )
                        )
                    else:
                        coordinator.self_report_dispatch(
                            self._args(
                                dispatch=renewed["dispatch_id"],
                                model="sonnet",
                                worktree=str(self.worktree),
                            )
                        )
                self.assertEqual(self._ledger_bytes(), before)
                self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), candidate)
                self.assertTrue(coordinator.validate_ledger(self._args())["valid"])
            self.tearDown()
            self.setUp()

    def _configure_preserved_recovery(
        self, approval_policy: str = "manual_all"
    ) -> None:
        (self.repo / ".harness/orchestration.json").write_text(
            json.dumps(
                {
                    "provider_profiles": {},
                    "assignment_plans": {},
                    "concurrency_budget": 1,
                    "verification_commands": ["true"],
                    "worker_attestation_required": True,
                    "approval_policy": approval_policy,
                    "auto_retry_min_severity": "warning",
                }
            ),
            encoding="utf-8",
        )

    def test_preserved_rewind_refuses_unconfirmed_or_foreign_progress(self) -> None:
        for scenario, message in (
            ("live", "worker may still be running"),
            ("missing-file", "does not match all pending"),
            ("extra-file", "does not match all pending"),
            ("wrong-head", "immutable startup snapshot"),
            ("wrong-branch", "branch"),
            ("unrelated-head", "not a descendant"),
            ("dirty-outside", "dirty files exceed"),
            ("commit-outside", "commits exceed"),
            ("staged-outside", "dirty files exceed"),
            ("symlink", "symlink"),
            ("forward", "skip forward"),
            ("policy", "explicit human approval"),
        ):
            with self.subTest(scenario=scenario):
                self._configure_preserved_recovery()
                # Narrow immutable scope so foreign files are distinguishable from writer work.
                batch = self._create_batch(allowed_paths=["services/**"])
                self._accepted_architect(batch["batch_id"])
                source = self._dispatch(batch["batch_id"], "developer")["brief"]
                self._start(source["dispatch_id"])
                candidate, _ = self._developer_commit("preserved")
                pending = self.worktree / "services/pending.py"
                pending.write_text("pending\n", encoding="utf-8")
                files = ["services/pending.py"]
                if scenario != "live":
                    coordinator.cancel_dispatch(
                        self._args(
                            dispatch=source["dispatch_id"],
                            runtime_stopped=True,
                            reason="runtime stopped",
                            **self._approval(),
                        )
                    )
                if scenario == "missing-file":
                    files = []
                elif scenario == "extra-file":
                    files.append("services/absent.py")
                elif scenario == "wrong-head":
                    candidate = source["snapshot_commit"]
                elif scenario == "wrong-branch":
                    _git(self.worktree, "checkout", "-b", "feature/issue-999-foreign")
                elif scenario == "unrelated-head":
                    candidate = _git(
                        self.worktree,
                        "commit-tree",
                        f"{candidate}^{{tree}}",
                        "-m",
                        "unrelated fixture root",
                    )
                    _git(
                        self.worktree,
                        "update-ref",
                        f"refs/heads/{self.branch}",
                        candidate,
                    )
                elif scenario == "dirty-outside":
                    (self.worktree / "foreign.py").write_text("foreign\n")
                    files.append("foreign.py")
                elif scenario == "commit-outside":
                    (self.worktree / "foreign.py").write_text("foreign\n")
                    _git(self.worktree, "add", "foreign.py")
                    _git(self.worktree, "commit", "-m", "foreign fixture change")
                    candidate = _git(self.worktree, "rev-parse", "HEAD")
                elif scenario == "staged-outside":
                    foreign = self.worktree / "foreign.py"
                    foreign.write_text("staged foreign\n")
                    _git(self.worktree, "add", "foreign.py")
                    foreign.unlink()
                elif scenario == "symlink":
                    pending.unlink()
                    pending.symlink_to(self.repo / "README.md")
                before = self._ledger_bytes()
                status_before = _git(self.worktree, "status", "--porcelain")
                with self.assertRaisesRegex(coordinator.CoordinatorError, message):
                    coordinator.rewind_batch(
                        self._args(
                            batch=batch["batch_id"],
                            to="qa" if scenario == "forward" else "developer",
                            note="preserve unfinished work",
                            preserved_commit=candidate,
                            preserved_file=files,
                            approved_by="policy:auto"
                            if scenario == "policy"
                            else "Malove",
                            approved_at=self.APPROVED_AT,
                        )
                    )
                self.assertEqual(self._ledger_bytes(), before)
                self.assertEqual(
                    _git(self.worktree, "status", "--porcelain"), status_before
                )
                self.assertTrue(coordinator.validate_ledger(self._args())["valid"])
            self.tearDown()
            self.setUp()

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
            "ticket": "#662",
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
        self,
        *,
        definition_of_done: list[str] | None = None,
        allowed_paths: list[str] | None = None,
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
        if allowed_paths is not None:
            plan["allowed_path"] = allowed_paths
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
        for entry in reversed(self._batch_record(batch_id)["dispatches"]):
            if (
                entry.get("state") == "approved"
                and entry.get("role") == role
                and entry.get("purpose", "work") == purpose
            ):
                return {
                    "brief": coordinator._read_object(
                        self._records() / "dispatches" / f"{entry['dispatch_id']}.json",
                        "dispatch",
                    )
                }
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
            "risks": {"status": "none", "details": ""},
            "blockers": {"status": "none", "details": ""},
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
        if not any(
            item.get("decision")
            for item in self._batch_record(batch_id)["dispatches"]
            if item["dispatch_id"] == brief["dispatch_id"]
        ):
            self._decide(batch_id, "accept")

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
        if not any(
            item.get("decision")
            for item in self._batch_record(batch_id)["dispatches"]
            if item["dispatch_id"] == brief["dispatch_id"]
        ):
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
                "risks": {"status": "none", "details": ""},
                "blockers": {"status": "none", "details": ""},
            }
            for axis, (severity, findings) in (("standards", standards), ("spec", spec))
        }

    def _reported_review(
        self,
        batch_id: str,
        candidate: str,
        *,
        outcome: str = "completed",
        blockers: JsonObject | None = None,
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
                blockers=blockers
                if blockers is not None
                else {"status": "none", "details": ""},
                checks_run=self._checks(
                    brief, "pass" if outcome == "completed" else "not_run"
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

    def test_ledger_validate_is_read_only_on_absent_and_populated_state(self) -> None:
        self.assertEqual(coordinator.validate_ledger(self._args())["valid"], True)
        self.assertFalse(self.state_dir.exists())
        batch = self._create_batch()
        before = self._ledger_bytes()
        result = coordinator.validate_ledger(self._args())
        self.assertTrue(result["valid"])
        self.assertIn(batch["batch_id"], result["batches"])
        self.assertEqual(before, self._ledger_bytes())

    def _ledger_bytes(self) -> dict[str, bytes]:
        return {
            str(p.relative_to(self.state_dir)): p.read_bytes()
            for p in self.state_dir.rglob("*")
            if p.is_file()
        }

    def _failed_launch(self, *, retry: bool = False) -> tuple[JsonObject, JsonObject]:
        runtime = {"claude": {"profiles": ["p"], "model": "sonnet", "effort": "high"}}
        (self.repo / ".harness/orchestration.json").write_text(
            json.dumps(
                {
                    "provider_profiles": {
                        "p": {
                            "capabilities": [
                                "architecture-analysis",
                                "backend-development",
                                "code-review",
                            ],
                            "fallback": [],
                            "known_limitations": ["none"],
                        }
                    },
                    "assignment_plans": {
                        role: {"zone": "repository", "runtimes": runtime}
                        for role in ("architect", "developer", "code-review")
                    },
                    "backend_zones": {"repository": {"paths": ["**"]}},
                    "concurrency_budget": 1,
                    "verification_commands": ["true"],
                    "approval_policy": "auto",
                    "worker_attestation_required": True,
                }
            )
        )
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        if retry:
            candidate = self._accepted_candidate(batch["batch_id"])
            findings = [
                {
                    "severity": "info",
                    "summary": f"item {i}",
                    "evidence": "services/x.py:1",
                }
                for i in range(4)
            ]
            self._reported_review(
                batch["batch_id"], candidate, standards=("clean", findings)
            )
            coordinator.auto_decide(self._args(batch=batch["batch_id"]))
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        if retry:
            self.retry_start = coordinator.preflight_dispatch(
                self._args(
                    batch=batch["batch_id"],
                    role="developer",
                    purpose="work",
                    runtime="claude",
                    candidate_commit=None,
                )
            )["retry_start"]
        coordinator.send_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                adapter=None,
                adapter_arg=None,
                checkout=None,
                worktree=str(self.worktree),
            )
        )
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.self_report_dispatch(
                self._args(
                    dispatch=brief["dispatch_id"],
                    model="sonnet",
                    worktree=str(self.repo),
                )
            )
        return batch, brief

    def test_auto_report_observes_mismatch_without_writing_stop(self) -> None:
        batch, _ = self._failed_launch()
        before = self._ledger_bytes()
        first = coordinator.auto_report(self._args(batch=batch["batch_id"]))
        second = coordinator.auto_report(self._args(batch=batch["batch_id"]))
        self.assertEqual(before, self._ledger_bytes())
        self.assertFalse(first["recorded"])
        self.assertEqual(first["observed_stop"]["reason"], "worktree-mismatch")
        self.assertEqual(first, second)

    def test_auto_decide_without_report_pauses_once_with_valid_evidence(self) -> None:
        batch, _ = self._failed_launch()
        paused = coordinator.auto_decide(self._args(batch=batch["batch_id"]))
        self.assertEqual(paused["batch_state"], "paused")
        self.assertEqual(
            self._batch_record(batch["batch_id"])["recovery_events"][-1]["kind"],
            "pause",
        )
        before = self._ledger_bytes()
        again = coordinator.auto_decide(self._args(batch=batch["batch_id"]))
        self.assertEqual(paused, again)
        self.assertEqual(before, self._ledger_bytes())
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_resume_stop_preserves_fix_forward_and_requires_fresh_human_approval(
        self,
    ) -> None:
        from harness.orchestration.workflow.decisions import _developer_retry_count

        batch, old = self._failed_launch(retry=True)
        coordinator.auto_decide(self._args(batch=batch["batch_id"]))
        before = self._batch_record(batch["batch_id"])
        self.assertEqual(_developer_retry_count(before), 1)
        result = coordinator.resume_stopped_batch(
            self._args(
                batch=batch["batch_id"],
                note="worker directory fixed",
                **self._approval(),
            )
        )
        self.assertEqual(result["next_action"], "developer-retry")
        snapshot = self._ledger_bytes()
        coordinator.resume_stopped_batch(
            self._args(
                batch=batch["batch_id"],
                note="worker directory fixed",
                **self._approval(),
            )
        )
        self.assertEqual(snapshot, self._ledger_bytes())
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.create_dispatch(
                self._args(
                    **self._proposal_fields(
                        batch["batch_id"], "developer", "work", None
                    ),
                    transition_digest=None,
                    approved_by=None,
                    approved_at=None,
                )
            )
        new = self._dispatch(batch["batch_id"], "developer")["brief"]
        self.assertNotEqual(old["dispatch_id"], new["dispatch_id"])
        self.assertNotEqual(old["transition_digest"], new["transition_digest"])
        self.assertEqual(old["snapshot_commit"], new["snapshot_commit"])
        prepared = coordinator.preflight_dispatch(
            self._args(
                batch=batch["batch_id"],
                role="developer",
                purpose="work",
                runtime="claude",
                candidate_commit=None,
            )
        )
        self.assertEqual(
            self.retry_start["handoff"], prepared["retry_start"]["handoff"]
        )
        after = self._batch_record(batch["batch_id"])
        self.assertEqual(before["auto_stop"], after["auto_stop"])
        self.assertEqual(_developer_retry_count(after), 1)
        self.assertEqual(
            coordinator.auto_report(self._args(batch=batch["batch_id"]))["report"][
                "outcome"
            ],
            "in-progress",
        )
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.heartbeat_dispatch(
                self._args(
                    dispatch=old["dispatch_id"],
                    note=None,
                    context_tokens=None,
                    context_source=None,
                )
            )
        self._start(new["dispatch_id"])
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_rewind_invalidates_qa_without_rewinding_git_or_deleting_reports(
        self,
    ) -> None:
        batch = self._create_batch()
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)
        candidate = self._accepted_candidate(batch_id)
        self._reported_review(batch_id, candidate)
        self._decide(batch_id, "accept")
        qa = self._dispatch(batch_id, "qa", candidate=candidate)["brief"]
        coordinator.run_qa(
            _ns(repo=str(self.repo), dispatch=qa["dispatch_id"], lease_seconds=None)
        )
        self._decide(batch_id, "accept")
        before = self._batch_record(batch_id)
        reports = {
            p: p.read_bytes() for p in (self._records() / "reports").glob("*.json")
        }
        result = coordinator.rewind_batch(
            self._args(
                batch=batch_id,
                to="code-review",
                note="repeat independent review",
                **self._approval(),
            )
        )
        self.assertEqual(result["next_action"], "code-review")
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), candidate)
        self.assertEqual(reports, {p: p.read_bytes() for p in reports})
        self.assertEqual(
            before["dispatches"], self._batch_record(batch_id)["dispatches"]
        )
        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(
                batch_id, "developer", purpose="publish", candidate=candidate
            )
        new = self._reported_review(batch_id, candidate)
        self.assertNotEqual(new["dispatch_id"], before["dispatches"][2]["dispatch_id"])
        self._decide(batch_id, "accept")
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_rewind_writer_keeps_candidate_and_requires_new_architect(self) -> None:
        batch = self._create_batch()
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)
        candidate = self._accepted_candidate(batch_id)
        coordinator.rewind_batch(
            self._args(
                batch=batch_id,
                to="developer",
                note="return to writer",
                **self._approval(),
            )
        )
        developer = self._dispatch(batch_id, "developer")["brief"]
        self.assertEqual(developer["snapshot_commit"], candidate)
        coordinator.cancel_dispatch(
            self._args(
                dispatch=developer["dispatch_id"],
                reason="stop unused writer",
                **self._approval(),
            )
        )
        coordinator.rewind_batch(
            self._args(
                batch=batch_id,
                to="architect",
                note="review architecture",
                **self._approval(),
            )
        )
        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(batch_id, "developer")
        self._accepted_architect(batch_id)
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), candidate)
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_early_429_resumes_known_startup_without_fabricating_checkpoint(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        coordinator.send_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                adapter=None,
                adapter_arg=None,
                checkout=None,
                worktree=str(self.worktree),
            )
        )
        coordinator.rate_limited_dispatch(
            self._args(dispatch=brief["dispatch_id"], retry_after_seconds=1)
        )
        args = self._args(
            dispatch=brief["dispatch_id"],
            termination_reason="429",
            trigger=None,
            measured_value=None,
            file=None,
            approved_by=None,
            approved_at=None,
            note=None,
        )
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.resume_dispatch(args)

        class Later(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> Later:
                return cls.fromtimestamp(
                    (datetime.now(tz) + timedelta(seconds=2)).timestamp(), tz
                )

        with mock.patch("harness.orchestration.workflow.reports.datetime", Later):
            resumed = coordinator.resume_dispatch(args)
        self.assertEqual(resumed["state"], "dispatched")
        self.assertEqual(
            self._batch_record(batch["batch_id"]).get("checkpoints", []), []
        )
        self.assertEqual(
            coordinator.self_report_dispatch(
                self._args(
                    dispatch=brief["dispatch_id"],
                    model="sonnet",
                    worktree=str(self.worktree),
                )
            )["state"],
            "working",
        )
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_send_rejects_wrong_selected_path_even_with_matching_sha_and_branch(
        self,
    ) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        _git(self.repo, "checkout", "--ignore-other-worktrees", self.branch)
        before = self._ledger_bytes()
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.send_dispatch(
                self._args(
                    dispatch=brief["dispatch_id"],
                    adapter=None,
                    adapter_arg=None,
                    checkout=None,
                    worktree=str(self.repo),
                )
            )
        self.assertEqual(before, self._ledger_bytes())
        sent = coordinator.send_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                adapter=None,
                adapter_arg=None,
                checkout=None,
                worktree=str(self.worktree),
            )
        )
        self.assertEqual(sent["worker_worktree"], str(self.worktree.resolve()))
        self.assertEqual(sent["worker_snapshot_commit"], brief["snapshot_commit"])

    def test_rewind_cannot_grant_unlimited_code_attempts(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        self._accepted_candidate(batch["batch_id"])
        config_path = self.repo / ".harness/orchestration.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        config["retry_policy"] = {"max_developer_retries": 0}
        config["access_policy"] = {}
        config_path.write_text(json.dumps(config))
        coordinator.rewind_batch(
            self._args(
                batch=batch["batch_id"],
                to="developer",
                note="repeat implementation",
                **self._approval(),
            )
        )
        with self.assertRaisesRegex(coordinator.CoordinatorError, "retry budget"):
            self._dispatch(batch["batch_id"], "developer")

    def test_live_worker_must_be_stopped_and_explicitly_retired_before_rewind(
        self,
    ) -> None:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        with self.assertRaisesRegex(coordinator.CoordinatorError, "still be running"):
            coordinator.rewind_batch(
                self._args(
                    batch=batch["batch_id"],
                    to="architect",
                    note="restart planning",
                    **self._approval(),
                )
            )
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.cancel_dispatch(
                self._args(
                    dispatch=brief["dispatch_id"],
                    reason="worker stopped",
                    runtime_stopped=False,
                    **self._approval(),
                )
            )
        coordinator.cancel_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                reason="operator stopped runtime",
                runtime_stopped=True,
                **self._approval(),
            )
        )
        coordinator.rewind_batch(
            self._args(
                batch=batch["batch_id"],
                to="architect",
                note="new plan",
                **self._approval(),
            )
        )
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_legacy_failed_abandon_is_superseded_without_rewriting_source(self) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        candidate = self._accepted_candidate(batch["batch_id"])
        source = self._batch_record(batch["batch_id"])
        # The former standalone command wrote failed + explicit abandon approval, no last_accepted.
        source["state"] = "failed"
        source["abandoned"] = {
            **self._approval(),
            "reason": "startup failure",
            "abandoned_at": self.APPROVED_AT,
            "open_dispatches": [],
        }
        source.setdefault("coordinator_decisions", []).append(
            {"decision": "abandon", **self._approval(), "note": "startup failure"}
        )
        ledger = LifecycleLedger(self.state_dir)
        with ledger.lock():
            ledger.replace_record(BatchRecord.from_dict(source))
        before = (
            self._records() / "batches" / f"{batch['batch_id']}.json"
        ).read_bytes()
        new = coordinator.create_batch(
            self._args(
                **self._batch_plan(), supersedes=batch["batch_id"], **self._approval()
            )
        )
        self.assertEqual(new["supersedes"]["start_commit"], candidate)
        self.assertEqual(
            before,
            (self._records() / "batches" / f"{batch['batch_id']}.json").read_bytes(),
        )
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.resume_stopped_batch(
                self._args(
                    batch=batch["batch_id"], note="resume refusal", **self._approval()
                )
            )
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_recovery_approvals_and_terminal_boundaries(self) -> None:
        batch, _ = self._failed_launch()
        coordinator.auto_decide(self._args(batch=batch["batch_id"]))
        for approval_fields in (
            {"approved_by": None, "approved_at": None},
            {"approved_by": "policy:auto", "approved_at": self.APPROVED_AT},
        ):
            before = self._ledger_bytes()
            with self.assertRaises(coordinator.CoordinatorError):
                coordinator.resume_stopped_batch(
                    self._args(batch=batch["batch_id"], note="fixed", **approval_fields)
                )
            self.assertEqual(before, self._ledger_bytes())
        coordinator.abandon_batch(
            self._args(
                batch=batch["batch_id"], reason="operator refused", **self._approval()
            )
        )
        for operation, extra in (
            (coordinator.resume_stopped_batch, {}),
            (coordinator.rewind_batch, {"to": "architect"}),
        ):
            with self.assertRaises(coordinator.CoordinatorError):
                operation(
                    self._args(
                        batch=batch["batch_id"],
                        note="invalid",
                        **extra,
                        **self._approval(),
                    )
                )

    def test_recovery_upgrades_control_runtime_with_audited_pin_and_preserves_plan(
        self,
    ) -> None:
        from harness.orchestration.workflow import runtime_pin

        batch, _ = self._failed_launch()
        coordinator.auto_decide(self._args(batch=batch["batch_id"]))
        old = self._batch_record(batch["batch_id"])
        package = self.repo / ".harness"
        workspace._store_runtime_snapshot(
            self.state_dir, workspace.MODULE_ROOT.parent, old["harness_runtime_sha256"]
        )
        # Install a new, real runtime tree, as the packager does. The marker changes its hash.
        import shutil

        shutil.copytree(
            workspace.MODULE_ROOT.parent,
            package,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (package / "orchestration" / "recovery_revision.py").write_text(
            "REVISION = 662\n"
        )
        result = coordinator.resume_stopped_batch(
            self._args(
                batch=batch["batch_id"],
                note="upgrade stopped control plane",
                **self._approval(),
            )
        )
        recovered = self._batch_record(batch["batch_id"])
        self.assertEqual(
            recovered["harness_runtime_sha256"], old["harness_runtime_sha256"]
        )
        self.assertEqual(
            result["event"]["evidence"]["runtime_upgrade"]["from"],
            old["harness_runtime_sha256"],
        )
        self.assertEqual(
            runtime_pin._pinned_hash(recovered),
            workspace._harness_runtime_sha256(self.repo),
        )
        workspace._validate_harness_runtime_snapshot(self.repo, recovered)
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_early_429_rejects_unknown_progress_and_dirty_files_without_evidence(
        self,
    ) -> None:
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(brief["dispatch_id"])
        (self.worktree / "unknown.txt").write_text("unregistered progress")
        before = self._ledger_bytes()
        with self.assertRaisesRegex(coordinator.CoordinatorError, "dirty"):
            coordinator.rate_limited_dispatch(
                self._args(dispatch=brief["dispatch_id"], retry_after_seconds=1)
            )
        self.assertEqual(before, self._ledger_bytes())
        _git(self.worktree, "add", "unknown.txt")
        _git(self.worktree, "commit", "-m", "feat: unregistered progress")
        before = self._ledger_bytes()
        with self.assertRaisesRegex(coordinator.CoordinatorError, "startup snapshot"):
            coordinator.rate_limited_dispatch(
                self._args(dispatch=brief["dispatch_id"], retry_after_seconds=1)
            )
        self.assertEqual(before, self._ledger_bytes())

    def test_recovered_429_requires_human_approval_and_spends_both_budgets(
        self,
    ) -> None:
        batch, _ = self._failed_launch()
        coordinator.auto_decide(self._args(batch=batch["batch_id"]))
        coordinator.resume_stopped_batch(
            self._args(
                batch=batch["batch_id"],
                note="fix worker directory",
                **self._approval(),
            )
        )
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        self._start(brief["dispatch_id"])
        coordinator.rate_limited_dispatch(
            self._args(dispatch=brief["dispatch_id"], retry_after_seconds=1)
        )
        before = self._ledger_bytes()
        coordinator.rate_limited_dispatch(
            self._args(dispatch=brief["dispatch_id"], retry_after_seconds=1)
        )
        self.assertEqual(before, self._ledger_bytes())
        args = self._args(
            dispatch=brief["dispatch_id"],
            termination_reason="429",
            trigger=None,
            measured_value=None,
            file=None,
            note=None,
            approved_by=None,
            approved_at=None,
        )

        class Later(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> Later:
                return cls.fromtimestamp(
                    (datetime.now(tz) + timedelta(seconds=2)).timestamp(), tz
                )

        with mock.patch("harness.orchestration.workflow.reports.datetime", Later):
            with self.assertRaises(coordinator.CoordinatorError):
                coordinator.resume_dispatch(args)
            for key, value in self._approval().items():
                setattr(args, key, value)
            result = coordinator.resume_dispatch(args)
        self.assertEqual(result["authorization"], "continue-rate-limit")
        from harness.orchestration.workflow.reports import _continuation_counts

        self.assertEqual(
            _continuation_counts(
                self._batch_record(batch["batch_id"]), brief["dispatch_id"]
            ),
            (1, 1),
        )
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.resume_dispatch(args)
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_rewind_before_first_accept_preserves_planning_approval(self) -> None:
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
        result = coordinator.rewind_batch(
            self._args(
                batch=batch["batch_id"],
                to="architect",
                note="repeat preparation",
                **self._approval(),
            )
        )
        self.assertEqual(result["state"], "planned")
        with self.assertRaises(coordinator.CoordinatorError):
            self._dispatch(batch["batch_id"], "architect")
        coordinator.approve_batch(
            self._args(batch=batch["batch_id"], **self._approval())
        )
        self._accepted_architect(batch["batch_id"])
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_no_implementation_completion_is_terminal_without_fake_publish(
        self,
    ) -> None:
        batch = self._create_batch()
        result = coordinator.mark_batch_not_required(
            self._args(
                batch=batch["batch_id"],
                reason="pinned snapshot meets the specification",
                **self._approval(),
            )
        )
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self._batch_record(batch["batch_id"])["dispatches"], [])
        for operation, extra in (
            (coordinator.resume_stopped_batch, {}),
            (coordinator.rewind_batch, {"to": "architect"}),
        ):
            with self.assertRaises(coordinator.CoordinatorError):
                operation(
                    self._args(
                        batch=batch["batch_id"],
                        note="invalid",
                        **extra,
                        **self._approval(),
                    )
                )
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_runtime_launch_failure_is_structured_and_restartable(self) -> None:
        batch, _ = self._failed_launch()
        coordinator.rewind_batch(
            self._args(
                batch=batch["batch_id"],
                to="architect",
                note="repeat planning",
                **self._approval(),
            )
        )
        path = self.repo / ".harness/orchestration.json"
        config = json.loads(path.read_text())
        config["assignment_plans"]["architect"]["transport"] = "external"
        path.write_text(json.dumps(config))
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        adapter = self.repo / "adapter-without-execute-permission"
        adapter.write_text("#!/bin/sh\nexit 0\n")
        adapter.chmod(0o600)
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.send_dispatch(
                self._args(
                    dispatch=brief["dispatch_id"],
                    adapter=str(adapter),
                    adapter_arg=None,
                    checkout=None,
                    worktree=str(self.worktree),
                )
            )
        status = coordinator._read_object(
            self._records() / "dispatch-status" / f"{brief['dispatch_id']}.json",
            "status",
        )
        self.assertEqual(status["runtime_failure"]["worker_started"], False)
        result = coordinator.resume_stopped_batch(
            self._args(
                batch=batch["batch_id"],
                note="fix adapter execute permission",
                **self._approval(),
            )
        )
        self.assertEqual(result["next_action"], "architect")
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def _use_external_developer(self) -> None:
        runtime = {"claude": {"profiles": ["p"], "model": "sonnet", "effort": "high"}}
        config_path = self.repo / ".harness/orchestration.json"
        config_path.write_text(
            json.dumps(
                {
                    "provider_profiles": {
                        "p": {
                            "capabilities": [
                                "architecture-analysis",
                                "backend-development",
                                "code-review",
                            ],
                            "fallback": [],
                            "known_limitations": ["none"],
                        }
                    },
                    "assignment_plans": {
                        "architect": {"zone": "repository", "runtimes": runtime},
                        "developer": {
                            "zone": "repository",
                            "runtimes": runtime,
                            "transport": "external",
                        },
                        "code-review": {"zone": "repository", "runtimes": runtime},
                    },
                    "backend_zones": {"repository": {"paths": ["**"]}},
                    "concurrency_budget": 1,
                    "verification_commands": ["true"],
                    "auto_retry_min_severity": "warning",
                }
            ),
            encoding="utf-8",
        )

    def test_external_developer_adapter_runs_from_the_registered_issue_worktree(
        self,
    ) -> None:
        self._use_external_developer()
        batch = self._create_batch()
        self.assertEqual(batch["auto_retry_min_severity"], "warning")
        self._accepted_architect(batch["batch_id"])
        brief = self._dispatch(batch["batch_id"], "developer")["brief"]
        marker = self.tmp / "adapter-cwd.txt"
        adapter = self.tmp / "cwd-adapter.py"
        adapter.write_text(
            "import os\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(os.getcwd(), encoding='utf-8')\n",
            encoding="utf-8",
        )

        result = coordinator.send_dispatch(
            self._args(
                dispatch=brief["dispatch_id"],
                adapter=str(adapter),
                adapter_arg=None,
                checkout=None,
                worktree=str(self.worktree),
            )
        )

        self.assertEqual(result["worker_worktree"], str(self.worktree.resolve()))
        self.assertEqual(
            marker.read_text(encoding="utf-8"), str(self.worktree.resolve())
        )

    def _dispatch_status(self, dispatch_id: str) -> JsonObject:
        status = coordinator.dispatch_status(
            self._args(dispatch=dispatch_id, batch=None, stale_after=None)
        )
        entry: JsonObject = status["dispatches"][0]
        return entry

    def test_issue_679_startup_failures_keep_an_explainable_batch_without_abandon(
        self,
    ) -> None:
        """Unsupported flag, unavailable self-report, then capacity before any code (#679)."""
        self._use_external_developer()
        batch = self._create_batch()
        self._accepted_architect(batch["batch_id"])
        launched = self.tmp / "adapter.launched"
        probed = self.tmp / "adapter.probed"
        adapter = self.tmp / "adapter.py"
        adapter.write_text(
            "import sys\nfrom pathlib import Path\n"
            "if '--help' in sys.argv:\n"
            f"    Path({str(probed)!r}).write_text('probed', encoding='utf-8')\n"
            "    print('usage: adapter dispatch [--profile PROFILE] --repo REPO --brief BRIEF')\n"
            "    raise SystemExit(0)\n"
            f"Path({str(launched)!r}).write_text('launched', encoding='utf-8')\n",
            encoding="utf-8",
        )

        def send(dispatch_id: str, *flags: str) -> JsonObject:
            return coordinator.send_dispatch(
                self._args(
                    dispatch=dispatch_id,
                    adapter=str(adapter),
                    adapter_arg=list(flags),
                    checkout=None,
                    worktree=str(self.worktree),
                )
            )

        # 1. An unsupported adapter flag is refused before the brief is handed off.
        first = self._dispatch(batch["batch_id"], "developer")["brief"]
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "--legacy-flag"
        ) as caught:
            send(first["dispatch_id"], "--legacy-flag")
        self.assertIn("nothing was handed off", caught.exception.remedy)
        self.assertFalse(launched.exists())
        self.assertEqual(
            self._dispatch_status(first["dispatch_id"])["state"], "approved"
        )

        # 2. The repaired launch starts a worker that never self-reports: it may still run.
        sent = send(first["dispatch_id"], "--profile=fast")
        self.assertEqual(sent["adapter_preflight"]["status"], "supported")
        self.assertTrue(launched.exists())
        startup = self._dispatch_status(first["dispatch_id"])["startup"]
        self.assertEqual(
            (
                startup["worker_state"],
                startup["classification"],
                startup["restart_allowed"],
            ),
            ("unknown", "model-self-report-missing", False),
        )
        coordinator.cancel_dispatch(
            self._args(
                dispatch=first["dispatch_id"],
                runtime_stopped=True,
                reason="runtime stopped: the model self-report never arrived",
                **self._approval(),
            )
        )
        self.assertEqual(
            self._batch_record(batch["batch_id"])["state"], "awaiting-approval"
        )
        # A send that fails its own checks never runs the adapter, not even to probe it.
        probed.unlink()
        with self.assertRaises(coordinator.CoordinatorError):
            send(first["dispatch_id"], "--profile=fast")
        self.assertFalse(probed.exists())

        # 3. Capacity after thread/start, before any code: evidence only, not READY, no abandon.
        second = self._dispatch(batch["batch_id"], "developer")["brief"]
        before = self._ledger_bytes()
        probe = coordinator.probe_dispatch_runtime(
            self._args(
                dispatch=second["dispatch_id"],
                runtime_command=sys.executable,
                runtime_arg=[
                    str(Path(__file__).with_name("fake_app_server.py")),
                    "--mode",
                    "capacity",
                ],
                worktree=None,
                timeout=20,
            )
        )
        self.assertEqual(before, self._ledger_bytes())
        self.assertEqual(
            (probe["status"], probe["ready"], probe["classification"]["category"]),
            ("failed", False, "capacity"),
        )
        self.assertNotEqual(self._batch_record(batch["batch_id"])["state"], "abandoned")
        self.assertEqual(
            self._dispatch_status(second["dispatch_id"])["state"], "approved"
        )
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def _batch_in_state(self, state: str) -> JsonObject:
        """Build a batch in `state` through public commands only."""
        if state == "paused":
            batch, _ = self._failed_launch()
            coordinator.auto_decide(self._args(batch=batch["batch_id"]))
            return batch
        if state == "planned":
            _git(
                self.repo,
                "worktree",
                "add",
                "-b",
                self.branch,
                str(self.worktree),
                "master",
            )
            return coordinator.create_batch(self._args(**self._batch_plan()))
        batch = self._create_batch()
        if state == "awaiting-approval":
            return batch
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        if state == "active":
            return batch
        self._submit(
            brief["dispatch_id"],
            self._base_report(
                brief,
                "architect",
                **(
                    {
                        "outcome": "blocked",
                        "blockers": {
                            "status": "present",
                            "details": "operator review required",
                        },
                    }
                    if state in {"blocked", "failed"}
                    else {}
                ),
            ),
        )
        if state in {"blocked", "failed"}:
            self._decide(batch["batch_id"], "block" if state == "blocked" else "fail")
        return batch

    def test_every_non_terminal_state_has_a_rewind_and_a_forward_path(self) -> None:
        for state in (
            "planned",
            "awaiting-approval",
            "active",
            "blocked",
            "failed",
            "paused",
        ):
            with self.subTest(state=state):
                batch = self._batch_in_state(state)
                batch_id = batch["batch_id"]
                self.assertEqual(self._batch_record(batch_id)["state"], state)
                before = self._ledger_bytes()
                # Forward: either resume-stop recovers the batch, or it refuses with a
                # remedy that names the next command; it never leaves the batch stuck.
                try:
                    coordinator.resume_stopped_batch(
                        self._args(batch=batch_id, note="forward", **self._approval())
                    )
                except coordinator.CoordinatorError as error:
                    self.assertTrue(
                        error.remedy, f"resume-stop from {state} has no remedy"
                    )
                    self.assertEqual(before, self._ledger_bytes())
                else:
                    self.assertTrue(coordinator.validate_ledger(self._args())["valid"])
                if state == "active":
                    # A live worker must be proven stopped before any recovery.
                    live = self._batch_record(batch_id)["dispatches"][-1]["dispatch_id"]
                    coordinator.cancel_dispatch(
                        self._args(
                            dispatch=live,
                            reason="operator stopped runtime",
                            runtime_stopped=True,
                            **self._approval(),
                        )
                    )
                # Backward: rewind to the first stage is reachable from every state.
                coordinator.rewind_batch(
                    self._args(
                        batch=batch_id, to="architect", note="back", **self._approval()
                    )
                )
                recovered = self._batch_record(batch_id)
                self.assertIn(recovered["state"], {"planned", "awaiting-approval"})
                self.assertTrue(coordinator.validate_ledger(self._args())["valid"])
            self.tearDown()
            self.setUp()

    def _blocked_architect(self, blockers: str) -> JsonObject:
        batch = self._create_batch()
        brief = self._dispatch(batch["batch_id"], "architect")["brief"]
        self._start(brief["dispatch_id"])
        self._submit(
            brief["dispatch_id"],
            self._base_report(
                brief,
                "architect",
                outcome="blocked",
                blockers={"status": "present", "details": blockers},
            ),
        )
        self._decide(batch["batch_id"], "block")
        self.assertEqual(self._batch_record(batch["batch_id"])["state"], "blocked")
        return batch

    def test_resume_stop_recognises_an_infrastructure_cause_in_the_blocked_report(
        self,
    ) -> None:
        batch = self._blocked_architect("worker session crashed: connection reset")
        result = coordinator.resume_stopped_batch(
            self._args(
                batch=batch["batch_id"], note="runtime restored", **self._approval()
            )
        )
        classification = result["event"]["evidence"]["failure"]["classification"]
        self.assertEqual(classification["method"], "heuristic")
        self.assertEqual(classification["category"], "network")
        self.assertEqual(result["state"], "awaiting-approval")
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_resume_stop_refuses_a_blocked_report_with_a_code_or_unknown_cause(
        self,
    ) -> None:
        for blockers in (
            "unit tests failed after the connection reset",
            "the plan is unclear",
        ):
            with self.subTest(blockers=blockers):
                batch = self._blocked_architect(blockers)
                before = self._ledger_bytes()
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "not confirmed"
                ):
                    coordinator.resume_stopped_batch(
                        self._args(
                            batch=batch["batch_id"], note="try", **self._approval()
                        )
                    )
                self.assertEqual(before, self._ledger_bytes())
            self.tearDown()
            self.setUp()

    def _blocked_developer(
        self, batch_id: str, **overrides: object
    ) -> tuple[JsonObject, str, list[str]]:
        """A developer whose work is complete in the report, but whose handoff failed."""
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
                blockers={
                    "status": "present",
                    "details": "runtime connection reset during the handoff to review",
                },
                **overrides,
            ),
        )
        return brief, candidate, changed

    def test_candidate_registration_after_a_rewind_keeps_the_ledger_readable(
        self,
    ) -> None:
        batch = self._create_batch()
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)
        coordinator.rewind_batch(
            self._args(
                batch=batch_id,
                to="architect",
                note="review architecture",
                **self._approval(),
            )
        )
        self._accepted_architect(batch_id)
        self._blocked_developer(batch_id)
        self._decide(
            batch_id, "retry", reason_category="verification-infrastructure"
        )
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])
        coordinator.rewind_batch(
            self._args(
                batch=batch_id,
                to="architect",
                note="supersede the registered candidate",
                **self._approval(),
            )
        )
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_conditional_accept_moves_a_complete_but_blocked_developer_to_review(
        self,
    ) -> None:
        batch = self._create_batch()
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)
        brief, candidate, changed = self._blocked_developer(batch_id)
        with self.assertRaisesRegex(coordinator.CoordinatorError, "non-completed"):
            self._decide(batch_id, "accept")
        for extra, message in (
            ({"infrastructure_failure": False, "note": "restored"}, "attest"),
            ({"infrastructure_failure": True, "note": "none"}, "recorded note"),
            (
                {
                    "infrastructure_failure": True,
                    "note": "restored",
                    "approved_by": "policy:auto",
                },
                "human approval",
            ),
        ):
            before = self._ledger_bytes()
            with self.assertRaisesRegex(coordinator.CoordinatorError, message):
                self._decide(batch_id, "accept-conditional", **extra)
            self.assertEqual(before, self._ledger_bytes())
        self._decide(
            batch_id,
            "accept-conditional",
            infrastructure_failure=True,
            note="runtime restored; commits match the plan",
        )
        stored = self._batch_record(batch_id)
        self.assertEqual(stored["next_action"], "risk-assessment")
        (condition,) = stored["conditions"]
        entry = next(
            e for e in stored["dispatches"] if e["dispatch_id"] == brief["dispatch_id"]
        )
        self.assertEqual(entry["decision"]["decision"], "accept-conditional")
        self.assertEqual(
            entry["decision"]["routing"],
            {
                "route": "accept-conditional",
                "stage": "developer",
                "candidate_commit": candidate,
            },
        )
        self.assertEqual(
            (condition["stage"], condition["status"], condition["candidate_commit"]),
            ("developer", "open", candidate),
        )
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])
        self._assess(batch_id, candidate, changed)
        # The pipeline moves forward: review is dispatched and accepted without the condition
        # blocking it, and the independent review closes the condition.
        self._reported_review(batch_id, candidate)
        self._decide(batch_id, "accept")
        closed = self._batch_record(batch_id)["conditions"][0]
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_conditional_accept_refuses_a_report_with_a_failed_check(self) -> None:
        batch = self._create_batch()
        batch_id = batch["batch_id"]
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
                blockers={"status": "present", "details": "the unit test failed"},
                checks_run=self._checks(brief, "fail"),
            ),
        )
        with self.assertRaisesRegex(coordinator.CoordinatorError, "failed check"):
            self._decide(
                batch_id,
                "accept-conditional",
                infrastructure_failure=True,
                note="restored",
            )

    def test_rewind_retires_the_condition_of_a_superseded_stage(self) -> None:
        batch = self._create_batch()
        batch_id = batch["batch_id"]
        self._accepted_architect(batch_id)
        self._blocked_developer(batch_id)
        self._decide(
            batch_id, "accept-conditional", infrastructure_failure=True, note="restored"
        )
        coordinator.rewind_batch(
            self._args(batch=batch_id, to="developer", note="redo", **self._approval())
        )
        from harness.orchestration.workflow import conditions

        self.assertEqual(conditions.open_conditions(self._batch_record(batch_id)), [])
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """One public coordinator command in its own process, as an operator or a worker runs it."""
        return subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATION_ROOT / "coordinator.py"),
                "--repo",
                str(self.repo),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def _cli_rewind(self, batch_id: str, target: str) -> JsonObject:
        result = self._cli(
            "batch",
            "rewind",
            "--batch",
            batch_id,
            "--to",
            target,
            "--approved-by",
            "Malove",
            "--approved-at",
            self.APPROVED_AT,
            "--note",
            "rework through a fresh writer",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(JsonObject, json.loads(result.stdout))

    def _cli_assess(
        self,
        batch_id: str,
        candidate: str,
        changed: list[str],
        *triggers: str,
    ) -> subprocess.CompletedProcess[str]:
        flags = [item for path in changed for item in ("--changed-file", path)]
        trigger_flags = [
            item
            for trigger in (triggers or ("transactions",))
            for item in ("--developer-trigger", trigger)
        ]
        return self._cli(
            "risk",
            "assess",
            "--batch",
            batch_id,
            "--candidate-commit",
            candidate,
            *flags,
            *trigger_flags,
        )

    def _changed_for(self, batch_id: str, candidate: str) -> list[str]:
        base = self._batch_record(batch_id)["base_commit"]
        return git_utils._changed_files_between(self.repo, base, candidate)

    def _accepted_review_for(self, batch_id: str, name: str) -> tuple[str, list[str]]:
        """A review-required candidate with an accepted composite review: QA is the next handoff."""
        self._accepted_architect(batch_id)
        candidate = self._accepted_candidate(batch_id, name)
        self._reported_review(batch_id, candidate)
        self._decide(batch_id, "accept")
        self.assertEqual(self._batch_record(batch_id)["next_action"], "qa")
        return candidate, self._changed_for(batch_id, candidate)

    def _evidence_bytes(self) -> dict[Path, bytes]:
        return {
            path: path.read_bytes()
            for folder in ("risk-assessments", "reports", "dispatches")
            for path in (self._records() / folder).glob("*.json")
        }

    def _cli_propose_qa(
        self, batch_id: str, candidate: str
    ) -> subprocess.CompletedProcess[str]:
        return self._cli(
            "dispatch",
            "propose",
            "--batch",
            batch_id,
            "--role",
            "qa",
            "--candidate-commit",
            candidate,
            "--model",
            "sonnet",
            "--effort",
            "high",
        )

    def _rewound_writer_report(
        self, batch_id: str, name: str, **overrides: object
    ) -> tuple[str, list[str], JsonObject]:
        """A fresh writer after a rewind: its report is submitted but not yet accepted."""
        brief = self._dispatch(batch_id, "developer")["brief"]
        self._start(brief["dispatch_id"])
        candidate, changed = self._developer_commit(name)
        base = self._batch_record(batch_id)["base_commit"]
        commits = _git(
            self.worktree, "rev-list", "--reverse", f"{base}..{candidate}"
        ).split()
        commit_map = [
            {"commit_sha": sha, "plan_entry_id": entry["id"]}
            for sha in commits
            for entry in brief["commit_plan"]
        ]
        self._submit(
            brief["dispatch_id"],
            self._developer_report(
                brief,
                candidate,
                changed,
                commit_map=commit_map,
                dod_coverage=[{"dod_item": 1, "commits": [candidate]}],
                divergence_justification="the rewound writer adds a second commit",
                **overrides,
            ),
        )
        return candidate, changed, brief

    def test_rewind_to_the_writer_binds_the_reassessment_until_the_new_candidate(
        self,
    ) -> None:
        batch_id = self._create_batch()["batch_id"]
        self._accepted_architect(batch_id)
        self._accepted_candidate(batch_id, "first")
        self._cli_rewind(batch_id, "developer")
        pending = self._batch_record(batch_id)
        self.assertIs(pending["risk_reassessment_required"], True)
        self.assertIs(pending["risk_reassessment_pending_next"], True)
        self.assertNotIn("risk_reassessment_candidate", pending)
        second, changed, _ = self._rewound_writer_report(batch_id, "second")
        bound = self._batch_record(batch_id)
        self.assertIs(bound["risk_reassessment_required"], True)
        self.assertEqual(bound["risk_reassessment_candidate"], second)
        self.assertNotIn("risk_reassessment_pending_next", bound)
        self._decide(batch_id, "accept")
        blocked = self._cli_propose_qa(batch_id, second)
        self.assertEqual(blocked.returncode, 2, blocked.stdout)
        assessed = self._cli_assess(batch_id, second, changed)
        self.assertEqual(assessed.returncode, 0, assessed.stderr)
        after = self._batch_record(batch_id)
        self.assertIs(after["risk_reassessment_required"], False)
        self.assertEqual(after["next_action"], "code-review")
        self._reported_review(batch_id, second)
        self._decide(batch_id, "accept")
        proposal = self._cli_propose_qa(batch_id, second)
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def _cli_accept_conditional(self, batch_id: str) -> None:
        result = self._cli(
            "batch",
            "decide",
            "--batch",
            batch_id,
            "--decision",
            "accept-conditional",
            "--infrastructure-failure",
            "--approved-by",
            "Malove",
            "--approved-at",
            self.APPROVED_AT,
            "--note",
            "runtime restored; commits match the plan",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_blocked_writer_after_a_rewind_binds_its_candidate_through_qa(
        self,
    ) -> None:
        batch_id = self._create_batch()["batch_id"]
        self._accepted_architect(batch_id)
        self._accepted_candidate(batch_id, "first")
        self._cli_rewind(batch_id, "developer")
        second, changed, _ = self._rewound_writer_report(
            batch_id,
            "second",
            outcome="blocked",
            blockers={
                "status": "present",
                "details": "runtime connection reset during the handoff to review",
            },
        )
        bound = self._batch_record(batch_id)
        self.assertIs(bound["risk_reassessment_required"], True)
        self.assertEqual(bound["risk_reassessment_candidate"], second)
        self.assertNotIn("risk_reassessment_pending_next", bound)
        self._cli_accept_conditional(batch_id)
        self.assertEqual(self._batch_record(batch_id)["next_action"], "risk-assessment")
        assessed = self._cli_assess(batch_id, second, changed)
        self.assertEqual(assessed.returncode, 0, assessed.stderr)
        after = self._batch_record(batch_id)
        self.assertIs(after["risk_reassessment_required"], False)
        self.assertEqual(after["next_action"], "code-review")
        self._reported_review(batch_id, second)
        self._decide(batch_id, "accept")
        proposal = self._cli_propose_qa(batch_id, second)
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_repeated_assessment_after_an_accepted_review_keeps_qa_reachable(
        self,
    ) -> None:
        batch_id = self._create_batch()["batch_id"]
        candidate, changed = self._accepted_review_for(batch_id, "stable")
        evidence = self._evidence_bytes()
        for _ in range(2):
            replay = self._cli_assess(batch_id, candidate, changed)
            self.assertEqual(replay.returncode, 0, replay.stderr)
            stored = self._batch_record(batch_id)
            self.assertEqual(stored["next_action"], "qa")
            self.assertIs(stored["risk_reassessment_required"], False)
        # The replays only append risk records: every earlier record is untouched.
        after = self._evidence_bytes()
        for path, content in evidence.items():
            self.assertEqual(after[path], content, path.name)
        proposal = self._cli_propose_qa(batch_id, candidate)
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_an_assessment_matching_a_trigger_the_review_never_covered_returns_to_review(
        self,
    ) -> None:
        batch_id = self._create_batch()["batch_id"]
        candidate, changed = self._accepted_review_for(batch_id, "wider")
        wider = self._cli_assess(
            batch_id, candidate, changed, "transactions", "concurrency-retry"
        )
        self.assertEqual(wider.returncode, 0, wider.stderr)
        self.assertEqual(self._batch_record(batch_id)["next_action"], "code-review")

    def test_qa_refusal_names_the_awaited_candidate_and_writes_nothing(self) -> None:
        batch_id = self._create_batch()["batch_id"]
        self._accepted_architect(batch_id)
        self._accepted_candidate(batch_id, "first")
        self._cli_rewind(batch_id, "developer")
        second, _, _ = self._rewound_writer_report(batch_id, "second")
        self._decide(batch_id, "accept")
        before = self._ledger_bytes()
        blocked = self._cli_propose_qa(batch_id, second)
        self.assertEqual(blocked.returncode, 2, blocked.stdout)
        remedy = blocked.stderr.partition("REMEDY:")[2]
        self.assertIn(second, remedy)
        self.assertEqual(before, self._ledger_bytes())

    def test_qa_refusal_while_the_writer_is_awaited_says_to_wait_for_its_report(
        self,
    ) -> None:
        batch_id = self._create_batch()["batch_id"]
        self._accepted_architect(batch_id)
        self._accepted_candidate(batch_id, "first")
        self._cli_rewind(batch_id, "developer")
        before = self._ledger_bytes()
        blocked = self._cli_propose_qa(
            batch_id, _git(self.worktree, "rev-parse", "HEAD")
        )
        self.assertEqual(blocked.returncode, 2, blocked.stdout)
        self.assertIn(
            "wait for the writer report", blocked.stderr.partition("REMEDY:")[2]
        )
        self.assertEqual(before, self._ledger_bytes())

    def test_a_second_rewind_keeps_the_pending_requirement(self) -> None:
        batch_id = self._create_batch()["batch_id"]
        self._accepted_architect(batch_id)
        self._accepted_candidate(batch_id, "first")
        self._cli_rewind(batch_id, "developer")
        self._cli_rewind(batch_id, "architect")
        stored = self._batch_record(batch_id)
        self.assertIs(stored["risk_reassessment_required"], True)
        self.assertIs(stored["risk_reassessment_pending_next"], True)
        self.assertNotIn("risk_reassessment_candidate", stored)
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_rewind_to_review_clears_the_requirement_and_keeps_history(self) -> None:
        batch_id = self._create_batch()["batch_id"]
        candidate, _ = self._accepted_review_for(batch_id, "reviewed")
        evidence = self._evidence_bytes()
        self._cli_rewind(batch_id, "code-review")
        stored = self._batch_record(batch_id)
        self.assertIs(stored["risk_reassessment_required"], False)
        self.assertNotIn("risk_reassessment_pending_next", stored)
        for path, content in evidence.items():
            self.assertEqual(path.read_bytes(), content, path.name)
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def _registered_candidate_after_verification(self, batch_id: str) -> tuple[str, list[str]]:
        """A blocked developer candidate retried as verification-infrastructure, then verified,
        accepted and risk-assessed: the registered candidate carries an active risk assessment."""
        self._accepted_architect(batch_id)
        _, candidate, changed = self._blocked_developer(batch_id)
        self._decide(batch_id, "retry", reason_category="verification-infrastructure")
        verification = self._dispatch(batch_id, "verification", candidate=candidate)[
            "brief"
        ]
        self._start(verification["dispatch_id"], checkout=self.worktree)
        self._submit(
            verification["dispatch_id"], self._base_report(verification, "verification")
        )
        self._decide(batch_id, "accept")
        self._assess(batch_id, candidate, changed)
        return candidate, changed

    def test_rewind_to_verification_binds_the_registered_candidate_through_qa(
        self,
    ) -> None:
        batch_id = self._create_batch()["batch_id"]
        candidate, changed = self._registered_candidate_after_verification(batch_id)
        evidence = self._evidence_bytes()
        self._cli_rewind(batch_id, "verification")
        stored = self._batch_record(batch_id)
        self.assertIs(stored["risk_reassessment_required"], True)
        self.assertEqual(stored["risk_reassessment_candidate"], candidate)
        self.assertNotIn("risk_reassessment_pending_next", stored)
        # The superseded assessments and reports stay readable and unmodified.
        for path, content in evidence.items():
            self.assertEqual(path.read_bytes(), content, path.name)
        before = self._ledger_bytes()
        blocked = self._cli_propose_qa(batch_id, candidate)
        self.assertEqual(blocked.returncode, 2, blocked.stdout)
        remedy = blocked.stderr.partition("REMEDY:")[2]
        self.assertIn(candidate, remedy)
        # The awaited step is the verification dispatch and its accept, not a writer report.
        self.assertIn("verification", remedy)
        self.assertNotIn("writer report", remedy)
        self.assertEqual(before, self._ledger_bytes())
        verification = self._dispatch(batch_id, "verification", candidate=candidate)[
            "brief"
        ]
        self._start(verification["dispatch_id"], checkout=self.worktree)
        self._submit(
            verification["dispatch_id"], self._base_report(verification, "verification")
        )
        self._decide(batch_id, "accept")
        assessed = self._cli_assess(batch_id, candidate, changed)
        self.assertEqual(assessed.returncode, 0, assessed.stderr)
        after = self._batch_record(batch_id)
        self.assertIs(after["risk_reassessment_required"], False)
        self.assertEqual(after["next_action"], "code-review")
        self._reported_review(batch_id, candidate)
        self._decide(batch_id, "accept")
        proposal = self._cli_propose_qa(batch_id, candidate)
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        self.assertTrue(coordinator.validate_ledger(self._args())["valid"])

    def test_an_illegal_reassessment_state_is_refused_before_any_ledger_write(
        self,
    ) -> None:
        batch_id = self._create_batch()["batch_id"]
        self._accepted_architect(batch_id)
        self._accepted_candidate(batch_id, "first")
        before = self._ledger_bytes()

        def illegal(batch: JsonObject, state: object) -> None:
            batch["risk_reassessment_required"] = True

        with (
            mock.patch.object(risk_policy, "apply_reassessment", illegal),
            self.assertRaisesRegex(
                coordinator.CoordinatorError, "illegal risk reassessment"
            ),
        ):
            coordinator.rewind_batch(
                self._args(
                    batch=batch_id,
                    to="developer",
                    note="return to writer",
                    **self._approval(),
                )
            )
        self.assertEqual(before, self._ledger_bytes())
