"""Candidate-installed memory and health run without the source repository."""

import json
import os
import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).resolve().parents[2] / "harness/bin/harness.py"


def test_installed_search_cli_returns_pointers_without_source_or_writes(
    tmp_path: Path,
) -> None:
    """A target project can run read-only memory search with just its installed payload."""
    from .test_build import configure, source

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    install = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "init",
            str(tmp_path),
            "--capability",
            "pvmalove-suite",
            "--qa-gate-command",
            "true",
        ],
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Glossary\nStatus: accepted\ntransaction")
    build = subprocess.run(
        [sys.executable, str(CLI), "memory", "build", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    cache = tmp_path / ".harness/.sandboxes/cache/memory"
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in cache.iterdir()}
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(tmp_path / ".harness/memory/search_cli.py"),
            str(tmp_path),
            "transaction",
        ],
        cwd=tmp_path.parent,
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "ok"
    assert len(data["pointers"]) == 1
    assert data["pointers"][0]["path"] == "CONTEXT.md"
    assert data["pointers"][0]["status"] == "accepted"
    assert {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in cache.iterdir()
    } == before
    assert not (tmp_path / ".harness/bin/harness.py").exists()


def test_installed_memory_uses_shared_contract(tmp_path: Path) -> None:
    """The capability ships dependencies needed by runtime memory and project health."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "init",
            str(tmp_path),
            "--capability",
            "pvmalove-suite",
            "--qa-gate-command",
            "true",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    code = """from pathlib import Path
import harness.memory
import json
from harness.health.project_files import validate_project_json
problems = []
validate_project_json(Path.cwd(), problems)
assert not problems, problems
assert harness.memory.build(Path.cwd()) == {"status": "built", "indexed": 0}
assert not Path('.harness/orchestration/__init__.py').exists()
config_path = Path('.harness/project.json')
config = json.loads(config_path.read_text())
config['memory'] = {'enabled': True}
config['memory_policy'].update(source_types=['task_archive', 'qa_finding', 'ledger', 'completion_report'], allow_paths=['docs/tasks/**/*.md', '.harness/orchestration/state/generations/**/*.json'], redact_rules=['projectsecret'])
config_path.write_text(json.dumps(config))
archive = Path('docs/tasks/issue-423-memory/issue-423-spec-memory.md')
archive.parent.mkdir(parents=True)
archive.write_text('# Archive\\narchiveword')
state = Path('.harness/orchestration/state')
generation = state / 'generations/generation-fixture'
(generation / 'reports').mkdir(parents=True)
(generation / 'dispatch-status').mkdir()
(state / 'ledger.json').write_text(json.dumps({'version': 3, 'generation': 'generation-fixture', 'selected_at': '2026-09-30'}))
(generation / 'reports/qa.json').write_text(json.dumps({'role': 'qa', 'outcome': 'pass', 'output': 'qualityword token=private projectsecret'}))
(generation / 'reports/dev.json').write_text(json.dumps({'role': 'developer', 'lessons': ['lessonword token=private projectsecret'], 'used_memory': ['unusedword']}))
(generation / 'dispatch-status/d.json').write_text(json.dumps({'dispatch_id': 'dispatchword', 'state': 'working'}))
problems = []
validate_project_json(Path.cwd(), problems)
assert not problems, problems
for term in ('archiveword', 'qualityword', 'dispatchword', 'lessonword'):
    assert harness.memory.search_with_refresh(Path.cwd(), term)['pointers']
assert harness.memory.search(Path.cwd(), 'private')['pointers'] == []
assert harness.memory.search(Path.cwd(), 'projectsecret')['pointers'] == []
assert harness.memory.search(Path.cwd(), 'unusedword')['pointers'] == []
assert harness.memory.search(Path.cwd(), 'lessonword')['pointers'][0]['status'] == 'не подтверждено человеком'
"""
    environment = {**os.environ, "PYTHONPATH": ""}
    # The installed .harness directory is the package alias used by existing standalone tools.
    code = (
        "import importlib.machinery, importlib.util, sys\nfrom pathlib import Path\nassert not Path('.harness/__init__.py').exists()\nspec = importlib.machinery.ModuleSpec('harness', None, is_package=True)\nspec.submodule_search_locations = ['.harness']\nsys.modules['harness'] = importlib.util.module_from_spec(spec)\n"
        + code
    )
    installed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert installed.returncode == 0, installed.stderr
    (tmp_path / "AGENTS.md").write_text("# Fixture instructions\n", encoding="utf-8")
    health = subprocess.run(
        [sys.executable, str(CLI), "health", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert health.returncode == 0, [
        check["id"]
        for check in json.loads(health.stdout)["checks"]
        if check["status"] == "fail"
    ]
