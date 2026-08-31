from typing import Protocol, Any
from .execution import ExecutionContext, ExecutionResult

class StateStore(Protocol):
    async def save_execution(self, context: ExecutionContext) -> None:
        ...

    async def get_execution(self, execution_id: str) -> ExecutionContext | None:
        ...

    async def append_event(self, event: dict[str, Any]) -> None:
        ...
