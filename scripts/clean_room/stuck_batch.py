"""Отказ от зависшего batch: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from types import SimpleNamespace


def run(ctx: SimpleNamespace) -> None:
    """Отказ от зависшего batch.

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
    # A worker that dies before confirming its model can never report, and a batch with no pending
    # report can never be decided. Without a sanctioned way out, that batch stays open for good and
    # the only remaining move is editing the state files by hand.
    wedge_state = test_root / "wedged-state"
    subprocess.run(
        ["git", "branch", "feature/issue-905-wedged"],
        cwd=orchestration_project,
        check=True,
    )
    sync_origin_base()
    wedged_batch = json.loads(
        coordinator_run(
            "--state-dir",
            str(wedge_state),
            "batch",
            "create",
            "--ticket",
            "#905",
            "--branch",
            "feature/issue-905-wedged",
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
        str(wedge_state),
        "batch",
        "approve",
        "--batch",
        wedged_batch,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T16:00:00Z",
    )
    wedged_dispatch = json.loads(
        coordinator_run(
            "--state-dir",
            str(wedge_state),
            "dispatch",
            "create",
            "--batch",
            wedged_batch,
            "--role",
            "architect",
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-09T16:00:10Z",
        ).stdout
    )["dispatch_id"]
    coordinator_run(
        "--state-dir",
        str(wedge_state),
        "dispatch",
        "send",
        "--dispatch",
        wedged_dispatch,
        "--adapter",
        str(fake_adapter),
    )
    for decision in ("fail", "block"):
        if (
            coordinator_run(
                "--state-dir",
                str(wedge_state),
                "batch",
                "decide",
                "--batch",
                wedged_batch,
                "--decision",
                decision,
                "--approved-by",
                "project coordinator",
                "--approved-at",
                "2026-09-09T16:00:20Z",
            ).returncode
            == 0
        ):
            sys.exit(
                f"batch decide --decision {decision} unexpectedly settled a batch with no report"
            )

    listed = coordinator_run("--state-dir", str(wedge_state), "batch", "list", "--open")
    if listed.returncode != 0:
        sys.exit("coordinator cannot list batches: " + listed.stderr)
    entry = next(
        (
            item
            for item in json.loads(listed.stdout)["batches"]
            if item["batch_id"] == wedged_batch
        ),
        None,
    )
    if entry is None or entry["open_dispatches"] != [wedged_dispatch]:
        sys.exit(
            "batch list did not surface the stuck dispatch: " + listed.stdout[:200]
        )

    if (
        coordinator_run(
            "--state-dir",
            str(wedge_state),
            "batch",
            "abandon",
            "--batch",
            wedged_batch,
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-09T16:00:30Z",
            "--reason",
            "   ",
        ).returncode
        == 0
    ):
        sys.exit("coordinator abandoned a batch without a recorded reason")

    abandoned = coordinator_run(
        "--state-dir",
        str(wedge_state),
        "batch",
        "abandon",
        "--batch",
        wedged_batch,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T16:00:30Z",
        "--reason",
        "orca worker died before the model self-report",
    )
    if abandoned.returncode != 0:
        sys.exit(
            "coordinator could not abandon an undecidable batch: " + abandoned.stderr
        )
    if json.loads(abandoned.stdout)["abandoned_dispatches"] != [wedged_dispatch]:
        sys.exit("abandonment did not name the dispatch it closed")
    record = json.loads(
        (lifecycle_records(wedge_state) / "batches" / f"{wedged_batch}.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        record["state"] != "failed"
        or record["abandoned"]["reason"]
        != "orca worker died before the model self-report"
    ):
        sys.exit(
            "abandonment did not record its reason beside the approval: "
            + json.dumps(record["abandoned"])
        )
    if [item["state"] for item in record["dispatches"]] != ["abandoned"]:
        sys.exit("abandonment left the stuck dispatch open")
    if (
        json.loads(
            (
                lifecycle_records(wedge_state)
                / "dispatch-status"
                / f"{wedged_dispatch}.json"
            ).read_text(encoding="utf-8")
        )["state"]
        != "abandoned"
    ):
        sys.exit("abandonment did not close the dispatch's own status record")
    if (
        coordinator_run(
            "--state-dir",
            str(wedge_state),
            "batch",
            "abandon",
            "--batch",
            wedged_batch,
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-09T16:00:40Z",
            "--reason",
            "again",
        ).returncode
        == 0
    ):
        sys.exit("coordinator abandoned a fully closed batch a second time")
    # Repair case: a batch whose state was moved by hand without closing what it held. The open
    # dispatch would otherwise be surfaced as live for ever.
    hand_edited = json.loads(
        (lifecycle_records(wedge_state) / "batches" / f"{wedged_batch}.json").read_text(
            encoding="utf-8"
        )
    )
    hand_edited["dispatches"][0]["state"] = "dispatched"
    (lifecycle_records(wedge_state) / "batches" / f"{wedged_batch}.json").write_text(
        json.dumps(hand_edited, ensure_ascii=False, indent=2) + chr(10),
        encoding="utf-8",
    )
    repaired = coordinator_run(
        "--state-dir",
        str(wedge_state),
        "batch",
        "abandon",
        "--batch",
        wedged_batch,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T16:00:50Z",
        "--reason",
        "closing a dispatch left open by a hand-edited record",
    )
    if repaired.returncode != 0:
        sys.exit(
            "coordinator cannot close a dispatch left open on a terminal batch: "
            + repaired.stderr
        )
    if (
        json.loads(
            (
                lifecycle_records(wedge_state) / "batches" / f"{wedged_batch}.json"
            ).read_text(encoding="utf-8")
        )["dispatches"][0]["state"]
        != "abandoned"
    ):
        sys.exit("repair did not close the dangling dispatch")
    # Closing the old batch is what frees the ticket to be started clean.
    sync_origin_base()
    restart = coordinator_run(
        "--state-dir",
        str(wedge_state),
        "batch",
        "create",
        "--ticket",
        "#905",
        "--branch",
        "feature/issue-905-wedged",
        "--worktree",
        str(shared_worktree),
        "--zone",
        "backend",
        "--definition-of-done",
        "implement the requested backend change",
        "--prohibited-change",
        "do not merge",
    )
    if restart.returncode != 0:
        sys.exit(
            "an abandoned batch still blocks a fresh one for the same ticket: "
            + restart.stderr
        )

    print("stuck-batch abandonment verification passed")
