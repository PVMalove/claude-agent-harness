"""Public experiment boundary: only verified artifacts may be used offline."""

import hashlib
from pathlib import Path

import pytest

from scripts.memory_vector_probe import verify_artifacts


def test_artifact_substitution_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"changed")
    lock: dict[str, object] = {
        "artifacts": [
            {
                "path": "model.onnx",
                "size": 8,
                "sha256": hashlib.sha256(b"original").hexdigest(),
            }
        ]
    }
    with pytest.raises(ValueError, match="artifact mismatch"):
        verify_artifacts(lock, tmp_path)


def test_prepare_rejects_unsafe_path_before_writing(tmp_path: Path) -> None:
    from scripts.memory_vector_probe import prepare

    lock: dict[str, object] = {
        "artifacts": [
            {
                "path": "../outside",
                "sha256": "a" * 64,
                "size": 1,
                "url": "https://example.invalid/file",
            }
        ],
        "sqlite_vec": {"wheels": []},
    }
    with pytest.raises(ValueError, match="invalid artifact"):
        prepare(lock, tmp_path)
    assert not (tmp_path.parent / "outside.partial").exists()


@pytest.mark.parametrize(
    "path,digest", [("../model", "a" * 64), ("model", ""), ("C:\\model", "a" * 64)]
)
def test_invalid_lock_is_rejected(tmp_path: Path, path: str, digest: str) -> None:
    with pytest.raises(ValueError, match="invalid artifact"):
        verify_artifacts(
            {"artifacts": [{"path": path, "size": 1, "sha256": digest}]}, tmp_path
        )


def test_cosine_filter_precedes_fusion_and_ties_use_path() -> None:
    from scripts.memory_vector_probe import fuse_ranks

    assert fuse_ranks(["b", "a"], [("a", 0.9), ("b", 0.9), ("c", 0.4)], 0.8) == [
        "a",
        "b",
    ]


@pytest.mark.parametrize(
    "recall,noise,platforms,expected",
    [
        (0.95, 0.60, True, "no-go"),
        (0.90, 0.50, True, "no-go"),
        (1.00, 0.61, True, "no-go"),
        (1.00, 0.50, False, "deferred"),
        (1.00, 0.50, True, "go"),
    ],
)
def test_recommendation_requires_gain_and_platform_evidence(
    recall: float, noise: float, platforms: bool, expected: str
) -> None:
    from scripts.memory_vector_probe import recommend

    baseline = {
        "mean_recall": {"1": 0.95, "3": 0.95, "5": 0.95},
        "mean_noise_ratio": 0.60,
    }
    result = {
        "mean_recall": {"1": recall, "3": recall, "5": recall},
        "mean_noise_ratio": noise,
    }
    assert recommend(baseline, result, platforms) == expected
