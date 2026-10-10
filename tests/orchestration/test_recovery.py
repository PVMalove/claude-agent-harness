"""Operator recovery through the public coordinator, with real Git and ledger evidence."""

from __future__ import annotations
import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta, tzinfo
from unittest import mock
from pathlib import Path
from harness.orchestration import coordinator
from harness.orchestration.core import git_utils, workspace
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger import ledger_ops
from harness.orchestration.ledger.lifecycle import LifecycleLedger, BatchRecord
from harness.orchestration.workflow import carried_items, commit_plan
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
        with self.assertRaisesRegex(coordinator.CoordinatorError, "budget exhausted"):
            coordinator.resume_stopped_batch(
                self._args(
                    batch=batch["batch_id"],
                    note="third launch failure",
                    **self._approval(),
                )
            )
        self.assertEqual(before, self._ledger_bytes())
        self.assertEqual(original, self._batch_record(batch["batch_id"])["auto_stop"])
        coordinator.validate_ledger(self._args())

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
                        blockers="operator review required",
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
                batch=batch["batch_id"], note="fixed checkout", **self._approval()
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
                    "risks": "none",
                    "blockers": "none",
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
                batch=batch["batch_id"], note="fix worker directory", **self._approval()
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
