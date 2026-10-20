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
from harness.orchestration import container_executor
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger.lifecycle import DispatchStatusRecord
from tests.orchestration.test_container_executor import FakeEngine


def git(repo: Path, *args: str) -> str:
    """Выполнить команду git в указанном репозитории и вернуть stdout."""
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
    """Набор тестов для операций очистки хранилища (.harness)."""

    def setUp(self) -> None:
        """Подготовить тестовый git-репозиторий и временную директорию."""
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

    def test_soft_previews_old_runs_scratch_logs_and_legacy_dirs_but_keeps_active_run_and_ledger(
        self,
    ) -> None:
        """Проверить, что мягкая очистка включает старые запуски, scratch и логи, но сохраняет активный запуск и ledger."""
        root = self.repo / ".harness"
        sandboxes = root / ".sandboxes"

        old_run = sandboxes / "runs" / "old"
        active_run = sandboxes / "runs" / "active"
        scratch_file = sandboxes / "scratch" / "transit.md"
        publication_file = sandboxes / "pr_body" / "pr-body-1-test.md"
        log_file = sandboxes / "logs" / "session.log"
        sandboxes_cache = sandboxes / "cache" / "rebuildable.json"
        sandboxes_reports = sandboxes / "reports" / "report.html"

        legacy_cache = root / ".cache"
        legacy_tmp = root / "tmp"
        legacy_logs = root / "test-logs"
        legacy_reports = root / "reports"
        legacy_scratch = root / "scratch"
        ledger = root / "orchestration" / "state" / "ledger.json"

        for directory in (
            old_run,
            active_run,
            scratch_file.parent,
            publication_file.parent,
            log_file.parent,
            sandboxes_cache.parent,
            sandboxes_reports.parent,
            legacy_cache,
            legacy_tmp,
            legacy_logs,
            legacy_reports,
            legacy_scratch,
            ledger.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        (old_run / "artifact.txt").write_text("done", encoding="utf-8")
        (active_run / ".active.json").write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8"
        )
        scratch_file.write_text("role inbox message", encoding="utf-8")
        publication_file.write_text("transient PR body", encoding="utf-8")
        log_file.write_text("test logs", encoding="utf-8")
        sandboxes_cache.write_text("rebuildable", encoding="utf-8")
        sandboxes_reports.write_text("report", encoding="utf-8")

        (legacy_cache / "old_cache.bin").write_text("old", encoding="utf-8")
        (legacy_tmp / "old_tmp.bin").write_text("old", encoding="utf-8")
        (legacy_logs / "old.log").write_text("old", encoding="utf-8")
        (legacy_reports / "old.html").write_text("old", encoding="utf-8")
        (legacy_scratch / "old_scratch.txt").write_text("old", encoding="utf-8")
        ledger.write_text("durable", encoding="utf-8")

        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        self.assertTrue(old_run.exists())

        expected_remove = {
            str(old_run),
            str(scratch_file),
            str(publication_file),
            str(log_file),
            str(legacy_cache),
            str(legacy_tmp),
            str(legacy_logs),
            str(legacy_reports),
            str(legacy_scratch),
        }
        self.assertEqual({item["path"] for item in plan["remove"]}, expected_remove)

        result = apply_cleanup(self.repo, plan)
        self.assertFalse(result["failed"])
        self.assertFalse(old_run.exists())
        self.assertFalse(scratch_file.exists())
        self.assertFalse(publication_file.exists())
        self.assertFalse(log_file.exists())
        self.assertFalse(legacy_cache.exists())
        self.assertFalse(legacy_scratch.exists())
        self.assertFalse(legacy_tmp.exists())
        self.assertFalse(legacy_logs.exists())
        self.assertFalse(legacy_reports.exists())

        self.assertTrue(active_run.exists())
        self.assertTrue(ledger.exists())
        self.assertTrue(sandboxes_cache.exists())
        self.assertTrue(sandboxes_reports.exists())

    def test_soft_removes_a_run_whose_windows_pid_no_longer_exists(self) -> None:
        """Проверить, что мягкая очистка удаляет запуск, чей PID больше не существует."""
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
        """Проверить, что мягкая очистка не завершает активный процесс при проверке его PID."""
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
                    side_effect=AssertionError(
                        "Windows PID probe must not call os.kill"
                    ),
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
        """Проверить, что мягкая очистка удаляет директории запусков с путями длиннее MAX_PATH в Windows."""
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
        """Проверить, что жесткая очистка удаляет кэш и отчеты sandboxes и требует явного подтверждения confirm='HARD'."""
        sandboxes = self.repo / ".harness" / ".sandboxes"
        cache_item = sandboxes / "cache" / "repo_map" / "results"
        cache_item.mkdir(parents=True, exist_ok=True)
        (cache_item / "cached.json").write_text("{}", encoding="utf-8")
        registry = sandboxes / "cache" / "repo_map" / "parser_bundle" / "registry"
        registry.mkdir(parents=True)
        (registry / "bundle.lock").write_text("offline source", encoding="utf-8")

        reports_item = sandboxes / "reports" / "summary.html"
        reports_item.parent.mkdir(parents=True, exist_ok=True)
        reports_item.write_text("report", encoding="utf-8")

        soft_plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        self.assertNotIn(
            str(cache_item), {item["path"] for item in soft_plan["remove"]}
        )
        self.assertNotIn(
            str(reports_item), {item["path"] for item in soft_plan["remove"]}
        )

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
        self.assertTrue((registry / "bundle.lock").exists())
        self.assertFalse(reports_item.exists())

    def test_hard_keeps_active_and_dirty_worktree_then_removes_local_branch_only(
        self,
    ) -> None:
        """Проверить, что жесткая очистка сохраняет активные и измененные worktree и удаляет только локальную ветку."""
        remote = self.base / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)], check=True, capture_output=True
        )
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
        (state / "ledger.json").write_text(
            json.dumps({"generation": "generation-test"}), encoding="utf-8"
        )
        batch = records / "batch-test.json"
        batch.write_text(
            json.dumps(
                {
                    "state": "awaiting-approval",
                    "worktree": ".harness/.sandboxes/worktrees/issue-1-one",
                }
            ),
            encoding="utf-8",
        )
        plan = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in plan["remove"]})

        batch.write_text(
            json.dumps(
                {
                    "state": "completed",
                    "worktree": ".harness/.sandboxes/worktrees/issue-1-one",
                }
            ),
            encoding="utf-8",
        )
        (tree / "dirty.txt").write_text("keep", encoding="utf-8")
        plan = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in plan["remove"]})

        (tree / "dirty.txt").unlink()
        git(self.repo, "push", "origin", "--delete", branch)
        git(
            self.repo,
            "update-ref",
            f"refs/remotes/origin/{branch}",
            git(self.repo, "rev-parse", branch),
        )
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

    def _add_origin(self) -> None:
        """Подключить bare-remote origin и отправить на него main."""
        remote = self.base / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)], check=True, capture_output=True
        )
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-u", "origin", "main")

    def _worktrees_dir(self) -> Path:
        """Вернуть каталог управляемых worktree, создав его."""
        path = self.repo / ".harness" / ".sandboxes" / "worktrees"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_batch(self, tree: Path, state: str) -> None:
        """Записать batch-запись ledger для worktree с заданным состоянием."""
        orchestration = self.repo / ".harness" / "orchestration" / "state"
        records = orchestration / "generations" / "generation-test" / "batches"
        records.mkdir(parents=True, exist_ok=True)
        (orchestration / "ledger.json").write_text(
            json.dumps({"generation": "generation-test"}), encoding="utf-8"
        )
        (records / "batch-test.json").write_text(
            json.dumps({"state": state, "worktree": str(tree.relative_to(self.repo))}),
            encoding="utf-8",
        )

    def test_hard_removes_clean_integration_worktree_only_when_pushed(self) -> None:
        """Проверить, что worktree с `integration/*` удаляется только после push на origin."""
        self._add_origin()
        tree = self._worktrees_dir() / "project-tracker"
        branch = "integration/project-tracker"
        git(self.repo, "worktree", "add", "-b", branch, str(tree), "HEAD")
        unpushed = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in unpushed["remove"]})

        git(self.repo, "push", "-u", "origin", branch)
        self._write_batch(tree, "awaiting-approval")
        active = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in active["remove"]})

        self._write_batch(tree, "completed")
        plan = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertIn(str(tree), {item["path"] for item in plan["remove"]})
        result = apply_cleanup(self.repo, plan, confirm="HARD")
        self.assertFalse(result["failed"])
        self.assertFalse(tree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", branch), "")
        self.assertTrue(git(self.repo, "ls-remote", "--heads", "origin", branch))

    def test_hard_keeps_integration_worktree_with_unrelated_branch_name(self) -> None:
        """Проверить, что ветка вне `feature/issue-*` и `integration/*` не удаляется."""
        self._add_origin()
        tree = self._worktrees_dir() / "random"
        git(self.repo, "worktree", "add", "-b", "scratch/random", str(tree), "HEAD")
        git(self.repo, "push", "-u", "origin", "scratch/random")
        plan = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in plan["remove"]})

    def test_hard_removes_clean_detached_worktree_only_when_head_is_on_origin(
        self,
    ) -> None:
        """Проверить, что чистый detached HEAD worktree удаляется только если коммит есть на origin."""
        self._add_origin()
        tree = self._worktrees_dir() / "review"
        git(self.repo, "worktree", "add", "--detach", str(tree), "HEAD")
        (tree / "local.txt").write_text("local", encoding="utf-8")
        git(tree, "add", "local.txt")
        git(tree, "commit", "-m", "test: local-only commit")
        local_only = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in local_only["remove"]})

        git(
            self.repo,
            "push",
            "origin",
            f"{git(tree, 'rev-parse', 'HEAD')}:refs/heads/review",
        )
        git(self.repo, "fetch", "origin")
        (tree / "dirty.txt").write_text("keep", encoding="utf-8")
        dirty = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in dirty["remove"]})

        (tree / "dirty.txt").unlink()
        newer = plan_cleanup(self.repo, "hard", min_age_hours=24 * 365)
        self.assertNotIn(str(tree), {item["path"] for item in newer["remove"]})
        self._write_batch(tree, "awaiting-approval")
        active = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertNotIn(str(tree), {item["path"] for item in active["remove"]})
        self._write_batch(tree, "completed")
        plan = plan_cleanup(self.repo, "hard", min_age_hours=0)
        self.assertIn(str(tree), {item["path"] for item in plan["remove"]})
        result = apply_cleanup(self.repo, plan, confirm="HARD")
        self.assertFalse(result["failed"])
        self.assertFalse(tree.exists())
        self.assertTrue(git(self.repo, "ls-remote", "--heads", "origin", "review"))

    def test_apply_cleanup_rejects_plan_outside_storage_root(self) -> None:
        """Проверить, что применение плана очистки отклоняет пути за пределами корня хранилища."""
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


