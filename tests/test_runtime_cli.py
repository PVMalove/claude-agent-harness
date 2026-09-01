import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RuntimeCliTests(unittest.TestCase):
    def run_cli(self, repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "harness.runtime.cli", "--repo", str(repository), *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def write_config(self, repository: Path, content: str) -> None:
        config_directory = repository / ".harness"
        config_directory.mkdir()
        (config_directory / "orchestration.toml").write_text(
            textwrap.dedent(content), encoding="utf-8"
        )

    def test_plan_rejects_worker_that_references_an_unknown_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            self.write_config(
                repository,
                """
                [workers.coder]
                provider = "missing"
                capabilities = ["filesystem"]

                [skills.implement]
                requires = ["filesystem"]

                [workflows.feature-development]
                steps = ["implement"]
                """,
            )

            result = self.run_cli(repository, "workflow", "plan", "feature-development")

            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "Worker 'coder' references unknown provider 'missing'", result.stdout
            )

    def test_explain_reports_capability_rejection_and_selected_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            self.write_config(
                repository,
                """
                [providers.custom]
                type = "cli"
                command = "custom-agent"

                [workers.limited]
                provider = "custom"
                capabilities = ["filesystem"]

                [workers.complete]
                provider = "custom"
                capabilities = ["filesystem", "git"]

                [skills.implement]
                requires = ["filesystem", "git"]

                [skills.implement.execution]
                preferred = ["complete"]
                """,
            )

            result = self.run_cli(repository, "skill", "explain", "implement")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("limited: rejected; missing capabilities: git", result.stdout)
            self.assertIn("Selected: complete -> custom", result.stdout)


if __name__ == "__main__":
    unittest.main()
