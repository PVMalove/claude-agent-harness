from typing import Protocol
from ..domain.provider import Provider
from ..domain.skill import Skill

class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, Provider] = {}

    def register(self, provider_id: str, provider: Provider) -> None:
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> Provider:
        if provider_id not in self._providers:
            raise KeyError(f"Provider not found: {provider_id}")
        return self._providers[provider_id]

class SkillRegistry:
    def __init__(self, skills: dict[str, Skill]):
        self.skills = skills

    def resolve(self, skill_name: str) -> Skill:
        if skill_name not in self.skills:
            raise KeyError(f"Skill not found: {skill_name}")
        return self.skills[skill_name]
