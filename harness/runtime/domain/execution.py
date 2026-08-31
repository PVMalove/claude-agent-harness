from dataclasses import dataclass, field
from typing import Any
from .policy import RetryPolicy

@dataclass
class ExecutionRequest:
    skill: str
    input: dict[str, Any]
    caller: str
    session_id: str
    project_id: str

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
    skill: str
    worker: str
    provider: str
    capabilities: set[str]
    timeout: int
    retry_policy: RetryPolicy

@dataclass
class ExecutionResult:
    execution_id: str
    status: str
    output: dict[str, Any] | None = None
    error: str | None = None
