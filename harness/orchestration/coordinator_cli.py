"""Маршрутизация аргументов командной строки для координатора оркестрации.

Этот модуль намеренно знает только имена команд и структуру аргументов. Доменные
обработчики внедряются через :mod:`coordinator`, что предотвращает связывание изменений
парсера с переходами состояний реестра жизненного цикла.
"""

from __future__ import annotations

import argparse
import types


def _common(parser: argparse.ArgumentParser) -> None:
    """Добавить общие аргументы командной строки (--repo, --state-dir) к парсеру."""
    parser.add_argument("--repo", default=argparse.SUPPRESS, help="target project root")
    parser.add_argument(
        "--state-dir", default=argparse.SUPPRESS, help="coordinator state directory"
    )


def build_parser(
    handlers: types.ModuleType, defaults: types.ModuleType
) -> argparse.ArgumentParser:
    """Собрать стабильный публичный CLI с использованием переданного фасада обработчиков координатора."""
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
        (
            "release-lock",
            handlers.release_ledger_lock,
            "Release a stuck ledger lock after checking its owner; a live owner is refused",
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
        "--allowed-path",
        action="append",
        help="repo-relative path or glob the batch's writer may change; repeated; the batch's explicit write scope",
    )
    create.add_argument(
        "--zone",
        help="legacy audit label recorded in the batch; it neither locks nor scopes anything",
    )
    create.add_argument(
        "--integration-ref",
        help="branch on origin this batch's base is fetched and pinned against; falls back to the project's base_branch for epic-less tasks",
    )
    create.add_argument(
        "--goal", help="approved local ticket goal frozen in the immutable batch plan"
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
    create.add_argument(
        "--supersedes",
        help="abandoned batch of the same ticket and issue branch this batch resumes from its "
        "abandoned.last_accepted record; requires --approved-by and --approved-at",
    )
    create.add_argument(
        "--approved-by", help="human who approved --supersedes; never a policy"
    )
    create.add_argument("--approved-at", help="time of the --supersedes approval")
    create.set_defaults(handler=handlers.create_batch)
    batch_preflight = batch_commands.add_parser(
        "preflight", help="reject an oversized ticket before creating a batch"
    )
    _common(batch_preflight)
    batch_preflight.add_argument("--ticket", required=True)
    batch_preflight.add_argument("--allowed-path", action="append")
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
    approve.add_argument(
        "--approved-by",
        help="human who approved the batch plan; omitted only under approval_policy auto, "
        "which records the approval as policy:auto",
    )
    approve.add_argument(
        "--approved-at",
        help="time of the approval; omitted only under approval_policy auto",
    )
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
    batch_resume = batch_commands.add_parser(
        "resume", help="resume a startup-blocked batch from its accepted ledger history"
    )
    _common(batch_resume)
    batch_resume.add_argument("--batch", required=True)
    batch_resume.add_argument("--reason", required=True)
    batch_resume.set_defaults(handler=handlers.resume_batch)
    batch_restore_runtime = batch_commands.add_parser(
        "restore-runtime",
        help="store the pinned runtime snapshot a batch needs after the installed runtime changed",
    )
    _common(batch_restore_runtime)
    batch_restore_runtime.add_argument("--batch", required=True)
    batch_restore_runtime.add_argument(
        "--from",
        dest="source",
        required=True,
        help=".harness directory installed from the revision the batch was planned on",
    )
    batch_restore_runtime.set_defaults(
        handler=handlers.restore_batch_runtime, installed_runtime_only=True
    )
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
    decide.add_argument(
        "--commit-plan-file",
        help="only with --decision accept on an architect report: a JSON file "
        '{"commit_plan": [{"id", "summary", "expected_paths", "covers"}, ...]} pinned as the '
        "batch's developer commit plan",
    )
    decide.add_argument(
        "--findings-file",
        help="only with --decision accept or override-warning on a developer work report: a "
        'JSON file {"findings": [{"summary", "files", "expected_evidence"}, ...]} of coordinator '
        "findings every later code-review brief carries until a review settles them",
    )
    decide.add_argument(
        "--carry-incomplete",
        action="store_true",
        help="only with --decision accept or override-warning on a read-only report that lists "
        "incomplete_items: carry each item into the work brief of its target role until an "
        "accepted dispatch of that role carried it",
    )
    decide.add_argument(
        "--narrowed",
        action="store_true",
        help="only with --decision retry on a read-only report that lists incomplete_items: "
        "re-run the same role on the same SHA with a brief that carries only those items",
    )
    decide.set_defaults(handler=handlers.decide_batch)
    auto_decide = batch_commands.add_parser(
        "auto-decide",
        help="under approval_policy auto: take the policy decision on the pending report and "
        "record it as policy:auto; a stop ends the automatic path of the batch",
    )
    _common(auto_decide)
    auto_decide.add_argument("--batch", required=True)
    auto_decide.add_argument(
        "--findings-file",
        help="a developer work report the policy accepts: coordinator findings carried into "
        "code-review, as for batch decide --findings-file",
    )
    auto_decide.add_argument(
        "--commit-plan-file",
        help="an architect report the policy accepts: the architect's commit plan, pinned when "
        "it lies inside allowed_paths and covers every definition-of-done item",
    )
    auto_decide.add_argument(
        "--bug-ticket",
        help="the tracker ticket of the tool that blocked the role; required for a tooling-retry",
    )
    auto_decide.add_argument(
        "--block-bypass",
        action="store_true",
        help="the role worked around a hook or tool block; requires --note naming the violation",
    )
    auto_decide.add_argument(
        "--note", help="added to the deterministic rationale of the decision"
    )
    auto_decide.set_defaults(handler=handlers.auto_decide)
    auto_report = batch_commands.add_parser(
        "auto-report",
        help="render the final report of an approval_policy auto batch; records a stop the "
        "ledger shows, else renders the live report",
    )
    _common(auto_report)
    auto_report.add_argument("--batch", required=True)
    auto_report.set_defaults(handler=handlers.auto_report)
    carry_over = batch_commands.add_parser(
        "carry-over",
        help="carry coordinator findings into review after the developer report was accepted, "
        "before its code-review dispatch is created",
    )
    _common(carry_over)
    carry_over.add_argument("--batch", required=True)
    carry_over.add_argument(
        "--findings-file",
        required=True,
        help='a JSON file {"findings": [{"summary", "files", "expected_evidence"}, ...]}',
    )
    carry_over.set_defaults(handler=handlers.carry_over_findings)
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
    packet.add_argument(
        "--reason-category",
        choices=defaults.RETRY_REASON_CATEGORIES,
        help="preview the retry route as batch decide computes it with this --reason-category; the preview does not check the developer-retry budget",
    )
    packet.add_argument(
        "--retry-role",
        choices=["developer"],
        help="preview the retry route as batch decide computes it with --retry-role developer; the preview does not check the developer-retry budget",
    )
    packet.add_argument(
        "--commit-plan-file",
        help="preview the scope_warnings batch decide --commit-plan-file records when it pins "
        "this commit plan on the pending architect report",
    )
    packet.add_argument(
        "--findings-file",
        help="preview the carry-over route that batch decide --findings-file on the pending "
        "developer report, or else batch carry-over, records with this findings file",
    )
    packet.add_argument(
        "--narrowed",
        action="store_true",
        help="preview the retry route as batch decide --decision retry --narrowed computes it",
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
    context_package_register.add_argument(
        "--no-memory",
        action="store_true",
        help="freeze a package without project memory",
    )
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
    context_package_register.add_argument("--min-starting-files", type=int)
    context_package_register.add_argument("--max-starting-files", type=int)
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
    preflight.add_argument(
        "--purpose", choices=sorted(defaults.DISPATCH_PURPOSES), default="work"
    )
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
        dispatch_shape.add_argument(
            "--no-memory",
            action="store_true",
            help="bypass project memory for this immutable dispatch package",
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
        "--adapter", help="runtime adapter; required for the external transport only"
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
        default=None,
    )
    dispatch_rate_limited.set_defaults(handler=handlers.rate_limited_dispatch)
    dispatch_wait = dispatch_commands.add_parser(
        "wait", help="wait locally for reported, stale, mismatch, 429 or failure"
    )
    _common(dispatch_wait)
    dispatch_wait.add_argument("--dispatch", required=True)
    dispatch_wait.add_argument("--timeout", type=int)
    dispatch_wait.add_argument("--poll-interval", type=int)
    dispatch_wait.add_argument("--stale-after", type=int)
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
    dispatch_status_command.add_argument("--stale-after", type=int)
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
    qa_run.add_argument("--lease-seconds", type=int)
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

    integration = commands.add_parser(
        "integration",
        help="record and observe the integration link of a published ticket branch",
    )
    integration_commands = integration.add_subparsers(
        dest="integration_command", required=True
    )
    local_qa = integration_commands.add_parser(
        "local-qa",
        help="run full local QA when combined-result CI cannot verify the pair",
    )
    _common(local_qa)
    local_qa.add_argument("--record", required=True)
    local_qa.add_argument(
        "--ci-condition", choices=defaults.LOCAL_QA_CI_CONDITIONS, required=True
    )
    local_qa.add_argument("--reason", required=True)
    local_qa.add_argument("--request", help="resume only this pinned request ID")
    local_qa.add_argument(
        "--retry", action="store_true", help="explicitly retry an operational attempt"
    )
    local_qa.add_argument("--lease-seconds", type=int)
    local_qa.set_defaults(handler=handlers.integration_local_qa)
    integration_prepare = integration_commands.add_parser(
        "prepare",
        help="record the link between ticket, branch, source batch, published candidate and target SHA; idempotent",
    )
    _common(integration_prepare)
    integration_prepare.add_argument("--ticket", required=True)
    integration_prepare.add_argument("--branch", required=True)
    integration_prepare.add_argument(
        "--batch",
        help="source batch ID; required when several batches published the branch",
    )
    integration_prepare.add_argument(
        "--candidate-commit",
        help="optional published SHA to check against the accepted publish report",
    )
    integration_prepare.add_argument("--remote", default="origin")
    integration_prepare.set_defaults(handler=handlers.integration_prepare)
    integration_status = integration_commands.add_parser(
        "status",
        help="read-only: observe whether a record's candidate/target pair is still current",
    )
    _common(integration_status)
    integration_status.add_argument("--record", help="integration record ID")
    integration_status.add_argument("--ticket")
    integration_status.add_argument("--branch")
    integration_status.add_argument(
        "--batch",
        help="source batch ID when --ticket and --branch match several records",
    )
    integration_status.set_defaults(handler=handlers.integration_status)
    integration_next = integration_commands.add_parser(
        "next",
        help="read-only: the next step of a PR continuation (refresh, resolver, route a failed check, confirm, verify, hand over)",
    )
    _common(integration_next)
    integration_next.add_argument("--record", help="integration record ID")
    integration_next.add_argument("--ticket")
    integration_next.add_argument("--branch")
    integration_next.add_argument(
        "--batch",
        help="source batch ID when --ticket and --branch match several records",
    )
    integration_next.add_argument(
        "--pull-request",
        type=int,
        help="the opened pull request; without it the step is the one before the pull request",
    )
    integration_next.set_defaults(handler=handlers.integration_next)
    integration_link = integration_commands.add_parser(
        "link-evidence",
        help="register a new CI, local-QA or resolver check of a candidate/target pair; idempotent",
    )
    _common(integration_link)
    integration_link.add_argument("--record", required=True)
    integration_link.add_argument(
        "--kind", required=True, choices=defaults.INTEGRATION_EVIDENCE_KINDS
    )
    integration_link.add_argument("--candidate-commit", required=True)
    integration_link.add_argument("--target-commit", required=True)
    integration_link.add_argument(
        "--result", required=True, choices=defaults.INTEGRATION_EVIDENCE_RESULTS
    )
    integration_link.add_argument(
        "--reference", required=True, help="where the check result can be inspected"
    )
    integration_link.add_argument(
        "--artifact-sha256", help="digest of the artifact the reference points to"
    )
    integration_link.set_defaults(handler=handlers.integration_link_evidence)
    integration_collect = integration_commands.add_parser(
        "collect-ci",
        help="collect CI evidence for the combined result of a pull request; records only accepted or failed evidence",
    )
    _common(integration_collect)
    integration_collect.add_argument("--record", help="integration record ID")
    integration_collect.add_argument("--ticket")
    integration_collect.add_argument("--branch")
    integration_collect.add_argument(
        "--batch",
        help="source batch ID when --ticket and --branch match several records",
    )
    integration_collect.add_argument("--pull-request", required=True, type=int)
    integration_collect.set_defaults(handler=handlers.integration_collect_ci)
    integration_refresh = integration_commands.add_parser(
        "refresh",
        help="PR preparation: rebase the own issue branch onto the current integration SHA when it moved",
    )
    _common(integration_refresh)
    integration_refresh.add_argument("--record", help="integration record ID")
    integration_refresh.add_argument("--ticket")
    integration_refresh.add_argument("--branch")
    integration_refresh.add_argument(
        "--batch",
        help="source batch ID when --ticket and --branch match several records",
    )
    integration_refresh.set_defaults(handler=handlers.integration_refresh)
    integration_resolve = integration_commands.add_parser(
        "resolve",
        help="a textual conflict with the integration tip: create the conflict-resolver batch (nothing is written to Git)",
    )
    _common(integration_resolve)
    integration_resolve.add_argument("--record", help="integration record ID")
    integration_resolve.add_argument("--ticket")
    integration_resolve.add_argument("--branch")
    integration_resolve.add_argument(
        "--batch",
        help="source batch ID when --ticket and --branch match several records",
    )
    integration_resolve.set_defaults(handler=handlers.integration_resolve)
    resolver_event = integration_commands.add_parser(
        "resolver-event",
        help="record a human decision or a scope change of a conflict-resolver dispatch as its own audit event",
    )
    _common(resolver_event)
    resolver_event.add_argument("--record", help="integration record ID")
    resolver_event.add_argument("--ticket")
    resolver_event.add_argument("--branch")
    resolver_event.add_argument("--batch", help="source batch ID")
    resolver_event.add_argument(
        "--kind", required=True, choices=["human-decision", "scope-change"]
    )
    resolver_event.add_argument("--dispatch", required=True)
    resolver_event.add_argument("--decided-by", required=True)
    resolver_event.add_argument("--note", required=True)
    resolver_event.add_argument("--option", help="the option id a human chose")
    resolver_event.add_argument(
        "--extends-budget",
        action="store_true",
        help="grant one more automatic resolver target after the two spent ones",
    )
    resolver_event.set_defaults(handler=handlers.resolver_event)

    report = commands.add_parser("report")
    report_commands = report.add_subparsers(dest="report_command", required=True)
    report_submit = report_commands.add_parser("submit", aliases=["record"])
    _common(report_submit)
    report_submit.add_argument("--file", required=True)
    report_submit.set_defaults(handler=handlers.submit_report)
    report_complete = report_commands.add_parser(
        "complete",
        help="run the pending steps of a recorded report's policy chain; idempotent",
    )
    _common(report_complete)
    report_complete.add_argument("--dispatch", required=True)
    report_complete.set_defaults(handler=handlers.complete_report)
    return root
