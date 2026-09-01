import asyncio
import uuid
import json
from ...domain.provider import Provider
from ...domain.execution import ExecutionPlan, ExecutionResult

class ClaudeProvider(Provider):
    def __init__(self):
        self.command = "claude"

    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        execution_id = str(uuid.uuid4())
        print(f"Executing {plan.skill} using Claude...")

        # Формируем промпт в виде слеш-команды
        input_str = json.dumps(dict(plan.input), ensure_ascii=False)
        prompt = f"/{plan.skill} {input_str}"
        args = ["-p", prompt]

        try:
            process = await asyncio.create_subprocess_exec(
                self.command, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                return ExecutionResult(
                    execution_id=execution_id,
                    status="FAILED",
                    error=stderr.decode('utf-8').strip() or f"Process exited with code {process.returncode}"
                )

            return ExecutionResult(
                execution_id=execution_id,
                status="SUCCESS",
                output={"log": stdout.decode('utf-8').strip()}
            )
        except FileNotFoundError:
            return ExecutionResult(
                execution_id=execution_id,
                status="FAILED",
                error=f"Command '{self.command}' not found. Is Claude Code installed?"
            )
        except Exception as e:
            return ExecutionResult(
                execution_id=execution_id,
                status="FAILED",
                error=str(e)
            )
