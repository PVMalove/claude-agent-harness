#!/usr/bin/env python3
"""Regression tests for delivery_stats.py's migration onto LifecycleLedger's lenient read API
(issue #197): one corrupted ledger record must degrade only the metric that depends on it, never
abort the whole report."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness.errors import HarnessError
from harness.orchestration.ledger import LifecycleLedger
from harness.reporting import delivery_stats

ORCHESTRATION_ROOT = Path(__file__).resolve().parents[1] / "harness" / "orchestration"


def _install_ledger_source(repo: Path) -> None:
    """Copy this repository's own harness/orchestration/ledger.py into <repo>/.harness/orchestration/,
    mirroring a real deployed project that has the optional backend-orchestration capability
    installed. delivery_stats.py resolves ledger.py relative to the analyzed --repo, not relative
    to its own installation, so the fixture has to look like a real deployment, not just call
    LifecycleLedger directly."""
    destination = repo / ".harness" / "orchestration"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy(ORCHESTRATION_ROOT / "ledger.py", destination / "ledger.py")


class OrchestrationMetricsLenientReadTests(unittest.TestCase):
    """Builds real ledger state via LifecycleLedger.ensure()/write_immutable(), then hand-corrupts
    one dispatch record file on disk to prove the lenient read API contains the damage."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _install_ledger_source(self.repo)
        self.state_dir = self.tmp / "state"

        self.ledger = LifecycleLedger(self.state_dir)
        self.ledger.ensure()
        generation = self.ledger.records_root()

        self.batch_id = "batch-197-1"
        self.ledger.write_immutable(generation / "plans" / f"{self.batch_id}.json", {"batch_id": self.batch_id})
        self.ledger.write_immutable(generation / "batches" / f"{self.batch_id}.json", {
            "batch_id": self.batch_id,
            "state": "active",
            "branch": "feature/issue-197-lenient-read-api",
            "dispatches": [
                {"dispatch_id": "dispatch-dev-1", "role": "developer"},
                {"dispatch_id": "dispatch-qa-1", "role": "qa", "decision": {"decision": "accept"}},
                {"dispatch_id": "dispatch-cr-1", "role": "code-review"},
            ],
        })
        self.ledger.write_immutable(generation / "dispatches" / "dispatch-dev-1.json", {
            "dispatch_id": "dispatch-dev-1", "write_paths": ["harness/**"],
        })
        self.cr_record_path = generation / "dispatches" / "dispatch-cr-1.json"
        self.ledger.write_immutable(self.cr_record_path, {
            "dispatch_id": "dispatch-cr-1",
            "review_scope": ["harness/orchestration/ledger.py", "outside/scope/file.py"],
        })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_corrupted_code_review_record_degrades_only_review_scope_to_missing(self) -> None:
        # Simulate a torn write: truncated bytes, not valid JSON.
        self.cr_record_path.write_text('{"review_sc', encoding="utf-8")

        report = delivery_stats.orchestration_metrics(self.repo, {197}, self.state_dir)

        self.assertEqual(report["status"], "ok")
        ticket = report["tickets"]["197"]
        self.assertEqual(len(ticket["worker_sessions"]), 3)
        self.assertEqual(ticket["qa_decided"], 1)
        self.assertEqual(ticket["qa_failed"], 0)
        self.assertEqual(ticket["qa_failure_rate"], 0.0)
        self.assertEqual(ticket["review_scope"], delivery_stats.MISSING)

    def test_missing_code_review_record_degrades_only_review_scope_to_missing(self) -> None:
        self.cr_record_path.unlink()

        report = delivery_stats.orchestration_metrics(self.repo, {197}, self.state_dir)

        self.assertEqual(report["status"], "ok")
        ticket = report["tickets"]["197"]
        self.assertEqual(len(ticket["worker_sessions"]), 3)
        self.assertEqual(ticket["qa_failure_rate"], 0.0)
        self.assertEqual(ticket["review_scope"], delivery_stats.MISSING)

    def test_uncorrupted_state_yields_populated_review_scope(self) -> None:
        report = delivery_stats.orchestration_metrics(self.repo, {197}, self.state_dir)

        ticket = report["tickets"]["197"]
        self.assertIsInstance(ticket["review_scope"], list)
        self.assertEqual(ticket["review_scope"][0]["files_total"], 2)


