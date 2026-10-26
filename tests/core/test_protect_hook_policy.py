"""Критичный guard protect-hook-policy (issue #794): правки политики hooks и shell-запись в project.json."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from harness.orchestration.hook_policy import parse_hook_response

HOOKS = Path(__file__).resolve().parents[2] / "harness" / "project" / "hooks"
BASH = shutil.which("bash")
POLICY = {"profile": "strict", "escalations": [{"roles": ["qa"], "profile": "strict"}]}


def _load(filename: str = "protect_hook_policy.py") -> ModuleType:
    """Import a hook the way the installed copy imports itself (siblings on sys.path)."""
    sys.path.insert(0, str(HOOKS))
    try:
        spec = importlib.util.spec_from_file_location(
            f"{filename[:-3]}_unit", HOOKS / filename
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(HOOKS))


guard = _load()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    config = tmp_path / ".harness" / "project.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {"qa_gate_commands": ["make lint"], "hook_policy": POLICY}, indent=2
        ),
        encoding="utf-8",
    )
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    (tmp_path / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _verdict(
    root: Path, tool: str, tool_input: dict[str, object], cwd: Path | None = None
) -> str | None:
    """None — разрешено, иначе reason_code отказа."""
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd or root)}
    try:
        guard.decide(payload, str(root))
    except guard.Refusal as refusal:
        return str(refusal.code)
    return None


def _write(root: Path, path: str | Path, content: str) -> str | None:
    return _verdict(root, "Write", {"file_path": str(path), "content": content})


def _edit(
    root: Path, path: str | Path, old: str, new: str, **extra: object
) -> str | None:
    return _verdict(
        root,
        "Edit",
        {"file_path": str(path), "old_string": old, "new_string": new, **extra},
    )


def _bash(root: Path, command: str, cwd: Path | None = None) -> str | None:
    return _verdict(root, "Bash", {"command": command}, cwd)


# -- Write/Edit of .harness/project.json: the hook_policy key is compared, not the file ----------


def test_an_edit_of_another_key_is_allowed(project: Path) -> None:
    config = project / ".harness" / "project.json"

    assert _edit(project, config, '"make lint"', '"make test"') is None
    document = {"qa_gate_commands": [], "hook_policy": POLICY, "language": "ru"}
    assert _write(project, config, json.dumps(document)) is None


def test_a_rewrite_that_keeps_the_parsed_policy_is_allowed(project: Path) -> None:
    config = project / ".harness" / "project.json"
    reordered = {
        "hook_policy": {"escalations": POLICY["escalations"], "profile": "strict"}
    }

    assert _write(project, config, json.dumps(reordered, indent=8)) is None


@pytest.mark.parametrize(
    "changed",
    [
        {"profile": "minimal"},
        {"profile": "strict"},
        {"profile": "strict", "escalations": []},
        None,
    ],
)
def test_a_changed_or_removed_policy_is_blocked(project: Path, changed: object) -> None:
    config = project / ".harness" / "project.json"
    document: dict[str, object] = {"qa_gate_commands": []}
    if changed is not None:
        document["hook_policy"] = changed

    assert _write(project, config, json.dumps(document)) == "hook-policy-changed"


def test_an_edit_that_changes_the_policy_is_blocked(project: Path) -> None:
    config = project / ".harness" / "project.json"

    assert (
        _edit(
            project,
            config,
            '"profile": "strict"',
            '"profile": "minimal"',
            replace_all=True,
        )
        == "hook-policy-changed"
    )
    edits = [
        {"old_string": '"make lint"', "new_string": '"make all"'},
        {"old_string": '"profile": "strict",', "new_string": '"profile": "standard",'},
    ]
    assert (
        _verdict(project, "MultiEdit", {"file_path": str(config), "edits": edits})
        == "hook-policy-changed"
    )
    assert (
        _verdict(project, "MultiEdit", {"file_path": str(config), "edits": edits[:1]})
        is None
    )


def test_a_policy_added_to_a_project_without_one_is_blocked(project: Path) -> None:
    config = project / ".harness" / "project.json"
    config.write_text(json.dumps({"qa_gate_commands": []}), encoding="utf-8")

    assert _write(project, config, json.dumps({"qa_gate_commands": ["x"]})) is None
    assert (
        _write(project, config, json.dumps({"hook_policy": {"profile": "standard"}}))
        == "hook-policy-changed"
    )


def test_creating_the_file_is_allowed_only_without_a_policy(project: Path) -> None:
    fresh = project / "other" / ".harness" / "project.json"

    assert _write(project, fresh, json.dumps({"language": "ru"})) is None
    assert (
        _write(project, fresh, json.dumps({"hook_policy": POLICY}))
        == "hook-policy-changed"
    )
    assert _edit(project, fresh, "", '{"language": "ru"}') is None


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '"text"', '{"hook_policy": {}, "hook_policy": {}}', ""],
)
def test_an_unparseable_result_is_blocked(project: Path, content: str) -> None:
    config = project / ".harness" / "project.json"

    assert _write(project, config, content) == "hook-policy-unparseable"


def test_an_unparseable_file_before_the_edit_is_blocked(project: Path) -> None:
    config = project / ".harness" / "project.json"
    config.write_text("{broken", encoding="utf-8")

    assert _edit(project, config, "broken", '"x": 1}') == "hook-policy-unparseable"
    assert _write(project, config, "{}") == "hook-policy-unparseable"
    config.write_bytes(b'{"a": "\xff"}')
    assert _write(project, config, "{}") == "hook-policy-unparseable"


@pytest.mark.parametrize(
    "tool_input",
    [
        {"old_string": "absent", "new_string": "x"},
        {"old_string": ",", "new_string": ";"},
        {"old_string": 1, "new_string": "x"},
        {"old_string": "", "new_string": "x"},
    ],
)
def test_an_edit_whose_result_cannot_be_computed_is_blocked(
    project: Path, tool_input: dict[str, object]
) -> None:
    config = project / ".harness" / "project.json"

    assert (
        _verdict(project, "Edit", {"file_path": str(config), **tool_input})
        == "hook-policy-unparseable"
    )
    assert (
        _verdict(project, "MultiEdit", {"file_path": str(config), "edits": []})
        == "hook-policy-unparseable"
    )
    assert (
        _verdict(project, "NotebookEdit", {"notebook_path": str(config)})
        == "hook-policy-unparseable"
    )
    assert (
        _verdict(project, "Write", {"file_path": str(config), "content": 5})
        == "hook-policy-unparseable"
    )


# -- Settings, .codex/hooks.json and the hook directories ----------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/settings.team.json",
        ".codex/hooks.json",
        ".claude/hooks/block-pr-merge.sh",
        ".claude/hooks/sub/new-hook.sh",
        ".codex/hooks/guard.py",
        ".CLAUDE/Settings.JSON",
    ],
)
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_an_agent_cannot_edit_the_hook_configuration(
    project: Path, relative: str, tool: str
) -> None:
    field = "notebook_path" if tool == "NotebookEdit" else "file_path"
    target = project / relative

    assert (
        _verdict(project, tool, {field: str(target), "content": "{}"})
        == "agent-config-edit"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "src/app.py",
        ".claude/settings.txt",
        ".claude/agents/test-author.md",
        "settings.json",
        "docs/hooks/block-pr-merge.md",
        "harness/project/hooks/block-pr-merge.sh",
        ".harness/project.json.bak",
    ],
)
def test_other_files_are_not_protected(project: Path, relative: str) -> None:
    assert _write(project, project / relative, "x") is None


def test_a_relative_path_is_resolved_against_the_payload_cwd(project: Path) -> None:
    assert (
        _verdict(
            project,
            "Write",
            {"file_path": "settings.json", "content": "{}"},
            project / ".claude",
        )
        == "agent-config-edit"
    )
    assert (
        _verdict(
            project,
            "Write",
            {"file_path": "../.claude/hooks/x.sh", "content": ""},
            project / "src",
        )
        == "agent-config-edit"
    )
    assert (
        _verdict(
            project, "Write", {"file_path": "settings.json", "content": "{}"}, project
        )
        is None
    )


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")


def test_a_symlink_to_a_protected_path_changes_nothing(project: Path) -> None:
    _link(project / "alias.json", project / ".harness" / "project.json")
    _link(project / "cfg.json", project / ".claude" / "settings.local.json")
    _link(project / "hookdir", project / ".claude" / "hooks")
    _link(project / "meta", project / ".harness")

    assert (
        _write(
            project,
            project / "alias.json",
            json.dumps({"hook_policy": {"profile": "minimal"}}),
        )
        == "hook-policy-changed"
    )
    assert (
        _write(project, project / "alias.json", json.dumps({"hook_policy": POLICY}))
        is None
    )
    assert _write(project, project / "cfg.json", "{}") == "agent-config-edit"
    assert _write(project, project / "hookdir" / "x.sh", "") == "agent-config-edit"
    assert (
        _write(project, project / "meta" / "project.json", "{}")
        == "hook-policy-changed"
    )


def test_a_payload_without_a_path_or_a_command_is_blocked(project: Path) -> None:
    assert _verdict(project, "Write", {}) == "hook-policy-protected"
    assert _verdict(project, "Write", {"file_path": ""}) == "hook-policy-protected"
    payloads: list[object] = [None, [], {"tool_input": "x"}, {"cwd": "x"}]
    for payload in payloads:
        with pytest.raises(guard.Refusal):
            guard.decide(payload, str(project))


# -- Bash: a shell write to .harness/project.json -------------------------------------------------

SHELL_WRITES = [
    "echo x > .harness/project.json",
    "echo x >> .harness/project.json",
    "echo x >| .harness/project.json",
    "echo x &> .harness/project.json",
    "echo x 2> .harness/project.json",
    ": > .harness/project.json",
    "cat a.json > .harness/project.json",
    ">.harness/project.json",
    "cat > .harness/project.json <<'EOF'\n{}\nEOF",
    'echo x > ".harness/project.json"',
    "echo x > ./.harness/../.harness/project.json",
    "sed -i s/a/b/ .harness/project.json",
    "sed -i.bak s/a/b/ .harness/project.json",
    "sed -ni p .harness/project.json",
    "sed --in-place s/a/b/ .harness/project.json",
    "echo x | tee .harness/project.json",
    "tee -a .harness/project.json < a.json",
    "cp a.json .harness/project.json",
    "mv a.json .harness/project.json",
    "install a.json .harness/project.json",
    "dd if=a.json of=.harness/project.json",
    "truncate -s 0 .harness/project.json",
    "ln -sf a.json .harness/project.json",
    "rm .harness/project.json",
    "rm -rf .harness",
    "mv .harness .harness-old",
    "rsync -a src/ .harness/",
    "git checkout -- .harness/project.json",
    "git restore .harness/project.json",
    "python3 -c \"open('.harness/project.json','w').write('{}')\"",
    "perl -pi -e 's/a/b/' .harness/project.json",
    "bash -c 'sed -i s/a/b/ .harness/project.json'",
    "env sed -i s/a/b/ .harness/project.json",
    "sudo tee .harness/project.json",
    "cd .harness && sed -i s/a/b/ project.json",
    "cd .harness; echo x > project.json",
    "sed -i s/a/b/ .harness/proj*.json",
    "sed -i s/a/b/ .harness/project.js[o]n",
    'sed -i "s/a/$X/" .harness/project.json',
    'echo "$(date)" > .harness/project.json',
    "f=.harness/project.json; echo x > $f",
    "echo `date` > .harness/project.json",
    "(echo x > .harness/project.json)",
    "true && echo x > .harness/project.json",
    "echo x | sed -i p .harness/project.json",
    # The directory as the target: the file is replaced together with the hook_policy key.
    "git checkout HEAD~1 -- .harness",
    "git restore --source=HEAD~1 .harness",
    "git restore --source=HEAD~1 .harness/",
    "git checkout HEAD~1 -- .harness/proj*",
    "git checkout HEAD~1 -- .harn*",
    "cp -r /nonexistent/dir/. .harness/",
    "cp -r /nonexistent/dir .harness",
    "tar -C .harness -xf /nonexistent/x.tar",
    "tar -xf /nonexistent/x.tar --directory=.harness/ -C .harness",
    "unzip /nonexistent/x.zip -d .harness",
    "cd .harness && cp /nonexistent/x .",
    "cp /nonexistent/x .harness/project.json",
]


@pytest.mark.parametrize("command", SHELL_WRITES)
def test_a_shell_write_to_the_project_config_is_blocked(
    project: Path, command: str
) -> None:
    assert _bash(project, command) == "project-json-shell-write"


def test_a_shell_write_through_an_absolute_path_or_a_symlink_is_blocked(
    project: Path,
) -> None:
    absolute = project / ".harness" / "project.json"
    _link(project / "alias.json", absolute)

    assert _bash(project, f"sed -i s/a/b/ {absolute}") == "project-json-shell-write"
    assert (
        _bash(project, f"echo x > {absolute}", cwd=project / "src")
        == "project-json-shell-write"
    )
    assert _bash(project, "sed -i s/a/b/ alias.json") == "project-json-shell-write"
    assert _bash(project, "echo x > alias.json") == "project-json-shell-write"


SHELL_READS = [
    "cat .harness/project.json",
    "grep -n hook_policy .harness/project.json",
    "grep -rn '.harness/project.json' harness/",
    "head -n 5 .harness/project.json",
    "sed -n 1,5p .harness/project.json",
    "jq .hook_policy .harness/project.json",
    "git diff .harness/project.json",
    "git -C . status --short .harness/project.json",
    "git add .harness/project.json",
    "git commit -m 'docs: describe .harness/project.json'",
    "git commit -m \"$(cat <<'EOF'\nfix: handle project.json\nEOF\n)\"",
    "ls -la .harness",
    "cd .harness && cat project.json",
    "wc -l .harness/project.json",
    "cat .harness/project.json | grep hook",
    "diff .harness/project.json a.json",
    "echo hello > notes.txt",
    "echo 'see .harness/project.json' > notes.txt",
    "python3 -m pytest -q tests",
    "sed -i s/a/b/ src/app.py",
    "tee out.log < in.txt",
    "mv a.txt b.txt",
    "git checkout feature/issue-1-demo",
    'git commit -m "$(date)"',
    "harness health .",
    "git -C .harness status",
    "git -C .harness diff --stat",
    "ls -la .harness/",
    "cp a.txt .harness/notes.txt",
    "python3 .harness/orchestration/coordinator.py --repo . ledger validate",
    "tar -xf /nonexistent/x.tar -C build",
    "git checkout HEAD~1 -- harness/",
]

# Fail closed by design: any non-reading program whose argument names the file is refused, so a
# note that merely mentions it goes through --body-file or a reading tool instead.
NAMED_WITHOUT_WRITE = [
    'gh issue comment 1 --body "edit project.json"',
    "python3 -m json.tool .harness/project.json",
]


@pytest.mark.parametrize("command", SHELL_READS)
def test_reading_or_unrelated_writes_are_allowed(project: Path, command: str) -> None:
    assert _bash(project, command) is None


@pytest.mark.parametrize("command", NAMED_WITHOUT_WRITE)
def test_naming_the_file_without_a_write_fails_closed(
    project: Path, command: str
) -> None:
    assert _bash(project, command) == "project-json-shell-write"


def test_the_decision_ignores_profile_role_and_environment(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = project / ".harness" / "project.json"
    outcomes = []
    for profile in ("minimal", "standard", "strict"):
        config.write_text(
            json.dumps({"hook_policy": {"profile": profile}}), encoding="utf-8"
        )
        for value in ("minimal", "standard", "strict", ""):
            monkeypatch.setenv("HOOK_PROFILE", value)
            outcomes.append(
                (
                    _bash(project, "echo x > .harness/project.json"),
                    _edit(project, ".claude/settings.json", "a", "b"),
                    _edit(project, config, f'"{profile}"', '"other"'),
                    _bash(project, "cat .harness/project.json"),
                )
            )

    assert set(outcomes) == {
        ("project-json-shell-write", "agent-config-edit", "hook-policy-changed", None)
    }


# -- the installed hook: envelope, exit codes and reason codes ------------------------------------

needs_bash = pytest.mark.skipif(BASH is None, reason="the hooks are bash scripts")


def _installed(tmp_path: Path, policy: object | None = POLICY) -> Path:
    shutil.copytree(
        HOOKS,
        tmp_path / ".claude" / "hooks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    config = tmp_path / ".harness" / "project.json"
    config.parent.mkdir()
    document: dict[str, object] = {"qa_gate_commands": []}
    if policy is not None:
        document["hook_policy"] = policy
    config.write_text(json.dumps(document), encoding="utf-8")
    return tmp_path


def _run(
    project: Path, payload: object, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [str(BASH), str(project / ".claude" / "hooks" / "protect-hook-policy.sh")],
        input=text,
        capture_output=True,
        text=True,
        env=env,
        cwd=project,
        check=False,
        timeout=30,
    )


@needs_bash
@pytest.mark.parametrize(
    "policy", [None, {"profile": "minimal"}, {"profile": "standard"}, POLICY]
)
def test_the_hook_blocks_in_every_profile_and_answers_with_a_registered_verdict(
    tmp_path: Path, policy: object
) -> None:
    project = _installed(tmp_path, policy)
    env = {**os.environ, "HOOK_PROFILE": "minimal"}
    cases = {
        "project-json-shell-write": {
            "tool_name": "Bash",
            "tool_input": {"command": "sed -i s/a/b/ .harness/project.json"},
        },
        "agent-config-edit": {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(project / ".claude/settings.json"),
                "content": "{}",
            },
        },
        "hook-policy-changed": {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(project / ".harness/project.json"),
                "content": json.dumps({"hook_policy": {"profile": "minimal"}}),
            },
        },
        "hook-policy-unparseable": {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(project / ".harness/project.json"),
                "content": "{",
            },
        },
    }

    if policy == {"profile": "minimal"}:
        # The edit must differ from the policy on disk: a change away from minimal.
        cases["hook-policy-changed"]["tool_input"] = {
            "file_path": str(project / ".harness/project.json"),
            "content": json.dumps({"hook_policy": {"profile": "strict"}}),
        }
    for reason, payload in cases.items():
        done = _run(project, payload, env)
        assert done.returncode == 2, reason
        verdict = parse_hook_response(done.stderr)
        assert (verdict["hook_id"], verdict["decision"], verdict["reason_code"]) == (
            "protect-hook-policy",
            "block",
            reason,
        )
        assert f"[{reason}]" in done.stderr.splitlines()[0]


@needs_bash
def test_the_hook_allows_an_edit_that_keeps_the_policy(tmp_path: Path) -> None:
    project = _installed(tmp_path)
    allowed = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(project / ".harness/project.json"),
            "content": json.dumps({"hook_policy": POLICY, "language": "ru"}),
        },
    }

    done = _run(project, allowed)

    assert done.returncode == 0
    assert done.stdout == ""
    assert parse_hook_response(done.stderr)["decision"] == "allow"
    assert (
        _run(
            project,
            {
                "tool_name": "Bash",
                "tool_input": {"command": "cat .harness/project.json"},
            },
        ).returncode
        == 0
    )


@needs_bash
def test_the_hook_blocks_an_unreadable_payload_and_a_crash(tmp_path: Path) -> None:
    project = _installed(tmp_path)

    garbled = _run(project, "not json")
    assert garbled.returncode == 2
    assert parse_hook_response(garbled.stderr)["reason_code"] == "hook-policy-protected"
    (project / ".claude" / "hooks" / "protect_hook_policy.py").write_text(
        "raise SystemExit(1)\n", encoding="utf-8"
    )
    crashed = _run(project, {"tool_input": {"command": "true"}})
    assert crashed.returncode == 2
    assert parse_hook_response(crashed.stderr)["reason_code"] == "hook-policy-protected"
    (project / ".claude" / "hooks" / "protect_hook_policy.py").unlink()
    missing = _run(project, {"tool_input": {"command": "true"}})
    assert missing.returncode == 2


@needs_bash
def test_the_hook_is_wired_for_shell_and_file_tools() -> None:
    template = json.loads(
        (HOOKS.parent / "settings.local.json.tmpl").read_text(encoding="utf-8")
    )
    wired = {
        entry["matcher"]: [hook["command"] for hook in entry["hooks"]]
        for entry in template["hooks"]["PreToolUse"]
    }

    assert any("protect-hook-policy.sh" in command for command in wired["Bash"])
    assert any(
        "protect-hook-policy.sh" in command
        for command in wired["Write|Edit|MultiEdit|NotebookEdit"]
    )


def test_the_strict_lexer_records_redirection_targets() -> None:
    pr_commands = _load("pr_commands.py")

    assert pr_commands.strict_redirections("echo x > a.txt 2>> b.txt < c.txt") == [
        (">", "a.txt"),
        (">>", "b.txt"),
        ("<", "c.txt"),
    ]
    assert pr_commands.strict_redirections("cat <<'EOF' > out\nbody\nEOF") == [
        ("<<", "EOF"),
        (">", "out"),
    ]
    assert pr_commands.strict_redirections("echo $HOME > out") is None
