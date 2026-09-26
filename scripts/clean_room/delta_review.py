"""Delta-review кандидата: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from types import SimpleNamespace

from scripts.clean_room.support import (
    capture,
    commit_map_for,
)


def run(ctx: SimpleNamespace) -> None:
    """Delta-review кандидата.

    Читает из контекста: `coordinator_run`, `fake_adapter`, `lifecycle_records`,
    `orchestration_project`, `shared_worktree`, `staged_payload`, `sync_origin_base`, `test_root`.
    """
    coordinator_run = ctx.coordinator_run
    fake_adapter = ctx.fake_adapter
    lifecycle_records = ctx.lifecycle_records
    orchestration_project = ctx.orchestration_project
    shared_worktree = ctx.shared_worktree
    staged_payload = ctx.staged_payload
    sync_origin_base = ctx.sync_origin_base
    test_root = ctx.test_root
    # A test-only fix closing a review's single Warning finding gets a cheaper follow-up review:
    # the Standards/Spec axis that was Clean is inherited without re-analysis, only the axis that
    # was Warning is re-checked, and the follow-up is always a new independent dispatch.
    delta_state = test_root / "delta-review-state"
    subprocess.run(
        ["git", "branch", "feature/issue-906-delta-review"],
        cwd=orchestration_project,
        check=True,
    )
    sync_origin_base()
    delta_batch_created = json.loads(
        coordinator_run(
            "--state-dir",
            str(delta_state),
            "batch",
            "create",
            "--ticket",
            "#906",
            "--branch",
            "feature/issue-906-delta-review",
            "--worktree",
            str(shared_worktree),
            "--zone",
            "backend",
            "--definition-of-done",
            "add the summary formatter with a passing test",
            "--prohibited-change",
            "do not merge",
        ).stdout
    )
    delta_batch = delta_batch_created["batch_id"]
    delta_batch_created["base_commit"]
    coordinator_run(
        "--state-dir",
        str(delta_state),
        "batch",
        "approve",
        "--batch",
        delta_batch,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T17:00:00Z",
    )

    def delta_role(role_name, model, approved_at, payload_extra):
        created = coordinator_run(
            "--state-dir",
            str(delta_state),
            "dispatch",
            "create",
            "--batch",
            delta_batch,
            "--role",
            role_name,
            "--approved-by",
            "project coordinator",
            "--approved-at",
            approved_at,
        )
        if created.returncode != 0:
            sys.exit(
                f"coordinator rejected the delta-review {role_name} dispatch: "
                + created.stderr
            )
        role_record = json.loads(created.stdout)
        role_dispatch = role_record["dispatch_id"]
        coordinator_run(
            "--state-dir",
            str(delta_state),
            "dispatch",
            "send",
            "--dispatch",
            role_dispatch,
            "--adapter",
            str(fake_adapter),
        )
        coordinator_run(
            "--state-dir",
            str(delta_state),
            "dispatch",
            "self-report",
            "--dispatch",
            role_dispatch,
            "--model",
            model,
        )
        payload = {
            "dispatch_id": role_dispatch,
            "ticket": "#906",
            "role": role_name,
            "outcome": "completed",
            "output": f"{role_name} finished the requested step",
            "commit_sha": "not applicable — read-only role",
            "changed_files": [],
            "checks_run": [] if role_name == "architect" else [
                {
                    "command": "python developer_check.py"
                    if role_name == "developer"
                    else f"{sys.executable} qa_baseline.py",
                    "result": "pass",
                    "evidence": "1 passed",
                }
            ],
            "risks": "none",
            "blockers": "none",
            "next_coordinator_action": "accept",
            "report_language": "ru",
        }
        payload.update(payload_extra)
        if role_record["brief"].get("commit_plan"):
            payload["commit_map"] = commit_map_for(
                role_record["brief"], payload["commit_sha"]
            )
        payload_file = staged_payload(f"delta-{role_name}-{role_dispatch}-report.json")
        payload_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        submitted = coordinator_run(
            "--state-dir",
            str(delta_state),
            "report",
            "submit",
            "--file",
            str(payload_file),
        )
        if submitted.returncode != 0:
            sys.exit(
                f"coordinator rejected the delta-review {role_name} report: "
                + submitted.stderr
            )
        return role_dispatch

    delta_architect_id = delta_role(
        "architect", "project-architect-model", "2026-09-09T17:00:10Z", {}
    )
    coordinator_run(
        "--state-dir",
        str(delta_state),
        "batch",
        "decide",
        "--batch",
        delta_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T17:00:20Z",
    )

    formatter_file = orchestration_project / "services" / "delta_formatter.py"
    formatter_file.write_text(
        'def format_summary():\n    return "summary"\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "services/delta_formatter.py"],
        cwd=orchestration_project,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "feat: add summary formatter helper"],
        cwd=orchestration_project,
        check=True,
    )
    delta_first_sha = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()
    delta_role(
        "developer",
        "project-developer-model",
        "2026-09-09T17:00:30Z",
        {
            "commit_sha": delta_first_sha,
            "changed_files": ["services/delta_formatter.py"],
        },
    )
    coordinator_run(
        "--state-dir",
        str(delta_state),
        "batch",
        "decide",
        "--batch",
        delta_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T17:00:40Z",
    )
    first_delta_risk = coordinator_run(
        "--state-dir",
        str(delta_state),
        "risk",
        "assess",
        "--batch",
        delta_batch,
        "--candidate-commit",
        delta_first_sha,
        "--changed-file",
        "services/delta_formatter.py",
    )
    if first_delta_risk.returncode != 0:
        sys.exit(
            "coordinator rejected the first delta-review-fixture risk assessment: "
            + first_delta_risk.stderr
        )

    first_review_dispatch = coordinator_run(
        "--state-dir",
        str(delta_state),
        "dispatch",
        "create",
        "--batch",
        delta_batch,
        "--role",
        "code-review",
        "--candidate-commit",
        delta_first_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T17:00:50Z",
    )
    if first_review_dispatch.returncode != 0:
        sys.exit(
            "coordinator rejected the first delta-review-fixture code-review dispatch: "
            + first_review_dispatch.stderr
        )
    first_review_id = json.loads(first_review_dispatch.stdout)["dispatch_id"]
    first_review_sent = coordinator_run(
        "--state-dir",
        str(delta_state),
        "dispatch",
        "send",
        "--dispatch",
        first_review_id,
        "--adapter",
        str(fake_adapter),
        "--checkout",
        str(orchestration_project),
    )
    if first_review_sent.returncode != 0:
        sys.exit(
            "coordinator rejected the first delta-review-fixture checkout: "
            + first_review_sent.stderr
        )
    coordinator_run(
        "--state-dir",
        str(delta_state),
        "dispatch",
        "self-report",
        "--dispatch",
        first_review_id,
        "--model",
        "project-code-review-model",
    )
    first_review_payload = {
        "dispatch_id": first_review_id,
        "ticket": "#906",
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
        "risks": "spec warning requires coordinator decision",
        "blockers": "none",
        "next_coordinator_action": "override-warning or retry",
        "report_language": "ru",
        "review": {
            "candidate_commit": delta_first_sha,
            "scope": ["services/delta_formatter.py"],
            "standards": {
                "severity": "clean",
                "findings": [],
                "risks": "none",
                "blockers": "none",
            },
            "spec": {
                "severity": "warning",
                "findings": [
                    {
                        "severity": "warning",
                        "evidence": "missing edge case",
                        "summary": "spec warning",
                    }
                ],
                "risks": "spec risk",
                "blockers": "none",
            },
        },
    }
    first_review_file = staged_payload(f"delta-first-review-{first_review_id}.json")
    first_review_file.write_text(
        json.dumps(first_review_payload, indent=2) + "\n", encoding="utf-8"
    )
    first_review_submit = coordinator_run(
        "--state-dir",
        str(delta_state),
        "report",
        "submit",
        "--file",
        str(first_review_file),
    )
    if first_review_submit.returncode != 0:
        sys.exit(
            "coordinator rejected the first delta-review-fixture composite review report: "
            + first_review_submit.stderr
        )
    if (
        coordinator_run(
            "--state-dir",
            str(delta_state),
            "batch",
            "decide",
            "--batch",
            delta_batch,
            "--decision",
            "retry",
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-09T17:01:00Z",
        ).returncode
        != 0
    ):
        sys.exit(
            "coordinator rejected a retry decision on a delta-review-fixture warning"
        )

    test_file = orchestration_project / "services" / "tests" / "test_delta_formatter.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "def test_format_summary():\n    assert True  # tighten flaky assertion timing\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "services/tests/test_delta_formatter.py"],
        cwd=orchestration_project,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "test: cover summary formatter edge case"],
        cwd=orchestration_project,
        check=True,
    )
    delta_second_sha = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()
    delta_role(
        "developer",
        "project-developer-model",
        "2026-09-09T17:01:10Z",
        {
            "commit_sha": delta_second_sha,
            "changed_files": [
                "services/delta_formatter.py",
                "services/tests/test_delta_formatter.py",
            ],
        },
    )
    coordinator_run(
        "--state-dir",
        str(delta_state),
        "batch",
        "decide",
        "--batch",
        delta_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T17:01:20Z",
    )
    second_delta_risk = coordinator_run(
        "--state-dir",
        str(delta_state),
        "risk",
        "assess",
        "--batch",
        delta_batch,
        "--candidate-commit",
        delta_second_sha,
        "--changed-file",
        "services/delta_formatter.py",
        "--changed-file",
        "services/tests/test_delta_formatter.py",
    )
    if second_delta_risk.returncode != 0:
        sys.exit(
            "coordinator rejected the delta-review fix candidate's risk assessment: "
            + second_delta_risk.stderr
        )

    wrong_role_delta = coordinator_run(
        "--state-dir",
        str(delta_state),
        "dispatch",
        "create",
        "--batch",
        delta_batch,
        "--role",
        "code-review",
        "--candidate-commit",
        delta_second_sha,
        "--delta-review-of",
        delta_architect_id,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T17:01:25Z",
    )
    if wrong_role_delta.returncode == 0:
        sys.exit(
            "coordinator accepted delta-review-of referencing a non-review dispatch"
        )
    if "code-review dispatch" not in wrong_role_delta.stderr:
        sys.exit(
            "coordinator did not explain the delta-review-of role mismatch: "
            + wrong_role_delta.stderr
        )

    second_review_dispatch = coordinator_run(
        "--state-dir",
        str(delta_state),
        "dispatch",
        "create",
        "--batch",
        delta_batch,
        "--role",
        "code-review",
        "--candidate-commit",
        delta_second_sha,
        "--delta-review-of",
        first_review_id,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T17:01:30Z",
    )
    if second_review_dispatch.returncode != 0:
        sys.exit(
            "coordinator rejected an eligible delta-review dispatch: "
            + second_review_dispatch.stderr
        )
    second_review_record = json.loads(second_review_dispatch.stdout)
    second_review_id = second_review_record["dispatch_id"]
    if second_review_id == first_review_id:
        sys.exit(
            "delta-review reused the prior review's dispatch ID instead of a new independent dispatch"
        )
    if second_review_record["brief"]["delta_review_of"] != first_review_id:
        sys.exit("delta-review brief did not reference the prior review as evidence")
    if second_review_record["brief"]["delta_review_axis"] != "spec":
        sys.exit("delta-review brief did not target the Spec axis")

    second_review_sent = coordinator_run(
        "--state-dir",
        str(delta_state),
        "dispatch",
        "send",
        "--dispatch",
        second_review_id,
        "--adapter",
        str(fake_adapter),
        "--checkout",
        str(orchestration_project),
    )
    if second_review_sent.returncode != 0:
        sys.exit(
            "coordinator rejected the delta-review checkout: "
            + second_review_sent.stderr
        )
    coordinator_run(
        "--state-dir",
        str(delta_state),
        "dispatch",
        "self-report",
        "--dispatch",
        second_review_id,
        "--model",
        "project-code-review-model",
    )
    second_review_payload = {
        "dispatch_id": second_review_id,
        "ticket": "#906",
        "role": "code-review",
        "outcome": "completed",
        "output": "delta-review completed",
        "commit_sha": "not applicable — read-only role",
        "changed_files": [],
        "checks_run": [
            {
                "command": f"{sys.executable} qa_baseline.py",
                "result": "pass",
                "evidence": "fix diff inspected",
            }
        ],
        "risks": "none",
        "blockers": "none",
        "next_coordinator_action": "accept",
        "report_language": "ru",
        "review": {
            "candidate_commit": delta_second_sha,
            "scope": [
                "services/delta_formatter.py",
                "services/tests/test_delta_formatter.py",
            ],
            "standards": {
                "severity": "clean",
                "findings": [],
                "risks": "inherited from the prior review",
                "blockers": "none",
                "inherited_from": first_review_id,
            },
            "spec": {
                "severity": "clean",
                "findings": [],
                "risks": "edge case covered",
                "blockers": "none",
            },
        },
    }
    # A delta-review report must not re-derive the inherited axis: prove this on the *live* dispatch
    # before the well-formed report below settles it, or the rejection would happen for the wrong
    # reason (an already-reported dispatch is never live, whatever the content).
    re_derived = json.loads(json.dumps(second_review_payload))
    re_derived["review"]["standards"] = {
        "severity": "warning",
        "findings": [
            {"severity": "warning", "summary": "reflagged", "evidence": "re-checked"}
        ],
        "risks": "re-checked",
        "blockers": "none",
        "inherited_from": first_review_id,
    }
    re_derived_file = staged_payload(
        f"delta-second-review-{second_review_id}-rederived.json"
    )
    re_derived_file.write_text(
        json.dumps(re_derived, indent=2) + "\n", encoding="utf-8"
    )
    re_derived_submit = coordinator_run(
        "--state-dir",
        str(delta_state),
        "report",
        "submit",
        "--file",
        str(re_derived_file),
    )
    if re_derived_submit.returncode == 0:
        sys.exit(
            "coordinator accepted a delta-review report that re-derived the inherited axis"
        )
    if "inherit" not in re_derived_submit.stderr:
        sys.exit(
            "coordinator did not explain the inherited-axis rejection: "
            + re_derived_submit.stderr
        )

    second_review_file = staged_payload(f"delta-second-review-{second_review_id}.json")
    second_review_file.write_text(
        json.dumps(second_review_payload, indent=2) + "\n", encoding="utf-8"
    )
    second_review_submit = coordinator_run(
        "--state-dir",
        str(delta_state),
        "report",
        "submit",
        "--file",
        str(second_review_file),
    )
    if second_review_submit.returncode != 0:
        sys.exit(
            "coordinator rejected a well-formed delta-review report: "
            + second_review_submit.stderr
        )
    second_review_markdown = (
        lifecycle_records(delta_state) / "reports" / f"{second_review_id}.md"
    ).read_text(encoding="utf-8")
    if (
        f"Review Standards inherited from: {first_review_id}"
        not in second_review_markdown
    ):
        sys.exit(
            "delta-review markdown projection did not reference the prior review as evidence"
        )

    delta_decision = coordinator_run(
        "--state-dir",
        str(delta_state),
        "batch",
        "decide",
        "--batch",
        delta_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T17:01:40Z",
    )
    if delta_decision.returncode != 0:
        sys.exit(
            "coordinator rejected acceptance of a resolved delta-review: "
            + delta_decision.stderr
        )
    if json.loads(delta_decision.stdout).get("next_action") != "qa":
        sys.exit("accepted delta-review did not advance the batch to QA")

    print("delta-review verification passed")
