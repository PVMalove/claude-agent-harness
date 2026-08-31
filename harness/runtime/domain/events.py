from dataclasses import dataclass
from typing import Any

@dataclass
class DomainEvent:
    execution_id: str

@dataclass
class ExecutionStarted(DomainEvent):
    skill: str
    caller: str

@dataclass
class ProviderSelected(DomainEvent):
    provider: str
    reason: str

@dataclass
class ProviderFailed(DomainEvent):
    provider: str
    error: str

@dataclass
class ExecutionCompleted(DomainEvent):
    status: str
    result: dict[str, Any] | None

@dataclass
class ExecutionFailed(DomainEvent):
    error: str
