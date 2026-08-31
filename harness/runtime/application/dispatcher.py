from typing import Any
from ..domain.execution import ExecutionRequest, ExecutionResult, ExecutionPlan
from ..domain.skill import Skill
from .resolver import CapabilityResolver
from .registry import ProviderRegistry

class SkillRegistry:
    def __init__(self, skills: dict[str, Skill]):
        self.skills = skills

    def resolve(self, skill_name: str) -> Skill:
        if skill_name not in self.skills:
            raise KeyError(f"Skill not found: {skill_name}")
        return self.skills[skill_name]

class PolicyEngine:
    def evaluate(self, request: ExecutionRequest, skill: Skill) -> Any:
        # Evaluate delegation and execution policies
        pass

class HealthRegistry:
    def filter(self, candidates: list[Any]) -> list[Any]:
        # Filter out unhealthy candidates
        return candidates

class Scheduler:
    def select(self, candidates: list[Any], request: ExecutionRequest) -> Any:
        if not candidates:
            raise RuntimeError("No candidates available for execution")
        # In the future, this will score candidates. For now, pick the first.
        return candidates[0]

class Planner:
    def create(self, request: ExecutionRequest, skill: Skill, worker: Any, policy: Any) -> ExecutionPlan:
        from ..domain.policy import RetryPolicy
        return ExecutionPlan(
            skill=request.skill,
            worker=worker.name,
            provider=worker.provider,
            capabilities=worker.capabilities,
            timeout=600, # Should come from config or policy
            retry_policy=RetryPolicy()
        )

class Executor:
    def __init__(self, provider_registry: ProviderRegistry):
        self.provider_registry = provider_registry

    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        provider = self.provider_registry.get(plan.provider)
        return await provider.execute(plan)

class Dispatcher:
    def __init__(self, 
                 skills: SkillRegistry, 
                 policy: PolicyEngine, 
                 resolver: CapabilityResolver, 
                 health: HealthRegistry, 
                 scheduler: Scheduler, 
                 planner: Planner, 
                 executor: Executor):
        self.skills = skills
        self.policy = policy
        self.resolver = resolver
        self.health = health
        self.scheduler = scheduler
        self.planner = planner
        self.executor = executor

    async def dispatch(self, request: ExecutionRequest) -> ExecutionResult:
        skill = self.skills.resolve(request.skill)
        policy = self.policy.evaluate(request, skill)
        candidates = self.resolver.resolve_candidates(skill.requirements)
        candidates = self.health.filter(candidates)
        
        # Consider preferred workers from the skill execution policy
        preferred = skill.execution_policy.preferred
        if preferred:
            preferred_candidates = [c for c in candidates if c.name in preferred]
            if preferred_candidates:
                candidates = preferred_candidates

        worker = self.scheduler.select(candidates, request)
        plan = self.planner.create(request, skill, worker, policy)
        return await self.executor.execute(plan)
