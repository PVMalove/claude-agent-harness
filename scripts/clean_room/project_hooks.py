"""Hooks целевого проекта: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from harness.storage import storage_path
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
    dangerous_hook = pv_project / ".claude" / "hooks" / "block-dangerous-git.sh"
    if not dangerous_hook.is_file():
        sys.exit("pvmalove-suite init did not scaffold block-dangerous-git.sh")
    qa_gate_hook = pv_project / ".claude" / "hooks" / "require-qa-gate.sh"
    if not qa_gate_hook.is_file():
        sys.exit("pvmalove-suite init did not scaffold require-qa-gate.sh")
    merge_hook = pv_project / ".claude" / "hooks" / "block-pr-merge.sh"
    if not merge_hook.is_file():
        sys.exit("pvmalove-suite init did not scaffold block-pr-merge.sh")
    if not (pv_project / ".claude" / "hooks" / "pr_commands.py").is_file():
        sys.exit("pvmalove-suite init did not scaffold the pr_commands.py hook helper")

    # A PR launched from the primary checkout can explicitly target a tested linked worktree.
    # Its QA marker must belong to that checkout, even when the primary checkout is dirty.
    commit = [
        "git",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-q",
    ]
    subprocess.run(["git", "add", "-A"], cwd=pv_project, check=True)
    subprocess.run([*commit, "-m", "test fixture"], cwd=pv_project, check=True)
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
    project_json.write_text(
        project_json.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    pr_command = f"gh pr create --head {linked_branch} --body-file pr-body.md"
    pr_payload = json.dumps(
        {"cwd": str(pv_project), "tool_input": {"command": pr_command}}
    )
    if run_hook(record_gate, pv_project, "", cwd=pv_project).returncode:
        sys.exit("could not record primary checkout QA marker")
    if run_hook(require_gate, pv_project, "", raw_payload=pr_payload).returncode == 0:
        sys.exit("primary checkout QA marker opened a linked-worktree PR")
    if run_hook(record_gate, pv_project, "", cwd=linked).returncode:
        sys.exit("could not record linked-worktree QA marker")
    if run_hook(require_gate, pv_project, "", raw_payload=pr_payload).returncode:
        sys.exit("linked-worktree QA marker did not permit its PR")
    local_pr_payload = json.dumps(
        {
            "cwd": str(linked),
            "tool_input": {"command": "gh pr create --body-file pr-body.md"},
        }
    )
    if run_hook(require_gate, pv_project, "", raw_payload=local_pr_payload).returncode:
        sys.exit("QA marker did not permit PR from the linked checkout cwd")
    unknown_pr_payload = json.dumps(
        {
            "cwd": str(linked),
            "tool_input": {"command": "gh pr create --head feature/issue-999-missing"},
        }
    )
    if (
        run_hook(
            require_gate, pv_project, "", raw_payload=unknown_pr_payload
        ).returncode
        == 0
    ):
        sys.exit("unknown PR head reused a linked-worktree QA marker")
    heads_payload = json.dumps(
        {
            "cwd": str(linked),
            "tool_input": {"command": "git ls-remote --heads origin foo"},
        }
    )
    for gate in (mark_gate, require_gate):
        if run_hook(gate, pv_project, "", raw_payload=heads_payload).returncode:
            sys.exit(f"{gate.name} treated --heads of a non-PR command as a PR head")
    head_eq_payload = json.dumps(
        {
            "cwd": str(pv_project),
            "tool_input": {"command": f"gh pr create --head={linked_branch}"},
        }
    )
    if run_hook(require_gate, pv_project, "", raw_payload=head_eq_payload).returncode:
        sys.exit("gh pr create --head=<branch> did not resolve its linked checkout")
    (linked / ".claude" / ".qa-gate" / "passed").unlink()
    mark_payload = json.dumps(
        {"cwd": str(linked), "tool_input": {"command": "echo test"}}
    )
    if run_hook(mark_gate, pv_project, "", raw_payload=mark_payload).returncode:
        sys.exit("could not mark linked-worktree QA pass")
    if run_hook(require_gate, pv_project, "", raw_payload=pr_payload).returncode:
        sys.exit("fallback QA marker did not permit linked-worktree PR")
    linked_config = linked / ".harness" / "project.json"
    linked_config.write_text(
        linked_config.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    if run_hook(require_gate, pv_project, "", raw_payload=pr_payload).returncode == 0:
        sys.exit("stale linked-worktree QA marker opened a PR")

    # The gitignored .harness/ is absent from a real linked worktree: mark falls back to
    # the project root config, and /to-pull-requests records coordinator-accepted QA there.
    no_harness = test_root / "no-harness"
    no_harness_branch = "feature/issue-463-no-harness"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", no_harness_branch, str(no_harness)],
        cwd=pv_project,
        check=True,
    )
    subprocess.run(["git", "rm", "-rq", ".harness"], cwd=no_harness, check=True)
    subprocess.run([*commit, "-m", "drop tracked .harness"], cwd=no_harness, check=True)
    no_harness_marker = no_harness / ".claude" / ".qa-gate" / "passed"
    no_harness_pr_payload = json.dumps(
        {
            "cwd": str(pv_project),
            "tool_input": {"command": f"gh pr create --head {no_harness_branch}"},
        }
    )

    def mark_no_harness(
        command: str, project_dir: Path = pv_project
    ) -> subprocess.CompletedProcess[str]:
        """Запустить PostToolUse-hook mark для Bash-команды в worktree без `.harness/`."""
        mark_input = {"cwd": str(no_harness), "tool_input": {"command": command}}
        return run_hook(mark_gate, project_dir, "", raw_payload=json.dumps(mark_input))

    def no_harness_pr_allowed() -> bool:
        """Проверить, пропускает ли require-qa-gate.sh PR ветки worktree без `.harness/`."""
        return not run_hook(
            require_gate, pv_project, "", raw_payload=no_harness_pr_payload
        ).returncode

    if no_harness_pr_allowed():
        sys.exit("PR without a QA marker opened from a worktree without .harness")
    for command in ("git status", "echo test"):
        result = mark_no_harness(command)
        if result.returncode:
            sys.exit(f"mark failed in a worktree without .harness: {result.stderr}")
        if no_harness_marker.is_file() != (command == "echo test"):
            sys.exit(f"mark ignored the project root config for {command!r}")
    if not no_harness_pr_allowed():
        sys.exit("root-config QA marker did not permit its PR")
    subprocess.run(
        [*commit, "--allow-empty", "-m", "move HEAD"], cwd=no_harness, check=True
    )
    if no_harness_pr_allowed():
        sys.exit("QA marker for a previous HEAD opened a PR")
    # /to-pull-requests: accepted coordinator QA evidence is recorded without a rerun.
    if run_hook(record_gate, pv_project, "", cwd=no_harness).returncode:
        sys.exit("could not record coordinator-accepted QA in the PR checkout")
    if not no_harness_pr_allowed():
        sys.exit("recorded coordinator-accepted QA did not permit its PR")
    tracked = no_harness / "AGENTS.md"
    tracked.write_text(tracked.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    if no_harness_pr_allowed():
        sys.exit("QA marker for a previous diff opened a PR")
    subprocess.run(["git", "checkout", "-q", "--", "AGENTS.md"], cwd=no_harness, check=True)
    no_harness_marker.unlink()
    # A session started inside the linked worktree points CLAUDE_PROJECT_DIR at it too:
    # mark then reads the main worktree's config.
    if mark_no_harness("echo test", no_harness).returncode or not no_harness_marker.is_file():
        sys.exit("mark ignored the main worktree config inside a linked-worktree session")
    no_harness_marker.unlink()
    hidden_json = project_json.with_name("project.json.hidden")
    project_json.rename(hidden_json)
    try:
        orphan_mark = mark_no_harness("echo test")
    finally:
        hidden_json.rename(project_json)
    if orphan_mark.returncode or no_harness_marker.is_file():
        sys.exit("mark without any project config did not exit quietly")
    # A checkout's own config, when present, takes precedence over the project root.
    local_config = no_harness / ".harness" / "project.json"
    local_config.parent.mkdir()
    local_config.write_text('{"qa_gate_commands": ["echo local"]}\n', encoding="utf-8")
    for command in ("echo test", "echo local"):
        if mark_no_harness(command).returncode:
            sys.exit(f"mark failed with a checkout config for {command!r}")
        if no_harness_marker.is_file() != (command == "echo local"):
            sys.exit(f"mark ignored the checkout config for {command!r}")

    scratch_gitignore = storage_path(pv_project, "scratch", ".gitignore")
    if not scratch_gitignore.is_file():
        sys.exit(
            "pvmalove-suite init did not scaffold .harness/.sandboxes/scratch/.gitignore"
        )
    if scratch_gitignore.read_text(encoding="utf-8") != "*\n!.gitignore\n":
        sys.exit(
            "pvmalove-suite init scaffolded .harness/.sandboxes/scratch/.gitignore with unexpected content"
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
            "update did not retrofit .harness/.sandboxes/scratch/.gitignore onto an already-installed project"
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
        == 0
    ):
        sys.exit(
            "block-scratch-outside-docs-tasks.sh allowed a PR body in the retired .harness/scratch/tmp/ path"
        )
    old_sandboxes_pr_body = (
        pv_project / ".harness" / ".sandboxes" / "scratch" / "tmp" / "pr-body-1-test.md"
    )
    if (
        run_hook(
            scratch_hook,
            pv_project,
            "",
            raw_payload=json.dumps(
                {"tool_input": {"file_path": str(old_sandboxes_pr_body)}}
            ),
        ).returncode
        == 0
    ):
        sys.exit(
            "block-scratch-outside-docs-tasks.sh allowed a PR body in .harness/.sandboxes/scratch/tmp/"
        )
    sandboxes_pr_body = storage_path(pv_project, "pr_body", "pr-body-1-test.md")
    for name in (
        "pr-body-1-test.md",
        "pr-comment-1-test.md",
        "issue-comment-1-test.md",
    ):
        publication_file = sandboxes_pr_body.parent / name
        if (
            run_hook(
                scratch_hook,
                pv_project,
                "",
                raw_payload=json.dumps(
                    {"tool_input": {"file_path": str(publication_file)}}
                ),
            ).returncode
            != 0
        ):
            sys.exit(f"block-scratch-outside-docs-tasks.sh rejected {name} in pr_body/")
    nested_non_body = sandboxes_pr_body.parent / "pr-body-dir" / "notes.md"
    if (
        run_hook(
            scratch_hook,
            pv_project,
            "",
            raw_payload=json.dumps({"tool_input": {"file_path": str(nested_non_body)}}),
        ).returncode
        == 0
    ):
        sys.exit(
            "block-scratch-outside-docs-tasks.sh accepted a filename without a body marker"
        )
    escaped_body = (
        pv_project.parent
        / "outside"
        / ".harness"
        / ".sandboxes"
        / "pr_body"
        / "pr-body-1.md"
    )
    escaped_result = run_hook(
        scratch_hook,
        pv_project,
        "",
        raw_payload=json.dumps({"tool_input": {"file_path": str(escaped_body)}}),
    )
    if escaped_result.returncode == 0:
        sys.exit(
            "block-scratch-outside-docs-tasks.sh accepted a path outside the project"
        )
    if (
        "вне проекта" not in escaped_result.stderr
        or "payload" in escaped_result.stderr
    ):
        sys.exit(
            "block-scratch-outside-docs-tasks.sh did not name the out-of-project path "
            f"as the reason: {escaped_result.stderr!r}"
        )
    # The runtime's own memory directory (<config>/projects/<slug>/memory/) lives outside the
    # project but is still writable; any other path under the runtime config stays blocked.
    runtime_home = test_root / "runtime-home"
    runtime_config = test_root / "runtime-config"
    home_env = {"HOME": str(runtime_home), "CLAUDE_CONFIG_DIR": ""}
    config_env = {"HOME": str(runtime_home), "CLAUDE_CONFIG_DIR": str(runtime_config)}
    home_projects = runtime_home / ".claude" / "projects" / "-repo"
    for file_path, env, allowed in (
        (home_projects / "memory" / "feedback.md", home_env, True),
        (runtime_config / "projects" / "-repo" / "memory" / "MEMORY.md", config_env, True),
        (home_projects / "notes.md", home_env, False),
        (runtime_home / ".claude" / "settings.json", home_env, False),
        (home_projects / "memory" / "feedback.md", config_env, False),
    ):
        result = run_hook(
            scratch_hook,
            pv_project,
            "",
            raw_payload=json.dumps({"tool_input": {"file_path": str(file_path)}}),
            env_overrides=env,
        )
        if (result.returncode == 0) != allowed:
            sys.exit(
                "block-scratch-outside-docs-tasks.sh "
                f"{'blocked' if allowed else 'allowed'} {file_path} with "
                f"CLAUDE_CONFIG_DIR={env['CLAUDE_CONFIG_DIR']!r}: {result.stderr!r}"
            )
    sandboxes_cache_path = (
        pv_project
        / ".harness"
        / ".sandboxes"
        / "cache"
        / "repo_map"
        / "results"
        / "test.json"
    )
    if (
        run_hook(
            scratch_hook,
            pv_project,
            "",
            raw_payload=json.dumps(
                {"tool_input": {"file_path": str(sandboxes_cache_path)}}
            ),
        ).returncode
        != 0
    ):
        sys.exit(
            "block-scratch-outside-docs-tasks.sh rejected a cache path in .harness/.sandboxes/cache/"
        )
    sandboxes_invalid_scratch = sandboxes_pr_body.parent / "notes.md"
    if (
        run_hook(
            scratch_hook,
            pv_project,
            "",
            raw_payload=json.dumps(
                {"tool_input": {"file_path": str(sandboxes_invalid_scratch)}}
            ),
        ).returncode
        == 0
    ):
        sys.exit(
            "block-scratch-outside-docs-tasks.sh allowed a non-PR file in .harness/.sandboxes/pr_body/"
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
    for command in (
        "sed -n 1,40p pytest.ini",
        "grep -n 'python -m unittest' .github/workflows/ci.yml",
        "rg -n 'echo test' .harness/project.json",
        "cat .github/workflows/ci.yml | grep pytest",
        "head -20 pytest.ini",
        "cat pytest.ini # don't run it here",
        "cat > notes.md <<'EOF'\nRun pytest before the PR; don't skip it.\nEOF",
    ):
        if run_hook(bounded_hook, pv_project, command).returncode != 0:
            sys.exit(
                f"require-bounded-check.sh blocked a command that only mentions a check: {command!r}"
            )
    for command in (
        "cd sub && pytest -q",
        "pytest | tail -5",
        "bash -lc 'echo test'",
        "uv run pytest",
        "FOO=1 python -m pytest",
        "cat notes.md\npytest",
        "pytest # it's fine",
        "timeout 600 bash -c 'pytest -q'",
        "find tests -name '*.py' -exec pytest {} +",
        "git bisect run pytest",
        'echo "$(pytest)"',
        "eval 'pytest -q'",
        "bash <<'EOF'\npytest\nEOF",
    ):
        if run_hook(bounded_hook, pv_project, command).returncode == 0:
            sys.exit(
                f"require-bounded-check.sh allowed a bare full-suite run: {command!r}"
            )
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

    escaped_clean_commit = json.dumps(
        {"tool_input": {"command": 'git commit -m "feat: parse \\"complex\\" json"'}}
    )
    if (
        run_hook(
            metadata_hook, pv_project, "", raw_payload=escaped_clean_commit
        ).returncode
        != 0
    ):
        sys.exit(
            "block-public-attribution.sh rejected a clean commit with escaped quotes"
        )
    multiline_clean_payload = json.dumps(
        {"tool_input": {"command": 'git commit -m "feat: valid commit message"'}},
        indent=2,
    )
    if (
        run_hook(
            metadata_hook, pv_project, "", raw_payload=multiline_clean_payload
        ).returncode
        != 0
    ):
        sys.exit("block-public-attribution.sh rejected a multiline JSON payload")
    multiline_attribution_payload = json.dumps(
        {
            "tool_input": {
                "command": 'git commit -m "feat: line 1\n\nCo-Authored-By: Claude <noreply@anthropic.com>"'
            }
        },
        indent=2,
    )
    if (
        run_hook(
            metadata_hook, pv_project, "", raw_payload=multiline_attribution_payload
        ).returncode
        == 0
    ):
        sys.exit(
            "block-public-attribution.sh allowed forbidden attribution in multiline commit payload"
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

    # Escaped quotes, multiline JSON, and Git command flags in check-branch-name.sh:
    escaped_branch_payload = json.dumps(
        {"tool_input": {"command": 'git checkout -b "release/rel-1-test"'}}
    )
    if (
        run_hook(hook, pv_project, "", raw_payload=escaped_branch_payload).returncode
        != 0
    ):
        sys.exit(
            "check-branch-name.sh rejected a branch name enclosed in escaped quotes"
        )
    escaped_invalid_payload = json.dumps(
        {"tool_input": {"command": 'git checkout -b "feature/invalid-branch"'}}
    )
    if (
        run_hook(hook, pv_project, "", raw_payload=escaped_invalid_payload).returncode
        == 0
    ):
        sys.exit(
            "check-branch-name.sh allowed an invalid branch name enclosed in escaped quotes"
        )
    multiline_branch_payload = json.dumps(
        {"tool_input": {"command": "git checkout -b release/rel-1-test"}},
        indent=2,
    )
    if (
        run_hook(hook, pv_project, "", raw_payload=multiline_branch_payload).returncode
        != 0
    ):
        sys.exit("check-branch-name.sh rejected a multiline JSON payload")
    if (
        run_hook(
            hook, pv_project, "git checkout -b release/rel-1-test --track origin/master"
        ).returncode
        != 0
    ):
        sys.exit("check-branch-name.sh rejected checkout -b with trailing --track flag")
    if (
        run_hook(
            hook, pv_project, "git switch -c release/rel-1-test -t origin/master"
        ).returncode
        != 0
    ):
        sys.exit("check-branch-name.sh rejected switch -c with trailing -t flag")
    if run_hook(hook, pv_project, "git switch -C release/rel-1-test").returncode != 0:
        sys.exit("check-branch-name.sh rejected switch -C (force create)")
    if run_hook(hook, pv_project, "git checkout -B release/rel-1-test").returncode != 0:
        sys.exit("check-branch-name.sh rejected checkout -B (force create)")
    if run_hook(hook, pv_project, "git checkout -b -f").returncode == 0:
        sys.exit("check-branch-name.sh allowed a flag '-f' as a branch name")
    if run_hook(hook, pv_project, "git checkout -b --orphan").returncode == 0:
        sys.exit("check-branch-name.sh allowed a flag '--orphan' as a branch name")

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

    # Escaped quotes, multiline JSON, and Git command flags in block-direct-master.sh:
    escaped_push_ok = json.dumps(
        {"tool_input": {"command": 'git push origin "release/rel-1-test"'}}
    )
    if (
        run_hook(direct_hook, pv_project, "", raw_payload=escaped_push_ok).returncode
        != 0
    ):
        sys.exit("block-direct-master.sh rejected a valid push with escaped quotes")
    escaped_push_blocked = json.dumps(
        {"tool_input": {"command": 'git push origin "HEAD:main"'}}
    )
    if (
        run_hook(
            direct_hook, pv_project, "", raw_payload=escaped_push_blocked
        ).returncode
        == 0
    ):
        sys.exit("block-direct-master.sh allowed a push to main with escaped quotes")
    escaped_integration_blocked = json.dumps(
        {"tool_input": {"command": 'git push origin "HEAD:integration/payments"'}}
    )
    if (
        run_hook(
            direct_hook, pv_project, "", raw_payload=escaped_integration_blocked
        ).returncode
        == 0
    ):
        sys.exit(
            "block-direct-master.sh allowed a push to integration branch with escaped quotes"
        )
    multiline_push_ok = json.dumps(
        {"tool_input": {"command": "git push origin release/rel-1-test"}},
        indent=2,
    )
    if (
        run_hook(direct_hook, pv_project, "", raw_payload=multiline_push_ok).returncode
        != 0
    ):
        sys.exit("block-direct-master.sh rejected a multiline JSON push payload")
    multiline_push_blocked = json.dumps(
        {"tool_input": {"command": "git push origin HEAD:main"}},
        indent=2,
    )
    if (
        run_hook(
            direct_hook, pv_project, "", raw_payload=multiline_push_blocked
        ).returncode
        == 0
    ):
        sys.exit("block-direct-master.sh allowed a multiline JSON push to main")
    if (
        run_hook(
            direct_hook, pv_project, "git push -u origin release/rel-1-test"
        ).returncode
        != 0
    ):
        sys.exit("block-direct-master.sh rejected git push with -u flag")
    if (
        run_hook(
            direct_hook,
            pv_project,
            "git push --force-with-lease origin release/rel-1-test",
        ).returncode
        != 0
    ):
        sys.exit(
            "block-direct-master.sh rejected git push with --force-with-lease flag"
        )
    if (
        run_hook(
            direct_hook, pv_project, "git push --set-upstream origin release/rel-1-test"
        ).returncode
        != 0
    ):
        sys.exit("block-direct-master.sh rejected git push with --set-upstream flag")
    if (
        run_hook(direct_hook, pv_project, "git push -f origin HEAD:main").returncode
        == 0
    ):
        sys.exit("block-direct-master.sh allowed git push -f to main")
    if (
        run_hook(direct_hook, pv_project, "git push --force origin master").returncode
        == 0
    ):
        sys.exit("block-direct-master.sh allowed git push --force origin master")
    if (
        run_hook(
            direct_hook, pv_project, "git push origin HEAD:refs/heads/master"
        ).returncode
        == 0
    ):
        sys.exit("block-direct-master.sh allowed git push to refs/heads/master")
    if (
        run_hook(
            direct_hook, pv_project, "git push origin HEAD:refs/heads/integration/auth"
        ).returncode
        == 0
    ):
        sys.exit(
            "block-direct-master.sh allowed git push to refs/heads/integration/auth"
        )

    # /to-spec publishes a new epic integration branch with `git push -u origin
    # integration/<name>` without switching the worktree: creating a branch absent on the
    # remote passes, while updating an existing one or an unverifiable remote stays blocked.
    direct_remote = test_root / "direct-remote.git"
    direct_repo = test_root / "direct-repo"
    subprocess.run(["git", "init", "--bare", "-q", str(direct_remote)], check=True)
    subprocess.run(["git", "init", "-q", str(direct_repo)], check=True)
    for args in (
        ["symbolic-ref", "HEAD", "refs/heads/master"],
        [*commit[1:], "--allow-empty", "-m", "test fixture"],
        ["remote", "add", "origin", str(direct_remote)],
        ["remote", "add", "offline", str(test_root / "missing-remote.git")],
        ["push", "-q", "origin", "master", "master:integration/existing"],
    ):
        subprocess.run(["git", *args], cwd=direct_repo, check=True)

    def direct_push(command: str) -> subprocess.CompletedProcess:
        return run_hook(direct_hook, direct_repo, command)

    for command in (
        "git push -u origin integration/new",
        "git push --set-upstream origin refs/heads/integration/new",
        "git branch integration/new origin/master && git push -u origin integration/new",
        "git push -u origin integration/new 2>&1",
        "git push -u origin integration/new > /dev/null",
    ):
        if direct_push(command).returncode != 0:
            sys.exit(
                f"block-direct-master.sh rejected creating a new integration branch from master: {command!r}"
            )
    for command in (
        "git push -u origin integration/existing",
        "git push origin integration/existing",
        "git push origin HEAD:integration/existing",
        "git push origin HEAD:refs/heads/integration/existing",
        "git push origin +HEAD:integration/existing",
        "git push -u origin integration/new integration/existing",
        "git push -u origin integration/new && git push",
        "git push -u origin integration/new master",
        "git commit -m x && git push -u origin integration/new",
        "git push -u origin integration/new && bash -c 'git push'",
        "git push -u origin integration/new && bash -c 'git push origin HEAD:integration/existing'",
        "git push -u origin integration/new; git -C . push origin HEAD:integration/existing",
        "git push -u origin integration/new && /usr/bin/git push origin HEAD:integration/existing",
        "git commit -m x",
        "git push origin HEAD:master",
    ):
        if direct_push(command).returncode == 0:
            sys.exit(
                f"block-direct-master.sh allowed a protected push or commit from master: {command!r}"
            )
    offline = direct_push("git push -u offline integration/new")
    if offline.returncode == 0 or "не удалось проверить" not in offline.stderr:
        sys.exit(
            "block-direct-master.sh did not block an unverifiable integration push with an "
            f"explanation: {offline.stderr!r}"
        )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/issue-1-test"],
        cwd=direct_repo,
        check=True,
    )
    if direct_push("git push -u origin integration/new").returncode != 0:
        sys.exit(
            "block-direct-master.sh rejected creating a new integration branch from an issue branch"
        )
    for command in (
        "git push origin HEAD:integration/existing",
        "bash -c 'git push origin HEAD:integration/existing'",
        "git push origin HEAD:integration/new2 && git --no-pager push origin HEAD:integration/existing",
    ):
        if direct_push(command).returncode == 0:
            sys.exit(
                f"block-direct-master.sh allowed an issue branch to update an existing integration branch: {command!r}"
            )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "integration/existing"],
        cwd=direct_repo,
        check=True,
    )
    for command in (
        "git commit -m x",
        "git push",
        "git push -u origin integration/existing",
    ):
        if direct_push(command).returncode == 0:
            sys.exit(
                f"block-direct-master.sh allowed a commit or push from an integration branch: {command!r}"
            )

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

    # block-dangerous-git.sh: safe commands, destructive commands, escaped quotes, multiline JSON
    if run_hook(dangerous_hook, pv_project, "git status").returncode != 0:
        sys.exit("block-dangerous-git.sh blocked git status")
    if (
        run_hook(
            dangerous_hook, pv_project, "git checkout -b release/rel-1-test"
        ).returncode
        != 0
    ):
        sys.exit("block-dangerous-git.sh blocked git checkout -b")
    if (
        run_hook(dangerous_hook, pv_project, "git branch -d old-feature").returncode
        != 0
    ):
        sys.exit("block-dangerous-git.sh blocked safe git branch -d")
    if run_hook(dangerous_hook, pv_project, "git reset --soft HEAD~1").returncode != 0:
        sys.exit("block-dangerous-git.sh blocked safe git reset --soft")
    if run_hook(dangerous_hook, pv_project, "git checkout -- file.py").returncode != 0:
        sys.exit("block-dangerous-git.sh blocked safe git checkout of a specific file")

    for destructive_cmd in (
        "git reset --hard HEAD~1",
        "git clean -f",
        "git clean -fd",
        "git branch -D old-branch",
        "git checkout .",
        "git restore .",
    ):
        if run_hook(dangerous_hook, pv_project, destructive_cmd).returncode == 0:
            sys.exit(
                f"block-dangerous-git.sh allowed destructive command: {destructive_cmd}"
            )

    escaped_safe_commit = json.dumps(
        {"tool_input": {"command": 'git commit -m "feat: \\"fix\\" something"'}}
    )
    if (
        run_hook(
            dangerous_hook, pv_project, "", raw_payload=escaped_safe_commit
        ).returncode
        != 0
    ):
        sys.exit("block-dangerous-git.sh rejected safe commit with escaped quotes")
    escaped_destructive_subshell = json.dumps(
        {"tool_input": {"command": 'bash -c "git reset --hard HEAD"'}}
    )
    if (
        run_hook(
            dangerous_hook, pv_project, "", raw_payload=escaped_destructive_subshell
        ).returncode
        == 0
    ):
        sys.exit(
            "block-dangerous-git.sh allowed destructive command inside escaped quotes"
        )

    multiline_safe_commit = json.dumps(
        {
            "tool_input": {
                "command": 'git commit -m "feat: line 1\n\nline 2 of message"'
            }
        },
        indent=2,
    )
    if (
        run_hook(
            dangerous_hook, pv_project, "", raw_payload=multiline_safe_commit
        ).returncode
        != 0
    ):
        sys.exit("block-dangerous-git.sh rejected safe multiline commit payload")
    multiline_destructive_payload = json.dumps(
        {"tool_input": {"command": "git reset --hard HEAD"}},
        indent=2,
    )
    if (
        run_hook(
            dangerous_hook, pv_project, "", raw_payload=multiline_destructive_payload
        ).returncode
        == 0
    ):
        sys.exit(
            "block-dangerous-git.sh allowed destructive command in multiline JSON payload"
        )

    # require-qa-gate.sh: blocks gh pr create without marker, allows with valid marker
    if run_hook(qa_gate_hook, pv_project, "git status").returncode != 0:
        sys.exit("require-qa-gate.sh blocked an unrelated git status command")
    if (
        run_hook(
            qa_gate_hook, pv_project, "gh pr create --body-file pr-body.md"
        ).returncode
        == 0
    ):
        sys.exit(
            "require-qa-gate.sh allowed gh pr create without QA gate passed marker"
        )

    escaped_pr_create = json.dumps(
        {
            "tool_input": {
                "command": 'gh pr create --title "My \\"Special\\" PR" --body-file "pr-body.md"'
            }
        }
    )
    if (
        run_hook(qa_gate_hook, pv_project, "", raw_payload=escaped_pr_create).returncode
        == 0
    ):
        sys.exit(
            "require-qa-gate.sh allowed gh pr create with escaped quotes without marker"
        )

    multiline_pr_create = json.dumps(
        {"tool_input": {"command": "gh pr create --body-file pr-body.md"}},
        indent=2,
    )
    if (
        run_hook(
            qa_gate_hook, pv_project, "", raw_payload=multiline_pr_create
        ).returncode
        == 0
    ):
        sys.exit(
            "require-qa-gate.sh allowed multiline JSON gh pr create without marker"
        )

    head_proc = subprocess.run(
        ["git", "-C", str(pv_project), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    head_sha = head_proc.stdout.strip()
    diff_proc = subprocess.run(
        ["git", "-C", str(pv_project), "diff", "HEAD"],
        capture_output=True,
        check=False,
    )
    diff_bytes = diff_proc.stdout if diff_proc.returncode == 0 else b""
    diff_hash = (
        subprocess.run(
            ["git", "-C", str(pv_project), "hash-object", "--stdin"],
            input=diff_bytes,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    qa_marker = pv_project / ".claude" / ".qa-gate" / "passed"
    qa_marker.parent.mkdir(parents=True, exist_ok=True)
    qa_marker.write_text(f"{head_sha}:{diff_hash}", encoding="utf-8")
    try:
        if (
            run_hook(
                qa_gate_hook, pv_project, "gh pr create --body-file pr-body.md"
            ).returncode
            != 0
        ):
            sys.exit(
                "require-qa-gate.sh blocked gh pr create with valid qa-gate marker"
            )
    finally:
        qa_marker.unlink(missing_ok=True)

    # block-pr-merge.sh: Zero Auto-Merge for gh and glab, decided on the tokens bash executes.
    for command in (
        "gh pr merge 12 --squash",
        "glab mr merge 12",
        "glab mr accept 12",
        "git push && glab mr merge 1",
        'git commit -m "x" && gh pr merge 1',
        "git status; gh pr merge",
        "false || gh pr merge 1",
        "gh pr merge 1&& echo done",
        "echo a#b; gh pr merge 1",
        "printf '1\\n' | xargs gh pr merge",
        "echo 'gh pr merge 1' | bash",
        "bash <<< 'glab mr merge 1'",
        "( gh pr merge 1 )",
        "{ glab mr merge 1; }",
        "if true; then gh pr merge 1; fi",
        "FOO=1 gh pr merge 1",
        "/usr/local/bin/gh pr merge 1",
        "gh.exe pr merge 1",
        '"gh" pr merge 1',
        "sudo -u root glab mr merge 1",
        "timeout 30 gh pr merge 1",
        "uv run glab mr accept 1",
        "docker exec c gh pr merge 1",
        "find . -name x -exec glab mr merge 1 \\;",
        "bash -c 'gh pr merge 1'",
        'sh -lc "glab mr merge 1"',
        "eval 'gh pr merge 1'",
        "ssh host 'glab mr merge 1'",
        "python3 -c \"import os; os.system('gh pr merge 1')\"",
        'echo "$(gh pr merge 1)"',
        "echo `glab mr merge 1`",
        "diff <(gh pr merge 1) notes.txt",
        "bash <<'EOF'\ngh pr merge 1\nEOF",
        "cat <<'EOF' | bash\nglab mr merge 1\nEOF",
        "cat <<EOF\n$(glab mr merge 1)\nEOF",
        "cat <<EOF\nit's `gh pr merge 1`\nEOF",
        "gh pr \\\nmerge 1",
        "echo 'unbalanced gh pr merge",
    ):
        if run_hook(merge_hook, pv_project, command).returncode != 2:
            sys.exit(f"block-pr-merge.sh allowed a merge: {command!r}")
    for command in (
        "git commit -m 'docs: forbid gh pr merge'",
        'python tool.py --note "never glab mr merge or accept"',
        "python tool.py --reason 'never run `gh pr merge` yourself'",
        "gh pr view 1 && gh pr checks 1",
        "glab mr view 3 --comments",
    ):
        if run_hook(merge_hook, pv_project, command).returncode != 0:
            sys.exit(f"block-pr-merge.sh blocked a merge mention: {command!r}")
    multiline_merge = json.dumps(
        {"tool_input": {"command": "git fetch &&\nglab mr merge 1"}}, indent=2
    )
    if (
        run_hook(merge_hook, pv_project, "", raw_payload=multiline_merge).returncode
        != 2
    ):
        sys.exit("block-pr-merge.sh allowed a merge in a multiline JSON payload")
    broken_payload = '{"tool_input": {"command": "gh pr merge 1"'
    if run_hook(merge_hook, pv_project, "", raw_payload=broken_payload).returncode != 2:
        sys.exit("block-pr-merge.sh allowed a merge in an unparsable payload")
    description_only = json.dumps(
        {
            "tool_input": {
                "command": "git status",
                "description": "check status before gh pr merge",
            }
        }
    )
    if run_hook(merge_hook, pv_project, "", raw_payload=description_only).returncode:
        sys.exit("block-pr-merge.sh blocked a merge mentioned only in the description")
    if (pv_project / ".claude" / "hooks" / "__pycache__").exists():
        sys.exit("a hook left .claude/hooks/__pycache__, which uninstall cannot prune")
