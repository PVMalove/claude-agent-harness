from typing import Any
from ...domain.state import StateStore
import datetime

class EventBus:
    def __init__(self, state_store: StateStore | None = None):
        self.state_store = state_store
        self.subscribers = []

    async def publish(self, event: Any) -> None:
        # In a real implementation, this would notify subscribers
        # and persist to the StateStore.

        # Stub: print event
        event_name = type(event).__name__
        print(f"[EventBus] Published {event_name}: {event}")

        if self.state_store:
            # We would convert event to dict here
            # await self.state_store.append_event({...})
            pass
