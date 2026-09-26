"""Checkpoint и сессия worker: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from types import SimpleNamespace

from scripts.clean_room.support import (
    capture,
)


def run(ctx: SimpleNamespace) -> None:
    """Checkpoint и сессия worker.

    Читает из контекста: `coordinator_run`, `fake_adapter`, `lifecycle_records`,
    `orchestration_config`, `orchestration_project`, `shared_worktree`, `staged_payload`,
    `sync_origin_base`, `test_root`.
    Передаёт дальше: `checkpoint_dev_id`, `checkpoint_state`.
    """
    coordinator_run = ctx.coordinator_run
    fake_adapter = ctx.fake_adapter
    lifecycle_records = ctx.lifecycle_records
    orchestration_config = ctx.orchestration_config
    orchestration_project = ctx.orchestration_project
    shared_worktree = ctx.shared_worktree
    staged_payload = ctx.staged_payload
    sync_origin_base = ctx.sync_origin_base
    test_root = ctx.test_root
    # Checkpoint and worker session (Issue #139): a write role may end its worker session with a
    # non-terminal checkpoint and resume the same dispatch ID under a fresh worker session; a
    # read-only role may never checkpoint at all, and a checkpoint is never a completion report.
    checkpoint_state = test_root / "checkpoint-state"
    subprocess.run(
        ["git", "branch", "feature/issue-139-checkpoint"],
        cwd=orchestration_project,
        check=True,
    )
    sync_origin_base()
    checkpoint_batch = json.loads(
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "batch",
            "create",
            "--ticket",
            "#139",
            "--branch",
            "feature/issue-139-checkpoint",
            "--worktree",
            str(shared_worktree),
            "--zone",
            "backend",
            "--definition-of-done",
            "implement checkpoint step one",
            "--definition-of-done",
            "implement checkpoint step two",
            "--prohibited-change",
            "do not merge",
        ).stdout
    )["batch_id"]
    coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "batch",
        "approve",
        "--batch",
        checkpoint_batch,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-14T16:00:00Z",
    )

    def checkpoint_report(role_name, dispatch_id, approved_action, extra):
        payload = {
            "dispatch_id": dispatch_id,
            "ticket": "#139",
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
            "next_coordinator_action": approved_action,
            "report_language": "ru",
        }
        payload.update(extra)
        payload_file = staged_payload(f"checkpoint-{role_name}-{dispatch_id}.json")
        payload_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        submitted = coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "report",
            "submit",
            "--file",
            str(payload_file),
        )
        if submitted.returncode != 0:
            sys.exit(
                f"coordinator rejected the checkpoint fixture's {role_name} report: "
                + submitted.stderr
            )

    checkpoint_architect = json.loads(
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "create",
            "--batch",
            checkpoint_batch,
            "--role",
            "architect",
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-14T16:00:10Z",
        ).stdout
    )["dispatch_id"]
    coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "dispatch",
        "send",
        "--dispatch",
        checkpoint_architect,
        "--adapter",
        str(fake_adapter),
    )
    coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "dispatch",
        "self-report",
        "--dispatch",
        checkpoint_architect,
        "--model",
        "project-architect-model",
    )

    read_only_probe_file = staged_payload("checkpoint-read-only-probe.json")
    read_only_probe_file.write_text(
        json.dumps(
            {
                "dispatch_id": checkpoint_architect,
                "commit_sha": "0" * 40,
                "changed_files": [],
                "remaining_definition_of_done": [],
                "passing_checks": [],
                "risks": "none",
                "blockers": "none",
                "context_package_id": "not applicable — no context package registered",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    read_only_probe = coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "dispatch",
        "checkpoint",
        "--file",
        str(read_only_probe_file),
    )
    if read_only_probe.returncode == 0:
        sys.exit("coordinator accepted a checkpoint for a read-only role")

    checkpoint_report("architect", checkpoint_architect, "accept", {})
    coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "batch",
        "decide",
        "--batch",
        checkpoint_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-14T16:00:20Z",
    )

    checkpoint_dev = coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "dispatch",
        "create",
        "--batch",
        checkpoint_batch,
        "--role",
        "developer",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-14T16:00:30Z",
    )
    if checkpoint_dev.returncode != 0:
        sys.exit(
            "coordinator rejected the checkpoint fixture's developer dispatch: "
            + checkpoint_dev.stderr
        )
    checkpoint_dev_record = json.loads(checkpoint_dev.stdout)
    checkpoint_dev_id = checkpoint_dev_record["dispatch_id"]
    checkpoint_context_package_id = checkpoint_dev_record["brief"]["context_package_id"]
    coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "dispatch",
        "send",
        "--dispatch",
        checkpoint_dev_id,
        "--adapter",
        str(fake_adapter),
    )
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "self-report",
            "--dispatch",
            checkpoint_dev_id,
            "--model",
            "project-developer-model",
        ).returncode
        != 0
    ):
        sys.exit(
            "coordinator rejected the checkpoint fixture's developer model self-report"
        )

    checkpoint_file_one = orchestration_project / "services" / "checkpoint_demo.py"
    checkpoint_file_one.write_text(
        'def demo():\n    return "session one"\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "services/checkpoint_demo.py"],
        cwd=orchestration_project,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "feat: checkpoint demo session one"],
        cwd=orchestration_project,
        check=True,
    )
    checkpoint_first_sha = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()

    bad_context_package_file = staged_payload("checkpoint-bad-context-package.json")
    bad_context_package_file.write_text(
        json.dumps(
            {
                "dispatch_id": checkpoint_dev_id,
                "commit_sha": checkpoint_first_sha,
                "changed_files": ["services/checkpoint_demo.py"],
                "remaining_definition_of_done": ["implement checkpoint step two"],
                "passing_checks": [
                    {
                        "command": "python developer_check.py",
                        "result": "pass",
                        "evidence": "1 passed",
                    }
                ],
                "risks": "none yet",
                "blockers": "none",
                "context_package_id": "context-package-does-not-exist",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "checkpoint",
            "--file",
            str(bad_context_package_file),
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator accepted a checkpoint referencing a nonexistent context package"
        )

    checkpoint_one_file = staged_payload("checkpoint-session-one.json")
    checkpoint_one_file.write_text(
        json.dumps(
            {
                "dispatch_id": checkpoint_dev_id,
                "commit_sha": checkpoint_first_sha,
                "changed_files": ["services/checkpoint_demo.py"],
                "remaining_definition_of_done": ["implement checkpoint step two"],
                "passing_checks": [
                    {
                        "command": "python developer_check.py",
                        "result": "pass",
                        "evidence": "1 passed",
                    }
                ],
                "risks": "none yet",
                "blockers": "none",
                "context_package_id": checkpoint_context_package_id,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint_one_result = coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "dispatch",
        "checkpoint",
        "--file",
        str(checkpoint_one_file),
    )
    if checkpoint_one_result.returncode != 0:
        sys.exit(
            "coordinator rejected a valid checkpoint: " + checkpoint_one_result.stderr
        )
    checkpoint_one_record = json.loads(checkpoint_one_result.stdout)
    checkpoint_record_path = (
        lifecycle_records(checkpoint_state)
        / "checkpoints"
        / f"{checkpoint_one_record['checkpoint_id']}.json"
    )
    if not checkpoint_record_path.is_file():
        sys.exit("checkpoint was not persisted as its own immutable ledger record")
    persisted_checkpoint = json.loads(
        checkpoint_record_path.read_text(encoding="utf-8")
    )
    if set(persisted_checkpoint) != {
        "checkpoint_id",
        "dispatch_id",
        "batch_id",
        "commit_sha",
        "changed_files",
        "remaining_definition_of_done",
        "passing_checks",
        "risks",
        "blockers",
        "context_package_id",
        "created_at",
    }:
        sys.exit(
            "checkpoint record captured more or less than commit SHA, changed files, remaining DoD, "
            "passing checks, risks/blockers and a Context Package reference"
        )

    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "self-report",
            "--dispatch",
            checkpoint_dev_id,
            "--model",
            "project-developer-model",
        ).returncode
        == 0
    ):
        sys.exit("coordinator accepted a self-report against a checkpointed dispatch")

    # Continuation authorization (Issue #140): a recognized rate-limit termination reason
    # authorizes resume automatically; anything else is a planned trigger requiring the same
    # coordinator decision accept/retry/block/fail already use.
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "resume",
            "--dispatch",
            checkpoint_dev_id,
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator resumed a checkpointed dispatch with no termination reason and no trigger"
        )
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "resume",
            "--dispatch",
            checkpoint_dev_id,
            "--termination-reason",
            "worker-crashed",
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator resumed a checkpointed dispatch on an unrecognized termination reason without a trigger"
        )

    checkpoint_resume = coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "dispatch",
        "resume",
        "--dispatch",
        checkpoint_dev_id,
        "--termination-reason",
        "rate_limit",
    )
    if checkpoint_resume.returncode != 0:
        sys.exit(
            "coordinator rejected a rate-limit resume of a checkpointed dispatch: "
            + checkpoint_resume.stderr
        )
    if json.loads(checkpoint_resume.stdout)["dispatch_id"] != checkpoint_dev_id:
        sys.exit("resuming a checkpoint did not keep the same dispatch ID")
    if json.loads(checkpoint_resume.stdout)["authorization"] != "continue-automatic":
        sys.exit("a rate-limit resume was not authorized automatically")
    checkpoint_after_rate_limit_resume = json.loads(
        (
            lifecycle_records(checkpoint_state) / "batches" / f"{checkpoint_batch}.json"
        ).read_text(encoding="utf-8")
    )
    if not any(
        entry.get("dispatch_id") == checkpoint_dev_id
        and entry.get("decision") == "continue-automatic"
        for entry in checkpoint_after_rate_limit_resume.get("coordinator_decisions", [])
    ):
        sys.exit(
            "rate-limit resume did not record a continue-automatic coordinator decision"
        )

    checkpoint_file_two = orchestration_project / "services" / "checkpoint_demo_v2.py"
    checkpoint_file_two.write_text(
        'def demo_v2():\n    return "session two"\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "services/checkpoint_demo_v2.py"],
        cwd=orchestration_project,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "feat: checkpoint demo session two"],
        cwd=orchestration_project,
        check=True,
    )
    checkpoint_second_sha = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()

    early_final_report_file = staged_payload("checkpoint-early-final-report.json")
    early_final_report_file.write_text(
        json.dumps(
            {
                "dispatch_id": checkpoint_dev_id,
                "ticket": "#139",
                "role": "developer",
                "outcome": "completed",
                "output": "both checkpoint sessions complete",
                "commit_sha": checkpoint_second_sha,
                "changed_files": [
                    "services/checkpoint_demo.py",
                    "services/checkpoint_demo_v2.py",
                ],
                "commit_map": [
                    {"commit_sha": sha, "plan_entry_id": entry["id"]}
                    for sha, entry in zip(
                        (checkpoint_first_sha, checkpoint_second_sha),
                        checkpoint_dev_record["brief"]["commit_plan"],
                        strict=True,
                    )
                ],
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
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "report",
            "submit",
            "--file",
            str(early_final_report_file),
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator accepted a completion report before the resumed session's fresh self-report"
        )

    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "self-report",
            "--dispatch",
            checkpoint_dev_id,
            "--model",
            "project-developer-model",
        ).returncode
        != 0
    ):
        sys.exit(
            "coordinator rejected the resumed worker session's fresh model self-report"
        )
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "heartbeat",
            "--dispatch",
            checkpoint_dev_id,
        ).returncode
        != 0
    ):
        sys.exit("coordinator rejected the resumed worker session's heartbeat")

    checkpoint_two_file = staged_payload("checkpoint-session-two.json")
    checkpoint_two_file.write_text(
        json.dumps(
            {
                "dispatch_id": checkpoint_dev_id,
                "commit_sha": checkpoint_second_sha,
                "changed_files": [
                    "services/checkpoint_demo.py",
                    "services/checkpoint_demo_v2.py",
                ],
                "remaining_definition_of_done": [],
                "passing_checks": [
                    {
                        "command": "python developer_check.py",
                        "result": "pass",
                        "evidence": "1 passed",
                    }
                ],
                "risks": "none",
                "blockers": "none",
                "context_package_id": checkpoint_context_package_id,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "checkpoint",
            "--file",
            str(checkpoint_two_file),
        ).returncode
        != 0
    ):
        sys.exit("coordinator rejected the checkpoint fixture's session-two checkpoint")

    # Planned trigger (no recognized termination reason): safe default toward approval, reused
    # coordinator_decisions record type, threshold and drift checks all enforced. `blockers` is
    # deliberately absent from the continuation facts -- only scope/DoD, risks and dependencies
    # gate drift, matching the issue's acceptance criteria.
    facts_unchanged = {
        "dispatch_id": checkpoint_dev_id,
        "remaining_definition_of_done": [],
        "risks": "none",
        "dependencies": ["none"],
    }
    facts_unchanged_file = staged_payload("continuation-facts-unchanged.json")
    facts_unchanged_file.write_text(
        json.dumps(facts_unchanged, indent=2) + "\n", encoding="utf-8"
    )
    facts_risks_drifted_file = staged_payload("continuation-facts-risks-drifted.json")
    facts_risks_drifted_file.write_text(
        json.dumps(
            {**facts_unchanged, "risks": "scope grew since the checkpoint"},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    facts_dependencies_drifted_file = staged_payload(
        "continuation-facts-dependencies-drifted.json"
    )
    facts_dependencies_drifted_file.write_text(
        json.dumps(
            {**facts_unchanged, "dependencies": ["#999"]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "resume",
            "--dispatch",
            checkpoint_dev_id,
            "--trigger",
            "context-limit",
            "--measured-value",
            "1000",
            "--file",
            str(facts_unchanged_file),
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-14T16:01:00Z",
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator resumed a planned-trigger continuation below its configured threshold"
        )
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "resume",
            "--dispatch",
            checkpoint_dev_id,
            "--trigger",
            "tdd-cycles",
            "--measured-value",
            "1",
            "--file",
            str(facts_unchanged_file),
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-14T16:01:00Z",
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator resumed a tdd-cycles planned trigger below its configured threshold"
        )
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "resume",
            "--dispatch",
            checkpoint_dev_id,
            "--trigger",
            "failure-log",
            "--measured-value",
            "100",
            "--file",
            str(facts_unchanged_file),
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-14T16:01:00Z",
        ).returncode
        == 0
    ):
        sys.exit(
            "coordinator resumed a failure-log planned trigger below its configured threshold"
        )

    risks_drifted_resume = coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "dispatch",
        "resume",
        "--dispatch",
        checkpoint_dev_id,
        "--trigger",
        "context-limit",
        "--measured-value",
        "200000",
        "--file",
        str(facts_risks_drifted_file),
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-14T16:01:00Z",
    )
    if risks_drifted_resume.returncode == 0:
        sys.exit(
            "coordinator resumed a planned-trigger continuation whose recorded risks drifted from the checkpoint"
        )
    if (
        "close" not in risks_drifted_resume.stderr.lower()
        or "new one" not in risks_drifted_resume.stderr.lower()
    ):
        sys.exit(
            "drifted continuation risks did not point the coordinator at closing and reopening the dispatch"
        )

    dependencies_drifted_resume = coordinator_run(
        "--state-dir",
        str(checkpoint_state),
        "dispatch",
        "resume",
        "--dispatch",
        checkpoint_dev_id,
        "--trigger",
        "vertical-slice",
        "--file",
        str(facts_dependencies_drifted_file),
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-14T16:01:00Z",
    )
    if dependencies_drifted_resume.returncode == 0:
        sys.exit(
            "coordinator resumed a vertical-slice continuation whose recorded dependencies drifted from the dispatch"
        )
    if (
        "close" not in dependencies_drifted_resume.stderr.lower()
        or "new one" not in dependencies_drifted_resume.stderr.lower()
    ):
        sys.exit(
            "drifted continuation dependencies did not point the coordinator at closing and reopening the dispatch"
        )

    # AC5: adaptive-policy thresholds come from project configuration, not a hard-coded value --
    # a project-configured low threshold authorizes exactly the measured value the default (150000)
    # rejected above.
    original_orchestration_config_text = orchestration_config.read_text(
        encoding="utf-8"
    )
    low_threshold_config = json.loads(original_orchestration_config_text)
    low_threshold_config["adaptive_continuation_policy"] = {"context_limit": 500}
    orchestration_config.write_text(
        json.dumps(low_threshold_config, indent=2) + "\n", encoding="utf-8"
    )
    try:
        planned_resume = coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "resume",
            "--dispatch",
            checkpoint_dev_id,
            "--trigger",
            "context-limit",
            "--measured-value",
            "500",
            "--file",
            str(facts_unchanged_file),
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-14T16:01:00Z",
        )
    finally:
        orchestration_config.write_text(
            original_orchestration_config_text, encoding="utf-8"
        )
    if planned_resume.returncode != 0:
        sys.exit(
            "coordinator rejected a planned-trigger continuation meeting the project-configured "
            "adaptive_continuation_policy threshold: " + planned_resume.stderr
        )
    if json.loads(planned_resume.stdout)["authorization"] != "continue":
        sys.exit(
            "a planned-trigger resume was not authorized through a coordinator decision"
        )
    checkpoint_after_planned_resume = json.loads(
        (
            lifecycle_records(checkpoint_state) / "batches" / f"{checkpoint_batch}.json"
        ).read_text(encoding="utf-8")
    )
    if not any(
        entry.get("dispatch_id") == checkpoint_dev_id
        and entry.get("decision") == "continue"
        and entry.get("approved_by") == "project coordinator"
        for entry in checkpoint_after_planned_resume.get("coordinator_decisions", [])
    ):
        sys.exit(
            "planned-trigger resume did not record a continue coordinator decision"
        )

    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "self-report",
            "--dispatch",
            checkpoint_dev_id,
            "--model",
            "project-developer-model",
        ).returncode
        != 0
    ):
        sys.exit(
            "coordinator rejected the second resumed worker session's fresh model self-report"
        )
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "dispatch",
            "heartbeat",
            "--dispatch",
            checkpoint_dev_id,
        ).returncode
        != 0
    ):
        sys.exit("coordinator rejected the second resumed worker session's heartbeat")
    if (
        coordinator_run(
            "--state-dir",
            str(checkpoint_state),
            "report",
            "submit",
            "--file",
            str(early_final_report_file),
        ).returncode
        != 0
    ):
        sys.exit(
            "coordinator rejected the single completion report closing the resumed dispatch"
        )

    checkpoint_final_batch = json.loads(
        (
            lifecycle_records(checkpoint_state) / "batches" / f"{checkpoint_batch}.json"
        ).read_text(encoding="utf-8")
    )
    checkpoint_final_entry = next(
        item
        for item in checkpoint_final_batch["dispatches"]
        if item["dispatch_id"] == checkpoint_dev_id
    )
    if checkpoint_final_entry["state"] != "reported":
        sys.exit(
            "checkpoint/resume round-trip did not end in exactly one completion report on the same dispatch"
        )

    print("checkpoint and worker session verification passed")
    ctx.checkpoint_dev_id = checkpoint_dev_id
    ctx.checkpoint_state = checkpoint_state
