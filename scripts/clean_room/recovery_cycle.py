"""Installed public CLI: fix-forward, startup pause, manual recovery, rewind and publish."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from scripts.clean_room.support import HARNESS, run_step


def run(ctx: SimpleNamespace) -> None:
    project = ctx.test_root / "recovery-project"
    remote = ctx.test_root / "recovery-origin.git"
    worktree = ctx.test_root / "recovery-worker"
    project.mkdir()
    run_step(["git", "init", "-q", str(project)], check=True)
    run_step(["git", "init", "--bare", "-q", str(remote)], check=True)
    run_step(
        [
            *HARNESS,
            "init",
            str(project),
            "--capability",
            "backend-orchestration",
            "--base-branch",
            "main",
            "--language",
            "ru",
            "--pr-base-branch",
            "main",
            "--qa-gate-command",
            "echo verified",
        ],
        check=True,
    )

    def git(*args: str, cwd: Path = project) -> str:
        return run_step(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("config", "user.name", "Fixture operator")
    git("config", "user.email", "fixture@example.invalid")
    git("checkout", "-b", "main")
    (project / "service.py").write_text("VALUE = 0\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "feat: seed recovery fixture")
    git("remote", "add", "origin", str(remote))
    git("push", "-u", "origin", "main")
    branch = "feature/issue-662-recovery"
    git("worktree", "add", "--no-track", "-b", branch, str(worktree), "origin/main")
    runtime = {"claude": {"profiles": ["fixture"], "model": "sonnet", "effort": "high"}}
    config = {
        "provider_profiles": {
            "fixture": {
                "capabilities": [
                    "architecture-analysis",
                    "backend-development",
                    "code-review",
                    "independent-verification",
                ],
                "fallback": [],
                "known_limitations": ["fixture"],
            }
        },
        "assignment_plans": {
            role: {"zone": "repository", "runtimes": runtime}
            for role in ("architect", "developer", "code-review", "qa", "verification")
        },
        "backend_zones": {"repository": {"paths": ["**"]}},
        "concurrency_budget": 1,
        "verification_commands": ["echo verified"],
        "approval_policy": "auto",
        "worker_attestation_required": True,
    }
    (project / ".harness/orchestration.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    cli = [
        sys.executable,
        str(project / ".harness/orchestration/coordinator.py"),
        "--repo",
        str(project),
    ]

    def command(*args: str, fails: bool = False) -> dict:
        result = run_step([*cli, *args], check=False, capture_output=True, text=True)
        if fails:
            if result.returncode == 0:
                raise AssertionError(f"unexpected recovery success: {args}")
            return {}
        if result.returncode:
            raise AssertionError(
                f"installed recovery CLI failed: {args}: {result.stderr}"
            )
        return json.loads(result.stdout)

    def approved() -> list[str]:
        return [
            "--approved-by",
            "fixture operator",
            "--approved-at",
            datetime.now(UTC).isoformat(),
        ]

    batch = command(
        "batch",
        "create",
        "--ticket",
        "#662",
        "--branch",
        branch,
        "--worktree",
        str(worktree),
        "--allowed-path",
        "service.py",
        "--definition-of-done",
        "update service",
        "--prohibited-change",
        "secrets",
        "--expected-file",
        "service.py",
        "--expected-service",
        "core",
        "--expected-changed-lines",
        "10",
        "--required-gate",
        "review",
        "--required-gate",
        "qa",
    )["batch_id"]
    command("batch", "approve", "--batch", batch)
    manual = False

    def dispatch(
        role: str, candidate: str | None = None, purpose: str = "work"
    ) -> dict:
        for status in command("dispatch", "status", "--batch", batch)["dispatches"]:
            if status["role"] == role and status["state"] == "approved":
                return {"dispatch_id": status["dispatch_id"], "role": role}
        fields = [
            "--batch",
            batch,
            "--role",
            role,
            "--purpose",
            purpose,
            "--runtime",
            "claude",
        ]
        if candidate:
            fields += ["--candidate-commit", candidate]
        if manual:
            proposal = command("dispatch", "propose", *fields)
            return command(
                "dispatch",
                "create",
                *fields,
                "--transition-digest",
                proposal["transition_digest"],
                *approved(),
            )["brief"]
        return command("dispatch", "create", *fields)["brief"]

    def start(brief: dict, wrong: bool = False) -> dict:
        args = [
            "dispatch",
            "send",
            "--dispatch",
            brief["dispatch_id"],
            "--worktree",
            str(worktree),
        ]
        if brief["role"] == "code-review":
            args += ["--checkout", str(worktree)]
        sent = command(*args)
        actual = json.loads(Path(sent["brief"]).read_text(encoding="utf-8"))
        command(
            "dispatch",
            "self-report",
            "--dispatch",
            brief["dispatch_id"],
            "--model",
            "sonnet",
            "--worktree",
            str(project if wrong else worktree),
            fails=wrong,
        )
        if not wrong:
            command("dispatch", "heartbeat", "--dispatch", brief["dispatch_id"])
        return actual

    def submit(brief: dict, **extra: object) -> None:
        payload = {
            "dispatch_id": brief["dispatch_id"],
            "ticket": "#662",
            "role": brief["role"],
            "outcome": "completed",
            "output": "fixture verified",
            "commit_sha": "not applicable — read-only role",
            "changed_files": [],
            "checks_run": [
                {"command": check, "result": "pass", "evidence": "fixture checked"}
                for check in brief["verification_commands"]
            ],
            "risks": "none",
            "blockers": "none",
            "next_coordinator_action": "decide",
            "report_language": "ru",
            **extra,
        }
        path = Path(brief["report_staging_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        command("report", "submit", "--file", str(path))

    def decision() -> None:
        command(
            "batch", "decide", "--batch", batch, "--decision", "accept", *approved()
        )

    def commit(value: int) -> str:
        (worktree / "service.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
        git("add", "service.py", cwd=worktree)
        git("commit", "-m", f"feat: service value {value}", cwd=worktree)
        return git("rev-parse", "HEAD", cwd=worktree)

    def writer_report(brief: dict, candidate: str) -> None:
        closure = [
            {"item_id": item["item_id"], "commits": [candidate]}
            for items in (brief.get("carried_items") or {}).values()
            for item in items
        ]
        extra = {"carried_item_closure": closure} if closure else {}
        submit(
            brief,
            commit_sha=candidate,
            changed_files=["service.py"],
            risk_triggers=["transactions"],
            commit_map=[
                {"commit_sha": candidate, "plan_entry_id": entry["id"]}
                for entry in brief["commit_plan"]
            ],
            **extra,
        )

    def review(candidate: str, findings: list[dict] | None = None) -> None:
        brief = start(dispatch("code-review", candidate))
        axes = {
            axis: {
                "severity": "clean",
                "findings": findings or [] if axis == "standards" else [],
                "risks": "none",
                "blockers": "none",
            }
            for axis in ("standards", "spec")
        }
        submit(
            brief,
            review={
                "candidate_commit": candidate,
                "scope": brief["review_scope"],
                **axes,
                **(
                    {
                        "carried_items": [
                            {
                                "item_id": item["item_id"],
                                "status": "closed",
                                "evidence": "service.py:1",
                            }
                            for items in brief["carried_items"].values()
                            for item in items
                        ]
                    }
                    if brief.get("carried_items")
                    else {}
                ),
            },
        )

    submit(start(dispatch("architect")))
    first_writer = start(dispatch("developer"))
    candidate = commit(1)
    writer_report(first_writer, candidate)
    command("batch", "auto-decide", "--batch", batch)
    command(
        "risk",
        "assess",
        "--batch",
        batch,
        "--candidate-commit",
        candidate,
        "--changed-file",
        "service.py",
        "--developer-trigger",
        "transactions",
    )
    findings = [
        {"severity": "info", "summary": f"fix item {i}", "evidence": "service.py:1"}
        for i in range(4)
    ]
    review(candidate, findings)
    command("batch", "auto-decide", "--batch", batch)
    old = start(dispatch("developer"), wrong=True)
    state = project / ".harness/orchestration/state"

    def ledger_bytes() -> dict:
        return {
            str(p.relative_to(state)): p.read_bytes()
            for p in state.rglob("*")
            if p.is_file()
        }

    before = ledger_bytes()
    observed = command("batch", "auto-report", "--batch", batch)
    assert observed["observed_stop"]["reason"] == "worktree-mismatch"
    command("batch", "auto-report", "--batch", batch)
    assert before == ledger_bytes()
    assert command("batch", "auto-decide", "--batch", batch)["batch_state"] == "paused"
    command("ledger", "validate")
    # Simulate an installed runtime update while the original batch pin remains historical.
    (project / ".harness/orchestration/recovery_epoch.py").write_text(
        "REVISION = 662\n", encoding="utf-8"
    )
    recovered = command(
        "batch",
        "resume-stop",
        "--batch",
        batch,
        "--note",
        "fixed launch directory",
        *approved(),
    )
    upgrade = recovered["event"]["evidence"]["runtime_upgrade"]
    assert upgrade["from"] != upgrade["to"] and upgrade["original"] == upgrade["from"]
    manual = True
    command("dispatch", "heartbeat", "--dispatch", old["dispatch_id"], fails=True)
    new = start(dispatch("developer"))
    assert (
        new["dispatch_id"] != old["dispatch_id"]
        and new["snapshot_commit"] == old["snapshot_commit"]
    )
    assert new["carried_items"] == old["carried_items"]
    candidate = commit(2)
    writer_report(new, candidate)
    decision()
    command(
        "risk",
        "assess",
        "--batch",
        batch,
        "--candidate-commit",
        candidate,
        "--changed-file",
        "service.py",
        "--developer-trigger",
        "transactions",
    )
    review(candidate)
    decision()
    qa = dispatch("qa", candidate)
    command("qa", "run", "--dispatch", qa["dispatch_id"])
    decision()
    head = git("rev-parse", "HEAD", cwd=worktree)
    command(
        "batch",
        "rewind",
        "--batch",
        batch,
        "--to",
        "code-review",
        "--note",
        "repeat independent review",
        *approved(),
    )
    assert git("rev-parse", "HEAD", cwd=worktree) == head
    command("ledger", "validate")
    command(
        "dispatch",
        "propose",
        "--batch",
        batch,
        "--role",
        "developer",
        "--purpose",
        "publish",
        "--candidate-commit",
        candidate,
        fails=True,
    )
    review(candidate)
    decision()
    qa = dispatch("qa", candidate)
    command("qa", "run", "--dispatch", qa["dispatch_id"])
    decision()
    publisher = dispatch("developer", candidate, "publish")
    command("dispatch", "publish", "--dispatch", publisher["dispatch_id"])
    decision()
    assert (
        command("batch", "list", "--ticket", "#662")["batches"][0]["state"]
        == "completed"
    )
    command(
        "batch",
        "rewind",
        "--batch",
        batch,
        "--to",
        "developer",
        "--note",
        "invalid terminal rewind",
        *approved(),
        fails=True,
    )
    command("ledger", "validate")
    print("installed orchestration recovery cycle passed")
