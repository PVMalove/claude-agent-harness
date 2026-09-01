from typing import Any
from ..domain.execution import ExecutionRequest, ExecutionResult
from .registry import SkillRegistry
from .routing.resolver import CapabilityResolver
from .routing.policy import PolicyEngine
from .routing.health import HealthRegistry
from .routing.scheduler import Scheduler
from .execution.planner import Planner
from .execution.executor import Executor

class Dispatcher:
    def __init__(self, 
                 skills: SkillRegistry, 
                 resolver: CapabilityResolver,
                 policy: PolicyEngine, 
                 health: HealthRegistry, 
                 scheduler: Scheduler, 
                 planner: Planner, 
                 executor: Executor,
                 events: Any): # events bus placeholder
        self.skills = skills
        self.resolver = resolver
        self.policy = policy
        self.health = health
        self.scheduler = scheduler
        self.planner = planner
        self.executor = executor
        self.events = events

    async def dispatch(self, request: ExecutionRequest) -> ExecutionResult:
        skill = self.skills.resolve(request.skill)

        # 1. Resolve candidates based on required capabilities
        candidates = self.resolver.resolve(requirements=skill.requirements)

        # 2. Authorize via Policy
        authorized = self.policy.authorize(
            request=request,
            candidates=candidates,
        )

        # 3. Filter healthy workers
        healthy = self.health.filter(authorized)

        # 4. Schedule (Select best worker)
        selected = self.scheduler.select(
            skill=skill,
            candidates=healthy,
        )

        # 5. Plan execution
        plan = self.planner.create(
            request=request,
            skill=skill,
            worker=selected,
        )

        # 6. Publish event (assuming we have an async publish method)
        if hasattr(self.events, "publish"):
            from ..domain.events import ProviderSelected
            # await self.events.publish(ProviderSelected(...))
            # Just a placeholder, adapt when event bus is ready

        # 7. Execute
        return await self.executor.execute(plan)
