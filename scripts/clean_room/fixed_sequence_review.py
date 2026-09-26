"""Фиксированная последовательность review: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from types import SimpleNamespace

from scripts.clean_room.support import (
    capture,
    commit_map_for,
)


def run(ctx: SimpleNamespace) -> None:
    """Фиксированная последовательность review.

    Читает из контекста: `coordinator_run`, `fake_adapter`, `orchestration_project`,
    `shared_worktree`, `staged_payload`, `sync_origin_base`, `test_root`.
    """
    coordinator_run = ctx.coordinator_run
    fake_adapter = ctx.fake_adapter
    orchestration_project = ctx.orchestration_project
    shared_worktree = ctx.shared_worktree
    staged_payload = ctx.staged_payload
    sync_origin_base = ctx.sync_origin_base
    test_root = ctx.test_root
    # The long pipeline reviews every candidate.  Risk assessment decides when review is *mandatory*,
    # never when it is permitted, so a low-risk candidate can still be sent to code-review.
    low_risk_state = test_root / "low-risk-state"
    subprocess.run(
        ["git", "branch", "feature/issue-904-low-risk"],
        cwd=orchestration_project,
        check=True,
    )
    sync_origin_base()
    low_risk_batch = json.loads(
        coordinator_run(
            "--state-dir",
            str(low_risk_state),
            "batch",
            "create",
            "--ticket",
            "#904",
            "--branch",
            "feature/issue-904-low-risk",
            "--worktree",
            str(shared_worktree),
            "--zone",
            "backend",
            "--definition-of-done",
            "add the greeting helper",
            "--prohibited-change",
            "do not merge",
        ).stdout
    )["batch_id"]
    coordinator_run(
        "--state-dir",
        str(low_risk_state),
        "batch",
        "approve",
        "--batch",
        low_risk_batch,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T15:00:00Z",
    )

    def low_risk_role(role_name, model, approved_at, payload_extra):
        created = coordinator_run(
            "--state-dir",
            str(low_risk_state),
            "dispatch",
            "create",
            "--batch",
            low_risk_batch,
            "--role",
            role_name,
            "--approved-by",
            "project coordinator",
            "--approved-at",
            approved_at,
        )
        if created.returncode != 0:
            sys.exit(
                f"coordinator rejected the low-risk {role_name} dispatch: "
                + created.stderr
            )
        role_record = json.loads(created.stdout)
        role_dispatch = role_record["dispatch_id"]
        coordinator_run(
            "--state-dir",
            str(low_risk_state),
            "dispatch",
            "send",
            "--dispatch",
            role_dispatch,
            "--adapter",
            str(fake_adapter),
        )
        coordinator_run(
            "--state-dir",
            str(low_risk_state),
            "dispatch",
            "self-report",
            "--dispatch",
            role_dispatch,
            "--model",
            model,
        )
        payload = {
            "dispatch_id": role_dispatch,
            "ticket": "#904",
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
        payload_file = staged_payload(f"low-risk-{role_name}-report.json")
        payload_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        submitted = coordinator_run(
            "--state-dir",
            str(low_risk_state),
            "report",
            "submit",
            "--file",
            str(payload_file),
        )
        if submitted.returncode != 0:
            sys.exit(
                f"coordinator rejected the low-risk {role_name} report: "
                + submitted.stderr
            )
        return role_dispatch

    low_risk_role("architect", "project-architect-model", "2026-09-09T15:00:10Z", {})
    coordinator_run(
        "--state-dir",
        str(low_risk_state),
        "batch",
        "decide",
        "--batch",
        low_risk_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T15:00:20Z",
    )
    greeting_file = orchestration_project / "services" / "greeting.py"
    greeting_file.write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    subprocess.run(
        ["git", "add", "services/greeting.py"], cwd=orchestration_project, check=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "feat: add greeting helper"],
        cwd=orchestration_project,
        check=True,
    )
    low_risk_sha = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()
    low_risk_role(
        "developer",
        "project-developer-model",
        "2026-09-09T15:00:30Z",
        {
            "commit_sha": low_risk_sha,
            "changed_files": ["services/greeting.py"],
        },
    )
    coordinator_run(
        "--state-dir",
        str(low_risk_state),
        "batch",
        "decide",
        "--batch",
        low_risk_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T15:00:40Z",
    )
    low_risk_assessment = coordinator_run(
        "--state-dir",
        str(low_risk_state),
        "risk",
        "assess",
        "--batch",
        low_risk_batch,
        "--candidate-commit",
        low_risk_sha,
        "--changed-file",
        "services/greeting.py",
    )
    if low_risk_assessment.returncode != 0:
        sys.exit(
            "coordinator rejected the low-risk assessment: "
            + low_risk_assessment.stderr
        )
    if json.loads(low_risk_assessment.stdout)["review_required"]:
        sys.exit(
            "clean-room fixture for the low-risk path unexpectedly matched a risk trigger"
        )
    optional_review = coordinator_run(
        "--state-dir",
        str(low_risk_state),
        "dispatch",
        "create",
        "--batch",
        low_risk_batch,
        "--role",
        "code-review",
        "--candidate-commit",
        low_risk_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T15:00:50Z",
    )
    if optional_review.returncode != 0:
        sys.exit(
            "coordinator refused code-review for a low-risk candidate: "
            + optional_review.stderr
        )

    print("fixed-sequence review verification passed")
