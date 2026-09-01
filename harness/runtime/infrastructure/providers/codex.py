import asyncio
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...domain.execution import ExecutionPlan, ExecutionResult
from ...domain.policy import RetryPolicy
from .protocol import ProtocolError, validate_pause_output


class CodexProvider:
    """Run Codex CLI in non-interactive JSONL mode and adapt its result to Harness."""

    type_name = "codex"

    def __init__(
        self,
        command: str = "codex",
        args: list[str] | None = None,
        timeout: float = 600.0,
        cwd: str | Path | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Codex provider timeout must be positive")
        self.command = command
        self.args = list(args) if args else ["exec", "--json"]
        self.timeout = float(timeout)
        self.cwd = str(cwd) if cwd is not None else None
        self.retry_policy = retry_policy or RetryPolicy()

    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        prompt = self._prompt(plan)
        try:
            process = await asyncio.create_subprocess_exec(
                self.command,
                *self._command_args(plan),
                prompt,
                cwd=self.cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            if "process" in locals() and process.returncode is None:
                process.kill()
                await process.wait()
            return ExecutionResult(
                execution_id=plan.execution_id,
                status="TIMEOUT",
                error=f"Codex timed out after {self.timeout:g} seconds",
            )
        except FileNotFoundError:
            return ExecutionResult(
                execution_id=plan.execution_id,
                status="FAILED",
                error=f"Command not found: {self.command}",
            )
        except Exception as error:
            return ExecutionResult(
                execution_id=plan.execution_id,
                status="FAILED",
                error=str(error),
            )

        output_text = stdout.decode("utf-8", errors="replace")
        error_text = stderr.decode("utf-8", errors="replace").strip()
        final_message, event_error, thread_id = self._final_message(output_text)

        if process.returncode != 0:
            return ExecutionResult(
                execution_id=plan.execution_id,
                status="FAILED",
                error=error_text or event_error or final_message or f"Codex exited with code {process.returncode}",
            )

        if not final_message:
            return ExecutionResult(
                execution_id=plan.execution_id,
                status="FAILED",
                error=error_text or event_error or "Codex produced no final agent message",
            )

        status, output, error = self._parse_final_message(final_message)
        if status == "PAUSED":
            if thread_id:
                output.setdefault("continuation_token", thread_id)
        error_details = None
        if error and error.startswith("Protocol error:"):
            error_details = {"code": "PROTOCOL_ERROR", "message": error}
        return ExecutionResult(
            execution_id=plan.execution_id,
            status=status,
            output=output,
            error=error,
            error_details=error_details,
        )

    @staticmethod
    def _prompt(plan: ExecutionPlan) -> str:
        input_json = json.dumps(dict(plan.input), ensure_ascii=False, sort_keys=True)
        answers = plan.input.get("_harness_answers", {})
        answers_json = json.dumps(answers, ensure_ascii=False, sort_keys=True)
        return (
            "You are a Harness workflow worker. Execute the requested skill in the current "
            "repository using the repository's installed skill instructions.\n\n"
            f"Skill: {plan.skill}\n"
            f"Input JSON: {input_json}\n"
            f"Required capabilities: {json.dumps(sorted(plan.resolved_capabilities))}\n\n"
            f"Answers already provided for this resumed step: {answers_json}\n\n"
            "Perform the work, including editing files and running checks when the skill requires it. "
            "Your final response must be exactly one JSON object, with no Markdown fences or extra text, "
            "in this form: {\"status\":\"SUCCESS\",\"output\":{}}. "
            "If user input is required before continuing, return "
            "{\"status\":\"PAUSED\",\"output\":{\"questions\":[{\"id\":\"...\",\"question\":\"...\",\"options\":[]} ]}}. "
            "Put all workflow handoff fields inside output. If the work cannot be completed, return "
            "{\"status\":\"FAILED\",\"output\":{},\"error\":\"...\"}."
        )

    @staticmethod
    def _final_message(raw_output: str) -> tuple[str | None, str | None, str | None]:
        messages: list[str] = []
        event_error: str | None = None
        thread_id: str | None = None
        for line in raw_output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping):
                continue
            if event.get("type") == "thread.started":
                candidate = event.get("thread_id")
                if isinstance(candidate, str):
                    thread_id = candidate
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    messages.append(text.strip())
            if event.get("type") in {"turn.failed", "error"}:
                error = event.get("error")
                if isinstance(error, str):
                    event_error = error
        return (messages[-1] if messages else None), event_error, thread_id

    def _command_args(self, plan: ExecutionPlan) -> list[str]:
        args = list(self.args)
        token = plan.input.get("_harness_continuation_token")
        if not isinstance(token, str) or not token:
            return args
        try:
            exec_index = args.index("exec")
        except ValueError:
            return args

        before_exec = args[:exec_index]
        exec_options = args[exec_index + 1 :]
        global_options = {"--sandbox", "--model", "--cd", "--ask-for-approval"}
        resume_options: list[str] = []
        index = 0
        while index < len(exec_options):
            option = exec_options[index]
            if option in global_options and index + 1 < len(exec_options):
                before_exec.extend((option, exec_options[index + 1]))
                index += 2
                continue
            resume_options.append(option)
            index += 1
        return before_exec + ["exec", "resume"] + resume_options + [token]

    @classmethod
    def _parse_final_message(
        cls, message: str
    ) -> tuple[str, dict[str, Any], str | None]:
        parsed = cls._parse_json_object(message)
        if parsed is None:
            return "FAILED", {}, "Protocol error: Codex final agent message must be a JSON object"

        status = parsed.get("status", "SUCCESS")
        if status not in {"SUCCESS", "FAILED", "PAUSED"}:
            return "FAILED", {}, f"Codex returned unsupported status: {status!r}"
        output = parsed.get("output", {})
        if output is None:
            output = {}
        if not isinstance(output, Mapping):
            return "FAILED", {}, "Codex output must be a JSON object"
        error = parsed.get("error")
        if error is not None and not isinstance(error, str):
            return "FAILED", {}, "Codex error must be a string"
        if status == "SUCCESS" and output.get("status") == "PAUSED":
            status = "PAUSED"
        if status == "PAUSED":
            try:
                validate_pause_output(output)
            except ProtocolError as error:
                return "FAILED", {}, f"Protocol error: {error}"
        if status == "FAILED" and not error:
            error = "Codex reported FAILED without an error"
        return status, dict(output), error

    @staticmethod
    def _parse_json_object(message: str) -> dict[str, Any] | None:
        candidate = message.strip()
        if candidate.startswith("```") and candidate.endswith("```"):
            candidate = candidate[3:-3].strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return dict(value) if isinstance(value, Mapping) else None
