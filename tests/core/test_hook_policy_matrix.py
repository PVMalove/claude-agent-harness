"""Матрица 3 профиля x 4 роли для критичных guard-ов (issue #794, ADR 0024, раздел 2a).

Ни один критичный guard не отключается в `minimal`, а `HOOK_PROFILE` и роль, переданные окружением
или payload, не меняют ни одного решения: профиль берётся только из `.harness/project.json`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from harness.orchestration.hook_policy import parse_hook_response

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "harness" / "project" / "hooks"
AGENT = ROOT / "harness" / "project" / "agents" / "test-author.md"
BASH = shutil.which("bash")
PROFILES = ("minimal", "standard", "strict")
ROLES = ("developer", "review", "qa", "coordinator")

pytestmark = pytest.mark.skipif(BASH is None, reason="the hooks are bash scripts")

# hook_id -> (payload that must be blocked, reason_code, payload that must be allowed). `{P}` is the
# project root.
CRITICAL_GUARDS: dict[str, tuple[dict[str, object], str, dict[str, object]]] = {
    "block-dangerous-git": (
        {"tool_input": {"command": "git reset --hard"}},
        "dangerous-git",
        {"tool_input": {"command": "git status"}},
    ),
    "block-direct-master": (
        {"cwd": "{P}", "tool_input": {"command": "git commit -m x"}},
        "direct-commit",
        {"cwd": "{P}", "tool_input": {"command": "git status"}},
    ),
    "block-pr-merge": (
        {"tool_input": {"command": "gh pr merge 1"}},
        "pr-merge",
        {"tool_input": {"command": "echo hello"}},
    ),
    "block-public-attribution": (
        {
            "tool_input": {
                "command": 'git commit -m "feat: x" -m "Co-Authored-By: Claude <noreply@anthropic.com>"'
            }
        },
        "public-attribution",
        {"tool_input": {"command": 'git commit -m "feat: add parser"'}},
    ),
    "check-branch-name": (
        {"tool_input": {"command": "git checkout -b badname"}},
        "branch-name",
        {"tool_input": {"command": "git status"}},
    ),
    "check-worktree-branch-name": (
        {"tool_input": {"name": "badname"}},
        "worktree-branch-name",
        {"tool_input": {}},
    ),
    "block-scratch-outside-docs-tasks": (
        {"tool_input": {"file_path": "{P}/../outside-scratch.md"}},
        "scratch-location",
        {"tool_input": {"file_path": "{P}/docs/tasks/note.md"}},
    ),
    "require-qa-gate": (
        {"cwd": "{P}", "tool_input": {"command": "gh pr create --title x"}},
        "qa-gate-missing",
        {"cwd": "{P}", "tool_input": {"command": "echo hi"}},
    ),
    "protect-hook-policy": (
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "{P}/.claude/settings.json", "content": "{}"},
        },
        "agent-config-edit",
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "{P}/docs/note.md", "content": "x"},
        },
    ),
}


def _fill(value: object, project: Path) -> object:
    if isinstance(value, str):
        return value.replace("{P}", str(project))
    if isinstance(value, dict):
        return {key: _fill(item, project) for key, item in value.items()}
    return value


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(project), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def projects(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Path]]:
    """One installed project per profile: git on `master`, every hook, the test-author agent."""
    made: dict[str, Path] = {}
    for profile in PROFILES:
        project = tmp_path_factory.mktemp(profile) / "project"
        (project / ".harness").mkdir(parents=True)
        (project / "docs" / "tasks").mkdir(parents=True)
        (project / ".claude" / "agents").mkdir(parents=True)
        shutil.copytree(
            HOOKS,
            project / ".claude" / "hooks",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (project / ".claude" / "agents" / "test-author.md").write_bytes(
            AGENT.read_bytes()
        )
        (project / "tests").mkdir()
        (project / ".harness" / "project.json").write_text(
            json.dumps(
                {
                    "language": "en",
                    "base_branch": "master",
                    "branch_pattern": "^feature/issue-[0-9]+-.+",
                    "qa_gate_commands": ["true"],
                    "test_paths": ["tests/**"],
                    "test_runner": {
                        "command": ["python3", "-m", "pytest", "{test_id}"]
                    },
                    "hook_policy": {"profile": profile},
                }
            ),
            encoding="utf-8",
        )
        _git(project, "init", "-q", "-b", "master")
        _git(project, "config", "user.email", "t@example.invalid")
        _git(project, "config", "user.name", "t")
        _git(project, "add", "-A")
        _git(project, "commit", "-q", "-m", "init")
        made[profile] = project
    yield made


def _environment(project: Path, role: str, profile: str) -> dict[str, str]:
    """The project directory plus every profile and role input an agent or a runtime could set."""
    others = [other for other in PROFILES if other != profile]
    return {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "HOOK_PROFILE": others[0],
        "HARNESS_HOOK_PROFILE": others[1],
        "HARNESS_ROLE": role,
        "CLAUDE_ROLE": role,
        "HOOK_ROLE": role,
    }


def _run(
    project: Path, hook_id: str, payload: object, role: str, profile: str
) -> subprocess.CompletedProcess[str]:
    body = {**payload, "role": role, "agent_type": role}  # type: ignore[dict-item]
    return subprocess.run(
        [str(BASH), str(project / ".claude" / "hooks" / f"{hook_id}.sh")],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env=_environment(project, role, profile),
        cwd=project,
        check=False,
        timeout=60,
    )


def _decision(done: subprocess.CompletedProcess[str]) -> tuple[int, str, str]:
    verdict = parse_hook_response(done.stderr)
    return done.returncode, str(verdict["decision"]), str(verdict["reason_code"])


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("profile", PROFILES)
def test_every_critical_guard_decides_the_same_in_every_profile_and_role(
    projects: dict[str, Path], profile: str, role: str
) -> None:
    project = projects[profile]
    jobs: list[tuple[str, str, object]] = []
    for hook_id, (block, _, allow) in CRITICAL_GUARDS.items():
        jobs.append((hook_id, "block", _fill(block, project)))
        jobs.append((hook_id, "allow", _fill(allow, project)))

    with ThreadPoolExecutor(max_workers=6) as pool:
        done = list(
            pool.map(lambda job: _run(project, job[0], job[2], role, profile), jobs)
        )

    for (hook_id, expected, _), result in zip(jobs, done, strict=True):
        label = f"{hook_id}/{expected} in {profile} as {role}: {result.stderr}"
        code, decision, reason = _decision(result)
        assert decision == expected, label
        if expected == "block":
            assert (code, reason) == (2, CRITICAL_GUARDS[hook_id][1]), label
        else:
            assert (code, reason) == (0, "ok"), label
        assert parse_hook_response(result.stderr)["profile"] == profile, label


def test_the_matrix_covers_every_critical_guard() -> None:
    spec = importlib.util.spec_from_file_location(
        "hook_response_matrix", HOOKS / "hook_response.py"
    )
    assert spec is not None and spec.loader is not None
    hook_response = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = hook_response
    spec.loader.exec_module(hook_response)

    critical = {
        hook for hook, (_, is_critical) in hook_response.HOOKS.items() if is_critical
    }

    assert critical == set(CRITICAL_GUARDS)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("profile", PROFILES)
def test_the_scoped_test_author_guard_denies_in_every_profile_and_role(
    projects: dict[str, Path], profile: str, role: str
) -> None:
    project = projects[profile]
    guard = project / ".claude" / "hooks" / "test-author-guard.py"

    def ask(file_path: str) -> subprocess.CompletedProcess[str]:
        payload = {
            "agent_type": "test-author",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": file_path},
            "role": role,
        }
        return subprocess.run(
            [sys.executable, str(guard)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=_environment(project, role, profile),
            check=False,
            timeout=60,
        )

    denied = ask("src/price.py")
    allowed = ask("tests/test_price.py")

    assert denied.returncode == 0
    answer = json.loads(denied.stdout)["hookSpecificOutput"]
    assert answer["permissionDecision"] == "deny"
    assert (allowed.returncode, allowed.stdout) == (0, "")


@pytest.mark.parametrize("profile", PROFILES)
def test_minimal_downgrades_only_the_behavioral_hook(
    projects: dict[str, Path], profile: str
) -> None:
    """Contrast: the profile is read, so the unchanged critical results above are not an accident."""
    project = projects[profile]
    command = "python3 -m pytest -q tests"
    payload = {"tool_input": {"command": command}}

    done = _run(project, "require-bounded-check", payload, "developer", profile)

    code, decision, _ = _decision(done)
    assert (code, decision) == ((0, "warn") if profile == "minimal" else (2, "block"))


_UNBOUNDED_CHECK = {"tool_input": {"command": "python -m pytest -q tests"}}


def _escalated_project(
    tmp_path: Path, projects: dict[str, Path], policy: object
) -> Path:
    """A copy of the `minimal` project whose hook_policy is `policy`."""
    project = tmp_path / "project"
    shutil.copytree(
        projects["minimal"], project, ignore=shutil.ignore_patterns("__pycache__")
    )
    config = project / ".harness" / "project.json"
    document = json.loads(config.read_text(encoding="utf-8"))
    document["hook_policy"] = policy
    config.write_text(json.dumps(document), encoding="utf-8")
    return project


@pytest.mark.parametrize("role", ROLES)
def test_an_escalation_applies_to_every_hook_decision_for_every_role(
    tmp_path: Path, projects: dict[str, Path], role: str
) -> None:
    """The hooks cannot see role or path, so an escalation of a minimal project applies to every
    call: the behavioural hook blocks again and every critical guard still blocks."""
    plain = _escalated_project(tmp_path / "plain", projects, {"profile": "minimal"})
    escalated = _escalated_project(
        tmp_path / "escalated",
        projects,
        {
            "profile": "minimal",
            "escalations": [{"roles": ["qa"], "profile": "standard"}],
        },
    )

    soft = _run(plain, "require-bounded-check", _UNBOUNDED_CHECK, role, "minimal")
    hard = _run(escalated, "require-bounded-check", _UNBOUNDED_CHECK, role, "minimal")

    assert _decision(soft) == (0, "warn", "unbounded-check"), soft.stderr
    assert _decision(hard) == (2, "block", "unbounded-check"), hard.stderr
    assert parse_hook_response(hard.stderr)["profile"] == "standard"
    for hook_id, (block, reason, _) in CRITICAL_GUARDS.items():
        done = _run(escalated, hook_id, _fill(block, escalated), role, "minimal")
        assert _decision(done) == (2, "block", reason), f"{hook_id}: {done.stderr}"


@pytest.mark.parametrize("role", ROLES)
def test_a_legacy_project_without_hook_policy_keeps_the_standard_decisions(
    tmp_path: Path, projects: dict[str, Path], role: str
) -> None:
    """A project installed before `hook_policy` has no key: the profile is `standard` from the
    default source and the blocking hook set is the one of an explicit `standard` project."""
    legacy = tmp_path / "project"
    shutil.copytree(
        projects["standard"], legacy, ignore=shutil.ignore_patterns("__pycache__")
    )
    config = legacy / ".harness" / "project.json"
    document = json.loads(config.read_text(encoding="utf-8"))
    del document["hook_policy"]
    config.write_text(json.dumps(document), encoding="utf-8")

    for hook_id, (block, reason, allow) in CRITICAL_GUARDS.items():
        denied = _run(legacy, hook_id, _fill(block, legacy), role, "standard")
        allowed = _run(legacy, hook_id, _fill(allow, legacy), role, "standard")
        assert _decision(denied) == (2, "block", reason), f"{hook_id}: {denied.stderr}"
        assert _decision(allowed) == (0, "allow", "ok"), f"{hook_id}: {allowed.stderr}"
        assert parse_hook_response(denied.stderr)["profile"] == "standard"
    behavioural = _run(
        legacy, "require-bounded-check", _UNBOUNDED_CHECK, role, "standard"
    )
    assert _decision(behavioural) == (2, "block", "unbounded-check"), behavioural.stderr
    assert parse_hook_response(behavioural.stderr)["profile"] == "standard"


def test_the_scoped_test_author_guard_stays_outside_the_envelope_by_decision() -> None:
    """ADR 0024 section 2a lists it as critical, but it answers with its own soft refusal (ADR 0014)
    inside the agent description, so it is wired outside the envelope; the matrix above proves it
    denies in every profile and role."""
    spec = importlib.util.spec_from_file_location(
        "hook_response_scoped", HOOKS / "hook_response.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert "test-author-guard" not in module.HOOKS
    assert "test-author-guard.py" in AGENT.read_text(encoding="utf-8")
