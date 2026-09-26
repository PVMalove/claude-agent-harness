"""Cleanup only disposes of recoverable local runtime data."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.cleanup import apply_cleanup, plan_cleanup


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / ".gitignore").write_text("/.harness/\n", encoding="utf-8")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-m", "test: initial fixture")

    def test_soft_previews_old_runs_scratch_logs_and_legacy_dirs_but_keeps_active_run_and_ledger(self) -> None:
        root = self.repo / ".harness"
        sandboxes = root / ".sandboxes"

        old_run = sandboxes / "runs" / "old"
        active_run = sandboxes / "runs" / "active"
        scratch_file = sandboxes / "scratch" / "transit.md"
        log_file = sandboxes / "logs" / "session.log"
        sandboxes_cache = sandboxes / "cache" / "rebuildable.json"
        sandboxes_reports = sandboxes / "reports" / "report.html"

        legacy_cache = root / ".cache"
        legacy_tmp = root / "tmp"
        legacy_logs = root / "test-logs"
        legacy_reports = root / "reports"
        ledger = root / "orchestration" / "state" / "ledger.json"

        for directory in (
            old_run,
            active_run,
            scratch_file.parent,
            log_file.parent,
            sandboxes_cache.parent,
            sandboxes_reports.parent,
            legacy_cache,
            legacy_tmp,
            legacy_logs,
            legacy_reports,
            ledger.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        (old_run / "artifact.txt").write_text("done", encoding="utf-8")
        (active_run / ".active.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        scratch_file.write_text("transient PR body", encoding="utf-8")
        log_file.write_text("test logs", encoding="utf-8")
        sandboxes_cache.write_text("rebuildable", encoding="utf-8")
        sandboxes_reports.write_text("report", encoding="utf-8")

        (legacy_cache / "old_cache.bin").write_text("old", encoding="utf-8")
        (legacy_tmp / "old_tmp.bin").write_text("old", encoding="utf-8")
        (legacy_logs / "old.log").write_text("old", encoding="utf-8")
        (legacy_reports / "old.html").write_text("old", encoding="utf-8")
        ledger.write_text("durable", encoding="utf-8")

        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        self.assertTrue(old_run.exists())

        expected_remove = {
            str(old_run),
            str(scratch_file),
            str(log_file),
            str(legacy_cache),
            str(legacy_tmp),
            str(legacy_logs),
            str(legacy_reports),
        }
        self.assertEqual({item["path"] for item in plan["remove"]}, expected_remove)

        result = apply_cleanup(self.repo, plan)
        self.assertFalse(result["failed"])
        self.assertFalse(old_run.exists())
        self.assertFalse(scratch_file.exists())
        self.assertFalse(log_file.exists())
        self.assertFalse(legacy_cache.exists())
        self.assertFalse(legacy_tmp.exists())
        self.assertFalse(legacy_logs.exists())
        self.assertFalse(legacy_reports.exists())

        self.assertTrue(active_run.exists())
        self.assertTrue(ledger.exists())
        self.assertTrue(sandboxes_cache.exists())
        self.assertTrue(sandboxes_reports.exists())

    def test_soft_removes_a_run_whose_windows_pid_no_longer_exists(self) -> None:
        stale = self.repo / ".harness" / ".sandboxes" / "runs" / "stale"
        stale.mkdir(parents=True)
        (stale / ".active.json").write_text(
            json.dumps({"pid": 987654321}), encoding="utf-8"
        )
        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        self.assertIn(str(stale), {item["path"] for item in plan["remove"]})
        result = apply_cleanup(self.repo, plan)
        self.assertFalse(result["failed"])
        self.assertFalse(stale.exists())

    def test_soft_does_not_terminate_a_live_run_while_checking_its_pid(self) -> None:
        active = self.repo / ".harness" / ".sandboxes" / "runs" / "active-child"
        active.mkdir(parents=True)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            (active / ".active.json").write_text(
                json.dumps({"pid": process.pid}), encoding="utf-8"
            )
            if os.name == "nt":
                with mock.patch(
                    "harness.cleanup.os.kill",
                    side_effect=AssertionError("Windows PID probe must not call os.kill"),
                ):
                    plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
            else:
                plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
            self.assertNotIn(str(active), {item["path"] for item in plan["remove"]})
            self.assertIsNone(process.poll())
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)

    @unittest.skipUnless(os.name == "nt", "Windows long-path cleanup")
    def test_soft_removes_a_run_with_paths_longer_than_max_path(self) -> None:
        stale = self.repo / ".harness" / ".sandboxes" / "runs" / "long-path"
        nested = stale.joinpath(*(f"part-{index}-" + "x" * 54 for index in range(4)))
        long_file = nested / "payload.txt"
        self.assertGreater(len(str(long_file)), 260)
        long_nested = Path("\\\\?\\" + str(nested))
        long_nested.mkdir(parents=True)
        (long_nested / long_file.name).write_text("finished", encoding="utf-8")

        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        result = apply_cleanup(self.repo, plan)

        self.assertFalse(result["failed"])
        self.assertFalse(stale.exists())

    def test_hard_cleans_sandboxes_cache_and_reports_and_requires_confirm(self) -> None:
        sandboxes = self.repo / ".harness" / ".sandboxes"
        cache_item = sandboxes / "cache" / "repo_map"
        cache_item.mkdir(parents=True, exist_ok=True)
        (cache_item / "cached.json").write_text("{}", encoding="utf-8")

        reports_item = sandboxes / "reports" / "summary.html"
        reports_item.parent.mkdir(parents=True, exist_ok=True)
        reports_item.write_text("report", encoding="utf-8")

        soft_plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        self.assertNotIn(str(cache_item), {item["path"] for item in soft_plan["remove"]})
        self.assertNotIn(str(reports_item), {item["path"] for item in soft_plan["remove"]})

        hard_plan = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertIn(str(cache_item), {item["path"] for item in hard_plan["remove"]})
        self.assertIn(str(reports_item), {item["path"] for item in hard_plan["remove"]})

        with self.assertRaises(ValueError) as ctx:
            apply_cleanup(self.repo, hard_plan)
        self.assertIn("confirm='HARD'", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            apply_cleanup(self.repo, hard_plan, confirm="NO")
        self.assertIn("confirm='HARD'", str(ctx.exception))

        result = apply_cleanup(self.repo, hard_plan, confirm="HARD")
        self.assertFalse(result["failed"])
        self.assertFalse(cache_item.exists())
        self.assertFalse(reports_item.exists())

    def test_hard_keeps_active_and_dirty_worktree_then_removes_local_branch_only(self) -> None:
        remote = self.base / "origin.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-u", "origin", "main")
        tree = self.repo / ".harness" / ".sandboxes" / "worktrees" / "issue-1-one"
        tree.parent.mkdir(parents=True)
        branch = "feature/issue-1-one"
        git(self.repo, "worktree", "add", "-b", branch, str(tree), "HEAD")
        unpushed = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in unpushed["remove"]})
        git(self.repo, "push", "-u", "origin", branch)

        state = self.repo / ".harness" / "orchestration" / "state"
        records = state / "generations" / "generation-test" / "batches"
        records.mkdir(parents=True)
        (state / "ledger.json").write_text(json.dumps({"generation": "generation-test"}), encoding="utf-8")
        batch = records / "batch-test.json"
        batch.write_text(
            json.dumps({"state": "awaiting-approval", "worktree": ".harness/.sandboxes/worktrees/issue-1-one"}),
            encoding="utf-8",
        )
        plan = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in plan["remove"]})

        batch.write_text(
            json.dumps({"state": "completed", "worktree": ".harness/.sandboxes/worktrees/issue-1-one"}),
            encoding="utf-8",
        )
        (tree / "dirty.txt").write_text("keep", encoding="utf-8")
        plan = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in plan["remove"]})

        (tree / "dirty.txt").unlink()
        git(self.repo, "push", "origin", "--delete", branch)
        git(self.repo, "update-ref", f"refs/remotes/origin/{branch}", git(self.repo, "rev-parse", branch))
        stale_tracking = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in stale_tracking["remove"]})
        git(self.repo, "push", "origin", branch)
        plan = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertIn(str(tree), {item["path"] for item in plan["remove"]})
        result = apply_cleanup(self.repo, plan, confirm="HARD")
        self.assertFalse(result["failed"])
        self.assertFalse(tree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", branch), "")
        self.assertTrue(git(self.repo, "ls-remote", "--heads", "origin", branch))

    def test_apply_cleanup_rejects_plan_outside_storage_root(self) -> None:
        outside_file = self.repo / "outside.txt"
        outside_file.write_text("keep", encoding="utf-8")
        fake_plan = {
            "mode": "soft",
            "root": str(self.repo / ".harness"),
            "min_age_hours": 0.0,
            "remove": [{"kind": "file", "path": str(outside_file)}],
            "skipped": [],
        }
        with self.assertRaises(ValueError) as ctx:
            apply_cleanup(self.repo, fake_plan)  # type: ignore[arg-type]
        self.assertIn("cleanup plan changed", str(ctx.exception))
        self.assertTrue(outside_file.exists())


if __name__ == "__main__":
    unittest.main()
