"""Installing parsers for chosen languages: pinned wheels, hash checks and a partial bundle."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import TracebackType

import pytest

from harness.repo_map import bundle_fetch
from harness.repo_map.bundle_fetch import (
    ParserInstallError,
    WheelPin,
    download_wheel,
    install_languages,
    load_pins,
    manifest_pair,
    parser_status,
    wheels_for,
)
from harness.repo_map.bundle_pins import CORE_DISTRIBUTION, CORE_VERSION, GRAMMARS
from harness.repo_map.parser_bundle import AppliedBundle, python_platform_tags

ROOT = Path(__file__).resolve().parents[2]
GOOD = b"wheel bytes"


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return path


def _pair() -> str:
    python_tag, platform_tag = python_platform_tags(sys.executable, 60)
    return f"{python_tag}-{platform_tag}"


def _pins(pair: str) -> list[WheelPin]:
    """One fake wheel per pinned distribution for `pair`; their bytes are `GOOD`."""
    wanted = {(CORE_DISTRIBUTION, CORE_VERSION)} | {
        (g.distribution, g.version) for g in GRAMMARS
    }
    return [
        WheelPin(
            name,
            version,
            f"{name}-{version}-py3-none-any.whl",
            hashlib.sha256(GOOD).hexdigest(),
            f"https://files.pythonhosted.org/{name}.whl",
            (manifest_pair(pair),),
        )
        for name, version in sorted(wanted)
    ]


def _fetch(pin: WheelPin, destination: Path) -> None:
    destination.write_bytes(GOOD)


def _stub_acquire(**_: object) -> AppliedBundle | str:
    return "uv executable unavailable"


def test_the_packaged_manifest_matches_the_release_manifest_and_the_grammar_pins() -> (
    None
):
    release = ROOT / ".github/parser-bundle-release-wheels.json"
    assert bundle_fetch.WHEELS_MANIFEST.read_bytes() == release.read_bytes()
    pins = load_pins()
    for pair in json.loads(release.read_text())["matrix"]:
        for distribution, version in (
            (CORE_DISTRIBUTION, CORE_VERSION),
            *{(g.distribution, g.version) for g in GRAMMARS},
        ):
            assert [
                p for p in pins if p.distribution == distribution and pair in p.pairs
            ], (distribution, pair)
            assert all(
                p.version == version for p in pins if p.distribution == distribution
            )


def test_a_macos_pair_is_named_like_the_manifest() -> None:
    assert manifest_pair("cp314-macosx_11_0_arm64") == "cp314-macos_arm64"
    assert manifest_pair("cp312-linux_x86_64") == "cp312-linux_x86_64"


def test_the_wheels_for_a_language_are_the_core_and_one_wheel_per_distribution() -> (
    None
):
    pins = load_pins()
    typescript = [g for g in GRAMMARS if g.name in ("typescript", "tsx")]
    chosen = wheels_for(pins, "cp312-linux_x86_64", typescript)
    assert [p.distribution for p in chosen] == [
        CORE_DISTRIBUTION,
        "tree_sitter_typescript",
    ]


def test_an_unsupported_platform_is_named(tmp_path: Path) -> None:
    with pytest.raises(ParserInstallError, match="нет закреплённого wheel"):
        wheels_for(load_pins(), "cp399-plan9_x", list(GRAMMARS)[:1])


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        return None


def test_a_download_is_checked_against_the_pinned_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = WheelPin(
        "d",
        "1",
        "d-1-py3-none-any.whl",
        hashlib.sha256(GOOD).hexdigest(),
        "https://files.pythonhosted.org/d.whl",
        ("p",),
    )
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _Response(b"tampered")
    )
    target = tmp_path / "d.whl"
    with pytest.raises(ParserInstallError, match="SHA-256"):
        download_wheel(pin, target)
    assert not target.exists()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response(GOOD))
    download_wheel(pin, target)
    assert target.read_bytes() == GOOD


@pytest.mark.parametrize(
    "url", ["http://files.pythonhosted.org/x.whl", "https://example.com/x.whl"]
)
def test_only_https_pypi_addresses_are_downloaded(tmp_path: Path, url: str) -> None:
    pin = WheelPin("d", "1", "d.whl", "0" * 64, url, ("p",))
    with pytest.raises(ParserInstallError, match="не принадлежит PyPI"):
        download_wheel(pin, tmp_path / "d.whl")


def test_installing_a_language_builds_a_partial_bundle_and_keeps_earlier_ones(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "project")
    pair = _pair()
    lines: list[str] = []
    first = install_languages(
        repo,
        ["python"],
        pins=_pins(pair),
        fetch=_fetch,
        progress=lines.append,
        acquire=_stub_acquire,
    )
    assert first.languages == ("python",)
    assert first.ready is False and first.problem == "uv executable unavailable"
    assert len(lines) == 2  # core and the Python grammar only
    status = parser_status(repo)
    assert status.ready and status.languages == ("python",)
    assert status.grammars == ("python",)
    second = install_languages(
        repo, ["typescript"], pins=_pins(pair), fetch=_fetch, acquire=_stub_acquire
    )
    assert second.languages == ("python", "typescript")
    assert parser_status(repo).grammars == ("python", "typescript", "tsx")
    assert not list(first.registry.parent.glob(".stage-*"))
    assert not first.registry.with_name("registry.previous").exists()


def test_a_failed_download_leaves_the_installed_bundle_untouched(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "project")
    pair = _pair()
    install_languages(
        repo, ["go"], pins=_pins(pair), fetch=_fetch, acquire=_stub_acquire
    )
    before = (parser_status(repo).registry / "parser_bundle.lock.json").read_bytes()

    def broken(pin: WheelPin, destination: Path) -> None:
        raise ParserInstallError("нет сети", remedy="повторите")

    with pytest.raises(ParserInstallError, match="нет сети"):
        install_languages(
            repo, ["java"], pins=_pins(pair), fetch=broken, acquire=_stub_acquire
        )
    registry = parser_status(repo).registry
    assert (registry / "parser_bundle.lock.json").read_bytes() == before
    assert not list(registry.parent.glob(".stage-*"))


def test_an_unknown_language_is_rejected(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "project")
    with pytest.raises(ParserInstallError, match="Неизвестный"):
        install_languages(repo, ["cobol"], pins=_pins(_pair()), fetch=_fetch)
    with pytest.raises(ParserInstallError):
        install_languages(repo, [], pins=_pins(_pair()), fetch=_fetch)


def test_status_without_a_bundle_names_the_reason(tmp_path: Path) -> None:
    status = parser_status(_git_repo(tmp_path / "project"))
    assert not status.ready and status.languages == ()
    assert status.problem == "offline parser bundle unavailable"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "harness/bin/harness.py"), "parsers", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_cli_reports_status_as_json_and_rejects_an_unknown_language(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "project")
    status = _cli("status", str(repo), "--json")
    report = json.loads(status.stdout)
    assert status.returncode == 0
    assert report["ready"] is False
    assert report["problem"] == "offline parser bundle unavailable"
    assert report["languages"] == []
    plain = _cli("status", str(repo))
    assert "offline parser bundle unavailable" in plain.stdout
    refused = _cli("install", str(repo), "--language", "cobol")
    assert refused.returncode == 2
    assert "Неизвестный" in refused.stderr and "REMEDY" in refused.stderr
    assert _cli("install", str(repo)).returncode == 2  # --language is required
