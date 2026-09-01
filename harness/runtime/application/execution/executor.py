from ...domain.execution import ExecutionPlan, ExecutionResult
from ..registry import ProviderRegistry
import asyncio

class Executor:
    def __init__(self, provider_registry: ProviderRegistry):
        self.provider_registry = provider_registry

    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        provider = self.provider_registry.get(plan.provider)
        attempts = plan.retry_policy.max_attempts
        result = await provider.execute(plan)
        for attempt in range(1, attempts):
            if result.status == "SUCCESS":
                break
            delay = 0.0
            if plan.retry_policy.backoff == "linear":
                delay = float(attempt)
            elif plan.retry_policy.backoff == "exponential":
                delay = float(2 ** (attempt - 1))
            if delay:
                await asyncio.sleep(delay)
            result = await provider.execute(plan)
        return result
