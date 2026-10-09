"""Целостность вендорного snapshot: каждый файл snapshot проверяется своей строкой SHA256SUMS."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.verification import vendor_pin


def _snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Snapshot из двух файлов с SHA256SUMS; модуль смотрит в `tmp_path` вместо репозитория."""
    snapshot = tmp_path / "skills" / "vendor" / "mattpocock"
    manifest = tmp_path / "third_party" / "mattpocock-skills"
    snapshot.mkdir(parents=True)
    manifest.mkdir(parents=True)
    for name in ("a.md", "b.md"):
        (snapshot / name).write_text(f"{name}\n", encoding="utf-8")
    monkeypatch.setattr(vendor_pin, "ROOT", tmp_path)
    monkeypatch.setattr(vendor_pin, "VENDOR_SNAPSHOT", snapshot)
    monkeypatch.setattr(vendor_pin, "VENDOR_MANIFEST_DIR", manifest)
    return manifest / "SHA256SUMS"


def _line(tmp_path: Path, relative: str) -> str:
    digest = hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
    return f"{digest}  {relative}\n"


def test_matching_checksums_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sums = _snapshot(tmp_path, monkeypatch)
    sums.write_text(
        _line(tmp_path, "skills/vendor/mattpocock/a.md")
        + _line(tmp_path, "skills/vendor/mattpocock/b.md"),
        encoding="utf-8",
    )

    vendor_pin._check_checksums()


@pytest.mark.parametrize(
    "listed",
    [
        # A duplicated line keeps the count equal while b.md is never verified.
        ["skills/vendor/mattpocock/a.md", "skills/vendor/mattpocock/a.md"],
        # A line for a file outside the snapshot does the same.
        ["skills/vendor/mattpocock/a.md", "outside.md"],
    ],
)
def test_an_unlisted_snapshot_file_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listed: list[str]
) -> None:
    sums = _snapshot(tmp_path, monkeypatch)
    (tmp_path / "outside.md").write_text("outside\n", encoding="utf-8")
    sums.write_text(
        "".join(_line(tmp_path, relative) for relative in listed), encoding="utf-8"
    )

    with pytest.raises(SystemExit) as raised:
        vendor_pin._check_checksums()

    assert "SHA256SUMS" in str(raised.value.code)
