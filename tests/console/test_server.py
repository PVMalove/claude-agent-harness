"""The console transport is tested on real loopback sockets over a temporary project."""

from __future__ import annotations

import asyncio
import json
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
            writer.write(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n\r\n".encode())
            await writer.drain()
            assert (await asyncio.wait_for(reader.read(), 2)).startswith(b"HTTP/1.1 200")
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
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert "unrecognized arguments: --host" in result.stderr


def test_imports_do_not_require_transport_dependencies() -> None:
    code = '''
import importlib.abc, json, sys
class Absent(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'h11', 'websockets'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Absent())
import harness.console.server, harness.console.launcher, harness.console.pin, harness.health
print(json.dumps({'loaded': sorted(set(sys.modules) & {'h11', 'websockets'})}))
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"loaded": []}
