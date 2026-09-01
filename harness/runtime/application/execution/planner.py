import uuid
import datetime
from ...domain.execution import ExecutionPlan, ExecutionRequest
from ...domain.skill import Skill
from ...domain.worker import Worker
from ...domain.policy import RetryPolicy
from ..registry import ProviderRegistry

class Planner:
    def __init__(self, provider_registry: ProviderRegistry):
        self.provider_registry = provider_registry

    def create(self, request: ExecutionRequest, skill: Skill, worker: Worker) -> ExecutionPlan:
        provider = self.provider_registry.get(worker.provider)
        provider_type = getattr(provider, "type_name", "unknown")

        routing_score = getattr(worker, "_last_routing_score", 0)
        routing_reason = getattr(worker, "_last_routing_reason", "Selected by scheduler")

        return ExecutionPlan(
            execution_id=str(uuid.uuid4()),
            skill=request.skill,
            input=request.input,
            worker=worker.name,
            provider=worker.provider,
            provider_type=provider_type,
            caller=request.caller,
            session_id=request.session_id,
            project_id=request.project_id,
            parent_execution_id=None,
            requirements=skill.requirements,
            resolved_capabilities=worker.capabilities,
            timeout=600, # Should come from config or policy
            retry_policy=RetryPolicy(),
            routing_reason=routing_reason,
            routing_score=routing_score,
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
