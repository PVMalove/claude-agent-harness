"""The require hook lets a PR be created on the qa-gate marker or on the coordinator's accepted QA
evidence for exactly the checkout's HEAD; anything else keeps blocking."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

HOOK = (
    Path(__file__).resolve().parents[2]
    / "harness"
    / "project"
    / "hooks"
    / "qa-gate-state.py"
)
ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills" / "first-party" / "engineering"
DESCRIPTIONS = ROOT / "docs" / "skills"
BRANCH = "feature/issue-7-demo"
QA = "make verify"
# The marker's state is `<HEAD>:<hash of git diff HEAD>`; a clean tree hashes the empty diff.
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
# Stands in for the coordinator CLI: replays a canned `qa evidence` answer and logs its argv.
STUB = """\
import json, sys, time
from pathlib import Path

here = Path(__file__).parent
(here / "argv.json").write_text(json.dumps(sys.argv[1:]))
answer = json.loads((here / "answer.json").read_text())
time.sleep(answer.get("sleep", 0))
print(answer["stdout"])
sys.exit(answer["code"])
"""


def git(cwd: Path, *args: str) -> str:
    """Run Git in ``cwd`` and return its stdout."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class Project:
    """A main checkout with a linked worktree on the issue branch and a stub coordinator."""

    main: Path
    linked: Path

    @property
    def head(self) -> str:
        """HEAD of the linked worktree."""
        return git(self.linked, "rev-parse", "HEAD")

    def answer(self, code: int = 0, sleep: float = 0, **evidence: object) -> None:
        """Set what the stub coordinator replies, after ``sleep`` seconds."""
        stdout = json.dumps(evidence) if evidence else "{}"
        (self.main / ".harness" / "orchestration" / "answer.json").write_text(
            json.dumps({"code": code, "sleep": sleep, "stdout": stdout})
        )

    def configure(self, commands: list[str] | None) -> None:
        """Set the project's ``qa_gate_commands``; ``None`` leaves the key out."""
        config = {} if commands is None else {"qa_gate_commands": commands}
        (self.main / ".harness" / "project.json").write_text(json.dumps(config))

    def accepted(
        self,
        candidate: str | None = None,
        outcome: str = "completed",
        checks: list[dict[str, str]] | None = None,
    ) -> None:
        """Reply with a QA report like the real ``qa evidence`` answer."""
        self.answer(
            candidate_commit=candidate or self.head,
            qa_report={
                "outcome": outcome,
                "checks_run": checks or [{"command": QA, "result": "pass"}],
            },
        )

    def require(self, branch: str = BRANCH) -> subprocess.CompletedProcess[str]:
        """Run the hook as a PR-creating Bash call; the linked worktree has no project dir."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        payload = {"tool_input": {"command": f"gh pr create --head {branch}"}}
        return subprocess.run(
            [sys.executable, str(HOOK), "require"],
            cwd=self.linked,
            env=env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )

    def mark(self) -> subprocess.CompletedProcess[str]:
        """Run the post-QA mark hook from the linked worktree, without a project dir."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        return subprocess.run(
            [sys.executable, str(HOOK), "mark"],
            cwd=self.linked,
            env=env,
            input=json.dumps({"tool_input": {"command": QA}}),
            capture_output=True,
            text=True,
            check=False,
        )

    def argv(self) -> list[str]:
        """The arguments the stub coordinator received."""
        path = self.main / ".harness" / "orchestration" / "argv.json"
        return json.loads(path.read_text())  # type: ignore[no-any-return]


@pytest.fixture
def project(tmp_path: Path) -> Project:
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    (main / ".gitignore").write_text(".harness/\n.claude/\n")
    (main / "a.txt").write_text("a\n")
    git(main, "add", ".")
    git(main, "commit", "-q", "-m", "init")
    orchestration = main / ".harness" / "orchestration"
    orchestration.mkdir(parents=True)
    (main / ".harness" / "project.json").write_text(
        json.dumps({"qa_gate_commands": [QA]})
    )
    (orchestration / "coordinator.py").write_text(STUB)
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "-q", "-b", BRANCH, str(linked))
    (linked / "b.txt").write_text("b\n")
    git(linked, "add", ".")
    git(linked, "commit", "-q", "-m", "work")
    found = Project(main, linked)
    found.accepted()
    return found


def test_accepted_qa_of_the_same_sha_on_a_clean_tree_opens_the_pr(
    project: Project,
) -> None:
    assert project.require().returncode == 0
    argv = project.argv()
    assert argv[argv.index("--ticket") + 1] == "#7"
    assert argv[argv.index("--branch") + 1] == BRANCH
    assert argv[argv.index("--candidate-commit") + 1] == project.head


@pytest.mark.parametrize("result", ["pass", "passed"])
def test_accepted_success_spellings_allow_publication(
    project: Project, result: str
) -> None:
    project.accepted(checks=[{"command": QA, "result": result}])
    assert project.require().returncode == 0


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(lambda p: p.accepted(candidate="0" * 40), id="other-sha"),
        pytest.param(lambda p: p.accepted(outcome="failed"), id="failed-outcome"),
        pytest.param(lambda p: p.accepted(outcome="blocked"), id="blocked-outcome"),
        pytest.param(
            lambda p: p.accepted(checks=[{"command": QA, "result": "fail"}]),
            id="failed-check",
        ),
        pytest.param(
            lambda p: p.accepted(checks=[{"command": "make lint", "result": "pass"}]),
            id="other-command",
        ),
        # Unaccepted, tampered-hash and missing evidence all make the CLI refuse.
        pytest.param(lambda p: p.answer(code=1), id="cli-refusal"),
        pytest.param(lambda p: p.answer(code=0), id="empty-answer"),
    ],
)
def test_evidence_that_is_not_accepted_green_for_head_keeps_blocking(
    project: Project, reply: Callable[[Project], None]
) -> None:
    reply(project)
    result = project.require()
    assert result.returncode == 2
    assert "qa-gate" in result.stderr


