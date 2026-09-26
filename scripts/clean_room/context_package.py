"""Context Package в ledger и gate свежести: сценарий clean-room из `scripts/test_clean_room.py`."""

import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

from scripts.clean_room.support import (
    capture,
    commit_map_for,
)


def run(ctx: SimpleNamespace) -> None:
    """Context Package в ledger и gate свежести.

    Читает из контекста: `coordinator_run`, `fake_adapter`, `lifecycle_records`,
    `orchestration_project`, `shared_worktree`, `staged_payload`, `sync_origin_base`, `test_root`.
    """
    coordinator_run = ctx.coordinator_run
    fake_adapter = ctx.fake_adapter
    lifecycle_records = ctx.lifecycle_records
    orchestration_project = ctx.orchestration_project
    shared_worktree = ctx.shared_worktree
    staged_payload = ctx.staged_payload
    sync_origin_base = ctx.sync_origin_base
    test_root = ctx.test_root
    # Context Package: ledger integration and freshness gate (Issue #138).  The
    # coordinator registers a built package as its own immutable, hashed ledger record -- the same
    # ownership pattern as a risk assessment -- and checks its freshness before every new dispatch
    # brief.  A package is shared across role sessions when the pinned base/candidate is unchanged.
    ctxpkg_state = test_root / "context-package-state"
    subprocess.run(
        ["git", "branch", "feature/issue-138-context-package"],
        cwd=orchestration_project,
        check=True,
    )
    sync_origin_base()
    ctxpkg_batch = json.loads(
        coordinator_run(
            "--state-dir",
            str(ctxpkg_state),
            "batch",
            "create",
            "--ticket",
            "#138",
            "--branch",
            "feature/issue-138-context-package",
            "--worktree",
            str(shared_worktree),
            "--zone",
            "backend",
            "--definition-of-done",
            "register a context package",
            "--prohibited-change",
            "do not merge",
        ).stdout
    )["batch_id"]
    coordinator_run(
        "--state-dir",
        str(ctxpkg_state),
        "batch",
        "approve",
        "--batch",
        ctxpkg_batch,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T15:20:00Z",
    )

    def ctxpkg_role(role_name, model, approved_at, payload_extra):
        created = coordinator_run(
            "--state-dir",
            str(ctxpkg_state),
            "dispatch",
            "create",
            "--batch",
            ctxpkg_batch,
            "--role",
            role_name,
            "--approved-by",
            "project coordinator",
            "--approved-at",
            approved_at,
        )
        if created.returncode != 0:
            sys.exit(
                f"coordinator rejected the context-package {role_name} dispatch: "
                + created.stderr
            )
        role_record = json.loads(created.stdout)
        role_dispatch = role_record["dispatch_id"]
        coordinator_run(
            "--state-dir",
            str(ctxpkg_state),
            "dispatch",
            "send",
            "--dispatch",
            role_dispatch,
            "--adapter",
            str(fake_adapter),
        )
        coordinator_run(
            "--state-dir",
            str(ctxpkg_state),
            "dispatch",
            "self-report",
            "--dispatch",
            role_dispatch,
            "--model",
            model,
        )
        payload = {
            "dispatch_id": role_dispatch,
            "ticket": "#138",
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
        payload_file = staged_payload(
            f"context-package-{role_name}-{role_dispatch}.json"
        )
        payload_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        submitted = coordinator_run(
            "--state-dir",
            str(ctxpkg_state),
            "report",
            "submit",
            "--file",
            str(payload_file),
        )
        if submitted.returncode != 0:
            sys.exit(
                f"coordinator rejected the context-package {role_name} report: "
                + submitted.stderr
            )
        return role_dispatch

    ctxpkg_role("architect", "project-architect-model", "2026-09-09T15:20:10Z", {})
    coordinator_run(
        "--state-dir",
        str(ctxpkg_state),
        "batch",
        "decide",
        "--batch",
        ctxpkg_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T15:20:20Z",
    )

    ctxpkg_file = orchestration_project / "services" / "context_package_demo.py"
    ctxpkg_file.write_text(
        'def demo():\n    return "context-package"\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "services/context_package_demo.py"],
        cwd=orchestration_project,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "feat: add context package demo"],
        cwd=orchestration_project,
        check=True,
    )
    ctxpkg_first_sha = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()
    ctxpkg_role(
        "developer",
        "project-developer-model",
        "2026-09-09T15:20:30Z",
        {
            "commit_sha": ctxpkg_first_sha,
            "changed_files": ["services/context_package_demo.py"],
        },
    )
    coordinator_run(
        "--state-dir",
        str(ctxpkg_state),
        "batch",
        "decide",
        "--batch",
        ctxpkg_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T15:20:40Z",
    )
    first_ctxpkg_risk = coordinator_run(
        "--state-dir",
        str(ctxpkg_state),
        "risk",
        "assess",
        "--batch",
        ctxpkg_batch,
        "--candidate-commit",
        ctxpkg_first_sha,
        "--changed-file",
        "services/context_package_demo.py",
    )
    if first_ctxpkg_risk.returncode != 0:
        sys.exit(
            "coordinator rejected the context-package fixture's first risk assessment: "
            + first_ctxpkg_risk.stderr
        )

    ctxpkg_batch_before = json.loads(
        (
            lifecycle_records(ctxpkg_state) / "batches" / f"{ctxpkg_batch}.json"
        ).read_text(encoding="utf-8")
    )
    ctxpkg_register = coordinator_run(
        "--state-dir",
        str(ctxpkg_state),
        "context-package",
        "register",
        "--batch",
        ctxpkg_batch,
        "--candidate-commit",
        ctxpkg_first_sha,
        "--min-starting-files",
        "1",
    )
    if ctxpkg_register.returncode != 0:
        sys.exit(
            "coordinator rejected a valid context package registration: "
            + ctxpkg_register.stderr
        )
    ctxpkg_record = json.loads(ctxpkg_register.stdout)
    if (
        ctxpkg_record["base_commit"] != ctxpkg_batch_before["base_commit"]
        or ctxpkg_record["candidate_commit"] != ctxpkg_first_sha
    ):
        sys.exit(
            "context package did not pin the batch base and the accepted developer candidate"
        )
    if "services/context_package_demo.py" not in ctxpkg_record["file_hashes"]:
        sys.exit(
            "context package did not include the changed file among its file hashes"
        )
    ctxpkg_record_path = (
        lifecycle_records(ctxpkg_state)
        / "context-packages"
        / f"{ctxpkg_record['context_package_id']}.json"
    )
    if not ctxpkg_record_path.is_file():
        sys.exit("context package was not persisted as its own immutable ledger record")
    ctxpkg_batch_after = json.loads(
        (
            lifecycle_records(ctxpkg_state) / "batches" / f"{ctxpkg_batch}.json"
        ).read_text(encoding="utf-8")
    )
    ctxpkg_pointer = next(
        (
            item
            for item in ctxpkg_batch_after.get("context_packages", [])
            if item["context_package_id"] == ctxpkg_record["context_package_id"]
        ),
        None,
    )
    ctxpkg_expected_sha256 = hashlib.sha256(
        (
            json.dumps(ctxpkg_record, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    if (
        ctxpkg_pointer is None
        or ctxpkg_pointer["record_sha256"] != ctxpkg_expected_sha256
    ):
        sys.exit(
            "batch did not record an immutable hashed pointer to the context package"
        )

    first_ctxpkg_review = coordinator_run(
        "--state-dir",
        str(ctxpkg_state),
        "dispatch",
        "create",
        "--batch",
        ctxpkg_batch,
        "--role",
        "code-review",
        "--candidate-commit",
        ctxpkg_first_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T15:20:50Z",
    )
    if first_ctxpkg_review.returncode != 0:
        sys.exit(
            "coordinator rejected the context-package fixture's first review dispatch: "
            + first_ctxpkg_review.stderr
        )
    first_ctxpkg_review_record = json.loads(first_ctxpkg_review.stdout)
    if (
        first_ctxpkg_review_record["brief"].get("context_package_id")
        != ctxpkg_record["context_package_id"]
    ):
        sys.exit("review did not reuse the matching shared Context Package")
    summary = first_ctxpkg_review_record["brief"].get("context_package_summary")
    if not isinstance(summary, dict) or "services/context_package_demo.py" not in {
        item.get("path") for item in summary.get("starting_files", [])
    }:
        sys.exit(
            "review brief did not receive the compact shared Context Package summary"
        )
    if (
        first_ctxpkg_review_record.get("context_package_freshness", {}).get("status")
        != "fresh"
    ):
        sys.exit(
            "coordinator did not record a fresh context package before a matching dispatch: "
            + json.dumps(first_ctxpkg_review_record)
        )
    first_ctxpkg_review_id = first_ctxpkg_review_record["dispatch_id"]
    if (
        coordinator_run(
            "--state-dir",
            str(ctxpkg_state),
            "dispatch",
            "send",
            "--dispatch",
            first_ctxpkg_review_id,
            "--adapter",
            str(fake_adapter),
            "--checkout",
            str(orchestration_project),
        ).returncode
        != 0
    ):
        sys.exit(
            "coordinator rejected the context-package fixture's first review checkout"
        )
    coordinator_run(
        "--state-dir",
        str(ctxpkg_state),
        "dispatch",
        "self-report",
        "--dispatch",
        first_ctxpkg_review_id,
        "--model",
        "project-code-review-model",
    )
    first_ctxpkg_review_payload = {
        "dispatch_id": first_ctxpkg_review_id,
        "ticket": "#138",
        "role": "code-review",
        "outcome": "completed",
        "output": "independent composite review completed",
        "commit_sha": "not applicable — read-only role",
        "changed_files": [],
        "checks_run": [
            {
                "command": f"{sys.executable} qa_baseline.py",
                "result": "pass",
                "evidence": "review scope inspected",
            }
        ],
        "risks": "standards warning requires coordinator decision",
        "blockers": "none",
        "next_coordinator_action": "override-warning or retry",
        "report_language": "ru",
        "review": {
            "candidate_commit": ctxpkg_first_sha,
            "scope": ["services/context_package_demo.py"],
            "standards": {
                "severity": "warning",
                "findings": [
                    {
                        "severity": "warning",
                        "evidence": "missing docstring",
                        "summary": "style warning",
                    }
                ],
                "risks": "style risk",
                "blockers": "none",
            },
            "spec": {
                "severity": "clean",
                "findings": [],
                "risks": "none",
                "blockers": "none",
            },
        },
    }
    first_ctxpkg_review_file = staged_payload(
        f"context-package-first-review-{first_ctxpkg_review_id}.json"
    )
    first_ctxpkg_review_file.write_text(
        json.dumps(first_ctxpkg_review_payload, indent=2) + "\n", encoding="utf-8"
    )
    if (
        coordinator_run(
            "--state-dir",
            str(ctxpkg_state),
            "report",
            "submit",
            "--file",
            str(first_ctxpkg_review_file),
        ).returncode
        != 0
    ):
        sys.exit(
            "coordinator rejected the context-package fixture's first review report"
        )
    if (
        coordinator_run(
            "--state-dir",
            str(ctxpkg_state),
            "batch",
            "decide",
            "--batch",
            ctxpkg_batch,
            "--decision",
            "retry",
            "--approved-by",
            "project coordinator",
            "--approved-at",
            "2026-09-09T15:21:00Z",
        ).returncode
        != 0
    ):
        sys.exit(
            "coordinator rejected a retry decision on the context-package fixture's review warning"
        )

    ctxpkg_second_file = (
        orchestration_project / "services" / "context_package_demo_v2.py"
    )
    ctxpkg_second_file.write_text(
        'def demo_v2():\n    return "context-package-v2"\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "services/context_package_demo_v2.py"],
        cwd=orchestration_project,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "feat: add second context package demo"],
        cwd=orchestration_project,
        check=True,
    )
    ctxpkg_second_sha = capture(
        ["git", "-C", str(orchestration_project), "rev-parse", "HEAD"]
    ).strip()
    ctxpkg_role(
        "developer",
        "project-developer-model",
        "2026-09-09T15:21:10Z",
        {
            "commit_sha": ctxpkg_second_sha,
            "changed_files": [
                "services/context_package_demo.py",
                "services/context_package_demo_v2.py",
            ],
        },
    )
    coordinator_run(
        "--state-dir",
        str(ctxpkg_state),
        "batch",
        "decide",
        "--batch",
        ctxpkg_batch,
        "--decision",
        "accept",
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T15:21:20Z",
    )
    second_ctxpkg_risk = coordinator_run(
        "--state-dir",
        str(ctxpkg_state),
        "risk",
        "assess",
        "--batch",
        ctxpkg_batch,
        "--candidate-commit",
        ctxpkg_second_sha,
        "--changed-file",
        "services/context_package_demo.py",
        "--changed-file",
        "services/context_package_demo_v2.py",
    )
    if second_ctxpkg_risk.returncode != 0:
        sys.exit(
            "coordinator rejected the context-package fixture's second risk assessment: "
            + second_ctxpkg_risk.stderr
        )

    # A new candidate receives one new immutable shared Context Package before review.
    second_ctxpkg_review = coordinator_run(
        "--state-dir",
        str(ctxpkg_state),
        "dispatch",
        "create",
        "--batch",
        ctxpkg_batch,
        "--role",
        "code-review",
        "--candidate-commit",
        ctxpkg_second_sha,
        "--approved-by",
        "project coordinator",
        "--approved-at",
        "2026-09-09T15:21:30Z",
    )
    if second_ctxpkg_review.returncode != 0:
        sys.exit(
            "coordinator could not refresh a shared context package before the review: "
            + second_ctxpkg_review.stderr
        )
    second_ctxpkg_review_record = json.loads(second_ctxpkg_review.stdout)
    second_ctxpkg_freshness = second_ctxpkg_review_record.get(
        "context_package_freshness"
    )
    if not second_ctxpkg_freshness or second_ctxpkg_freshness.get("status") != "fresh":
        sys.exit(
            "coordinator did not refresh the Context Package before the review dispatch: "
            + json.dumps(second_ctxpkg_review_record)
        )
    if second_ctxpkg_freshness.get("registered_candidate_commit") != ctxpkg_second_sha:
        sys.exit("refreshed context package did not pin the new candidate")
    if second_ctxpkg_freshness.get("current_candidate_commit") != ctxpkg_second_sha:
        sys.exit(
            "stale context package evidence did not reference the current accepted developer candidate"
        )
    ctxpkg_final_batch = json.loads(
        (
            lifecycle_records(ctxpkg_state) / "batches" / f"{ctxpkg_batch}.json"
        ).read_text(encoding="utf-8")
    )
    final_package_pointer = next(
        entry
        for entry in ctxpkg_final_batch["context_packages"]
        if entry["context_package_id"]
        == second_ctxpkg_review_record["brief"]["context_package_id"]
    )
    final_package = json.loads(
        (
            lifecycle_records(ctxpkg_state)
            / "context-packages"
            / f"{final_package_pointer['context_package_id']}.json"
        ).read_text(encoding="utf-8")
    )
    if (
        final_package.get("role") != "shared"
        or final_package.get("estimated_tokens", 0) < 1
    ):
        sys.exit(
            "review brief did not receive a token-estimated shared context package"
        )

    print("context package ledger integration and freshness-gate verification passed")
