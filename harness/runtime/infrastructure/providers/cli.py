import asyncio
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...domain.provider import Provider
from ...domain.execution import ExecutionPlan, ExecutionResult
from ...domain.policy import RetryPolicy
from .protocol import (
    ProtocolError,
    execution_request,
    execution_result,
    question_params,
    question_response,
)


class CLIProvider(Provider):
    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: float = 600.0,
        cwd: str | Path | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("CLI provider timeout must be positive")
        self.command = command
        self.args = args or []
        self.timeout = float(timeout)
        self.cwd = str(cwd) if cwd is not None else None
        self.retry_policy = retry_policy or RetryPolicy()

    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        try:
            return await asyncio.wait_for(self._execute_stream(plan), self.timeout)
        except asyncio.TimeoutError:
            return ExecutionResult(
                execution_id=plan.execution_id,
                status="TIMEOUT",
                error=f"Provider timed out after {self.timeout:g} seconds",
            )
        except FileNotFoundError:
            return ExecutionResult(
                execution_id=plan.execution_id,
                status="FAILED",
                error=f"Command not found: {self.command}",
            )
        except ProtocolError as error:
            return ExecutionResult(
                execution_id=plan.execution_id,
                status="FAILED",
                error=f"Protocol error: {error}",
            )
        except (TypeError, ValueError) as error:
            return ExecutionResult(
                execution_id=plan.execution_id,
                status="FAILED",
                error=f"Could not encode provider request: {error}",
            )
        except Exception as error:
            return ExecutionResult(
                execution_id=plan.execution_id,
                status="FAILED",
                error=str(error),
            )

    async def _execute_stream(self, plan: ExecutionPlan) -> ExecutionResult:
        process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            request = execution_request(
                execution_id=plan.execution_id,
                skill=plan.skill,
                input_data=plan.input,
                capabilities=plan.resolved_capabilities,
            )
            await self._write_message(process, request)
            terminal = await self._read_stream(process, plan.execution_id)

            process.stdin.close()
            returncode = await process.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            if returncode != 0:
                return ExecutionResult(
                    execution_id=plan.execution_id,
                    status="FAILED",
                    error=stderr or f"Provider exited with code {returncode}",
                )
            return terminal
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            if process.stdin and not process.stdin.is_closing():
                process.stdin.close()
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def _read_stream(
        self, process: asyncio.subprocess.Process, execution_id: str
    ) -> ExecutionResult:
        while True:
            raw_line = await process.stdout.readline()
            if not raw_line:
                raise ProtocolError("provider closed the stream without a terminal result")
            try:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                message = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ProtocolError(f"provider emitted malformed JSON: {error}") from error
            if not isinstance(message, Mapping):
                raise ProtocolError("provider message must be a JSON object")

            if "method" in message:
                request_id, questions = question_params(message)
                answers = await self._ask_user(questions)
                await self._write_message(process, question_response(request_id, answers))
                continue

            status, output, error = execution_result(message, execution_id)
            return ExecutionResult(
                execution_id=execution_id,
                status=status,
                output=output,
                error=error,
            )

    async def _write_message(
        self, process: asyncio.subprocess.Process, message: dict[str, Any]
    ) -> None:
        if process.stdin is None:
            raise ProtocolError("provider stdin is unavailable")
        process.stdin.write(
            (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await process.stdin.drain()

    async def _ask_user(self, questions: list[Mapping[str, Any]]) -> list[Any]:
        answers: list[Any] = []
        print("\n--- ВОПРОС ОТ АГЕНТА ---")
        for question in questions:
            prompt = question.get("question")
            options = question.get("options", [])
            if not isinstance(prompt, str) or not isinstance(options, list):
                raise ProtocolError("AskUserQuestion has invalid question or options")
            if not all(isinstance(option, str) for option in options):
                raise ProtocolError("AskUserQuestion options must be strings")
            print(f"В: {prompt}")
            for index, option in enumerate(options):
                print(f"  {index + 1}) {option}")
            choice = await asyncio.to_thread(input, "Ваш выбор (номер): ")
            try:
                answers.append(options[int(choice) - 1])
            except (ValueError, IndexError):
                answers.append(choice)
        print("------------------------\n")
        return answers
