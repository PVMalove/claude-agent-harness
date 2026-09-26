"""Значения coordinator по умолчанию без конфига: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from types import SimpleNamespace

from scripts.clean_room.support import (
    HARNESS,
    ROOT,
    run_ok,
)


def run(ctx: SimpleNamespace) -> None:
    """Значения coordinator по умолчанию без конфига.

    Читает из контекста: `coordinator_run`, `fake_adapter`, `orchestration_config`,
    `orchestration_project`, `shared_worktree`, `sync_origin_base`, `test_root`.
    """
    coordinator_run = ctx.coordinator_run
    fake_adapter = ctx.fake_adapter
    orchestration_config = ctx.orchestration_config
    orchestration_project = ctx.orchestration_project
    shared_worktree = ctx.shared_worktree
    sync_origin_base = ctx.sync_origin_base
    test_root = ctx.test_root
    # Zero project configuration is a supported default: the whole repository is the zone and the
    # invoking session supplies the role runtime, so an in-process pipeline needs no assignment plan.
    zero_config_state = test_root / "zero-config-state"
    saved_config = orchestration_config.read_text(encoding="utf-8")
    # `harness init` seeds an intentionally empty template. A present-but-empty file states nothing,
    # so it has to mean the same as no file: otherwise a freshly initialised project takes the
    # configured path, finds no zone and no assignment, and cannot start a batch at all — while
    # deleting the seeded file would fix it.
    empty_seed = json.loads(
        (ROOT / "harness" / "project" / "orchestration.json.tmpl").read_text(
            encoding="utf-8"
        )
    )
    if empty_seed.get("backend_zones") or empty_seed.get("assignment_plans"):
        sys.exit(
            "the orchestration seed is no longer empty; this scenario needs rewriting"
        )
    orchestration_config.write_text(
        json.dumps(empty_seed, indent=2) + "\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "branch", "feature/issue-906-empty-seed"],
        cwd=orchestration_project,
        check=True,
    )
    sync_origin_base()
    seeded = coordinator_run(
        "--state-dir",
        str(test_root / "empty-seed-state"),
        "batch",
        "create",
        "--ticket",
        "#906",
        "--branch",
        "feature/issue-906-empty-seed",
        "--worktree",
        str(shared_worktree),
        "--definition-of-done",
        "implement the requested change",
        "--prohibited-change",
        "do not merge",
    )
    if seeded.returncode != 0:
        sys.exit(
            "the seeded empty orchestration config blocks a batch instead of falling back: "
            + seeded.stderr
        )
    if json.loads(seeded.stdout)["zone"] != "repository":
        sys.exit(
            "an empty orchestration config did not fall back to the whole-repository default"
        )

    orchestration_config.unlink()
    run_ok(HARNESS + ["health", str(orchestration_project)])
    subprocess.run(
        ["git", "branch", "feature/issue-903-zero-config"],
        cwd=orchestration_project,
        check=True,
    )
    sync_origin_base()
    zero_batch = coordinator_run(
        "--state-dir",
        str(zero_config_state),
        "batch",
        "create",
        "--ticket",
        "#903",
        "--branch",
        "feature/issue-903-zero-config",
        "--worktree",
        str(shared_worktree),
        "--definition-of-done",
        "implement the requested change",
        "--prohibited-change",
        "do not merge",
    )
    if zero_batch.returncode != 0:
        sys.exit(
            "coordinator rejected a batch without project orchestration config: "
            + zero_batch.stderr
        )
    zero_batch_id = json.loads(zero_batch.stdout)["batch_id"]
    if json.loads(zero_batch.stdout)["zone"] != "repository":
        sys.exit("zero-config batch did not default to the whole repository")
    coordinator_run(
        "--state-dir",
        str(zero_config_state),
        "batch",
        "approve",
        "--batch",
        zero_batch_id,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T14:00:00Z",
    )
    if (
        coordinator_run(
            "--state-dir",
            str(zero_config_state),
            "dispatch",
            "create",
            "--batch",
            zero_batch_id,
            "--role",
            "architect",
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-09T14:00:10Z",
        ).returncode
        == 0
    ):
        sys.exit("zero-config dispatch was created without a session model and effort")
    zero_dispatch = coordinator_run(
        "--state-dir",
        str(zero_config_state),
        "dispatch",
        "create",
        "--batch",
        zero_batch_id,
        "--role",
        "architect",
        "--model",
        "session-model",
        "--effort",
        "high",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T14:00:20Z",
    )
    if zero_dispatch.returncode != 0:
        sys.exit("coordinator rejected a zero-config dispatch: " + zero_dispatch.stderr)
    zero_brief = json.loads(zero_dispatch.stdout)["brief"]
    if (
        zero_brief["resolved_model"] != "session-model"
        or zero_brief["resolved_effort"] != "high"
    ):
        sys.exit(
            "zero-config dispatch did not take its runtime from the invoking session"
        )
    if zero_brief["resolved_transport"] != "in-process":
        sys.exit("zero-config dispatch did not fall back to the in-process transport")
    zero_dispatch_id = json.loads(zero_dispatch.stdout)["dispatch_id"]
    if (
        coordinator_run(
            "--state-dir",
            str(zero_config_state),
            "dispatch",
            "send",
            "--dispatch",
            zero_dispatch_id,
            "--adapter",
            str(fake_adapter),
        ).returncode
        == 0
    ):
        sys.exit("an in-process dispatch was handed to a runtime adapter")
    zero_sent = coordinator_run(
        "--state-dir",
        str(zero_config_state),
        "dispatch",
        "send",
        "--dispatch",
        zero_dispatch_id,
    )
    if zero_sent.returncode != 0:
        sys.exit("coordinator rejected an in-process handoff: " + zero_sent.stderr)
    if json.loads(zero_sent.stdout)["transport"] != "in-process":
        sys.exit(
            "in-process handoff did not report its transport to the coordinator session"
        )
    orchestration_config.write_text(saved_config, encoding="utf-8")

    print("zero-config coordinator defaults verification passed")
