"""Fail-closed edge cases of the Repo Map package: unreadable inputs, absent tools, invariants."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from tests.repo_map._parser_bundle_fixtures import (
    MINIMAL_WORKER_SCRIPT_SOURCE,
    build_bundle_dir,
    write_worker_script,
)

from harness.errors import INTERNAL_INVARIANT_REMEDY, HarnessError, PolicyError
from harness.repo_map import bundle_worker, contract, parser_bundle
from harness.repo_map.bundle_lock import BundleFormatError, parse_lock
from harness.repo_map.cache import write_cache
from harness.repo_map.git_source import RepoMapGitError, run_git
from harness.repo_map.policy import load_policy

PAIR = "cp312-test_platform"


def test_a_lock_with_invalid_utf8_is_a_format_error_not_a_crash() -> None:
    with pytest.raises(BundleFormatError, match="invalid text encoding"):
        parse_lock(b'{"core_version": "\xff"}')


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script interpreter")
def test_undecodable_interpreter_tags_degrade_instead_of_raising(
    tmp_path: Path,
) -> None:
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair=PAIR)
    interpreter = tmp_path / "python"
    interpreter.write_text("#!/bin/sh\nprintf '\\377\\376'\n", encoding="utf-8")
    interpreter.chmod(0o755)

    located = parser_bundle.locate_bundle(
        repo=tmp_path,
        registry_paths=(str(bundle_dir),),
        python_executable=str(interpreter),
        timeout_seconds=30,
    )

    assert located == "parser subprocess failed"


def test_an_unreadable_wheel_or_worker_is_a_hash_mismatch(tmp_path: Path) -> None:
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair=PAIR)
    lock = parse_lock((bundle_dir / "parser_bundle.lock.json").read_bytes())

    with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
        wheelhouse = parser_bundle.verify_wheelhouse(
            lock, bundle_dir / "wheelhouse" / PAIR, PAIR
        )
        worker = parser_bundle.verify_worker_script(lock, bundle_dir / "worker.py")

    assert wheelhouse == {"ok": False, "reason": "parser bundle hash mismatch"}
    assert worker is False


def test_applied_provenance_without_a_lock_is_an_internal_invariant() -> None:
    with pytest.raises(HarnessError) as raised:
        parser_bundle.build_provenance(
            None,
            bundle_mode="applied",
            bundle_source="local-cache",
            python_tag="cp312",
            platform_tag="test",
        )
    assert raised.value.remedy == INTERNAL_INVARIANT_REMEDY


def test_a_worker_without_pipes_is_killed_and_reported_as_an_invariant(
    tmp_path: Path,
) -> None:
    script, digest = write_worker_script(tmp_path, MINIMAL_WORKER_SCRIPT_SOURCE)
    process = mock.Mock(stdin=None, stdout=None, stderr=None)

    with mock.patch(
        "harness.repo_map.bundle_worker.subprocess.Popen", return_value=process
    ):
        with pytest.raises(HarnessError) as raised:
            bundle_worker.run_bundle_parser(
                sys.executable,
                script,
                tmp_path,
                {"paths": {}},
                timeout_seconds=5,
                max_output_bytes=1_000,
                expected_script_sha256=digest,
            )

    assert raised.value.remedy == INTERNAL_INVARIANT_REMEDY
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with()


def test_an_unlaunchable_git_is_a_repo_map_git_error(tmp_path: Path) -> None:
    with mock.patch(
        "harness.repo_map.git_source.subprocess.run",
        side_effect=FileNotFoundError("git"),
    ):
        with pytest.raises(RepoMapGitError) as raised:
            run_git(tmp_path, 10, "rev-parse", "HEAD")
    assert "could not run git rev-parse" in raised.value.message
    assert "install Git" in raised.value.remedy


def test_a_failed_cache_publish_leaves_no_temporary_file(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"

    with mock.patch.object(Path, "replace", side_effect=OSError("read-only")):
        write_cache(cache_dir, "a" * 64, '{"commit": "x"}\n')

    assert list(cache_dir.iterdir()) == []


def test_a_non_object_shipped_schema_is_an_internal_invariant() -> None:
    contract._schema.cache_clear()
    try:
        with mock.patch("harness.repo_map.contract.json.loads", return_value=[]):
            with pytest.raises(HarnessError) as raised:
                contract.validation_error({})
    finally:
        contract._schema.cache_clear()
    assert raised.value.remedy == INTERNAL_INVARIANT_REMEDY


def test_a_policy_file_with_invalid_utf8_is_a_policy_error(tmp_path: Path) -> None:
    policy = tmp_path / "orchestration.json"
    policy.write_bytes(b'{"repo_map_policy": {"tier": "\xff"}}')

    with pytest.raises(PolicyError) as raised:
        load_policy(policy, explicit=True)

    assert "not valid UTF-8" in raised.value.message
    assert "save the policy file as UTF-8" in raised.value.remedy


def test_run_git_still_reports_a_timeout(tmp_path: Path) -> None:
    with mock.patch(
        "harness.repo_map.git_source.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["git"], 1),
    ):
        with pytest.raises(RepoMapGitError, match="timed out after 1 seconds"):
            run_git(tmp_path, 1, "ls-tree")