class OrchestrationMetricsWithoutBackendOrchestrationTests(unittest.TestCase):
    """harness/orchestration/ (ledger.py) belongs to the optional backend-orchestration capability;
    harness/reporting/ (delivery_stats.py) ships in the always-installed base suite. A project that
    never opted into backend-orchestration (no .harness/orchestration/ledger.py at all) must still
    degrade the orchestration metric to missing, not crash the whole report."""

    def test_reports_missing_when_the_analyzed_repo_has_no_ledger_module(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()

            report = delivery_stats.orchestration_metrics(repo, {197}, repo / ".harness/orchestration/state")

        self.assertEqual(report["status"], delivery_stats.MISSING)

    def test_reads_raw_ledger_state_written_directly_with_no_ledger_module_present(self) -> None:
        """Mirrors scripts/test-clean-room's synthetic-ledger fixture: state files written by
        hand, in a repo with no .harness/orchestration/ledger.py at all (legacy layout: no
        pointer file, records directly under the state root)."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            state = repo / ".harness" / "orchestration" / "state"
            (state / "batches").mkdir(parents=True)
            (state / "dispatches").mkdir()
            (state / "batches" / "batch-950-synthetic.json").write_text(json.dumps({
                "batch_id": "batch-950-synthetic",
                "branch": "feature/issue-950-synthetic",
                "dispatches": [
                    {"dispatch_id": "dispatch-950-developer", "role": "developer"},
                    {"dispatch_id": "dispatch-950-qa", "role": "qa", "decision": {"decision": "accept"}},
                ],
            }), encoding="utf-8")
            (state / "dispatches" / "dispatch-950-developer.json").write_text(
                json.dumps({"write_paths": ["services/**"]}), encoding="utf-8")

            report = delivery_stats.orchestration_metrics(repo, {950}, state)

        self.assertEqual(report["status"], "ok")
        ticket = report["tickets"]["950"]
        self.assertEqual(len(ticket["worker_sessions"]), 2)
        self.assertEqual(ticket["qa_failure_rate"], 0.0)

    def test_corrupted_record_degrades_gracefully_with_no_ledger_module_present(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            state = repo / ".harness" / "orchestration" / "state"
            (state / "batches").mkdir(parents=True)
            (state / "dispatches").mkdir()
            (state / "batches" / "batch-950-synthetic.json").write_text(json.dumps({
                "batch_id": "batch-950-synthetic",
                "branch": "feature/issue-950-synthetic",
                "dispatches": [
                    {"dispatch_id": "dispatch-950-developer", "role": "developer"},
                    {"dispatch_id": "dispatch-950-review", "role": "code-review"},
                ],
            }), encoding="utf-8")
            (state / "dispatches" / "dispatch-950-developer.json").write_text(
                json.dumps({"write_paths": ["services/**"]}), encoding="utf-8")
            (state / "dispatches" / "dispatch-950-review.json").write_text("not json", encoding="utf-8")

            report = delivery_stats.orchestration_metrics(repo, {950}, state)

        self.assertEqual(report["status"], "ok")
        ticket = report["tickets"]["950"]
        self.assertEqual(len(ticket["worker_sessions"]), 2)
        self.assertEqual(ticket["review_scope"], delivery_stats.MISSING)


def _install_incompatible_ledger_source(repo: Path) -> None:
    """A synthetic ledger.py mimicking a pre-#197 LifecycleLedger: it has no
    ``records_root_lenient()``/``read_record_lenient()`` at all. Loaded via the real
    ``_load_ledger_class`` the same way a real deployment's ledger.py would be, so
    ``orchestration_metrics()`` ends up calling methods that do not exist on the loaded class --
    reproducing the reviewer's repro: an old/incompatible module must degrade to the fallback
    algorithm, not raise AttributeError out of the whole report."""
    destination = repo / ".harness" / "orchestration"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "ledger.py").write_text(
        '"""Synthetic pre-#197 LifecycleLedger fixture: no lenient read API."""\n'
        "\n\n"
        "class LifecycleLedger:\n"
        "    def __init__(self, root):\n"
        "        self.root = root\n",
        encoding="utf-8",
    )


