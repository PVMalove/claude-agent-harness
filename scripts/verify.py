#!/usr/bin/env python3
"""Project-wide verification: config/skill sanity checks, vendor pin integrity, then the full
clean-room test-clean-room run."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import atexit
from collections.abc import Callable, Mapping
from pathlib import Path

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "[ERROR] verify requires Python {}+ (found {}).\n".format(
            ".".join(map(str, MIN_PYTHON)), sys.version.split()[0]
        )
    )
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.storage import storage_path


def run_ok(
    cmd: list[str],
    *,
    env: Mapping[str, str] | None = None,
    stdout: int | None = None,
    cwd: Path | None = None,
) -> None:
    """Run a command that must succeed; propagate its real exit code (and its own stderr) on
    failure, matching `set -e` instead of dumping a CalledProcessError traceback."""
    result = subprocess.run(cmd, env=env, stdout=stdout, cwd=cwd, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def run_stage(
    name: str,
    cmd: list[str],
    *,
    env: Mapping[str, str] | None = None,
    stdout: int | None = None,
    cwd: Path | None = None,
) -> None:
    """Run one named verification stage and emit its elapsed monotonic duration."""
    started = time.perf_counter()
    try:
        run_ok(cmd, env=env, stdout=stdout, cwd=cwd)
    except SystemExit:
        print(f"[verify] {name}: failed in {time.perf_counter() - started:.2f}s")
        raise
    print(f"[verify] {name}: passed in {time.perf_counter() - started:.2f}s")


def isolated_temp_env(base: Mapping[str, str], run_tmp: Path) -> dict[str, str]:
    """`base` with every temp variable pointed at this run's own root (issue #305). pytest's
    default root is %TEMP%\\pytest-of-<USERNAME>, shared by every account that inherits USERNAME
    and TEMP (agent sandboxes run as separate local users); Python 3.13+ creates it owner-only on
    Windows, so whichever account made it first locks the others out with WinError 5."""
    return dict(
        base,
        TMP=str(run_tmp),
        TEMP=str(run_tmp),
        TMPDIR=str(run_tmp),
        PYTHONPYCACHEPREFIX=str(run_tmp / "pycache"),
    )


def _clear_read_only(
    func: Callable[[str], object], path: str, _exc: BaseException
) -> None:
    # git leaves object files read-only on Windows; clear the bit and retry the removal.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, onexc=_clear_read_only)


def grep_line(path: Path, exact_line: str) -> None:
    if exact_line not in path.read_text(encoding="utf-8").splitlines():
        sys.exit(f"{path}: expected line {exact_line!r} not found")


def grep_contains(path: Path, substring: str) -> None:
    if substring not in path.read_text(encoding="utf-8"):
        sys.exit(f"{path}: expected text {substring!r} not found")


def check_no_todo(base: Path) -> None:
    found = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_bytes().split(b"\n"), 1):
            if b"TODO" in line:
                found.append(
                    f"{path}:{lineno}:{line.decode('utf-8', errors='backslashreplace')}"
                )
    if found:
        print("\n".join(found))
        sys.exit("global-skills still contains TODO markers")


DOCS_AGENTS_TEMPLATE = ROOT / "harness" / "project" / "docs-agents"


def _normalized_text(path: Path) -> str:
    """Decode ignoring a BOM and normalize line endings, so a file re-saved by an editor that
    defaults to UTF-8-BOM/CRLF doesn't read as content drift against a plain-UTF-8/LF copy."""
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")


def check_docs_agents_mirror() -> None:
    """docs/agents/*.md is this repo's own copy of the files harness/project/docs-agents/*.md
    scaffolds into every pvmalove-suite project (scaffold_pvmalove_extras in harness/bin/
    harness). Both are maintained by hand - catch prose drift between them here instead of
    relying on every edit remembering to touch both."""
    for template in sorted(DOCS_AGENTS_TEMPLATE.glob("*.md")):
        counterpart = ROOT / "docs" / "agents" / template.name
        if not counterpart.is_file():
            sys.exit(f"{template} has no docs/agents counterpart: {counterpart}")
        if _normalized_text(template) != _normalized_text(counterpart):
            sys.exit(
                f"docs/agents/{template.name} has drifted from harness/project/docs-agents/{template.name}"
            )


