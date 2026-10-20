"""Saved configurations are durable, reversible and independent of draft application."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import harness.configuration as configuration
import harness.configuration_store as store_module
from harness.configuration import read_config_bundle
from harness.configuration_store import ConfigurationStore
from harness.health.project_tracker import resolve_project_tracker


def project(language: str) -> bytes:
    return json.dumps(
        {
            "language": language,
            "base_branch": "main",
            "branch_pattern": "^feature/issue-[0-9]+-.+",
            "qa_gate_commands": ["pytest"],
        }
    ).encode()


def test_saved_draft_can_be_applied_and_the_previous_set_restored(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    (root / "project.json").write_bytes(project("ru"))
    original = read_config_bundle(tmp_path)
    store = ConfigurationStore(tmp_path)
    chosen = store.save({**original, "project.json": project("en")}, "English reports")
    assert read_config_bundle(tmp_path) == original
    previous = store.apply(store.load(chosen.id), expected=original)
    assert (
        json.loads(read_config_bundle(tmp_path)["project.json"] or b"{}")["language"]
        == "en"
    )
    store.apply(store.load(previous.id), expected=read_config_bundle(tmp_path))
    assert read_config_bundle(tmp_path) == original
    assert len(store.versions()) == 3


def test_interrupted_pair_is_read_as_previous_set_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    (root / "project.json").write_bytes(project("ru"))
    before = read_config_bundle(tmp_path)
    atomic = store_module._atomic

    def interrupted(path: Path, raw: bytes | None) -> None:
        if path == root / "orchestration.json":
            assert read_config_bundle(tmp_path) == before
            raise KeyboardInterrupt("simulated process interruption")
        atomic(path, raw)

    monkeypatch.setattr(store_module, "_atomic", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ConfigurationStore(tmp_path).apply(
            {"project.json": project("en")}, expected=before
        )
    assert (root / "project.json").read_bytes() == project("en")
    assert read_config_bundle(tmp_path) == before
    monkeypatch.setattr(store_module, "_atomic", atomic)
    ConfigurationStore(tmp_path).recover()
    assert read_config_bundle(tmp_path) == before
    assert (root / "project.json").read_bytes() == project("ru")
    assert not (root / "orchestration.json").exists()


def test_tracker_reader_uses_previous_generation_during_interrupted_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    old = json.loads(project("ru"))
    old["tracker"] = {
        "type": "github",
        "host": "github.com",
        "project": "example/backend",
    }
    (root / "project.json").write_bytes(json.dumps(old).encode())
    before = read_config_bundle(tmp_path)
    atomic = store_module._atomic

    def interrupted(path: Path, raw: bytes | None) -> None:
        if path == root / "orchestration.json":
            raise KeyboardInterrupt
        atomic(path, raw)

    monkeypatch.setattr(store_module, "_atomic", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ConfigurationStore(tmp_path).apply(
            {"project.json": project("en")}, expected=before
        )
    assert resolve_project_tracker(tmp_path).effective.type == "github"


def test_stale_draft_corrupt_version_and_invalid_old_contract(tmp_path: Path) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    (root / "project.json").write_bytes(project("ru"))
    store = ConfigurationStore(tmp_path)
    before = read_config_bundle(tmp_path)
    (root / "project.json").write_bytes(project("en"))
    with pytest.raises(ValueError, match="другим процессом"):
        store.apply({"project.json": project("ru")}, expected=before)
    snapshot = store.save(read_config_bundle(tmp_path))
    (store.root / snapshot.id / "project.json").write_bytes(project("ru"))
    with pytest.raises(ValueError, match="хеш"):
        store.load(snapshot.id)
    invalid = project("wrong")
    (root / "project.json").write_bytes(invalid)
    backup = store.apply(
        {"project.json": project("ru")}, expected=read_config_bundle(tmp_path)
    )
    assert (store.root / backup.id / "project.json").read_bytes() == invalid
    with pytest.raises(ValueError, match="несовместим"):
        store.load(backup.id)


def test_store_rejects_symlinks_secrets_and_competing_writers(tmp_path: Path) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    (root / "project.json").write_bytes(project("ru"))
    store = ConfigurationStore(tmp_path)
    with store._locked(), pytest.raises(ValueError, match="Другой процесс"):
        ConfigurationStore(tmp_path).recover()
    secret = json.loads(project("ru"))
    secret["qa_gate_commands"] = ["TOKEN=hidden"]
    with pytest.raises(
        ValueError, match="project.json содержит значение, похожее на секрет"
    ):
        store.save(
            {"project.json": json.dumps(secret).encode(), "orchestration.json": None}
        )
    (root / "orchestration.json").symlink_to(tmp_path / "outside.json")
    with pytest.raises(ValueError, match="ссылками"):
        read_config_bundle(tmp_path)


def test_recovery_refuses_external_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    (root / "project.json").write_bytes(project("ru"))
    atomic = store_module._atomic

    def interrupt(path: Path, raw: bytes | None) -> None:
        if path == root / "orchestration.json":
            raise KeyboardInterrupt
        atomic(path, raw)

    monkeypatch.setattr(store_module, "_atomic", interrupt)
    with pytest.raises(KeyboardInterrupt):
        ConfigurationStore(tmp_path).apply(
            {"project.json": project("en")}, expected=read_config_bundle(tmp_path)
        )
    monkeypatch.setattr(store_module, "_atomic", atomic)
    external = project("ru") + b"\n"
    (root / "project.json").write_bytes(external)
    with pytest.raises(ValueError, match="внешнее изменение"):
        ConfigurationStore(tmp_path).recover()
    assert (root / "project.json").read_bytes() == external


def test_recovery_refuses_external_journal_symlink(tmp_path: Path) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    (root / "project.json").write_bytes(project("ru"))
    before = read_config_bundle(tmp_path)
    store = ConfigurationStore(tmp_path)
    store._prepare()
    outside = tmp_path / "outside-journal.json"
    outside.write_bytes(
        store_module.json_bytes(
            {
                "schema": 1,
                "state": "prepared",
                "id": "test",
                "before": store_module._encode(
                    {**before, "project.json": project("en")}
                ),
                "after": store_module._encode(before),
            }
        )
    )
    store.marker.symlink_to(outside)
    with pytest.raises(ValueError, match="ссылками"):
        store.recover()
    assert (root / "project.json").read_bytes() == project("ru")


def test_commit_marker_sync_failure_rolls_back_instead_of_claiming_false_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    (root / "project.json").write_bytes(project("ru"))
    before = read_config_bundle(tmp_path)
    atomic = store_module._atomic
    failed = False

    def fail_after_replace(path: Path, raw: bytes | None) -> None:
        nonlocal failed
        atomic(path, raw)
        if (
            path.name == ".transaction.json"
            and raw
            and json.loads(raw)["state"] == "committed"
            and not failed
        ):
            failed = True
            raise OSError("simulated directory sync failure after replace")

    monkeypatch.setattr(store_module, "_atomic", fail_after_replace)
    with pytest.raises(ValueError, match="восстановлена"):
        ConfigurationStore(tmp_path).apply(
            {"project.json": project("en")}, expected=before
        )
    assert failed
    assert read_config_bundle(tmp_path) == before
    assert (root / "project.json").read_bytes() == project("ru")


def test_coordinator_pins_one_generation_for_policy_and_qa_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    from harness.configuration_schema import PACKAGE
    from harness.orchestration.core import config

    root = tmp_path / ".harness"
    root.mkdir()
    shutil.copytree(PACKAGE / "orchestration/roles", root / "orchestration/roles")
    old_project = json.loads(project("ru"))
    old_project["qa_gate_commands"] = ["make old"]
    old: configuration.Bundle = {
        "project.json": json.dumps(old_project).encode(),
        "orchestration.json": b'{"infrastructure_retry_policy":{"enabled":true}}',
    }
    new_project = {**old_project, "qa_gate_commands": ["make new"]}
    new: configuration.Bundle = {
        "project.json": json.dumps(new_project).encode(),
        "orchestration.json": b'{"infrastructure_retry_policy":{"enabled":false}}',
    }
    calls = []

    def interleaved(repo: Path) -> configuration.Bundle:
        calls.append(repo)
        return old if len(calls) == 1 else new

    monkeypatch.setattr(configuration, "read_config_bundle", interleaved)
    if hasattr(config, "read_config_bundle"):
        monkeypatch.setattr(config, "read_config_bundle", interleaved)
    resolved = config._config(tmp_path)
    assert resolved["infrastructure_retry_policy"]["enabled"] is True
    assert resolved["verification_commands"] == ["make old"]
    assert calls == [tmp_path]


def test_health_readers_keep_previous_generation_until_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    from harness.configuration_schema import PACKAGE
    from harness.health.checks.orchestration import _attention_policy_threshold
    from harness.health.checks.tracker import _base_branch
    from harness.health.project_files import verification_routing_health

    root = tmp_path / ".harness"
    root.mkdir()
    shutil.copytree(PACKAGE / "orchestration/roles", root / "orchestration/roles")
    old_orchestration = {
        "infrastructure_retry_policy": {"enabled": True},
        "attention_policy": {"stale_dispatch_seconds": 20},
        "verification_commands": ["make old"],
    }
    (root / "project.json").write_bytes(project("ru"))
    (root / "orchestration.json").write_bytes(json.dumps(old_orchestration).encode())
    before = read_config_bundle(tmp_path)
    new_project = {**json.loads(project("en")), "base_branch": "release"}
    new_orchestration = {
        **old_orchestration,
        "attention_policy": {"stale_dispatch_seconds": 30},
        "developer_verification_commands": ["make focused"],
    }
    atomic = store_module._atomic

    def interrupt_before_commit(path: Path, raw: bytes | None) -> None:
        if (
            path.name == ".transaction.json"
            and raw
            and json.loads(raw)["state"] == "committed"
        ):
            raise KeyboardInterrupt
        atomic(path, raw)

    monkeypatch.setattr(store_module, "_atomic", interrupt_before_commit)
    with pytest.raises(KeyboardInterrupt):
        ConfigurationStore(tmp_path).apply(
            {
                "project.json": json.dumps(new_project).encode(),
                "orchestration.json": json.dumps(new_orchestration).encode(),
            },
            expected=before,
        )
    assert json.loads((root / "project.json").read_bytes())["base_branch"] == "release"
    assert _base_branch(tmp_path) == "main"
    assert _attention_policy_threshold(tmp_path) == 20
    assert verification_routing_health(tmp_path)
    monkeypatch.setattr(store_module, "_atomic", atomic)
    ConfigurationStore(tmp_path).recover()
    assert read_config_bundle(tmp_path) == before


def test_readers_accept_a_symlinked_or_relative_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "real"
    (repo / ".harness").mkdir(parents=True)
    (repo / ".harness/project.json").write_bytes(project("ru"))
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    assert read_config_bundle(link)["project.json"] == project("ru")
    assert configuration.read_config_file(link / ".harness/project.json") == project(
        "ru"
    )
    monkeypatch.chdir(repo)
    assert configuration.read_config_file(Path(".harness/project.json")) == project(
        "ru"
    )
    (repo / ".harness/orchestration.json").symlink_to(repo / "outside.json")
    with pytest.raises(ValueError, match="ссылками"):
        read_config_bundle(link)


def test_environment_references_are_not_secrets_and_backup_failure_names_the_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    document = json.loads(project("ru"))
    document["qa_gate_commands"] = ["TOKEN=$CI_TOKEN pytest"]
    (root / "project.json").write_bytes(json.dumps(document).encode())
    store = ConfigurationStore(tmp_path)
    store.apply({"project.json": project("en")}, expected=read_config_bundle(tmp_path))
    document["qa_gate_commands"] = ["TOKEN=abc123 pytest"]
    (root / "project.json").write_bytes(json.dumps(document).encode())
    with pytest.raises(ValueError, match="project.json содержит значение"):
        store.apply(
            {"project.json": project("ru")}, expected=read_config_bundle(tmp_path)
        )


def test_damaged_versions_stay_listed_with_a_reason_and_incompatibility_is_explained(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    (root / "project.json").write_bytes(project("ru"))
    store = ConfigurationStore(tmp_path)
    good = store.save(read_config_bundle(tmp_path), "good")
    broken = store.root / "20200101T000000Z-aaaaaaaaaaaa"
    broken.mkdir()
    (broken / "manifest.json").write_text("{", encoding="utf-8")
    listed = {v.id: v for v in store.versions()}
    assert listed[good.id].problem == ""
    assert listed[broken.name].problem
    invalid = json.loads(project("ru"))
    invalid["language"] = "xx"
    bundle = {"project.json": json.dumps(invalid).encode(), "orchestration.json": None}
    with pytest.raises(ValueError, match="language"):
        store._validate(bundle)


def test_versions_and_backups_live_inside_the_project_store(tmp_path: Path) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    (root / "project.json").write_bytes(project("ru"))
    original = read_config_bundle(tmp_path)
    store = ConfigurationStore(tmp_path)
    assert store.root == (tmp_path / ".harness" / "config_versions").resolve()
    assert not store.root.exists()  # looking at the path creates nothing
    chosen = store.save({**original, "project.json": project("en")}, "English")
    backup = store.apply(store.load(chosen.id), expected=original)
    assert store.root.is_dir() and (store.root / ".gitignore").read_text() == "*\n"
    stored = {path.name for path in store.root.iterdir() if path.is_dir()}
    assert stored == {chosen.id, backup.id}
    assert store.load(backup.id)["project.json"] == project("ru")
    # A new instance reads the earlier versions and can go back to the original.
    again = ConfigurationStore(tmp_path)
    assert {v.id for v in again.versions()} == stored
    again.apply(again.load(backup.id), expected=read_config_bundle(tmp_path))
    assert read_config_bundle(tmp_path) == original
    assert (tmp_path / ".harness" / "project.json").read_bytes() == project("ru")


def test_two_projects_keep_separate_stores(tmp_path: Path) -> None:
    stores = []
    for name, language in (("first", "ru"), ("second", "en")):
        repo = tmp_path / name
        (repo / ".harness").mkdir(parents=True)
        (repo / ".harness" / "project.json").write_bytes(project(language))
        store = ConfigurationStore(repo)
        store.save(read_config_bundle(repo), name)
        stores.append(store)
    first, second = stores
    assert first.root != second.root
    assert [v.description for v in first.versions()] == ["first"]
    assert [v.description for v in second.versions()] == ["second"]


def test_versions_saved_under_the_former_name_are_moved_not_lost(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    (root / "project.json").write_bytes(project("ru"))
    saved = ConfigurationStore(tmp_path).save(read_config_bundle(tmp_path), "старая")
    (root / "config_versions").rename(root / "config-versions")
    store = ConfigurationStore(tmp_path)
    assert not (root / "config-versions").exists()
    assert [v.id for v in store.versions()] == [saved.id]
    assert store.load(saved.id) == read_config_bundle(tmp_path)


def test_an_existing_new_store_wins_over_the_former_directory(tmp_path: Path) -> None:
    root = tmp_path / ".harness"
    (root / "config-versions").mkdir(parents=True)
    (root / "config_versions").mkdir()
    store = ConfigurationStore(tmp_path)
    assert store.root.name == "config_versions"
    assert (root / "config-versions").is_dir()


def test_project_set_with_test_runner_is_saved_and_loaded_without_problems(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    runner = {"command": ["python3", "-m", "pytest", "{test_id}"]}
    raw = json.dumps({**json.loads(project("ru")), "test_runner": runner}).encode()
    (root / "project.json").write_bytes(raw)
    bundle = read_config_bundle(tmp_path)
    sut = ConfigurationStore(tmp_path)

    problems = sut.problems(bundle)
    saved = sut.save(bundle, "test runner")

    assert problems == []
    assert sut.load(saved.id)["project.json"] == raw