class OrchestrationMetricsIncompatibleLedgerModuleTests(unittest.TestCase):
    """The analyzed --repo has *some* .harness/orchestration/ledger.py, so ``_load_ledger_class``
    successfully loads a ``LifecycleLedger`` class -- but it is an old, pre-#197 version lacking
    the lenient read API. delivery_stats.py must not assume a loaded module is compatible."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _install_incompatible_ledger_source(self.repo)
        self.state_dir = self.tmp / "state"

        # Build real, well-formed ledger state with the current (compatible) LifecycleLedger --
        # only the module installed under the analyzed repo is incompatible/old.
        ledger = LifecycleLedger(self.state_dir)
        ledger.ensure()
        generation = ledger.records_root()
        batch_id = "batch-197-2"
        ledger.write_immutable(generation / "plans" / f"{batch_id}.json", {"batch_id": batch_id})
        ledger.write_immutable(generation / "batches" / f"{batch_id}.json", {
            "batch_id": batch_id,
            "branch": "feature/issue-197-lenient-read-api",
            "dispatches": [
                {"dispatch_id": "dispatch-dev-2", "role": "developer"},
                {"dispatch_id": "dispatch-qa-2", "role": "qa", "decision": {"decision": "accept"}},
            ],
        })
        ledger.write_immutable(generation / "dispatches" / "dispatch-dev-2.json", {
            "dispatch_id": "dispatch-dev-2", "write_paths": ["harness/**"],
        })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_incompatible_loaded_module_does_not_raise_and_degrades_gracefully(self) -> None:
        # Must not raise AttributeError: 'LifecycleLedger' object has no attribute
        # 'records_root_lenient' (or a static 'read_record_lenient').
        report = delivery_stats.orchestration_metrics(self.repo, {197}, self.state_dir)

        self.assertEqual(report["status"], "ok")
        ticket = report["tickets"]["197"]
        self.assertEqual(len(ticket["worker_sessions"]), 2)
        self.assertEqual(ticket["qa_failure_rate"], 0.0)


def _turn(branch, session_id, model, input_tokens, output_tokens, *,
          is_sidechain=False, cache_write=0, cache_read=0, timestamp="2026-01-01T10:00:00.000Z") -> str:
    """One assistant-turn JSONL record, complete enough to satisfy _usage_complete()."""
    return json.dumps({
        "type": "assistant", "gitBranch": branch, "sessionId": session_id,
        "timestamp": timestamp, "isSidechain": is_sidechain,
        "message": {"model": model, "usage": {
            "input_tokens": input_tokens, "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read, "output_tokens": output_tokens}},
    })


class ClaudeUsageSubagentTranscriptTests(unittest.TestCase):
    """Issue #206: a subagent's own transcript (<parent-session>/subagents/agent-<hex>.jsonl) must
    be counted under its own identity, not collapsed into its parent's session_stats row -- even
    though the subagent transcript's own "sessionId" field replays the *parent's* session id."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        (self.project / "parent1.jsonl").write_text(
            _turn("feature/issue-42-x", "parent1", "model-a", 100, 50) + "\n", encoding="utf-8")
        subagents = self.project / "parent1" / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent-aaa.jsonl").write_text(
            _turn("feature/issue-42-x", "parent1", "model-a", 10, 5, is_sidechain=True) + "\n", encoding="utf-8")
        (subagents / "agent-bbb.jsonl").write_text(
            _turn("feature/issue-42-x", "parent1", "model-a", 777, 333, is_sidechain=True) + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_subagent_counted_and_not_collapsed_with_sibling(self) -> None:
        report = delivery_stats.claude_usage([self.project], {42})

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["sidechain"]["turns"], 2)
        ids = {s["id"] for s in report["session_stats"]}
        self.assertIn("agent-aaa", ids)
        self.assertIn("agent-bbb", ids)
        self.assertIn("parent1", ids)
        by_id = {s["id"]: s for s in report["session_stats"]}
        self.assertEqual(by_id["agent-aaa"]["kind"], "subagent")
        self.assertEqual(by_id["agent-bbb"]["kind"], "subagent")
        self.assertEqual(by_id["parent1"]["kind"], "main")
        self.assertNotEqual(by_id["agent-aaa"]["total_input"], by_id["agent-bbb"]["total_input"])
        self.assertEqual(report["turns"], 3)

    def test_claude_usage_unchanged_without_subagents(self) -> None:
        import shutil
        subagents_dir = self.project / "parent1" / "subagents"
        shutil.rmtree(subagents_dir)
        
        report = delivery_stats.claude_usage([self.project], {42})
        
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["sidechain"]["turns"], 0)
        self.assertEqual(report["turns"], 1)
        
        by_id = {s["id"]: s for s in report["session_stats"]}
        self.assertIn("parent1", by_id)
        self.assertEqual(by_id["parent1"]["kind"], "main")
        self.assertEqual(by_id["parent1"]["turns"], 1)
        self.assertEqual(len(by_id), 1)


