import asyncio
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from harness.runtime.domain.execution import ExecutionPlan
from harness.runtime.domain.policy import RetryPolicy
from harness.runtime.infrastructure.providers.cli import CLIProvider
from harness.runtime.infrastructure.providers.protocol import (
    ProtocolError,
    execution_request,
    question_params,
)


def question_message(questions: list[dict]) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "AskUserQuestion",
        "params": {
            "protocol": "harness.provider",
            "version": 1,
            "questions": questions,
        },
    }


def execution_plan() -> ExecutionPlan:
    return ExecutionPlan(
        execution_id="execution-1",
        skill="grill-with-docs",
        input={},
        worker="worker",
        provider="provider",
        provider_type="cli",
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


class ProviderProtocolTests(unittest.TestCase):
    def test_accepts_structured_questions_without_changing_ui_metadata(self) -> None:
        questions = [
            {
                "id": "retention",
                "header": "Data policy",
                "question": "How should old data be handled?",
                "options": ["(Recommended) Archive", "Delete"],
                "multiSelect": False,
            },
            {
                "id": "owner",
                "header": "Owner",
                "question": "Who owns the migration?",
                "options": ["(Recommended) Platform", "Product"],
                "multiSelect": False,
            },
        ]

        request_id, parsed = question_params(question_message(questions))

        self.assertEqual(request_id, 101)
        self.assertEqual(parsed, questions)

    def test_accepts_open_question_without_inventing_options(self) -> None:
        question = {
            "id": "name",
            "question": "What should this capability be called?",
        }

        _, parsed = question_params(question_message([question]))

        self.assertEqual(parsed, [question])
        self.assertNotIn("options", parsed[0])

    def test_rejects_duplicate_or_blank_question_ids(self) -> None:
        for questions in (
            [
                {"id": "same", "question": "First?"},
                {"id": "same", "question": "Second?"},
            ],
            [{"id": "  ", "question": "Missing identity?"}],
        ):
            with self.subTest(questions=questions):
                with self.assertRaisesRegex(ProtocolError, "question ids"):
                    question_params(question_message(questions))

    def test_rejects_malformed_structured_question(self) -> None:
        malformed = {
            "id": "choice",
            "header": "Choice",
            "question": "Pick one?",
            "options": ["A"],
            "multiSelect": True,
        }

        with self.assertRaises(ProtocolError):
            question_params(question_message([malformed]))

    def test_rejects_structured_question_without_explicit_single_select_mode(self) -> None:
        question = {
            "id": "choice",
            "header": "Choice",
            "question": "Pick one?",
            "options": ["(Recommended) A", "B"],
        }

        with self.assertRaisesRegex(ProtocolError, "single-select"):
            question_params(question_message([question]))

    def test_execute_request_carries_reserved_resume_answers(self) -> None:
        request = execution_request(
            execution_id="execution-1",
            skill="grill-with-docs",
            input_data={"task": "OAuth", "_harness_answers": {"choice": "yes"}},
            capabilities={"reasoning"},
        )

        self.assertEqual(
            request["params"]["input"]["_harness_answers"], {"choice": "yes"}
        )

    def test_cli_adapter_pauses_without_sending_a_question_answer(self) -> None:
        provider_program = textwrap.dedent(
            """
            import json
            import sys

            json.loads(sys.stdin.readline())
            print(json.dumps({
                "jsonrpc": "2.0",
                "id": 101,
                "method": "AskUserQuestion",
                "params": {
                    "protocol": "harness.provider",
                    "version": 1,
                    "questions": [{
                        "id": "choice",
                        "header": "Continue",
                        "question": "Continue?",
                        "options": ["(Recommended) Yes", "No"],
                        "multiSelect": False,
                    }],
                },
            }), flush=True)
            trailing_input = sys.stdin.read()
            with open("trailing-input.txt", "w", encoding="utf-8") as output:
                output.write(trailing_input)
            """
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            provider = CLIProvider(
                command=sys.executable,
                args=["-c", provider_program],
                timeout=5,
                cwd=temporary_directory,
            )

            result = asyncio.run(provider.execute(execution_plan()))

            self.assertEqual(result.status, "PAUSED")
            self.assertEqual(result.output["question_request_id"], 101)
            self.assertEqual(
                Path(temporary_directory, "trailing-input.txt").read_text(), ""
            )

    def test_cli_adapter_reports_malformed_question_as_structured_failure(self) -> None:
        provider_program = (
            "import json; "
            "print(json.dumps({'jsonrpc':'2.0','id':101,'method':'AskUserQuestion',"
            "'params':{'protocol':'harness.provider','version':1,'questions':[]}}), flush=True)"
        )
        provider = CLIProvider(
            command=sys.executable,
            args=["-c", provider_program],
            timeout=5,
        )

        result = asyncio.run(provider.execute(execution_plan()))

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error_details["code"], "PROTOCOL_ERROR")

    def test_cli_adapter_maps_a_normal_success_result(self) -> None:
        provider_program = (
            "import json; "
            "print(json.dumps({'jsonrpc':'2.0','id':'execution-1',"
            "'result':{'protocol':'harness.provider','version':1,'status':'SUCCESS',"
            "'output':{'marker':'done'}}}), flush=True)"
        )
        provider = CLIProvider(
            command=sys.executable,
            args=["-c", provider_program],
            timeout=5,
        )

        result = asyncio.run(provider.execute(execution_plan()))

        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.output, {"marker": "done"})


if __name__ == "__main__":
    unittest.main()
