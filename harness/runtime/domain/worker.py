from dataclasses import dataclass, field

@dataclass
class Worker:
    name: str
    provider: str
    capabilities: set[str] = field(default_factory=set)
