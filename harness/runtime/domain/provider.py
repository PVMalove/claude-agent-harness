from typing import Protocol
from .execution import ExecutionPlan, ExecutionResult

class Provider(Protocol):
    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        ...
