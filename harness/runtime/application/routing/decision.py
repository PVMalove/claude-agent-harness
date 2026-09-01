from dataclasses import dataclass

from ...domain.skill import Skill
from ...domain.worker import Worker


@dataclass(frozen=True)
class RoutingDecision:
    skill: Skill
    worker: Worker | None
    score: int
    reason: str
    rejections: dict[str, str]
    error_code: str | None = None
