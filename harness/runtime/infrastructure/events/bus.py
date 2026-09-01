from typing import Any
from ...domain.state import StateStore
from dataclasses import asdict, is_dataclass

class EventBus:
    def __init__(self, state_store: StateStore | None = None):
        self.state_store = state_store
        self.subscribers = []

    async def publish(self, event: Any) -> None:
        if self.state_store:
            payload = asdict(event) if is_dataclass(event) else dict(event)
            payload["event_type"] = type(event).__name__
            await self.state_store.append_event(payload)
