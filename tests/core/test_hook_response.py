"""Конверт ответа управляемых hooks (issue #794): вердикт, коды возврата, профиль и критичность."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from harness.orchestration.contract import ContractError
from harness.orchestration import hook_policy
from harness.orchestration.hook_policy import parse_hook_response

HOOKS = Path(__file__).resolve().parents[2] / "harness" / "project" / "hooks"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="the hooks are bash scripts")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hook_response_unit", HOOKS / "hook_response.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hook_response = _load()
CRITICAL = sorted(h for h, (_, critical) in hook_response.HOOKS.items() if critical)
NON_CRITICAL = sorted(
    h for h, (_, critical) in hook_response.HOOKS.items() if not critical
)


def test_build_response_carries_the_six_fields() -> None:
    response = hook_response.build_response(
        "block-pr-merge", "standard", "block", "pr-merge", "remedy", "ref"
    )

    assert list(response) == list(hook_response.ENVELOPE_FIELDS)
    assert parse_hook_response(json.dumps(response)) == response


@pytest.mark.parametrize(
    "bad",
    [
        {"hook_id": "unknown-hook"},
        {"profile": "lax"},
        {"decision": "approve"},
        {"reason_code": "whatever"},
        {"profile": "Standard"},
    ],
)
def test_an_unregistered_value_is_a_contract_error(bad: dict[str, str]) -> None:
    fields = {
        "hook_id": "block-pr-merge",
        "profile": "standard",
        "decision": "block",
        "reason_code": "pr-merge",
        **bad,
    }
    with pytest.raises(ValueError):
        hook_response.build_response(**fields)
    line = json.dumps({**fields, "remedy": "", "evidence_ref": None})
    with pytest.raises(ContractError):
        parse_hook_response(line)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '{"hook_id": "block-pr-merge"}',
        json.dumps(
            {
                "hook_id": "block-pr-merge",
                "profile": "standard",
                "decision": "allow",
                "reason_code": "ok",
                "remedy": 1,
                "evidence_ref": None,
            }
        ),
        json.dumps(
            {
                "hook_id": "block-pr-merge",
                "profile": "standard",
                "decision": "allow",
                "reason_code": "ok",
                "remedy": "",
                "evidence_ref": 7,
            }
        ),
    ],
)
def test_a_malformed_verdict_is_a_contract_error(text: str) -> None:
    with pytest.raises(ContractError):
        parse_hook_response(text)


def test_the_verdict_is_the_last_line() -> None:
    response = hook_response.build_response(
        "count-skill-usage", "standard", "allow", "ok"
    )
    text = "free text from the hook\n" + json.dumps(response) + "\n\n"

    assert parse_hook_response(text) == response


@pytest.mark.parametrize("hook_id", sorted(hook_response.HOOKS))
def test_exit_code_zero_allows_for_every_hook_and_profile(hook_id: str) -> None:
    for profile in hook_response.PROFILES:
        response = hook_response.decide(hook_id, profile, 0, "")
        assert (response["decision"], response["reason_code"]) == ("allow", "ok")


@pytest.mark.parametrize("hook_id", CRITICAL)
@pytest.mark.parametrize("profile", hook_response.PROFILES)
def test_a_critical_hook_blocks_in_every_profile(hook_id: str, profile: str) -> None:
    blocked = hook_response.decide(hook_id, profile, 2, "refused")
    failed = hook_response.decide(hook_id, profile, 1, "traceback")
    missing = hook_response.decide(hook_id, profile, None, "")

    assert blocked["decision"] == "block"
    assert blocked["reason_code"] == hook_response.HOOKS[hook_id][0]
    assert blocked["remedy"] == "refused"
    for response in (failed, missing):
        assert (response["decision"], response["reason_code"]) == (
            "block",
            "internal-error",
        )


@pytest.mark.parametrize(
    "hook_id", [h for h in NON_CRITICAL if hook_response.HOOKS[h][0] != "ok"]
)
def test_a_non_critical_block_is_downgraded_only_by_the_minimal_profile(
    hook_id: str,
) -> None:
    for profile in ("standard", "strict"):
        assert hook_response.decide(hook_id, profile, 2, "x")["decision"] == "block"
    downgraded = hook_response.decide(hook_id, "minimal", 2, "x")
    assert downgraded["decision"] == "warn"
    assert downgraded["reason_code"] == hook_response.HOOKS[hook_id][0]


@pytest.mark.parametrize("hook_id", NON_CRITICAL)
def test_a_non_critical_internal_error_only_warns(hook_id: str) -> None:
    for code in (1, 127, None):
        response = hook_response.decide(hook_id, "standard", code, "")
        assert (response["decision"], response["reason_code"]) == (
            "warn",
            "internal-error",
        )


def test_a_hook_that_never_blocks_treats_exit_two_as_an_error() -> None:
    response = hook_response.decide("count-skill-usage", "standard", 2, "")

    assert (response["decision"], response["reason_code"]) == ("warn", "internal-error")


def test_the_remedy_is_bounded() -> None:
    response = hook_response.decide("block-pr-merge", "standard", 2, "x" * 5000)

    assert len(response["remedy"]) == hook_response.REMEDY_LIMIT


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (None, "standard"),
        ({}, "standard"),
        ({"hook_policy": {"profile": "minimal"}}, "minimal"),
        ({"hook_policy": {"profile": "strict", "escalations": []}}, "strict"),
        # Present but unreadable or invalid: only toward strict.
        ("{not json", "strict"),
        ("[]", "strict"),
        ({"hook_policy": []}, "strict"),
        ({"hook_policy": None}, "strict"),
        ({"hook_policy": {}}, "strict"),
        ({"hook_policy": {"profile": "lax"}}, "strict"),
        ({"hook_policy": {"profile": "minimal", "escalations": {}}}, "strict"),
        ({"hook_policy": {"profile": "minimal", "escalations": ["x"]}}, "strict"),
        (
            {"hook_policy": {"profile": "minimal", "escalations": [{"profile": "x"}]}},
            "strict",
        ),
    ],
)
def test_the_profile_comes_only_from_project_json(
    tmp_path: Path, document: object, expected: str
) -> None:
    if document is not None:
        path = tmp_path / ".harness" / "project.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            document if isinstance(document, str) else json.dumps(document),
            encoding="utf-8",
        )

    assert hook_response.read_profile(tmp_path) == expected


_ESCALATING = {
    "profile": "minimal",
    "escalations": [
        {"roles": ["qa"], "profile": "standard"},
        {"paths": ["docs/**"], "roles": ["developer"], "profile": "strict"},
    ],
}


def _write_policy(root: Path, policy: object) -> None:
    path = root / ".harness" / "project.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hook_policy": policy}), encoding="utf-8")


@pytest.mark.parametrize(
    "policy",
    [
        {"profile": "minimal"},
        {"profile": "standard", "escalations": []},
        _ESCALATING,
        {
            "profile": "minimal",
            "escalations": [{"roles": ["qa"], "profile": "standard"}],
        },
        {"profile": "strict", "escalations": [{"roles": ["qa"], "profile": "strict"}]},
    ],
)
def test_the_envelope_profile_matches_the_resolver_for_the_strictest_escalation(
    tmp_path: Path, policy: dict[str, object]
) -> None:
    """The hook cannot see role or path, so it applies the strictest escalation to every call: the
    profile equals ``resolve_profile`` with every escalation condition satisfied."""
    _write_policy(tmp_path, policy)
    normalized = hook_policy.normalize_policy(policy)
    unconditional = {
        "profile": normalized["profile"],
        "escalations": [
            {"paths": [], "roles": [], "profile": item["profile"]}
            for item in normalized["escalations"]  # type: ignore[attr-defined]
        ],
    }

    assert hook_response.read_profile(tmp_path) == hook_policy.resolve_profile(
        unconditional
    )
    # Any concrete role and path resolves to a profile no stricter than the envelope applies.
    order = hook_response.PROFILES.index
    for role in ("developer", "review", "qa", "coordinator"):
        for paths in ([], ["docs/a.md"], ["src/a.py"]):
            resolved = hook_policy.resolve_profile(normalized, role=role, paths=paths)
            assert order(resolved) <= order(hook_response.read_profile(tmp_path))


def test_an_escalation_to_standard_stops_the_minimal_downgrade(tmp_path: Path) -> None:
    low = {"profile": "minimal"}
    escalated = {
        "profile": "minimal",
        "escalations": [{"roles": ["qa"], "profile": "standard"}],
    }
    _write_policy(tmp_path, low)
    soft = hook_response.decide(
        "require-bounded-check", hook_response.read_profile(tmp_path), 2, "no"
    )
    _write_policy(tmp_path, escalated)
    hard = hook_response.decide(
        "require-bounded-check", hook_response.read_profile(tmp_path), 2, "no"
    )

    assert (soft["decision"], soft["profile"]) == ("warn", "minimal")
    assert (hard["decision"], hard["profile"]) == ("block", "standard")


def _project(tmp_path: Path, hook_policy: object | None = None) -> Path:
    """An installed copy: <project>/.claude/hooks/ with every hook and helper."""
    shutil.copytree(
        HOOKS,
        tmp_path / ".claude" / "hooks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    if hook_policy is not None:
        path = tmp_path / ".harness" / "project.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"hook_policy": hook_policy}), encoding="utf-8")
    return tmp_path


def _wrapper(
    project: Path,
    hook_id: str,
    script_body: str,
    *,
    env: dict[str, str] | None = None,
    stdin: str = "payload",
) -> subprocess.CompletedProcess[str]:
    script = project / "inner.sh"
    script.write_text(script_body, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(project / ".claude" / "hooks" / "hook_response.py"),
            "run",
            hook_id,
            str(BASH),
            str(script),
        ],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )


@needs_bash
def test_the_wrapper_passes_stdin_and_stdout_and_ends_stderr_with_the_verdict(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    done = _wrapper(project, "count-skill-usage", "cat; echo note >&2")

    assert done.returncode == 0
    assert done.stdout == "payload"
    assert done.stderr.startswith("note\n")
    verdict = parse_hook_response(done.stderr)
    assert (verdict["hook_id"], verdict["decision"], verdict["profile"]) == (
        "count-skill-usage",
        "allow",
        "standard",
    )


@needs_bash
def test_a_blocking_exit_code_is_a_block_verdict_with_exit_two(tmp_path: Path) -> None:
    project = _project(tmp_path)

    done = _wrapper(project, "block-pr-merge", "echo refused >&2; exit 2")

    assert done.returncode == 2
    verdict = parse_hook_response(done.stderr)
    assert verdict["decision"] == "block"
    assert verdict["reason_code"] == "pr-merge"
    assert "refused" in str(verdict["remedy"])
    assert verdict["evidence_ref"] == ".claude/hooks/block-pr-merge.sh"
    assert "refused" in done.stderr.splitlines()[0]


@needs_bash
@pytest.mark.parametrize("hook_id", CRITICAL)
def test_a_critical_hook_blocks_on_an_internal_error_and_a_crash(
    tmp_path: Path, hook_id: str
) -> None:
    project = _project(tmp_path)

    for body in ("exit 1", "exit 127", "kill -9 $$"):
        done = _wrapper(project, hook_id, body)
        assert done.returncode == 2, body
        verdict = parse_hook_response(done.stderr)
        assert (verdict["decision"], verdict["reason_code"]) == (
            "block",
            "internal-error",
        )


@needs_bash
def test_a_non_critical_hook_warns_on_an_internal_error(tmp_path: Path) -> None:
    project = _project(tmp_path)

    done = _wrapper(project, "require-bounded-check", "exit 1")

    assert done.returncode == 0
    verdict = parse_hook_response(done.stderr)
    assert (verdict["decision"], verdict["reason_code"]) == ("warn", "internal-error")


@needs_bash
def test_the_minimal_profile_downgrades_a_non_critical_block_only(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, {"profile": "minimal"})

    soft = _wrapper(project, "require-bounded-check", "echo bad name >&2; exit 2")
    hard = _wrapper(project, "block-direct-master", "echo direct >&2; exit 2")
    branch = _wrapper(project, "check-branch-name", "echo name >&2; exit 2")

    assert soft.returncode == 0
    verdict = parse_hook_response(soft.stderr)
    assert (verdict["decision"], verdict["profile"]) == ("warn", "minimal")
    for critical in (hard, branch):
        assert critical.returncode == 2
        assert parse_hook_response(critical.stderr)["decision"] == "block"


@needs_bash
@pytest.mark.parametrize(
    "policy", [None, {"profile": "standard"}, {"profile": "strict"}]
)
def test_a_project_without_a_minimal_profile_keeps_every_blocking_decision(
    tmp_path: Path, policy: object
) -> None:
    project = _project(tmp_path, policy)

    for hook_id in CRITICAL + ["require-bounded-check"]:
        done = _wrapper(project, hook_id, "echo no >&2; exit 2")
        assert done.returncode == 2, hook_id
        assert parse_hook_response(done.stderr)["decision"] == "block"


@needs_bash
def test_the_profile_environment_variable_changes_no_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    standard = _project(tmp_path / "standard", {"profile": "standard"})
    minimal = _project(tmp_path / "minimal", {"profile": "minimal"})
    env = {**os.environ, "HOOK_PROFILE": "minimal", "HARNESS_HOOK_PROFILE": "minimal"}

    kept = _wrapper(standard, "require-bounded-check", "exit 2", env=env)
    plain = _wrapper(
        minimal,
        "require-bounded-check",
        "exit 2",
        env={**env, "HOOK_PROFILE": "strict"},
    )

    assert kept.returncode == 2
    assert parse_hook_response(kept.stderr)["profile"] == "standard"
    assert plain.returncode == 0
    assert parse_hook_response(plain.stderr)["profile"] == "minimal"


@needs_bash
def test_a_command_that_cannot_start_is_an_internal_error(tmp_path: Path) -> None:
    project = _project(tmp_path)
    wrapper = project / ".claude" / "hooks" / "hook_response.py"

    for hook_id, code in (("block-pr-merge", 2), ("require-bounded-check", 0)):
        done = subprocess.run(
            [sys.executable, str(wrapper), "run", hook_id, str(tmp_path / "missing")],
            input="",
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert done.returncode == code
        assert parse_hook_response(done.stderr)["reason_code"] == "internal-error"


@needs_bash
@pytest.mark.parametrize(
    ("hook_id", "code", "decision"),
    [("block-pr-merge", 2, "block"), ("require-bounded-check", 0, "warn")],
)
def test_a_hanging_hook_body_is_an_internal_error_after_the_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hook_id: str,
    code: int,
    decision: str,
) -> None:
    import io

    project = _project(tmp_path)
    script = project / "inner.sh"
    script.write_text("sleep 30", encoding="utf-8")
    monkeypatch.setattr(hook_response, "INNER_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(hook_response, "project_root", lambda: project)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"payload")))

    returned = hook_response.main(["run", hook_id, str(BASH), str(script)])

    assert returned == code
    verdict = parse_hook_response(capsys.readouterr().err)
    assert (verdict["decision"], verdict["reason_code"]) == (decision, "internal-error")
    assert "тайм-аут" in str(verdict["remedy"])


def test_the_timeout_is_below_the_runtime_hook_timeout() -> None:
    assert 0 < hook_response.INNER_TIMEOUT_SECONDS < 60


def test_an_unknown_hook_or_usage_is_refused(tmp_path: Path) -> None:
    wrapper = HOOKS / "hook_response.py"

    for argv in (
        ["run", "no-such-hook", "true"],
        ["run"],
        ["walk", "block-pr-merge", "x"],
    ):
        done = subprocess.run(
            [sys.executable, str(wrapper), *argv],
            input="",
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert done.returncode == 2
        assert "hook_response.py" in done.stderr


@needs_bash
def test_the_real_merge_hook_answers_through_the_envelope(tmp_path: Path) -> None:
    project = _project(tmp_path)
    hook = project / ".claude" / "hooks" / "block-pr-merge.sh"

    def run(command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BASH), str(hook)],
            input=json.dumps({"tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    blocked = run("gh pr merge 1")
    allowed = run("echo hello")

    assert blocked.returncode == 2
    assert "Zero Auto-Merge" in blocked.stderr
    verdict = parse_hook_response(blocked.stderr)
    assert (verdict["hook_id"], verdict["decision"], verdict["reason_code"]) == (
        "block-pr-merge",
        "block",
        "pr-merge",
    )
    assert allowed.returncode == 0
    assert parse_hook_response(allowed.stderr)["decision"] == "allow"
    assert allowed.stdout == ""


@needs_bash
def test_a_hook_without_the_helper_decides_alone(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / ".claude" / "hooks" / "hook_response.py").unlink()
    hook = project / ".claude" / "hooks" / "block-pr-merge.sh"

    done = subprocess.run(
        [str(BASH), str(hook)],
        input=json.dumps({"tool_input": {"command": "gh pr merge 1"}}),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert done.returncode == 2
    assert "Zero Auto-Merge" in done.stderr
    with pytest.raises(ContractError):
        parse_hook_response(done.stderr)


@needs_bash
@pytest.mark.parametrize("hook_id", sorted(hook_response.HOOKS))
def test_every_managed_event_hook_runs_through_the_envelope(hook_id: str) -> None:
    text = (HOOKS / f"{hook_id}.sh").read_text(encoding="utf-8")

    assert f'hook_response.py" run {hook_id} ' in text
