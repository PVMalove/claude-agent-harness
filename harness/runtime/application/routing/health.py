from typing import Any
from ...domain.worker import Worker

class HealthRegistry:
    def filter(self, candidates: list[Worker]) -> list[Worker]:
        # In a real implementation, this would ping workers or check their
        # recent error rates.
        # For now, assume all candidates are healthy.
        return candidates
