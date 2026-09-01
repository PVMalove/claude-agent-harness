import asyncio
import json
import sys
import unittest

from harness.runtime.domain.execution import ExecutionPlan
from harness.runtime.domain.policy import RetryPolicy
from harness.runtime.infrastructure.providers.codex import CodexProvider


class CodexProviderTests(unittest.TestCase):
    def test_adapts_codex_jsonl_agent_message_to_harness_result(self) -> None:
        payload = json.dumps(
            {
                "status": "SUCCESS",
                "output": {"context_id": "ctx-42"},
            }
        )
        program = (
            "import json; "
            f"print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':{payload!r}}}}}))"
        )
        provider = CodexProvider(
            command=sys.executable,
            args=["-c", program],
            timeout=5,
        )
        plan = ExecutionPlan(
            execution_id="execution-1",
            skill="grill-with-docs",
            input={"task": "OAuth"},
            worker="codex",
            provider="codex",
            provider_type="codex",
            caller="USER",
            session_id="session-1",
            project_id="project-1",
            parent_execution_id=None,
            requirements={"reasoning"},
            resolved_capabilities={"reasoning"},
            timeout=5,
            retry_policy=RetryPolicy(),
            routing_reason="test",
            routing_score=1,
        )

        result = asyncio.run(provider.execute(plan))

        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.execution_id, "execution-1")
        self.assertEqual(result.output, {"context_id": "ctx-42"})

    def test_maps_nested_paused_result_and_thread_id(self) -> None:
        payload = json.dumps(
            {
                "status": "SUCCESS",
                "output": {
                    "status": "PAUSED",
                    "questions": [{"id": "choice", "question": "Continue?"}],
                },
            }
        )
        self.assertEqual(
            CodexProvider._final_message(
                json.dumps({
                    "type": "thread.started",
                    "thread_id": "thread-42",
                })
                + "\n"
                + json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": payload},
                })
            ),
            (payload, None, "thread-42"),
        )
        status, output, error = CodexProvider._parse_final_message(payload)
        self.assertEqual(status, "PAUSED")
        self.assertEqual(output["questions"][0]["id"], "choice")
        self.assertIsNone(error)

    def test_rejects_malformed_nested_paused_questions(self) -> None:
        payload = json.dumps(
            {
                "status": "PAUSED",
                "output": {"questions": [{"id": "same", "question": "First?"},
                                             {"id": "same", "question": "Second?"}]},
            }
        )

        status, output, error = CodexProvider._parse_final_message(payload)

        self.assertEqual(status, "FAILED")
        self.assertEqual(output, {})
        self.assertIn("Protocol error", error)

    def test_rejects_non_json_final_agent_message(self) -> None:
        status, output, error = CodexProvider._parse_final_message("not JSON")

        self.assertEqual(status, "FAILED")
        self.assertEqual(output, {})
        self.assertIn("Protocol error", error)


if __name__ == "__main__":
    unittest.main()
