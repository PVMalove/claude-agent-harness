from ...domain.execution import ExecutionRequest
from ...domain.policy import DelegationPolicy
from ...domain.worker import Worker


class PolicyResult:
    def __init__(
        self,
        authorized: list[Worker],
        rejections: dict[str, str],
        error_code: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.authorized = authorized
        self.rejections = rejections
        self.error_code = error_code
        self.reason = reason


class PolicyEngine:
    def __init__(
        self,
        delegation: dict[str, DelegationPolicy] | None = None,
        max_depth: int | None = None,
    ) -> None:
        self.delegation = delegation or {}
        self.max_depth = max_depth

    def evaluate(self, request: ExecutionRequest, candidates: list[Worker]) -> PolicyResult:
        if self.max_depth is not None and request.depth > self.max_depth:
            reason = (
                f"depth {request.depth} exceeds configured maximum {self.max_depth}"
            )
            return PolicyResult(
                authorized=[],
                rejections={worker.name: reason for worker in candidates},
                error_code="DEPTH_LIMIT_EXCEEDED",
                reason=reason,
            )

        policy = self.delegation.get(request.caller)
        # USER is the trusted root caller. A configured policy for USER is still
        # enforced, which allows installations to make the root restrictive.
        # Do not treat a caller-controlled string such as SYSTEM as trusted.
        if policy is None and request.caller == "USER":
            return PolicyResult(authorized=candidates, rejections={})
        if policy is None:
            reason = f"caller '{request.caller}' has no delegation policy"
            return PolicyResult(
                authorized=[],
                rejections={worker.name: reason for worker in candidates},
                error_code="DELEGATION_DENIED",
                reason=reason,
            )

        authorized: list[Worker] = []
        rejections: dict[str, str] = {}
        for worker in candidates:
            if any(
                rule.worker == worker.name
                and (request.skill in rule.skills or "*" in rule.skills)
                for rule in policy.allow
            ):
                authorized.append(worker)
            else:
                rejections[worker.name] = (
                    f"caller '{request.caller}' is not allowed to dispatch "
                    f"skill '{request.skill}' to worker '{worker.name}'"
                )
        return PolicyResult(
            authorized=authorized,
            rejections=rejections,
            error_code="DELEGATION_DENIED" if not authorized else None,
            reason=(
                f"caller '{request.caller}' is not authorized for skill '{request.skill}'"
                if not authorized
                else None
            ),
        )

    def authorize(self, request: ExecutionRequest, candidates: list[Worker]) -> list[Worker]:
        return self.evaluate(request, candidates).authorized
