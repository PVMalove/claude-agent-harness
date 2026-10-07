"""Gate базового коммита: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from types import SimpleNamespace

from scripts.clean_room.support import (
    capture,
    commit_map_for,
)


def run(ctx: SimpleNamespace) -> None:
    """Gate базового коммита.

    Читает из контекста: `coordinator_run`, `fake_adapter`, `lifecycle_records`,
    `orchestration_project`, `orchestration_remote`, `shared_worktree`, `staged_payload`,
    `sync_origin_base`, `test_root`.
    """
    coordinator_run = ctx.coordinator_run
    fake_adapter = ctx.fake_adapter
    lifecycle_records = ctx.lifecycle_records
    orchestration_project = ctx.orchestration_project
    orchestration_remote = ctx.orchestration_remote
    shared_worktree = ctx.shared_worktree
    staged_payload = ctx.staged_payload
    sync_origin_base = ctx.sync_origin_base
    test_root = ctx.test_root
    # Base-commit gate: `batch create` pins base_commit to a freshly fetched integration ref tip
    # (never a raw local HEAD), and a mandatory freshness re-check blocks code-review/publish
    # dispatch creation once that ref has moved — cleared only by a new developer (rebase)
    # dispatch, whose resulting commit is a new candidate that must be risk-assessed again.
    stale_state = test_root / "stale-base-state"
    sync_origin_base()
    subprocess.run(
        ["git", "branch", "feature/issue-134-stale-base"],
        cwd=orchestration_project,
        check=True,
    )
    pre_batch_head = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()
    stale_batch = json.loads(
        coordinator_run(
            "--state-dir",
            str(stale_state),
            "batch",
            "create",
            "--ticket",
            "#134",
            "--branch",
            "feature/issue-134-stale-base",
            "--worktree",
            str(shared_worktree),
            "--zone",
            "backend",
            "--definition-of-done",
            "protect the base commit",
            "--prohibited-change",
            "do not merge",
        ).stdout
    )["batch_id"]
    stale_batch_record = json.loads(
        (lifecycle_records(stale_state) / "batches" / f"{stale_batch}.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        stale_batch_record["base_commit"] != pre_batch_head
        or stale_batch_record["integration_base_commit"] != pre_batch_head
    ):
        sys.exit(
            "batch create did not pin base_commit to the fetched integration ref tip"
        )
    if stale_batch_record["integration_ref"] is not None:
        sys.exit("batch create recorded an integration_ref without one being requested")
    if stale_batch_record["branch_start_commit"] != pre_batch_head:
        sys.exit("batch create did not record branch_start_commit as the local HEAD")
    coordinator_run(
        "--state-dir",
        str(stale_state),
        "batch",
        "approve",
        "--batch",
        stale_batch,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T18:00:00Z",
    )

    def stale_role(role_name, model, approved_at, payload_extra):
        """Создать и отправить диспатч роли для проверки устаревшего базового коммита."""
        created = coordinator_run(
            "--state-dir",
            str(stale_state),
            "dispatch",
            "create",
            "--batch",
            stale_batch,
            "--role",
            role_name,
            "--approved-by",
            "project coordinator",
            "--approved-at",
            approved_at,
        )
        if created.returncode != 0:
            sys.exit(
                f"coordinator rejected the stale-base {role_name} dispatch: "
                + created.stderr
            )
        role_record = json.loads(created.stdout)
        role_dispatch = role_record["dispatch_id"]
        coordinator_run(
            "--state-dir",
            str(stale_state),
            "dispatch",
            "send",
            "--dispatch",
            role_dispatch,
            "--adapter",
            str(fake_adapter),
        )
        coordinator_run(
            "--state-dir",
            str(stale_state),
            "dispatch",
            "self-report",
            "--dispatch",
            role_dispatch,
            "--model",
            model,
        )
        payload = {
            "dispatch_id": role_dispatch,
            "ticket": "#134",
            "role": role_name,
            "outcome": "completed",
            "output": f"{role_name} finished the requested step",
            "commit_sha": "not applicable — read-only role",
            "changed_files": [],
            "checks_run": []
            if role_name == "architect"
            else [
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
        payload_file = staged_payload(f"stale-base-{role_name}-report.json")
        payload_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        submitted = coordinator_run(
            "--state-dir",
            str(stale_state),
            "report",
            "submit",
            "--file",
            str(payload_file),
        )
        if submitted.returncode != 0:
            sys.exit(
                f"coordinator rejected the stale-base {role_name} report: "
                + submitted.stderr
            )
        return role_dispatch

    stale_role("architect", "project-architect-model", "2026-09-09T18:00:10Z", {})
    coordinator_run(
        "--state-dir",
        str(stale_state),
        "batch",
        "decide",
        "--batch",
        stale_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T18:00:20Z",
    )
    stale_feature_file = orchestration_project / "services" / "stale_base.py"
    stale_feature_file.parent.mkdir(parents=True, exist_ok=True)
    stale_feature_file.write_text(
        'def marker():\n    return "first"\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "services/stale_base.py"], cwd=orchestration_project, check=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "feat: add stale-base marker"],
        cwd=orchestration_project,
        check=True,
    )
    stale_sha = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()
    stale_role(
        "developer",
        "project-developer-model",
        "2026-09-09T18:00:30Z",
        {
            "commit_sha": stale_sha,
            "changed_files": ["services/stale_base.py"],
        },
    )
    coordinator_run(
        "--state-dir",
        str(stale_state),
        "batch",
        "decide",
        "--batch",
        stale_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T18:00:40Z",
    )
    stale_assessment = coordinator_run(
        "--state-dir",
        str(stale_state),
        "risk",
        "assess",
        "--batch",
        stale_batch,
        "--candidate-commit",
        stale_sha,
        "--changed-file",
        "services/stale_base.py",
    )
    if stale_assessment.returncode != 0:
        sys.exit(
            "coordinator rejected the stale-base risk assessment: "
            + stale_assessment.stderr
        )

    # Simulate someone else's PR landing on the integration ref after this batch's base was
    # pinned: push an unrelated commit straight to origin main from a separate clone, without
    # touching this checkout's own history.
    drift_worktree = test_root / "stale-base-drift"
    # The bare remote's own HEAD symref points at a branch ("master") nothing has ever pushed, so
    # an unqualified clone can't check anything out - name "main" explicitly.
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--branch",
            "main",
            str(orchestration_remote),
            str(drift_worktree),
        ],
        check=True,
    )
    (drift_worktree / "UPSTREAM.md").write_text(
        "someone else's merge\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "UPSTREAM.md"], cwd=drift_worktree, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=upstream@example.com",
            "-c",
            "user.name=Upstream",
            "commit",
            "-qm",
            "chore: unrelated upstream change",
        ],
        cwd=drift_worktree,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "HEAD:main"], cwd=drift_worktree, check=True
    )
    drifted_sha = capture(
        ["git", "-C", str(drift_worktree), "rev-parse", "HEAD"]
    ).strip()

    # The base-freshness gate was removed: upstream drift neither blocks code-review of the
    # pinned candidate nor forces a developer rebase.
    review = coordinator_run(
        "--state-dir",
        str(stale_state),
        "dispatch",
        "create",
        "--batch",
        stale_batch,
        "--role",
        "code-review",
        "--candidate-commit",
        stale_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T18:00:50Z",
    )
    if review.returncode != 0:
        sys.exit(
            "coordinator refused code-review of the pinned candidate after upstream drift: "
            + review.stderr
        )
    drifted_batch_record = json.loads(
        (lifecycle_records(stale_state) / "batches" / f"{stale_batch}.json").read_text(
            encoding="utf-8"
        )
    )
    if drifted_batch_record.get("required_next_role") == "developer":
        sys.exit("upstream drift forced a developer dispatch as the next role")
    if drifted_batch_record.get("base_rebase_required"):
        sys.exit("upstream drift set base_rebase_required")
    if "rebase_target_commit" in drifted_batch_record:
        sys.exit("upstream drift recorded a rebase_target_commit")
    if drifted_batch_record["integration_base_commit"] == drifted_sha:
        sys.exit("the coordinator repinned the base on upstream drift")

    print("base-commit gate verification passed")
