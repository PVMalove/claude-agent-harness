from dataclasses import dataclass, field

@dataclass
class Workflow:
    name: str
    steps: list[str] = field(default_factory=list)
