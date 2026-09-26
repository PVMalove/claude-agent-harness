"""Модули Repo Map за пределами CLI: бюджет, чтение Git, worker и диагностика сбоев."""

import importlib
import importlib.machinery
import importlib.util
import io
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _parser_bundle_fixtures import build_bundle_dir, write_worker_script

from harness.repo_map import budget, bundle_worker, parser_bundle
from harness.repo_map.git_source import Blob, read_blobs
from harness.repo_map.graph import GRAMMAR_FAMILIES, Diagnostic, EdgeRecord

ROOT = Path(__file__).resolve().parents[1]

# The worker imports `tree_sitter*` for type checking only and is checked by the separate bundle mypy
# run; importing it by name keeps the main mypy run from following it.
worker = importlib.import_module("harness.repo_map.tree_sitter_worker")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _reference_selection(
    payload: dict[str, object],
    ordered: list[str],
    files: dict[str, dict[str, object]],
    edges: list[EdgeRecord],
    diagnostics: list[Diagnostic],
    max_tokens: int,
) -> str:
    """The original quadratic selection: re-serialize the whole map for every candidate file."""
    payload["files"], payload["edges"], payload["diagnostics"] = [], [], []
    encoded, _ = budget.sized(payload)
    selected: set[str] = set()
    for path in ordered:
        candidate = selected | {path}
        payload["files"] = [files[item] for item in ordered if item in candidate]
        payload["edges"] = [e for e in edges if e["source"] in candidate and e["target"] in candidate]
        payload["diagnostics"] = [d for d in diagnostics if d["path"] in candidate]
        trial, size = budget.sized(payload)
        if size <= max_tokens:
            selected, encoded = candidate, trial
    return encoded


@pytest.mark.parametrize("seed", range(25))
def test_linear_budget_selects_exactly_what_full_reserialization_selects(seed: int) -> None:
    rng = random.Random(seed)
    paths = [f"pkg/модуль_{index}_{'x' * rng.randrange(40)}.py" for index in range(30)]
    files: dict[str, dict[str, object]] = {
        path: {
            "path": path,
            "signatures": [f"def f{index}(value: int) -> str"] * rng.randrange(3),
            "parser_status": "ok",
        }
        for index, path in enumerate(paths)
    }
    edges: list[EdgeRecord] = sorted(
        (
            {"source": source, "target": target, "kind": "import", "confidence": "high"}
            for source, target in {
                (rng.choice(paths), rng.choice(paths)) for _ in range(40)
            }
            if source != target
        ),
        key=lambda edge: (edge["source"], edge["target"]),
    )
    diagnostics: list[Diagnostic] = [
        {"code": "syntax_error", "path": path} for path in sorted(rng.sample(paths, 5))
    ]
    ordered = rng.sample(paths, len(paths))
    max_tokens = rng.randrange(300, 3000)

    def payload() -> dict[str, object]:
        return {"commit": "0" * 40, "tier": "full", "estimated_tokens": 0}

    assert budget.select_within_budget(
        payload(), ordered, files, edges, diagnostics, max_tokens
    ) == _reference_selection(payload(), ordered, files, edges, diagnostics, max_tokens)


