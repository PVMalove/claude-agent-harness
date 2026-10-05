"""Hooks целевого проекта: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from harness.storage import storage_path
from scripts.clean_room.merge_corpus import (
    EXEMPT_MENTIONS,
    OLD_MERGE_CHECK,
    PERF_PAYLOADS,
    PROBE_PAYLOADS,
    REVIEW_PAYLOADS,
    glab_variants,
)
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

    def gate_code(command: str, cwd: Path) -> int:
        """Код require-qa-gate.sh для команды, запущенной из `cwd`."""
        payload = json.dumps({"cwd": str(cwd), "tool_input": {"command": command}})
        return run_hook(require_gate, pv_project, "", raw_payload=payload).returncode

    # Creates that publish the linked branch from the primary checkout's cwd.
    linked_creates = [
        f"glab mr create -s {linked_branch} --fill",
        f"glab mr create -fs {linked_branch}",
        f"glab mr create -s{linked_branch}",
        f"glab mr create --source-branch {linked_branch} --fill",
        f"glab mr create --source-branch={linked_branch}",
        # The `new` aliases and the gh -H shorthand of --head publish the same branch.
        f"glab mr new -s {linked_branch}",
        f"gh pr new --head {linked_branch}",
        f"gh pr create -H {linked_branch}",
        f"gh pr create -H{linked_branch}",
        f"gh pr create -dH {linked_branch}",
        f"gh pr create -H=origin:{linked_branch}",
    ]
    for command in linked_creates:
        if gate_code(command, pv_project) != 2:
            sys.exit(f"primary checkout QA marker opened a linked MR: {command!r}")
    if gate_code("glab mr create --fill", linked) != 2:
        sys.exit("glab mr create from an untested linked checkout cwd was allowed")
    if run_hook(record_gate, pv_project, "", cwd=linked).returncode:
        sys.exit("could not record linked-worktree QA marker")
    if run_hook(require_gate, pv_project, "", raw_payload=pr_payload).returncode:
        sys.exit("linked-worktree QA marker did not permit its PR")
    for command in linked_creates:
        if gate_code(command, pv_project):
            sys.exit(f"linked-worktree QA marker did not permit: {command!r}")
    for command in (
        "glab mr create --fill",
        # -s of another command and glab -H (a repository) do not name the MR branch.
        "git commit -s -m wip && glab mr create --fill",
        "glab mr create -H group/fork --fill",
    ):
        if gate_code(command, linked):
            sys.exit(f"QA marker did not permit MR from the linked cwd: {command!r}")
    for command in (
        "glab mr create -s feature/issue-999-missing",
        "glab mr create -s",
        f"glab mr create -s {linked_branch} && gh pr create --fill",
    ):
        if gate_code(command, linked) != 2:
            sys.exit(f"unresolvable MR source branch reused a QA marker: {command!r}")
    # A --head the pre-#443 global scan finds is always checked, even where the token parse
    # sees no create: the cwd checkout's marker must not open a PR for another branch.
    missing_head = "gh pr create --head feature/issue-999-missing --fill"
    for command in (
        f"{{ cat <<'EOF'\n{missing_head}\nEOF\n}} | bash",
        f"cat <<'EOF' |\n{missing_head}\nEOF\nbash",
        f"true # note \\\n{missing_head}",
        f"echo $(true)#; {missing_head}",
        f"cat <<'EOF'\nx\\\nEOF\n{missing_head}\nEOF",
    ):
        if gate_code(command, linked) != 2:
            sys.exit(f"QA gate reused the cwd marker for another PR head: {command!r}")
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
    if gate_code(f"glab mr create -s {linked_branch}", pv_project) != 2:
        sys.exit("stale linked-worktree QA marker opened an MR")

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
    subprocess.run(
        ["git", "checkout", "-q", "--", "AGENTS.md"], cwd=no_harness, check=True
    )
    no_harness_marker.unlink()
    # A session started inside the linked worktree points CLAUDE_PROJECT_DIR at it too:
    # mark then reads the main worktree's config.
    if (
        mark_no_harness("echo test", no_harness).returncode
        or not no_harness_marker.is_file()
    ):
        sys.exit(
            "mark ignored the main worktree config inside a linked-worktree session"
        )
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
    if "вне проекта" not in escaped_result.stderr or "payload" in escaped_result.stderr:
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
        (
            runtime_config / "projects" / "-repo" / "memory" / "MEMORY.md",
            config_env,
            True,
        ),
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

    # A heredoc body is stdin data: a script that only mentions publish commands publishes
    # nothing, even when its quotes do not balance for a shell lexer.
    script_heredoc = (
        "python - <<'EOF'\n"
        "import subprocess\n"
        "# It's a fixture for .claude/hooks: git commit -m \"x\", git push, gh pr create.\n"
        'subprocess.run(["gh", "pr", "create", "--body-file", "body.md"])\n'
        "EOF"
    )
    commit_message_file = pv_project / "commit-message.txt"
    commit_message_file.write_text("feat: document runtime behavior\n", encoding="utf-8")
    for allowed_cmd in (
        script_heredoc,
        f"{script_heredoc}\necho done",
        'echo "it\'s only text: git commit, git push"',
        "python -c 'print(\"gh pr create --body-file body.md\")'",
        "git commit -m \"$(cat <<'EOF'\n"
        'fix(hooks): don\'t parse "heredoc" bodies\n'
        "\n"
        "Keeps the check on shell-level commands.\n"
        "EOF\n"
        ')"',
        f"git commit -F {commit_message_file}",
    ):
        result = run_hook(metadata_hook, pv_project, allowed_cmd)
        if result.returncode != 0:
            sys.exit(
                "block-public-attribution.sh rejected a command without public "
                f"attribution: {allowed_cmd!r}: {result.stderr}"
            )
    attributed_message_file = pv_project / "attributed-message.txt"
    attributed_message_file.write_text(
        "feat: add hook\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
        encoding="utf-8",
    )
    for blocked_cmd in (
        "git commit -m \"$(cat <<'EOF'\n"
        "feat: add hook\n"
        "\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n"
        "EOF\n"
        ')"',
        f'{script_heredoc}\ngit commit -m "chore: generated by Codex"',
        'echo "<<X"\ngit commit -m "chore: generated by Codex"\nX',
        'echo hi # <<X\ngit commit -m "chore: generated by Codex"\nX',
        'echo $((1 << 2))\ngit commit -m "chore: generated by Codex"\n2',
        f"git commit -F {attributed_message_file}",
        f"gh pr edit 1 --body-file {pr_body}",
    ):
        if run_hook(metadata_hook, pv_project, blocked_cmd).returncode == 0:
            sys.exit(
                f"block-public-attribution.sh allowed public attribution: {blocked_cmd!r}"
            )
    attributed_repo = test_root / "attributed_push"
    attributed_repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=attributed_repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--no-verify",
            "--allow-empty",
            "-qm",
            "feat: add hook\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        ],
        cwd=attributed_repo,
        check=True,
    )
    if run_hook(metadata_hook, attributed_repo, "git push origin HEAD").returncode == 0:
        sys.exit(
            "block-public-attribution.sh allowed a push of an attributed local commit"
        )
    if run_hook(metadata_hook, attributed_repo, script_heredoc).returncode != 0:
        sys.exit(
            "block-public-attribution.sh treated a heredoc mention of git push as a push"
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

    # Each git commit/push is checked in the checkout it runs in (#476): `git -C`/`--work-tree`/
    # `--git-dir`, then a preceding `cd`, then the payload cwd, then the project root. Mentions
    # in other commands' arguments, quotes and heredocs are not calls.
    direct_wt = test_root / "direct-wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feature/issue-2-wt", str(direct_wt)],
        cwd=direct_repo,
        check=True,
    )

    def direct_at(
        root: Path, command: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess:
        payload: dict[str, object] = {"tool_input": {"command": command}}
        if cwd is not None:
            payload["cwd"] = str(cwd)
        return run_hook(direct_hook, root, "", raw_payload=json.dumps(payload))

    heredoc_commit = (
        f"git -C {direct_wt} commit -m \"$(cat <<'EOF'\n"
        'fix: git push origin HEAD:master\nEOF\n)"'
    )
    for command, cwd in (
        ("git commit -m x", direct_wt),
        ("git push -u origin feature/issue-2-wt", direct_wt),
        (f"git -C {direct_wt} commit -m x", None),
        (f"git -C {direct_wt} push origin feature/issue-2-wt", None),
        (f"cd {direct_wt} && git commit -m x", None),
        (f"cd {direct_wt}; git commit -m x", None),
        (f"cd {direct_wt} && git add -A && git commit -m x && git push", None),
        (f"cd {direct_wt} && git push 2>&1 | tail -3", None),
        (f"git -C {direct_wt} push -u origin integration/new", None),
        (heredoc_commit, None),
        (
            f"cd {direct_wt} && git push -u origin feature/issue-2-wt && "
            "gh pr create --base integration/existing",
            None,
        ),
        ("grep -rn 'git commit' .", None),
        ('rg "git push" docs', None),
        ('echo "git commit -m x"', None),
        ("git log --grep 'git push'", None),
        ("cat <<'EOF'\ngit push origin HEAD:integration/existing\nEOF", None),
    ):
        if direct_at(direct_repo, command, cwd).returncode != 0:
            sys.exit(
                "block-direct-master.sh rejected a call outside the protected project root "
                f"or a mere mention: {command!r} (cwd {cwd})"
            )
    for root, command, cwd in (
        (direct_repo, "git -c user.name=x commit -m x", None),
        (direct_repo, "git -c user.name=x push", None),
        (direct_repo, f"git --git-dir={direct_repo}/.git commit -m x", direct_wt),
        (direct_wt, f"git --git-dir={direct_repo}/.git push", direct_wt),
        (direct_wt, f"cd {direct_repo} && git push", direct_wt),
        (direct_repo, f"git --work-tree={direct_wt} commit -m x", None),
        (direct_repo, f"git --work-tree {direct_wt} push", None),
        (direct_repo, "git commit -m x", direct_repo),
        (direct_repo, f"cd {direct_wt} && cd {direct_repo} && git commit -m x", None),
        (direct_repo, f"cd {direct_wt} && git push origin HEAD:master", None),
        (
            direct_repo,
            f"git -C {direct_wt} push origin HEAD:integration/existing",
            None,
        ),
        (direct_repo, 'bash -c "git -C . commit -m x"', None),
        (direct_wt, f"git -C {direct_repo} commit -m x", None),
        (direct_wt, f"cd {direct_repo} && git commit -m x", None),
        (direct_wt, f"git -C {direct_repo} push", None),
        (direct_wt, f"git -C {direct_repo} push origin HEAD:feature/issue-2-wt", None),
        (direct_wt, "git commit -m x", direct_repo),
        # A `cd` that may not run, or runs in a subshell, a pipeline or the background, does
        # not move a later call out of the protected checkout.
        (direct_repo, f'git commit -m "$(cd {direct_wt})"', None),
        (direct_repo, f"(cd {direct_wt}); git commit -m x", None),
        (direct_repo, f"bash -c 'cd {direct_wt}'; git commit -m x", None),
        (direct_repo, f"cd {direct_wt} | true; git commit -m x", None),
        (direct_repo, f"cd {direct_wt} || git commit -m x", None),
        (direct_repo, f"false && cd {direct_wt}; git commit -m x", None),
        (direct_repo, f"cd {direct_wt} && sleep 0 & git commit -m x", None),
        (direct_wt, f"builtin cd {direct_repo}; git commit -m x", direct_wt),
        (direct_wt, f"bash <<'EOF'\ngit -C {direct_repo} commit -m x\nEOF", direct_wt),
    ):
        if direct_at(root, command, cwd).returncode == 0:
            sys.exit(
                "block-direct-master.sh allowed a commit or push in a protected checkout: "
                f"{command!r} (root {root.name}, cwd {cwd})"
            )
    # A commit or push whose checkout or text cannot be resolved fails closed.
    for command, reason in (
        ('cd "$WT" && git commit -m x', "checkout"),
        (f"cd {test_root / 'missing'}; git commit -m x", "checkout"),
        ("python3 -c 'import os; os.system(\"git commit -m x\")'", "разобрать"),
        ('X="git push origin HEAD:master"; $X', "разобрать"),
        ('sh -c "$(printf %s "git push origin HEAD:master")"', "разобрать"),
        (f"GIT_DIR={direct_repo}/.git git commit -m x", "checkout"),
        (f"env -C {direct_repo} git commit -m x", "checkout"),
        (f"echo {direct_repo} | xargs -I{{}} git -C {{}} commit -m x", "checkout"),
        # `cd` is followed only through a command the strict lexer reads: no `$`.
        (f"cd {direct_wt} && git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"", "checkout"),
    ):
        blocked = direct_at(direct_wt, command, direct_wt)
        if blocked.returncode == 0 or reason not in blocked.stderr:
            sys.exit(
                "block-direct-master.sh did not fail closed on an unresolvable commit: "
                f"{command!r}: {blocked.stderr!r}"
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

    # block-dangerous-git.sh: a destructive command quoted as data (string literal, interpreter
    # argument, heredoc body) is not a run; the same command executed by a shell still is.
    mentioned_not_run = (
        "python -c \"print('git reset --hard')\"",
        "python3 -c 'assert \"git reset --hard\" in doc'",
        "echo \"git clean -f\" && grep -r 'git branch -D' docs",
        "python3 - <<'EOF'\nassert \"git reset --hard\" in doc\nEOF",
        "cat <<EOF\ngit restore .\nEOF",
        "git commit -m \"$(cat <<'EOF'\nfix: don't run git checkout . here\nEOF\n)\"",
        "git status # git reset --hard",
        "python3 -c $'print(1) # git reset --hard' || echo \"git clean -f\"",
        "echo \"git reset --hard\" > notes.txt && bash run.sh",
    )
    for safe_cmd in mentioned_not_run:
        if run_hook(dangerous_hook, pv_project, safe_cmd).returncode != 0:
            sys.exit(f"block-dangerous-git.sh blocked a mention of a command: {safe_cmd!r}")
    really_run = (
        "echo ok && git reset --hard",
        "cd sub; git clean -fd",
        "(git branch -D old)",
        "sudo git clean -f",
        "echo x | xargs git branch -D",
        "if true; then git checkout .; fi",
        "sh -c 'git restore .'",
        "eval \"git reset --hard\"",
        "echo \"$(git branch -D old)\"",
        "echo `git checkout .`",
        "bash <<'EOF'\ngit reset --hard\nEOF",
        "cat <<EOF\n$(git reset --hard)\nEOF",
        "cat <<'EOF'\ntext\nEOF\ngit reset --hard",
        "python -c \"unterminated git reset --hard",
        "echo \"git reset --hard\" | sh",
        "sh <<< \"git clean -f\"",
        "eval $'git branch -D old'",
        "bash <(echo 'git restore .')",
    )
    for destructive_cmd in really_run:
        if run_hook(dangerous_hook, pv_project, destructive_cmd).returncode == 0:
            sys.exit(
                f"block-dangerous-git.sh allowed destructive command: {destructive_cmd!r}"
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
    # PR/MR creation is still detected by text: a quoted mention needs QA evidence too.
    for command in (
        "glab mr create --fill",
        "gh pr new --fill",
        "glab mr new --fill",
        'git commit -m "docs: run qa-gate before gh pr create"',
        'python tool.py --note "glab mr create needs qa-gate"',
    ):
        if run_hook(qa_gate_hook, pv_project, command).returncode != 2:
            sys.exit(f"require-qa-gate.sh allowed a create without marker: {command!r}")

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

    # block-pr-merge.sh: Zero Auto-Merge for gh and glab. Differential over the #443 corpus: what
    # the pre-#443 check blocked stays blocked, with its glab variants, except allowlisted mentions.
    exempt = {name for name, _, _ in EXEMPT_MENTIONS}
    corpus = [(name, command) for name, command, _ in EXEMPT_MENTIONS]
    corpus += [*REVIEW_PAYLOADS, *PROBE_PAYLOADS, *PERF_PAYLOADS]
    cases = [
        (name, variant, bool(OLD_MERGE_CHECK.search(payload)))
        for name, command in corpus
        for payload in [json.dumps({"tool_input": {"command": command}})]
        for variant in glab_variants(command)
    ]

    def merge_code(command: str) -> int:
        """Код block-pr-merge.sh для команды."""
        return run_hook(merge_hook, pv_project, command).returncode

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(merge_code, [variant for _, variant, _ in cases]))
    missed = sorted(
        {
            name
            for (name, _, old), code in zip(cases, codes)
            if old and name not in exempt and code != 2
        }
    )
    if missed:
        sys.exit(
            f"block-pr-merge.sh allowed {len(missed)} corpus payloads the pre-#443 check "
            f"blocked: {missed}"
        )
    blocked_mentions = sorted(
        {name for (name, _, _), code in zip(cases, codes) if name in exempt and code}
    )
    if blocked_mentions:
        sys.exit(f"block-pr-merge.sh blocked allowlisted mentions: {blocked_mentions}")
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
        # A heredoc starts only at a `<<` operator outside quotes and comments: operator text
        # in quotes, in a comment or in a here-string cannot hide a merge on a later line.
        "echo '<<EOF'\ngh pr merge 1\nEOF",
        'echo "<<EOF"\ngh pr merge 1\nEOF',
        "echo '<<EOF'\nglab mr merge 1\nEOF",
        'echo "<<EOF"\nglab mr merge 1\nEOF',
        "echo '<<EOF'\nglab mr accept 1\nEOF",
        "echo $'\\' <<EOF '' \\'\ngh pr merge 1\nEOF",
        'echo "${x:-"<<EOF "}"\ngh pr merge 1\nEOF',
        "git commit -m 'see <<EOF'\ngh pr merge 1\nEOF",
        "# cat <<EOF\ngh pr merge 1\nEOF",
        "ls # it's <<EOF\nglab mr merge 1\nEOF",
        "cat <<'A' <<\"B\"\nnever gh pr merge\nA\nnor glab mr merge\nB\ngh pr merge 1",
        "cat <<-'EOF'\n\tgh pr merge is manual\n\tEOF\ngh pr merge 1",
        "cat <<< EOF\ngh pr merge 1\nEOF",
        'printf "%s" "<<X"; gh pr merge 1\nX',
        "true '<<EOF' && gh pr merge 1\nEOF",
        # An operator the scanner cannot decide blocks on its merge text (fail closed).
        "cat <<$END\nnever gh pr merge\n$END",
        'cat <<EOF"x"\nEOFx\ngh pr merge 1\nEOF',
        "echo $((x<<y))\ngh pr merge 1\ny",
        "a[x<<y ]=1\ngh pr merge 1\ny",
        # Merge text outside the allowlist blocks (fail closed): an unknown program, a comment,
        # a substitution, backquotes.
        "git commit -m \"$(cat <<'EOF'\nfeat: don't run gh pr merge\nEOF\n)\"",
        'python tool.py --note "never glab mr merge or accept"',
        "python tool.py --reason 'never run `gh pr merge` yourself'",
        "cat notes.md # gh pr merge later",
        "echo 'gh pr merge 1' | tee notes.txt",
        "git -C repo commit -m 'docs: forbid gh pr merge'",
        "/bin/echo gh pr merge 1",
        # A merge split by quotes is still a merge.
        "gh pr mer''ge 1",
        'glab mr "accept" 1',
        "bash -c 'glab mr acc''ept 1'",
    ):
        if run_hook(merge_hook, pv_project, command).returncode != 2:
            sys.exit(f"block-pr-merge.sh allowed a merge: {command!r}")
    for command in (
        "cat <<'EOF'\nnever run gh pr merge or glab mr merge\nEOF",
        'cat > notes.md <<"EOF"\nuse glab mr accept manually\nEOF',
        "cat <<\\EOF\ngh pr merge is manual\nEOF",
        "cat <<EOF\nnever run gh pr merge\nEOF",
        'cat <<"EOF"\ngh pr merge 1\nEOF',  # the h4 probe
        "cat <<'EOF'\ngh pr merge 1\nEOF",
        "cat <<\\EOF\ngh pr merge 1\nEOF",
        "cat <<'A' <<\"B\"\nnever gh pr merge\nA\nnor glab mr merge\nB",
        "cat <<-'EOF'\n\tgh pr merge is manual\n\tEOF",
        "cat <<'EOF' > notes.md\nrun glab mr merge by hand\nEOF",
        "echo gh pr merge 1",
        "echo 'glab mr merge 1'",
        "printf '%s\\n' \"glab mr merge\" > notes.txt",
        "grep -n 'gh pr merge' docs/hooks/block-pr-merge.md",
        "git commit -m 'docs: forbid gh pr merge'",
        "git commit -m 'docs: forbid glab mr accept' && echo done",
        "gh issue comment 7 --body 'gh pr merge is manual' 2>&1",
        "glab mr update 3 --description 'never glab mr merge' | head -n 1",
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
    # The pre-#443 raw-payload check still decides when the parsed command hides its match.
    duplicate = '{"tool_input": {"command": "gh pr merge 1", "command": "git status"}}'
    if run_hook(merge_hook, pv_project, "", raw_payload=duplicate).returncode != 2:
        sys.exit("block-pr-merge.sh allowed a payload the pre-#443 check blocked")
    # Python decides every call: no interpreter, or one that fails, blocks any command.
    bash_only = test_root / "bash-only-path"
    bash_only.mkdir()
    if not Path(BASH).is_absolute():
        (bash_only / "bash").symlink_to(shutil.which(BASH) or BASH)
    no_python = run_hook(
        merge_hook, pv_project, "git status", env_overrides={"PATH": str(bash_only)}
    )
    if no_python.returncode != 2:
        sys.exit("block-pr-merge.sh allowed a command without Python to check it")
    # A partial copy of pr_commands.py blocks every hook that uses it, cut mid-statement or between
    # functions.
    source = (pv_project / ".claude" / "hooks" / "pr_commands.py").read_text(
        encoding="utf-8"
    )
    middle = len(source) // 2
    create_payload = json.dumps(
        {"cwd": str(linked), "tool_input": {"command": "gh pr create --fill"}}
    )
    if (
        run_hook(record_gate, pv_project, "", cwd=linked).returncode
        or run_hook(require_gate, pv_project, "", raw_payload=create_payload).returncode
    ):
        sys.exit("a fresh linked-worktree QA marker did not permit its PR")
    for label, end in (
        ("cut mid-statement", source.index("(", middle) + 1),
        ("cut between functions", source.index("\ndef ", middle)),
    ):
        partial_hooks = test_root / f"partial-hooks-{end}"
        shutil.copytree(pv_project / ".claude" / "hooks", partial_hooks)
        (partial_hooks / "pr_commands.py").write_text(source[:end], encoding="utf-8")
        partial_merge = run_hook(partial_hooks / "block-pr-merge.sh", pv_project, "ls")
        if partial_merge.returncode != 2:
            sys.exit(f"block-pr-merge.sh with pr_commands.py {label} allowed a command")
        partial_direct = run_hook(
            partial_hooks / "block-direct-master.sh", pv_project, "ls"
        )
        if partial_direct.returncode != 2:
            sys.exit(
                f"block-direct-master.sh with pr_commands.py {label} allowed a command"
            )
        partial_gate = run_hook(
            partial_hooks / "require-qa-gate.sh",
            pv_project,
            "",
            raw_payload=create_payload,
        )
        if partial_gate.returncode != 2:
            sys.exit(f"require-qa-gate.sh with pr_commands.py {label} allowed a PR")
    if (pv_project / ".claude" / "hooks" / "__pycache__").exists():
        sys.exit("a hook left .claude/hooks/__pycache__, which uninstall cannot prune")
