"""Isolated #431 experiment; never changes the production memory engine."""

from __future__ import annotations

import hashlib
import math
import json
import argparse
import urllib.request
from pathlib import Path
from typing import cast


def artifact_target(
    artifact: dict[str, object], directory: Path
) -> tuple[Path, int, str]:
    """Validate destination and pinned metadata before any read or write."""
    relative = artifact.get("path")
    digest = artifact.get("sha256")
    size = artifact.get("size")
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in relative.split("/")
        or "\\" in relative
        or ":" in relative
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
        or type(size) is not int
        or size <= 0
    ):
        raise ValueError("invalid artifact lock")
    path = directory / relative
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("artifact escaped directory")
    return path, size, digest


def verify_artifacts(lock: dict[str, object], directory: Path) -> None:
    """Reject substituted local artifacts before native loading or inference."""
    artifacts = lock.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("lock requires artifacts")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("invalid artifact")
        path, size, digest = artifact_target(artifact, directory)
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != size or actual != digest:
            raise ValueError(f"artifact mismatch: {path.name}")


def fuse_ranks(
    fts: list[str], vectors: list[tuple[str, float]], threshold: float
) -> list[str]:
    """Equal-weight RRF; filter vector cosine before merging ranks."""
    ranked = sorted(
        ((p, c) for p, c in vectors if c >= threshold),
        key=lambda item: (-item[1], item[0]),
    )
    scores: dict[str, float] = {}
    for paths in (fts, [p for p, _ in ranked]):
        for rank, path in enumerate(paths, 1):
            scores[path] = scores.get(path, 0.0) + 1 / (60 + rank)
    return sorted(scores, key=lambda p: (-scores[p], p))


def recommend(
    baseline: dict[str, object], candidate: dict[str, object], platforms_passed: bool
) -> str:
    """Recommendation only; missing platform evidence never authorizes slice 2."""
    if not platforms_passed:
        return "deferred"
    old = cast(dict[str, float], baseline["mean_recall"])
    new = cast(dict[str, float], candidate["mean_recall"])
    pairs = [(new[str(k)], old[str(k)]) for k in (1, 3, 5)]
    pairs.append(
        (
            -cast(float, candidate["mean_noise_ratio"]),
            -cast(float, baseline["mean_noise_ratio"]),
        )
    )
    if any(not math.isfinite(v) for pair in pairs for v in pair):
        raise ValueError("non-finite metrics")
    return (
        "go"
        if all(a >= b for a, b in pairs) and any(a > b for a, b in pairs)
        else "no-go"
    )


def prepare(lock: dict[str, object], directory: Path) -> None:
    """Explicit network stage; pinned files are verified again by the offline worker."""
    records = cast(list[dict[str, object]], lock["artifacts"])
    extension = cast(dict[str, object], lock["sqlite_vec"])
    wheels = cast(list[dict[str, object]], extension["wheels"])
    records = records + [{**row, "path": row["filename"]} for row in wheels]
    for row in records:
        target, size, expected = artifact_target(row, directory)
        if not cast(str, row["url"]).startswith("https://"):
            raise ValueError("artifact URL must use HTTPS")
        if target.is_file():
            try:
                verify_artifacts({"artifacts": [row]}, directory)
                continue
            except ValueError:
                pass
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".partial")
        with (
            urllib.request.urlopen(cast(str, row["url"]), timeout=60) as source,
            partial.open("wb") as out,
        ):
            downloaded = 0
            while chunk := source.read(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > size:
                    raise ValueError("download exceeds pinned size")
                out.write(chunk)
        with partial.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != row["sha256"] or partial.stat().st_size != row["size"]:
            partial.unlink()
            raise ValueError(f"download mismatch: {row['path']}")
        partial.replace(target)
    verify_artifacts(lock, directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock", type=Path, default=Path("tests/memory/vector_probe.lock.json")
    )
    parser.add_argument("--artifacts", type=Path, required=True)
    arguments = parser.parse_args()
    prepare(json.loads(arguments.lock.read_text(encoding="utf-8")), arguments.artifacts)
