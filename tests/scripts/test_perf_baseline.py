"""Tests for the 1.x performance baseline helpers in scripts/perf/baseline.py (issue #795)."""

from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
