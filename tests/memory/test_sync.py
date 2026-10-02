"""Tracker snapshots through the explicit public writer and offline search."""

import importlib
import json
import subprocess
import sqlite3
import os
import sys
from pathlib import Path

import pytest

from harness.memory import search
from .test_build import configure
from .test_worktrees import checkouts as checkouts

SNAPSHOT = ".harness/.sandboxes/memory/snapshot"


@pytest.fixture
def remote_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://github.com/team/project.git",
        ],
        check=True,
    )
    configure(
        tmp_path,
        source_types=["task_archive", "completion_report"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
    )
    return tmp_path


def fake_inventory(
    monkeypatch: pytest.MonkeyPatch,
    items: list[dict[str, object]],
    comments: list[dict[str, object]] | None = None,
) -> None:
    def page(argv: list[str], repo: Path) -> list[dict[str, object]]:
        if "page=2" in argv[-1] or "pulls?" in argv[-1]:
            return []
        return (comments or []) if "comments?" in argv[-1] else items

    monkeypatch.setattr(
        importlib.import_module("harness.memory.sync"), "fetch_page", page
    )


def test_ungranted_snapshot_is_not_read_by_offline_build(remote_repo: Path) -> None:
    from harness.memory import build

    configure(remote_repo, source_types=["adr"], allow_paths=["docs/adr/*.md"])
    snapshot = remote_repo / SNAPSHOT
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text("invalid raw secret=shouldnotread")
    assert build(remote_repo)["indexed"] == 0


def test_sync_sanitizes_all_retained_fields_and_ignores_unmarked_comments(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    configure(
        remote_repo,
        source_types=["task_archive", "completion_report"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
        redact_rules=["customprivate"],
    )
    report = {
        "title": "reportword token=titleprivate",
        "output": "customprivate password=reportprivate",
        "risks": ["riskword https://private.invalid/token"],
        "blockers": "/tmp/private.log",
        "unknown": "unknownprivate",
        "checks_run": [{"command": "commandprivate"}],
    }
    fake_inventory(
        monkeypatch,
        [
            {
                "number": 1,
                "state": "closed",
                "title": "ticketword customprivate",
                "body": "token=bodyprivate",
                "updated_at": "credential=dateprivate",
            }
        ],
        [
            {
                "id": 4,
                "body": "## Completion report\n```json\n"
                + json.dumps(report)
                + "\n```",
            },
            {"id": 5, "body": "unmarkedprivate"},
            {"id": 6, "body": "## Completion report\n```json\ninvalid\n```"},
        ],
    )
    assert sync(remote_repo)["skipped_reports"] == 2
    serialized = "".join(
        path.read_text() for path in (remote_repo / SNAPSHOT).rglob("*.json")
    )
    with sqlite3.connect(
        remote_repo / ".harness/.sandboxes/cache/memory/index.sqlite3"
    ) as db:
        serialized += "\n".join(db.iterdump())
    for sentinel in (
        "customprivate",
        "titleprivate",
        "reportprivate",
        "bodyprivate",
        "dateprivate",
        "private.invalid",
        "/tmp/private.log",
        "unknownprivate",
        "commandprivate",
        "unmarkedprivate",
    ):
        assert sentinel not in serialized
    assert search(remote_repo, "riskword")["pointers"]


@pytest.mark.parametrize(
    "prefix", ["glpat-", "github_pat_"], ids=["gitlab_pat", "github_fine_grained_pat"]
)
def test_sync_redacts_standalone_tracker_tokens_before_persistence(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    from harness.memory import sync

    configure(
        remote_repo,
        source_types=["task_archive", "completion_report"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
        redact_rules=[],
    )
    token = prefix + "SyntheticMemoryFixture0123456789_A-b"
    fake_inventory(
        monkeypatch,
        [
            {
                "number": 1,
                "state": "closed",
                "title": "ticketword " + token,
                "body": "bodyword " + token,
                "updated_at": token,
            }
        ],
        [
            {
                "id": 4,
                "body": "## Completion report\n```json\n"
                + json.dumps(
                    {
                        "title": token,
                        "ticket": token,
                        "role": token,
                        "outcome": token,
                        "output": token,
                        "lessons": ["lessonword " + token],
                        "date": token,
                    }
                )
                + "\n```",
            }
        ],
    )
    result = sync(remote_repo)
    assert result["status"] == "synced"
    snapshot_text = "".join(
        path.read_text() for path in (remote_repo / SNAPSHOT).rglob("*.json")
    )
    with sqlite3.connect(
        remote_repo / ".harness/.sandboxes/cache/memory/index.sqlite3"
    ) as db:
        index_text = "\n".join(db.iterdump())
        assert db.execute("SELECT count(*) FROM documents").fetchone() == (2,)
    snapshot_retained = token in snapshot_text
    index_retained = token in index_text
    assert not snapshot_retained
    assert not index_retained
    assert search(remote_repo, "bodyword")["pointers"]
    assert search(remote_repo, "lessonword")["pointers"]


@pytest.mark.parametrize(
    "types,paths",
    [
        ([], [SNAPSHOT + "/**"]),
        (["ledger"], [SNAPSHOT + "/**"]),
        (["task_archive"], ["docs/tasks/**"]),
    ],
)
def test_sync_requires_independent_type_and_path_grants(
    remote_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    types: list[str],
    paths: list[str],
) -> None:
    from harness.memory import sync

    configure(remote_repo, source_types=types, allow_paths=paths)

    def forbidden(repo: Path) -> tuple[str, str]:
        pytest.fail("ungranted tracker access")

    monkeypatch.setattr(
        importlib.import_module("harness.memory.sync"), "tracker", forbidden
    )
    assert sync(remote_repo)["status"] == "disabled"
    assert not (remote_repo / SNAPSHOT).exists()


def test_completion_only_grant_never_publishes_ticket_text(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    configure(
        remote_repo,
        source_types=["completion_report"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
    )
    fake_inventory(
        monkeypatch,
        [{"number": 1, "state": "closed", "title": "ticketprivate"}],
        [
            {
                "id": 8,
                "body": '## Completion report\n```json\n{"output":"reportword"}\n```',
            }
        ],
    )
    assert sync(remote_repo)["synced"] == 1
    assert search(remote_repo, "ticketprivate")["pointers"] == []
    assert search(remote_repo, "reportword")["pointers"]


def test_sync_rejects_symlink_namespace_before_publication(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from harness.memory import sync

    fake_inventory(
        monkeypatch, [{"number": 1, "state": "closed", "title": "ticketword"}]
    )
    directory = remote_repo / SNAPSHOT
    directory.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    directory.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        sync(remote_repo)
    assert list(elsewhere.iterdir()) == []


def test_remote_report_projects_identity_and_skips_invalid_structure(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    fake_inventory(
        monkeypatch,
        [{"number": 1, "state": "closed"}],
        [
            {
                "id": 5,
                "body": '## Completion report\n```json\n{"output":{},"unknown":"x"}\n```',
            },
            {
                "id": 6,
                "body": '## Completion report\n```json\n{"output":"reportword","role":"developerword","ticket":"#1","outcome":"completed"}\n```',
            },
        ],
    )
    result = sync(remote_repo)
    assert result["skipped_reports"] == 1
    assert search(remote_repo, "developerword")["pointers"]


def test_cli_error_does_not_expose_tracker_output(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    directory = remote_repo / "bin"
    directory.mkdir()
    if sys.platform == "win32":
        executable = directory / "gh.cmd"
        executable.write_text(
            f'@"{sys.executable}" -c "import sys; sys.stderr.write(\'token=errorprivate\'); sys.exit(7)"\n',
            encoding="utf-8",
        )
    else:
        executable = directory / "gh"
        executable.write_text(
            f"#!{sys.executable}\nimport sys\nsys.stderr.write('token=errorprivate')\nsys.exit(7)\n"
        )
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    with pytest.raises(ValueError, match="gh failed.*exit 7") as error:
        sync(remote_repo)
    assert "errorprivate" not in str(error.value)
    assert not (remote_repo / SNAPSHOT).exists()


def test_windows_cmd_shim_keeps_query_string_quoted(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("harness.memory.sync")
    launched: list[object] = []

    def popen(cmd: object, **kwargs: object) -> object:
        launched.append(cmd)
        raise OSError

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.shutil, "which", lambda name: rf"C:\bin\{name}.cmd")
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    endpoint = "repos/o/r/issues?state=closed&per_page=100&page=1"
    with pytest.raises(ValueError, match="gh unavailable"):
        module.fetch_page(["gh", "api", endpoint], remote_repo)
    assert launched == [rf'cmd.exe /d /s /c ""C:\bin\gh.cmd" "api" "{endpoint}""']


def test_narrow_ticket_grant_does_not_fetch_pull_requests(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    configure(
        remote_repo,
        source_types=["task_archive"],
        allow_paths=[SNAPSHOT + "/records/ticket-*.json"],
    )

    def page(argv: list[str], repo: Path) -> list[dict[str, object]]:
        assert "pulls?" not in argv[-1]
        return []

    monkeypatch.setattr(
        importlib.import_module("harness.memory.sync"), "fetch_page", page
    )
    assert sync(remote_repo)["synced"] == 0


def snapshot_state(repo: Path) -> dict[str, tuple[bytes, int]]:
    paths = list((repo / SNAPSHOT).rglob("*.json")) + [
        repo / ".harness/.sandboxes/cache/memory/index.sqlite3"
    ]
    return {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}


def test_identical_sync_reuses_files_and_complete_inventory_reconciles(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    fake_inventory(monkeypatch, [{"number": 1, "state": "closed", "title": "oldword"}])
    sync(remote_repo)
    before = snapshot_state(remote_repo)
    sync(remote_repo)
    assert snapshot_state(remote_repo) == before
    fake_inventory(monkeypatch, [{"number": 2, "state": "closed", "title": "newword"}])
    sync(remote_repo)
    assert search(remote_repo, "oldword")["pointers"] == []
    assert search(remote_repo, "newword")["pointers"]
    assert all(Path(path).exists() for path in before)
    fake_inventory(monkeypatch, [])
    sync(remote_repo)
    assert search(remote_repo, "newword")["pointers"] == []


@pytest.mark.parametrize(
    "failure", ["late_page", "cap", "policy_race", "manifest_race"]
)
def test_failed_collection_keeps_previous_selected_snapshot_and_index(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from harness.memory import sync

    fake_inventory(monkeypatch, [{"number": 1, "state": "closed", "title": "oldword"}])
    sync(remote_repo)
    before = snapshot_state(remote_repo)
    module = importlib.import_module("harness.memory.sync")

    def page(argv: list[str], repo: Path) -> list[dict[str, object]]:
        if "page=2" in argv[-1]:
            if failure == "late_page":
                raise ValueError(
                    "memory sync: gh failed (exit 1); check access and network"
                )
            return []
        if "issues?" not in argv[-1]:
            return []
        if failure == "policy_race":
            configure(repo, source_types=[], allow_paths=[])
        if failure == "manifest_race":
            (repo / SNAPSHOT / "manifest.json").write_text('{"version":1,"records":[]}')
        return [{"number": 2, "state": "closed", "title": "newword"}]

    monkeypatch.setattr(module, "fetch_page", page)
    if failure == "cap":
        monkeypatch.setattr(module, "MAX_RECORDS", 0)
    with pytest.raises(ValueError):
        sync(remote_repo)
    after = snapshot_state(remote_repo)
    for path, evidence in before.items():
        if failure != "manifest_race" or not path.endswith("manifest.json"):
            assert after[path] == evidence
    assert not any(
        "newword" in path.read_text()
        for path in (remote_repo / SNAPSHOT).rglob("*.json")
    )


def test_index_failure_is_explicit_and_offline_rebuild_recovers(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync, rebuild, build

    fake_inventory(monkeypatch, [{"number": 1, "state": "closed", "title": "oldword"}])
    sync(remote_repo)
    index = remote_repo / ".harness/.sandboxes/cache/memory/index.sqlite3"
    before = index.read_bytes()
    fake_inventory(monkeypatch, [{"number": 2, "state": "closed", "title": "newword"}])

    def broken(repo: Path) -> dict[str, object]:
        raise ValueError("index failed")

    module = importlib.import_module("harness.memory.sync")
    monkeypatch.setattr(module, "refresh", broken)
    assert sync(remote_repo)["status"] == "snapshot_synced_index_failed"
    assert index.read_bytes() == before

    def offline(argv: list[str], repo: Path) -> list[dict[str, object]]:
        pytest.fail("offline operation accessed tracker")

    monkeypatch.setattr(module, "fetch_page", offline)
    rebuild(remote_repo)
    build(remote_repo)
    assert search(remote_repo, "newword")["pointers"]
    assert search(remote_repo, "oldword")["pointers"] == []


@pytest.mark.parametrize("tool,host", [("gh", "github.com"), ("glab", "gitlab.com")])
def test_installed_sync_uses_real_fake_cli_without_orchestration(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch, tool: str, host: str
) -> None:
    from .test_delivery import CLI

    subprocess.run(
        [
            "git",
            "-C",
            str(remote_repo),
            "remote",
            "set-url",
            "origin",
            f"https://{host}/team/project.git",
        ],
        check=True,
    )
    installed = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "init",
            str(remote_repo),
            "--capability",
            "pvmalove-suite",
            "--qa-gate-command",
            "true",
        ],
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    configure(
        remote_repo,
        source_types=["task_archive"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
    )
    if sys.platform == "win32":
        py_script = remote_repo / f"{tool}_fake.py"
        py_script.write_text(
            "import sys,json\ne=sys.argv[-1]\nprint(json.dumps([] if 'page=2' in e else [{'number':1,'iid':1,'state':'closed','title':'installedword'}]))\n",
            encoding="utf-8",
        )
        executable = remote_repo / f"{tool}.cmd"
        executable.write_text(
            f'@"{sys.executable}" "{py_script}" %*\n', encoding="utf-8"
        )
    else:
        executable = remote_repo / tool
        executable.write_text(
            f"#!{sys.executable}\nimport sys,json\ne=sys.argv[-1]\nprint(json.dumps([] if 'page=2' in e else [{{'number':1,'iid':1,'state':'closed','title':'installedword'}}]))\n"
        )
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(remote_repo) + os.pathsep + os.environ["PATH"])
    code = "import importlib.util,sys\nfrom pathlib import Path\ns=importlib.util.spec_from_file_location('harness','.harness/__init__.py',submodule_search_locations=['.harness'])\np=importlib.util.module_from_spec(s)\nsys.modules['harness']=p\ns.loader.exec_module(p)\nfrom harness.memory import sync,search\nassert sync(Path.cwd())['status']=='synced'\nassert search(Path.cwd(),'installedword')['pointers']\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=remote_repo,
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not (remote_repo / ".harness/orchestration").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "invalid_json",
        "timeout",
        "output_limit",
        "missing_cli",
        "page_limit",
        "request_limit",
    ],
)
def test_tracker_process_failures_preserve_previous_bytes(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from harness.memory import sync

    fake_inventory(monkeypatch, [{"number": 1, "state": "closed", "title": "oldword"}])
    sync(remote_repo)
    before = snapshot_state(remote_repo)
    monkeypatch.undo()
    module = importlib.import_module("harness.memory.sync")
    directory = remote_repo / "fake-bin"
    directory.mkdir()
    (directory / "git").symlink_to("/usr/bin/git")
    executable = directory / "gh"
    scripts = {
        "invalid_json": "print('broken token=private')",
        "timeout": "import time;time.sleep(1)",
        "output_limit": "print('x' * 10000)",
        "page_limit": 'print(\'[{"number":1,"state":"closed"}]\')',
        "request_limit": "print('[]')",
    }
    if failure != "missing_cli":
        executable.write_text(f"#!{sys.executable}\n" + scripts[failure] + "\n")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory))
    if failure == "timeout":
        monkeypatch.setattr(module, "TIMEOUT", 0.1)
    if failure == "output_limit":
        monkeypatch.setattr(module, "MAX_RESPONSE", 128)
    if failure == "page_limit":
        monkeypatch.setattr(module, "MAX_PAGES", 1)
    if failure == "request_limit":
        monkeypatch.setattr(module, "MAX_REQUESTS", 1)
    with pytest.raises(ValueError) as error:
        sync(remote_repo)
    assert "private" not in str(error.value)
    assert snapshot_state(remote_repo) == before


def test_linked_worktree_reads_main_snapshot_but_cannot_sync(
    checkouts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: F811 (pytest fixture injection)
    from harness.memory import sync

    main, linked = checkouts
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "remote",
            "add",
            "origin",
            "https://github.com/team/project.git",
        ],
        check=True,
    )
    configure(
        main, source_types=["task_archive"], allow_paths=[SNAPSHOT + "/records/*.json"]
    )
    fake_inventory(
        monkeypatch, [{"number": 1, "state": "closed", "title": "sharedword"}]
    )
    sync(main)
    before = snapshot_state(main)
    with pytest.raises(ValueError, match="main checkout"):
        sync(linked)
    assert search(linked, "sharedword")["pointers"]
    assert snapshot_state(main) == before


def test_merged_pull_request_pointer_preserves_terminal_status(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    configure(
        remote_repo,
        source_types=["task_archive"],
        allow_paths=[SNAPSHOT + "/records/pull_request-*.json"],
    )

    def page(argv: list[str], repo: Path) -> list[dict[str, object]]:
        return (
            []
            if "page=2" in argv[-1]
            else [
                {
                    "number": 4,
                    "state": "closed",
                    "merged_at": "2026-10-01",
                    "title": "mergedword",
                }
            ]
        )

    monkeypatch.setattr(
        importlib.import_module("harness.memory.sync"), "fetch_page", page
    )
    sync(remote_repo)
    pointers = search(remote_repo, "mergedword")["pointers"]
    assert isinstance(pointers, list) and pointers[0]["status"] == "merged"


def test_context_package_assembles_from_snapshot_without_tracker(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync
    from harness.orchestration.ledger.lifecycle import LifecycleLedger
    from harness.orchestration.workflow.context_package import _persist_context_package
    from .test_build import git

    configure(
        remote_repo,
        source_types=["task_archive"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
        min_similarity=0,
    )
    git(remote_repo, "config", "user.name", "Fixture")
    git(remote_repo, "config", "user.email", "fixture@example.invalid")
    (remote_repo / "app.py").write_text("def packageword():\n    return 1\n")
    git(remote_repo, "add", "app.py")
    git(remote_repo, "commit", "-qm", "fixture")
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=remote_repo, text=True
    ).strip()
    fake_inventory(
        monkeypatch, [{"number": 1, "state": "closed", "title": "packageword"}]
    )
    sync(remote_repo)

    def offline(argv: list[str], repo: Path) -> list[dict[str, object]]:
        pytest.fail("Context Package accessed tracker")

    monkeypatch.setattr(
        importlib.import_module("harness.memory.sync"), "fetch_page", offline
    )
    root = remote_repo / "state"
    ledger = LifecycleLedger(root)
    ledger.ensure()
    batch: dict[str, object] = {
        "batch_id": "batch-memory",
        "base_commit": sha,
        "goal": "packageword",
        "definition_of_done": [],
        "context_packages": [],
    }
    package = _persist_context_package(
        remote_repo,
        root,
        ledger,
        batch,
        role="shared",
        snapshot=sha,
        inclusion_reason="test",
    )
    assert package["memory"]["pointers"]


def test_immutable_record_collision_keeps_previous_selection(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    fake_inventory(monkeypatch, [{"number": 1, "state": "closed", "title": "oldword"}])
    sync(remote_repo)
    selected = json.loads((remote_repo / SNAPSHOT / "manifest.json").read_text())[
        "records"
    ][0]
    (remote_repo / selected).write_text("collision")
    before = snapshot_state(remote_repo)
    with pytest.raises(ValueError, match="collision"):
        sync(remote_repo)
    assert snapshot_state(remote_repo) == before


def test_superseded_remote_report_is_not_searchable(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    fake_inventory(
        monkeypatch,
        [{"number": 1, "state": "closed"}],
        [
            {
                "id": 9,
                "body": '## Completion report\n```json\n{"output":"obsoleteword","status":"superseded by ADR-0010"}\n```',
            }
        ],
    )
    sync(remote_repo)
    assert search(remote_repo, "obsoleteword")["pointers"] == []


def test_specific_ticket_id_path_grant_is_supported(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    configure(
        remote_repo,
        source_types=["task_archive"],
        allow_paths=[SNAPSHOT + "/records/ticket-42-*.json"],
    )
    fake_inventory(
        monkeypatch,
        [
            {"number": 42, "state": "closed", "title": "selectedword"},
            {"number": 43, "state": "closed", "title": "excludedword"},
        ],
    )
    assert sync(remote_repo)["synced"] == 1
    assert search(remote_repo, "selectedword")["pointers"]
    assert search(remote_repo, "excludedword")["pointers"] == []


@pytest.mark.parametrize("host,tool", [("github.com", "gh"), ("gitlab.com", "glab")])
def test_sync_imports_terminal_records_and_marked_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, tool: str
) -> None:
    from harness.memory import sync

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            f"https://{host}/team/project.git",
        ],
        check=True,
    )
    configure(
        tmp_path,
        source_types=["task_archive", "completion_report"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
    )
    calls: list[list[str]] = []

    def page(argv: list[str], repo: Path) -> list[dict[str, object]]:
        calls.append(argv)
        assert argv[0] == tool
        endpoint = argv[-1]
        if "page=2" in endpoint:
            return []
        if "comments?" in endpoint or "notes?" in endpoint:
            return [
                {
                    "id": 31,
                    "body": "## Completion report\n```json\n"
                    + json.dumps({"output": "reportword", "role": "developer"})
                    + "\n```",
                }
            ]
        pull = "pulls?" in endpoint or "merge_requests?" in endpoint
        return [
            {
                "number": 2 if pull else 1,
                "iid": 2 if pull else 1,
                "title": "pullword" if pull else "ticketword",
                "state": "closed",
                "body": "archiveword",
                "description": "archiveword",
            }
        ]

    monkeypatch.setattr(
        importlib.import_module("harness.memory.sync"), "fetch_page", page
    )
    result = sync(tmp_path)
    assert result["status"] == "synced"
    for query in ("ticketword", "pullword", "reportword"):
        pointers = search(tmp_path, query)["pointers"]
        assert isinstance(pointers, list) and pointers
        assert Path(tmp_path / pointers[0]["path"]).is_file()
    report_pointers = search(tmp_path, "reportword")["pointers"]
    assert isinstance(report_pointers, list)
    assert report_pointers[0]["status"] == "не подтверждено человеком"
    assert calls
