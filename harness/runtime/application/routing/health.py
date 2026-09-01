from ...domain.worker import Worker


class HealthRegistry:
    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self.statuses = statuses or {}

    def is_healthy(self, worker: Worker) -> bool:
        return self.statuses.get(worker.name, worker.health) == "healthy"

    def filter(self, candidates: list[Worker]) -> list[Worker]:
        return [worker for worker in candidates if self.is_healthy(worker)]

    def rejection_reasons(self, candidates: list[Worker]) -> dict[str, str]:
        return {
            worker.name: "unhealthy"
            for worker in candidates
            if not self.is_healthy(worker)
        }
