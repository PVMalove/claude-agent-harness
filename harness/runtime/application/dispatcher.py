from typing import Any
from ..domain.execution import ExecutionRequest, ExecutionResult
from .registry import SkillRegistry
from .routing.resolver import CapabilityResolver
from .routing.policy import PolicyEngine
from .routing.health import HealthRegistry
from .routing.scheduler import Scheduler
from .execution.planner import Planner
from .execution.executor import Executor
from .routing.decision import RoutingDecision

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

    def route(self, request: ExecutionRequest) -> RoutingDecision:
        skill = self.skills.resolve(request.skill)

        candidates = self.resolver.resolve(requirements=skill.requirements)
        rejections = {
            worker.name: (
                "missing capabilities: "
                + ", ".join(sorted(skill.requirements - worker.capabilities))
            )
            for worker in self.resolver.workers.values()
            if worker not in candidates
        }

        authorized = self.policy.authorize(
            request=request,
            candidates=candidates,
        )
        rejections.update(
            {
                worker.name: "rejected by delegation policy"
                for worker in candidates
                if worker not in authorized
            }
        )

        healthy = self.health.filter(authorized)
        rejections.update(
            {
                worker.name: "unhealthy"
                for worker in authorized
                if worker not in healthy
            }
        )

        selected = self.scheduler.select(
            skill=skill,
            candidates=healthy,
        )
        return RoutingDecision(
            skill=skill,
            worker=selected.worker,
            score=selected.score,
            reason=selected.reason,
            rejections=rejections,
        )

    async def dispatch(self, request: ExecutionRequest) -> ExecutionResult:
        decision = self.route(request)

        plan = self.planner.create(
            request=request,
            decision=decision,
        )

        # 6. Publish event (assuming we have an async publish method)
        if hasattr(self.events, "publish"):
            from ..domain.events import ProviderSelected
            # await self.events.publish(ProviderSelected(...))
            # Just a placeholder, adapt when event bus is ready

        # 7. Execute
        return await self.executor.execute(plan)
