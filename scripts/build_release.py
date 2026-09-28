"""Сборка версионированного установочного архива и заметок о релизе из отслеживаемых файлов git."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import tarfile
from pathlib import Path


ARCHIVE_ROOTS = (
    "harness/",
    "skills/",
    "global/",
    "global-skills/",
    "bin/",
    "third_party/",
)
VERSION_HEADING = re.compile(r"^## \[([^]]+)\](?: - [0-9]{4}-[0-9]{2}-[0-9]{2})?\s*$")
CHANGELOG_CATEGORIES = frozenset(
    {
        "Added",
        "Changed",
        "Deprecated",
        "Removed",
        "Fixed",
        "Security",
        "Breaking Changes",
    }
)
TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


def release_notes(changelog: str, version: str) -> str:
    """Извлечь и проверить заметки о релизе из CHANGELOG.md для указанной версии."""
    lines = changelog.splitlines()
    matches = [
        index
        for index, line in enumerate(lines)
        if (match := VERSION_HEADING.fullmatch(line)) and match.group(1) == version
    ]
    if len(matches) != 1:
        raise ValueError(f"CHANGELOG.md must contain exactly one [{version}] section")
    start = matches[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    body = "\n".join(lines[start:end]).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md [{version}] has no release notes")
    # Keep a Changelog: a release lists only the categories it has, each with at least one entry.
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body.splitlines():
        if line.startswith("### "):
            category = line[4:].strip()
            if category not in CHANGELOG_CATEGORIES:
                raise ValueError(
                    f"CHANGELOG.md [{version}] has an unknown category: {category}"
                )
            current = sections.setdefault(category, [])
        elif current is not None and line.startswith("- "):
            current.append(line)
    if not sections:
        raise ValueError(f"CHANGELOG.md [{version}] has no ### category")
    for category, entries in sections.items():
        if not entries:
            raise ValueError(
                f"CHANGELOG.md [{version}] has an empty {category} category"
            )
    return body + "\n"


def tracked_installation_files(repo: Path) -> list[str]:
    """Получить список отслеживаемых git файлов, входящих в установочный архив релиза."""
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True
    )
    tracked = [path.decode("utf-8") for path in result.stdout.split(b"\0") if path]
    included = [
        path
        for path in tracked
        if path.startswith(ARCHIVE_ROOTS)
        or path == "README.md"
        or path in {"LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "NOTICE"}
    ]
    return sorted(included)


def check_release(repo: Path, tag: str) -> tuple[str, list[str]]:
    """Проверить релиз без сборки: тег, harness/VERSION, раздел CHANGELOG и отслеживаемые файлы."""
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError(f"release tag must be a SemVer vMAJOR.MINOR.PATCH: {tag}")
    version = tag[1:]
    if (repo / "harness" / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError(f"{tag} does not match harness/VERSION")
    notes = release_notes((repo / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    files = tracked_installation_files(repo)
    if (
        not files
        or "README.md" not in files
        or not any(path.startswith("harness/") for path in files)
    ):
        raise ValueError("tracked installation payload is incomplete")
    return notes, files


def build_release(repo: Path, tag: str, output: Path) -> tuple[Path, Path, Path]:
    """Собрать установочный архив релиза, вычислить его sha256 и записать release notes."""
    notes, files = check_release(repo, tag)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"claude-agent-harness-{tag}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for path in files:
            bundle.add(repo / path, arcname=path, recursive=False)
    checksum = output / f"{archive.name}.sha256"
    checksum.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
        encoding="utf-8",
    )
    notes_file = output / f"{tag}-release-notes.md"
    notes_file.write_text(notes, encoding="utf-8")
    return archive, checksum, notes_file


def main() -> int:
    """Точка входа CLI: сборка или валидация релиза по переданным параметрам."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="only validate the tag, harness/VERSION, CHANGELOG and payload; build nothing",
    )
    arguments = parser.parse_args()
    if not arguments.check and arguments.output is None:
        parser.error("--output is required unless --check is given")
    repo = Path(__file__).resolve().parent.parent
    try:
        if arguments.check:
            check_release(repo, arguments.tag)
            print(f"{arguments.tag}: release checks passed")
            return 0
        archive, checksum, notes = build_release(repo, arguments.tag, arguments.output)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(archive)
    print(checksum)
    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