def test_a_dirty_tree_keeps_blocking_even_with_accepted_evidence(
    project: Project,
) -> None:
    (project.linked / "b.txt").write_text("changed\n")
    assert project.require().returncode == 2


def test_without_a_coordinator_the_marker_path_is_unchanged(project: Project) -> None:
    (project.main / ".harness" / "orchestration" / "coordinator.py").unlink()
    assert project.require().returncode == 2
    marker = project.linked / ".claude" / ".qa-gate"
    marker.mkdir(parents=True)
    (marker / "passed").write_text(f"{project.head}:{EMPTY_BLOB}\n")
    assert project.require().returncode == 0


def test_a_branch_without_an_issue_number_has_no_evidence_path(
    project: Project,
) -> None:
    git(project.linked, "branch", "-m", "scratch")
    assert project.require(branch="scratch").returncode == 2


def test_every_qa_gate_command_needs_a_passing_check(project: Project) -> None:
    project.configure(["make lint", QA])
    project.accepted()
    assert project.require().returncode == 2
    project.accepted(
        checks=[
            {"command": "make lint", "result": "pass"},
            {"command": QA, "result": "pass"},
        ]
    )
    assert project.require().returncode == 0


def test_a_project_without_qa_gate_commands_has_no_evidence_path(
    project: Project,
) -> None:
    project.configure(None)
    assert project.require().returncode == 2


def test_a_coordinator_that_does_not_answer_in_time_blocks(project: Project) -> None:
    spec = importlib.util.spec_from_file_location("qa_gate_state", HOOK)
    assert spec is not None and spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HOOK.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HOOK.parent))
    project.accepted()
    project.answer(sleep=5, candidate_commit=project.head, qa_report={})
    setattr(module, "EVIDENCE_TIMEOUT_SECONDS", 0.5)
    assert module.coordinator_evidence(project.main, project.linked) is False


def test_the_marker_is_written_in_a_linked_worktree_without_a_project_dir(
    project: Project,
) -> None:
    (project.main / ".harness" / "orchestration" / "coordinator.py").unlink()
    assert project.mark().returncode == 0
    assert (project.linked / ".claude" / ".qa-gate" / "passed").is_file()
    assert project.require().returncode == 0


def skill_record_command(skill: str) -> str:
    """The exact command the skill gives for recording the qa-gate pass marker."""
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    commands: list[str] = re.findall(
        r"`(bash [^`]*record-qa-gate-pass\.sh[^`]*)`", text
    )
    (command,) = commands
    return command


def runtime_require_command(template: str) -> str:
    """The `require-qa-gate` Bash hook command a runtime's hooks template installs."""
    path = HOOK.parents[1] / template
    groups = json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    commands: list[str] = [
        hook["command"]
        for group in groups
        if group["matcher"] == "Bash"
        for hook in group["hooks"]
        if "require-qa-gate" in hook["command"]
    ]
    (command,) = commands
    return command


@pytest.mark.parametrize("skill", ["qa-gate", "to-pull-requests"])
@pytest.mark.parametrize(
    ("template", "project_dir"),
    [
        pytest.param("settings.local.json.tmpl", True, id="first-runtime"),
        pytest.param("codex-hooks.json.tmpl", False, id="second-runtime"),
    ],
)
def test_the_skill_records_the_marker_that_opens_the_pr_in_each_runtime(
    project: Project, skill: str, template: str, project_dir: bool
) -> None:
    # Without a coordinator only the marker can open the PR, not accepted QA evidence.
    (project.main / ".harness" / "orchestration" / "coordinator.py").unlink()
    shutil.copytree(
        HOOK.parent,
        project.main / ".claude" / "hooks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    if project_dir:
        env["CLAUDE_PROJECT_DIR"] = str(project.main)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"gh pr create --head {BRANCH}"},
        "cwd": str(project.main),
    }

    def create_pr() -> subprocess.CompletedProcess[str]:
        """Run the runtime's require hook on a PR-creating call from the session root."""
        return subprocess.run(
            ["bash", "-c", runtime_require_command(template)],
            cwd=project.main,
            env=env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )

    without_marker = create_pr()
    recorded = subprocess.run(
        ["bash", "-c", skill_record_command(skill)],
        cwd=project.linked,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    after_marker = create_pr()

    assert without_marker.returncode == 2
    assert recorded.returncode == 0, recorded.stderr
    assert (project.linked / ".claude" / ".qa-gate" / "passed").is_file()
    assert after_marker.returncode == 0, after_marker.stderr


@pytest.mark.parametrize("skill", ["qa-gate", "to-pull-requests"])
def test_the_russian_description_gives_the_same_record_command(skill: str) -> None:
    description = (DESCRIPTIONS / f"{skill}.md").read_text(encoding="utf-8")

    assert skill_record_command(skill) in description
