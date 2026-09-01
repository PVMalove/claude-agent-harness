import json
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WorkflowPauseTests(unittest.TestCase):
    def run_cli(self, repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "harness.runtime.cli", "--repo", str(repository), *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_json_rpc_question_pauses_and_resume_persists_answers_by_id(self) -> None:
        provider_program = """
            import json
            import sys

            request = json.loads(sys.stdin.readline())
            input_data = request["params"]["input"]
            if "_harness_answers" not in input_data:
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "method": "AskUserQuestion",
                    "params": {
                        "protocol": "harness.provider",
                        "version": 1,
                        "questions": [{
                            "id": "choice",
                            "question": "Continue?",
                            "header": "Continue",
                            "options": ["(Recommended) yes", "no"],
                            "multiSelect": False
                        }]
                    }
                }), flush=True)
            else:
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocol": "harness.provider",
                        "version": 1,
                        "status": "SUCCESS",
                        "output": {
                            "answer": input_data["_harness_answers"]["choice"]
                        }
                    }
                }), flush=True)
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            (repository / ".harness").mkdir()
            config = f"""
                [providers.fixture]
                type = "cli"
                command = {json.dumps(sys.executable)}
                args = ["-c", {json.dumps(textwrap.dedent(provider_program))}]

                [workers.worker]
                provider = "fixture"
                capabilities = ["filesystem"]

                [skills.pause-me]
                requires = ["filesystem"]

                [workflows.pause-pipeline]
                steps = ["pause-me"]
            """
            (repository / ".harness" / "orchestration.toml").write_text(
                textwrap.dedent(config), encoding="utf-8"
            )

            first = self.run_cli(repository, "workflow", "run", "pause-pipeline")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn("Workflow paused", first.stdout)
            self.assertIn("[choice] Continue?", first.stdout)
            self.assertIn("Other (free text)", first.stdout)
            execution_id = re.search(r"Execution ID: ([0-9a-f-]+)", first.stdout).group(1)

            paused = self.run_cli(repository, "workflow", "status", execution_id)
            self.assertIn("Status: PAUSED", paused.stdout)
            self.assertIn('"choice"', paused.stdout)
            self.assertIn('"question_request_id"', paused.stdout)

            resumed = self.run_cli(
                repository,
                "workflow",
                "resume",
                execution_id,
                "--answers",
                '{"choice":"yes"}',
            )
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)

            completed = self.run_cli(repository, "workflow", "status", execution_id)
            self.assertIn("Status: COMPLETED", completed.stdout)
            self.assertIn('"choice": "yes"', completed.stdout)
            self.assertIn('"answer": "yes"', completed.stdout)
            self.assertIn("Answers: {}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
