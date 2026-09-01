import uuid
import datetime
from ...domain.execution import ExecutionPlan, ExecutionRequest
from ...domain.policy import RetryPolicy
from ..registry import ProviderRegistry
from ..routing.decision import RoutingDecision

class Planner:
    def __init__(self, provider_registry: ProviderRegistry):
        self.provider_registry = provider_registry

    def create(self, request: ExecutionRequest, decision: RoutingDecision) -> ExecutionPlan:
        provider = self.provider_registry.get(decision.worker.provider)
        provider_type = getattr(provider, "type_name", "unknown")

        return ExecutionPlan(
            execution_id=str(uuid.uuid4()),
            skill=request.skill,
            input=request.input,
            worker=decision.worker.name,
            provider=decision.worker.provider,
            provider_type=provider_type,
            caller=request.caller,
            session_id=request.session_id,
            project_id=request.project_id,
            parent_execution_id=None,
            requirements=decision.skill.requirements,
            resolved_capabilities=decision.worker.capabilities,
            timeout=600, # Should come from config or policy
            retry_policy=RetryPolicy(),
            routing_reason=decision.reason,
            routing_score=decision.score,
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
