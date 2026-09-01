import subprocess
import sys
import tempfile
import textwrap
import unittest
import json
import re
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

    def write_feature_development_config(
        self,
        repository: Path,
        provider_program: str,
        *,
        steps: str = '["grill-with-docs", "to-spec", "to-tickets", "implement"]',
        quality_phases: str | None = '["tdd", "code-review", "qa-gate"]',
    ) -> None:
        quality_config = (
            f"""
                [skills.implement.quality]
                required = {quality_phases}
            """
            if quality_phases is not None
            else ""
        )
        self.write_config(
            repository,
            f"""
            [providers.fixture]
            type = "cli"
            command = {json.dumps(sys.executable)}
            args = ["-c", {json.dumps(textwrap.dedent(provider_program))}]

            [workers.coder]
            provider = "fixture"
            capabilities = ["filesystem"]

            [skills.grill-with-docs]
            requires = ["filesystem"]

            [skills.to-spec]
            requires = ["filesystem"]

            [skills.to-tickets]
            requires = ["filesystem"]

            [skills.implement]
            requires = ["filesystem"]
            {quality_config}
            [workflows.feature-development]
            steps = {steps}

            [workflows.feature-development.mappings.to-spec]
            context_id = "grill-with-docs.output.context_id"

            [workflows.feature-development.mappings.to-tickets]
            spec_file = "to-spec.output.spec_file"

            [workflows.feature-development.mappings.implement]
            ticket_id = "to-tickets.output.ticket_id"
            """,
        )

    def test_feature_development_forwards_context_and_quality_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                from pathlib import Path
                import sys

                request = json.loads(sys.stdin.readline())
                with Path("requests.jsonl").open("a", encoding="utf-8") as requests:
                    requests.write(json.dumps(request) + "\\n")
                outputs = {
                    "grill-with-docs": {"context_id": "ctx-42"},
                    "to-spec": {"spec_file": "docs/tasks/42.md"},
                    "to-tickets": {"ticket_id": "42"},
                    "implement": {
                        "quality_status": {
                            "tdd": "passed",
                            "code-review": "passed",
                            "qa-gate": "passed",
                        }
                    },
                }
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocol": "harness.provider",
                        "version": 1,
                        "status": "SUCCESS",
                        "output": outputs[request["params"]["skill"]],
                    },
                }), flush=True)
            """
            self.write_feature_development_config(
                repository,
                provider_program,
            )

            shown = self.run_cli(repository, "workflow", "show", "feature-development")
            self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
            self.assertEqual(
                shown.stdout.splitlines()[2:6],
                [
                    "1. grill-with-docs",
                    "2. to-spec",
                    "3. to-tickets",
                    "4. implement",
                ],
            )
            self.assertNotIn("tdd", shown.stdout)
            self.assertNotIn("code-review", shown.stdout)
            self.assertNotIn("qa-gate", shown.stdout)

            result = self.run_cli(
                repository,
                "workflow",
                "run",
                "feature-development",
                "--input",
                '{"idea":"ship feature","private":"do-not-forward"}',
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            execution_id = re.search(r"Execution ID: ([0-9a-f-]+)", result.stdout).group(1)
            state = self.run_cli(repository, "workflow", "status", execution_id)
            self.assertEqual(state.returncode, 0, state.stdout + state.stderr)
            self.assertIn('"quality_status": {', state.stdout)
            for phase in ("tdd", "code-review", "qa-gate"):
                self.assertIn(f'"{phase}": "passed"', state.stdout)

            requests = [
                json.loads(line)
                for line in (repository / "requests.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [request["params"]["input"] for request in requests],
                [
                    {"idea": "ship feature", "private": "do-not-forward"},
                    {"context_id": "ctx-42"},
                    {"spec_file": "docs/tasks/42.md"},
                    {"ticket_id": "42"},
                ],
            )
            self.assertEqual(
                [request["params"]["skill"] for request in requests],
                ["grill-with-docs", "to-spec", "to-tickets", "implement"],
            )

    def test_feature_development_rejects_an_incomplete_quality_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                import sys

                request = json.loads(sys.stdin.readline())
                outputs = {
                    "grill-with-docs": {"context_id": "ctx-42"},
                    "to-spec": {"spec_file": "docs/tasks/42.md"},
                    "to-tickets": {"ticket_id": "42"},
                    "implement": {"quality_status": {"tdd": "passed"}},
                }
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocol": "harness.provider",
                        "version": 1,
                        "status": "SUCCESS",
                        "output": outputs[request["params"]["skill"]],
                    },
                }), flush=True)
            """
            self.write_feature_development_config(
                repository,
                provider_program,
            )

            result = self.run_cli(
                repository,
                "workflow",
                "run",
                "feature-development",
                "--input",
                '{"ticket_id":"42"}',
            )

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            execution_id = re.search(r"Execution ID: ([0-9a-f-]+)", result.stdout).group(1)
            state = self.run_cli(repository, "workflow", "status", execution_id)
            self.assertEqual(state.returncode, 0, state.stdout + state.stderr)
            self.assertIn('"status": "FAILED"', state.stdout)
            self.assertIn('"code": "QUALITY_CONTRACT_FAILED"', state.stdout)
            self.assertIn('"failed_phases": ["code-review", "qa-gate"]', state.stdout)

    def test_feature_development_requires_its_quality_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            self.write_feature_development_config(
                repository,
                "pass",
                quality_phases=None,
            )

            result = self.run_cli(repository, "workflow", "show", "feature-development")

            self.assertEqual(result.returncode, 1)
            self.assertIn("must require quality phases", result.stdout)

    def test_feature_development_rejects_quality_phases_as_workflow_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            self.write_feature_development_config(
                repository,
                "pass",
                steps=(
                    '["grill-with-docs", "to-spec", "to-tickets", "implement", '
                    '"tdd", "code-review", "qa-gate"]'
                ),
            )

            result = self.run_cli(repository, "workflow", "show", "feature-development")

            self.assertEqual(result.returncode, 1)
            self.assertIn("must contain exactly the macro steps", result.stdout)

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

                [workflows.routing-workflow]
                steps = ["implement"]
                """,
            )

            result = self.run_cli(repository, "workflow", "plan", "routing-workflow")

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

                [workflows.routing-workflow]
                steps = ["implement"]
                """,
            )

            result = self.run_cli(repository, "skill", "explain", "implement")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("limited: rejected; missing capabilities: git", result.stdout)
            self.assertIn("Selected: complete -> custom", result.stdout)

            plan = self.run_cli(repository, "workflow", "plan", "routing-workflow")

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

    def test_mcp_provider_uses_the_configured_command_and_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                import sys

                request = json.loads(sys.stdin.readline())
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocol": "harness.provider",
                        "version": 1,
                        "status": "SUCCESS",
                        "output": {
                            "provider": sys.argv[1],
                            "skill": request["params"]["skill"],
                        },
                    },
                }), flush=True)
            """
            self.write_config(
                repository,
                f"""
                [providers.mcp_fixture]
                type = "mcp"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(textwrap.dedent(provider_program))}, "configured-mcp"]

                [workers.qa]
                provider = "mcp_fixture"
                capabilities = ["testing"]

                [skills.qa-gate]
                requires = ["testing"]
                """,
            )

            result = self.run_cli(repository, "skill", "run", "qa-gate")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"provider": "configured-mcp"', result.stdout)
            self.assertIn('"skill": "qa-gate"', result.stdout)

    def test_mcp_provider_reports_protocol_failures_as_failed_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = "print('{\"status\": \"SUCCESS\"}', flush=True)"
            self.write_config(
                repository,
                f"""
                [providers.mcp_fixture]
                type = "mcp"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(provider_program)}]

                [workers.qa]
                provider = "mcp_fixture"
                capabilities = ["testing"]

                [skills.qa-gate]
                requires = ["testing"]
                """,
            )

            result = self.run_cli(repository, "skill", "run", "qa-gate")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Execution Status: FAILED", result.stdout)
            self.assertIn("Protocol error", result.stdout)

    def test_workflow_routes_review_and_qa_gate_to_the_healthy_mcp_qa_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                import sys

                request = json.loads(sys.stdin.readline())
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocol": "harness.provider",
                        "version": 1,
                        "status": "SUCCESS",
                        "output": {
                            "worker": sys.argv[1],
                            "skill": request["params"]["skill"],
                        },
                    },
                }), flush=True)
            """
            self.write_config(
                repository,
                f"""
                [providers.cli_fixture]
                type = "cli"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(textwrap.dedent(provider_program))}, "coder"]

                [providers.mcp_fixture]
                type = "mcp"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(textwrap.dedent(provider_program))}, "qa"]

                [workers.coder]
                provider = "cli_fixture"
                capabilities = ["testing"]
                health = "healthy"

                [workers.qa]
                provider = "mcp_fixture"
                capabilities = ["testing"]
                priority = 10
                health = "healthy"

                [skills.code-review]
                requires = ["testing"]
                [skills.code-review.execution]
                preferred = ["qa"]

                [skills.qa-gate]
                requires = ["testing"]
                [skills.qa-gate.execution]
                preferred = ["qa"]

                [workflows.quality]
                steps = ["code-review", "qa-gate"]
                """,
            )

            result = self.run_cli(repository, "workflow", "run", "quality")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("[1/2] code-review", result.stdout)
            self.assertIn("[2/2] qa-gate", result.stdout)
            self.assertEqual(result.stdout.count("-> mcp_fixture"), 2)
            execution_id = re.search(r"Execution ID: ([0-9a-f-]+)", result.stdout).group(1)
            state = self.run_cli(repository, "workflow", "status", execution_id)
            self.assertIn('"worker": "qa"', state.stdout)
            self.assertIn('"skill": "qa-gate"', state.stdout)

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

                [workflows.policy-workflow]
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
                "policy-workflow",
                "--caller",
                "auditor",
            )
            self.assertEqual(denied_plan.returncode, 1)
            self.assertIn("error: DELEGATION_DENIED", denied_plan.stdout)

            denied_workflow = self.run_cli(
                repository,
                "workflow",
                "run",
                "policy-workflow",
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

    def test_workflow_passes_only_explicitly_mapped_context_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                from pathlib import Path
                import sys

                request = json.loads(sys.stdin.readline())
                with Path("requests.jsonl").open("a", encoding="utf-8") as requests:
                    requests.write(json.dumps(request) + "\\n")
                outputs = {
                    "first": {"context_id": "ctx-123", "private": "do-not-forward"},
                    "second": {"spec_file": "docs/spec.md"},
                    "third": {"ticket_id": "TASK-41"},
                }
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocol": "harness.provider",
                        "version": 1,
                        "status": "SUCCESS",
                        "output": outputs[request["params"]["skill"]],
                    },
                }), flush=True)
                """
            self.write_config(
                repository,
                f"""
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

                [skills.third]
                requires = ["filesystem"]

                [workflows.pipeline]
                steps = ["first", "second", "third"]

                [workflows.pipeline.mappings.second]
                context_id = "first.output.context_id"

                [workflows.pipeline.mappings.third]
                spec_file = "second.output.spec_file"
                """,
            )

            result = self.run_cli(
                repository,
                "workflow",
                "run",
                "pipeline",
                "--input",
                '{"idea":"add durable state","private":"secret"}',
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            requests = [
                json.loads(line)
                for line in (repository / "requests.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [request["params"]["input"] for request in requests],
                [
                    {"idea": "add durable state", "private": "secret"},
                    {"context_id": "ctx-123"},
                    {"spec_file": "docs/spec.md"},
                ],
            )
            execution_id = re.search(r"Execution ID: ([0-9a-f-]+)", result.stdout).group(1)
            state = self.run_cli(repository, "workflow", "status", execution_id)
            self.assertIn("Context version: 1", state.stdout)
            self.assertIn("Context: ", state.stdout)
            self.assertIn('"context_id": "first.output.context_id"', state.stdout)

    def test_workflow_state_survives_cli_restart_and_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                from pathlib import Path
                import sys

                request = json.loads(sys.stdin.readline())
                skill = request["params"]["skill"]
                if skill == "second" and not Path("failed-once").exists():
                    Path("failed-once").write_text("yes", encoding="utf-8")
                    result = {"status": "FAILED", "error": "try again"}
                else:
                    result = {
                        "status": "SUCCESS",
                        "output": {"value": "ready"},
                    }
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

                [workflows.pipeline]
                steps = ["first", "second"]

                [workflows.pipeline.mappings.second]
                value = "first.output.value"
                """,
            )

            first_run = self.run_cli(
                repository,
                "workflow",
                "run",
                "pipeline",
                "--input",
                '{"seed":"start"}',
            )
            self.assertEqual(first_run.returncode, 1, first_run.stdout + first_run.stderr)
            execution_id = re.search(r"Execution ID: ([0-9a-f-]+)", first_run.stdout).group(1)

            status = self.run_cli(repository, "workflow", "status", execution_id)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            self.assertIn("Status: FAILED", status.stdout)
            self.assertIn('"value": "ready"', status.stdout)

            resumed = self.run_cli(repository, "workflow", "resume", execution_id)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)

            completed = self.run_cli(repository, "workflow", "status", execution_id)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("Status: COMPLETED", completed.stdout)

    def test_workflow_can_be_cancelled_and_resumed_from_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            provider_program = """
                import json
                from pathlib import Path
                import sys

                request = json.loads(sys.stdin.readline())
                if request["params"]["skill"] == "second" and not Path("failed-once").exists():
                    Path("failed-once").write_text("yes", encoding="utf-8")
                    result = {"status": "FAILED", "error": "pause me"}
                else:
                    result = {"status": "SUCCESS", "output": {"done": True}}
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

                [workflows.pipeline]
                steps = ["first", "second"]
                """,
            )

            first_run = self.run_cli(repository, "workflow", "run", "pipeline")
            execution_id = re.search(r"Execution ID: ([0-9a-f-]+)", first_run.stdout).group(1)

            cancelled = self.run_cli(repository, "workflow", "cancel", execution_id)
            self.assertEqual(cancelled.returncode, 0, cancelled.stdout + cancelled.stderr)
            self.assertIn("Status: CANCELLED", cancelled.stdout)

            resumed = self.run_cli(repository, "workflow", "resume", execution_id)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            completed = self.run_cli(repository, "workflow", "status", execution_id)
            self.assertIn("Status: COMPLETED", completed.stdout)

    def test_workflow_rejects_an_invalid_context_mapping_before_running(self) -> None:
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

                [skills.first]
                requires = ["filesystem"]

                [skills.second]
                requires = ["filesystem"]

                [workflows.pipeline]
                steps = ["first", "second"]

                [workflows.pipeline.mappings.second]
                value = "unknown.output.value"
                """,
            )

            result = self.run_cli(repository, "workflow", "plan", "pipeline")

            self.assertEqual(result.returncode, 1)
            self.assertIn("has an invalid source 'unknown.output.value'", result.stdout)

    def test_workflow_rejects_a_malformed_context_mapping_path_before_running(self) -> None:
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

                [skills.first]
                requires = ["filesystem"]

                [skills.second]
                requires = ["filesystem"]

                [workflows.pipeline]
                steps = ["first", "second"]

                [workflows.pipeline.mappings.second]
                value = "first.output.value."
                """,
            )

            result = self.run_cli(repository, "workflow", "plan", "pipeline")

            self.assertEqual(result.returncode, 1)
            self.assertIn("has an invalid source 'first.output.value.'", result.stdout)

    def test_workflow_rejects_duplicate_steps_before_planning(self) -> None:
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

                [skills.first]
                requires = ["filesystem"]

                [workflows.pipeline]
                steps = ["first", "first"]
                """,
            )

            result = self.run_cli(repository, "workflow", "plan", "pipeline")

            self.assertEqual(result.returncode, 1)
            self.assertIn("contains duplicate step 'first'", result.stdout)


if __name__ == "__main__":
    unittest.main()
