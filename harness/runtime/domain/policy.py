from dataclasses import dataclass, field
from typing import Literal

@dataclass
class RetryPolicy:
    max_attempts: int = 1
    backoff: Literal["none", "exponential", "linear"] = "none"

@dataclass
class DelegationRule:
    worker: str
    skills: set[str]

@dataclass
class DelegationPolicy:
    allow: list[DelegationRule] = field(default_factory=list)

@dataclass
class ExecutionPolicy:
    preferred: list[str] = field(default_factory=list)
