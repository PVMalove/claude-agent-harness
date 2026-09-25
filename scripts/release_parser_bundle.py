#!/usr/bin/env python3
"""Assemble a complete, audited offline parser release from pinned, staged wheels.

The release job downloads the wheels named by the committed manifest. This script has no
network path: it verifies every staged byte, builds the nine-pair lock, generates a CycloneDX
1.6 SBOM, and requires a clean pip-audit JSON result before making the output visible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_parser_bundle import CORE_ABI_RANGE, CORE_VERSION, GRAMMARS, WORKER

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".github" / "parser-bundle-release-wheels.json"
PYTHON_TAGS = ("cp312", "cp313", "cp314")
PLATFORMS = ("win_amd64", "linux_x86_64", "macos_arm64")
MATRIX = tuple(f"{python_tag}-{platform}" for platform in PLATFORMS for python_tag in PYTHON_TAGS)
PACKAGES = {
    "tree_sitter": CORE_VERSION,
    "tree_sitter_python": "0.25.0",
    "tree_sitter_typescript": "0.23.2",
    "tree_sitter_javascript": "0.25.0",
}


class WheelPin(TypedDict):
    distribution: str
    version: str
    filename: str
    sha256: str
    url: str
    pairs: list[str]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(path: Path) -> list[WheelPin]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("matrix") != list(MATRIX):
        raise ValueError("release wheel manifest has an incomplete or changed matrix")
    wheels = payload.get("wheels")
    if not isinstance(wheels, list) or len(wheels) != 18:
        raise ValueError("release wheel manifest must pin exactly 18 unique wheels")
    seen: set[str] = set()
    coverage: dict[str, set[str]] = {pair: set() for pair in MATRIX}
    validated: list[WheelPin] = []
    for wheel in wheels:
        if not isinstance(wheel, dict) or set(wheel) != {
            "distribution", "version", "filename", "sha256", "url", "pairs"
        }:
            raise ValueError("invalid release wheel entry")
        distribution = wheel["distribution"]
        version = wheel["version"]
        filename = wheel["filename"]
        digest = wheel["sha256"]
        url = wheel["url"]
        pairs = wheel["pairs"]
        if not isinstance(distribution, str) or PACKAGES.get(distribution) != version:
            raise ValueError("release wheel distribution or version differs from parser pins")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".whl"):
            raise ValueError("unsafe release wheel filename")
        if not filename.startswith(f"{distribution}-{version}-") or filename in seen:
            raise ValueError("duplicate or mismatched release wheel filename")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid release wheel hash")
        if not isinstance(url, str) or not url.startswith("https://files.pythonhosted.org/") or not url.endswith("/" + filename):
            raise ValueError("release wheel URL must identify the pinned PyPI file")
        if not isinstance(pairs, list) or not pairs or not all(isinstance(pair, str) for pair in pairs) or len(set(pairs)) != len(pairs):
            raise ValueError("invalid release wheel pair list")
        for pair in pairs:
            if pair not in coverage or distribution in coverage[pair]:
                raise ValueError("release wheel matrix has a missing or duplicate distribution")
            python_tag, platform = pair.split("-", 1)
            if platform == "win_amd64" and not filename.endswith("win_amd64.whl"):
                raise ValueError("release wheel platform mismatch")
            if platform == "linux_x86_64" and ("manylinux" not in filename or not filename.endswith("x86_64.whl")):
                raise ValueError("release wheel platform mismatch")
            if platform == "macos_arm64" and not filename.endswith("macosx_11_0_arm64.whl"):
                raise ValueError("release wheel platform mismatch")
            if distribution == "tree_sitter" and f"-{python_tag}-{python_tag}-" not in filename:
                raise ValueError("release core wheel interpreter mismatch")
            if distribution != "tree_sitter" and "-abi3-" not in filename:
                raise ValueError("release grammar wheel must be abi3")
            coverage[pair].add(distribution)
        seen.add(filename)
        validated.append(cast(WheelPin, wheel))
    if any(names != set(PACKAGES) for names in coverage.values()):
        raise ValueError("release wheel matrix is incomplete")
    return validated


def _requirements(wheels: list[WheelPin]) -> str:
    lines = []
    for distribution, version in sorted(PACKAGES.items()):
        hashes = sorted({str(wheel["sha256"]) for wheel in wheels if wheel["distribution"] == distribution})
        lines.append(f"{distribution.replace('_', '-')}=={version} " + " ".join(
            f"--hash=sha256:{digest}" for digest in hashes
        ))
    return "\n".join(lines) + "\n"


def _run(command: list[str]) -> int:
    return subprocess.run(command, capture_output=True, check=False).returncode


def build_release(
    wheelhouse: Path,
    out: Path,
    *,
    manifest_path: Path = MANIFEST,
    cyclonedx: str = "cyclonedx-py",
    pip_audit: str = "pip-audit",
    runner: Callable[[list[str]], int] = _run,
) -> Path:
    wheels = _manifest(manifest_path)
    expected = {str(wheel["filename"]) for wheel in wheels}
    actual = {path.name for path in wheelhouse.iterdir() if path.is_file()}
    if actual != expected or any(path.is_dir() for path in wheelhouse.iterdir()):
        raise ValueError("staged wheelhouse differs from pinned release manifest")
    for wheel in wheels:
        if _sha256(wheelhouse / str(wheel["filename"])) != wheel["sha256"]:
            raise ValueError("staged release wheel hash mismatch")
    if out.exists():
        raise ValueError("release output already exists")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="parser-release-", dir=out.parent) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        by_pair = {
            pair: [wheel for wheel in wheels if pair in wheel["pairs"]] for pair in MATRIX
        }
        wheelhouses: dict[str, list[dict[str, str]]] = {}
        for pair in MATRIX:
            target = staging / "wheelhouse" / pair
            target.mkdir(parents=True)
            wheelhouses[pair] = []
            for wheel in sorted(by_pair[pair], key=lambda item: str(item["filename"])):
                filename = str(wheel["filename"])
                shutil.copyfile(wheelhouse / filename, target / filename)
                wheelhouses[pair].append({"filename": filename, "sha256": str(wheel["sha256"])})
        shutil.copyfile(WORKER, staging / WORKER.name)
        grammars = []
        for pin in GRAMMARS:
            hashes = {
                pair: str(next(wheel for wheel in by_pair[pair] if wheel["distribution"] == pin.distribution)["sha256"])
                for pair in MATRIX
            }
            grammars.append({
                "name": pin.name,
                "version": pin.version,
                "abi": pin.abi,
                "extensions": list(pin.extensions),
                "sha256": hashes[MATRIX[0]],
                "sha256_by_pair": hashes,
            })
        lock = {
            "core_version": CORE_VERSION,
            "core_abi_range": CORE_ABI_RANGE,
            "worker_script": WORKER.name,
            "script_sha256": _sha256(staging / WORKER.name),
            "grammars": grammars,
            "wheelhouses": wheelhouses,
        }
        (staging / "parser_bundle.lock.json").write_text(
            json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        requirements = staging / "parser_bundle.requirements.txt"
        requirements.write_text(_requirements(wheels), encoding="utf-8")
        sbom_path = staging / "parser_bundle.sbom.cdx.json"
        if runner([
            cyclonedx, "requirements", str(requirements), "--spec-version", "1.6",
            "--output-format", "JSON", "--output-reproducible", "--output-file", str(sbom_path),
        ]) != 0 or not sbom_path.is_file():
            raise ValueError("CycloneDX generation failed")
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.6":
            raise ValueError("CycloneDX generator returned an unexpected format")
        components = sbom.get("components")
        if not isinstance(components, list):
            raise ValueError("CycloneDX generator omitted components")
        components.extend({
            "type": "file",
            "name": str(wheel["filename"]),
            "bom-ref": "wheel:" + str(wheel["filename"]),
            "hashes": [{"alg": "SHA-256", "content": wheel["sha256"]}],
            "properties": [{"name": "harness:parser-bundle:pairs", "value": ",".join(wheel["pairs"])}],
        } for wheel in wheels)
        sbom_path.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        audit_cache = staging / ".audit-cache"
        audit_cache.mkdir()
        audit_path = staging / "parser_bundle.audit.json"
        if runner([
            pip_audit, "-r", str(requirements), "--require-hashes", "--disable-pip",
            "--cache-dir", str(audit_cache), "--format", "json", "--output", str(audit_path),
        ]) != 0 or not audit_path.is_file():
            raise ValueError("pip-audit failed or could not reach the vulnerability service")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        dependencies = audit.get("dependencies")
        expected_dependencies = {
            name.replace("_", "-"): version for name, version in PACKAGES.items()
        }
        audited_dependencies: dict[str, str] = {}
        if not isinstance(dependencies, list):
            raise ValueError("pip-audit result is incomplete or reports vulnerabilities")
        for item in dependencies:
            if not isinstance(item, dict):
                raise ValueError("pip-audit result is incomplete or reports vulnerabilities")
            name = item.get("name")
            version = item.get("version")
            vulns = item.get("vulns")
            if (
                not isinstance(name, str)
                or not isinstance(version, str)
                or not isinstance(vulns, list)
                or vulns
                or name in audited_dependencies
            ):
                raise ValueError("pip-audit result is incomplete or reports vulnerabilities")
            audited_dependencies[name] = version
        if audited_dependencies != expected_dependencies:
            raise ValueError("pip-audit result is incomplete or reports vulnerabilities")
        shutil.rmtree(audit_cache)
        staging.rename(out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    build_release(args.wheelhouse, args.out)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
