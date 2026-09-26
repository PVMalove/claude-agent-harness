"""Целостность закреплённого snapshot вендорных скиллов mattpocock."""

from __future__ import annotations

import hashlib
import json
import sys

from scripts.verification.paths import CAPABILITIES, ROOT

VENDOR_MANIFEST_DIR = ROOT / "third_party" / "mattpocock-skills"
VENDOR_SNAPSHOT = ROOT / "skills" / "vendor" / "mattpocock"


def _manifest_skills() -> list[str]:
    """Скиллы закреплённого plugin.json и проверка совпадения с mattpocock-suite в CAPABILITIES.json."""
    plugin = json.loads((VENDOR_MANIFEST_DIR / "plugin.json").read_text(encoding="utf-8"))
    capabilities = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
    expected = [entry.removeprefix("./skills/") for entry in plugin["skills"]]
    actual = [
        entry.removeprefix("vendor/mattpocock/") for entry in capabilities["mattpocock-suite"]["skills"]
    ]
    if expected != actual:
        sys.exit("mattpocock-suite does not match the pinned plugin manifest")
    return expected


def _check_checksums() -> None:
    """Сверить каждый файл snapshot с SHA256SUMS и число файлов с числом строк."""
    checksum_lines = (VENDOR_MANIFEST_DIR / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    vendor_files = sorted(path for path in VENDOR_SNAPSHOT.rglob("*") if path.is_file())
    if len(checksum_lines) != len(vendor_files):
        sys.exit(
            f"vendor file count mismatch: checksums={len(checksum_lines)} snapshot={len(vendor_files)}"
        )
    for line in checksum_lines:
        expected_hash, relative = line.split("  ", 1)
        actual_hash = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            sys.exit(f"vendor checksum mismatch: {relative}")


def check_vendor_pin() -> None:
    """Проверить манифест, число скиллов и SHA-256 каждого файла вендорного snapshot."""
    expected = _manifest_skills()
    skills = sorted(VENDOR_SNAPSHOT.rglob("SKILL.md"))
    if len(skills) != len(expected):
        sys.exit(f"vendor count mismatch: manifest={len(expected)} snapshot={len(skills)}")
    _check_checksums()
