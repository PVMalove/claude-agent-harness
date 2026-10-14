"""References to the command boundaries that enforce dispatch lifecycle transitions."""

DISPATCH_TRANSITION_VALIDATORS = (
    "harness.orchestration.workflow.dispatch:create_dispatch",
    "harness.orchestration.workflow.dispatch:cancel_dispatch",
    "harness.orchestration.workflow.delivery:send_dispatch",
    "harness.orchestration.workflow.reports:self_report_dispatch",
    "harness.orchestration.workflow.reports:checkpoint_dispatch",
    "harness.orchestration.workflow.reports:resume_dispatch",
    "harness.orchestration.workflow.reports:rate_limited_dispatch",
    "harness.orchestration.workflow.reports:submit_report",
)
