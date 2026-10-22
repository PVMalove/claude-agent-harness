//! The states the file ledger names. `tests/orchestration/test_runtime_store_canonical.py` fails
//! when a list differs from `BatchState` or `DispatchState` in `harness/orchestration/core/constants.py`.

pub const BATCH_STATES: [&str; 9] = [
    "planned",
    "awaiting-approval",
    "active",
    "blocked",
    "failed",
    "completed",
    "not-required",
    "abandoned",
    "paused",
];

pub const DISPATCH_STATES: [&str; 11] = [
    "proposed",
    "approved",
    "dispatched",
    "working",
    "blocked",
    "failed",
    "reported",
    "checkpointed",
    "rate_limited",
    "abandoned",
    "cancelled",
];
