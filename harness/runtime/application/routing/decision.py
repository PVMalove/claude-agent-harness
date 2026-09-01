from dataclasses import dataclass

from ...domain.skill import Skill
from ...domain.worker import Worker


@dataclass(frozen=True)
class RoutingDecision:
    skill: Skill
    worker: Worker
    score: int
    reason: str
    rejections: dict[str, str]
