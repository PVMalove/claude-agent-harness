"""The packager must inspect explicit Git roots safely in a Windows sandbox."""

import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLI = runpy.run_path(str(Path(__file__).resolve().parents[1] / "harness" / "bin" / "harness"))


class HarnessGitTests(unittest.TestCase):
    def test_root_gitignore_covering_harness_does_not_gain_redundant_entries(self) -> None:
        self.assertEqual(CLI["missing_runtime_gitignore_lines"](".harness/\n/docs/tasks/\n"), [])

    def test_git_probes_trust_only_the_explicit_root_and_decode_localized_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            calls: list[list[str]] = []

            def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                self.assertEqual(command[:5], ["git", "-c", f"safe.directory={repo}", "-C", str(repo)])
                self.assertEqual(options.get("encoding"), "utf-8")
                self.assertEqual(options.get("errors"), "replace")
                return subprocess.CompletedProcess(command, 0, str(repo), "")

            with mock.patch("subprocess.run", side_effect=run):
                CLI["ensure_git_repo"](repo)

            self.assertEqual(len(calls), 1)

    def test_project_skill_inventory_trusts_only_the_explicit_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            skill = repo / "local" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("test", encoding="utf-8")

            def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
                self.assertEqual(command[:5], ["git", "-c", f"safe.directory={repo}", "-C", str(repo)])
                return subprocess.CompletedProcess(command, 0, b"local/SKILL.md\0", b"")

            with mock.patch("subprocess.run", side_effect=run):
                self.assertEqual(CLI["project_skill_files"](repo, skill.parent), [skill])

    def test_source_revision_trusts_its_source_checkout(self) -> None:
        source = CLI["ROOT"]

        def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[:5], ["git", "-c", f"safe.directory={source}", "-C", str(source)])
            self.assertEqual(options.get("encoding"), "utf-8")
            self.assertEqual(options.get("errors"), "replace")
            return subprocess.CompletedProcess(command, 0, "123abc\n", "")

        with mock.patch("subprocess.run", side_effect=run):
            self.assertEqual(CLI["source_revision"](), "123abc")


if __name__ == "__main__":
    unittest.main()
