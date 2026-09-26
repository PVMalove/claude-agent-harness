"""Композитное review с учётом риска: сценарий clean-room из `scripts/test_clean_room.py`."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.clean_room.support import (
    capture,
    commit_map_for,
)


def run(ctx: SimpleNamespace) -> None:
    """Композитное review с учётом риска.

    Читает из контекста: `adapter_run`, `coordinator_path`, `coordinator_run`, `fake_adapter`,
    `fake_adapter_log`, `fake_orca_log`, `legacy_batch`, `legacy_batch_id`, `legacy_plan`,
    `lifecycle_records`, `orchestration_config`, `orchestration_project`, `orchestration_remote`,
    `staged_payload`, `state_root`, `sync_origin_base`, `test_root`, `valid_orchestration`.
    Передаёт дальше: `qa_id`, `shared_worktree`.
    """
    adapter_run = ctx.adapter_run
    coordinator_path = ctx.coordinator_path
    coordinator_run = ctx.coordinator_run
    fake_adapter = ctx.fake_adapter
    fake_adapter_log = ctx.fake_adapter_log
    fake_orca_log = ctx.fake_orca_log
    legacy_batch = ctx.legacy_batch
    legacy_batch_id = ctx.legacy_batch_id
    legacy_plan = ctx.legacy_plan
    lifecycle_records = ctx.lifecycle_records
    orchestration_config = ctx.orchestration_config
    orchestration_project = ctx.orchestration_project
    orchestration_remote = ctx.orchestration_remote
    staged_payload = ctx.staged_payload
    state_root = ctx.state_root
    sync_origin_base = ctx.sync_origin_base
    test_root = ctx.test_root
    valid_orchestration = ctx.valid_orchestration
    damaged_ledger_state = test_root / "damaged-ledger-state"
    (damaged_ledger_state / "batches").mkdir(parents=True)
    (damaged_ledger_state / "batches" / "batch-damaged.json").write_text(
        "{ not JSON", encoding="utf-8"
    )
    damaged = coordinator_run(
        "--state-dir", str(damaged_ledger_state), "ledger", "migrate"
    )
    if damaged.returncode == 0 or (damaged_ledger_state / "ledger.json").exists():
        sys.exit("ledger migration accepted damaged state or switched it into service")

    incomplete_ledger_state = test_root / "incomplete-ledger-state"
    (incomplete_ledger_state / "batches").mkdir(parents=True)
    (incomplete_ledger_state / "plans").mkdir(parents=True)
    incomplete_batch = dict(legacy_batch)
    incomplete_batch["dispatches"] = [
        {
            "dispatch_id": "dispatch-00000000-0000-0000-0000-000000000123",
            "role": "architect",
            "state": "approved",
            "brief_sha256": "missing-evidence",
        }
    ]
    (incomplete_ledger_state / "batches" / f"{legacy_batch_id}.json").write_text(
        json.dumps(incomplete_batch, indent=2) + "\n", encoding="utf-8"
    )
    (incomplete_ledger_state / "plans" / f"{legacy_batch_id}.json").write_text(
        json.dumps(legacy_plan, indent=2) + "\n", encoding="utf-8"
    )
    incomplete = coordinator_run(
        "--state-dir", str(incomplete_ledger_state), "ledger", "migrate"
    )
    if incomplete.returncode == 0 or (incomplete_ledger_state / "ledger.json").exists():
        sys.exit("ledger migration accepted incomplete dispatch evidence")

    baseline_file = orchestration_project / "README.md"
    baseline_file.write_text("clean-room baseline\n", encoding="utf-8")
    # The QA lane runs this project's own verification command verbatim. Keep that command
    # dependency-free: a third-party test runner is not installed on every clean-room host, and the
    # gate is here to prove the lane's behaviour, not pytest's.
    (orchestration_project / "qa_baseline.py").write_text(
        'print("token=visible")\n', encoding="utf-8"
    )
    (orchestration_project / "developer_check.py").write_text(
        'print("focused developer check")\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=orchestration_project, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "chore: create baseline"],
        cwd=orchestration_project,
        check=True,
    )
    subprocess.run(
        ["git", "branch", "feature/issue-901-coordinator"],
        cwd=orchestration_project,
        check=True,
    )

    # `batch create` eagerly validates that `--worktree` names a path git itself already tracks
    # as a linked worktree (coordinator.py's `_validate_worktree`). The validation only checks
    # membership in `git worktree list`, not that the worktree is checked out to the batch's own
    # branch, so every synthetic batch below can point at this one shared, detached worktree.
    shared_worktree = test_root / "shared-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(shared_worktree), "master"],
        cwd=orchestration_project,
        check=True,
    )

    # An assignment plan must never silently launch an external runtime.  A role without an
    # explicit transport stays in the coordinator session, and an approved-but-unsent brief can
    # be cancelled without abandoning its whole batch or losing the audit trail.
    implicit_transport_state = test_root / "implicit-transport-state"
    active_orchestration_config = orchestration_config.read_text(encoding="utf-8")
    implicit_transport = json.loads(json.dumps(valid_orchestration))
    del implicit_transport["assignment_plans"]["architect"]["transport"]
    orchestration_config.write_text(
        json.dumps(implicit_transport, indent=2) + "\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "branch", "feature/issue-907-implicit-transport"],
        cwd=orchestration_project,
        check=True,
    )
    sync_origin_base("integration/test-907")
    implicit_batch = json.loads(
        coordinator_run(
            "--state-dir",
            str(implicit_transport_state),
            "batch",
            "create",
            "--ticket",
            "#907",
            "--branch",
            "feature/issue-907-implicit-transport",
            "--worktree",
            str(shared_worktree),
            "--zone",
            "backend",
            "--definition-of-done",
            "confirm the dispatch transport",
            "--prohibited-change",
            "do not merge",
            "--integration-ref",
            "integration/test-907",
        ).stdout
    )["batch_id"]
    coordinator_run(
        "--state-dir",
        str(implicit_transport_state),
        "batch",
        "approve",
        "--batch",
        implicit_batch,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T11:59:00Z",
    )
    implicit_dispatch = coordinator_run(
        "--state-dir",
        str(implicit_transport_state),
        "dispatch",
        "create",
        "--batch",
        implicit_batch,
        "--role",
        "architect",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T11:59:10Z",
    )
    if implicit_dispatch.returncode != 0:
        sys.exit(
            "coordinator rejected the implicit in-process transport: "
            + implicit_dispatch.stderr
        )
    implicit_dispatch_id = json.loads(implicit_dispatch.stdout)["dispatch_id"]
    if (
        json.loads(implicit_dispatch.stdout)["brief"]["resolved_transport"]
        != "in-process"
    ):
        sys.exit("an omitted role transport did not resolve to in-process")
    # The corrected assignment can already be on disk; cancellation must still be possible because
    # it audits the immutable brief rather than validating it as a newly executable dispatch.
    orchestration_config.write_text(active_orchestration_config, encoding="utf-8")
    cancelled = coordinator_run(
        "--state-dir",
        str(implicit_transport_state),
        "dispatch",
        "cancel",
        "--dispatch",
        implicit_dispatch_id,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T11:59:20Z",
        "--reason",
        "correct the runtime before any worker starts",
    )
    if (
        cancelled.returncode != 0
        or json.loads(cancelled.stdout)["state"] != "cancelled"
    ):
        sys.exit(
            "coordinator could not cancel an approved but unsent dispatch: "
            + cancelled.stderr
        )
    cancelled_batch = json.loads(
        (
            lifecycle_records(implicit_transport_state)
            / "batches"
            / f"{implicit_batch}.json"
        ).read_text(encoding="utf-8")
    )
    if (
        cancelled_batch["state"] != "awaiting-approval"
        or cancelled_batch["dispatches"][0]["state"] != "cancelled"
    ):
        sys.exit(
            "dispatch cancellation did not preserve an awaiting-approval batch audit trail"
        )

    # A Windows Python configured with a legacy output encoding must still emit valid UTF-8 JSON;
    # otherwise a Git Bash display artifact looks like corrupted Russian batch evidence.
    unicode_state = test_root / "unicode-output-state"
    unicode_env = dict(os.environ, PYTHONIOENCODING="cp1251")
    sync_origin_base("integration/test-908")
    # The ticket title stays multilingual: it names the human's work item and is not the
    # agent-to-agent brief text the language contract governs.
    unicode_ticket = "#908 Проверить кодировку"
    unicode_result = subprocess.run(
        [
            sys.executable,
            str(coordinator_path),
            "--repo",
            str(orchestration_project),
            "--state-dir",
            str(unicode_state),
            "batch",
            "create",
            "--ticket",
            unicode_ticket,
            "--branch",
            "feature/issue-908-unicode-output",
            "--worktree",
            str(shared_worktree),
            "--zone",
            "backend",
            "--definition-of-done",
            "verify console encoding",
            "--prohibited-change",
            "do not change stored data",
            "--integration-ref",
            "integration/test-908",
            "--expected-file",
            "backend/encoding.py",
            "--expected-service",
            "backend",
            "--expected-changed-lines",
            "10",
        ],
        capture_output=True,
        env=unicode_env,
        check=False,
    )
    if unicode_result.returncode != 0:
        sys.exit(
            "coordinator rejected the Unicode output fixture: "
            + unicode_result.stderr.decode("utf-8", errors="replace")
        )
    if json.loads(unicode_result.stdout.decode("utf-8"))["ticket"] != unicode_ticket:
        sys.exit(
            "coordinator stdout is not UTF-8 when Python inherits a legacy output encoding"
        )

    # The same contract the roles are told to honour, enforced where it is machine-checkable: a
    # brief field written in Russian never reaches a worker prompt in the first place.
    sync_origin_base("integration/test-909")
    non_english = coordinator_run(
        "--state-dir",
        str(unicode_state),
        "batch",
        "create",
        "--ticket",
        "#909",
        "--branch",
        "feature/issue-909-language-contract",
        "--worktree",
        str(shared_worktree),
        "--zone",
        "backend",
        "--definition-of-done",
        "Проверить кодировку",
        "--prohibited-change",
        "do not change stored data",
        "--integration-ref",
        "integration/test-909",
    )
    if non_english.returncode == 0:
        sys.exit(
            "coordinator accepted a non-English definition_of_done into an agent-facing brief"
        )
    if "must be written in English" not in non_english.stderr:
        sys.exit(
            "coordinator rejected a non-English brief without naming the language contract"
        )

    sync_origin_base("integration/test-901")
    planned = coordinator_run(
        "batch",
        "create",
        "--ticket",
        "#901",
        "--branch",
        "feature/issue-901-coordinator",
        "--worktree",
        str(shared_worktree),
        "--zone",
        "backend",
        "--definition-of-done",
        "implement the requested backend change",
        "--prohibited-change",
        "do not merge",
        "--integration-ref",
        "integration/test-901",
    )
    if planned.returncode != 0:
        sys.exit("coordinator rejected a valid planned batch: " + planned.stderr)
    batch = json.loads(planned.stdout)
    if batch["state"] != "planned" or not batch["batch_id"].startswith("batch-"):
        sys.exit("coordinator did not create a planned batch")
    batch_id = batch["batch_id"]

    approved_batch = coordinator_run(
        "batch",
        "approve",
        "--batch",
        batch_id,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:00:00Z",
    )
    if (
        approved_batch.returncode != 0
        or json.loads(approved_batch.stdout)["state"] != "awaiting-approval"
    ):
        sys.exit(
            "coordinator did not move the batch to awaiting-approval: "
            + approved_batch.stderr
            + approved_batch.stdout
        )

    # The architect gate is a hard ordering rule, not a convention: no developer brief exists for a
    # batch until an architect report for it has been accepted.
    premature = coordinator_run(
        "dispatch",
        "create",
        "--batch",
        batch_id,
        "--role",
        "developer",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:00:30Z",
    )
    if premature.returncode == 0:
        sys.exit(
            "coordinator created a developer dispatch without an accepted architect report"
        )
    if "accepted architect report" not in premature.stderr:
        sys.exit(
            "coordinator did not explain the missing architect report: "
            + premature.stderr
        )

    architect = coordinator_run(
        "dispatch",
        "create",
        "--batch",
        batch_id,
        "--role",
        "architect",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:00:40Z",
    )
    if architect.returncode != 0:
        sys.exit("coordinator rejected a valid architect dispatch: " + architect.stderr)
    architect_record = json.loads(architect.stdout)
    architect_id = architect_record["dispatch_id"]
    if architect_record["brief"]["resolved_transport"] != "orca":
        sys.exit("configured assignment plan did not default to the orca transport")
    architect_sent = coordinator_run(
        "dispatch", "send", "--dispatch", architect_id, "--adapter", str(fake_adapter)
    )
    if architect_sent.returncode != 0:
        sys.exit("coordinator rejected the architect handoff: " + architect_sent.stderr)
    active_reset = coordinator_run("ledger", "reset", "--confirm", "RESET")
    if active_reset.returncode == 0 or "active" not in active_reset.stderr:
        sys.exit("ledger reset did not reject an active batch")

    architect_report_file = staged_payload("architect-report.json")
    architect_payload = {
        "dispatch_id": architect_id,
        "ticket": "#901",
        "role": "architect",
        "outcome": "completed",
        "output": "recorded the boundary decision and acceptance criteria",
        "commit_sha": "not applicable — read-only role",
        "changed_files": [],
        # The architect owns no verification gate: its brief approves no commands to report.
        "checks_run": [],
        "risks": "none",
        "blockers": "none",
        "next_coordinator_action": "accept and dispatch the developer",
        "report_language": "ru",
    }
    architect_report_file.write_text(
        json.dumps(architect_payload, indent=2) + "\n", encoding="utf-8"
    )
    if (
        coordinator_run(
            "report", "submit", "--file", str(architect_report_file)
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator accepted a completion report before the role confirmed its model"
        )

    # A dispatch proves it is alive twice over: once by naming the model it is actually running, and
    # then by heartbeat.  Both are watched by the coordinator, whatever transport carried the brief.
    stale_status_path = (
        lifecycle_records(state_root) / "dispatch-status" / f"{architect_id}.json"
    )
    live_status = json.loads(stale_status_path.read_text(encoding="utf-8"))
    stale_status_path.write_text(
        json.dumps(
            dict(live_status, heartbeat_at="2000-01-01T00:00:00+00:00"), indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    watchdog = coordinator_run(
        "dispatch", "status", "--batch", batch_id, "--stale-after", "60"
    )
    if watchdog.returncode != 0:
        sys.exit("coordinator could not report dispatch liveness: " + watchdog.stderr)
    watchdog_report = json.loads(watchdog.stdout)
    if architect_id not in watchdog_report["stale"]:
        sys.exit("dispatch watchdog did not flag a silent dispatch as stale")
    # A coordinator session inspecting leftovers has to see whose ticket each dispatch belongs to
    # before it proposes a new batch for that ticket.
    if {entry.get("ticket") for entry in watchdog_report["dispatches"]} != {"#901"}:
        sys.exit(
            "dispatch status does not name the ticket of each dispatch: "
            + watchdog.stdout[:200]
        )
    if (
        coordinator_run("dispatch", "heartbeat", "--dispatch", architect_id).returncode
        != 0
    ):
        sys.exit("coordinator rejected a heartbeat from a live dispatch")
    if json.loads(
        coordinator_run(
            "dispatch", "status", "--dispatch", architect_id, "--stale-after", "60"
        ).stdout
    )["stale"]:
        sys.exit("dispatch watchdog kept a heartbeating dispatch stale")

    if (
        coordinator_run(
            "dispatch",
            "self-report",
            "--dispatch",
            architect_id,
            "--model",
            "project-architect-model",
        ).returncode
        != 0
    ):
        sys.exit(
            "coordinator rejected a model self-report matching the immutable brief"
        )
    if (
        coordinator_run(
            "report", "submit", "--file", str(architect_report_file)
        ).returncode
        != 0
    ):
        sys.exit("coordinator rejected a valid architect report")
    accepted_architect = coordinator_run(
        "batch",
        "decide",
        "--batch",
        batch_id,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:00:50Z",
    )
    if (
        accepted_architect.returncode != 0
        or json.loads(accepted_architect.stdout)["next_action"] != "developer"
    ):
        sys.exit(
            "accepted architect report did not prepare the developer dispatch: "
            + accepted_architect.stderr
            + accepted_architect.stdout
        )
    fake_adapter_log.unlink(missing_ok=True)

    dispatch = coordinator_run(
        "dispatch",
        "create",
        "--batch",
        batch_id,
        "--role",
        "developer",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:01:00Z",
    )
    if dispatch.returncode != 0:
        sys.exit("coordinator rejected a valid approved dispatch: " + dispatch.stderr)
    dispatch_record = json.loads(dispatch.stdout)
    dispatch_id = dispatch_record["dispatch_id"]
    brief_file = lifecycle_records(state_root) / "dispatches" / f"{dispatch_id}.json"
    brief_bytes = brief_file.read_bytes()
    if (
        dispatch_record["state"] != "approved"
        or dispatch_record["brief"]["role"] != "developer"
    ):
        sys.exit("coordinator did not create an approved developer dispatch")

    if (
        coordinator_run(
            "dispatch",
            "send",
            "--dispatch",
            dispatch_id,
            "--adapter",
            str(fake_adapter),
            "--adapter-arg=--brief",
            "--adapter-arg=unapproved.json",
        ).returncode
        == 0
    ):
        sys.exit("coordinator allowed adapter arguments to replace the approved brief")
    if fake_adapter_log.exists():
        sys.exit(
            "coordinator invoked the adapter after rejecting an unsafe adapter argument"
        )

    sent = coordinator_run(
        "dispatch",
        "send",
        "--dispatch",
        dispatch_id,
        "--adapter",
        str(fake_adapter),
    )
    if sent.returncode != 0:
        sys.exit("coordinator rejected an approved adapter handoff: " + sent.stderr)
    adapter_args = json.loads(fake_adapter_log.read_text(encoding="utf-8"))
    # Compare resolved paths: the coordinator resolves --repo, and on Windows that expands an 8.3
    # short name (a runner's TEMP under RUNNER~1), so the raw strings differ for the same file.
    if (
        Path(adapter_args[adapter_args.index("--brief") + 1]).resolve()
        != brief_file.resolve()
    ):
        sys.exit("coordinator did not pass the approved immutable brief to the adapter")
    if brief_file.read_bytes() != brief_bytes:
        sys.exit("coordinator changed the immutable dispatch brief")
    if (
        coordinator_run(
            "dispatch",
            "send",
            "--dispatch",
            dispatch_id,
            "--adapter",
            str(fake_adapter),
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator reused a dispatched identifier instead of requiring a new approval"
        )

    candidate_file = orchestration_project / "services" / "retry.py"
    candidate_file.parent.mkdir(parents=True, exist_ok=True)
    candidate_file.write_text(
        "def retry():\n    return True\n\n# queue consumer changed; transaction boundary changed\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "services/retry.py"], cwd=orchestration_project, check=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "feat: add retry path"],
        cwd=orchestration_project,
        check=True,
    )
    candidate_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=orchestration_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    report_file = staged_payload("developer-report.json")
    report_payload = {
        "dispatch_id": dispatch_id,
        "ticket": "#901",
        "role": "developer",
        "outcome": "completed",
        "output": "implemented the requested backend change",
        "commit_sha": candidate_sha,
        "changed_files": ["services/retry.py"],
        "commit_map": commit_map_for(dispatch_record["brief"], candidate_sha),
        "checks_run": [
            {
                "command": "python developer_check.py",
                "result": "pass",
                "evidence": "1 passed",
            }
        ],
        "risks": "none",
        "blockers": "none",
        "next_coordinator_action": "accept",
        "report_language": "ru",
        "risk_triggers": ["authorization-security"],
    }
    report_file.write_text(
        json.dumps(report_payload, indent=2) + "\n", encoding="utf-8"
    )
    if (
        coordinator_run(
            "dispatch",
            "self-report",
            "--dispatch",
            dispatch_id,
            "--model",
            "project-developer-model",
        ).returncode
        != 0
    ):
        sys.exit("coordinator rejected the developer model self-report")
    reported = coordinator_run("report", "submit", "--file", str(report_file))
    if reported.returncode != 0:
        sys.exit("coordinator rejected a valid completion report: " + reported.stderr)
    report_result = json.loads(reported.stdout)
    if report_result["state"] != "reported":
        sys.exit("coordinator did not leave the dispatch at reported")
    stored_report = lifecycle_records(state_root) / "reports" / f"{dispatch_id}.json"
    stored_markdown = lifecycle_records(state_root) / "reports" / f"{dispatch_id}.md"
    if not stored_report.is_file() or not stored_markdown.is_file():
        sys.exit("coordinator did not store canonical JSON and Markdown report")
    if json.loads(stored_report.read_text(encoding="utf-8")) != report_payload:
        sys.exit("coordinator changed the canonical completion report")

    invalid_report = staged_payload("invalid-report.json")
    invalid_payload = dict(report_payload, role="qa")
    invalid_report.write_text(json.dumps(invalid_payload), encoding="utf-8")
    if (
        coordinator_run("report", "submit", "--file", str(invalid_report)).returncode
        == 0
    ):
        sys.exit("coordinator accepted a role-incompatible report")
    if (
        stored_report.read_text(encoding="utf-8")
        != json.dumps(report_payload, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    ):
        sys.exit("coordinator changed a report after rejecting an incompatible report")

    secret_report = staged_payload("secret-report.json")
    secret_report.write_text(
        json.dumps(dict(report_payload, api_token="must-not-pass")), encoding="utf-8"
    )
    if (
        coordinator_run("report", "submit", "--file", str(secret_report)).returncode
        == 0
    ):
        sys.exit("coordinator accepted a secret-shaped report")

    overwritten_report = staged_payload("overwritten-report.json")
    overwritten_report.write_text(json.dumps(report_payload), encoding="utf-8")
    if (
        coordinator_run(
            "report", "submit", "--file", str(overwritten_report)
        ).returncode
        == 0
    ):
        sys.exit("coordinator overwrote an immutable report")

    decision = coordinator_run(
        "batch",
        "decide",
        "--batch",
        batch_id,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:02:00Z",
    )
    if (
        decision.returncode != 0
        or json.loads(decision.stdout)["state"] != "awaiting-approval"
    ):
        sys.exit("coordinator did not record the explicit post-report decision")

    risk = coordinator_run(
        "risk",
        "assess",
        "--batch",
        batch_id,
        "--candidate-commit",
        candidate_sha,
        "--changed-file",
        "services/retry.py",
    )
    if risk.returncode != 0:
        sys.exit("coordinator rejected a valid risk assessment: " + risk.stderr)
    risk_result = json.loads(risk.stdout)
    if not risk_result["review_required"]:
        sys.exit("coordinator skipped review for a manifest trigger")
    if set(risk_result["matched_triggers"]) != {
        "authorization-security",
        "concurrency-retry",
        "queues",
        "retry-dlq",
        "transactions",
    }:
        sys.exit(
            "risk assessment did not record every matching manifest trigger: "
            + json.dumps(risk_result)
        )

    review_dispatch = coordinator_run(
        "dispatch",
        "create",
        "--batch",
        batch_id,
        "--role",
        "code-review",
        "--candidate-commit",
        candidate_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:03:00Z",
    )
    if review_dispatch.returncode != 0:
        sys.exit(
            "coordinator rejected a risk-gated review dispatch: "
            + review_dispatch.stderr
        )
    review_record = json.loads(review_dispatch.stdout)
    review_id = review_record["dispatch_id"]
    if review_record["brief"]["candidate_commit"] != candidate_sha:
        sys.exit("review brief was not pinned to the candidate commit")
    if review_record["brief"]["review_scope"] != ["services/retry.py"]:
        sys.exit("review brief did not preserve the immutable review scope")

    mismatched_checkout = test_root / "mismatched-checkout"
    subprocess.run(
        ["git", "clone", "-q", str(orchestration_project), str(mismatched_checkout)],
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "HEAD^"], cwd=mismatched_checkout, check=True
    )
    if (
        coordinator_run(
            "dispatch",
            "send",
            "--dispatch",
            review_id,
            "--adapter",
            str(fake_adapter),
            "--checkout",
            str(mismatched_checkout),
        ).returncode
        == 0
    ):
        sys.exit("coordinator accepted a review checkout at a mismatched commit")
    if fake_adapter_log.exists():
        fake_adapter_log.unlink()
    subprocess.run(
        ["git", "checkout", "-q", candidate_sha], cwd=mismatched_checkout, check=True
    )

    mutable_file = orchestration_project / "services" / "unreviewed.tmp"
    mutable_file.write_text("outside the pinned commit\n", encoding="utf-8")
    if (
        coordinator_run(
            "dispatch",
            "send",
            "--dispatch",
            review_id,
            "--adapter",
            str(fake_adapter),
            "--checkout",
            str(orchestration_project),
        ).returncode
        == 0
    ):
        sys.exit("coordinator accepted a review checkout with mutable files")
    mutable_file.unlink()

    review_sent = coordinator_run(
        "dispatch",
        "send",
        "--dispatch",
        review_id,
        "--adapter",
        str(fake_adapter),
        "--checkout",
        str(orchestration_project),
    )
    if review_sent.returncode != 0:
        sys.exit(
            "coordinator rejected a correctly pinned review checkout: "
            + review_sent.stderr
        )
    runtime_review = adapter_run(
        lifecycle_records(state_root) / "dispatches" / f"{review_id}.json",
        repo=mismatched_checkout,
    )
    if runtime_review.returncode != 0:
        sys.exit(
            "Orca adapter rejected the coordinator review brief: "
            + runtime_review.stderr
        )
    runtime_calls = [
        json.loads(line)
        for line in fake_orca_log.read_text(encoding="utf-8").splitlines()
    ]
    runtime_worker_calls = [call for call in runtime_calls if call[1] == "worker-start"]
    if (
        not runtime_worker_calls
        or runtime_worker_calls[-1][runtime_worker_calls[-1].index("--base-branch") + 1]
        != candidate_sha
    ):
        sys.exit("Orca adapter did not pin the runtime worker to candidate_commit")

    review_report_file = staged_payload("review-report.json")
    review_payload = {
        "dispatch_id": review_id,
        "ticket": "#901",
        "role": "code-review",
        "outcome": "completed",
        "output": "independent composite review completed",
        "commit_sha": "not applicable — read-only role",
        "changed_files": [],
        "checks_run": [
            {
                "command": f"{sys.executable} qa_baseline.py",
                "result": "pass",
                "evidence": "review scope inspected",
            }
        ],
        "risks": "standards warning requires coordinator decision",
        "blockers": "none",
        "next_coordinator_action": "override-warning or retry",
        "report_language": "ru",
        "review": {
            "candidate_commit": candidate_sha,
            "scope": ["services/retry.py"],
            "standards": {
                "severity": "warning",
                "findings": [
                    {
                        "severity": "warning",
                        "evidence": "style",
                        "summary": "style warning",
                    }
                ],
                "risks": "style risk",
                "blockers": "none",
            },
            "spec": {
                "severity": "clean",
                "findings": [],
                "risks": "none",
                "blockers": "none",
            },
        },
    }
    review_report_file.write_text(
        json.dumps(review_payload, indent=2) + "\n", encoding="utf-8"
    )
    if (
        coordinator_run(
            "dispatch",
            "self-report",
            "--dispatch",
            review_id,
            "--model",
            "project-code-review-model",
        ).returncode
        != 0
    ):
        sys.exit("coordinator rejected the code-review model self-report")
    review_report_result = coordinator_run(
        "report", "submit", "--file", str(review_report_file)
    )
    if review_report_result.returncode != 0:
        sys.exit(
            "coordinator rejected a valid composite review report: "
            + review_report_result.stderr
        )
    review_markdown = (
        lifecycle_records(state_root) / "reports" / f"{review_id}.md"
    ).read_text(encoding="utf-8")
    if (
        "Review Standards severity: warning" not in review_markdown
        or "Review Spec severity: clean" not in review_markdown
    ):
        sys.exit("Markdown review projection did not preserve both independent axes")
    if (
        coordinator_run(
            "batch",
            "decide",
            "--batch",
            batch_id,
            "--decision",
            "accept",
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-09T12:04:00Z",
        ).returncode
        == 0
    ):
        sys.exit("coordinator accepted a review warning without an explicit override")
    override = coordinator_run(
        "batch",
        "decide",
        "--batch",
        batch_id,
        "--decision",
        "override-warning",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:05:00Z",
        "--note",
        "accepted the recorded Standards warning",
    )
    if override.returncode != 0:
        sys.exit(
            "coordinator rejected an explicit warning override: " + override.stderr
        )

    qa_dispatch = coordinator_run(
        "dispatch",
        "create",
        "--batch",
        batch_id,
        "--role",
        "qa",
        "--candidate-commit",
        candidate_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:05:30Z",
    )
    if qa_dispatch.returncode != 0:
        sys.exit(
            "coordinator rejected a QA dispatch after accepted review: "
            + qa_dispatch.stderr
        )
    qa_id = json.loads(qa_dispatch.stdout)["dispatch_id"]
    qa_evidence = (
        "qa",
        "evidence",
        "--ticket",
        "#901",
        "--branch",
        "feature/issue-901-coordinator",
        "--candidate-commit",
        candidate_sha,
    )
    if coordinator_run(*qa_evidence).returncode == 0:
        sys.exit("QA evidence validation accepted a candidate without a QA report")
    queue_root = lifecycle_records(state_root) / "qa-lane" / "queue"
    queue_root.mkdir(parents=True, exist_ok=True)
    prior_queue = (
        queue_root
        / "00000000000000000001-dispatch-00000000-0000-0000-0000-000000000000.json"
    )
    prior_queue.write_text(
        json.dumps(
            {
                "dispatch_id": "dispatch-00000000-0000-0000-0000-000000000000",
                "sequence": 1,
                "queued_at": "2026-09-09T12:05:20+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (lifecycle_records(state_root) / "qa-lane" / "sequence.json").write_text(
        '{"next": 2}\n', encoding="utf-8"
    )
    queued = coordinator_run("qa", "run", "--dispatch", qa_id)
    if queued.returncode != 0 or json.loads(queued.stdout).get("state") != "queued":
        sys.exit("QA runner did not leave a later request visibly queued")
    lane_status = coordinator_run("qa", "status")
    if lane_status.returncode != 0 or len(json.loads(lane_status.stdout)["queue"]) != 2:
        sys.exit("QA lane status did not expose FIFO queue entries")
    lease_path = lifecycle_records(state_root) / "qa-lane" / "lease.json"
    lease_path.write_text(
        json.dumps(
            {
                "dispatch_id": "dispatch-00000000-0000-0000-0000-000000000000",
                "host": "stale-host",
                "pid": 99999,
                "acquired_at": "2026-09-09T12:00:00+00:00",
                "expires_at": "2026-09-09T12:01:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if coordinator_run("qa", "run", "--dispatch", qa_id).returncode == 0:
        sys.exit("QA runner automatically force-unlocked a stale lease")
    recovered = coordinator_run(
        "qa",
        "clear-stale-lease",
        "--expected-host",
        "stale-host",
        "--expected-pid",
        "99999",
        "--expected-expiry",
        "2026-09-09T12:01:00+00:00",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:05:40Z",
        "--reason",
        "owner validated as stale",
    )
    if (
        recovered.returncode != 0
        or json.loads(recovered.stdout).get("state") != "cleared"
    ):
        sys.exit("coordinator did not require and record stale QA lease recovery")
    qa_run = coordinator_run("qa", "run", "--dispatch", qa_id, "--lease-seconds", "60")
    if qa_run.returncode != 0:
        sys.exit("clean-room QA runner rejected a pinned candidate: " + qa_run.stderr)
    qa_result = json.loads(qa_run.stdout)
    if qa_result["state"] != "reported":
        sys.exit("clean-room QA runner did not produce a canonical report")
    qa_report = json.loads(Path(qa_result["report"]).read_text(encoding="utf-8"))
    if qa_report["checks_run"][0]["evidence"].startswith("exit ") is False:
        sys.exit("QA report did not record the gate exit code")
    if qa_report["outcome"] != "completed":
        sys.exit("clean-room QA gate unexpectedly failed: " + json.dumps(qa_report))
    artifact = Path(qa_result["artifact"])
    if (
        not artifact.is_file()
        or hashlib.sha256(artifact.read_bytes()).hexdigest() != qa_result["sha256"]
    ):
        sys.exit("QA evidence artifact is not immutable and checksum-addressed")
    artifact_text = artifact.read_text(encoding="utf-8")
    if not artifact_text.startswith(
        f"$ {sys.executable} qa_baseline.py\nexit_code=0\n"
    ):
        sys.exit("QA evidence artifact does not contain the full gate output")
    if "token=visible" in artifact_text or "token=<redacted>" not in artifact_text:
        sys.exit("QA evidence artifact was not sanitised")
    if coordinator_run(*qa_evidence).returncode == 0:
        sys.exit("QA evidence validation accepted an unaccepted QA report")
    if (
        coordinator_run(
            "dispatch", "send", "--dispatch", qa_id, "--adapter", str(fake_adapter)
        ).returncode
        == 0
    ):
        sys.exit("QA runner allowed a second execution of the same dispatch")
    accepted_qa = coordinator_run(
        "batch",
        "decide",
        "--batch",
        batch_id,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:05:45Z",
    )
    if accepted_qa.returncode != 0:
        sys.exit("coordinator did not accept a successful QA report")
    if json.loads(accepted_qa.stdout).get("next_action") != "publish":
        sys.exit("accepted green QA did not prepare a publish-only next action")
    accepted_evidence = coordinator_run(*qa_evidence)
    if accepted_evidence.returncode != 0:
        sys.exit(
            "QA evidence validation rejected accepted evidence: "
            + accepted_evidence.stderr
        )
    if json.loads(accepted_evidence.stdout).get("candidate_commit") != candidate_sha:
        sys.exit("QA evidence validation did not return the accepted candidate SHA")
    # Historical abandoned plans can share the ticket and branch after a coordinator correction.
    # They carry no QA report for this candidate and must not make PR evidence ambiguous.
    sync_origin_base()
    for history_index in range(2):
        historical = coordinator_run(
            "batch",
            "create",
            "--ticket",
            "#901",
            "--branch",
            "feature/issue-901-coordinator",
            "--worktree",
            str(shared_worktree),
            "--zone",
            "backend",
            "--definition-of-done",
            "superseded planning attempt",
            "--prohibited-change",
            "do not merge",
        )
        if historical.returncode != 0:
            sys.exit(
                "clean-room fixture could not create a historical batch: "
                + historical.stderr
            )
        historical_id = json.loads(historical.stdout)["batch_id"]
        abandoned_history = coordinator_run(
            "batch",
            "abandon",
            "--batch",
            historical_id,
            "--approved-by",
            "project coordinator",
            "--approved-at",
            f"2026-09-09T12:05:{46 + history_index:02d}Z",
            "--reason",
            "superseded before any dispatch",
        )
        if abandoned_history.returncode != 0:
            sys.exit(
                "clean-room fixture could not abandon historical batch: "
                + abandoned_history.stderr
            )
    if coordinator_run(*qa_evidence).returncode != 0:
        sys.exit("QA evidence became ambiguous because of abandoned historical batches")
    state_before_evidence = {
        path.relative_to(state_root).as_posix(): path.read_bytes()
        for path in state_root.rglob("*")
        if path.is_file()
    }
    if coordinator_run(*qa_evidence).returncode != 0:
        sys.exit(
            "QA evidence validation failed when repeated for an accepted candidate"
        )
    state_after_evidence = {
        path.relative_to(state_root).as_posix(): path.read_bytes()
        for path in state_root.rglob("*")
        if path.is_file()
    }
    if state_after_evidence != state_before_evidence:
        sys.exit("QA evidence validation reran or mutated the quality gate")
    if (
        coordinator_run(
            "--state-dir", str(test_root / "forged-qa-state"), *qa_evidence
        ).returncode
        == 0
    ):
        sys.exit("QA evidence validation accepted a non-repository state directory")
    baseline_sha = subprocess.run(
        ["git", "rev-parse", "HEAD^"],
        cwd=orchestration_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if (
        coordinator_run(
            "qa",
            "evidence",
            "--ticket",
            "#901",
            "--branch",
            "feature/issue-901-coordinator",
            "--candidate-commit",
            baseline_sha,
        ).returncode
        == 0
    ):
        sys.exit(
            "QA evidence validation accepted evidence for a mismatched candidate SHA"
        )
    if (
        coordinator_run(
            "dispatch",
            "create",
            "--batch",
            batch_id,
            "--role",
            "code-review",
            "--candidate-commit",
            candidate_sha,
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-09T12:06:00Z",
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator allowed a role other than the prepared publish dispatch after green QA"
        )
    publish_dispatch = coordinator_run(
        "dispatch",
        "create",
        "--batch",
        batch_id,
        "--role",
        "developer",
        "--purpose",
        "publish",
        "--candidate-commit",
        candidate_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T12:06:00Z",
    )
    if (
        publish_dispatch.returncode != 0
        or json.loads(publish_dispatch.stdout)["brief"]["purpose"] != "publish"
    ):
        sys.exit(
            "coordinator did not prepare a publish-only brief for the accepted QA candidate"
        )
    if (
        coordinator_run(
            "dispatch",
            "send",
            "--dispatch",
            json.loads(publish_dispatch.stdout)["dispatch_id"],
            "--adapter",
            str(fake_adapter),
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator allowed a publish-only brief through an unverified runtime adapter"
        )
    published = coordinator_run(
        "dispatch",
        "publish",
        "--dispatch",
        json.loads(publish_dispatch.stdout)["dispatch_id"],
    )
    if (
        published.returncode != 0
        or json.loads(published.stdout).get("candidate_commit") != candidate_sha
    ):
        sys.exit(
            "coordinator did not publish the accepted QA candidate: " + published.stderr
        )
    remote_candidate = capture(
        [
            "git",
            "ls-remote",
            "--heads",
            str(orchestration_remote),
            "refs/heads/feature/issue-901-coordinator",
        ]
    ).split()[0]
    if remote_candidate != candidate_sha:
        sys.exit("coordinator did not publish the exact accepted QA candidate SHA")
    if coordinator_run(*qa_evidence).returncode != 0:
        sys.exit(
            "manual PR evidence validation rejected the published accepted candidate"
        )

    qa_help = coordinator_run("qa", "--help")
    if qa_help.returncode != 0 or "run" not in qa_help.stdout:
        sys.exit("coordinator does not expose the clean-room QA runner")

    print("risk-aware composite review verification passed")
    ctx.qa_id = qa_id
    ctx.shared_worktree = shared_worktree
