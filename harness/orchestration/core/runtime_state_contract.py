"""Reserved operational-store vocabularies for #683–#684; no lifecycle authority."""

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
