"""Self-report модели и watchdog dispatch: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from types import SimpleNamespace


def run(ctx: SimpleNamespace) -> None:
    """Self-report модели и watchdog dispatch.

    Читает из контекста: `coordinator_run`, `fake_adapter`, `lifecycle_records`,
    `orchestration_project`, `shared_worktree`, `sync_origin_base`, `test_root`.
    """
    coordinator_run = ctx.coordinator_run
    fake_adapter = ctx.fake_adapter
    lifecycle_records = ctx.lifecycle_records
    orchestration_project = ctx.orchestration_project
    shared_worktree = ctx.shared_worktree
    sync_origin_base = ctx.sync_origin_base
    test_root = ctx.test_root
    # A mismatched model self-report is terminal for its batch, so it runs on its own state.
    mismatch_state = test_root / "mismatch-state"
    subprocess.run(
        ["git", "branch", "feature/issue-902-model-mismatch"],
        cwd=orchestration_project,
        check=True,
    )
    sync_origin_base()
    mismatch_batch = json.loads(
        coordinator_run(
            "--state-dir",
            str(mismatch_state),
            "batch",
            "create",
            "--ticket",
            "#902",
            "--branch",
            "feature/issue-902-model-mismatch",
            "--worktree",
            str(shared_worktree),
            "--zone",
            "backend",
            "--definition-of-done",
            "implement the requested backend change",
            "--prohibited-change",
            "do not merge",
        ).stdout
    )["batch_id"]
    coordinator_run(
        "--state-dir",
        str(mismatch_state),
        "batch",
        "approve",
        "--batch",
        mismatch_batch,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T13:00:00Z",
    )
    mismatch_dispatch = json.loads(
        coordinator_run(
            "--state-dir",
            str(mismatch_state),
            "dispatch",
            "create",
            "--batch",
            mismatch_batch,
            "--role",
            "architect",
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-09T13:00:10Z",
        ).stdout
    )["dispatch_id"]
    coordinator_run(
        "--state-dir",
        str(mismatch_state),
        "dispatch",
        "send",
        "--dispatch",
        mismatch_dispatch,
        "--adapter",
        str(fake_adapter),
    )
    # "Sonnet 5" is the display name that reached the worker in the incident this rule exists for;
    # the approved brief resolved the CLI alias instead.
    mismatched = coordinator_run(
        "--state-dir",
        str(mismatch_state),
        "dispatch",
        "self-report",
        "--dispatch",
        mismatch_dispatch,
        "--model",
        "Sonnet 5",
    )
    if mismatched.returncode == 0:
        sys.exit(
            "coordinator accepted a model self-report that contradicts the immutable brief"
        )
    if "project-architect-model" not in mismatched.stderr:
        sys.exit(
            "coordinator did not name the approved model in the mismatch: "
            + mismatched.stderr
        )
    blocked_status = json.loads(
        (
            lifecycle_records(mismatch_state)
            / "dispatch-status"
            / f"{mismatch_dispatch}.json"
        ).read_text(encoding="utf-8")
    )
    if (
        blocked_status["state"] != "blocked"
        or blocked_status["model_self_report"]["match"] is not False
    ):
        sys.exit("a mismatched model self-report did not block its dispatch")
    if (
        json.loads(
            (
                lifecycle_records(mismatch_state) / "batches" / f"{mismatch_batch}.json"
            ).read_text(encoding="utf-8")
        )["state"]
        != "blocked"
    ):
        sys.exit("a mismatched model self-report did not block its batch")

    print("dispatch model self-report and watchdog verification passed")
