"""harness.console.repo_map: Repo Map facts for the console on a real repository and cache fixture
(tests/_console_repo_map_fixture.py). No textual needed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from _console_repo_map_fixture import (
    HUB,
    build_repo_map_fixture,
    cache_dir,
    init_repo,
    map_payload,
    write_cached_map,
)
from harness.console import repo_map as console_repo_map
from harness.console.export import export_text, render_markdown
from harness.console.repo_map import RepoMapView
from harness.console.runner import capturing_runner


class _RecordingRunner:
    def __init__(self, result: "subprocess.CompletedProcess[str]") -> None:
        self.result = result
        self.calls: list[tuple[list[str], Path | None]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append((list(argv), cwd))
        return self.result


def _completed(
    stdout: str = "", stderr: str = "", code: int = 0
) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(["repo_map"], code, stdout, stderr)


def _view(commit: str = "f" * 40) -> RepoMapView:
    view = console_repo_map.parse_map(map_payload(commit), origin="кэш")
    assert not isinstance(view, str)
    return view


def test_head_commit_is_the_full_sha(tmp_path: Path) -> None:
    commit = init_repo(tmp_path)
    assert console_repo_map.head_commit(tmp_path) == commit
    assert len(commit) == 40
    assert console_repo_map.head_commit(tmp_path / "missing") is None


def test_cached_map_for_head_is_found_and_verified(tmp_path: Path) -> None:
    commit = build_repo_map_fixture(tmp_path)

    view = console_repo_map.cached_map(tmp_path, commit)

    assert view is not None
    assert view.origin == "кэш"
    assert view.commit == commit
    assert view.tier == "full"
    assert [item.path for item in view.files][0] == HUB


def test_cached_map_ignores_other_commits_and_corrupt_entries(tmp_path: Path) -> None:
    commit = init_repo(tmp_path)
    write_cached_map(tmp_path, map_payload("0" * 40), "b" * 64)
    broken = write_cached_map(tmp_path, map_payload(commit), "c" * 64)
    broken.write_text(
        broken.read_text("utf-8").replace("load_config", "tampered"), "utf-8"
    )
    (cache_dir(tmp_path) / ("d" * 64 + ".json")).write_text("{not json", "utf-8")

    assert console_repo_map.cached_map(tmp_path, commit) is None


def test_cached_map_prefers_full_tier_then_newest(tmp_path: Path) -> None:
    commit = init_repo(tmp_path)
    full = write_cached_map(tmp_path, map_payload(commit), "1" * 64)
    minimal = write_cached_map(tmp_path, map_payload(commit, tier="minimal"), "2" * 64)
    os.utime(full, (1_000_000, 1_000_000))
    os.utime(minimal, (2_000_000, 2_000_000))

    view = console_repo_map.cached_map(tmp_path, commit)
    assert view is not None and view.tier == "full"

    full.unlink()
    view = console_repo_map.cached_map(tmp_path, commit)
    assert view is not None and view.tier == "minimal"


def test_no_cache_directory_means_no_cached_map(tmp_path: Path) -> None:
    commit = init_repo(tmp_path)
    assert console_repo_map.cached_map(tmp_path, commit) is None


def test_parse_map_rejects_a_payload_outside_schema_v1() -> None:
    payload = map_payload("f" * 40)
    payload["extra_field_outside_v1"] = True
    files = payload["files"]
    assert isinstance(files, list)
    files[0]["owner"] = "not in schema"

    problem = console_repo_map.parse_map(payload, origin="построена")

    assert isinstance(problem, str)
    assert "схеме v1" in problem


def test_build_argv_prefers_the_installed_cli(tmp_path: Path) -> None:
    argv = console_repo_map.build_argv(tmp_path, "f" * 40)
    assert argv[:2] == [sys.executable, "-B"]
    assert argv[2].endswith(str(Path("harness") / "repo_map" / "repo_map.py"))
    assert argv[3:] == ["--repo", str(tmp_path), "--commit", "f" * 40]

    installed = tmp_path / ".harness" / "repo_map" / "repo_map.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("", "utf-8")
    assert console_repo_map.build_argv(tmp_path, "f" * 40)[2] == str(installed)


def test_build_map_runs_the_cli_through_the_runner(tmp_path: Path) -> None:
    commit = "f" * 40
    runner = _RecordingRunner(_completed(json.dumps(map_payload(commit))))

    view = console_repo_map.build_map(tmp_path, commit, runner)

    assert not isinstance(view, str)
    assert view.origin == "построена"
    assert runner.calls == [(console_repo_map.build_argv(tmp_path, commit), tmp_path)]


def test_build_map_reports_cli_failure_invalid_json_and_contract_drift(
    tmp_path: Path,
) -> None:
    failed = console_repo_map.build_map(
        tmp_path,
        "f" * 40,
        _RecordingRunner(
            _completed(stderr="error: bad commit\nremedy: pass HEAD", code=2)
        ),
    )
    assert isinstance(failed, str)
    assert "кодом 2" in failed and "remedy: pass HEAD" in failed

    not_json = console_repo_map.build_map(
        tmp_path, "f" * 40, _RecordingRunner(_completed("{"))
    )
    assert isinstance(not_json, str) and "не JSON" in not_json

    drift = console_repo_map.build_map(
        tmp_path,
        "f" * 40,
        _RecordingRunner(_completed(json.dumps({"schema_version": 2}))),
    )
    assert isinstance(drift, str) and "схеме v1" in drift


def test_build_map_with_the_real_cli_degrades_without_a_bundle(tmp_path: Path) -> None:
    commit = init_repo(tmp_path)

    view = console_repo_map.build_map(tmp_path, commit, capturing_runner)

    assert not isinstance(view, str), view
    assert view.commit == commit
    assert view.tier == "minimal"
    assert view.degradation_reason == "offline parser bundle unavailable"
    assert sorted(item.path for item in view.files) == [
        "README.md",
        "pkg/cli.py",
        "pkg/core.py",
    ]


def test_summary_has_tier_reason_commit_counts_and_provenance() -> None:
    view = _view()
    summary = console_repo_map.summary_lines(view)
    assert "tier: full (parser: bundle)" in summary
    assert "причина деградации: parser bundle applied" in summary
    assert f"commit: {'f' * 40}" in summary
    assert "файлов: 4" in summary
    assert "рёбер: 4" in summary
    assert "токенов (оценка): 512" in summary

    provenance = console_repo_map.provenance_lines(view)
    assert "bundle_source: local-cache" in provenance
    assert f"grammar: python@0.23.6 abi=14 sha256={'12' * 32}" in provenance


def test_symbol_search_matches_signatures_case_insensitively() -> None:
    view = _view()
    assert [
        item.path for item in console_repo_map.search_files(view, "LOAD_config")
    ] == [HUB]
    assert [item.path for item in console_repo_map.search_files(view, "main")] == [
        "pkg/cli.py"
    ]
    assert console_repo_map.search_files(view, "absent") == []
    assert len(console_repo_map.search_files(view, "  ")) == 4


def test_relations_group_by_direction_kind_and_confidence() -> None:
    view = _view()
    groups = [
        (group.direction, group.kind, group.confidence, group.paths)
        for group in console_repo_map.relations(view, HUB)
    ]
    assert groups == [
        ("исходящие", "unique-name-ref", "medium", ("pkg/cli.py",)),
        ("входящие", "import", "high", ("pkg/cli.py",)),
        ("входящие", "unique-name-ref", "medium", ("pkg/cli.py",)),
        ("входящие", "ambiguous-name-ref", "low", ("pkg/broken.py",)),
    ]
    assert console_repo_map.relations_text(view, "README.md") == "README.md\nсвязей нет"


def test_hubs_rank_by_distinct_incoming_neighbours() -> None:
    view = _view()
    assert console_repo_map.hubs(view) == [(HUB, 2), ("pkg/cli.py", 1)]
    assert console_repo_map.hubs(view, limit=1) == [(HUB, 2)]


def test_diagnostics_and_file_labels() -> None:
    view = _view()
    assert console_repo_map.diagnostics_text(view) == "syntax_error: pkg/broken.py"
    assert console_repo_map.file_label(view.files[2]) == "broken.py [syntax_error]"
    minimal = console_repo_map.parse_map(
        map_payload("f" * 40, tier="minimal"), origin="кэш"
    )
    assert not isinstance(minimal, str)
    assert console_repo_map.file_label(minimal.files[0]) == "core.py"
    assert console_repo_map.diagnostics_text(minimal) == "диагностик нет"
    assert console_repo_map.hubs_text(minimal) == "рёбер нет — хабов нет"


def test_markdown_document_and_json_export(tmp_path: Path) -> None:
    view = _view()
    text = render_markdown(console_repo_map.map_document(view))
    assert text.startswith(f"# Repo Map {'f' * 12}\n")
    assert "- **Tier:** full (bundle)" in text
    assert "## Хабы (входящая степень)" in text
    assert "    def load_config(path)" in text
    assert "pkg/cli.py -> pkg/core.py (import, high)" in text

    path = export_text(
        tmp_path,
        console_repo_map.map_json(view),
        slug=console_repo_map.export_slug(view),
        suffix=".json",
        now=datetime(2026, 9, 27, 18, 5, 9, tzinfo=UTC),
    )
    assert path == (
        tmp_path
        / "docs"
        / "tasks"
        / "console-exports"
        / f"2026-09-27-180509-repo-map-{'f' * 12}.json"
    )
    assert json.loads(path.read_text("utf-8")) == map_payload("f" * 40)
