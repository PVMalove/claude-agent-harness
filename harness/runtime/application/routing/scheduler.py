from typing import Any
from ...domain.execution import ExecutionRequest
from ...domain.skill import Skill
from ...domain.worker import Worker

class Scheduler:
    def select(self, skill: Skill, candidates: list[Worker]) -> Worker:
        if not candidates:
            raise RuntimeError(f"No candidates available for skill: {skill.name}")

        preferred = skill.execution_policy.preferred if skill.execution_policy else []

        best_worker = None
        best_score = -1

        for worker in candidates:
            # capability_score: number of capabilities
            capability_score = len(worker.capabilities)
            # priority_score: stub
            priority_score = 0
            # preference_score: 100 if preferred, 0 otherwise
            preference_score = 100 if worker.name in preferred else 0
            # health_score: stub
            health_score = 50

            total_score = capability_score + priority_score + preference_score + health_score

            # We can attach the score to the worker temporarily or just pick the max
            if total_score > best_score:
                best_score = total_score
                best_worker = worker
                # Assuming we might want to store routing reasons later,
                # we could wrap this in a selection object.

        # Hack to attach routing info for the planner
        if best_worker:
            best_worker._last_routing_score = best_score
            best_worker._last_routing_reason = "Highest overall score (capability + preference + health)"

        return best_worker
