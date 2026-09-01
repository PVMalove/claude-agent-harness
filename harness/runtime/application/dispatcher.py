import asyncio
import json
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
                 events: Any,
                 max_parallel: int = 8): # events bus placeholder
        self.skills = skills
        self.resolver = resolver
        self.policy = policy
        self.health = health
        self.scheduler = scheduler
        self.planner = planner
        self.executor = executor
        self.events = events
        self._slots = asyncio.Semaphore(max_parallel)

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

        policy_result = self.policy.evaluate(
            request=request,
            candidates=candidates,
        )
        authorized = policy_result.authorized
        rejections.update(policy_result.rejections)

        healthy = self.health.filter(authorized)
        rejections.update(self.health.rejection_reasons(authorized))

        if not healthy:
            if policy_result.error_code:
                error_code = policy_result.error_code
                reason = policy_result.reason or "Dispatch rejected by policy"
            elif not candidates:
                error_code = "NO_ELIGIBLE_WORKER"
                reason = f"No worker has the capabilities required by skill '{skill.name}'"
            elif not authorized:
                error_code = "DELEGATION_DENIED"
                reason = f"No authorized worker is available for skill '{skill.name}'"
            else:
                error_code = "NO_HEALTHY_WORKER"
                reason = f"No healthy worker is available for skill '{skill.name}'"
            return RoutingDecision(
                skill=skill,
                worker=None,
                score=0,
                reason=reason,
                rejections=rejections,
                error_code=error_code,
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

        if decision.worker is None:
            error = {
                "code": decision.error_code or "ROUTING_FAILED",
                "message": decision.reason,
                "rejections": decision.rejections,
            }
            return ExecutionResult(
                execution_id="",
                status="FAILED",
                error=json.dumps(error, ensure_ascii=False, sort_keys=True),
                error_details=error,
            )

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
        async with self._slots:
            return await self.executor.execute(plan)