RETIRED_PATH_INVENTORY_TERM = re.compile(r"filtered\s+Repo\s+Map", re.IGNORECASE)
PATH_INVENTORY_ROOTS = (
    ROOT / "skills" / "first-party" / "pvmalove" / "to-tickets",
    ROOT / "docs" / "agents",
    DOCS_AGENTS_TEMPLATE,
    ROOT / "docs" / "skills",
    ROOT / "docs" / "diagrams",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "README.md",
)


def check_no_retired_path_inventory_term() -> None:
    """The path-only artifact /to-tickets builds is the "Path inventory"; "Repo Map" belongs only
    to the semantic map (CONTEXT.md). Catch the retired "filtered Repo Map" name coming back."""
    for base in PATH_INVENTORY_ROOTS:
        for path in sorted(base.rglob("*") if base.is_dir() else [base]):
            if path.is_file() and RETIRED_PATH_INVENTORY_TERM.search(
                path.read_text(encoding="utf-8", errors="replace")
            ):
                sys.exit(
                    f'{path}: retired term "filtered Repo Map"; use "Path inventory"'
                )


def check_docs_agents_enumeration() -> None:
    """README.md and harness-guide.md each spell out, by hand, the docs/agents/{...}.md
    brace-list scaffold_pvmalove_extras deploys. Catch a file added to (or removed from)
    harness/project/docs-agents/ without updating both listings - this shipped once already:
    harness-guide.md itself was missing from its own enumeration."""
    names = {p.stem for p in DOCS_AGENTS_TEMPLATE.glob("*.md")}
    for doc in (ROOT / "README.md", ROOT / "docs" / "agents" / "harness-guide.md"):
        text = _normalized_text(doc)
        match = re.search(r"docs/agents/\{([a-z0-9,-]+)\}\.md", text)
        if not match:
            sys.exit(f"{doc}: no docs/agents/{{...}}.md enumeration found")
        listed = set(match.group(1).split(","))
        if listed != names:
            sys.exit(
                f"{doc}: docs/agents/{{...}}.md enumeration {sorted(listed)} does not match "
                f"harness/project/docs-agents/*.md {sorted(names)}"
            )


def check_pvmalove_override_docs_sync() -> None:
    """CAPABILITIES.json's pvmalove-suite.overrides is the source of truth for which skills are
    locally customized; docs/agents/harness-guide.md section 7 restates the same set by hand,
    with the *why* prose CAPABILITIES.json doesn't carry. Catch the two falling out of sync -
    this exact drift shipped once already: wayfinder was added to overrides without a matching
    table row."""
    capabilities = json.loads(
        (ROOT / "harness" / "CAPABILITIES.json").read_text(encoding="utf-8")
    )
    expected = set(capabilities["pvmalove-suite"]["overrides"].keys())

    guide = _normalized_text(ROOT / "docs" / "agents" / "harness-guide.md")
    section = re.search(r"\n## 7\. .*?\n(.*?)\n## ", guide, re.DOTALL)
    if not section:
        sys.exit("harness-guide.md: could not find section 7 (local customizations)")
    documented = set(
        re.findall(r"^\| `([a-z0-9-]+)` \|", section.group(1), re.MULTILINE)
    )
    if documented != expected:
        sys.exit(
            "harness-guide.md section 7 table is out of sync with pvmalove-suite overrides in "
            f"CAPABILITIES.json: missing={sorted(expected - documented)} extra={sorted(documented - expected)}"
        )


def check_pvmalove_additions_docs_sync() -> None:
    """CAPABILITIES.json's pvmalove-suite.additions is the source of truth for first-party-only
    skills; docs/agents/harness-guide.md section 12's project-specific table restates the same
    set by hand. Catch the two falling out of sync - this exact drift shipped once already:
    setup-labels was added to additions without a matching table row."""
    capabilities = json.loads(
        (ROOT / "harness" / "CAPABILITIES.json").read_text(encoding="utf-8")
    )
    expected = {
        entry.removeprefix("first-party/pvmalove/")
        for entry in capabilities["pvmalove-suite"]["additions"]
    }

    guide = _normalized_text(ROOT / "docs" / "agents" / "harness-guide.md")
    section = re.search(
        r"\n### Проектные \(first-party, вне апстрима\)\n(.*?)\n---\n", guide, re.DOTALL
    )
    if not section:
        sys.exit(
            "harness-guide.md: could not find the project-specific skill catalog table"
        )
    documented = set(
        re.findall(r"^\| `([a-z0-9-]+)` \(skill\) \|", section.group(1), re.MULTILINE)
    )
    if documented != expected:
        sys.exit(
            "harness-guide.md project-specific skill table is out of sync with pvmalove-suite "
            f"additions in CAPABILITIES.json: missing={sorted(expected - documented)} extra={sorted(documented - expected)}"
        )


