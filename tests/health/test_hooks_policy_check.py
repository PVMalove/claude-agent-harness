"""`harness health` reports the `hooks.policy` check of the installed project hooks."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

from harness.health.hook_settings import (
    CODEX_SUPPORT,
    HOOK_CATALOG,
    is_wired,
    script_name,
)
from harness.json_types import JsonObject
from tests.core.test_uninstall import git, harness

PROJECT_TEMPLATES = Path(__file__).resolve().parents[2] / "harness" / "project"

SETTINGS = ".claude/settings.local.json"
CODEX = ".codex/hooks.json"
HOOKS_DIR = ".claude/hooks"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    result = harness("init", str(root), "--capability", "engineering-suite")
    assert result.returncode == 0, result.stderr
    return root


def check(repo: Path) -> JsonObject:
    """The `hooks.policy` entry of `harness health --json`."""
    result = harness("health", str(repo), "--json")
    report = json.loads(result.stdout)
    found: JsonObject = next(c for c in report["checks"] if c["id"] == "hooks.policy")
    return found


def load(repo: Path, relative: str) -> JsonObject:
    document: JsonObject = json.loads((repo / relative).read_text(encoding="utf-8"))
    return document


def save(repo: Path, relative: str, document: JsonObject) -> None:
    (repo / relative).write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )


def drop_script(document: JsonObject, script: str) -> None:
    for groups in document["hooks"].values():
        for group in groups:
            group["hooks"] = [h for h in group["hooks"] if script not in h["command"]]


def test_installed_project_reports_profile_source_and_wiring(repo: Path) -> None:
    result = check(repo)

    assert result["group"] == "hooks"
    assert result["status"] == "warn"
    message = result["message"]
    assert "профиль standard" in message
    assert ".harness/project.json" in message
    assert "claude: критических hooks подключено 9/9" in message
    assert "codex: критических hooks подключено 7/7" in message


def test_events_the_second_runtime_lacks_are_listed_and_a_missing_out_of_hook_check_warns(
    repo: Path,
) -> None:
    result = check(repo)

    assert result["status"] == "warn"
    message = result["message"]
    assert "ограничение адаптера codex:" in message
    assert (
        "codex: нет события или payload: block-scratch-outside-docs-tasks "
        "(нет проверки вне hook)"
    ) in message
    assert (
        "check-worktree-branch-name (вне hook: batch create по branch_pattern)"
        in message
    )
    assert "count-skill-usage" in message


def test_the_second_runtime_lists_the_change_impact_edit_rows_with_their_out_of_hook_check(
    repo: Path,
) -> None:
    result = check(repo)
    message = result["message"]

    _, marker, lacking = message.partition("codex: нет события или payload: ")
    assert marker
    for row in (
        "impact-first-touch (вне hook: ChangeImpact-gate по ADR 0023)",
        "sensitive-path-recheck (вне hook: граница strict (dispatch propose, qa run, publish))",
        "targeted-check-reminder (вне hook: сверка checks_run)",
    ):
        assert row in lacking
    # The hooks are advisory and checked outside the hook: no advisory finding, no missing wiring.
    unprotected = message.partition("без события и без проверки вне hook: ")[2]
    for hook_id in ("impact-first-touch", "sensitive-path-recheck", "targeted-check"):
        assert hook_id not in unprotected.split(";")[0]
    assert "не подключён hook targeted-check-reminder" not in message
    assert "claude: нет события" not in message


def test_a_missing_script_of_several_events_is_named_once(repo: Path) -> None:
    (repo / HOOKS_DIR / "targeted-check-reminder.sh").unlink()

    result = check(repo)

    assert result["status"] == "warn"
    assert result["message"].count("targeted-check-reminder.sh: файл отсутствует") == 1


def test_qa_marker_hook_is_missing_for_the_second_runtime_only(repo: Path) -> None:
    second = load(repo, CODEX)
    assert not [
        handler
        for groups in second["hooks"].values()
        for group in groups
        for handler in group["hooks"]
        if script_name(handler) == "mark-qa-gate-passed.sh"
    ]

    message = check(repo)["message"]

    assert re.search(r"codex: нет события или payload: .*mark-qa-gate-passed", message)
    assert "claude: нет события" not in message
    assert "не подключён hook mark-qa-gate-passed" not in message


def load_hook_response() -> object:
    spec = importlib.util.spec_from_file_location(
        "hook_response_catalog", PROJECT_TEMPLATES / "hooks" / "hook_response.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hook_catalog_agrees_with_the_response_envelope() -> None:
    hooks: dict[str, tuple[str, bool]] = getattr(load_hook_response(), "HOOKS")
    catalog = {spec.hook_id: spec for spec in HOOK_CATALOG}

    # `test-author-guard` is wired in the agent frontmatter and runs outside the envelope.
    assert set(catalog) - set(hooks) == {"test-author-guard"}
    assert set(hooks) <= set(catalog)
    assert {hook_id: catalog[hook_id].critical for hook_id in hooks} == {
        hook_id: critical for hook_id, (_, critical) in hooks.items()
    }
    assert catalog["test-author-guard"].critical
    for spec in HOOK_CATALOG:
        assert (PROJECT_TEMPLATES / "hooks" / spec.script).is_file(), spec.script


def test_hook_catalog_agrees_with_the_templates() -> None:
    first = json.loads((PROJECT_TEMPLATES / "settings.local.json.tmpl").read_text("utf-8"))
    second = json.loads((PROJECT_TEMPLATES / "codex-hooks.json.tmpl").read_text("utf-8"))
    agent = (PROJECT_TEMPLATES / "agents" / "test-author.md").read_text("utf-8")
    frontmatter = agent.split("---", 2)[1]

    settings_specs = [spec for spec in HOOK_CATALOG if spec.wiring == "settings"]
    wired_scripts = {
        script_name(handler)
        for groups in first["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    }
    assert wired_scripts == {spec.script for spec in settings_specs}
    for spec in HOOK_CATALOG:
        if spec.wiring == "agent":
            assert spec.script in frontmatter
    for spec in settings_specs:
        for tool in spec.tools:
            assert is_wired(first, spec, tool), (spec.hook_id, tool)
            # The second runtime wires exactly what it delivers.
            assert is_wired(second, spec, tool) == ((spec.event, tool) in CODEX_SUPPORT), (
                spec.hook_id,
                tool,
            )


def test_profile_without_hook_policy_key_comes_from_the_default_source(
    repo: Path,
) -> None:
    project_json = repo / ".harness/project.json"
    data = json.loads(project_json.read_text(encoding="utf-8"))
    del data["hook_policy"]
    project_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    message = check(repo)["message"]

    assert "профиль standard (по умолчанию)" in message


def test_legacy_project_keeps_the_standard_wiring_after_update(repo: Path) -> None:
    project_json = repo / ".harness/project.json"
    data = json.loads(project_json.read_text(encoding="utf-8"))
    del data["hook_policy"]
    project_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    updated = harness("update", str(repo))
    assert updated.returncode == 0, updated.stderr

    result = check(repo)

    assert result["status"] == "warn"
    assert "профиль standard (по умолчанию)" in result["message"]
    assert "claude: критических hooks подключено 9/9" in result["message"]
    assert "hook_policy" not in json.loads(project_json.read_text(encoding="utf-8"))


def test_escalations_are_listed_with_the_effective_profile(repo: Path) -> None:
    project_json = repo / ".harness/project.json"
    data = json.loads(project_json.read_text(encoding="utf-8"))
    data["hook_policy"] = {
        "profile": "standard",
        "escalations": [{"profile": "strict", "roles": ["developer"]}],
    }
    project_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    message = check(repo)["message"]

    assert "профиль strict" in message
    assert "эскалации: strict: roles=developer" in message


def test_invalid_hook_policy_fails_because_the_hooks_apply_strict(repo: Path) -> None:
    project_json = repo / ".harness/project.json"
    data = json.loads(project_json.read_text(encoding="utf-8"))
    data["hook_policy"] = {"profile": "lenient"}
    project_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    result = check(repo)

    assert result["status"] == "fail"
    assert "применяют strict" in result["message"]


def test_missing_critical_script_fails(repo: Path) -> None:
    (repo / HOOKS_DIR / "block-dangerous-git.sh").unlink()

    result = check(repo)

    assert result["status"] == "fail"
    assert "block-dangerous-git.sh: файл отсутствует" in result["message"]


@pytest.mark.skipif(os.name == "nt", reason="the executable bit is a POSIX notion")
def test_non_executable_critical_script_fails(repo: Path) -> None:
    script = repo / HOOKS_DIR / "block-pr-merge.sh"
    script.chmod(script.stat().st_mode & ~0o111)

    result = check(repo)

    assert result["status"] == "fail"
    assert "block-pr-merge.sh: нет бита исполнения" in result["message"]


def test_removed_managed_entry_fails_and_names_the_hook(repo: Path) -> None:
    document = load(repo, SETTINGS)
    drop_script(document, "require-qa-gate.sh")
    save(repo, SETTINGS, document)

    result = check(repo)

    assert result["status"] == "fail"
    assert "claude: критических hooks подключено 8/9" in result["message"]
    assert "не подключён критический hook require-qa-gate" in result["message"]
    assert result["fix"] is not None


def test_removed_entry_of_the_second_runtime_fails(repo: Path) -> None:
    document = load(repo, CODEX)
    drop_script(document, "block-dangerous-git.sh")
    save(repo, CODEX, document)

    result = check(repo)

    assert result["status"] == "fail"
    assert (
        "не подключён критический hook block-dangerous-git (codex)" in result["message"]
    )


def test_missing_file_of_the_second_runtime_warns(repo: Path) -> None:
    (repo / CODEX).unlink()

    result = check(repo)

    assert result["status"] == "warn"
    assert ".codex/hooks.json отсутствует" in result["message"]


def test_script_drift_from_the_template_warns(repo: Path) -> None:
    script = repo / HOOKS_DIR / "count-skill-usage.sh"
    script.write_text(
        script.read_text(encoding="utf-8") + "\n# local\n", encoding="utf-8"
    )

    result = check(repo)

    assert (
        "дрейф: .claude/hooks/count-skill-usage.sh отличается от шаблона"
        in result["message"]
    )
    assert "--force-seed-file .claude/hooks/count-skill-usage.sh" in result["message"]


def test_changed_managed_entry_is_reported_as_drift_not_as_a_missing_hook(
    repo: Path,
) -> None:
    document = load(repo, SETTINGS)
    for group in document["hooks"]["PreToolUse"]:
        for handler in group["hooks"]:
            if "block-dangerous-git.sh" in handler["command"]:
                handler["timeout"] = 5
    save(repo, SETTINGS, document)

    result = check(repo)

    assert result["status"] == "warn"
    assert "дрейф: конфликтов managed hooks 1 (claude)" in result["message"]
    assert "подключено 9/9" in result["message"]


def test_check_is_skipped_without_the_hooks_capabilities(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    git(root, "init", "-b", "main")
    assert harness("init", str(root)).returncode == 0

    result = check(root)

    assert result["status"] == "skipped"


def test_standalone_run_says_that_drift_was_not_checked(repo: Path) -> None:
    from harness.health import registry

    report = registry.run(repo)

    result = next(c for c in report.checks if c.id == "hooks.policy")
    assert re.search(r"дрейф не проверен", result.message)


def test_console_route_of_the_policy_warn_does_not_promise_that_update_fixes_it() -> None:
    from harness.console import i18n
    from harness.console.shell import check_route

    route = check_route("hooks.policy")

    # The warn stays after `harness update` (the second runtime lacks some hooks), so the console
    # neither calls it a wiring error nor sends the user to Update Harness.
    assert route.section == "Diagnostics"
    assert "неверно" not in route.title
    assert route.title != check_route("environment.hook_bash").title
    assert i18n.tr("en", route.title) != route.title
