"""The console transport is tested on real loopback sockets over a temporary project."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from harness.console.server import CONSOLE_SERVER_REQUIREMENTS, ConsoleServer


@pytest.fixture
def project(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_server_dependencies_are_exact_dev_pins_only() -> None:
    config = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert config["project"]["dependencies"] == []
    assert set(CONSOLE_SERVER_REQUIREMENTS) <= set(config["dependency-groups"]["dev"])
    assert CONSOLE_SERVER_REQUIREMENTS == ("h11==0.16.0", "websockets==17.3")


def test_server_binds_ipv4_loopback_and_ephemeral_port(project: Path) -> None:
    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        try:
            assert server.port > 0
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(
                f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n\r\n".encode()
            )
            await writer.drain()
            assert (await asyncio.wait_for(reader.read(), 2)).startswith(
                b"HTTP/1.1 200"
            )
            writer.close()
            await writer.wait_closed()
            with pytest.raises(AttributeError):
                setattr(server, "port", 42)
        finally:
            await server.close()
        await asyncio.wait_for(server.wait_closed(), 2)
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", server.port)

    asyncio.run(scenario())


def test_server_module_has_no_host_override() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "harness.console.server", ".", "--host", "0.0.0.0"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "unrecognized arguments: --host" in result.stderr


def test_imports_do_not_require_transport_dependencies() -> None:
    code = """
import importlib.abc, json, sys
class Absent(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'h11', 'websockets'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Absent())
import harness.console.server, harness.console.launcher, harness.console.pin, harness.health
print(json.dumps({'loaded': sorted(set(sys.modules) & {'h11', 'websockets'})}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"loaded": []}


def test_discovery_is_private_fresh_and_removed_by_its_owner(project: Path) -> None:
    from harness.storage import storage_path
    import os
    import stat

    async def scenario() -> None:
        path = storage_path(project, "runs", "console") / "server.json"
        tokens: list[str] = []
        for _ in range(2):
            server = ConsoleServer(project)
            await server.start()
            try:
                record = json.loads(path.read_text())
                assert set(record) == {"port", "token", "pid"}
                assert record["port"] == server.port
                assert record["pid"] == os.getpid()
                token = record["token"]
                assert len(token) == 64 and set(token) <= set("0123456789abcdef")
                tokens.append(token)
                if os.name != "nt":
                    assert stat.S_IMODE(path.stat().st_mode) == 0o600
                    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            finally:
                await server.close()
            assert not path.exists()
        assert tokens[0] != tokens[1]
        assert (path.parent / "server.lock").exists()

    asyncio.run(scenario())


def test_live_discovery_owner_cannot_be_stolen_by_another_process(
    project: Path,
) -> None:
    from harness.storage import storage_path

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        path = storage_path(project, "runs", "console") / "server.json"
        original = path.read_bytes()
        code = """
import asyncio, sys
from pathlib import Path
from harness.console.discovery import DiscoveryBusyError
from harness.console.server import ConsoleServer
async def main():
    server = ConsoleServer(Path(sys.argv[1]))
    try:
        await server.start()
    except DiscoveryBusyError:
        print('busy')
    else:
        await server.close()
        raise RuntimeError('stole live owner')
asyncio.run(main())
"""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", code, str(project)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "busy"
            assert path.read_bytes() == original
        finally:
            await server.close()

    asyncio.run(scenario())


def test_stale_discovery_is_replaced_atomically_and_cleanup_checks_owner(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console import discovery
    import os
    import stat

    async def scenario() -> None:
        path = discovery.discovery_path(project)
        path.parent.mkdir(parents=True)
        path.write_text('{"pid": -1, "port": 1, "token": "stale"}')
        stale = path.read_bytes()
        replace = os.replace
        published: list[Path] = []

        def observed(source: Path, destination: Path) -> None:
            assert destination == path
            assert path.read_bytes() == stale
            if os.name != "nt":
                assert stat.S_IMODE(source.stat().st_mode) == 0o600
            assert json.loads(source.read_text())["port"] == server.port
            published.append(source)
            replace(source, destination)

        server = ConsoleServer(project)
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", observed)
            await server.start()
        assert len(published) == 1 and not published[0].exists()
        # An unrelated replacement record must survive this owner's cleanup.
        other = path.with_name("replacement.json")
        other.write_text('{"token":"different owner"}')
        replace(other, path)
        await server.close()
        assert json.loads(path.read_text()) == {"token": "different owner"}
        path.unlink()

    asyncio.run(scenario())


def test_publication_failure_rolls_back_listener_and_lock(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console import discovery

    async def scenario() -> None:
        server = ConsoleServer(project)

        def fail(source: Path, destination: Path) -> None:
            raise OSError("publication failed")

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", fail)
            with pytest.raises(OSError, match="publication failed"):
                await server.start()
        await asyncio.wait_for(server.wait_closed(), 2)
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", server.port)
        path = discovery.discovery_path(project)
        assert not path.exists()
        assert not list(path.parent.glob("*.tmp"))
        replacement = ConsoleServer(project)
        await replacement.start()
        await replacement.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "component",
    [
        ".harness",
        ".harness/.sandboxes",
        ".harness/.sandboxes/runs",
        ".harness/.sandboxes/runs/console",
        ".harness/.sandboxes/runs/console/server.json",
        ".harness/.sandboxes/runs/console/server.lock",
    ],
)
def test_discovery_refuses_symlinks(project: Path, component: str) -> None:
    link = project / component
    link.parent.mkdir(parents=True, exist_ok=True)
    destination = project / "elsewhere"
    destination.mkdir()
    try:
        link.symlink_to(destination, target_is_directory=True)
    except OSError:
        if sys.platform == "win32":
            pytest.skip("Windows symlink privilege unavailable")
        raise

    async def scenario() -> None:
        server = ConsoleServer(project)
        with pytest.raises((ValueError, OSError)):
            await server.start()
        await asyncio.wait_for(server.wait_closed(), 2)
        assert not list(destination.iterdir())

    asyncio.run(scenario())


def test_linked_worktree_uses_main_project_discovery(project: Path) -> None:
    from harness.console.discovery import DiscoveryBusyError, discovery_path

    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    linked = project.parent / "linked"
    subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "-qb", "linked", str(linked)],
        check=True,
        capture_output=True,
    )
    assert discovery_path(linked) == discovery_path(project)

    async def scenario() -> None:
        owner = ConsoleServer(project)
        await owner.start()
        try:
            with pytest.raises(DiscoveryBusyError):
                await ConsoleServer(linked).start()
            assert not (linked / ".harness").exists()
        finally:
            await owner.close()

    asyncio.run(scenario())