class LiveProbeTests(unittest.TestCase):
    """Issue #206: live_probe() is a branch-scoped, non-epic-scoped snapshot separate from
    claude_usage()'s post-epic report."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "project2"
        self.project.mkdir()
        (self.project / "s1.jsonl").write_text(
            "\n".join([
                _turn("feature/issue-9-y", "s1", "model-a", 500, 10, timestamp="2026-01-01T10:00:00.000Z"),
                _turn("feature/issue-9-y", "s1", "model-a", 120, 10, timestamp="2026-01-01T10:05:00.000Z"),
            ]) + "\n", encoding="utf-8")
        subagents = self.project / "s1" / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent-ccc.jsonl").write_text(
            _turn("feature/issue-9-y", "s1", "model-a", 42, 5, is_sidechain=True) + "\n", encoding="utf-8")
        (self.project / "s2.jsonl").write_text(
            _turn("feature/issue-999-other", "s2", "model-a", 999, 10) + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_live_probe_shape_on_synthetic_data(self) -> None:
        result = delivery_stats.live_probe([self.project], "feature/issue-9-y")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["branch"], "feature/issue-9-y")
        ids = {s["id"] for s in result["sessions"]}
        self.assertEqual(ids, {"s1", "agent-ccc"})
        by_id = {s["id"]: s for s in result["sessions"]}
        self.assertEqual(by_id["s1"]["turns"], 2)
        self.assertEqual(by_id["s1"]["max_input"], 500)
        self.assertEqual(by_id["s1"]["last_input"], 120)
        self.assertEqual(by_id["agent-ccc"]["kind"], "subagent")
        self.assertEqual(by_id["agent-ccc"]["turns"], 1)
        self.assertEqual(by_id["agent-ccc"]["last_input"], 42)

    def test_live_probe_missing_when_no_matching_branch(self) -> None:
        result = delivery_stats.live_probe([self.project], "feature/issue-000-nowhere")

        self.assertEqual(result["status"], delivery_stats.MISSING)


class NarrowedTypeBehaviorTests(unittest.TestCase):
    """Characterises the runtime behaviour of the functions whose annotations issue #230 narrowed
    (no existing coverage), so the narrowing cannot change what they return."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_claude_usage_ignores_a_record_whose_branch_is_not_a_string(self) -> None:
        project = self.tmp / "project"
        project.mkdir()
        good = _turn("feature/issue-42-x", "s1", "model-a", 10, 5)
        no_branch = json.dumps({"type": "assistant", "gitBranch": None, "message": {"model": "m", "usage": {}}})
        (project / "s1.jsonl").write_text(good + "\n" + no_branch + "\n", encoding="utf-8")

        report = delivery_stats.claude_usage([project], {42})

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["turns"], 1)
        self.assertEqual(report["branches"], ["feature/issue-42-x"])

    def test_claude_usage_is_missing_when_a_turn_usage_is_not_a_dict(self) -> None:
        project = self.tmp / "project"
        project.mkdir()
        bad_usage = json.dumps({
            "type": "assistant", "gitBranch": "feature/issue-42-x", "sessionId": "s2",
            "message": {"model": "model-a", "usage": "not-a-dict"},
        })
        (project / "s2.jsonl").write_text(bad_usage + "\n", encoding="utf-8")

        report = delivery_stats.claude_usage([project], {42})

        self.assertEqual(report["status"], delivery_stats.MISSING)
        self.assertIn("неполная", report["reason"])

    def test_codex_usage_counts_a_repeated_ordinal_once(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        sessions = self.tmp / "sessions"
        sessions.mkdir()
        record = json.dumps({
            "timestamp": "2026-01-01T10:00:00.000Z", "ordinal": 1, "cwd": str(repo),
            "payload": {"type": "token_count", "model": "codex-a", "info": {"last_token_usage": {
                "input_tokens": 10, "cached_input_tokens": 2, "cache_write_input_tokens": 0, "output_tokens": 3}}},
        })
        (sessions / "rollout-1.jsonl").write_text(record + "\n" + record + "\n", encoding="utf-8")
        window = (delivery_stats._moment("2026-01-01T00:00:00Z"), delivery_stats._moment("2026-01-02T00:00:00Z"))

        report = delivery_stats.codex_usage(sessions, repo, window)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["turns"], 1)
        self.assertEqual(report["models"]["codex-a"]["output_tokens"], 3)

    def test_cache_split_is_missing_without_ok_status_or_tokens_and_splits_otherwise(self) -> None:
        self.assertEqual(delivery_stats.cache_split({"status": delivery_stats.MISSING}), delivery_stats.MISSING)
        empty = {"status": "ok", "models": {"m": {
            "input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}
        self.assertEqual(delivery_stats.cache_split(empty), delivery_stats.MISSING)
        used = {"status": "ok", "models": {"m": {
            "input_tokens": 50, "cache_creation_input_tokens": 25, "cache_read_input_tokens": 25}}}
        split = delivery_stats.cache_split(used)
        self.assertEqual(split["total_input"], 100)
        self.assertEqual(split["fresh_percent"], 50.0)

    def test_provider_delta_is_missing_unless_both_sides_are_ok_and_skips_absent_cache_fields(self) -> None:
        ok = {"status": "ok", "input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
        later = {"status": "ok", "input_tokens": 30, "output_tokens": 9, "total_tokens": 39,
                 "cache_write_tokens": 5, "cache_read_tokens": 7}
        self.assertEqual(delivery_stats._provider_delta("x", ok), delivery_stats.MISSING)
        self.assertEqual(delivery_stats._provider_delta({"status": "missing"}, ok), delivery_stats.MISSING)
        delta = delivery_stats._provider_delta(ok, later)
        self.assertEqual(delta["input_tokens"], 20)
        self.assertEqual(delta["cache_write_tokens"], delivery_stats.MISSING)

        both = delivery_stats._provider_delta(later, later)
        self.assertEqual(both["cache_read_tokens"], 0)

    def test_developer_write_paths_returns_the_latest_developer_dispatch_zone(self) -> None:
        dispatches = self.tmp / "dispatches"
        dispatches.mkdir()
        (dispatches / "d1.json").write_text(json.dumps({"write_paths": ["old/"]}), encoding="utf-8")
        (dispatches / "d2.json").write_text(json.dumps({"write_paths": ["new/"]}), encoding="utf-8")
        batch = {"dispatches": [
            {"role": "developer", "dispatch_id": "d1"},
            {"role": "qa", "dispatch_id": "q1"},
            {"role": "developer", "dispatch_id": "d2"},
        ]}

        self.assertEqual(delivery_stats._developer_write_paths(self.tmp, batch, None), ["new/"])
        self.assertIsNone(delivery_stats._developer_write_paths(self.tmp, {"dispatches": []}, None))


class StatsErrorRemedyTests(unittest.TestCase):
    """Every `raise StatsError(...)` in delivery_stats.py carries a non-empty message and remedy
    (issue #230): one row per raise site, so a dropped remedy fails a named case."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def _assert_stats_error(self, raised: Any, message: str, remedy: str) -> None:
        self.assertIsInstance(raised.exception, HarnessError)
        self.assertIn(message, raised.exception.message)
        self.assertIn(remedy, raised.exception.remedy)

    def test_git_failure(self) -> None:
        with mock.patch.object(delivery_stats, "_run", return_value=(1, "", "boom")), \
                self.assertRaises(delivery_stats.StatsError) as raised:
            delivery_stats._git(self.tmp, "status")
        self._assert_stats_error(raised, "git status failed: boom", "git status")

    def test_project_config_invalid_json(self) -> None:
        (self.tmp / ".harness").mkdir()
        (self.tmp / ".harness" / "project.json").write_text("{", encoding="utf-8")
        with self.assertRaises(delivery_stats.StatsError) as raised:
            delivery_stats._project_config(self.tmp)
        self._assert_stats_error(raised, "not valid JSON", ".harness/project.json")

    def test_gh_failures(self) -> None:
        for outcome, message, remedy in (
            ((127, "", "no gh"), "gh CLI is required", "--tickets"),
            ((1, "", "denied"), "gh issue view failed: denied", "authentication"),
            ((0, "not json", ""), "not valid JSON", "gh issue view"),
        ):
            with self.subTest(message=message):
                with mock.patch.object(delivery_stats, "_run", return_value=outcome), \
                        self.assertRaises(delivery_stats.StatsError) as raised:
                    delivery_stats._gh(self.tmp, "issue", "view")
                self._assert_stats_error(raised, message, remedy)

    def test_offline_scope_rejects_non_numeric_ticket(self) -> None:
        with self.assertRaises(delivery_stats.StatsError) as raised:
            delivery_stats.offline_scope(1, "2,abc")
        self._assert_stats_error(raised, "'abc'", "--tickets")

    def test_rate_card_failures(self) -> None:
        for name, text, message, remedy in (
            ("bad.json", "{", "rate card is not valid JSON", "fix the JSON syntax"),
            ("shape.json", "[]", "rate card must be an object", "'models' object"),
        ):
            with self.subTest(name=name):
                path = self._write(name, text)
                with self.assertRaises(delivery_stats.StatsError) as raised:
                    delivery_stats.load_rates(path)
                self._assert_stats_error(raised, message, remedy)

    def test_load_baseline_failures(self) -> None:
        version = delivery_stats.BASELINE_SCHEMA_VERSION
        telemetry = {"status": "ok", "input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        for name, text, message, remedy in (
            ("bad.json", "{", "не является JSON", "fix the JSON syntax"),
            ("version.json", "{}", "неподдерживаемый формат", "--save-baseline"),
            ("shape.json", json.dumps({"schema_version": version}), "не содержит providers и epic", "providers and epic"),
            (
                "missing.json",
                json.dumps({"schema_version": version, "providers": {"claude": telemetry}, "epic": {}}),
                "не содержит telemetry codex",
                "codex telemetry",
            ),
            (
                "incomplete.json",
                json.dumps({
                    "schema_version": version,
                    "providers": {"claude": {"status": "ok"}, "codex": telemetry},
                    "epic": {},
                }),
                "неполную telemetry claude",
                "complete integers",
            ),
        ):
            with self.subTest(name=name):
                path = self._write(name, text)
                with self.assertRaises(delivery_stats.StatsError) as raised:
                    delivery_stats.load_baseline(path)
                self._assert_stats_error(raised, message, remedy)

    def test_load_baseline_unreadable_file(self) -> None:
        path = self.tmp / "absent.json"
        with self.assertRaises(delivery_stats.StatsError) as raised:
            delivery_stats.load_baseline(path)
        self._assert_stats_error(raised, "не прочитать baseline", "file-system error")

    def test_save_baseline_unwritable_destination(self) -> None:
        blocker = self._write("blocker", "file")
        with self.assertRaises(delivery_stats.StatsError) as raised:
            delivery_stats.save_baseline({}, blocker / "baseline.json")
        self._assert_stats_error(raised, "не сохранить baseline", "file-system error")

    def test_build_report_failures(self) -> None:
        home = self.tmp / "home"
        home.mkdir()
        args = argparse.Namespace(
            repo=str(self.tmp), epic=7, tickets="7", base="master", home=str(home), claude_projects=None,
            codex_sessions=None, rates=None, orchestration_state_dir=None, baseline=None,
        )
        with self.assertRaises(delivery_stats.StatsError) as raised:
            delivery_stats.build_report(args)
        self._assert_stats_error(raised, "not a git repository", "--repo")

        (self.tmp / ".git").mkdir()
        with mock.patch.object(delivery_stats, "_local_issue_branches", return_value=set()), \
                self.assertRaises(delivery_stats.StatsError) as raised:
            delivery_stats.build_report(args)
        self._assert_stats_error(raised, "epic #7: no pull request", "--tickets")


if __name__ == "__main__":
    unittest.main()
