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

    def test_soft_previews_old_temp_and_cache_but_keeps_active_run_and_ledger(self) -> None:
        root = self.repo / ".harness"
        old = root / "tmp" / "tests" / "old"
        active = root / "tmp" / "tests" / "active"
        cache = root / ".cache" / "repo_map" / "results" / "old.json"
        ledger = root / "orchestration" / "state" / "ledger.json"
        for directory in (old, active, cache.parent, ledger.parent):
            directory.mkdir(parents=True, exist_ok=True)
        (old / "artifact.txt").write_text("done", encoding="utf-8")
        (active / ".active.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        cache.write_text("rebuildable", encoding="utf-8")
        ledger.write_text("durable", encoding="utf-8")

        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        self.assertTrue(old.exists())
        self.assertEqual({item["path"] for item in plan["remove"]}, {str(old), str(cache)})
        result = apply_cleanup(self.repo, plan)
        self.assertFalse(result["failed"])
        self.assertFalse(old.exists())
        self.assertFalse(cache.exists())
        self.assertTrue(active.exists())
        self.assertTrue(ledger.exists())

    def test_soft_removes_a_run_whose_windows_pid_no_longer_exists(self) -> None:
        stale = self.repo / ".harness" / "tmp" / "tests" / "stale"
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
        active = self.repo / ".harness" / "tmp" / "tests" / "active-child"
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
        stale = self.repo / ".harness" / "tmp" / "tests" / "long-path"
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

    def test_hard_keeps_active_and_dirty_worktree_then_removes_local_branch_only(self) -> None:
        remote = self.base / "origin.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-u", "origin", "main")
        tree = self.repo / ".harness" / "worktrees" / "issue-1-one"
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
        batch.write_text(json.dumps({"state": "awaiting-approval", "worktree": ".harness/worktrees/issue-1-one"}), encoding="utf-8")
        plan = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in plan["remove"]})

        batch.write_text(json.dumps({"state": "completed", "worktree": ".harness/worktrees/issue-1-one"}), encoding="utf-8")
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
        result = apply_cleanup(self.repo, plan)
        self.assertFalse(result["failed"])
        self.assertFalse(tree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", branch), "")
        self.assertTrue(git(self.repo, "ls-remote", "--heads", "origin", branch))


if __name__ == "__main__":
    unittest.main()
