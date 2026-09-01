from dataclasses import dataclass, field
from typing import Any, Mapping, TypedDict


class WorkflowStepResult(TypedDict, total=False):
    step: int
    skill: str
    execution_id: str
    status: str
    input: dict[str, Any]
    lineage: dict[str, str]
    output: dict[str, Any] | None
    error: str | None
    error_details: dict[str, Any] | None

@dataclass
class Workflow:
    name: str
    steps: list[str] = field(default_factory=list)
    parallel: bool = False
    mappings: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
