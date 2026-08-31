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
            process = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Send initial request
            process.stdin.write(json.dumps(request_data).encode('utf-8') + b'\n')
            await process.stdin.drain()
            
            final_output = {}
            
            # Read stdout line by line
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                    
                line_str = line.decode('utf-8').strip()
                if not line_str:
                    continue
                    
                try:
                    message = json.loads(line_str)
                except json.JSONDecodeError:
                    print(f"[{self.command} STDOUT]: {line_str}")
                    continue
                    
                # Handle RPC request from provider
                if "method" in message and message["method"] == "AskUserQuestion":
                    params = message.get("params", {})
                    questions = params.get("questions", [])
                    answers = []
                    
                    print("\n--- ВОПРОС ОТ АГЕНТА ---")
                    for q in questions:
                        print(f"В: {q.get('question')}")
                        for i, opt in enumerate(q.get('options', [])):
                            print(f"  {i+1}) {opt}")
                        # In a real environment, this would call a UI rendering function.
                        # For the CLI mock, we use a simple input.
                        choice = input("Ваш выбор (номер): ")
                        try:
                            choice_idx = int(choice) - 1
                            answers.append(q['options'][choice_idx])
                        except:
                            answers.append(choice)
                    print("------------------------\n")
                            
                    # Send response back to provider
                    response = {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "result": {"answers": answers}
                    }
                    process.stdin.write(json.dumps(response).encode('utf-8') + b'\n')
                    await process.stdin.drain()
                    
                # Final result from provider
                elif "status" in message:
                    final_output = message
                    
            await process.wait()
            
            if process.returncode != 0:
                stderr = await process.stderr.read()
                return ExecutionResult(
                    execution_id=execution_id,
                    status="FAILED",
                    error=stderr.decode('utf-8') or f"Process exited with code {process.returncode}"
                )
                
            return ExecutionResult(
                execution_id=execution_id,
                status=final_output.get("status", "SUCCESS"),
                output=final_output.get("output", {})
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
