"""ChangeImpact: ImpactSet по diff `base..candidate` и графу импортов Repo Map (ADR 0023, раздел 1)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from harness.repo_map.backend import ParseOutcome
from harness.repo_map.bundle_worker import FileFacts, ImportFact
from harness.repo_map.change_impact import (
    ChangedPath,
    ImpactSet,
    compute_change_impact,
)
from harness.repo_map.git_source import read_blobs

_IMPORT = re.compile(r"^import ([\w.]+)$", re.MULTILINE)
_FROM = re.compile(r"^from ([\w.]+) import ([\w, ]+)$", re.MULTILINE)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class RegexBackend:
    """A `ParserBackend` that reads `import x` and `from x import y` lines of the commit."""

    def __init__(self, repo: Path) -> None:
        self._repo = repo

    def identity(self) -> str:
        return "regex"

    def parse(self, commit: str, paths: list[str]) -> ParseOutcome:
        blobs = read_blobs(
            self._repo, commit, paths, max_file_bytes=1_000_000, timeout_seconds=30
        )
        records: dict[str, dict[str, object]] = {
            path: {"path": path, "signatures": [], "parser_status": "ok"}
            for path in paths
        }
        facts: dict[str, FileFacts] = {}
        for path in paths:
            content = blobs[path].content
            if not path.endswith(".py") or content is None:
                continue
            text = content.decode("utf-8")
            imports = [
                ImportFact(module=module, level=0, names=[])
                for module in _IMPORT.findall(text)
            ] + [
                ImportFact(
                    module=module,
                    level=0,
                    names=[name.strip() for name in names.split(",")],
                )
                for module, names in _FROM.findall(text)
            ]
            facts[path] = FileFacts(
                parser_status="ok",
                signatures=[],
                imports=imports,
                definitions=[],
                references=[],
            )
        return ParseOutcome(
            True,
            "regex",
            {},
            records,
            facts,
            [],
            {".py": "python"},
        )


class UnavailableBackend:
    def identity(self) -> str:
        return "unavailable"

    def parse(self, commit: str, paths: list[str]) -> ParseOutcome:
        return ParseOutcome.degraded("parser bundle is not installed", {})


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


BASE_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "VALUE = 1\n",
    "pkg/lib.py": "from pkg import core\n",
    "pkg/app.py": "import pkg.lib\n",
    "pkg/standalone.py": "OTHER = 2\n",
    "tests/__init__.py": "",
    "tests/test_app.py": "import pkg.app\n",
    "tests/test_standalone.py": "import pkg.standalone\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _init(root)
    _write(root, BASE_FILES)
    _commit(root, "base")
    return root


def _impact(repo: Path, base: str, **kwargs: object) -> ImpactSet:
    candidate = _git(repo, "rev-parse", "HEAD")
    return compute_change_impact(
        repo,
        base,
        candidate,
        backend=RegexBackend(repo),
        **kwargs,  # type: ignore[arg-type]
    )


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _codes(impact: ImpactSet) -> set[str]:
    return {reason.code for reason in impact.unknown}


def test_a_modified_module_impacts_its_reverse_import_closure_and_tests(
    repo: Path,
) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n"})
    _commit(repo, "change core")

    impact = _impact(repo, base)

    assert impact.changed == (ChangedPath("modified", "pkg/core.py", None),)
    assert impact.impacted_modules == ("pkg/app.py", "pkg/core.py", "pkg/lib.py")
    assert impact.related_tests == ("tests/test_app.py",)
    assert impact.unknown == ()


def test_added_deleted_and_renamed_paths_are_all_changed(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/extra.py": "EXTRA = 1\n"})
    _git(repo, "rm", "-q", "pkg/standalone.py", "tests/test_standalone.py")
    _git(repo, "mv", "pkg/app.py", "pkg/application.py")
    _commit(repo, "reshape")

    impact = _impact(repo, base)

    assert set(impact.changed) == {
        ChangedPath("added", "pkg/extra.py", None),
        ChangedPath("deleted", "pkg/standalone.py", None),
        ChangedPath("deleted", "tests/test_standalone.py", None),
        ChangedPath("renamed", "pkg/application.py", "pkg/app.py"),
    }
    assert "pkg/extra.py" in impact.impacted_modules
    assert "pkg/application.py" in impact.impacted_modules


def test_related_tests_are_not_cut_off_at_the_first_changed_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wide"
    _init(root)
    files = {"pkg/__init__.py": "", "tests/__init__.py": ""}
    for index in range(15):
        files[f"pkg/m{index:02}.py"] = "X = 1\n"
        files[f"tests/test_m{index:02}.py"] = f"import pkg.m{index:02}\n"
    _write(root, files)
    base = _commit(root, "base")
    _write(root, {f"pkg/m{index:02}.py": "X = 2\n" for index in range(15)})
    _commit(root, "change all")

    impact = _impact(root, base)

    assert impact.related_tests == tuple(f"tests/test_m{i:02}.py" for i in range(15))
    assert impact.unknown == ()


@pytest.mark.parametrize(
    "path",
    [
        "conftest.py",
        "tests/conftest.py",
        "tests/nested/conftest.py",
        "pyproject.toml",
        "uv.lock",
        "Makefile",
        ".github/workflows/verify.yml",
        "scripts/verify.py",
        "scripts/verification/stage.py",
        "harness/CAPABILITIES.json",
        "skills/first-party/x/SKILL.md",
    ],
)
def test_build_and_verification_config_changes_set_unknown(
    repo: Path, path: str
) -> None:
    base = _base(repo)
    _write(repo, {path: "changed\n"})
    _commit(repo, "touch config")

    impact = _impact(repo, base)

    assert impact.unknown
    assert {reason.path for reason in impact.unknown} >= {path}


def test_a_non_python_file_sets_unknown(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"docs/notes.md": "text\n"})
    _commit(repo, "docs")

    assert _codes(_impact(repo, base)) == {"non-python-file"}


def test_a_renamed_module_that_others_import_sets_unknown(repo: Path) -> None:
    base = _base(repo)
    _git(repo, "mv", "pkg/core.py", "pkg/kernel.py")
    _commit(repo, "rename core")

    impact = _impact(repo, base)

    assert _codes(impact) == {"removed-module-referenced"}
    assert "pkg/lib.py" in impact.impacted_modules
    assert "tests/test_app.py" in impact.related_tests


def test_a_deleted_module_that_others_import_sets_unknown(repo: Path) -> None:
    base = _base(repo)
    _git(repo, "rm", "-q", "pkg/core.py")
    _commit(repo, "delete core")

    assert _codes(_impact(repo, base)) == {"removed-module-referenced"}


def test_a_renamed_module_nobody_imports_stays_known(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/orphan.py": "Z = 1\n"})
    base = _commit(repo, "add orphan")
    _git(repo, "mv", "pkg/orphan.py", "pkg/loner.py")
    _commit(repo, "rename orphan")

    impact = _impact(repo, base)

    assert impact.unknown == ()
    assert impact.changed == (ChangedPath("renamed", "pkg/loner.py", "pkg/orphan.py"),)


def test_related_tests_above_the_threshold_set_unknown(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n", "pkg/standalone.py": "OTHER = 3\n"})
    _commit(repo, "change two")

    within = _impact(repo, base, max_related_tests=2)
    above = _impact(repo, base, max_related_tests=1)

    assert within.unknown == ()
    assert _codes(above) == {"related-tests-over-threshold"}
    assert above.related_tests == ("tests/test_app.py", "tests/test_standalone.py")


def test_a_dynamic_import_in_a_touched_module_sets_unknown(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/standalone.py": "import importlib\nOTHER = 2\n"})
    _commit(repo, "dynamic")

    assert _codes(_impact(repo, base)) == {"dynamic-import"}


def test_an_unavailable_parser_sets_graph_degraded(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n"})
    candidate = _commit(repo, "change core")

    impact = compute_change_impact(repo, base, candidate, backend=UnavailableBackend())

    assert impact.changed == (ChangedPath("modified", "pkg/core.py", None),)
    assert impact.impacted_modules == ()
    assert impact.related_tests == ()
    assert [(reason.code, reason.detail) for reason in impact.unknown] == [
        ("graph-degraded", "parser bundle is not installed")
    ]


def test_an_empty_diff_has_an_empty_known_impact(repo: Path) -> None:
    base = _base(repo)

    impact = _impact(repo, base)

    assert impact == ImpactSet((), (), (), ())
