from dataclasses import dataclass, field
from typing import Any, Mapping
import datetime
from .policy import RetryPolicy

@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    skill: str
    input: Mapping[str, Any] = field(default_factory=dict)
    caller: str = "USER"
    session_id: str = field(default_factory=str)
    project_id: str = field(default_factory=str)
    depth: int = 0

@dataclass
class ExecutionContext:
    execution_id: str
    session_id: str
    parent_execution_id: str | None
    caller: str
    project: str
    depth: int
    skill: str
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class ExecutionPlan:
    execution_id: str
    skill: str
    input: Mapping[str, Any]
    worker: str
    provider: str
    provider_type: str
    caller: str
    session_id: str
    project_id: str
    parent_execution_id: str | None
    requirements: set[str]
    resolved_capabilities: set[str]
    timeout: float
    retry_policy: RetryPolicy
    routing_reason: str
    routing_score: int
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

@dataclass
class ExecutionResult:
    execution_id: str
    status: str
    output: dict[str, Any] | None = None
    error: str | None = None
    error_details: dict[str, Any] | None = None
