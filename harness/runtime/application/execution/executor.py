from ...domain.execution import ExecutionPlan, ExecutionResult
from ..registry import ProviderRegistry

class Executor:
    def __init__(self, provider_registry: ProviderRegistry):
        self.provider_registry = provider_registry

    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        provider = self.provider_registry.get(plan.provider)
        return await provider.execute(plan)
