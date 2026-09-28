"""`harness uninstall` removes everything init installed and keeps what the project changed."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.uninstall import BACKUP_DIR, CONFIRM_WORD, apply_uninstall, plan_uninstall

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "harness" / "bin" / "harness.py"


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


def harness(*args: str) -> subprocess.CompletedProcess[str]:
    """Запустить CLI харнесса без интерактивного ввода."""
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        check=False,
    )


class UninstallTests(unittest.TestCase):
    """Полное удаление харнесса из проекта, установленного через `init`."""

    def setUp(self) -> None:
        """Создать git-репозиторий и установить в него харнесс."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-m", "test: initial fixture")
        installed = harness(
            "init",
            str(self.repo),
            "--capability",
            "pvmalove-suite",
            "--capability",
            "backend-orchestration",
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def _project_files(self) -> set[str]:
        """Все файлы рабочей копии, кроме `.git` и резервной копии удаления."""
        return {
            path.relative_to(self.repo).as_posix()
            for path in self.repo.rglob("*")
            if (path.is_file() or path.is_symlink())
            and ".git" not in path.relative_to(self.repo).parts[:1]
            and BACKUP_DIR not in path.relative_to(self.repo).parts[:1]
        }

    def test_uninstall_restores_the_project_and_backs_up_its_own_changes(
        self,
    ) -> None:
        """Проверить, что удаление возвращает проект к исходному виду и сохраняет его изменения."""
        rule = next((self.repo / ".claude" / "rules").glob("*.md"))
        rule.write_text("project edit\n", encoding="utf-8")
        own_skill = self.repo / ".harness" / "skills" / "my-skill" / "SKILL.md"
        own_skill.parent.mkdir(parents=True)
        own_skill.write_text("# mine\n", encoding="utf-8")

        plan = plan_uninstall(self.repo)
        self.assertIsNone(plan["blocked"])
        self.assertIn(rule.relative_to(self.repo).as_posix(), plan["backup"])
        self.assertIn(".harness/skills/my-skill/SKILL.md", plan["backup"])
        self.assertIn(".harness/project.json", plan["backup"])
        self.assertNotIn("CLAUDE.md", plan["backup"])
        self.assertTrue((self.repo / ".harness").exists())

        result = apply_uninstall(self.repo, plan, confirm=CONFIRM_WORD)

        self.assertEqual(result["failed"], [])
        self.assertEqual(self._project_files(), {".gitignore"})
        self.assertEqual(
            (self.repo / ".gitignore").read_text(encoding="utf-8"), "node_modules/\n"
        )
        backup = Path(result["backup_dir"] or "")
        self.assertEqual(
            (backup / rule.relative_to(self.repo)).read_text(encoding="utf-8"),
            "project edit\n",
        )
        self.assertEqual(
            (backup / ".harness/skills/my-skill/SKILL.md").read_text(encoding="utf-8"),
            "# mine\n",
        )
        self.assertEqual(plan_uninstall(self.repo)["remove"], [])

    def test_uninstall_refuses_without_confirmation_or_with_a_stale_plan(self) -> None:
        """Проверить отказ без слова подтверждения и при изменившемся плане."""
        plan = plan_uninstall(self.repo)
        with self.assertRaisesRegex(ValueError, CONFIRM_WORD):
            apply_uninstall(self.repo, plan, confirm="yes")
        (self.repo / "AGENTS.md").unlink()
        with self.assertRaisesRegex(ValueError, "preview again"):
            apply_uninstall(self.repo, plan, confirm=CONFIRM_WORD)
        self.assertTrue((self.repo / ".harness").is_dir())

    def test_uninstall_is_blocked_while_a_batch_is_active(self) -> None:
        """Проверить, что активный batch оркестрации блокирует удаление."""
        batches = self.repo / ".harness" / "orchestration" / "state" / "batches"
        batches.mkdir(parents=True)
        (batches / "batch-1.json").write_text(
            json.dumps({"state": "active", "worktree": ".harness/.sandboxes/wt"}),
            encoding="utf-8",
        )
        plan = plan_uninstall(self.repo)
        self.assertIn("активные batch", plan["blocked"] or "")
        with self.assertRaisesRegex(ValueError, "активные batch"):
            apply_uninstall(self.repo, plan, confirm=CONFIRM_WORD)
        self.assertTrue((self.repo / ".harness").is_dir())

    def test_cli_previews_by_default_and_requires_the_confirm_word(self) -> None:
        """Проверить, что CLI без --apply только показывает план, а --apply требует UNINSTALL."""
        preview = harness("uninstall", str(self.repo))
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn(".harness", json.loads(preview.stdout)["plan"]["remove"][0]["path"])
        self.assertTrue((self.repo / ".harness").is_dir())

        refused = harness("uninstall", str(self.repo), "--apply")
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue((self.repo / ".harness").is_dir())

        applied = harness(
            "uninstall", str(self.repo), "--apply", "--confirm", CONFIRM_WORD
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertFalse((self.repo / ".harness").exists())


if __name__ == "__main__":
    unittest.main()
