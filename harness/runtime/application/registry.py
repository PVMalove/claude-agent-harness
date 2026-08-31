from typing import Protocol
from ..domain.provider import Provider

class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, Provider] = {}

    def register(self, provider_id: str, provider: Provider) -> None:
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> Provider:
        if provider_id not in self._providers:
            raise KeyError(f"Provider not found: {provider_id}")
        return self._providers[provider_id]

    def candidates(self, capabilities: set[str]) -> list[Provider]:
        # This requires the Provider to expose its capabilities.
        # But wait, capabilities are associated with Workers according to the config.
        # A provider itself just executes. The config maps workers to providers and capabilities.
        # So "candidate providers" might actually be "candidate workers".
        # Let's adjust this later based on the dispatcher logic.
        pass
