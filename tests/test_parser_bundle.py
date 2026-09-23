"""Unit-level behavior of the offline parser-bundle loader (harness/repo_map/parser_bundle.py).

Every test here proves the generic mechanism -- lock parsing, hash verification, an offline
`pip install --target`, and a bounded worker subprocess -- against synthetic fixtures assembled
locally (see tests/_parser_bundle_fixtures.py). No test in this file makes a network call; the
`pip install` calls below all pass `--no-index` and are skipped with a clear reason if pip is
unavailable in the running interpreter.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _parser_bundle_fixtures import (
    FAILING_WORKER_SCRIPT_SOURCE,
    MINIMAL_WORKER_SCRIPT_SOURCE,
    OUTPUT_BEFORE_STDIN_DRAIN_WORKER_SCRIPT_SOURCE,
    OVERSIZED_WORKER_SCRIPT_SOURCE,
    SLOW_WORKER_SCRIPT_SOURCE,
    build_bundle_dir,
    write_worker_script,
)

from harness.repo_map import parser_bundle

ROOT = Path(__file__).resolve().parents[1]
PARSER_BUNDLE_SOURCE = (ROOT / "harness" / "repo_map" / "parser_bundle.py").read_text(
    encoding="utf-8"
)


def _pip_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("pip") is not None


def _running_pair(timeout_seconds: int = 30) -> str:
    python_tag, platform_tag = parser_bundle.python_platform_tags(
        sys.executable, timeout_seconds
    )
    return f"{python_tag}-{platform_tag}"


def test_parser_bundle_module_never_imports_tree_sitter() -> None:
    tree = ast.parse(PARSER_BUNDLE_SOURCE)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(module.startswith("tree_sitter") for module in imported_modules)


def test_parse_lock_accepts_well_formed_json(tmp_path: Path) -> None:
    lock_path = build_bundle_dir(tmp_path / "bundle", pair="cp312-any") / "parser_bundle.lock.json"
    lock = parser_bundle.parse_lock(lock_path.read_bytes())
    assert lock.core_version == "0.1.0"
    assert lock.worker_script == "worker.py"
    assert len(lock.grammars) == 1
    assert lock.grammars[0].extensions == (".stub",)
    assert "cp312-any" in lock.wheelhouses


def test_parse_lock_rejects_malformed_json() -> None:
    with pytest.raises(parser_bundle.BundleFormatError):
        parser_bundle.parse_lock(b"not json")
    with pytest.raises(parser_bundle.BundleFormatError):
        parser_bundle.parse_lock(b"[]")
    with pytest.raises(parser_bundle.BundleFormatError):
        parser_bundle.parse_lock(json.dumps({"grammars": []}).encode())


def test_find_bundle_returns_first_directory_with_a_lock_file(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair="cp312-any")
    assert parser_bundle.find_bundle((empty_dir, bundle_dir)) == bundle_dir
    assert parser_bundle.find_bundle((empty_dir,)) is None


def test_python_platform_tags_returns_a_cpython_tag_for_the_running_interpreter() -> None:
    python_tag, platform_tag = parser_bundle.python_platform_tags(sys.executable, 30)
    assert re.fullmatch(r"cp3\d+", python_tag)
    assert platform_tag


def test_verify_wheelhouse_detects_missing_pair_and_hash_mismatch(tmp_path: Path) -> None:
    pair = _running_pair()
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair=pair)
    lock = parser_bundle.parse_lock((bundle_dir / "parser_bundle.lock.json").read_bytes())

    ok = parser_bundle.verify_wheelhouse(lock, bundle_dir / "wheelhouse" / pair, pair)
    assert ok == {"ok": True, "reason": None}

    missing = parser_bundle.verify_wheelhouse(lock, bundle_dir / "wheelhouse" / "cp1-nowhere", "cp1-nowhere")
    assert missing == {
        "ok": False,
        "reason": "parser wheelhouse missing for interpreter/platform pair",
    }

    corrupted_dir = tmp_path / "corrupted" / pair
    corrupted_dir.mkdir(parents=True)
    (corrupted_dir / "stubparser-1.0.0-py3-none-any.whl").write_bytes(b"not the real wheel")
    corrupted = parser_bundle.verify_wheelhouse(lock, corrupted_dir, pair)
    assert corrupted == {"ok": False, "reason": "parser bundle hash mismatch"}


@pytest.mark.skipif(not _pip_available(), reason="pip is not importable in this interpreter")
def test_install_bundle_installs_offline_and_is_idempotent(tmp_path: Path) -> None:
    pair = _running_pair()
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair=pair)
    lock = parser_bundle.parse_lock((bundle_dir / "parser_bundle.lock.json").read_bytes())
    install_dir = tmp_path / "install"

    parser_bundle.install_bundle(
        lock,
        bundle_dir / "wheelhouse" / pair,
        install_dir,
        sys.executable,
        pair=pair,
        timeout_seconds=120,
    )
    assert (install_dir / "stubparser" / "__init__.py").is_file()
    marker = install_dir / parser_bundle.INSTALL_MARKER_FILENAME
    assert marker.read_text(encoding="utf-8").strip() == lock.raw_sha256

    # Idempotent: a second call with the same lock hash must not fail or need to re-run pip.
    parser_bundle.install_bundle(
        lock,
        bundle_dir / "wheelhouse" / pair,
        install_dir,
        sys.executable,
        pair=pair,
        timeout_seconds=120,
    )
    assert marker.read_text(encoding="utf-8").strip() == lock.raw_sha256


@pytest.mark.skipif(not _pip_available(), reason="pip is not importable in this interpreter")
def test_run_bundle_parser_succeeds_and_uses_the_installed_package(tmp_path: Path) -> None:
    pair = _running_pair()
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair=pair)
    lock = parser_bundle.parse_lock((bundle_dir / "parser_bundle.lock.json").read_bytes())
    install_dir = tmp_path / "install"
    parser_bundle.install_bundle(
        lock,
        bundle_dir / "wheelhouse" / pair,
        install_dir,
        sys.executable,
        pair=pair,
        timeout_seconds=120,
    )
    result = parser_bundle.run_bundle_parser(
        sys.executable,
        bundle_dir / "worker.py",
        install_dir,
        {"paths": {"a.stub": "aGVsbG8="}},
        timeout_seconds=30,
        max_output_bytes=1_000_000,
        expected_script_sha256=lock.script_sha256,
    )
    assert not isinstance(result, str)
    assert result["files"]["a.stub"]["parser_status"] == "ok"


def test_run_bundle_parser_degrades_on_time_limit(tmp_path: Path) -> None:
    script_path, script_sha256 = write_worker_script(
        tmp_path, SLOW_WORKER_SCRIPT_SOURCE, filename="slow.py"
    )
    result = parser_bundle.run_bundle_parser(
        sys.executable,
        script_path,
        tmp_path,
        {"paths": {}},
        timeout_seconds=1,
        max_output_bytes=1_000_000,
        expected_script_sha256=script_sha256,
    )
    assert result == "parser subprocess exceeded time limit"


def test_run_bundle_parser_degrades_on_output_size_limit(tmp_path: Path) -> None:
    script_path, script_sha256 = write_worker_script(
        tmp_path, OVERSIZED_WORKER_SCRIPT_SOURCE, filename="oversized.py"
    )
    result = parser_bundle.run_bundle_parser(
        sys.executable,
        script_path,
        tmp_path,
        {"paths": {}},
        timeout_seconds=10,
        max_output_bytes=100,
        expected_script_sha256=script_sha256,
    )
    assert result == "parser subprocess exceeded output size limit"


def test_run_bundle_parser_degrades_on_subprocess_failure(tmp_path: Path) -> None:
    script_path, script_sha256 = write_worker_script(
        tmp_path, FAILING_WORKER_SCRIPT_SOURCE, filename="failing.py"
    )
    result = parser_bundle.run_bundle_parser(
        sys.executable,
        script_path,
        tmp_path,
        {"paths": {}},
        timeout_seconds=10,
        max_output_bytes=1_000_000,
        expected_script_sha256=script_sha256,
    )
    assert result == "parser subprocess failed"


def test_run_bundle_parser_degrades_on_script_hash_mismatch_toctou(tmp_path: Path) -> None:
    """A worker script swapped after `acquire_bundle` verified it must never be executed."""
    script_path, original_sha256 = write_worker_script(
        tmp_path, MINIMAL_WORKER_SCRIPT_SOURCE, filename="worker.py"
    )
    # Simulate a TOCTOU swap: the file on disk no longer matches the hash that was verified
    # earlier (e.g. by `acquire_bundle`).
    script_path.write_text(FAILING_WORKER_SCRIPT_SOURCE, encoding="utf-8", newline="\n")
    result = parser_bundle.run_bundle_parser(
        sys.executable,
        script_path,
        tmp_path,
        {"paths": {}},
        timeout_seconds=10,
        max_output_bytes=1_000_000,
        expected_script_sha256=original_sha256,
    )
    assert result == "parser bundle hash mismatch"


def test_run_bundle_parser_does_not_deadlock_on_large_request_with_slow_reader(
    tmp_path: Path,
) -> None:
    """A request bigger than the OS pipe buffer must not deadlock a worker that writes output

    before draining stdin: stdin must be written from its own thread, not synchronously before
    the reader starts.
    """
    script_path, script_sha256 = write_worker_script(
        tmp_path, OUTPUT_BEFORE_STDIN_DRAIN_WORKER_SCRIPT_SOURCE, filename="write_then_drain.py"
    )
    # Several MB, well past typical OS pipe buffers (64KB on Windows/Linux).
    request: dict[str, object] = {"paths": {"a.stub": "x" * (8 * 1024 * 1024)}}
    result = parser_bundle.run_bundle_parser(
        sys.executable,
        script_path,
        tmp_path,
        request,
        timeout_seconds=15,
        max_output_bytes=1_000_000,
        expected_script_sha256=script_sha256,
    )
    assert result != "parser subprocess exceeded time limit"
    assert not isinstance(result, str)
    assert result["files"] == {}


def test_acquire_bundle_returns_offline_unavailable_when_nothing_is_found(tmp_path: Path) -> None:
    result = parser_bundle.acquire_bundle(
        repo=tmp_path,
        registry_paths=(),
        python_executable=sys.executable,
        timeout_seconds=30,
    )
    assert result == "offline parser bundle unavailable"


def test_acquire_bundle_reports_hash_mismatch(tmp_path: Path) -> None:
    pair = _running_pair()
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair=pair)
    # Corrupt the wheel after the lock was written against its original bytes.
    wheel_path = bundle_dir / "wheelhouse" / pair / "stubparser-1.0.0-py3-none-any.whl"
    wheel_path.write_bytes(b"corrupted")
    result = parser_bundle.acquire_bundle(
        repo=tmp_path,
        registry_paths=(str(bundle_dir),),
        python_executable=sys.executable,
        timeout_seconds=30,
    )
    assert result == "parser bundle hash mismatch"


def test_acquire_bundle_reports_missing_wheelhouse_for_pair(tmp_path: Path) -> None:
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair="cp1-nonexistent-platform")
    result = parser_bundle.acquire_bundle(
        repo=tmp_path,
        registry_paths=(str(bundle_dir),),
        python_executable=sys.executable,
        timeout_seconds=30,
    )
    assert result == "parser wheelhouse missing for interpreter/platform pair"


def _lock_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "core_version": "0.1.0",
        "core_abi_range": ">=1,<2",
        "worker_script": "worker.py",
        "script_sha256": "a" * 64,
        "grammars": [],
        "wheelhouses": {
            "cp312-any": [{"filename": "stubparser-1.0.0-py3-none-any.whl", "sha256": "b" * 64}]
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "worker_script",
    [
        "../evil.py",
        "..\\evil.py",
        "/etc/evil.py",
        "C:\\evil.py",
        "sub/evil.py",
        "sub\\evil.py",
        ".",
        "..",
        "evil.py\n--index-url http://example.invalid",
    ],
)
def test_parse_lock_rejects_unsafe_worker_script(worker_script: str) -> None:
    with pytest.raises(parser_bundle.BundleFormatError):
        parser_bundle.parse_lock(
            json.dumps(_lock_payload(worker_script=worker_script)).encode()
        )


@pytest.mark.parametrize(
    "filename",
    [
        "../evil-1.0.0-py3-none-any.whl",
        "..\\evil-1.0.0-py3-none-any.whl",
        "/etc/evil-1.0.0-py3-none-any.whl",
        "sub/evil-1.0.0-py3-none-any.whl",
        "evil-1.0.0-py3-none-any.whl\n--index-url http://example.invalid",
        "not-a-wheel.txt",
        "evil-1.0.0-py3-none-any.whl ",
    ],
)
def test_parse_lock_rejects_unsafe_wheelhouse_filename(filename: str) -> None:
    with pytest.raises(parser_bundle.BundleFormatError):
        parser_bundle.parse_lock(
            json.dumps(
                _lock_payload(wheelhouses={"cp312-any": [{"filename": filename, "sha256": "b" * 64}]})
            ).encode()
        )


def test_install_bundle_uses_isolated_flag_and_strips_pip_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `PIP_*` variable or pip config file in the parent's environment may reach the pip

    subprocess: the call is stubbed out entirely, so this proves the args/env passed to
    `subprocess.run` without ever touching the network or installing anything.
    """
    pair = _running_pair()
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair=pair)
    lock = parser_bundle.parse_lock((bundle_dir / "parser_bundle.lock.json").read_bytes())
    install_dir = tmp_path / "install"

    monkeypatch.setenv("PIP_INDEX_URL", "http://example.invalid/simple")
    monkeypatch.setenv("PIP_FIND_LINKS", "http://example.invalid/links")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(tmp_path / "pip.conf"))
    monkeypatch.setenv("PARSER_BUNDLE_TEST_KEEP", "keep-me")

    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    parser_bundle.install_bundle(
        lock,
        bundle_dir / "wheelhouse" / pair,
        install_dir,
        sys.executable,
        pair=pair,
        timeout_seconds=30,
    )

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "--isolated" in cmd
    assert "--no-index" in cmd

    env = captured["env"]
    assert isinstance(env, dict)
    assert not any(key.startswith("PIP_") for key in env)
    assert env.get("PARSER_BUNDLE_TEST_KEEP") == "keep-me"
