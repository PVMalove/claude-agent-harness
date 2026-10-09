#!/usr/bin/env python3
"""QA preparation, gate and failure routing end to end (issue #618).

A real Git repository, a real ledger and real preparation and gate commands: ``qa run`` executes
them in a clean-room checkout of the candidate, writes the report, and the coordinator decides the
retry. Only the environment is varied (a marker file that makes the "registry" unreachable), never
a prepared report or a replaced route.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from harness.orchestration import coordinator, qa_lane
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import decisions
from tests.orchestration import test_coordinator as coordinator_tests

INHERIT = {"defaults": {"mode": "inherit"}}
LOCK_SYNC_ERROR = (
    "npm ERR! `npm ci` can only install packages when your package.json and "
    "package-lock.json are in sync"
)
OUTAGE_ERROR = "npm ERR! network request failed: getaddrinfo EAI_AGAIN registry.example"


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments], check=True, capture_output=True
    )


class PreparationFixture(unittest.TestCase):
    """A reviewed candidate whose project tracks manifest and lock files, QA not yet run."""

    def setUp(self) -> None:
        self.fx = coordinator_tests.CoordinatorRetryRoutingTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.repo = self.fx.repo
        (self.repo / "package.json").write_text("{}\n", encoding="utf-8")
        (self.repo / "package-lock.json").write_text("{}\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "project files")
        _git(self.repo, "push", "origin", "master")
        self.scripts = self.fx.tmp / "scripts"
        self.scripts.mkdir()
        # Environment switches live outside the checkout, so a QA run cannot see them as code.
        self.registry_down = self.fx.tmp / "registry-down"
        self.lock_broken = self.fx.tmp / "lock-broken"
        self.gate_failing = self.fx.tmp / "gate-failing"
        self.gate_ran = self.fx.tmp / "gate-ran"
        self.prepared_marker = "prepared.flag"
        prepare = self._script(
            "prepare",
            f"""
            import os, sys
            if os.path.exists({str(self.registry_down)!r}):
                print({OUTAGE_ERROR!r}, file=sys.stderr)
                sys.exit(1)
            if os.path.exists({str(self.lock_broken)!r}):
                print({LOCK_SYNC_ERROR!r}, file=sys.stderr)
                sys.exit(1)
            open({self.prepared_marker!r}, "w").write("ready")
            """,
        )
        gate_one = self._script(
            "gate_one",
            f"""
            import os, sys
            open({str(self.gate_ran)!r}, "a").write("one\\n")
            print("prepared:", os.path.exists({self.prepared_marker!r}))
            sys.exit(2 if os.path.exists({str(self.gate_failing)!r}) else 0)
            """,
        )
        gate_two = self._script("gate_two", "print('second check')")
        # Independent facts: a reachability probe and an offline project-file check.
        self.probe = self._script(
            "probe",
            f"""
            import os, sys
            sys.exit(6 if os.path.exists({str(self.registry_down)!r}) else 0)
            """,
        )
        self.file_check = self._script(
            "file_check",
            f"""
            import os, sys
            sys.exit(1 if os.path.exists({str(self.lock_broken)!r}) else 0)
            """,
        )
        self.prepare, self.gate = prepare, [gate_one, gate_two]
        # The batch plan pins the verification commands, so the gate is declared before the batch.
        (self.repo / ".harness/project.json").write_text(
            json.dumps({"qa_gate_commands": self.gate}), encoding="utf-8"
        )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "declare the QA gate")
        _git(self.repo, "push", "origin", "master")
        # The QA lane is repository-scoped: its ledger is the repository's own state directory.
        self.fx.state_dir = self.repo / coordinator.STATE_REL
        self.batch_id = self.fx._create_batch()["batch_id"]
        self.fx._accepted_architect(self.batch_id)
        self.candidate = self.fx._accepted_candidate(self.batch_id)
        self.fx._reported_review(self.batch_id, self.candidate)
        self.fx._decide(self.batch_id, "accept")

    def _script(self, name: str, body: str) -> str:
        path = self.scripts / f"{name}.py"
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        # A bare `python` is always the launcher the gate runner pins; `python.exe` is not.
        return f'python "{path}"'

    def configure(self, *, preparation: list[str] | None, facts: bool = True) -> None:
        config: JsonObject = {
            "access_policy": INHERIT,
            "verification_commands": self.gate,
        }
        if preparation is not None:
            config["qa_preparation"] = preparation
            if facts:
                config["qa_environment_probes"] = [self.probe]
                config["qa_project_file_checks"] = [self.file_check]
        (self.repo / ".harness/orchestration.json").write_text(
            json.dumps(config), encoding="utf-8"
        )

    def qa_brief(self) -> JsonObject:
        created: JsonObject = self.fx._dispatch(
            self.batch_id, "qa", candidate=self.candidate
        )
        brief: JsonObject = created["brief"]
        return brief

    def run_qa(self, brief: JsonObject) -> JsonObject:
        return coordinator.run_qa(
            argparse.Namespace(
                repo=str(self.repo),
                state_dir=None,
                dispatch=brief["dispatch_id"],
                lease_seconds=None,
            )
        )

    def report_of(self, result: JsonObject) -> JsonObject:
        report: JsonObject = json.loads(
            Path(result["report"]).read_text(encoding="utf-8")
        )
        return report

    def assert_lane_free_and_dispatch(self, brief: JsonObject, state: str) -> None:
        lane = coordinator.qa_status(
            argparse.Namespace(repo=str(self.repo), state_dir=None)
        )
        self.assertEqual((lane["lease"], lane["queue"]), (None, []))
        status = coordinator._load_dispatch_status(
            self.repo / coordinator.STATE_REL, brief["dispatch_id"]
        )
        self.assertEqual(status["state"], state)

    def developer_retries(self) -> int:
        return decisions._developer_retry_count(self.fx._batch_record(self.batch_id))


class QaPreparationE2ETests(PreparationFixture):
    def test_an_infrastructure_failure_blocks_then_reruns_the_same_sha_for_free(
        self,
    ) -> None:
        self.configure(preparation=[self.prepare])
        first = self.qa_brief()
        self.registry_down.write_text("down", encoding="utf-8")

        result = self.run_qa(first)

        self.assertEqual(result["state"], "reported")
        report = self.report_of(result)
        self.assertEqual(report["outcome"], "blocked")
        stages = report["qa_stages"]
        self.assertEqual(stages["failed_stage"], "preparation")
        self.assertEqual(stages["code_checks_started"], "not_started")
        self.assertEqual(stages["diagnosis"]["category"], "infrastructure")
        stage, probe, check = stages["stages"]
        self.assertEqual(
            [(probe["stage"], probe["result"]), (check["stage"], check["result"])],
            [("environment-probe", "fail"), ("project-file-check", "pass")],
        )
        self.assertEqual(stage["command"], self.prepare)
        self.assertEqual(stage["exit_code"], 1)
        self.assertIn("EAI_AGAIN", stage["diagnostics"])
        # No fabricated failed code check: every gate command is a command never reached.
        self.assertEqual(
            [(c["command"], c["result"]) for c in report["checks_run"]],
            [(command, "not-run") for command in self.gate],
        )
        self.assertIn("code was not verified", report["output"])
        self.assertFalse(self.gate_ran.exists(), "the gate must not start")
        artifact = Path(result["artifact"]).read_text(encoding="utf-8")
        # The artifact shows what ran; the stage keeps the approved command and names what ran.
        self.assertIn(f"$ {stage['executed_command']}", artifact)
        self.assertIn("EAI_AGAIN", artifact)
        self.assert_lane_free_and_dispatch(first, "reported")

        # The coordinator confirms the environment ready and decides: same SHA, no developer budget.
        decided = self.fx._decide(self.batch_id, "retry")
        routing = decided["coordinator_decisions"][-1]["routing"]
        self.assertEqual(
            (
                routing["route"],
                routing["reason_category"],
                routing["next_role"],
                routing["candidate_commit"],
            ),
            (
                "same-candidate-rerun",
                "verification-infrastructure",
                "qa",
                self.candidate,
            ),
        )
        self.assertEqual(self.developer_retries(), 0)
        self.registry_down.unlink()
        second = self.qa_brief()
        self.assertNotEqual(second["dispatch_id"], first["dispatch_id"])
        for field in (
            "candidate_commit",
            "verification_commands",
            "write_paths",
            "review_scope",
            "runtime_access",
            "branch",
            "worktree",
        ):
            self.assertEqual(second[field], first[field], field)
        self.assertTrue(second["coordinator_approval"])

        rerun_result = self.run_qa(second)
        rerun = self.report_of(rerun_result)

        self.assertEqual(rerun["outcome"], "completed")
        self.assertEqual(rerun["qa_stages"]["code_checks_started"], "started")
        self.assertEqual(
            [stage["stage"] for stage in rerun["qa_stages"]["stages"]],
            ["preparation", "gate", "gate"],
        )
        self.assertTrue(self.gate_ran.exists())
        self.assertIn(
            "prepared: True",
            Path(rerun_result["artifact"]).read_text(encoding="utf-8"),
        )
        # The first dispatch, its report and its decision stay as audit evidence.
        entries = {
            item["dispatch_id"]: item
            for item in self.fx._batch_record(self.batch_id)["dispatches"]
        }
        self.assertEqual(entries[first["dispatch_id"]]["state"], "reported")
        self.assertEqual(entries[first["dispatch_id"]]["decision"]["decision"], "retry")
        self.assertTrue(Path(result["report"]).is_file())
        self.assertEqual(self.developer_retries(), 0)
        accepted = self.fx._decide(self.batch_id, "accept")
        self.assertEqual(accepted["coordinator_decisions"][-1]["decision"], "accept")

    def test_a_confirmed_project_defect_goes_to_the_developer(self) -> None:
        self.configure(preparation=[self.prepare])
        brief = self.qa_brief()
        self.lock_broken.write_text("broken", encoding="utf-8")

        report = self.report_of(self.run_qa(brief))

        self.assertEqual(report["outcome"], "failed")
        diagnosis = report["qa_stages"]["diagnosis"]
        self.assertEqual(diagnosis["category"], "project-defect")
        self.assertIn(
            f"project-file-check-failed:{self.file_check}", diagnosis["signals"]
        )
        self.assertEqual(report["qa_stages"]["code_checks_started"], "not_started")
        self.assertFalse(self.gate_ran.exists())
        decided = self.fx._decide(self.batch_id, "retry")
        routing = decided["coordinator_decisions"][-1]["routing"]
        self.assertEqual(
            (routing["route"], routing["reason_category"], routing["next_role"]),
            ("developer-retry", "code", "developer"),
        )
        self.assertEqual(self.developer_retries(), 1)

    def test_an_outage_log_without_independent_facts_waits_for_triage(self) -> None:
        # The log names an outage, but no probe or project-file check confirms it.
        self.configure(preparation=[self.prepare], facts=False)
        brief = self.qa_brief()
        self.registry_down.write_text("down", encoding="utf-8")

        report = self.report_of(self.run_qa(brief))

        self.assertEqual(report["outcome"], "blocked")
        self.assertEqual(report["qa_stages"]["diagnosis"]["category"], "unknown")
        with self.assertRaises(CoordinatorError) as refused:
            self.fx._decide(self.batch_id, "retry")
        self.assertIn("needs triage", refused.exception.message)
        self.assertEqual(self.developer_retries(), 0)

    def test_an_unknown_cause_waits_for_triage_and_retries_nothing_by_itself(
        self,
    ) -> None:
        unknown = self._script("unknown", "import sys; print('boom'); sys.exit(3)")
        self.configure(preparation=[unknown])
        brief = self.qa_brief()

        report = self.report_of(self.run_qa(brief))

        self.assertEqual(report["outcome"], "blocked")
        self.assertEqual(report["qa_stages"]["diagnosis"]["category"], "unknown")
        self.assertIn("triage", report["blockers"])
        before = self.fx._batch_record(self.batch_id)
        with self.assertRaises(CoordinatorError) as refused:
            self.fx._decide(self.batch_id, "retry")
        self.assertIn("needs triage", refused.exception.message)
        self.assertEqual(self.fx._batch_record(self.batch_id), before)
        self.assertEqual(self.developer_retries(), 0)
        self.assertEqual(
            len(before["dispatches"]),
            len(self.fx._batch_record(self.batch_id)["dispatches"]),
        )
        self.assert_lane_free_and_dispatch(brief, "reported")

    def test_a_gate_failure_after_a_green_preparation_keeps_its_findings(self) -> None:
        self.configure(preparation=[self.prepare])
        brief = self.qa_brief()
        self.gate_failing.write_text("failing", encoding="utf-8")

        result = self.run_qa(brief)

        report = self.report_of(result)
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(report["qa_stages"]["failed_stage"], "gate")
        self.assertEqual(report["qa_stages"]["code_checks_started"], "started")
        self.assertNotIn("diagnosis", report["qa_stages"])
        self.assertEqual(
            [c["result"] for c in report["checks_run"]], ["fail", "not-run"]
        )
        decided = self.fx._decide(
            self.batch_id, "retry", reason_category="verification-infrastructure"
        )
        routing = decided["coordinator_decisions"][-1]["routing"]
        self.assertEqual(
            (routing["route"], routing["reason_category"]),
            ("developer-retry", "code"),
        )

    def test_a_verification_only_configuration_keeps_its_report_shape(self) -> None:
        self.configure(preparation=None)
        brief = self.qa_brief()

        report = self.report_of(self.run_qa(brief))

        self.assertEqual(report["outcome"], "completed")
        self.assertNotIn("qa_stages", report)
        self.assertEqual([c["result"] for c in report["checks_run"]], ["pass", "pass"])

    def test_a_report_failure_after_preparation_leaves_the_lane_free_for_a_rerun(
        self,
    ) -> None:
        self.configure(preparation=[self.prepare])
        brief = self.qa_brief()
        self.registry_down.write_text("down", encoding="utf-8")
        broken = coordinator.CoordinatorError("report disk full", remedy="free space")

        with (
            mock.patch.object(qa_lane, "_record_report", side_effect=broken),
            self.assertRaises(coordinator.CoordinatorError),
        ):
            self.run_qa(brief)

        self.assert_lane_free_and_dispatch(brief, "approved")
        self.registry_down.unlink()
        # No manual ledger edit and no new batch: the same approved dispatch runs again.
        report = self.report_of(self.run_qa(brief))
        self.assertEqual(report["outcome"], "completed")
        self.assert_lane_free_and_dispatch(brief, "reported")


if __name__ == "__main__":
    unittest.main()