class ContainerCleanupTests(unittest.TestCase):
    """Orphaned container resources of this repository: preview first, evidence before removal."""

    def setUp(self) -> None:
        """Подготовить репозиторий с docker-исполнителем, ledger и скриптованный движок."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / ".gitignore").write_text("/.harness/\n", encoding="utf-8")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-m", "test: initial fixture")
        self.root = self.repo / ".harness"
        self.root.mkdir()
        self.write_config({"extensions": {"runtime_access": "docker"}})
        self.repository = str((self.repo / ".git").resolve())
        self.engine = FakeEngine()
        patcher = mock.patch.object(container_executor, "run_engine", self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, config: dict[str, object]) -> None:
        """Записать orchestration.json проекта."""
        (self.root / "orchestration.json").write_text(
            json.dumps(config), encoding="utf-8"
        )

    def dispatch(
        self,
        number: int,
        worker: str = "exited",
        *,
        repository: str | None = None,
        sidecar: bool = True,
    ) -> str:
        """Добавить в движок worker, sidecar и сеть dispatch; вернуть id dispatch."""
        dispatch_id = f"dispatch-{number:04d}"
        labels = {
            "harness.dispatch": dispatch_id,
            "harness.repository": repository or self.repository,
        }
        name = f"harness-{dispatch_id}"
        self.engine.add_container(f"{number:02d}" * 32, name, labels, worker)
        if sidecar:
            self.engine.add_container(
                f"{number:02d}" * 31 + "aa", f"{name}-proxy", labels, "running"
            )
            self.engine.objects[f"{number:02d}" * 31 + "bb"] = {
                "kind": "network",
                "id": f"{number:02d}" * 31 + "bb",
                "name": f"{name}-net",
                "labels": labels,
            }
        return dispatch_id

    def record(self, dispatch_id: str, state: str, **extra: JsonObject) -> Path:
        """Записать статус dispatch в текущее поколение ledger."""
        state_dir = self.root / "orchestration" / "state"
        records = state_dir / "generations" / "generation-test" / "dispatch-status"
        records.mkdir(parents=True, exist_ok=True)
        (state_dir / "ledger.json").write_text(
            json.dumps({"generation": "generation-test"}), encoding="utf-8"
        )
        path = records / f"{dispatch_id}.json"
        path.write_text(
            json.dumps(
                DispatchStatusRecord.from_dict(
                    {"dispatch_id": dispatch_id, "state": state, **extra}
                ).to_dict()
            ),
            encoding="utf-8",
        )
        return path

    def planned(self) -> list[str]:
        """Имена ресурсов контейнеров в плане."""
        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        return [
            item["path"]
            for item in plan["remove"]
            if item["kind"] in {"container", "network"}
        ]

    def skipped(self) -> dict[str, str]:
        """Причины пропуска по имени."""
        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        return {item["path"]: item["reason"] for item in plan["skipped"]}

    def test_an_orphan_of_this_repository_is_planned_and_a_foreign_one_never(
        self,
    ) -> None:
        """Проверить, что в план попадает только сирота этого репозитория, по порядку worker, sidecar, сеть."""
        own = self.dispatch(1)
        self.dispatch(2, repository="/elsewhere/.git")

        self.assertEqual(
            self.planned(),
            [f"harness-{own}", f"harness-{own}-proxy", f"harness-{own}-net"],
        )
        self.assertEqual(self.skipped(), {})

    def test_a_label_that_only_the_engine_filter_would_match_is_rechecked(
        self,
    ) -> None:
        """Проверить, что метку репозитория проверяют по самому объекту, а не по фильтру движка."""
        self.dispatch(1, repository="/elsewhere/.git")
        self.engine.ignore_label_filters = True

        self.assertEqual(self.planned(), [])

    def test_a_worker_that_is_not_stopped_protects_its_whole_dispatch(self) -> None:
        """Проверить, что живой worker защищает все ресурсы dispatch и cleanup его не останавливает."""
        live = self.dispatch(1, worker="running")

        self.assertEqual(self.planned(), [])
        self.assertIn("docker stop", self.skipped()[f"harness-{live}"])
        result = apply_cleanup(
            self.repo, plan_cleanup(self.repo, "soft", min_age_hours=0)
        )
        self.assertEqual(result["removed"], [])
        self.assertEqual(self.engine.removals(), [])

    def test_the_ledger_decides_about_a_known_dispatch(self) -> None:
        """Проверить правила для открытого dispatch и терминального без evidence."""
        open_ = self.dispatch(1)
        self.record(open_, "working")
        bare = self.dispatch(2)
        self.record(bare, "reported")
        proven = self.dispatch(3)
        self.record(proven, "reported", worker_exit={"state": "exited"})

        self.assertEqual(
            self.planned(),
            [f"harness-{proven}", f"harness-{proven}-proxy", f"harness-{proven}-net"],
        )
        skipped = self.skipped()
        self.assertEqual(skipped[f"harness-{open_}"], "dispatch is open in the ledger")
        self.assertIn("dispatch status first", skipped[f"harness-{bare}"])

    def test_a_terminal_dispatch_whose_worker_is_gone_needs_no_evidence_to_release_the_rest(
        self,
    ) -> None:
        """Проверить, что без worker evidence не требуется для удаления sidecar и сети."""
        gone = self.dispatch(1)
        self.record(gone, "cancelled")
        del self.engine.objects["01" * 32]

        self.assertEqual(
            self.planned(), [f"harness-{gone}-proxy", f"harness-{gone}-net"]
        )

    def test_apply_removes_the_orphans_and_keeps_persisted_evidence(self) -> None:
        """Проверить удаление сирот, сохранность worker_exit и безопасность повторного запуска."""
        proven = self.dispatch(1)
        record = self.record(proven, "reported", worker_exit={"exit_code": 3})
        before = record.read_bytes()
        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)

        result = apply_cleanup(self.repo, plan)

        self.assertEqual(result["failed"], [])
        self.assertEqual(
            result["removed"],
            [f"harness-{proven}", f"harness-{proven}-proxy", f"harness-{proven}-net"],
        )
        self.assertEqual(self.engine.objects, {})
        self.assertEqual(record.read_bytes(), before)
        self.engine.calls.clear()
        again = plan_cleanup(self.repo, "soft", min_age_hours=0)
        self.assertEqual(again["remove"], [])
        self.assertEqual(
            apply_cleanup(self.repo, again),
            {"removed": [], "failed": [], "skipped": []},
        )
        self.assertEqual(self.engine.removals(), [])

    def test_apply_refuses_a_plan_that_changed_after_the_preview(self) -> None:
        """Проверить, что изменившийся набор ресурсов или состояние worker требуют нового просмотра."""
        self.dispatch(1)
        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        self.engine.objects["01" * 32]["status"] = "running"

        with self.assertRaises(ValueError) as refused:
            apply_cleanup(self.repo, plan)

        self.assertIn("cleanup plan changed", str(refused.exception))
        self.assertEqual(self.engine.removals(), [])

    def test_a_resource_that_is_already_gone_counts_as_removed(self) -> None:
        """Проверить, что отсутствующий ресурс не считается ошибкой, а отказ движка считается."""
        orphan = self.dispatch(1)
        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        self.engine.failures["network-rm"] = "Error: No such network: x"

        result = apply_cleanup(self.repo, plan)

        self.assertEqual(result["failed"], [])
        self.assertIn(f"harness-{orphan}-net", result["removed"])

        refused = self.dispatch(2)
        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)
        self.engine.failures.clear()
        self.engine.failures["rm"] = "container is in use"

        result = apply_cleanup(self.repo, plan)

        self.assertEqual(
            [item["path"] for item in result["failed"]][:1], [f"harness-{refused}"]
        )

    def test_a_project_without_the_docker_executor_never_reaches_the_engine(
        self,
    ) -> None:
        """Проверить, что без выбранного docker-исполнителя движок не опрашивается."""
        self.dispatch(1)
        self.write_config({})

        plan = plan_cleanup(self.repo, "soft", min_age_hours=0)

        self.assertEqual(plan["remove"], [])
        self.assertEqual(self.engine.calls, [])

    def test_an_unavailable_engine_or_ledger_plans_nothing(self) -> None:
        """Проверить пропуск при недоступном движке и нечитаемом ledger."""
        self.dispatch(1)
        self.engine.failures["ps"] = OSError("docker vanished")
        self.assertEqual(
            self.skipped(), {"container resources": "container engine unavailable"}
        )
        self.assertEqual(self.planned(), [])

        self.engine.failures.clear()
        state = self.root / "orchestration" / "state"
        state.mkdir(parents=True)
        (state / "ledger.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(
            self.skipped(), {"container resources": "Git or ledger state unavailable"}
        )
        self.assertEqual(self.planned(), [])


if __name__ == "__main__":
    unittest.main()