def check_pvmalove_suite_summary_sync() -> None:
    """README.md and CONTEXT.md each restate pvmalove-suite's overrides/additions by name in
    prose, next to the same counts harness-guide.md's tables already get checked against above.
    Catch the same drift class there too - this exact drift shipped once already: stale '5
    overrides, 1 addition' counts survived three days and a section-7-only fix untouched."""
    capabilities = json.loads(
        (ROOT / "harness" / "CAPABILITIES.json").read_text(encoding="utf-8")
    )
    expected_overrides = set(capabilities["pvmalove-suite"]["overrides"].keys())
    expected_additions = {
        entry.removeprefix("first-party/pvmalove/")
        for entry in capabilities["pvmalove-suite"]["additions"]
    }

    for doc in (ROOT / "README.md", ROOT / "CONTEXT.md"):
        text = _normalized_text(doc)

        overrides_match = re.search(
            r"переопределены в `skills/first-party/pvmalove/`:\s*(.+?);", text
        )
        if not overrides_match:
            sys.exit(f"{doc}: no pvmalove-suite overrides list found")
        documented_overrides = set(
            re.findall(r"`([a-z0-9-]+)`", overrides_match.group(1))
        )
        if documented_overrides != expected_overrides:
            sys.exit(
                f"{doc}: overrides list {sorted(documented_overrides)} does not match "
                f"CAPABILITIES.json overrides {sorted(expected_overrides)}"
            )

        additions_match = re.search(r"доп\. скиллы:\s*(.+?)\.", text)
        if not additions_match:
            sys.exit(f"{doc}: no pvmalove-suite additions list found")
        documented_additions = set(
            re.findall(r"`([a-z0-9-]+)`", additions_match.group(1))
        )
        if documented_additions != expected_additions:
            sys.exit(
                f"{doc}: additions list {sorted(documented_additions)} does not match "
                f"CAPABILITIES.json additions {sorted(expected_additions)}"
            )


def check_vendor_pin() -> None:
    plugin = json.loads(
        (ROOT / "third_party" / "mattpocock-skills" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    capabilities = json.loads(
        (ROOT / "harness" / "CAPABILITIES.json").read_text(encoding="utf-8")
    )
    expected = [entry.removeprefix("./skills/") for entry in plugin["skills"]]
    actual = capabilities["mattpocock-suite"]["skills"]
    actual = [entry.removeprefix("vendor/mattpocock/") for entry in actual]
    if expected != actual:
        sys.exit("mattpocock-suite does not match the pinned plugin manifest")

    vendor = ROOT / "skills" / "vendor" / "mattpocock"
    skills = sorted(vendor.rglob("SKILL.md"))
    if len(skills) != len(expected):
        sys.exit(
            f"vendor count mismatch: manifest={len(expected)} snapshot={len(skills)}"
        )

    checksum_lines = (
        (ROOT / "third_party" / "mattpocock-skills" / "SHA256SUMS")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    vendor_files = sorted(path for path in vendor.rglob("*") if path.is_file())
    if len(checksum_lines) != len(vendor_files):
        sys.exit(
            f"vendor file count mismatch: checksums={len(checksum_lines)} snapshot={len(vendor_files)}"
        )
    for line in checksum_lines:
        expected_hash, relative = line.split("  ", 1)
        payload = (ROOT / relative).read_bytes()
        actual_hash = hashlib.sha256(payload).hexdigest()
        if actual_hash != expected_hash:
            sys.exit(f"vendor checksum mismatch: {relative}")


ALWAYS_SENT_INSTRUCTION_FILES = [
    ROOT / "CLAUDE.md",
    ROOT / "AGENTS.md",
    ROOT / "harness" / "orchestration" / "playbook.md",
] + sorted((ROOT / "harness" / "orchestration" / "roles").glob("*.md"))
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})?\b"
)
_DISPATCH_ID_RE = re.compile(
    r"\b(?:batch|dispatch)-[0-9a-fA-F][0-9a-fA-F-]{5,}\b", re.IGNORECASE
)
_HEX_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")


