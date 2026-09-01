import subprocess
import sys
import tempfile
import textwrap
import unittest
import json
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

                [workflows.feature-development]
                steps = ["implement"]
                """,
            )

            result = self.run_cli(repository, "skill", "explain", "implement")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("limited: rejected; missing capabilities: git", result.stdout)
            self.assertIn("Selected: complete -> custom", result.stdout)

            plan = self.run_cli(repository, "workflow", "plan", "feature-development")

            self.assertEqual(plan.returncode, 0, plan.stderr)
            self.assertIn("reason: Highest overall score", plan.stdout)
            self.assertIn("rejected limited: missing capabilities: git", plan.stdout)

    def test_skill_run_uses_the_resolved_capabilities_in_the_provider_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                import sys

                request = json.loads(sys.stdin.readline())
                assert request["jsonrpc"] == "2.0"
                assert request["method"] == "execute"
                assert request["params"]["protocol"] == "harness.provider"
                assert request["params"]["version"] == 1
                assert request["params"]["input"] == {"file": "main.py"}
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocol": "harness.provider",
                        "version": 1,
                        "status": "SUCCESS",
                        "output": {"echo": request["params"]["input"]},
                    },
                }), flush=True)
            """
            self.write_config(
                repository,
                f"""
                [providers.fixture]
                type = "cli"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(provider_program)}]

                [workers.coder]
                provider = "fixture"
                capabilities = ["filesystem"]

                [skills.implement]
                requires = ["filesystem"]
                """,
            )

            result = self.run_cli(
                repository,
                "skill",
                "run",
                "implement",
                "--input",
                '{"file":"main.py"}',
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Execution Status: SUCCESS", result.stdout)
            self.assertIn(
                'Execution Output: {"echo": {"file": "main.py"}}', result.stdout
            )

    def test_skill_run_reports_timeout_from_the_declared_provider_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = "import time; time.sleep(1)"
            self.write_config(
                repository,
                f"""
                [providers.fixture]
                type = "cli"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(provider_program)}]
                timeout = 0.05

                [workers.coder]
                provider = "fixture"
                capabilities = ["filesystem"]

                [skills.implement]
                requires = ["filesystem"]
                """,
            )

            result = self.run_cli(repository, "skill", "run", "implement")

            self.assertEqual(result.returncode, 1)
            self.assertIn("Execution Status: TIMEOUT", result.stdout)
            self.assertIn("timed out", result.stdout)

    def test_skill_run_rejects_an_invalid_provider_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = "print('{\"status\": \"SUCCESS\"}', flush=True)"
            self.write_config(
                repository,
                f"""
                [providers.fixture]
                type = "cli"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(provider_program)}]

                [workers.coder]
                provider = "fixture"
                capabilities = ["filesystem"]

                [skills.implement]
                requires = ["filesystem"]
                """,
            )

            result = self.run_cli(repository, "skill", "run", "implement")

            self.assertEqual(result.returncode, 1)
            self.assertIn("Execution Status: FAILED", result.stdout)
            self.assertIn("Protocol error", result.stdout)

    def test_skill_run_rejects_a_provider_that_closes_without_a_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = "import sys; sys.stdin.readline()"
            self.write_config(
                repository,
                f"""
                [providers.fixture]
                type = "cli"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(provider_program)}]

                [workers.coder]
                provider = "fixture"
                capabilities = ["filesystem"]

                [skills.implement]
                requires = ["filesystem"]
                """,
            )

            result = self.run_cli(repository, "skill", "run", "implement")

            self.assertEqual(result.returncode, 1)
            self.assertIn("Execution Status: FAILED", result.stdout)
            self.assertIn("without a terminal result", result.stdout)

    def test_explain_reports_delegation_and_depth_policy_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            self.write_config(
                repository,
                """
                [providers.fixture]
                type = "cli"
                command = "fixture-agent"

                [workers.coder]
                provider = "fixture"
                capabilities = ["filesystem"]

                [skills.implement]
                requires = ["filesystem"]

                [[policies.delegation.coordinator.allow]]
                worker = "coder"
                skills = ["implement"]

                [policies.limits]
                max_depth = 1

                [workflows.feature-development]
                steps = ["implement"]
                """,
            )

            unauthorized = self.run_cli(
                repository, "skill", "explain", "implement", "--caller", "auditor"
            )
            self.assertEqual(unauthorized.returncode, 1)
            self.assertIn("coder: rejected;", unauthorized.stdout)
            self.assertIn("DELEGATION_DENIED", unauthorized.stdout)
            self.assertIn("Selected: NONE", unauthorized.stdout)

            too_deep = self.run_cli(
                repository,
                "skill",
                "explain",
                "implement",
                "--caller",
                "coordinator",
                "--depth",
                "2",
            )
            self.assertEqual(too_deep.returncode, 1)
            self.assertIn("DEPTH_LIMIT_EXCEEDED", too_deep.stdout)
            self.assertIn("depth 2 exceeds configured maximum 1", too_deep.stdout)

            denied_run = self.run_cli(
                repository, "skill", "run", "implement", "--caller", "auditor"
            )
            self.assertEqual(denied_run.returncode, 1)
            self.assertIn('"code": "DELEGATION_DENIED"', denied_run.stdout)
            self.assertIn('"rejections": {"coder":', denied_run.stdout)

            spoofed_system = self.run_cli(
                repository, "skill", "run", "implement", "--caller", "SYSTEM"
            )
            self.assertEqual(spoofed_system.returncode, 1)
            self.assertIn('"code": "DELEGATION_DENIED"', spoofed_system.stdout)

            denied_plan = self.run_cli(
                repository,
                "workflow",
                "plan",
                "feature-development",
                "--caller",
                "auditor",
            )
            self.assertEqual(denied_plan.returncode, 1)
            self.assertIn("error: DELEGATION_DENIED", denied_plan.stdout)

            denied_workflow = self.run_cli(
                repository,
                "workflow",
                "run",
                "feature-development",
                "--caller",
                "auditor",
            )
            self.assertEqual(denied_workflow.returncode, 1)
            self.assertIn('"code": "DELEGATION_DENIED"', denied_workflow.stdout)

    def test_explain_skips_an_unhealthy_preferred_worker_for_a_healthy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            self.write_config(
                repository,
                """
                [providers.fixture]
                type = "cli"
                command = "fixture-agent"

                [workers.preferred]
                provider = "fixture"
                capabilities = ["filesystem"]
                health = "unhealthy"

                [workers.fallback]
                provider = "fixture"
                capabilities = ["filesystem"]

                [skills.implement]
                requires = ["filesystem"]

                [skills.implement.execution]
                preferred = ["preferred"]
                """,
            )

            result = self.run_cli(repository, "skill", "explain", "implement")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("preferred: rejected; unhealthy", result.stdout)
            self.assertIn("Selected: fallback -> fixture", result.stdout)

            invalid_health = repository / ".harness" / "orchestration.toml"
            invalid_health.write_text(
                invalid_health.read_text(encoding="utf-8").replace(
                    'health = "unhealthy"', "health = true"
                ),
                encoding="utf-8",
            )
            invalid = self.run_cli(repository, "skill", "explain", "implement")
            self.assertEqual(invalid.returncode, 1)
            self.assertIn("health must be 'healthy' or 'unhealthy'", invalid.stdout)

    def test_skill_run_retries_a_failed_provider_until_it_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                from pathlib import Path
                import sys

                request = json.loads(sys.stdin.readline())
                attempts_file = Path("attempts.txt")
                attempt = int(attempts_file.read_text()) + 1 if attempts_file.exists() else 1
                attempts_file.write_text(str(attempt))
                if attempt == 1:
                    result = {"status": "FAILED", "error": "transient failure"}
                else:
                    result = {"status": "SUCCESS", "output": {"attempt": attempt}}
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocol": "harness.provider",
                        "version": 1,
                        **result,
                    },
                }), flush=True)
                sys.exit(0)
                """
            self.write_config(
                repository,
                f"""
                [providers.fixture]
                type = "cli"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(textwrap.dedent(provider_program))}]

                [providers.fixture.retry]
                max_attempts = 2

                [workers.coder]
                provider = "fixture"
                capabilities = ["filesystem"]

                [skills.implement]
                requires = ["filesystem"]
                """,
            )

            result = self.run_cli(repository, "skill", "run", "implement")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Execution Output: {"attempt": 2}', result.stdout)

    def test_parallel_workflow_is_bounded_by_max_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                import time
                from pathlib import Path

                request = json.loads(__import__("sys").stdin.readline())
                marker = Path("active-provider")
                try:
                    marker.mkdir()
                except FileExistsError:
                    result = {"status": "FAILED", "error": "parallelism limit exceeded"}
                else:
                    time.sleep(0.15)
                    marker.rmdir()
                    result = {"status": "SUCCESS", "output": {"bounded": True}}
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocol": "harness.provider",
                        "version": 1,
                        **result,
                    },
                }), flush=True)
                """
            self.write_config(
                repository,
                f"""
                [runtime]
                max_parallel = 1

                [providers.fixture]
                type = "cli"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(textwrap.dedent(provider_program))}]

                [workers.coder]
                provider = "fixture"
                capabilities = ["filesystem"]

                [skills.first]
                requires = ["filesystem"]

                [skills.second]
                requires = ["filesystem"]

                [workflows.parallel]
                steps = ["first", "second"]
                parallel = true
                """,
            )

            result = self.run_cli(repository, "workflow", "run", "parallel")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Workflow completed.", result.stdout)

    def test_parallel_workflow_runs_independent_steps_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                import time
                from pathlib import Path

                request = json.loads(__import__("sys").stdin.readline())
                marker = Path("active-provider")
                overlap = Path("overlap-seen")
                try:
                    marker.mkdir()
                except FileExistsError:
                    overlap.write_text("yes")
                    result = {"status": "SUCCESS", "output": {"overlap": True}}
                else:
                    time.sleep(0.15)
                    result = (
                        {"status": "SUCCESS", "output": {"overlap": True}}
                        if overlap.exists()
                        else {"status": "FAILED", "error": "steps were not concurrent"}
                    )
                    marker.rmdir()
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocol": "harness.provider",
                        "version": 1,
                        **result,
                    },
                }), flush=True)
                """
            self.write_config(
                repository,
                f"""
                [runtime]
                max_parallel = 2

                [providers.fixture]
                type = "cli"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(textwrap.dedent(provider_program))}]

                [workers.coder]
                provider = "fixture"
                capabilities = ["filesystem"]

                [skills.first]
                requires = ["filesystem"]

                [skills.second]
                requires = ["filesystem"]

                [workflows.parallel]
                steps = ["first", "second"]
                parallel = true
                """,
            )

            result = self.run_cli(repository, "workflow", "run", "parallel")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Workflow completed.", result.stdout)


if __name__ == "__main__":
    unittest.main()
