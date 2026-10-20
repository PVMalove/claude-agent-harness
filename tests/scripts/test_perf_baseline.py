"""Tests for the 1.x performance baseline helpers in scripts/perf/baseline.py (issue #795)."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.perf import baseline


class PercentilesTests(unittest.TestCase):
    def test_nearest_rank_percentiles_of_ten_samples(self) -> None:
        samples = [7.0, 1.0, 10.0, 3.0, 5.0, 2.0, 9.0, 4.0, 8.0, 6.0]

        result = baseline.percentiles(samples)

        self.assertEqual(
            result,
            {"n": 10, "min": 1.0, "p50": 5.0, "p95": 10.0, "p99": 10.0, "max": 10.0},
        )

    def test_empty_sample_is_rejected_instead_of_reported_as_zero(self) -> None:
        with self.assertRaises(ValueError):
            baseline.percentiles([])


class FullRunTests(unittest.TestCase):
    FULL = (
        "python scripts/verify.py",
        ".harness/.venv/bin/python scripts/verify.py --fail-fast",
        "make verify",
        "python -m pytest -q -n 4 tests",
        ".harness/.venv/bin/python -m pytest -q tests/",
        "cd /repo && make verify",
        "bash -lc 'make verify'",
        "python -m pytest -p no:cacheprovider tests",
        "python -m pytest -q",
    )
    FOCUSED = (
        "python scripts/verify.py --only clean-room",
        "python -m pytest -q tests/orchestration tests/memory",
        ".harness/.venv/bin/python -m pytest -q tests/core/test_gate_runner.py",
        "python -m pytest -q tests/scripts/test_perf_baseline.py::PercentilesTests",
        "make lint",
        "python -m mypy",
        "python -m ruff format .",
        "python scripts/verify.py --only=clean-room",
        "python -m pytest -q 'unbalanced",
        "python -m pytest -k foo tests",
        "python -m pytest -m 'not slow' tests",
    )

    def test_whole_suite_commands_count_as_full_runs(self) -> None:
        for command in self.FULL:
            with self.subTest(command=command):
                self.assertTrue(baseline.is_full_run(command))

    def test_focused_and_static_commands_are_not_full_runs(self) -> None:
        for command in self.FOCUSED:
            with self.subTest(command=command):
                self.assertFalse(baseline.is_full_run(command))


class VerifyStageDurationTests(unittest.TestCase):
    def test_passed_and_failed_stage_lines_are_read_and_other_output_ignored(
        self,
    ) -> None:
        log = (
            "private-terms: no matches\n"
            "[verify] mypy: passed in 10.84s\n"
            "....F.. [ 99%]\n"
            "[verify] pytest: failed in 518.60s\n"
            "[verify] clean-room: passed in 237.88s\n"
        )

        result = baseline.verify_stage_durations(log)

        self.assertEqual(
            result,
            {
                "mypy": {"status": "passed", "seconds": 10.84},
                "pytest": {"status": "failed", "seconds": 518.6},
                "clean-room": {"status": "passed", "seconds": 237.88},
            },
        )


class SummarizeTests(unittest.TestCase):
    def test_percentiles_cover_only_successful_runs(self) -> None:
        samples: list[baseline.Sample] = [
            {"seconds": 2.0, "returncode": 0},
            {"seconds": 9.0, "returncode": 1, "stderr_tail": ["boom"]},
            {"seconds": 4.0, "returncode": 0},
        ]

        result = baseline.summarize(samples)

        self.assertEqual(
            result,
            {
                "status": "measured",
                "failed_runs": 1,
                "n": 2,
                "min": 2.0,
                "p50": 2.0,
                "p95": 4.0,
                "p99": 4.0,
                "max": 4.0,
            },
        )

    def test_no_successful_run_is_reported_as_not_measured(self) -> None:
        samples: list[baseline.Sample] = [{"seconds": 0.3, "returncode": 2}]

        result = baseline.summarize(samples)

        self.assertEqual(result, {"status": "not_measured", "failed_runs": 1})


class RedactTests(unittest.TestCase):
    def test_local_paths_are_replaced_and_ansi_codes_removed(self) -> None:
        log = "\x1b[32mok\x1b[0m /work/main/wt/scripts and /work/main/.harness in /home/u\n"

        result = baseline.redact(
            log,
            {
                "/work/main/wt": "<repo>",
                "/work/main": "<main-checkout>",
                "/home/u": "<home>",
            },
        )

        self.assertEqual(
            result, "ok <repo>/scripts and <main-checkout>/.harness in <home>\n"
        )


class ImplementRunsTests(unittest.TestCase):
    def _write(self, path: Path, record: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")

    def _dispatch(
        self,
        generation: Path,
        dispatch_id: str,
        batch_id: str,
        role: str,
        updated_at: str,
        commands: list[str],
    ) -> None:
        self._write(
            generation / "dispatches" / f"{dispatch_id}.json",
            {"dispatch_id": dispatch_id, "batch_id": batch_id, "role": role},
        )
        self._write(
            generation / "dispatch-status" / f"{dispatch_id}.json",
            {"dispatch_id": dispatch_id, "state": "reported", "updated_at": updated_at},
        )
        self._write(
            generation / "reports" / f"{dispatch_id}.json",
            {
                "dispatch_id": dispatch_id,
                "role": role,
                "checks_run": [
                    {"command": command, "result": "pass"} for command in commands
                ],
            },
        )

    def test_batch_duration_and_full_runs_come_from_its_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            generation = Path(tmp)
            self._write(
                generation / "batches" / "batch-a.json",
                {
                    "batch_id": "batch-a",
                    "ticket": "#1",
                    "state": "completed",
                    "created_at": "2026-10-01T10:00:00+00:00",
                },
            )
            self._dispatch(
                generation,
                "dispatch-1",
                "batch-a",
                "developer",
                "2026-10-01T10:30:00+00:00",
                [
                    "python -m mypy",
                    "python -m pytest -q -n 4 tests",
                    "python scripts/verify.py",
                ],
            )
            self._dispatch(
                generation,
                "dispatch-2",
                "batch-a",
                "qa",
                "2026-10-01T11:15:30+00:00",
                ["python scripts/verify.py"],
            )
            self._dispatch(
                generation,
                "dispatch-3",
                "batch-a",
                "code-review",
                "2026-10-01T10:50:00+00:00",
                ["python -m pytest -q tests/orchestration"],
            )
            self._write(
                generation / "reports" / "dispatch-4.json",
                {
                    "dispatch_id": "dispatch-4",
                    "role": "qa",
                    "checks_run": [{"result": "pass"}],
                },
            )
            self._write(
                generation / "dispatches" / "dispatch-4.json",
                {"dispatch_id": "dispatch-4", "batch_id": "batch-a", "role": "qa"},
            )

            result = baseline.implement_runs(generation)

        self.assertEqual(
            result,
            [
                {
                    "batch_id": "batch-a",
                    "ticket": "#1",
                    "state": "completed",
                    "created_at": "2026-10-01T10:00:00+00:00",
                    "finished_at": "2026-10-01T11:15:30+00:00",
                    "duration_seconds": 4530.0,
                    "full_runs": {"developer": 2, "qa": 1},
                }
            ],
        )

    def test_batch_without_dispatch_status_has_no_duration_instead_of_zero(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            generation = Path(tmp)
            self._write(
                generation / "batches" / "batch-b.json",
                {
                    "batch_id": "batch-b",
                    "ticket": "#2",
                    "state": "abandoned",
                    "created_at": "2026-10-02T09:00:00+00:00",
                },
            )

            result = baseline.implement_runs(generation)

        self.assertEqual(
            result,
            [
                {
                    "batch_id": "batch-b",
                    "ticket": "#2",
                    "state": "abandoned",
                    "created_at": "2026-10-02T09:00:00+00:00",
                    "finished_at": None,
                    "duration_seconds": None,
                    "full_runs": {},
                }
            ],
        )


class LedgerHistoryTests(unittest.TestCase):
    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_only_local_qa_results_are_reported_in_finish_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            generation = Path(tmp)
            qa = generation / "reports" / "integration-local-qa"
            self._write(
                qa / "local-qa-a.json",
                json.dumps({"event": "request", "local_qa_id": "local-qa-a"}),
            )
            self._write(
                qa / "local-qa-b.json",
                json.dumps(
                    {
                        "event": "result",
                        "local_qa_id": "local-qa-b",
                        "finished_at": "2026-10-08T10:00:00+00:00",
                        "state": "completed",
                        "target_sha": "bbb",
                        "duration_seconds": 462.1,
                        "verification_commands": ["python scripts/verify.py"],
                    }
                ),
            )
            self._write(
                qa / "local-qa-c.json",
                json.dumps(
                    {
                        "event": "result",
                        "local_qa_id": "local-qa-c",
                        "finished_at": "2026-10-07T10:00:00+00:00",
                        "state": "completed",
                        "target_sha": "ccc",
                        "duration_seconds": 17.8,
                        "verification_commands": ["make lint"],
                    }
                ),
            )

            result = baseline.local_qa_results(generation)

        self.assertEqual(
            result,
            [
                {
                    "local_qa_id": "local-qa-c",
                    "finished_at": "2026-10-07T10:00:00+00:00",
                    "state": "completed",
                    "target_sha": "ccc",
                    "duration_seconds": 17.8,
                    "commands": ["make lint"],
                    "full_run": False,
                },
                {
                    "local_qa_id": "local-qa-b",
                    "finished_at": "2026-10-08T10:00:00+00:00",
                    "state": "completed",
                    "target_sha": "bbb",
                    "duration_seconds": 462.1,
                    "commands": ["python scripts/verify.py"],
                    "full_run": True,
                },
            ],
        )

    def test_qa_logs_without_verify_stages_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            generation = Path(tmp)
            artifacts = generation / "qa-artifacts"
            self._write(artifacts / "a.log", "[verify] mypy: passed in 8.38s\n")
            self._write(artifacts / "b.log", "exit 0; private-terms: no matches\n")

            result = baseline.qa_artifact_stages(generation)

        self.assertEqual(
            result, {"a.log": {"mypy": {"status": "passed", "seconds": 8.38}}}
        )


class TimeCommandTests(unittest.TestCase):
    def test_every_successful_run_is_sampled_with_its_summary_line(self) -> None:
        argv = [sys.executable, "-c", "print('collecting'); print('3 passed in 0.01s')"]

        with tempfile.TemporaryDirectory() as tmp:
            samples = baseline.time_command(
                argv, cwd=Path(tmp), repeats=2, keep_summary_line=True
            )

        self.assertEqual(
            [(s["returncode"], s.get("summary_line")) for s in samples],
            [(0, "3 passed in 0.01s"), (0, "3 passed in 0.01s")],
        )
        self.assertTrue(all(s["seconds"] > 0 for s in samples))

    def test_repeats_stop_after_the_first_refusal(self) -> None:
        argv = [
            sys.executable,
            "-c",
            "import sys; print('ERROR: refused', file=sys.stderr); sys.exit(2)",
        ]

        with tempfile.TemporaryDirectory() as tmp:
            samples = baseline.time_command(argv, cwd=Path(tmp), repeats=5)

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["returncode"], 2)
        self.assertEqual(samples[0].get("stderr_tail"), ["ERROR: refused"])


class MainArgumentTests(unittest.TestCase):
    def _main_exit_code(self, argv: list[str]) -> object:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            baseline.main(argv)
        return raised.exception.code

    def test_zero_repeats_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = self._main_exit_code(
                ["--repo", tmp, "--state-dir", tmp, "--out", tmp, "--repeats", "0"]
            )

        self.assertEqual(code, 2)

    def test_checkout_that_already_holds_a_ledger_state_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".harness" / "orchestration" / "state").mkdir(parents=True)
            out = Path(tmp) / "out"

            code = self._main_exit_code(
                ["--repo", str(repo), "--state-dir", tmp, "--out", str(out)]
            )
            out_created = out.exists()

        self.assertEqual(code, 2)
        self.assertFalse(out_created)


class MainRunTests(unittest.TestCase):
    """`main` on a stub checkout: coordinator and lifecycle stubs, the focused test files."""

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _stub_checkout(self, repo: Path) -> None:
        self._write(repo / "harness/orchestration/coordinator.py", "print('{}')\n")
        for package in (
            "harness",
            "harness/orchestration",
            "harness/orchestration/ledger",
        ):
            self._write(repo / package / "__init__.py", "")
        self._write(
            repo / "harness/orchestration/ledger/lifecycle.py",
            "class LifecycleLedger:\n"
            "    @staticmethod\n"
            "    def _validate_audit(root):\n"
            "        return None\n",
        )
        for test_file in baseline.FOCUSED_TESTS:
            self._write(repo / test_file, "def test_ok():\n    pass\n")
        self._write(
            repo / "tests/orchestration/test_verification_profiles.py",
            "import unittest\n"
            "class VerificationProfileBatchTests(unittest.TestCase):\n"
            "    def test_a_profile_covering_every_allowed_path_is_frozen_in_the_batch(self):\n"
            "        pass\n",
        )
        self._write(repo / ".gitignore", ".harness/\n")
        # Как в настоящем checkout: без своего ini-файла rootdir pytest уходит к ближайшему
        # предку с конфигурацией (у `make verify` TMPDIR лежит внутри репозитория), и `--lf` не
        # узнаёт node id из `lastfailed`.
        self._write(
            repo / "pyproject.toml",
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        )
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
        for argv in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", *identity, "commit", "-qm", "stub"],
        ):
            subprocess.run(argv, cwd=repo, check=True)

    def _state(self, state: Path) -> Path:
        generation = state / "generations" / "generation-1"
        self._write(state / "ledger.json", json.dumps({"generation": "generation-1"}))
        self._write(generation / "audit" / "audit-1.json", "{}")
        self._write(
            generation / "dispatch-status" / "dispatch-1.json",
            json.dumps(
                {
                    "dispatch_id": "dispatch-1",
                    "state": "reported",
                    "updated_at": "2026-10-01T10:00:00+00:00",
                }
            ),
        )
        for directory in ("batches", "dispatches"):
            (generation / directory).mkdir()
        return generation / "dispatch-status" / "dispatch-1.json"

    def test_measurement_writes_raw_data_and_leaves_both_states_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, state, out = (
                Path(tmp) / "repo",
                Path(tmp) / "state",
                Path(tmp) / "out",
            )
            self._stub_checkout(repo)
            source_status = self._state(state)
            argv = ["--repo", str(repo), "--state-dir", str(state), "--out", str(out)]
            repeats = ["--repeats", "2", "--slow-repeats", "1", "--test-repeats", "1"]

            code = baseline.main([*argv, *repeats])

            written = sorted(path.name for path in out.iterdir())
            coordinator = json.loads(
                (out / "coordinator.json").read_text(encoding="utf-8")
            )
            pytest_runs = json.loads((out / "pytest.json").read_text(encoding="utf-8"))
            environment = json.loads(
                (out / "environment.json").read_text(encoding="utf-8")
            )
            audit = json.loads((out / "audit.json").read_text(encoding="utf-8"))
            copy_left = (repo / ".harness/orchestration/state").exists()
            source_state = json.loads(source_status.read_text(encoding="utf-8"))[
                "state"
            ]

        self.assertEqual(code, 0)
        self.assertEqual(
            written,
            [
                "audit.json",
                "coordinator.json",
                "environment.json",
                "implement-runs.json",
                "ledger-size.json",
                "local-qa.json",
                "pytest.json",
                "qa-artifact-stages.json",
                "run.json",
            ],
        )
        heartbeat = coordinator["dispatch heartbeat"]
        self.assertEqual(heartbeat["live_state"], "synthetic")
        self.assertEqual(heartbeat["summary"]["n"], 2)
        self.assertEqual(coordinator["ledger status"]["summary"]["status"], "measured")
        self.assertEqual(audit["records"], 1)
        self.assertEqual(audit["summary"]["status"], "measured")
        self.assertIn(
            "1 passed", pytest_runs["failed_only"]["samples"][0]["summary_line"]
        )
        self.assertIn("5 passed", pytest_runs["focused"]["samples"][0]["summary_line"])
        self.assertTrue(environment["worktree_clean"])
        self.assertFalse(copy_left)
        self.assertEqual(source_state, "reported")


if __name__ == "__main__":
    unittest.main()
