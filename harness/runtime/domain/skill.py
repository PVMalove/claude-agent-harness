from dataclasses import dataclass, field
from .policy import ExecutionPolicy

@dataclass
class Skill:
    name: str
    requirements: set[str] = field(default_factory=set)
    execution_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    quality_phases: tuple[str, ...] = ()
