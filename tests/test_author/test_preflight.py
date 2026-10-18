"""Behavior tests for the fail-closed test-author preflight."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import cast

import pytest

from tests.conftest import find_bash

ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = ROOT / "harness" / "project" / "hooks" / "test-author-preflight.sh"
GUARD = ROOT / "harness" / "project" / "hooks" / "test-author-guard.py"
AGENT = ROOT / "harness" / "project" / "agents" / "test-author.md"


def _project(
    tmp_path: Path, *, test_paths: object = ["tests/**"], name: str = "project"
) -> Path:
    """Create a trusted project with a controllable isolation probe."""
    project = tmp_path / name
    (project / ".harness").mkdir(parents=True)
    hooks = project / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (project / ".claude" / "agents").mkdir()
    (project / "tests").mkdir()
    (project / "src").mkdir()
    config = {
        "language": "en",
        "base_branch": "main",
        "branch_pattern": "^feature/.+",
        "qa_gate_commands": [],
        "test_paths": test_paths,
        "test_runner": {"command": ["python3", "-m", "pytest", "{test_id}"]},
    }
    (project / ".harness" / "project.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    for source, target in (
        (GUARD, hooks / "test-author-guard.py"),
        (PREFLIGHT, hooks / "test-author-preflight.sh"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
    (project / ".claude" / "agents" / "test-author.md").write_bytes(AGENT.read_bytes())
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {"projects": {str(project.resolve()): {"hasTrustDialogAccepted": True}}}
        ),
        encoding="utf-8",
    )
    return project


def _runner(project: Path, answer: str) -> None:
    """Install a runner stub with the probe answer used by the preflight seam."""
    path = project / ".claude" / "hooks" / "run-test.sh"
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{answer}'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(
    project: Path, *, env_overrides: dict[str, str | None] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run preflight and keep its machine-readable output bounded."""
    environment = dict(
        os.environ,
        CLAUDE_PROJECT_DIR=str(project),
        HOME=str(project.parent),
        USERPROFILE=str(project.parent),
    )
    environment.pop("CLAUDE_CONFIG_DIR", None)
    for key, value in (env_overrides or {}).items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    return subprocess.run(
        [find_bash(), str(project / ".claude" / "hooks" / "test-author-preflight.sh")],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def _answer(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """Assert the preflight process contract and parse its only output line."""
    assert result.returncode == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    return cast(dict[str, object], json.loads(result.stdout))


def test_preflight_activates_only_after_probe_and_scoped_hook_contract_passes(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _runner(project, '{"status":"ok"}')

    answer = _answer(_run(project))

    assert answer == {"status": "ok"}


def test_preflight_requires_a_trusted_claude_project_identity(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _runner(project, '{"status":"ok"}')

    assert _answer(_run(project, env_overrides={"CLAUDE_PROJECT_DIR": None})) == {
        "status": "disabled",
        "reason": "project_untrusted",
    }


def test_preflight_fails_closed_without_claude_workspace_trust(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _runner(project, '{"status":"ok"}')
    state = tmp_path / ".claude.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["projects"][str(project.resolve())]["hasTrustDialogAccepted"] = False
    state.write_text(json.dumps(data), encoding="utf-8")

    assert _answer(_run(project)) == {
        "status": "disabled",
        "reason": "project_untrusted",
    }


def test_preflight_requires_the_installed_test_author_agent(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / ".claude" / "agents" / "test-author.md").unlink()
    _runner(project, '{"status":"ok"}')

    assert _answer(_run(project)) == {
        "status": "disabled",
        "reason": "agent_missing",
    }


def test_preflight_requires_an_enabled_scoped_hook(tmp_path: Path) -> None:
    project = _project(tmp_path)
    agent = project / ".claude" / "agents" / "test-author.md"
    agent.write_text(
        agent.read_text(encoding="utf-8").replace(
            "  PreToolUse:\n", "  PreToolUse: {}\n"
        ),
        encoding="utf-8",
    )
    _runner(project, '{"status":"ok"}')

    assert _answer(_run(project)) == {
        "status": "disabled",
        "reason": "scoped_hook_unsupported",
    }


@pytest.mark.parametrize("variable", ["CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SAFE_MODE"])
def test_preflight_disables_unsupported_claude_code_modes(
    tmp_path: Path, variable: str
) -> None:
    project = _project(tmp_path)
    _runner(project, '{"status":"ok"}')

    assert _answer(_run(project, env_overrides={variable: "1"})) == {
        "status": "disabled",
        "reason": "claude_code_unsupported",
    }


def test_missing_test_paths_disables_test_author(tmp_path: Path) -> None:
    project = _project(tmp_path, test_paths=None)
    data = json.loads((project / ".harness" / "project.json").read_text())
    data.pop("test_paths")
    (project / ".harness" / "project.json").write_text(json.dumps(data))
    _runner(project, '{"status":"ok"}')

    assert _answer(_run(project)) == {
        "status": "disabled",
        "reason": "test_paths_missing",
    }


def test_unavailable_isolation_disables_test_author_without_detail_leak(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _runner(
        project,
        '{"status":"unavailable","reason":"isolation_unavailable","detail":"bwrap_missing"}',
    )

    answer = _answer(_run(project))

    assert answer == {"status": "disabled", "reason": "isolation_unavailable"}


def test_runner_configuration_is_required_for_activation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    data = json.loads((project / ".harness" / "project.json").read_text())
    data.pop("test_runner")
    (project / ".harness" / "project.json").write_text(json.dumps(data))
    _runner(project, '{"status":"ok"}')

    assert _answer(_run(project)) == {
        "status": "disabled",
        "reason": "test_runner_missing",
    }


@pytest.mark.parametrize("bad_answer", ["not-json", '{"status":"disabled"}'])
def test_unverifiable_guard_or_probe_fails_closed(
    tmp_path: Path, bad_answer: str
) -> None:
    project = _project(tmp_path)
    _runner(project, '{"status":"ok"}')
    if bad_answer == "not-json":
        (project / ".claude" / "hooks" / "test-author-guard.py").write_text(
            "print('not-json')\n", encoding="utf-8"
        )
    else:
        (project / ".claude" / "hooks" / "run-test.sh").write_text(
            "#!/bin/sh\nprintf '%s\\n' '{\"status\":\"disabled\"}'\n",
            encoding="utf-8",
        )
        (project / ".claude" / "hooks" / "run-test.sh").chmod(
            (project / ".claude" / "hooks" / "run-test.sh").stat().st_mode
            | stat.S_IXUSR
        )

    answer = _answer(_run(project))

    expected = (
        "guard_hook_unavailable"
        if bad_answer == "not-json"
        else "isolation_unavailable"
    )
    assert answer == {"status": "disabled", "reason": expected}


def _trust_state(project: Path, entry: object) -> dict[str, object]:
    """A `.claude.json` body with one project entry."""
    return {"projects": {str(project.resolve()): entry}}


@pytest.mark.parametrize(
    "state",
    [
        "missing-file",
        "invalid-json",
        "not-an-object",
        "projects-not-an-object",
        "other-project-only",
        "entry-not-an-object",
        "trust-is-a-string",
        "trust-not-accepted",
        "entry-without-trust",
    ],
)
def test_every_unverifiable_trust_state_fails_closed(
    tmp_path: Path, state: str
) -> None:
    project = _project(tmp_path)
    _runner(project, '{"status":"ok"}')
    path = tmp_path / ".claude.json"
    bodies: dict[str, object] = {
        "invalid-json": "{not json",
        "not-an-object": "[]",
        "projects-not-an-object": json.dumps({"projects": []}),
        "other-project-only": json.dumps(
            {"projects": {"/elsewhere": {"hasTrustDialogAccepted": True}}}
        ),
        "entry-not-an-object": json.dumps(_trust_state(project, True)),
        "trust-is-a-string": json.dumps(
            _trust_state(project, {"hasTrustDialogAccepted": "true"})
        ),
        "trust-not-accepted": json.dumps(
            _trust_state(project, {"hasTrustDialogAccepted": False})
        ),
        "entry-without-trust": json.dumps(_trust_state(project, {})),
    }
    if state == "missing-file":
        path.unlink()
    else:
        path.write_text(str(bodies[state]), encoding="utf-8")

    assert _answer(_run(project)) == {
        "status": "disabled",
        "reason": "project_untrusted",
    }


def test_claude_config_dir_overrides_the_default_trust_file(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _runner(project, '{"status":"ok"}')
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    overrides: dict[str, str | None] = {"CLAUDE_CONFIG_DIR": str(config_dir)}

    # The default file trusts the project, but the configured directory has no decision.
    assert _answer(_run(project, env_overrides=overrides)) == {
        "status": "disabled",
        "reason": "project_untrusted",
    }

    (config_dir / ".claude.json").write_text(
        json.dumps(_trust_state(project, {"hasTrustDialogAccepted": True})),
        encoding="utf-8",
    )
    assert _answer(_run(project, env_overrides=overrides)) == {"status": "ok"}


def test_project_path_with_a_space_still_activates(tmp_path: Path) -> None:
    project = _project(tmp_path, name="my project")
    _runner(project, '{"status":"ok"}')

    assert _answer(_run(project)) == {"status": "ok"}


def test_unknown_reasons_from_guard_or_probe_are_not_passed_through(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _runner(
        project,
        '{"status":"unavailable","reason":"secret detail","detail":"/home/user"}',
    )
    probe = _answer(_run(project))

    guard = project / ".claude" / "hooks" / "test-author-guard.py"
    guard.write_text(
        "import json\nprint(json.dumps({'status': 'disabled', 'reason': 'leak /home/user'}))\n",
        encoding="utf-8",
    )
    _runner(project, '{"status":"ok"}')
    config = _answer(_run(project))

    assert probe == {"status": "disabled", "reason": "isolation_unavailable"}
    assert config == {"status": "disabled", "reason": "guard_hook_unavailable"}


def test_trust_given_only_to_a_parent_directory_keeps_the_author_disabled(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _runner(project, '{"status":"ok"}')
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {"projects": {str(tmp_path.resolve()): {"hasTrustDialogAccepted": True}}}
        ),
        encoding="utf-8",
    )

    # Claude Code may trust the project through its parent, but without an entry for the exact
    # path the preflight stays on the safe side (docs/agents/test-author.md, section 3).
    assert _answer(_run(project)) == {
        "status": "disabled",
        "reason": "project_untrusted",
    }


@pytest.mark.parametrize("reason", ["config_invalid", "write_zone_rejected"])
def test_preflight_preserves_runner_mount_refusal(tmp_path: Path, reason: str) -> None:
    project = _project(tmp_path)
    _runner(
        project, json.dumps({"status": "unavailable", "reason": reason, "detail": None})
    )
    assert _answer(_run(project)) == {"status": "disabled", "reason": reason}
