"""Hooks целевого проекта: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.clean_room.support import (
    BASH,
    HARNESS,
    run_hook,
    run_ok,
)


def run(ctx: SimpleNamespace) -> None:
    """Hooks целевого проекта.

    Читает из контекста: `pv_project`, `test_root`.
    """
    pv_project = ctx.pv_project
    test_root = ctx.test_root
    project_json = pv_project / ".harness" / "project.json"
    if not project_json.is_file():
        sys.exit("pvmalove-suite init did not scaffold .harness/project.json")
    if not (pv_project / ".harness" / "project.schema.json").is_file():
        sys.exit("pvmalove-suite init did not scaffold .harness/project.schema.json")

    hook = pv_project / ".claude" / "hooks" / "check-branch-name.sh"
    if not hook.is_file():
        sys.exit("pvmalove-suite init did not scaffold check-branch-name.sh")
    metadata_hook = pv_project / ".claude" / "hooks" / "block-public-attribution.sh"
    if not metadata_hook.is_file():
        sys.exit("pvmalove-suite init did not scaffold block-public-attribution.sh")
    scratch_hook = (
        pv_project / ".claude" / "hooks" / "block-scratch-outside-docs-tasks.sh"
    )
    if not scratch_hook.is_file():
        sys.exit(
            "pvmalove-suite init did not scaffold block-scratch-outside-docs-tasks.sh"
        )
    bounded_hook = pv_project / ".claude" / "hooks" / "require-bounded-check.sh"
    if not bounded_hook.is_file():
        sys.exit("pvmalove-suite init did not scaffold require-bounded-check.sh")

    # A PR launched from the primary checkout can explicitly target a tested linked worktree.
    # Its QA marker must belong to that checkout, even when the primary checkout is dirty.
    subprocess.run(["git", "add", "-A"], cwd=pv_project, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "test fixture"],
        cwd=pv_project,
        check=True,
    )
    linked = test_root / "linked"
    linked_branch = "feature/issue-373-linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", linked_branch, str(linked)],
        cwd=pv_project,
        check=True,
    )
    require_gate = pv_project / ".claude" / "hooks" / "require-qa-gate.sh"
    record_gate = pv_project / ".claude" / "hooks" / "record-qa-gate-pass.sh"
    mark_gate = pv_project / ".claude" / "hooks" / "mark-qa-gate-passed.sh"
    project_json.write_text(project_json.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    pr_command = f"gh pr create --head {linked_branch} --body-file pr-body.md"
    pr_payload = json.dumps({"cwd": str(pv_project), "tool_input": {"command": pr_command}})
    if run_hook(record_gate, pv_project, "", cwd=pv_project).returncode:
        sys.exit("could not record primary checkout QA marker")
    if run_hook(require_gate, pv_project, "", raw_payload=pr_payload).returncode == 0:
        sys.exit("primary checkout QA marker opened a linked-worktree PR")
    if run_hook(record_gate, pv_project, "", cwd=linked).returncode:
        sys.exit("could not record linked-worktree QA marker")
    if run_hook(require_gate, pv_project, "", raw_payload=pr_payload).returncode:
        sys.exit("linked-worktree QA marker did not permit its PR")
    local_pr_payload = json.dumps(
        {"cwd": str(linked), "tool_input": {"command": "gh pr create --body-file pr-body.md"}}
    )
    if run_hook(require_gate, pv_project, "", raw_payload=local_pr_payload).returncode:
        sys.exit("QA marker did not permit PR from the linked checkout cwd")
    unknown_pr_payload = json.dumps(
        {"cwd": str(linked), "tool_input": {"command": "gh pr create --head feature/issue-999-missing"}}
    )
    if run_hook(require_gate, pv_project, "", raw_payload=unknown_pr_payload).returncode == 0:
        sys.exit("unknown PR head reused a linked-worktree QA marker")
    (linked / ".claude" / ".qa-gate" / "passed").unlink()
    mark_payload = json.dumps({"cwd": str(linked), "tool_input": {"command": "echo test"}})
    if run_hook(mark_gate, pv_project, "", raw_payload=mark_payload).returncode:
        sys.exit("could not mark linked-worktree QA pass")
    if run_hook(require_gate, pv_project, "", raw_payload=pr_payload).returncode:
        sys.exit("fallback QA marker did not permit linked-worktree PR")
    linked_config = linked / ".harness" / "project.json"
    linked_config.write_text(linked_config.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    if run_hook(require_gate, pv_project, "", raw_payload=pr_payload).returncode == 0:
        sys.exit("stale linked-worktree QA marker opened a PR")

    scratch_gitignore = pv_project / ".harness" / "scratch" / ".gitignore"
    if not scratch_gitignore.is_file():
        sys.exit("pvmalove-suite init did not scaffold .harness/scratch/.gitignore")
    if scratch_gitignore.read_text(encoding="utf-8") != "*\n!.gitignore\n":
        sys.exit(
            "pvmalove-suite init scaffolded .harness/scratch/.gitignore with unexpected content"
        )

    root_gitignore = pv_project / ".gitignore"
    if not root_gitignore.is_file():
        sys.exit("pvmalove-suite init did not create a root .gitignore")
    if "/docs/tasks/" not in root_gitignore.read_text(encoding="utf-8").splitlines():
        sys.exit("pvmalove-suite init did not add /docs/tasks/ to the root .gitignore")

    # Retrofit path: emulate a project harnessed before these two protections existed by
    # removing them, then confirm a plain `update` restores both without disturbing the
    # rest of the root .gitignore, and that re-running it does not duplicate the line.
    scratch_gitignore.unlink()
    other_gitignore_lines = [
        line
        for line in root_gitignore.read_text(encoding="utf-8").splitlines()
        if line != "/docs/tasks/"
    ]
    root_gitignore.write_text(
        "\n".join(other_gitignore_lines) + ("\n" if other_gitignore_lines else ""),
        encoding="utf-8",
        newline="\n",
    )
    run_ok(HARNESS + ["update", str(pv_project)])
    if not scratch_gitignore.is_file():
        sys.exit(
            "update did not retrofit .harness/scratch/.gitignore onto an already-installed project"
        )
    updated_gitignore_lines = root_gitignore.read_text(encoding="utf-8").splitlines()
    if "/docs/tasks/" not in updated_gitignore_lines:
        sys.exit(
            "update did not retrofit /docs/tasks/ into an already-installed project's root .gitignore"
        )
    for line in other_gitignore_lines:
        if line not in updated_gitignore_lines:
            sys.exit(
                f"update retrofit of /docs/tasks/ removed an unrelated root .gitignore line: {line}"
            )
    run_ok(HARNESS + ["update", str(pv_project)])
    if (
        root_gitignore.read_text(encoding="utf-8").splitlines().count("/docs/tasks/")
        != 1
    ):
        sys.exit(
            "repeated update runs duplicated the /docs/tasks/ line in the root .gitignore"
        )

    scratch_pr_body = pv_project / ".claude" / "tmp" / "pr-body-1-test.md"
    if (
        run_hook(
            scratch_hook,
            pv_project,
            "",
            raw_payload=json.dumps({"tool_input": {"file_path": str(scratch_pr_body)}}),
        ).returncode
        == 0
    ):
        sys.exit(
            "block-scratch-outside-docs-tasks.sh allowed a PR body in the retired .claude/tmp/ path"
        )
    windows_scratch_pr_body = r"C:\repo\.claude\tmp\pr-body-1-test.md"
    if (
        run_hook(
            scratch_hook,
            pv_project,
            "",
            raw_payload=json.dumps(
                {"tool_input": {"file_path": windows_scratch_pr_body}}
            ),
        ).returncode
        == 0
    ):
        sys.exit(
            "block-scratch-outside-docs-tasks.sh allowed an escaped Windows retired .claude\\tmp\\ path"
        )
    harness_scratch_pr_body = (
        pv_project / ".harness" / "scratch" / "tmp" / "pr-body-1-test.md"
    )
    if (
        run_hook(
            scratch_hook,
            pv_project,
            "",
            raw_payload=json.dumps(
                {"tool_input": {"file_path": str(harness_scratch_pr_body)}}
            ),
        ).returncode
        != 0
    ):
        sys.exit(
            "block-scratch-outside-docs-tasks.sh rejected a PR body in .harness/scratch/tmp/"
        )
    docs_pr_body = pv_project / "docs" / "tasks" / "pr-body-1-test.md"
    if (
        run_hook(
            scratch_hook,
            pv_project,
            "",
            raw_payload=json.dumps({"tool_input": {"file_path": str(docs_pr_body)}}),
        ).returncode
        == 0
    ):
        sys.exit("block-scratch-outside-docs-tasks.sh allowed a PR body in docs/tasks")
    root_pr_body = pv_project / "pr-body-1-test.md"
    if (
        run_hook(
            scratch_hook,
            pv_project,
            "",
            raw_payload=json.dumps({"tool_input": {"file_path": str(root_pr_body)}}),
        ).returncode
        == 0
    ):
        sys.exit(
            "block-scratch-outside-docs-tasks.sh allowed a PR body outside repository scratch"
        )

    # pv_project's qa_gate_commands (.harness/project.json) is ["echo test"] (--qa-gate-command
    # above), so that literal command is the project's own full-suite gate for this hook.
    if run_hook(bounded_hook, pv_project, "echo test").returncode == 0:
        sys.exit(
            "require-bounded-check.sh allowed a bare qa_gate_commands run without the bounded wrapper"
        )
    if (
        run_hook(
            bounded_hook,
            pv_project,
            "python .harness/skills/qa-gate/scripts/test_summary.py -- bash -lc 'echo test'",
        ).returncode
        != 0
    ):
        sys.exit(
            "require-bounded-check.sh blocked a qa_gate_commands run already going through test_summary.py"
        )
    if run_hook(bounded_hook, pv_project, "pytest").returncode == 0:
        sys.exit("require-bounded-check.sh allowed a bare full-suite pytest run")
    if run_hook(bounded_hook, pv_project, "python -m pytest").returncode == 0:
        sys.exit(
            "require-bounded-check.sh allowed a bare full-suite 'python -m pytest' run"
        )
    if run_hook(bounded_hook, pv_project, "python -m unittest").returncode == 0:
        sys.exit(
            "require-bounded-check.sh allowed a bare full-suite 'python -m unittest' run"
        )
    if (
        run_hook(
            bounded_hook,
            pv_project,
            "pytest tests/test_coordinator.py::CoordinatorLedgerMigrationTests::test_foo",
        ).returncode
        != 0
    ):
        sys.exit("require-bounded-check.sh blocked a point pytest node-id run")
    if (
        run_hook(
            bounded_hook,
            pv_project,
            "python -m unittest tests.test_coordinator.CoordinatorLedgerMigrationTests.test_foo",
        ).returncode
        != 0
    ):
        sys.exit("require-bounded-check.sh blocked a point unittest test-path run")
    if run_hook(bounded_hook, pv_project, "ls -la").returncode != 0:
        sys.exit("require-bounded-check.sh blocked an unrelated command")
    unharnessed = test_root / "unharnessed_project"
    unharnessed.mkdir(parents=True, exist_ok=True)
    if run_hook(bounded_hook, unharnessed, "echo test").returncode != 0:
        sys.exit(
            "require-bounded-check.sh fired in a project with no .harness/project.json"
        )

    if (
        run_hook(
            metadata_hook, pv_project, 'git commit -m "feat: document runtime behavior"'
        ).returncode
        != 0
    ):
        sys.exit("block-public-attribution.sh rejected a project-only commit message")
    if (
        run_hook(
            metadata_hook,
            pv_project,
            'git commit -m "feat: document runtime behavior"',
            raw_payload=json.dumps(
                {
                    "tool_input": {
                        "command": 'git commit -m "feat: document runtime behavior"'
                    },
                    "cwd": str(pv_project),
                    "session_id": "claude-agent-harness-session",
                }
            ),
        ).returncode
        != 0
    ):
        sys.exit(
            "block-public-attribution.sh read forbidden text outside tool_input.command"
        )
    if (
        run_hook(
            metadata_hook,
            pv_project,
            'git commit -m "chore: add Claude-Session trailer"',
        ).returncode
        == 0
    ):
        sys.exit("block-public-attribution.sh allowed forbidden commit attribution")
    if (
        run_hook(
            metadata_hook,
            pv_project,
            "",
            raw_payload='{not valid JSON with git commit -m "generated by Codex"}',
        ).returncode
        == 0
    ):
        sys.exit("block-public-attribution.sh allowed an invalid hook payload")
    git_bash_bin = Path(BASH).resolve().parent.parent / "usr" / "bin"
    if (
        sys.platform == "win32"
        and run_hook(
            metadata_hook,
            pv_project,
            'git commit -m "chore: generated by Codex"',
            env_overrides={"PATH": str(git_bash_bin)},
        ).returncode
        == 0
    ):
        sys.exit(
            "block-public-attribution.sh allowed forbidden metadata without Python"
        )
    pr_body = pv_project / "pr-body.md"
    pr_body.write_text("## Summary\nKeep the change focused.\n", encoding="utf-8")
    if (
        run_hook(
            metadata_hook, pv_project, f"gh pr create --body-file {pr_body}"
        ).returncode
        != 0
    ):
        sys.exit("block-public-attribution.sh rejected a project-only PR body")
    scratch_body = pv_project / ".claude" / "tmp" / "pr-body.md"
    scratch_body.parent.mkdir(parents=True, exist_ok=True)
    scratch_body.write_text("## Summary\nKeep the change focused.\n", encoding="utf-8")
    if (
        run_hook(
            metadata_hook, pv_project, f'gh pr create --body-file "{scratch_body}"'
        ).returncode
        != 0
    ):
        sys.exit(
            "block-public-attribution.sh treated a Claude scratch-file path as PR metadata"
        )
    pr_body.write_text(
        "## Summary\nhttps://claude.ai/code/session/example\n", encoding="utf-8"
    )
    if (
        run_hook(
            metadata_hook, pv_project, f"gh pr create --body-file {pr_body}"
        ).returncode
        == 0
    ):
        sys.exit("block-public-attribution.sh allowed forbidden PR attribution")
    if (
        run_hook(
            metadata_hook, pv_project, f"gh pr create --body-file '{pr_body}'"
        ).returncode
        == 0
    ):
        sys.exit(
            "block-public-attribution.sh allowed forbidden attribution in a single-quoted PR body file"
        )
    if (
        run_hook(
            metadata_hook, pv_project, 'gh pr create --body-file "$pr_body"'
        ).returncode
        == 0
    ):
        sys.exit(
            "block-public-attribution.sh allowed an unreadable PR body-file expression"
        )

    # .harness/project.json is present, with branch_pattern set above to a distinctive regex
    # that differs from the hook's own built-in default - prove the configured value, not the
    # default, is what actually gets enforced.
    if (
        run_hook(hook, pv_project, "git checkout -b feature/issue-1-test").returncode
        == 0
    ):
        sys.exit("check-branch-name.sh ignored the configured branch_pattern")
    result = run_hook(hook, pv_project, "git checkout -b release/rel-1-test")
    if result.returncode != 0:
        sys.exit(
            f"check-branch-name.sh rejected a branch matching the configured pattern: {result.stderr}"
        )
    result = run_hook(hook, pv_project, "git switch -c release/rel-2-test")
    if result.returncode != 0:
        sys.exit(
            f"check-branch-name.sh rejected a switch command matching the configured pattern: {result.stderr}"
        )

    # The direct-commit guard must protect both the configured base branch and every epic
    # integration branch, while allowing an issue branch to push normally.
    direct_hook = pv_project / ".claude" / "hooks" / "block-direct-master.sh"
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/rel-1-test"],
        cwd=pv_project,
        check=True,
    )
    if (
        run_hook(
            direct_hook, pv_project, "git push origin release/rel-1-test"
        ).returncode
        != 0
    ):
        sys.exit("block-direct-master.sh rejected a push from an issue branch")
    if run_hook(direct_hook, pv_project, "git push origin HEAD:main").returncode == 0:
        sys.exit("block-direct-master.sh allowed a push to base_branch")
    if (
        run_hook(
            direct_hook, pv_project, "git push origin HEAD:integration/payments"
        ).returncode
        == 0
    ):
        sys.exit("block-direct-master.sh allowed a push to an integration branch")

    # Remove .harness/project.json entirely - the hook must fall back to its own built-in
    # default pattern instead of crashing or blocking every branch name.
    saved_project_json = project_json.read_text(encoding="utf-8")
    project_json.unlink()
    if run_hook(hook, pv_project, "git checkout -b release/rel-1-test").returncode == 0:
        sys.exit(
            "check-branch-name.sh fallback unexpectedly accepted a non-default branch with no project.json"
        )
    result = run_hook(hook, pv_project, "git checkout -b feature/issue-1-test")
    if result.returncode != 0:
        sys.exit(
            f"check-branch-name.sh did not fall back to its default pattern: {result.stderr}"
        )
    project_json.write_text(saved_project_json, encoding="utf-8")