def test_windows_acl_failure_precedes_any_secret_write(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console import discovery

    async def scenario() -> None:
        server = ConsoleServer(project)
        create_private = discovery._private_file

        def denied(path: Path) -> int:
            if path.name.startswith(".server-"):
                raise PermissionError("private ACL unavailable")
            return create_private(path)

        with monkeypatch.context() as patch:
            # All secret files use this seam; a failed secure create must never fall back.
            patch.setattr(discovery, "_private_file", denied)
            with pytest.raises(PermissionError, match="private ACL unavailable"):
                await server.start()
        path = discovery.discovery_path(project)
        assert not path.exists()
        assert not list(path.parent.glob("*.tmp"))
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", server.port)
        await asyncio.wait_for(server.wait_closed(), 2)

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="requires a real Windows ACL")
def test_windows_discovery_has_protected_current_user_only_dacl(project: Path) -> None:
    from harness.console.discovery import discovery_path

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        try:
            path = str(discovery_path(project)).replace("'", "''")
            script = (
                "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                f"$paths=@('{path}', (Split-Path -Path '{path}')); "
                "foreach($p in $paths) { $a=Get-Acl -LiteralPath $p; "
                "if(!$a.AreAccessRulesProtected -or $a.Access.Count -ne 1) {exit 3}; "
                "if($a.Access[0].IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $sid) {exit 4}; "
                "if($a.Access[0].AccessControlType -ne 'Allow' -or $a.Access[0].FileSystemRights -ne 'FullControl') {exit 5} }; 'private'"
            )
            result = await asyncio.to_thread(
                subprocess.run,
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "private"
        finally:
            await server.close()

    asyncio.run(scenario())
