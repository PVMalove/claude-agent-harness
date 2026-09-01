from ...domain.worker import Worker

class CapabilityResolver:
    def __init__(self, workers: dict[str, Worker]):
        self.workers = workers

    def resolve(self, requirements: set[str]) -> list[Worker]:
        candidates = []
        for worker in self.workers.values():
            if requirements.issubset(worker.capabilities):
                candidates.append(worker)
        return candidates
