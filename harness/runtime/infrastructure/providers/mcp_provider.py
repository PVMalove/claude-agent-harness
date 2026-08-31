import asyncio
import json
import uuid

from ...domain.provider import Provider
from ...domain.execution import ExecutionPlan, ExecutionResult

class MCPProvider(Provider):
    def __init__(self, command: str, args: list[str] | None = None):
        self.command = command
        self.args = args or []

    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        execution_id = str(uuid.uuid4())
        
        # An MCP implementation would typically initialize an MCP client, 
        # negotiate capabilities, and use tool calls or similar.
        # Here we mock the invocation over stdio.
        
        request_data = {
            "jsonrpc": "2.0",
            "id": execution_id,
            "method": "execute_skill",
            "params": {
                "skill": plan.skill,
                "capabilities": list(plan.capabilities)
            }
        }
        
        try:
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
                    error=stderr.decode('utf-8') or f"MCP Process exited with code {process.returncode}"
                )
                
            try:
                response = json.loads(stdout.decode('utf-8'))
                if "error" in response:
                    return ExecutionResult(
                        execution_id=execution_id,
                        status="FAILED",
                        error=response["error"].get("message", "Unknown MCP error")
                    )
                output = response.get("result", {})
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
