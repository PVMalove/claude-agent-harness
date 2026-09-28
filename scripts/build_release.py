"""Build the versioned installation archive and release notes from tracked sources."""

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
TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


def release_notes(changelog: str, version: str) -> str:
    lines = changelog.splitlines()
    matches = [index for index, line in enumerate(lines) if (match := VERSION_HEADING.fullmatch(line)) and match.group(1) == version]
    if len(matches) != 1:
        raise ValueError(f"CHANGELOG.md must contain exactly one [{version}] section")
    start = matches[0] + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    body = "\n".join(lines[start:end]).strip()
    for heading in ("### Added", "### Fixed", "### Breaking Changes"):
        if heading not in body.splitlines():
            raise ValueError(f"CHANGELOG.md [{version}] is missing {heading}")
    if not body:
        raise ValueError(f"CHANGELOG.md [{version}] has no release notes")
    return body + "\n"


def tracked_installation_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True
    )
    tracked = [path.decode("utf-8") for path in result.stdout.split(b"\0") if path]
    included = [
        path for path in tracked
        if path.startswith(ARCHIVE_ROOTS)
        or path == "README.md"
        or path in {"LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "NOTICE"}
    ]
    return sorted(included)


def build_release(repo: Path, tag: str, output: Path) -> tuple[Path, Path, Path]:
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError(f"release tag must be a SemVer vMAJOR.MINOR.PATCH: {tag}")
    version = tag[1:]
    if (repo / "harness" / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError(f"{tag} does not match harness/VERSION")
    notes = release_notes((repo / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    files = tracked_installation_files(repo)
    if not files or "README.md" not in files or not any(path.startswith("harness/") for path in files):
        raise ValueError("tracked installation payload is incomplete")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"claude-agent-harness-{tag}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for path in files:
            bundle.add(repo / path, arcname=path, recursive=False)
    checksum = output / f"{archive.name}.sha256"
    checksum.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n", encoding="utf-8")
    notes_file = output / f"{tag}-release-notes.md"
    notes_file.write_text(notes, encoding="utf-8")
    return archive, checksum, notes_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    try:
        archive, checksum, notes = build_release(repo, arguments.tag, arguments.output)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(archive)
    print(checksum)
    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
