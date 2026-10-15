"""Reserved operational-store vocabularies for #683–#684; no lifecycle authority."""

from pathlib import Path

RUNTIME_STATE_CONTRACT = "runtimeState.v1"
# The independent emergency record of a state root: diagnostics only, never an authorization.
RUNTIME_EMERGENCY_CONTRACT = "runtimeEmergency.v1"
# Relative to the shared storage root (`<main checkout>/.harness`), never to a state root.
RUNTIME_STATE_DIR = Path("orchestration/runtime-state")

RUNTIME_VOCABULARIES = {
    "runtime.state": ("ready", "running", "suspended", "reconciling", "completed"),
    "runtime.operation_kind": (
        "dispatch-send",
        "checkpoint",
        "report-submit",
        "transition",
        "migrate",
        "local-write",
    ),
    "runtime.operation_phase": (
        "prepared",
        "effect-observed",
        "ledger-committed",
        "committed",
    ),
    "runtime.effect_state": ("not-started", "confirmed", "unknown"),
    "runtime.failure_code": (
        "config-invalid",
        "runtime-compatibility",
        "integrity-failure",
        "execution-failure",
        "ambiguous-effect",
        "storage-unavailable",
        "state-persist-failed",
        "concurrency-conflict",
        "internal-error",
    ),
}
RUNTIME_TRANSITIONS = {
    "ready": ("running",),
    "running": ("completed", "suspended"),
    "suspended": ("reconciling",),
    "reconciling": ("ready", "suspended"),
    "completed": (),
}
RUNTIME_OPERATION_TRANSITIONS = {
    "prepared": ("effect-observed",),
    "effect-observed": ("ledger-committed",),
    "ledger-committed": ("committed",),
    "committed": (),
}
RUNTIME_EFFECT_TRANSITIONS = {
    "not-started": ("confirmed", "unknown"),
    "unknown": ("confirmed",),
    "confirmed": (),
}
_TRANSITION_TABLES = {
    "runtime.state": RUNTIME_TRANSITIONS,
    "runtime.operation_phase": RUNTIME_OPERATION_TRANSITIONS,
    "runtime.effect_state": RUNTIME_EFFECT_TRANSITIONS,
}


def transition_problem(field_id: str, before: str, after: str) -> str | None:
    """``None`` when ``after`` keeps ``before`` or follows its table, else a refusal naming the
    allowed targets; an unknown field or source value is refused too, never treated as open."""
    table = _TRANSITION_TABLES.get(field_id)
    if table is None:
        return f"{field_id} has no runtime transition table"
    if before not in table:
        return f"{field_id} {before!r} is not a known source value"
    if after == before or after in table[before]:
        return None
    allowed = ", ".join(table[before]) or "none (terminal)"
    return f"{field_id} cannot change from {before!r} to {after!r}; allowed: {allowed}"
