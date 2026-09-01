import asyncio
import json
from typing import Any, Mapping
from ..domain.execution import ExecutionContext, ExecutionRequest, ExecutionResult
from ..domain.events import (
    ExecutionCompleted,
    ExecutionFailed,
    ExecutionStarted,
    ProviderSelected,
)
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

        state_store = getattr(self.events, "state_store", None)
        if state_store and hasattr(state_store, "save_execution"):
            await state_store.save_execution(ExecutionContext(
                execution_id=plan.execution_id,
                session_id=plan.session_id,
                parent_execution_id=plan.parent_execution_id,
                caller=plan.caller,
                project=plan.project_id,
                depth=request.depth,
                skill=plan.skill,
                metadata={
                    "worker": plan.worker,
                    "provider": plan.provider,
                    "requirements": sorted(plan.requirements),
                    "resolved_capabilities": sorted(plan.resolved_capabilities),
                    "routing_reason": plan.routing_reason,
                    "routing_score": plan.routing_score,
                },
            ))
        await self._publish(ExecutionStarted(
            execution_id=plan.execution_id, skill=plan.skill, caller=plan.caller
        ))
        await self._publish(ProviderSelected(
            execution_id=plan.execution_id,
            provider=plan.provider,
            reason=plan.routing_reason,
        ))

        # Execute
        async with self._slots:
            result = await self.executor.execute(plan)
        result = self._enforce_quality_contract(decision.skill, result)
        if state_store and hasattr(state_store, "save_execution_result"):
            await state_store.save_execution_result(plan.execution_id, result)
        if result.status == "SUCCESS":
            await self._publish(ExecutionCompleted(
                execution_id=plan.execution_id,
                status=result.status,
                result=result.output,
            ))
        else:
            await self._publish(ExecutionFailed(
                execution_id=plan.execution_id,
                error=result.error or "Provider execution failed",
            ))
        return result

    @staticmethod
    def _enforce_quality_contract(skill: Any, result: ExecutionResult) -> ExecutionResult:
        required_phases = getattr(skill, "quality_phases", ())
        if result.status != "SUCCESS" or not required_phases:
            return result

        quality_status = (
            result.output.get("quality_status")
            if isinstance(result.output, Mapping)
            else None
        )
        failed_phases = [
            phase
            for phase in required_phases
            if not isinstance(quality_status, Mapping)
            or quality_status.get(phase) != "passed"
        ]
        if not failed_phases:
            return result

        details = {
            "code": "QUALITY_CONTRACT_FAILED",
            "skill": skill.name,
            "required_phases": list(required_phases),
            "failed_phases": failed_phases,
        }
        return ExecutionResult(
            execution_id=result.execution_id,
            status="FAILED",
            error=json.dumps(details, ensure_ascii=False, sort_keys=True),
            error_details=details,
        )

    async def _publish(self, event: Any) -> None:
        publish = getattr(self.events, "publish", None)
        if publish:
            await publish(event)
