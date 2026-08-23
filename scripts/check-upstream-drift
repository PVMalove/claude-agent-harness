#!/usr/bin/env python3
"""Report whether the vendored mattpocock/skills snapshot (skills/vendor/mattpocock/, pinned by
third_party/mattpocock-skills/UPSTREAM.lock) has fallen behind the actual upstream repository.

Informational, not a resync: never writes to skills/vendor/ or UPSTREAM.lock. Exits 1 when
upstream has moved so a periodic CI run surfaces it as a failed run; exits 0 when in sync."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "[ERROR] check-upstream-drift requires Python %s+ (found %s).\n"
        % (".".join(map(str, MIN_PYTHON)), sys.version.split()[0])
    )
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = ROOT / "third_party" / "mattpocock-skills" / "UPSTREAM.lock"
PLUGIN_FILE = ROOT / "third_party" / "mattpocock-skills" / "plugin.json"
VENDOR_ROOT = ROOT / "skills" / "vendor" / "mattpocock"
CAPABILITIES_FILE = ROOT / "harness" / "CAPABILITIES.json"

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse_lock(path: Path) -> dict[str, str]:
    fields = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def latest_upstream_tag(repo_url: str) -> tuple[str, str] | None:
    """Query refs without cloning. Returns (tag_name, commit_sha) for the highest vX.Y.Z tag,
    or None if the remote has no such tag."""
    result = subprocess.run(
        ["git", "ls-remote", "--tags", repo_url],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        sys.exit(f"cannot query {repo_url}: {result.stderr.strip()}")

    best: tuple[tuple[int, int, int], str, str] | None = None
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        name = ref.removeprefix("refs/tags/").removesuffix("^{}")
        match = TAG_RE.match(name)
        if not match:
            continue
        version = tuple(int(part) for part in match.groups())
        # A peeled `^{}` line (annotated tag) carries the real commit and must win over the
        # tag-object sha seen first for the same name - later line always wins on a tie.
        if best is None or version >= best[0]:
            best = (version, name, sha)
    return (best[1], best[2]) if best else None


def diff_skill_names(new_snapshot: Path, skills: list[str]) -> list[str]:
    changed = []
    for entry in skills:
        relative = entry.removeprefix("./skills/")
        name = Path(relative).name
        new_dir = new_snapshot / "skills" / relative
        old_dir = VENDOR_ROOT / relative
        result = subprocess.run(
            ["git", "diff", "--no-index", "--quiet", str(old_dir), str(new_dir)],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            changed.append(name)
    return changed


def main() -> int:
    if not LOCK_FILE.is_file():
        sys.exit(f"missing {LOCK_FILE}")
    lock = parse_lock(LOCK_FILE)
    repo_url = lock["repository"]
    pinned_ref = lock["source_ref"].removeprefix("refs/tags/")
    pinned_revision = lock["revision"]

    latest = latest_upstream_tag(repo_url)
    if latest is None:
        sys.exit(f"no vX.Y.Z tags found on {repo_url}")
    latest_tag, latest_sha = latest

    if latest_tag == pinned_ref:
        print(f"up to date: {pinned_ref} ({pinned_revision}) is the latest upstream tag")
        return 0

    print(f"upstream has moved: pinned {pinned_ref} ({pinned_revision}) -> latest {latest_tag} ({latest_sha})")

    with tempfile.TemporaryDirectory(prefix="upstream-drift-") as tmp:
        clone_dir = Path(tmp) / "upstream"
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", latest_tag, repo_url, str(clone_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            sys.exit(f"cannot clone {repo_url}@{latest_tag}: {result.stderr.strip()}")

        plugin = json.loads(PLUGIN_FILE.read_text(encoding="utf-8"))
        changed = diff_skill_names(clone_dir, plugin["skills"])
        if not changed:
            print("no file content changed between the pinned and latest tag - only the tag/revision itself moved")
        else:
            capabilities = json.loads(CAPABILITIES_FILE.read_text(encoding="utf-8"))
            overridden = set(capabilities.get("pvmalove-suite", {}).get("overrides", {}).keys())
            safe = sorted(name for name in changed if name not in overridden)
            needs_review = sorted(name for name in changed if name in overridden)

            if safe:
                print(f"\nSafe to resync (not project-owned by any first-party override): {', '.join(safe)}")
            if needs_review:
                print(
                    f"\nNeeds manual review before resync (pvmalove-suite overrides these by name, "
                    f"docs/adr/0002 - compare against the new upstream version before deciding whether "
                    f"the first-party fork needs rebasing): {', '.join(needs_review)}"
                )

    print(
        f"\nTo pick this up: update third_party/mattpocock-skills/ (UPSTREAM.lock, plugin.json, "
        f"SHA256SUMS) and skills/vendor/mattpocock/ to {latest_tag}, per the process this repo "
        f"already used to pin {pinned_ref}."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
