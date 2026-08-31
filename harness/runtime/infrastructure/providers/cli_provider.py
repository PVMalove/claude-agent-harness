import asyncio
import json
import uuid
from typing import Any

from ...domain.provider import Provider
from ...domain.execution import ExecutionPlan, ExecutionResult

class CLIProvider(Provider):
    def __init__(self, command: str, args: list[str] | None = None):
        self.command = command
        self.args = args or []

    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        execution_id = str(uuid.uuid4())
        
        # Prepare the JSON request
        request_data = {
            "execution_id": execution_id,
            "skill": plan.skill,
            "capabilities": list(plan.capabilities)
        }
        
        try:
            # We use asyncio.create_subprocess_exec to run the CLI command asynchronously.
            # However, since these commands might not exist on the machine during this test, 
            # we will just echo or simulate if command is not found, or actually try to run.
            # In a real environment, it sends JSON to stdin and reads JSON from stdout.
            process = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate(input=json.dumps(request_data).encode('utf-8'))
            
            if process.returncode != 0:
                return ExecutionResult(
                    execution_id=execution_id,
                    status="FAILED",
                    error=stderr.decode('utf-8') or f"Process exited with code {process.returncode}"
                )
                
            try:
                output = json.loads(stdout.decode('utf-8'))
            except json.JSONDecodeError:
                output = {"raw_output": stdout.decode('utf-8')}
                
            return ExecutionResult(
                execution_id=execution_id,
                status="SUCCESS",
                output=output
            )
        except FileNotFoundError:
            return ExecutionResult(
                execution_id=execution_id,
                status="FAILED",
                error=f"Command not found: {self.command}"
            )
        except Exception as e:
            return ExecutionResult(
                execution_id=execution_id,
                status="FAILED",
                error=str(e)
            )

class ClaudeProvider(CLIProvider):
    pass

class AntigravityProvider(CLIProvider):
    pass

class CodexProvider(CLIProvider):
    pass