def check_no_dispatch_specific_data_in_always_sent_files() -> None:
    """The instruction files sent with every dispatch (system instructions, role manifests, the
    playbook) must stay a stable, cacheable prefix. Per-dispatch data (commit SHA, timestamp,
    batch/dispatch ID) belongs only in the immutable brief, never here."""
    problems = []
    for path in ALWAYS_SENT_INSTRUCTION_FILES:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _TIMESTAMP_RE.search(line):
                problems.append(
                    f"{path}:{lineno}: looks like a timestamp: {line.strip()}"
                )
            if _DISPATCH_ID_RE.search(line):
                problems.append(
                    f"{path}:{lineno}: looks like a batch/dispatch identifier: {line.strip()}"
                )
            for token in _HEX_TOKEN_RE.findall(line):
                if any(ch.isdigit() for ch in token):
                    problems.append(
                        f"{path}:{lineno}: looks like a commit SHA ({token}): {line.strip()}"
                    )
    if problems:
        sys.exit(
            "always-sent instruction files must stay free of dispatch-specific data:\n"
            + "\n".join(problems)
        )


def main() -> None:
    # Create the short, owner-specific root before *any* Python subprocess.  py_compile and mypy
    # also write bytecode; leaving their cache beside source files fails in restricted worktrees.
    tests_root = storage_path(ROOT, "tmp", "tests")
    tests_root.mkdir(parents=True, exist_ok=True)
    run_tmp = Path(tempfile.mkdtemp(prefix="v", dir=tests_root))
    (run_tmp / ".active.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    atexit.register(lambda: remove_tree(run_tmp) if run_tmp.exists() else None)
    test_env = isolated_temp_env(dict(os.environ, PYTHONPATH=str(ROOT)), run_tmp)
    run_ok(
        [
            sys.executable,
            "-m",
            "json.tool",
            str(ROOT / "harness" / "CAPABILITIES.json"),
        ],
        stdout=subprocess.DEVNULL, env=test_env,
    )
    run_ok(
        [
            sys.executable,
            "-m",
            "py_compile",
            str(ROOT / "harness" / "bin" / "harness"),
            str(ROOT / "scripts" / "build_registry.py"),
            str(ROOT / "bin" / "install-global"),
            str(ROOT / "scripts" / "verify.py"),
            str(ROOT / "scripts" / "test_clean_room.py"),
        ], env=test_env
    )

    start_project = ROOT / "global-skills" / "start-project" / "SKILL.md"
    integrate_project = ROOT / "global-skills" / "integrate-project" / "SKILL.md"
    grep_line(start_project, "name: start-project")
    grep_line(integrate_project, "name: integrate-project")
    check_no_todo(ROOT / "global-skills")
    grep_contains(start_project, "until the owner confirms")
    grep_contains(start_project, "Prove each layer separately")
    grep_contains(integrate_project, "Audit is read-only")
    grep_contains(ROOT / "docs" / "skills" / "implement.md", "module-owned guidance")
    grep_contains(
        ROOT / "docs" / "skills" / "pilot.md",
        "самоотчёт роли не является token telemetry",
    )

    run_ok([sys.executable, str(ROOT / "scripts" / "build_registry.py")], env=test_env)
    run_ok(["git", "-C", str(ROOT), "diff", "--exit-code", "--", "skills/REGISTRY.md"])

    check_docs_agents_mirror()
    check_no_retired_path_inventory_term()
    grep_contains(
        ROOT / "skills" / "first-party" / "pvmalove" / "to-tickets" / "SKILL.md",
        "or symbol signatures",
    )
    agents_seed = ROOT / "harness" / "project" / "AGENTS.md.tmpl"
    grep_contains(agents_seed, "For code discovery, run the Repo Map")
    grep_contains(agents_seed, "then use targeted `rg` searches and reads.")
    check_docs_agents_enumeration()
    check_pvmalove_override_docs_sync()
    check_pvmalove_additions_docs_sync()
    check_pvmalove_suite_summary_sync()
    check_vendor_pin()
    check_no_dispatch_specific_data_in_always_sent_files()

    run_stage("mypy", [sys.executable, "-m", "mypy"], cwd=ROOT, env=test_env)

    try:
        run_stage(
            "pytest",
            [
                sys.executable,
                "-m",
                "pytest",
                "-n",
                "4",
                "--basetemp",
                str(run_tmp / "p"),
                str(ROOT / "tests"),
            ],
            env=test_env,
        )
        run_stage(
            "clean-room",
            [sys.executable, str(ROOT / "scripts" / "test_clean_room.py")],
            env=dict(test_env, HARNESS_TEST_RUN_ROOT=str(run_tmp)),
        )
    finally:
        remove_tree(run_tmp)

    print("agent-harness verification passed")


if __name__ == "__main__":
    main()
