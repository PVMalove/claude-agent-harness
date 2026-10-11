"""Saved configurations are durable, reversible and independent of draft application."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import harness.configuration as configuration
from harness.configuration import ConfigurationStore, read_config_bundle
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
    atomic = configuration._atomic

    def interrupted(path: Path, raw: bytes | None) -> None:
        if path == root / "orchestration.json":
            assert read_config_bundle(tmp_path) == before
            raise KeyboardInterrupt("simulated process interruption")
        atomic(path, raw)

    monkeypatch.setattr(configuration, "_atomic", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ConfigurationStore(tmp_path).apply(
            {"project.json": project("en")}, expected=before
        )
    assert (root / "project.json").read_bytes() == project("en")
    assert read_config_bundle(tmp_path) == before
    monkeypatch.setattr(configuration, "_atomic", atomic)
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
    atomic = configuration._atomic

    def interrupted(path: Path, raw: bytes | None) -> None:
        if path == root / "orchestration.json":
            raise KeyboardInterrupt
        atomic(path, raw)

    monkeypatch.setattr(configuration, "_atomic", interrupted)
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
    with pytest.raises(ValueError, match="Секреты"):
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
    atomic = configuration._atomic

    def interrupt(path: Path, raw: bytes | None) -> None:
        if path == root / "orchestration.json":
            raise KeyboardInterrupt
        atomic(path, raw)

    monkeypatch.setattr(configuration, "_atomic", interrupt)
    with pytest.raises(KeyboardInterrupt):
        ConfigurationStore(tmp_path).apply(
            {"project.json": project("en")}, expected=read_config_bundle(tmp_path)
        )
    monkeypatch.setattr(configuration, "_atomic", atomic)
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
        configuration._json(
            {
                "schema": 1,
                "state": "prepared",
                "id": "test",
                "before": configuration._encode(
                    {**before, "project.json": project("en")}
                ),
                "after": configuration._encode(before),
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
    atomic = configuration._atomic
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

    monkeypatch.setattr(configuration, "_atomic", fail_after_replace)
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
    from harness.console.configuration import PACKAGE
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
    from harness.console.configuration import PACKAGE
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
    atomic = configuration._atomic

    def interrupt_before_commit(path: Path, raw: bytes | None) -> None:
        if (
            path.name == ".transaction.json"
            and raw
            and json.loads(raw)["state"] == "committed"
        ):
            raise KeyboardInterrupt
        atomic(path, raw)

    monkeypatch.setattr(configuration, "_atomic", interrupt_before_commit)
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
    monkeypatch.setattr(configuration, "_atomic", atomic)
    ConfigurationStore(tmp_path).recover()
    assert read_config_bundle(tmp_path) == before
