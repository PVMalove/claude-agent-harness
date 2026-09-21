"""Command-line wiring for the orchestration coordinator.

This module deliberately knows command names and argument shapes only.  Domain handlers remain
injected by :mod:`coordinator`, keeping parser changes from coupling to ledger transitions.
"""

from __future__ import annotations

import argparse
import types


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=argparse.SUPPRESS, help="target project root")
    parser.add_argument(
        "--state-dir", default=argparse.SUPPRESS, help="coordinator state directory"
    )


def build_parser(
    handlers: types.ModuleType, defaults: types.ModuleType
) -> argparse.ArgumentParser:
    """Build the stable public CLI using an injected coordinator handler facade."""
    root = argparse.ArgumentParser(
        description="Coordinate approved backend role dispatches."
    )
    root.add_argument("--repo", default=".", help="target project root")
    root.add_argument("--state-dir", help="coordinator state directory")
    commands = root.add_subparsers(dest="command", required=True)

    ledger = commands.add_parser(
        "ledger", help="inspect or explicitly maintain versioned lifecycle state"
    )
    ledger_commands = ledger.add_subparsers(dest="ledger_command", required=True)
    for name, handler, help_text in (
        (
            "status",
            handlers.ledger_status,
            "Report the selected lifecycle-ledger generation",
        ),
        (
            "migrate",
            handlers.migrate_ledger,
            "Validate and migrate legacy state to current schema",
        ),
        (
            "clean",
            handlers.clean_ledger,
            "Safely remove orphaned dispatch evidence to fix migration errors",
        ),
    ):
        command = ledger_commands.add_parser(name, help=help_text)
        _common(command)
        command.set_defaults(handler=handler)
    ledger_reset = ledger_commands.add_parser(
        "reset", help="Select an empty generation (requires --confirm RESET)"
    )
    _common(ledger_reset)
    ledger_reset.add_argument(
        "--confirm", required=True, help="literal RESET acknowledgement"
    )
    ledger_reset.set_defaults(handler=handlers.reset_ledger)

    batch = commands.add_parser("batch")
    batch_commands = batch.add_subparsers(dest="batch_command", required=True)
    create = batch_commands.add_parser("create", aliases=["plan"])
    _common(create)
    create.add_argument("--ticket", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--worktree", required=True)
    create.add_argument(
        "--zone",
        default=defaults.DEFAULT_ZONE,
        help="backend zone; defaults to the whole repository",
    )
    create.add_argument(
        "--integration-ref",
        help="branch on origin this batch's base is fetched and pinned against; falls back to the project's base_branch for epic-less tasks",
    )
    create.add_argument("--definition-of-done", action="append", required=True)
    create.add_argument("--prohibited-change", action="append", required=True)
    create.add_argument("--required-gate", action="append")
    create.add_argument("--dependency", action="append")
    create.add_argument(
        "--expected-file", action="append", help="planned changed path; repeated"
    )
    create.add_argument(
        "--expected-service",
        action="append",
        help="bounded context/service expected to change; repeated",
    )
    create.add_argument(
        "--expected-changed-lines", type=int, help="conservative expected diff size"
    )
    create.add_argument(
        "--expected-context-tokens",
        type=int,
        help="optional observed context estimate; never lowers the deterministic floor",
    )
    create.set_defaults(handler=handlers.create_batch)
    batch_preflight = batch_commands.add_parser(
        "preflight", help="reject an oversized ticket before creating a batch"
    )
    _common(batch_preflight)
    batch_preflight.add_argument("--ticket", required=True)
    batch_preflight.add_argument("--zone", default=defaults.DEFAULT_ZONE)
    batch_preflight.add_argument("--definition-of-done", action="append", required=True)
    batch_preflight.add_argument("--dependency", action="append")
    batch_preflight.add_argument("--expected-file", action="append")
    batch_preflight.add_argument("--expected-service", action="append")
    batch_preflight.add_argument("--expected-changed-lines", type=int)
    batch_preflight.add_argument("--expected-context-tokens", type=int)
    batch_preflight.set_defaults(handler=handlers.preflight_batch)
    approve = batch_commands.add_parser("approve")
    _common(approve)
    approve.add_argument("--batch", required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--approved-at", required=True)
    approve.set_defaults(handler=handlers.approve_batch)
    batch_list = batch_commands.add_parser("list")
    _common(batch_list)
    batch_list.add_argument("--ticket", help="only batches of this ticket")
    batch_list.add_argument("--state", help="only batches in this state")
    batch_list.add_argument(
        "--open", action="store_true", help="hide batches already in a terminal state"
    )
    batch_list.set_defaults(handler=handlers.list_batches)
    batch_abandon = batch_commands.add_parser("abandon")
    _common(batch_abandon)
    batch_abandon.add_argument("--batch", required=True)
    batch_abandon.add_argument("--approved-by", required=True)
    batch_abandon.add_argument("--approved-at", required=True)
    batch_abandon.add_argument(
        "--reason", required=True, help="why this batch can no longer be decided"
    )
    batch_abandon.set_defaults(handler=handlers.abandon_batch)
    batch_not_required = batch_commands.add_parser(
        "not-required",
        help="record that the pinned snapshot needs no implementation",
    )
    _common(batch_not_required)
    batch_not_required.add_argument("--batch", required=True)
    batch_not_required.add_argument("--approved-by", required=True)
    batch_not_required.add_argument("--approved-at", required=True)
    batch_not_required.add_argument(
        "--reason", required=True, help="evidence that no implementation is required"
    )
    batch_not_required.set_defaults(handler=handlers.mark_batch_not_required)
    decide = batch_commands.add_parser("decide")
    _common(decide)
    decide.add_argument("--batch", required=True)
    decide.add_argument("--decision", choices=sorted(defaults.DECISIONS), required=True)
    decide.add_argument("--approved-by", required=True)
    decide.add_argument("--approved-at", required=True)
    decide.add_argument("--note", default="none")
    decide.add_argument(
        "--reason", help="required for abandon: why the batch is abandoned"
    )
    decide.add_argument(
        "--reason-category",
        choices=defaults.RETRY_REASON_CATEGORIES,
        help="why the reporting role stopped, for retry; structured report data overrides an unsupported claim",
    )
    decide.add_argument(
        "--retry-role",
        choices=["developer"],
        help="force a developer retry where the coordinator would re-run the same candidate",
    )
    decide.set_defaults(handler=handlers.decide_batch)
    attention = batch_commands.add_parser(
        "attention", help="operational-loop attention state of a batch"
    )
    attention_commands = attention.add_subparsers(
        dest="attention_command", required=True
    )
    attention_check_command = attention_commands.add_parser(
        "check",
        help="evaluate the batch and persist needs_attention when a human is needed",
    )
    _common(attention_check_command)
    attention_check_command.add_argument("--batch", required=True)
    attention_check_command.set_defaults(handler=handlers.attention_check)
    attention_resolve_command = attention_commands.add_parser(
        "resolve",
        help="a human acknowledges the open findings and lets dispatching resume",
    )
    _common(attention_resolve_command)
    attention_resolve_command.add_argument("--batch", required=True)
    attention_resolve_command.add_argument(
        "--note", required=True, help="what was checked before resuming"
    )
    attention_resolve_command.add_argument("--approved-by", required=True)
    attention_resolve_command.add_argument("--approved-at", required=True)
    attention_resolve_command.set_defaults(handler=handlers.attention_resolve)
    packet = batch_commands.add_parser(
        "decision-packet", help="render concise evidence required for an approval"
    )
    _common(packet)
    packet.add_argument("--batch", required=True)
    packet.add_argument(
        "--dispatch", help="approved or reported dispatch in this batch"
    )
    packet.set_defaults(handler=handlers.decision_packet)

    risk = commands.add_parser("risk")
    risk_commands = risk.add_subparsers(dest="risk_command", required=True)
    assess = risk_commands.add_parser("assess")
    _common(assess)
    assess.add_argument("--batch", required=True)
    assess.add_argument("--candidate-commit", required=True)
    assess.add_argument(
        "--base-commit",
        help="optional immutable diff base for a multi-commit candidate",
    )
    assess.add_argument("--changed-file", action="append", required=True)
    assess.add_argument("--developer-trigger", action="append")
    assess.set_defaults(handler=handlers.assess_risk)

    context_package = commands.add_parser("context-package")
    context_package_commands = context_package.add_subparsers(
        dest="context_package_command", required=True
    )
    context_package_register = context_package_commands.add_parser("register")
    _common(context_package_register)
    context_package_register.add_argument("--batch", required=True)
    context_package_register.add_argument("--candidate-commit", required=True)
    context_package_register.add_argument(
        "--role",
        default="shared",
        choices=["shared"],
        help="packages are shared across role sessions",
    )
    context_package_register.add_argument(
        "--inclusion-reason", default="manual immutable context registration"
    )
    context_package_register.add_argument(
        "--base-commit", help="optional immutable diff base; defaults to the batch base"
    )
    context_package_register.add_argument(
        "--symbol-graph-depth",
        type=int,
        default=None,
        help="override context_package_policy.symbol_graph_depth for this package only",
    )
    context_package_register.add_argument(
        "--max-related-tests",
        type=int,
        default=None,
        help="override context_package_policy.max_related_tests for this package only",
    )
    context_package_register.add_argument("--min-starting-files", type=int, default=5)
    context_package_register.add_argument("--max-starting-files", type=int, default=10)
    context_package_register.add_argument(
        "--max-package-size-bytes",
        type=int,
        help="legacy diagnostic ceiling; token policy remains authoritative",
    )
    context_package_register.add_argument(
        "--max-package-tokens", type=int, help="optional stricter token ceiling"
    )
    context_package_register.set_defaults(handler=handlers.register_context_package)

    dispatch = commands.add_parser("dispatch")
    dispatch_commands = dispatch.add_subparsers(dest="dispatch_command", required=True)
    preflight = dispatch_commands.add_parser(
        "preflight", help="validate a future dispatch without creating it"
    )
    _common(preflight)
    preflight.add_argument("--batch", required=True)
    preflight.add_argument("--role", required=True)
    preflight.add_argument("--runtime")
    preflight.add_argument("--candidate-commit")
    preflight.set_defaults(handler=handlers.preflight_dispatch)
    dispatch_propose = dispatch_commands.add_parser(
        "propose",
        help="render the canonical transition and its digest for approval; writes no brief",
    )
    dispatch_create = dispatch_commands.add_parser("create", aliases=["approve"])
    for dispatch_shape in (dispatch_propose, dispatch_create):
        _common(dispatch_shape)
        dispatch_shape.add_argument("--batch", required=True)
        dispatch_shape.add_argument("--role", default="developer")
        dispatch_shape.add_argument(
            "--runtime",
            help="named runtime from the role assignment plan; required when a multi-runtime role has no default_runtime",
        )
        dispatch_shape.add_argument(
            "--purpose", choices=sorted(defaults.DISPATCH_PURPOSES), default="work"
        )
        dispatch_shape.add_argument("--candidate-commit")
        dispatch_shape.add_argument(
            "--delta-review-of",
            help="prior retried code-review dispatch id this test-only fix delta-reviews; code-review role only",
        )
        dispatch_shape.add_argument(
            "--model",
            help="session model, used only without .harness/orchestration.json",
        )
        dispatch_shape.add_argument(
            "--effort",
            help="session effort, used only without .harness/orchestration.json",
        )
    dispatch_propose.set_defaults(
        handler=handlers.create_dispatch,
        propose=True,
        transition_digest=None,
        approved_by=None,
        approved_at=None,
    )
    dispatch_create.add_argument(
        "--approved-by", help="required by manual_all and risk milestones"
    )
    dispatch_create.add_argument(
        "--approved-at", help="required by manual_all and risk milestones"
    )
    dispatch_create.add_argument(
        "--transition-digest",
        help="the transition_digest 'dispatch propose' printed for exactly this dispatch; required with --approved-by, so an approval is valid only for the transition it was shown",
    )
    dispatch_create.set_defaults(handler=handlers.create_dispatch, propose=False)
    dispatch_send = dispatch_commands.add_parser("send")
    _common(dispatch_send)
    dispatch_send.add_argument("--dispatch", required=True)
    dispatch_send.add_argument(
        "--adapter", help="runtime adapter; required for the orca transport only"
    )
    dispatch_send.add_argument("--adapter-arg", action="append")
    dispatch_send.add_argument(
        "--checkout",
        help="required for role code-review only: path to a worktree checked out at the dispatch's "
        "candidate_commit; every other role omits this",
    )
    dispatch_send.set_defaults(handler=handlers.send_dispatch)
    dispatch_cancel = dispatch_commands.add_parser("cancel")
    _common(dispatch_cancel)
    dispatch_cancel.add_argument("--dispatch", required=True)
    dispatch_cancel.add_argument("--approved-by", required=True)
    dispatch_cancel.add_argument("--approved-at", required=True)
    dispatch_cancel.add_argument(
        "--reason",
        required=True,
        help="why this approved brief must not reach a runtime",
    )
    dispatch_cancel.set_defaults(handler=handlers.cancel_dispatch)
    dispatch_self_report = dispatch_commands.add_parser("self-report")
    _common(dispatch_self_report)
    dispatch_self_report.add_argument("--dispatch", required=True)
    dispatch_self_report.add_argument(
        "--model", required=True, help="the model the role is actually running"
    )
    dispatch_self_report.add_argument(
        "--worktree",
        help="canonical Git worktree from git rev-parse --show-toplevel; required when project policy enables worker attestation",
    )
    dispatch_self_report.set_defaults(handler=handlers.self_report_dispatch)
    dispatch_heartbeat = dispatch_commands.add_parser("heartbeat")
    _common(dispatch_heartbeat)
    dispatch_heartbeat.add_argument("--dispatch", required=True)
    dispatch_heartbeat.add_argument("--note", default="none")
    dispatch_heartbeat.add_argument(
        "--context-tokens",
        type=int,
        help="coordinator-measured token count from a live context probe (issue #206); requires --context-source",
    )
    dispatch_heartbeat.add_argument(
        "--context-source",
        choices=["probe"],
        help="source of --context-tokens; only a coordinator-run probe, never a role self-report",
    )
    dispatch_heartbeat.set_defaults(handler=handlers.heartbeat_dispatch)
    dispatch_rate_limited = dispatch_commands.add_parser(
        "rate-limited", help="record a checkpointed provider 429 and retry window"
    )
    _common(dispatch_rate_limited)
    dispatch_rate_limited.add_argument("--dispatch", required=True)
    dispatch_rate_limited.add_argument(
        "--retry-after-seconds",
        type=int,
        default=defaults.DEFAULT_RATE_LIMIT_RETRY_SECONDS,
    )
    dispatch_rate_limited.set_defaults(handler=handlers.rate_limited_dispatch)
    dispatch_wait = dispatch_commands.add_parser(
        "wait", help="wait locally for reported, stale, mismatch, 429 or failure"
    )
    _common(dispatch_wait)
    dispatch_wait.add_argument("--dispatch", required=True)
    dispatch_wait.add_argument("--timeout", type=int, default=60)
    dispatch_wait.add_argument("--poll-interval", type=int, default=5)
    dispatch_wait.add_argument(
        "--stale-after", type=int, default=defaults.DEFAULT_STALE_AFTER_SECONDS
    )
    dispatch_wait.set_defaults(handler=handlers.wait_dispatch)
    dispatch_pressure = dispatch_commands.add_parser(
        "context-pressure",
        help="record a provider/runtime-observed context measurement; observation only, never a routing input",
    )
    _common(dispatch_pressure)
    dispatch_pressure.add_argument("--dispatch", required=True)
    dispatch_pressure.add_argument(
        "--observed-tokens",
        type=int,
        help="tokens the provider or runtime observed; omit to ask the configured context_telemetry_provider",
    )
    dispatch_pressure.add_argument(
        "--source",
        choices=defaults.CONTEXT_TELEMETRY_SOURCES,
        help="who observed --observed-tokens; a model's self-report is never accepted",
    )
    dispatch_pressure.set_defaults(handler=handlers.record_context_pressure)
    dispatch_telemetry = dispatch_commands.add_parser(
        "telemetry", help="record source-observed worker or coordinator metrics"
    )
    _common(dispatch_telemetry)
    dispatch_telemetry.add_argument("--file", required=True)
    dispatch_telemetry.set_defaults(handler=handlers.record_telemetry)
    dispatch_checkpoint = dispatch_commands.add_parser("checkpoint")
    _common(dispatch_checkpoint)
    dispatch_checkpoint.add_argument("--file", required=True)
    dispatch_checkpoint.set_defaults(handler=handlers.checkpoint_dispatch)
    dispatch_resume = dispatch_commands.add_parser("resume")
    _common(dispatch_resume)
    dispatch_resume.add_argument("--dispatch", required=True)
    dispatch_resume.add_argument(
        "--termination-reason",
        default="",
        help="runtime adapter termination reason; a recognized rate limit (rate_limit/rate-limit/429) authorizes the new worker session automatically",
    )
    dispatch_resume.add_argument(
        "--trigger",
        choices=sorted(defaults.PLANNED_TRIGGER_KINDS),
        help="planned-trigger kind; required unless --termination-reason is a recognized rate limit",
    )
    dispatch_resume.add_argument(
        "--measured-value",
        type=int,
        help="measured value for a context-limit/tdd-cycles/failure-log planned trigger, checked against the project's adaptive_continuation_policy threshold",
    )
    dispatch_resume.add_argument(
        "--file",
        help="continuation facts JSON restating remaining DoD/risks/blockers/dependencies; required for a planned-trigger continuation",
    )
    dispatch_resume.add_argument("--approved-by")
    dispatch_resume.add_argument("--approved-at")
    dispatch_resume.add_argument("--note")
    dispatch_resume.set_defaults(handler=handlers.resume_dispatch)
    dispatch_status_command = dispatch_commands.add_parser("status")
    _common(dispatch_status_command)
    dispatch_status_command.add_argument("--dispatch")
    dispatch_status_command.add_argument("--batch")
    dispatch_status_command.add_argument(
        "--stale-after", type=int, default=defaults.DEFAULT_STALE_AFTER_SECONDS
    )
    dispatch_status_command.set_defaults(handler=handlers.dispatch_status)
    dispatch_publish = dispatch_commands.add_parser("publish")
    _common(dispatch_publish)
    dispatch_publish.add_argument("--dispatch", required=True)
    dispatch_publish.add_argument("--remote", default="origin")
    dispatch_publish.set_defaults(handler=handlers.publish_dispatch)

    qa = commands.add_parser("qa")
    qa_commands = qa.add_subparsers(dest="qa_command", required=True)
    qa_run = qa_commands.add_parser("run")
    _common(qa_run)
    qa_run.add_argument("--dispatch", required=True)
    qa_run.add_argument("--lease-seconds", type=int, default=1800)
    qa_run.set_defaults(handler=handlers.run_qa)
    qa_status_command = qa_commands.add_parser("status")
    _common(qa_status_command)
    qa_status_command.set_defaults(handler=handlers.qa_status)
    qa_evidence_command = qa_commands.add_parser("evidence")
    _common(qa_evidence_command)
    qa_evidence_command.add_argument(
        "--batch",
        help="optional batch ID when multiple batches accepted the same candidate",
    )
    qa_evidence_command.add_argument("--ticket", required=True)
    qa_evidence_command.add_argument("--branch", required=True)
    qa_evidence_command.add_argument("--candidate-commit", required=True)
    qa_evidence_command.set_defaults(handler=handlers.qa_evidence)
    qa_clear = qa_commands.add_parser("clear-stale-lease")
    _common(qa_clear)
    qa_clear.add_argument("--approved-by", required=True)
    qa_clear.add_argument("--approved-at", required=True)
    qa_clear.add_argument("--expected-host", required=True)
    qa_clear.add_argument("--expected-pid", type=int, required=True)
    qa_clear.add_argument("--expected-expiry", required=True)
    qa_clear.add_argument("--reason", required=True)
    qa_clear.set_defaults(handler=handlers.clear_qa_lease)

    report = commands.add_parser("report")
    report_commands = report.add_subparsers(dest="report_command", required=True)
    report_submit = report_commands.add_parser("submit", aliases=["record"])
    _common(report_submit)
    report_submit.add_argument("--file", required=True)
    report_submit.set_defaults(handler=handlers.submit_report)
    return root
