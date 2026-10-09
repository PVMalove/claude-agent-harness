"""Параллельные batch с явным scope: сценарий clean-room из `scripts/test_clean_room.py`."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from scripts.clean_room.support import run_step


def run(ctx: SimpleNamespace) -> None:
    """Два batch с пересекающимися путями и одной legacy-зоной идут параллельно, третий упирается в бюджет.

    Читает из контекста: `coordinator_run`, `orchestration_project`, `sync_origin_base`, `test_root`.
    Конфигурация проекта здесь зонная (`backend_zones`, `concurrency_budget: 2`): так проверяется
    обновление существующего проекта, чей конфиг и ledger несут зоны.
    """
    coordinator_run = ctx.coordinator_run
    orchestration_project = ctx.orchestration_project
    sync_origin_base = ctx.sync_origin_base
    test_root = ctx.test_root
    state = test_root / "parallel-batches-state"

    def plan(ticket, slug, *scope):
        branch = f"feature/issue-{ticket.lstrip('#')}-{slug}"
        worktree = test_root / f"parallel-{slug}"
        run_step(
            ["git", "worktree", "add", "-q", "-b", branch, str(worktree)],
            cwd=orchestration_project,
            check=True,
        )
        sync_origin_base()
        return coordinator_run(
            "--state-dir",
            str(state),
            "batch",
            "create",
            "--ticket",
            ticket,
            "--branch",
            branch,
            "--worktree",
            str(worktree),
            "--definition-of-done",
            "implement the requested backend change",
            "--prohibited-change",
            "do not merge",
            *scope,
        )

    def approve_and_dispatch(batch_id, minute):
        approval = ("--approved-by", "project coordinator")
        coordinator_run(
            "--state-dir",
            str(state),
            "batch",
            "approve",
            "--batch",
            batch_id,
            *approval,
            "--approved-at",
            f"2026-09-09T15:{minute:02d}:00Z",
        )
        return coordinator_run(
            "--state-dir",
            str(state),
            "dispatch",
            "create",
            "--batch",
            batch_id,
            "--role",
            "architect",
            *approval,
            "--approved-at",
            f"2026-09-09T15:{minute:02d}:10Z",
        )

    # Both batches carry the same legacy zone label and overlapping files; neither locks the other.
    first = plan("#931", "orders", "--zone", "backend", "--allowed-path", "services/**")
    second = plan(
        "#932", "billing", "--zone", "backend", "--allowed-path", "services/orders/**"
    )
    for created in (first, second):
        if created.returncode != 0:
            sys.exit("coordinator rejected a parallel batch: " + created.stderr)
    first_batch = json.loads(first.stdout)
    second_batch = json.loads(second.stdout)
    if first_batch["zone"] != second_batch["zone"] or first_batch["allowed_paths"] != [
        "services/**"
    ]:
        sys.exit("parallel batches did not keep their zone label and explicit scope")
    for index, batch in enumerate((first_batch, second_batch)):
        dispatched = approve_and_dispatch(batch["batch_id"], 10 + index)
        if dispatched.returncode != 0:
            sys.exit(
                "overlapping scope or a shared zone blocked a parallel dispatch: "
                + dispatched.stderr
            )

    # The budget is the only limit: a third running batch is refused and the remedy names it.
    third = plan("#933", "shipping", "--allowed-path", "services/shipping/**")
    if third.returncode != 0:
        sys.exit("coordinator rejected a zone-free batch: " + third.stderr)
    third_batch = json.loads(third.stdout)
    if third_batch["zone"] is not None:
        sys.exit("a batch planned without --zone recorded a zone")
    over_budget = approve_and_dispatch(third_batch["batch_id"], 20)
    if over_budget.returncode == 0:
        sys.exit("a dispatch beyond concurrency_budget was created")
    if "concurrency_budget" not in over_budget.stderr:
        sys.exit("the budget rejection did not name concurrency_budget")

    # The same work is never planned twice while the first batch is unfinished.
    twin = coordinator_run(
        "--state-dir",
        str(state),
        "batch",
        "create",
        "--ticket",
        "#931",
        "--branch",
        "feature/issue-931-orders",
        "--worktree",
        str(test_root / "parallel-orders"),
        "--allowed-path",
        "services/**",
        "--definition-of-done",
        "repeat the unfinished work",
        "--prohibited-change",
        "do not merge",
    )
    if twin.returncode == 0:
        sys.exit("a second batch for unfinished work was planned")
    if "already holds unfinished work" not in twin.stderr:
        sys.exit("the duplicate batch rejection did not explain the guard")

    print("parallel batches with explicit scope verification passed")
