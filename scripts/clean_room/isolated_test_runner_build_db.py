"""Installed protected compiler build and real PostgreSQL evidence for issue #556.

No host service, container socket or database credentials are used. Missing real binaries are
explicit; HARNESS_REQUIRE_TEST_DATABASE=1 requires a real PostgreSQL pass, never a fake process.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from scripts.clean_room.isolated_test_runner import _expect, _hook, _snapshot
from scripts.clean_room.support import run_health

BUILD_TEST = """import errno, subprocess
assert subprocess.check_output(['artifacts/calc'], text=True).strip() == '5'
try:
    open('artifacts/calc', 'w')
except OSError as error:
    assert error.errno == errno.EROFS
else:
    raise AssertionError('artifact writable in test phase')
"""
DB_TOOL = """import os, subprocess, sys
from pathlib import Path
TOOLS = {tools}
data = os.environ['HARNESS_TEST_DATABASE_DIR']
sockets = os.environ['HARNESS_TEST_DATABASE_SOCKET_DIR']
if sys.argv[1] == 'server':
    subprocess.run([TOOLS['initdb'], '-D', data, '--no-locale', '-A', 'trust', '-U', 'harness_test'], check=True)
    os.execv(TOOLS['postgres'], [TOOLS['postgres'], '-D', data, '-k', sockets, '-h', '',
                               '-c', 'unix_socket_permissions=0700', '-c', 'fsync=off'])
connection = ['-h', sockets, '-U', 'harness_test', '-d', 'postgres']
if sys.argv[1] == 'ready':
    sys.exit(subprocess.run([TOOLS['pg_isready'], *connection], check=False).returncode)
def sql(statement, ok=True):
    result = subprocess.run([TOOLS['psql'], *connection, '-AtX', '-v', 'ON_ERROR_STOP=1',
                             '-v', 'VERBOSITY=sqlstate', '-c', statement], capture_output=True, text=True)
    assert (result.returncode == 0) == ok, result.stderr
    return result.stdout.strip() if ok else result.stderr
assert sql('SELECT version()').startswith('PostgreSQL ')
assert sql('SHOW data_directory') == data
assert sql('SHOW listen_addresses') == ''
assert sql('SELECT inet_server_addr() IS NULL') == 't'
assert sql("SELECT to_regclass('accounts') IS NULL") == 't'
sql('CREATE TABLE accounts (id int PRIMARY KEY, amount int CHECK (amount >= 0)); INSERT INTO accounts VALUES (1, 10)')
sql('BEGIN; UPDATE accounts SET amount = 99; ROLLBACK')
assert sql('SELECT amount FROM accounts WHERE id = 1') == '10'
assert '23514' in sql('INSERT INTO accounts VALUES (2, -1)', False)
assert '23505' in sql('INSERT INTO accounts VALUES (1, 20)', False)
assert sql('SELECT count(*) FROM accounts') == '1'
Path(os.environ['HARNESS_TEST_REPORT_DIR'], 'database-dir').write_text(data)
"""


def _settings(project: Path, runner: dict[str, object]) -> None:
    path = project / ".harness/project.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["test_runner"] = runner
    path.write_text(json.dumps(data), encoding="utf-8")
    run_health(project)


def run(project: Path) -> None:
    """Prove both phases against installed files; the caller creates a fresh project."""
    if _hook(project, "--probe").get("status") != "ok":
        if os.environ.get("HARNESS_REQUIRE_TEST_DATABASE") == "1":
            raise SystemExit(
                "real database proof unavailable: protected execution unavailable"
            )
        print(
            "isolated-build-db: protected execution unavailable; real build/database not proved",
            flush=True,
        )
        return
    compiler = shutil.which("cc")
    if compiler is None:
        if os.environ.get("HARNESS_REQUIRE_TEST_SANDBOX") == "1":
            raise SystemExit("real compiler build unavailable: cc missing")
        print("isolated-build-db: cc missing; real build not proved", flush=True)
    else:
        (project / "artifacts").mkdir()
        (project / "src/calc.c").write_text(
            '#include <stdio.h>\nint main(void) { puts("5"); return 0; }\n'
        )
        (project / "tests/build_case.py").write_text(BUILD_TEST)
        build = {
            "command": [compiler, "src/calc.c", "-o", "artifacts/calc"],
            "artifact_paths": ["artifacts/**"],
        }
        _settings(
            project,
            {
                "command": ["python3", "tests/build_case.py", "{test_id}"],
                "build": build,
            },
        )
        before = _snapshot(project)
        _expect(_hook(project, "build.legit"), "compiler build", result="passed")
        assert _snapshot(project) == before and not list(
            (project / "artifacts").iterdir()
        )
        print(
            "isolated-build-db: real compiler, RO artifacts and protected sources verified",
            flush=True,
        )
    tools = {
        name: shutil.which(name)
        for name in ("postgres", "initdb", "psql", "pg_isready")
    }
    database = {
        "engine": "postgresql",
        "test_ids": ["sql.*"],
        "command": ["python3", "src/db_tool.py", "server"],
        "ready_command": ["python3", "src/db_tool.py", "ready"],
    }
    # A pure domain test must succeed even when the selected DB tools do not exist.
    (project / "tests/domain.py").write_text("assert 2 + 3 == 5\n")
    _settings(
        project,
        {"command": ["python3", "tests/domain.py", "{test_id}"], "database": database},
    )
    _expect(_hook(project, "domain.add"), "domain without DB tools", result="passed")
    if not all(tools.values()):
        missing = ",".join(name for name, path in tools.items() if not path)
        if os.environ.get("HARNESS_REQUIRE_TEST_DATABASE") == "1":
            raise SystemExit(f"real PostgreSQL proof unavailable: missing {missing}")
        print(
            f"isolated-build-db: real PostgreSQL not proved; missing {missing}",
            flush=True,
        )
        return
    (project / "src/db_tool.py").write_text(DB_TOOL.replace("{tools}", repr(tools)))
    _settings(
        project,
        {
            "command": ["python3", "src/db_tool.py", "test", "{test_id}"],
            "database": database,
        },
    )
    before = _snapshot(project)
    directories = []
    for _ in range(2):
        answer = _hook(project, "sql.constraints")
        _expect(answer, "real PostgreSQL SQL/rollback/constraints", result="passed")
        report = (
            project
            / ".harness/.sandboxes/runs"
            / f"isolated-test-{answer['run_id']}"
            / "reports/database-dir"
        )
        directories.append(report.read_text())
    assert directories[0] != directories[1]
    assert _snapshot(project) == before
    print(
        "isolated-build-db: real PostgreSQL identity, private fresh clusters, SQL, rollback, constraints and no TCP verified",
        flush=True,
    )


if __name__ == "__main__":
    import tempfile
    from harness.storage import storage_path
    from scripts.clean_room.isolated_test_runner import _install

    root = storage_path(Path.cwd(), "runs")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-db-proof-", dir=root) as temporary:
        run(_install(Path(temporary) / "project"))
