"""`harness init|adopt|update` merge managed hook entries into the runtime settings file."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from harness.health.hook_settings import entry_digest
from harness.json_types import JsonObject
from tests.core.test_uninstall import git, harness

SETTINGS = ".claude/settings.local.json"
INTEGRATIONS = ".harness/integrations.json"
USER_HOOK = {"type": "command", "command": "bash scripts/user-guard.sh"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    return root


def install(repo: Path) -> None:
    result = harness("init", str(repo), "--capability", "pvmalove-suite")
    assert result.returncode == 0, result.stderr


def script_of(handler: JsonObject) -> str | None:
    match = re.search(r"\.claude/hooks/([\w.-]+)", str(handler.get("command", "")))
    return match.group(1) if match else None


def load(repo: Path) -> JsonObject:
    document: JsonObject = json.loads((repo / SETTINGS).read_text(encoding="utf-8"))
    return document


def save(repo: Path, document: JsonObject) -> None:
    (repo / SETTINGS).write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )


def handlers(document: JsonObject, script: str) -> list[tuple[str, str, JsonObject]]:
    """(event, matcher, handler) of every handler that runs `script`."""
    found = []
    for event, groups in document["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                if script_of(handler) == script:
                    found.append((event, group.get("matcher", ""), handler))
    return found


def drop_script(document: JsonObject, script: str) -> None:
    """Remove every handler of `script` and the groups that it leaves empty."""
    for event in list(document["hooks"]):
        groups = document["hooks"][event]
        for group in groups:
            group["hooks"] = [h for h in group["hooks"] if script_of(h) != script]
        document["hooks"][event] = [g for g in groups if g["hooks"]]


def template_document() -> JsonObject:
    from harness.bin.harness import PROJECT_TEMPLATE_DIR

    document: JsonObject = json.loads(
        (PROJECT_TEMPLATE_DIR / "settings.local.json.tmpl").read_text(encoding="utf-8")
    )
    return document


def test_update_restores_removed_managed_entries_and_keeps_user_hook(
    repo: Path,
) -> None:
    install(repo)
    document = load(repo)
    drop_script(document, "protect-hook-policy.sh")
    bash_group = next(
        group for group in document["hooks"]["PreToolUse"] if group["matcher"] == "Bash"
    )
    bash_group["hooks"].append(USER_HOOK)
    save(repo, document)

    result = harness("update", str(repo))

    assert result.returncode == 0, result.stderr
    merged = load(repo)
    restored = handlers(merged, "protect-hook-policy.sh")
    assert sorted(matcher for _, matcher, _ in restored) == [
        "Bash",
        "Write|Edit|MultiEdit|NotebookEdit",
    ]
    user_commands = [
        h["command"]
        for group in merged["hooks"]["PreToolUse"]
        for h in group["hooks"]
        if h["command"] == USER_HOOK["command"]
    ]
    assert user_commands == [USER_HOOK["command"]]
    assert merged["permissions"] == document["permissions"]


def test_update_replaces_outdated_managed_entry_recorded_in_baseline(
    repo: Path,
) -> None:
    install(repo)
    document = load(repo)
    ((event, matcher, handler),) = handlers(document, "block-dangerous-git.sh")
    outdated = dict(handler, command=str(handler["command"]).replace("bash ", "sh "))
    handler.update(outdated)
    save(repo, document)
    inventory = json.loads((repo / INTEGRATIONS).read_text(encoding="utf-8"))
    entry = next(
        item
        for item in inventory["integrations"]
        if item["id"] == "pvmalove-suite-hooks"
    )
    entry["managed_entries"] = sorted(
        [*entry.get("managed_entries", []), entry_digest(event, matcher, outdated)]
    )
    (repo / INTEGRATIONS).write_text(json.dumps(inventory, indent=2), encoding="utf-8")

    result = harness("update", str(repo))

    assert result.returncode == 0, result.stderr
    ((_, _, current),) = handlers(load(repo), "block-dangerous-git.sh")
    assert current["command"].startswith("bash ")


def test_update_leaves_user_modified_managed_entry_untouched(repo: Path) -> None:
    install(repo)
    document = load(repo)
    ((_, _, handler),) = handlers(document, "block-dangerous-git.sh")
    handler["timeout"] = 5
    save(repo, document)
    before = (repo / SETTINGS).read_bytes()

    result = harness("update", str(repo))

    assert result.returncode == 0, result.stderr
    assert (repo / SETTINGS).read_bytes() == before


def test_force_seed_file_and_force_seed_files_write_the_template_bytes(
    repo: Path,
) -> None:
    install(repo)
    template = (
        Path(__file__).resolve().parents[2] / "harness/project/settings.local.json.tmpl"
    ).read_bytes()
    for flag in (["--force-seed-file", SETTINGS], ["--force-seed-files"]):
        save(repo, {"hooks": {}, "permissions": {"allow": ["Bash(own)"]}})
        result = harness("update", str(repo), *flag)
        assert result.returncode == 0, result.stderr
        assert (repo / SETTINGS).read_bytes() == template


@pytest.mark.parametrize("command", ["init", "adopt"])
def test_install_merges_into_existing_user_settings(repo: Path, command: str) -> None:
    path = repo / SETTINGS
    path.parent.mkdir(parents=True)
    user = {
        "permissions": {"allow": ["Bash(own)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [USER_HOOK]}]},
    }
    path.write_text(json.dumps(user), encoding="utf-8")

    result = harness(command, str(repo), "--capability", "pvmalove-suite")

    assert result.returncode == 0, result.stderr
    merged = load(repo)
    assert merged["permissions"] == user["permissions"]
    first_group = merged["hooks"]["PreToolUse"][0]
    assert first_group["hooks"][0] == USER_HOOK
    expected = {
        script
        for groups in template_document()["hooks"].values()
        for group in groups
        for h in group["hooks"]
        if (script := script_of(h))
    }
    for script in expected:
        assert len(handlers(merged, script)) == len(
            handlers(template_document(), script)
        ), script


def test_unreadable_settings_file_is_left_unchanged(repo: Path) -> None:
    install(repo)
    path = repo / SETTINGS
    path.write_text("{not json", encoding="utf-8")

    result = harness("update", str(repo))

    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == "{not json"


def test_conflict_is_reported_with_the_entry_and_left_unchanged(repo: Path) -> None:
    install(repo)
    document = load(repo)
    ((_, _, handler),) = handlers(document, "block-dangerous-git.sh")
    handler["timeout"] = 5
    save(repo, document)
    before = (repo / SETTINGS).read_bytes()

    result = harness("update", str(repo))

    assert result.returncode == 0, result.stderr
    assert (repo / SETTINGS).read_bytes() == before
    assert (
        "hooks conflict: .claude/settings.local.json: PreToolUse [Bash] "
        "block-dangerous-git.sh"
    ) in result.stdout
    assert "conflicts=1" in result.stdout
    assert "--force-seed-file .claude/settings.local.json" in result.stdout
    assert len(handlers(load(repo), "block-dangerous-git.sh")) == 1


def test_conflict_is_not_adopted_into_the_recorded_baseline(repo: Path) -> None:
    install(repo)
    document = load(repo)
    ((event, matcher, handler),) = handlers(document, "block-dangerous-git.sh")
    handler["timeout"] = 5
    save(repo, document)
    harness("update", str(repo))

    inventory = json.loads((repo / INTEGRATIONS).read_text(encoding="utf-8"))
    entry = next(
        item
        for item in inventory["integrations"]
        if item["id"] == "pvmalove-suite-hooks"
    )

    assert entry_digest(event, matcher, handler) not in entry["managed_entries"]
    again = harness("update", str(repo))
    assert "conflicts=1" in again.stdout


def test_repeated_update_is_idempotent_and_prints_the_report(repo: Path) -> None:
    install(repo)
    document = load(repo)
    drop_script(document, "block-pr-merge.sh")
    save(repo, document)
    first = harness("update", str(repo))
    assert "added=1 replaced=0" in first.stdout
    settings = (repo / SETTINGS).read_bytes()
    inventory = (repo / INTEGRATIONS).read_bytes()

    second = harness("update", str(repo))

    assert second.returncode == 0, second.stderr
    assert (repo / SETTINGS).read_bytes() == settings
    assert (repo / INTEGRATIONS).read_bytes() == inventory
    assert (
        "hooks merge: .claude/settings.local.json: added=0 replaced=0" in second.stdout
    )
    assert "conflicts=0" in second.stdout
    assert (
        "hooks check: .claude/settings.local.json: critical wired 9/9" in second.stdout
    )
    assert len(handlers(load(repo), "block-pr-merge.sh")) == 1


def test_report_names_the_critical_hook_that_a_conflict_leaves_unwired(
    repo: Path,
) -> None:
    install(repo)
    document = load(repo)
    ((_, _, handler),) = handlers(document, "block-dangerous-git.sh")
    drop_script(document, "block-dangerous-git.sh")
    document["hooks"]["PreToolUse"].append({"matcher": "Edit", "hooks": [handler]})
    save(repo, document)

    result = harness("update", str(repo))

    assert "conflicts=" in result.stdout
    assert (
        "hooks check: .claude/settings.local.json: critical wired 8/9; "
        "missing: block-dangerous-git"
    ) in result.stdout


def test_update_of_a_legacy_project_leaves_project_json_without_hook_policy(
    repo: Path,
) -> None:
    install(repo)
    project_json = repo / ".harness/project.json"
    data = json.loads(project_json.read_text(encoding="utf-8"))
    del data["hook_policy"]
    project_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    legacy = project_json.read_bytes()
    document = load(repo)
    drop_script(document, "block-pr-merge.sh")
    save(repo, document)

    result = harness("update", str(repo))

    assert result.returncode == 0, result.stderr
    assert "added=1 replaced=0" in result.stdout
    assert project_json.read_bytes() == legacy
    assert "hook_policy" not in json.loads(project_json.read_text(encoding="utf-8"))
    assert len(handlers(load(repo), "block-pr-merge.sh")) == 1


CODEX_CONFIG = ".codex/hooks.json"
CODEX_COMMAND = re.compile(
    r'cd "\$\(git rev-parse --show-toplevel\)" && bash "\.claude/hooks/([\w.-]+)"'
)


def load_codex(repo: Path) -> JsonObject:
    document: JsonObject = json.loads((repo / CODEX_CONFIG).read_text(encoding="utf-8"))
    return document


def codex_triples(document: JsonObject) -> list[tuple[str, str, str]]:
    """(event, matcher, script) of every handler of the second runtime's file."""
    triples = []
    for event, groups in document["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                match = CODEX_COMMAND.fullmatch(handler["command"])
                assert match, handler["command"]
                triples.append((event, group["matcher"], match.group(1)))
    return triples


def test_init_installs_the_second_runtime_adapter_from_the_same_scripts(
    repo: Path,
) -> None:
    install(repo)

    expected = sorted(
        (event, group["matcher"], script)
        for event, groups in template_document()["hooks"].items()
        for group in groups
        for handler in group["hooks"]
        if (script := script_of(handler))
        and event == "PreToolUse"
        and "Bash" in group["matcher"].split("|")
    )
    assert sorted(codex_triples(load_codex(repo))) == expected
    inventory = json.loads((repo / INTEGRATIONS).read_text(encoding="utf-8"))
    entry = next(
        item
        for item in inventory["integrations"]
        if item["id"] == "pvmalove-suite-codex-hooks"
    )
    assert entry["kind"] == "hook"
    assert entry["runtimes"] == ["codex"]
    assert entry["config"] == CODEX_CONFIG
    assert entry["managed_entries"] == sorted(set(entry["managed_entries"]))
    assert len(entry["managed_entries"]) == len(expected)


def test_second_runtime_adapter_skips_hooks_whose_payload_it_cannot_deliver(
    repo: Path,
) -> None:
    install(repo)

    scripts = {script for _, _, script in codex_triples(load_codex(repo))}

    assert scripts.isdisjoint(
        {
            "block-scratch-outside-docs-tasks.sh",
            "check-worktree-branch-name.sh",
            "count-skill-usage.sh",
        }
    )
    assert "protect-hook-policy.sh" in scripts


def test_second_runtime_adapter_does_not_wire_the_qa_marker_hook(repo: Path) -> None:
    # The second runtime fires PostToolUse after a Bash command that exited non-zero, and its
    # payload has no exit status: `mark-qa-gate-passed` would record a marker for a failed QA run.
    install(repo)

    def marker_triples() -> list[tuple[str, str, str]]:
        return [
            triple
            for triple in codex_triples(load_codex(repo))
            if triple[2] == "mark-qa-gate-passed.sh" or triple[0] == "PostToolUse"
        ]

    assert marker_triples() == []
    assert handlers(load(repo), "mark-qa-gate-passed.sh")

    result = harness("update", str(repo))

    assert result.returncode == 0, result.stderr
    assert marker_triples() == []


def test_second_runtime_file_follows_the_same_merge_rules(repo: Path) -> None:
    install(repo)
    document = load_codex(repo)
    drop = "block-pr-merge.sh"
    for group in document["hooks"]["PreToolUse"]:
        group["hooks"] = [h for h in group["hooks"] if drop not in h["command"]]
        if any("block-dangerous-git.sh" in h["command"] for h in group["hooks"]):
            group["hooks"].append(USER_HOOK)
            for h in group["hooks"]:
                if "block-dangerous-git.sh" in h["command"]:
                    h["timeout"] = 5
    (repo / CODEX_CONFIG).write_text(json.dumps(document, indent=2), encoding="utf-8")

    result = harness("update", str(repo))

    assert result.returncode == 0, result.stderr
    merged = load_codex(repo)
    commands = [h["command"] for g in merged["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert sum(drop in command for command in commands) == 1
    assert commands.count(USER_HOOK["command"]) == 1
    assert (
        f"hooks conflict: {CODEX_CONFIG}: PreToolUse [Bash] block-dangerous-git.sh"
        in result.stdout
    )
    assert f"hooks merge: {CODEX_CONFIG}: added=1" in result.stdout


def test_force_seed_file_replaces_the_second_runtime_file_with_the_template(
    repo: Path,
) -> None:
    install(repo)
    template = (
        Path(__file__).resolve().parents[2] / "harness/project/codex-hooks.json.tmpl"
    ).read_bytes()
    (repo / CODEX_CONFIG).write_text('{"hooks": {}}\n', encoding="utf-8")

    result = harness("update", str(repo), "--force-seed-file", CODEX_CONFIG)

    assert result.returncode == 0, result.stderr
    assert (repo / CODEX_CONFIG).read_bytes() == template


def test_second_runtime_update_is_idempotent(repo: Path) -> None:
    install(repo)
    first = (repo / CODEX_CONFIG).read_bytes()
    inventory = (repo / INTEGRATIONS).read_bytes()

    result = harness("update", str(repo))

    assert (repo / CODEX_CONFIG).read_bytes() == first
    assert (repo / INTEGRATIONS).read_bytes() == inventory
    assert f"hooks merge: {CODEX_CONFIG}: added=0 replaced=0" in result.stdout
