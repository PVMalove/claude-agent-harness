"""Byte-identical output across cold cache runs for the new #279 languages (ADR 0024).

Same guarantee as the existing Python/TS/JS determinism test (tests/test_repo_map_tree_sitter.py),
checked once per new language rather than folded into each language's own signature/relations file.
"""

from pathlib import Path

import pytest
from conftest import build_map
from test_repo_map_tree_sitter_csharp import PACKAGE as CSHARP_PACKAGE
from test_repo_map_tree_sitter_go import PACKAGE as GO_PACKAGE
from test_repo_map_tree_sitter_java import PACKAGE as JAVA_PACKAGE

from harness.repo_map import repo_map


@pytest.mark.parametrize(
    "package,seed_path",
    [
        (GO_PACKAGE, "pkg/consumer.go"),
        (JAVA_PACKAGE, "Consumer.java"),
        (CSHARP_PACKAGE, "Consumer.cs"),
    ],
    ids=["go", "java", "csharp"],
)
def test_output_is_byte_identical_across_cold_runs(
    tmp_path: Path, bundle_dir: Path, package: dict[str, str], seed_path: str
) -> None:
    _, result = build_map(tmp_path, bundle_dir, package)
    repo = tmp_path / "project"
    policy = repo_map.load_policy(tmp_path / "orchestration.json", explicit=True)
    commit = result["commit"]
    assert isinstance(commit, str)
    first = repo_map.build_map(repo, commit, 8000, [seed_path], policy, cache_dir=tmp_path / "cold-a")
    second = repo_map.build_map(repo, commit, 8000, [seed_path], policy, cache_dir=tmp_path / "cold-b")
    assert first.encode("utf-8") == second.encode("utf-8")
    assert result["tier"] == "full"
