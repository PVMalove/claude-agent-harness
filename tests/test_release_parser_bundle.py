"""Release assembly is offline, complete for the support matrix, and fails closed."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast

import pytest

from harness.repo_map import parser_bundle
from scripts import release_parser_bundle as release
from scripts.build_parser_bundle import CORE_VERSION


class TestManifest(TypedDict):
    schema_version: int
    matrix: list[str]
    wheels: list[release.WheelPin]


def _staged(tmp_path: Path) -> tuple[Path, Path, TestManifest]:
    payload = cast(TestManifest, json.loads(release.MANIFEST.read_text(encoding="utf-8")))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for wheel in payload["wheels"]:
        # Keep fixture paths below the Windows MAX_PATH limit; real wheel names stay pinned
        # in the committed manifest and are checked independently by the assembler.
        pair = wheel["pairs"][0]
        python_tag, platform = pair.split("-", 1)
        platform_tag = "manylinux_x86_64" if platform == "linux_x86_64" else (
            "macosx_11_0_arm64" if platform == "macos_arm64" else "win_amd64"
        )
        abi = python_tag if wheel["distribution"] == "tree_sitter" else "abi3"
        wheel_tag = python_tag if wheel["distribution"] == "tree_sitter" else (
            "cp39" if wheel["distribution"] in {"tree_sitter_typescript", "tree_sitter_java"} else "cp310"
        )
        wheel["filename"] = f"{wheel['distribution']}-{wheel['version']}-{wheel_tag}-{abi}-{platform_tag}.whl"
        wheel["url"] = "https://files.pythonhosted.org/fixture/" + wheel["filename"]
        content = wheel["filename"].encode()
        (wheels / wheel["filename"]).write_bytes(content)
        wheel["sha256"] = hashlib.sha256(content).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return wheels, manifest, payload


def _fake_tools(
    commands: list[list[str]], *, vulnerable: bool = False, audit_error: bool = False,
    dependencies: list[dict[str, object]] | None = None,
) -> Callable[[list[str]], int]:
    def run(command: list[str]) -> int:
        commands.append(command)
        if command[1] == "requirements":
            Path(command[command.index("--output-file") + 1]).write_text(json.dumps({
                "bomFormat": "CycloneDX", "specVersion": "1.6", "components": [],
            }), encoding="utf-8")
            return 0
        if audit_error:
            return 1
        audited: list[dict[str, object]] = dependencies if dependencies is not None else [
            {"name": name.replace("_", "-"), "version": version, "vulns": []}
            for name, version in release.PACKAGES.items()
        ]
        if vulnerable:
            audited[0] = {**audited[0], "vulns": [{"id": "TEST-CVE"}]}
        Path(command[command.index("--output") + 1]).write_text(
            json.dumps({"dependencies": audited}), encoding="utf-8"
        )
        return 0
    return run


def test_release_bundle_contains_complete_lock_sbom_and_clean_audit(tmp_path: Path) -> None:
    wheels, manifest, payload = _staged(tmp_path)
    commands: list[list[str]] = []
    out = release.build_release(
        wheels, tmp_path / "release", manifest_path=manifest, runner=_fake_tools(commands)
    )
    lock = parser_bundle.parse_lock((out / "parser_bundle.lock.json").read_bytes())
    assert set(lock.wheelhouses) == set(release.MATRIX)
    assert all(len(artifacts) == len(release.PACKAGES) for artifacts in lock.wheelhouses.values())
    assert {grammar.name for grammar in lock.grammars} == {
        "python", "typescript", "tsx", "javascript", "go", "java", "csharp"
    }
    typescript = next(grammar for grammar in lock.grammars if grammar.name == "typescript")
    assert typescript.sha256_by_pair is not None
    assert typescript.sha256_by_pair["cp312-win_amd64"] != typescript.sha256_by_pair["cp312-linux_x86_64"]
    provenance = parser_bundle.build_provenance(
        lock, bundle_mode="applied", bundle_source="local-cache",
        python_tag="cp312", platform_tag="linux_x86_64"
    )
    grammar = next(item for item in cast(list[dict[str, object]], provenance["grammars"]) if item["name"] == "typescript")
    assert grammar["sha256"] == typescript.sha256_by_pair["cp312-linux_x86_64"]
    sbom = json.loads((out / "parser_bundle.sbom.cdx.json").read_text(encoding="utf-8"))
    files = [item for item in sbom["components"] if item["type"] == "file"]
    assert len(files) == release.EXPECTED_WHEEL_COUNT
    assert {item["hashes"][0]["content"] for item in files} == {
        wheel["sha256"] for wheel in payload["wheels"]
    }
    assert (out / "parser_bundle.audit.json").is_file()
    assert not (out / ".audit-cache").exists()
    assert "--require-hashes" in commands[1]
    assert "--disable-pip" in commands[1]
    assert "--cache-dir" in commands[1]
    assert commands[0][commands[0].index("--spec-version") + 1] == "1.6"


def test_release_rejects_missing_or_modified_wheel(tmp_path: Path) -> None:
    wheels, manifest, payload = _staged(tmp_path)
    first = wheels / payload["wheels"][0]["filename"]
    first.unlink()
    with pytest.raises(ValueError, match="differs"):
        release.build_release(wheels, tmp_path / "missing", manifest_path=manifest)
    first.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        release.build_release(wheels, tmp_path / "modified", manifest_path=manifest)


@pytest.mark.parametrize("vulnerable,audit_error", [(True, False), (False, True)])
def test_release_audit_failure_never_exposes_output(
    tmp_path: Path, vulnerable: bool, audit_error: bool
) -> None:
    wheels, manifest, _ = _staged(tmp_path)
    out = tmp_path / "release"
    with pytest.raises(ValueError, match="pip-audit"):
        release.build_release(
            wheels, out, manifest_path=manifest,
            runner=_fake_tools([], vulnerable=vulnerable, audit_error=audit_error),
        )
    assert not out.exists()


@pytest.mark.parametrize("dependencies", [
    [],
    [{"name": "tree-sitter", "version": CORE_VERSION, "vulns": []}],
    [
        {"name": name.replace("_", "-"), "version": version, "vulns": []}
        for name, version in release.PACKAGES.items() if name != "tree_sitter_python"
    ],
    [
        {"name": name.replace("_", "-"), "version": "0.0.0" if name == "tree_sitter_python" else version, "vulns": []}
        for name, version in release.PACKAGES.items()
    ],
    [
        {"name": name.replace("_", "-"), "version": version, "vulns": []}
        for name, version in release.PACKAGES.items()
    ] + [{"name": "tree-sitter", "version": CORE_VERSION, "vulns": []}],
    [
        {"name": name.replace("_", "-"), "version": version, "vulns": []}
        for name, version in release.PACKAGES.items() if name != "tree_sitter_python"
    ] + [{"name": "tree-sitter-python", "version": "0.25.0"}],
])
def test_release_rejects_incomplete_audit(
    tmp_path: Path, dependencies: list[dict[str, object]]
) -> None:
    wheels, manifest, _ = _staged(tmp_path)
    out = tmp_path / "release"
    with pytest.raises(ValueError, match="pip-audit"):
        release.build_release(
            wheels, out, manifest_path=manifest,
            runner=_fake_tools([], dependencies=dependencies),
        )
    assert not out.exists()
