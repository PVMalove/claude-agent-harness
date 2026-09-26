"""Базовые метрики оркестрации: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from types import SimpleNamespace


def run(ctx: SimpleNamespace) -> None:
    """Базовые метрики оркестрации.

    Читает из контекста: `checkpoint_dev_id`, `checkpoint_state`, `claude_dir`,
    `orchestration_project`, `qa_id`, `report`, `stats_cli`, `stats_repo`, `test_root`.
    """
    checkpoint_dev_id = ctx.checkpoint_dev_id
    checkpoint_state = ctx.checkpoint_state
    claude_dir = ctx.claude_dir
    orchestration_project = ctx.orchestration_project
    qa_id = ctx.qa_id
    report = ctx.report
    stats_cli = ctx.stats_cli
    stats_repo = ctx.stats_repo
    test_root = ctx.test_root
    # Orchestration-derived baseline metrics (Issue #141): worker sessions per dispatch with each
    # restart's coordinator-recorded reason, QA failure rate, and code-review scope excess -- all
    # read from the coordinator's own ledger records, never a role's self-report, and reported as
    # missing rather than zero when the ledger is absent.
    # `report` here is still the very first ticket-7 fixture read (`unpriced`, captured before
    # `stats_repo` ever gained a `.harness/orchestration` directory below), so this is a genuine
    # no-ledger case, not the synthetic ledger this same block adds to stats_repo further down.
    ledgerless_report = report
    if ledgerless_report["orchestration"].get("status") != "нет данных":
        sys.exit(
            "delivery-stats invented orchestration metrics with no ledger present: "
            + json.dumps(ledgerless_report["orchestration"], ensure_ascii=False)
        )

    # orchestration_project's fixture batches are all recorded against one shared checkout (their
    # "branch" is a coordinator-record label, never actually checked out), so its git history alone
    # never shows ticket-scoped diff volume. Feed one minimal turn per ticket so delivery-stats' own
    # "no evidence at all" guard does not mask the orchestration-ledger read this is testing.
    orch_claude_dir = test_root / "orch-claude-sessions"
    orch_claude_dir.mkdir()

    def orch_turn(branch):
        return json.dumps(
            {
                "type": "assistant",
                "gitBranch": branch,
                "sessionId": "orch-fixture",
                "timestamp": "2026-09-14T16:00:00.000Z",
                "cwd": str(orchestration_project),
                "message": {
                    "model": "orch-fixture-model",
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 10,
                    },
                },
            }
        )

    (orch_claude_dir / "orch-fixture.jsonl").write_text(
        "\n".join(
            [
                orch_turn("feature/issue-901-coordinator"),
                orch_turn("feature/issue-139-checkpoint"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ticket_901_stats = subprocess.run(
        [
            sys.executable,
            str(stats_cli),
            "--repo",
            str(orchestration_project),
            "--epic",
            "901",
            "--tickets",
            "901",
            "--home",
            str(test_root / "stats-empty-home"),
            "--claude-projects",
            str(orch_claude_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if ticket_901_stats.returncode != 0:
        sys.exit(
            "delivery-stats failed reading the orchestration ledger: "
            + ticket_901_stats.stderr
        )
    orch_901 = json.loads(ticket_901_stats.stdout)["orchestration"]
    if orch_901.get("status") != "ok":
        sys.exit(
            "delivery-stats did not find the orchestration ledger: "
            + json.dumps(orch_901, ensure_ascii=False)
        )
    ticket_901_metrics = orch_901["tickets"]["901"]
    sessions_by_dispatch = {
        s["dispatch_id"]: s for s in ticket_901_metrics["worker_sessions"]
    }
    if (
        sessions_by_dispatch[qa_id]["sessions"] != 1
        or sessions_by_dispatch[qa_id]["restarts"]
    ):
        sys.exit(
            "delivery-stats invented a worker-session restart for a single-session dispatch: "
            + json.dumps(sessions_by_dispatch[qa_id])
        )
    if ticket_901_metrics["qa_failure_rate"] != 0.0:
        sys.exit(
            "delivery-stats mis-scored QA failure rate on an accepted QA dispatch: "
            + json.dumps(ticket_901_metrics)
        )
    accepted_scope = ticket_901_metrics["review_scope"]
    if (
        not isinstance(accepted_scope, list)
        or accepted_scope[0]["files_out_of_scope"] != 0
        or accepted_scope[0]["share"] != 0.0
    ):
        sys.exit(
            "delivery-stats mis-scored review scope excess for an in-zone diff: "
            + json.dumps(accepted_scope)
        )

    # A checkpointed/resumed dispatch spans more than one worker session, and each restart's reason
    # is the coordinator's own recorded decision note, not an invented label.
    checkpoint_stats = subprocess.run(
        [
            sys.executable,
            str(stats_cli),
            "--repo",
            str(orchestration_project),
            "--epic",
            "139",
            "--tickets",
            "139",
            "--home",
            str(test_root / "stats-empty-home"),
            "--claude-projects",
            str(orch_claude_dir),
            "--orchestration-state-dir",
            str(checkpoint_state),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if checkpoint_stats.returncode != 0:
        sys.exit(
            "delivery-stats failed reading the checkpoint ledger: "
            + checkpoint_stats.stderr
        )
    checkpoint_metrics = json.loads(checkpoint_stats.stdout)["orchestration"][
        "tickets"
    ]["139"]
    dev_session = next(
        s
        for s in checkpoint_metrics["worker_sessions"]
        if s["dispatch_id"] == checkpoint_dev_id
    )
    if dev_session["sessions"] != 3:
        sys.exit(
            "delivery-stats did not count every checkpoint/resume worker session: "
            + json.dumps(dev_session)
        )
    if [restart["decision"] for restart in dev_session["restarts"]] != [
        "continue-automatic",
        "continue",
    ]:
        sys.exit(
            "delivery-stats lost the coordinator's own restart decisions: "
            + json.dumps(dev_session)
        )
    if "termination_reason=rate_limit" not in dev_session["restarts"][0]["reason"]:
        sys.exit(
            "delivery-stats did not surface the rate-limit restart reason: "
            + json.dumps(dev_session)
        )
    if "context-limit" not in dev_session["restarts"][1]["reason"]:
        sys.exit(
            "delivery-stats did not surface the planned-trigger restart reason: "
            + json.dumps(dev_session)
        )

    # A code-review diff can, in principle, span more than the write role's declared zone, and a
    # batch can carry more than one QA outcome; delivery-stats must score both from the raw ledger
    # records rather than only ever reporting the all-clean case the coordinator's own enforcement
    # normally guarantees. Written directly as ledger records (bypassing the coordinator CLI, which
    # would refuse an out-of-zone developer commit by construction) to exercise that arithmetic.
    synthetic_state = stats_repo / ".harness" / "orchestration" / "state"
    (synthetic_state / "batches").mkdir(parents=True)
    (synthetic_state / "dispatches").mkdir()
    (synthetic_state / "batches" / "batch-950-synthetic.json").write_text(
        json.dumps(
            {
                "batch_id": "batch-950-synthetic",
                "branch": "feature/issue-950-synthetic",
                "dispatches": [
                    {
                        "dispatch_id": "dispatch-950-developer",
                        "role": "developer",
                        "state": "reported",
                    },
                    {
                        "dispatch_id": "dispatch-950-review",
                        "role": "code-review",
                        "state": "reported",
                    },
                    {
                        "dispatch_id": "dispatch-950-qa-one",
                        "role": "qa",
                        "state": "reported",
                        "decision": {"decision": "block"},
                    },
                    {
                        "dispatch_id": "dispatch-950-qa-two",
                        "role": "qa",
                        "state": "reported",
                        "decision": {"decision": "accept"},
                    },
                ],
                "coordinator_decisions": [
                    {
                        "dispatch_id": "dispatch-950-developer",
                        "decision": "continue-automatic",
                        "note": "termination_reason=rate_limit",
                        "approved_at": "2026-01-01T00:00:00+00:00",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (synthetic_state / "dispatches" / "dispatch-950-developer.json").write_text(
        json.dumps({"write_paths": ["services/**"]}), encoding="utf-8"
    )
    (synthetic_state / "dispatches" / "dispatch-950-review.json").write_text(
        json.dumps({"review_scope": ["services/a.py", "docs/out.md"]}), encoding="utf-8"
    )
    synthetic_stats = subprocess.run(
        [
            sys.executable,
            str(stats_cli),
            "--repo",
            str(stats_repo),
            "--epic",
            "7",
            "--tickets",
            "7,950",
            "--home",
            str(test_root / "stats-home"),
            "--claude-projects",
            str(claude_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if synthetic_stats.returncode != 0:
        sys.exit(
            "delivery-stats failed reading a synthetic ledger record: "
            + synthetic_stats.stderr
        )
    synthetic_metrics = json.loads(synthetic_stats.stdout)["orchestration"]["tickets"][
        "950"
    ]
    if synthetic_metrics["qa_failure_rate"] != 0.5:
        sys.exit(
            "delivery-stats mis-scored a mixed QA outcome: "
            + json.dumps(synthetic_metrics)
        )
    synthetic_scope = synthetic_metrics["review_scope"][0]
    if (
        synthetic_scope["files_out_of_scope"] != 1
        or synthetic_scope["out_of_scope_files"] != ["docs/out.md"]
        or synthetic_scope["share"] != 0.5
    ):
        sys.exit(
            "delivery-stats did not score a review diff file outside the declared zone: "
            + json.dumps(synthetic_scope)
        )
    synthetic_dev_session = next(
        s
        for s in synthetic_metrics["worker_sessions"]
        if s["dispatch_id"] == "dispatch-950-developer"
    )
    if synthetic_dev_session["sessions"] != 2:
        sys.exit(
            "delivery-stats miscounted a synthetic ledger's worker sessions: "
            + json.dumps(synthetic_dev_session)
        )

    print("orchestration baseline metrics verification passed")
