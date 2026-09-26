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

    blocked_review = coordinator_run(
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
    if blocked_review.returncode == 0:
        sys.exit(
            "coordinator created a code-review dispatch against a stale integration base"
        )
    if "stale" not in blocked_review.stderr:
        sys.exit(
            "coordinator did not explain the stale-base block: " + blocked_review.stderr
        )
    blocked_batch_record = json.loads(
        (lifecycle_records(stale_state) / "batches" / f"{stale_batch}.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        blocked_batch_record["next_action"] != "developer"
        or blocked_batch_record.get("required_next_role") != "developer"
    ):
        sys.exit(
            "a stale base did not force a new developer dispatch as the only next action"
        )
    if blocked_batch_record["integration_base_commit"] == drifted_sha:
        sys.exit(
            "the coordinator repinned the base directly instead of requiring a developer rebase"
        )
    if blocked_batch_record.get("rebase_target_commit") != drifted_sha:
        sys.exit("a stale-base block did not record the integration tip the rebase must land on")

    # The block is cleared only by a developer dispatch — never a coordinator/human git operation.
    still_blocked_publish = coordinator_run(
        "--state-dir",
        str(stale_state),
        "dispatch",
        "create",
        "--batch",
        stale_batch,
        "--role",
        "developer",
        "--purpose",
        "publish",
        "--candidate-commit",
        stale_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T18:00:55Z",
    )
    if still_blocked_publish.returncode == 0:
        sys.exit(
            "coordinator allowed a publish dispatch while a developer rebase was required"
        )

    rebase_created = coordinator_run(
        "--state-dir",
        str(stale_state),
        "dispatch",
        "create",
        "--batch",
        stale_batch,
        "--role",
        "developer",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T18:01:00Z",
    )
    if rebase_created.returncode != 0:
        sys.exit(
            "coordinator refused the developer rebase dispatch after a stale-base block: "
            + rebase_created.stderr
        )
    rebase_record = json.loads(rebase_created.stdout)
    rebase_dispatch = rebase_record["dispatch_id"]
    coordinator_run(
        "--state-dir",
        str(stale_state),
        "dispatch",
        "send",
        "--dispatch",
        rebase_dispatch,
        "--adapter",
        str(fake_adapter),
    )
    coordinator_run(
        "--state-dir",
        str(stale_state),
        "dispatch",
        "self-report",
        "--dispatch",
        rebase_dispatch,
        "--model",
        "project-developer-model",
    )
    # The "Candidate commit" rule already in force: resubmitting the same, already-assessed
    # candidate as the rebase's result must not be accepted as resolving the staleness.
    same_sha_payload = {
        "dispatch_id": rebase_dispatch,
        "ticket": "#134",
        "role": "developer",
        "outcome": "completed",
        "output": "attempted rebase without producing a new commit",
        "commit_sha": stale_sha,
        "changed_files": ["services/stale_base.py"],
        "commit_map": commit_map_for(rebase_record["brief"], stale_sha),
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
    }
    same_sha_file = staged_payload("stale-base-rebase-same-sha-report.json")
    same_sha_file.write_text(
        json.dumps(same_sha_payload, indent=2) + "\n", encoding="utf-8"
    )
    same_sha_submit = coordinator_run(
        "--state-dir",
        str(stale_state),
        "report",
        "submit",
        "--file",
        str(same_sha_file),
    )
    if same_sha_submit.returncode == 0:
        sys.exit(
            "coordinator accepted a rebase report that reused the already-assessed stale candidate"
        )

    # A new commit that does not contain the moved integration tip is not a rebase.
    stale_feature_file.write_text(
        'def marker():\n    return "not rebased"\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "services/stale_base.py"], cwd=orchestration_project, check=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "feat: new commit without the moved integration ref"],
        cwd=orchestration_project,
        check=True,
    )
    unrebased_sha = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()
    unrebased_file = staged_payload("stale-base-unrebased-report.json")
    unrebased_file.write_text(
        json.dumps(
            {
                **same_sha_payload,
                "commit_sha": unrebased_sha,
                "commit_map": commit_map_for(rebase_record["brief"], unrebased_sha),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    unrebased_submit = coordinator_run(
        "--state-dir", str(stale_state), "report", "submit", "--file", str(unrebased_file)
    )
    if unrebased_submit.returncode == 0 or "integration tip" not in unrebased_submit.stderr:
        sys.exit(
            "coordinator accepted a rebase report whose candidate lacks the moved integration tip: "
            + unrebased_submit.stderr
        )
    subprocess.run(
        ["git", "reset", "-q", "--hard", stale_sha], cwd=orchestration_project, check=True
    )

    # A real rebase: the candidate now contains the upstream commit, which must not be counted as
    # this ticket's commit or changed file.
    subprocess.run(
        ["git", "fetch", "-q", "origin", "main"], cwd=orchestration_project, check=True
    )
    subprocess.run(
        ["git", "rebase", "-q", "FETCH_HEAD"], cwd=orchestration_project, check=True
    )
    rebased_sha = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()
    rebase_payload = {
        "dispatch_id": rebase_dispatch,
        "ticket": "#134",
        "role": "developer",
        "outcome": "completed",
        "output": "rebased onto the current integration tip",
        "commit_sha": rebased_sha,
        "changed_files": ["services/stale_base.py"],
        "commit_map": commit_map_for(rebase_record["brief"], rebased_sha),
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
    }
    rebase_file = staged_payload("stale-base-rebase-report.json")
    rebase_file.write_text(
        json.dumps(rebase_payload, indent=2) + "\n", encoding="utf-8"
    )
    rebase_submit = coordinator_run(
        "--state-dir", str(stale_state), "report", "submit", "--file", str(rebase_file)
    )
    if rebase_submit.returncode != 0:
        sys.exit(
            "coordinator rejected the rebase developer report: " + rebase_submit.stderr
        )
    rebase_accept = coordinator_run(
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
        "2026-09-09T18:01:10Z",
    )
    if rebase_accept.returncode != 0:
        sys.exit(
            "coordinator rejected acceptance of the rebase developer report: "
            + rebase_accept.stderr
        )
    if json.loads(rebase_accept.stdout).get("next_action") != "risk-assessment":
        sys.exit(
            "accepting the rebase developer report did not require a fresh risk assessment"
        )
    refreshed_batch_record = json.loads(
        (lifecycle_records(stale_state) / "batches" / f"{stale_batch}.json").read_text(
            encoding="utf-8"
        )
    )
    if refreshed_batch_record["integration_base_commit"] != drifted_sha:
        sys.exit(
            "accepting the rebase did not repin integration_base_commit to the moved integration tip"
        )
    if refreshed_batch_record.get("base_rebase_required") or (
        "rebase_target_commit" in refreshed_batch_record
    ):
        sys.exit("accepting the rebase did not clear the stale-base rebase state")
    if refreshed_batch_record["base_commit"] != stale_batch_record["base_commit"]:
        sys.exit("a rebase mutated the batch's immutable base_commit")

    # The rebase's commit is a brand-new candidate: code-review must still be refused until it is
    # risk-assessed again, per the existing rule that any new candidate needs its own assessment.
    premature_rebase_review = coordinator_run(
        "--state-dir",
        str(stale_state),
        "dispatch",
        "create",
        "--batch",
        stale_batch,
        "--role",
        "code-review",
        "--candidate-commit",
        rebased_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T18:01:20Z",
    )
    if premature_rebase_review.returncode == 0:
        sys.exit(
            "coordinator allowed code-review before the rebased candidate was risk-assessed"
        )
    rebase_assessment = coordinator_run(
        "--state-dir",
        str(stale_state),
        "risk",
        "assess",
        "--batch",
        stale_batch,
        "--candidate-commit",
        rebased_sha,
        "--changed-file",
        "services/stale_base.py",
    )
    if rebase_assessment.returncode != 0:
        sys.exit(
            "coordinator rejected risk assessment for the rebased candidate: "
            + rebase_assessment.stderr
        )

    # The base is fresh again — review now proceeds like an ordinary, never-stale batch.
    cleared_review = coordinator_run(
        "--state-dir",
        str(stale_state),
        "dispatch",
        "create",
        "--batch",
        stale_batch,
        "--role",
        "code-review",
        "--candidate-commit",
        rebased_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T18:01:30Z",
    )
    if cleared_review.returncode != 0:
        sys.exit(
            "coordinator refused code-review after the stale base was cleared by a rebase: "
            + cleared_review.stderr
        )

    print("base-commit gate verification passed")