def test_read_blobs_reads_every_object_kind_in_two_git_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("print('a')\n", encoding="utf-8")
    (repo / "with space.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "big.py").write_text("y" * 64, encoding="utf-8")
    (repo / "bin.dat").write_bytes(b"\x00\x01")
    _git(repo, "add", ".")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendored")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    calls: list[list[str]] = []
    real_run = subprocess.run

    def _counting_run(command: list[str], *args: object, **kwargs: object) -> object:
        calls.append(command)
        return real_run(command, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", _counting_run)
    blobs = read_blobs(
        repo,
        commit,
        ["a.py", "with space.py", "big.py", "bin.dat", "vendored"],
        max_file_bytes=32,
        timeout_seconds=10,
    )

    assert blobs["a.py"] == Blob(11, b"print('a')\n")
    assert blobs["with space.py"] == Blob(6, b"x = 1\n")
    assert blobs["big.py"] == Blob(64, None)
    assert blobs["bin.dat"] == Blob(2, b"\x00\x01")
    assert blobs["vendored"] == Blob(None, None)
    assert len(calls) == 2


def test_worker_skips_only_the_files_of_a_grammar_missing_from_the_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "paths": {"main.py": "cHJpbnQoKQ==", "main.go": "cGFja2FnZSBt", "note.txt": "aGk="},
        "languages": {".py": "python", ".go": "go", ".txt": "unknown"},
    }
    loaded: list[str] = []

    def _load(grammar: str) -> object:
        loaded.append(grammar)
        return None if grammar == "go" else object()

    monkeypatch.setattr(worker, "_load_parser", _load)
    monkeypatch.setattr(worker, "_file_facts", lambda _parser, _content, language: {"grammar": language})
    monkeypatch.setattr(sys, "argv", ["tree_sitter_worker.py", "unused"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)

    worker.main()

    assert json.loads(output.getvalue()) == {"files": {"main.py": {"grammar": "python"}}}
    assert sorted(loaded) == ["go", "python"]


def test_worker_without_request_languages_uses_the_compatible_default() -> None:
    assert worker._request_languages({"paths": {}}) == worker._DEFAULT_LANGUAGES
    assert worker._request_languages({"languages": {".stub": "stub-lang"}}) == {".stub": "stub-lang"}


def test_supported_grammars_agree_between_worker_graph_and_protocol() -> None:
    assert set(worker._LANGUAGE_EXTRACTORS) == set(bundle_worker.SUPPORTED_GRAMMARS)
    assert set(worker._GRAMMAR_PACKAGES) == set(bundle_worker.SUPPORTED_GRAMMARS)
    assert set(GRAMMAR_FAMILIES) == set(bundle_worker.SUPPORTED_GRAMMARS)
    assert set(worker._DEFAULT_LANGUAGES.values()) <= set(bundle_worker.SUPPORTED_GRAMMARS)


@pytest.mark.skipif(shutil.which("uv") is None, reason="health reports a missing uv before the lock")
def test_health_names_a_registry_stub_bundle_instead_of_reporting_full_tier(tmp_path: Path) -> None:
    cli = _load_cli()
    project = tmp_path / "project"
    (project / ".harness" / "repo_map").mkdir(parents=True)
    (project / ".harness" / "repo_map" / "repo_map.py").write_text("", encoding="utf-8")
    python_tag, platform_tag = parser_bundle.python_platform_tags(sys.executable, 30)
    build_bundle_dir(
        parser_bundle.default_registry_dir(project), pair=f"{python_tag}-{platform_tag}"
    )

    lines = getattr(cli, "repo_map_health")(project)

    assert lines[0] == "Repo Map: tier=minimal (parser bundle has no supported grammars)"
    assert "тестовый stub" in lines[-1]


def _load_cli() -> object:
    loader = importlib.machinery.SourceFileLoader("harness_cli", str(ROOT / "harness" / "bin" / "harness"))
    spec = importlib.util.spec_from_loader("harness_cli", loader)
    assert spec is not None
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)
    return cli


def test_health_warns_when_the_developer_would_run_the_full_gate(tmp_path: Path) -> None:
    cli = _load_cli()
    config = tmp_path / ".harness" / "orchestration.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"verification_commands": ["python scripts/verify.py"]}))
    assert getattr(cli, "verification_routing_health")(tmp_path)[0].startswith("ПРЕДУПРЕЖДЕНИЕ")

    config.write_text(
        json.dumps(
            {
                "verification_commands": ["python scripts/verify.py"],
                "developer_verification_commands": ["python -m pytest tests/unit"],
            }
        )
    )
    assert getattr(cli, "verification_routing_health")(tmp_path) == []


def test_failed_worker_run_keeps_the_tail_of_its_stderr(tmp_path: Path) -> None:
    script, digest = write_worker_script(
        tmp_path,
        "import sys\nsys.stderr.write('x' * 10000 + 'grammar import exploded')\nsys.exit(3)\n",
        filename="noisy.py",
    )
    log = tmp_path / "logs" / bundle_worker.WORKER_ERROR_LOG_FILENAME

    result = bundle_worker.run_bundle_parser(
        sys.executable,
        script,
        tmp_path,
        {"paths": {}},
        timeout_seconds=30,
        max_output_bytes=1000,
        expected_script_sha256=digest,
        error_log=log,
    )

    assert result == "parser subprocess failed"
    text = log.read_text(encoding="utf-8")
    assert text.startswith("reason: parser subprocess failed\n")
    assert text.endswith("grammar import exploded")
    assert len(text.encode("utf-8")) <= bundle_worker.STDERR_TAIL_BYTES + len(
        "reason: parser subprocess failed\n"
    )
