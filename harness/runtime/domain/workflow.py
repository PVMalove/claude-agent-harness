from dataclasses import dataclass, field
from typing import Mapping

@dataclass
class Workflow:
    name: str
    steps: list[str] = field(default_factory=list)
    parallel: bool = False
    mappings: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
