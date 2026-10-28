"""The console transport is tested on real loopback sockets over a temporary project."""

from __future__ import annotations

import asyncio
import functools
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tomllib

import pytest

from harness.console.pin import CONSOLE_SERVER_REQUIREMENTS
from harness.console.data import DashboardData
from harness.console.data import collect_dashboard
from harness.console.server import ConsoleServer
from harness.console.snapshots import (
    HOME_TTL_SECONDS,
    HomeContext,
    HomeSnapshot,
    SnapshotService,
    build_home_snapshot,
    collect_home,
)
from tests.console.test_snapshots import FakeClock


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


def test_server_binds_ipv4_loopback_and_ephemeral_port(project: Path) -> None:
    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        try:
            import socket

            assert server.port > 0
            assert server._listener is not None
            assert server._listener.sockets[0].family == socket.AF_INET
            assert server._listener.sockets[0].getsockname() == (
                "127.0.0.1",
                server.port,
            )
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


def test_server_command_has_no_host_override() -> None:
    harness_cli = Path(__file__).parents[2] / "harness" / "bin" / "harness.py"
    result = subprocess.run(
        [sys.executable, str(harness_cli), "console-server", ".", "--host", "0.0.0.0"],
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


def test_authenticated_websocket_ping_and_http_method_with_body(project: Path) -> None:
    from harness.console.discovery import discovery_path
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus
    import http.client

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        record = json.loads(discovery_path(project).read_text())
        uri = f"ws://127.0.0.1:{server.port}/ws"
        try:
            with pytest.raises(InvalidStatus) as rejected:
                async with connect(uri, proxy=None, open_timeout=2):
                    pytest.fail("missing token was accepted")
            assert rejected.value.response.status_code == 401
            async with connect(
                uri,
                proxy=None,
                additional_headers={"Authorization": "Bearer " + record["token"]},
                open_timeout=2,
                close_timeout=1,
            ) as client:
                pong = await client.ping(b"alive")
                await asyncio.wait_for(pong, 2)

            def post() -> tuple[int, str | None, bytes]:
                client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
                try:
                    client.request("POST", "/", body=b"nonempty body")
                    response = client.getresponse()
                    return response.status, response.getheader("Allow"), response.read()
                finally:
                    client.close()

            status, allow, _ = await asyncio.to_thread(post)
            assert (status, allow) == (405, "GET, HEAD")
        finally:
            await server.close()

    asyncio.run(scenario())


async def _raw(port: int, data: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 2)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("credential", ["native", "browser-bearer", "browser-cookie"])
def test_native_and_browser_websocket_credentials(
    project: Path, credential: str
) -> None:
    from harness.console.discovery import discovery_path
    from websockets.asyncio.client import connect

    from websockets.typing import Origin

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        token = json.loads(discovery_path(project).read_text())["token"]
        origin = Origin(f"http://127.0.0.1:{server.port}") if credential != "native" else None
        headers = (
            {"Cookie": "console_token=" + token}
            if credential == "browser-cookie"
            else {"Authorization": "Bearer " + token}
        )
        try:
            async with connect(
                f"ws://127.0.0.1:{server.port}/ws",
                origin=origin,
                additional_headers=headers,
                proxy=None,
                open_timeout=2,
                close_timeout=1,
            ) as client:
                await asyncio.wait_for(await client.ping(b"test"), 2)
        finally:
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case,status",
    [
        ("wrong-token", 401),
        ("duplicate-auth", 401),
        ("conflict", 401),
        ("duplicate-cookie", 401),
        ("cookie-no-origin", 401),
        ("ambiguous-cookie", 401),
        ("foreign-origin", 403),
        ("null-origin", 403),
        ("duplicate-origin", 403),
        ("foreign-host", 403),
        ("wrong-port", 403),
        ("duplicate-host", 400),
        ("missing-host", 400),
        ("query-token", 404),
        ("body-upgrade", 400),
        ("chunked-upgrade", 400),
        ("bad-key", 400),
        ("bad-version", 400),
    ],
)
def test_websocket_admission_fails_closed(
    project: Path, case: str, status: int
) -> None:
    from harness.console.discovery import discovery_path

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        token = json.loads(discovery_path(project).read_text())["token"]
        host = f"127.0.0.1:{server.port}"
        origin = "http://" + host
        headers = [
            ("Host", host),
            ("Upgrade", "websocket"),
            ("Connection", "Upgrade"),
            ("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ=="),
            ("Sec-WebSocket-Version", "13"),
            ("Authorization", "Bearer " + token),
        ]
        target = "/ws"
        if case == "wrong-token":
            headers[-1] = ("Authorization", "Bearer wrong")
        elif case == "duplicate-auth":
            headers.append(headers[-1])
        elif case == "conflict":
            headers.extend([("Origin", origin), ("Cookie", "console_token=wrong")])
        elif case in {"duplicate-cookie", "cookie-no-origin", "ambiguous-cookie"}:
            headers.pop()
            headers.append(("Cookie", "console_token=" + token))
            if case != "cookie-no-origin":
                headers.append(("Origin", origin))
            if case == "duplicate-cookie":
                headers.append(("Cookie", "console_token=" + token))
            if case == "ambiguous-cookie":
                headers[-2] = (
                    "Cookie",
                    "console_token=" + token + "; console_token=wrong",
                )
        elif case in {"foreign-origin", "null-origin", "duplicate-origin"}:
            headers.append(
                (
                    "Origin",
                    {
                        "foreign-origin": "https://example.invalid",
                        "null-origin": "null",
                        "duplicate-origin": origin,
                    }[case],
                )
            )
            if case == "duplicate-origin":
                headers.append(("Origin", origin))
        elif case == "foreign-host":
            headers[0] = ("Host", "example.invalid")
        elif case == "wrong-port":
            headers[0] = ("Host", "127.0.0.1:0")
        elif case == "duplicate-host":
            headers.append(("Host", host))
        elif case == "missing-host":
            headers.pop(0)
        elif case == "query-token":
            headers.pop()
            target += "?token=" + token
        elif case == "body-upgrade":
            headers.append(("Content-Length", "1"))
        elif case == "chunked-upgrade":
            headers.append(("Transfer-Encoding", "chunked"))
        elif case == "bad-key":
            headers[3] = ("Sec-WebSocket-Key", "invalid")
        elif case == "bad-version":
            headers[4] = ("Sec-WebSocket-Version", "12")
        request = (
            f"GET {target} HTTP/1.1\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in headers)
            + "\r\n"
        ).encode()
        try:
            answer = await _raw(server.port, request)
            assert answer.startswith(f"HTTP/1.1 {status} ".encode()), case
            assert b"101 Switching" not in answer
            assert token.encode() not in answer
        finally:
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method,body,chunked",
    [
        ("POST", b"", False),
        ("POST", b"x", False),
        ("PUT", b"x" * 262144, False),
        ("DELETE", b"chunked body", True),
        ("PATCH", b"x", False),
        ("OPTIONS", b"x", False),
        ("TRACE", b"x", False),
        ("CONNECT", b"", False),
    ],
    ids=[
        "post-empty",
        "post-body",
        "put-large",
        "delete-chunked",
        "patch",
        "options",
        "trace",
        "connect",
    ],
)
def test_page_methods_return_405_with_bodies(
    project: Path, method: str, body: bytes, chunked: bool
) -> None:
    import http.client

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()

        def request() -> tuple[int, str | None, bytes]:
            client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
            try:
                client.request(
                    method,
                    "/",
                    body=iter([body]) if chunked else body,
                    encode_chunked=chunked,
                )
                response = client.getresponse()
                return response.status, response.getheader("Allow"), response.read()
            finally:
                client.close()

        try:
            status, allow, payload = await asyncio.to_thread(request)
            assert (status, allow) == (405, "GET, HEAD")
            assert payload
        finally:
            await server.close()

    asyncio.run(scenario())


def test_expect_body_is_not_required_before_final_405(project: Path) -> None:
    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            try:
                writer.write(
                    f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nContent-Length: 999999999\r\nExpect: 100-continue\r\n\r\n".encode()
                )
                await writer.drain()
                answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 0.3)
                assert answer.startswith(b"HTTP/1.1 405 ")
                assert b"Allow: GET, HEAD\r\n" in answer
                assert b"100 Continue" not in answer
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await server.close()

    asyncio.run(scenario())


def test_get_head_headers_and_missing_page(project: Path) -> None:
    import http.client

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()

        def request(method: str, path: str) -> tuple[int, dict[str, str], bytes]:
            client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
            try:
                client.request(method, path)
                response = client.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                client.close()

        try:
            get = await asyncio.to_thread(request, "GET", "/")
            head = await asyncio.to_thread(request, "HEAD", "/")
            assert get[0] == head[0] == 200
            assert get[1] == head[1]
            assert get[2] and head[2] == b""
            headers = get[1]
            assert headers["Cache-Control"] == "no-store"
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert headers["X-Frame-Options"] == "DENY"
            assert "default-src 'none'" in headers["Content-Security-Policy"]
            missing = await asyncio.to_thread(request, "HEAD", "/unknown")
            assert missing[0] == 404 and missing[2] == b""
        finally:
            await server.close()

    asyncio.run(scenario())


def test_fragmented_upgrade_and_coalesced_first_frame_are_consumed_once(
    project: Path,
) -> None:
    from harness.console.discovery import discovery_path
    from websockets.client import ClientProtocol
    from websockets.frames import Frame, Opcode
    from websockets.uri import parse_uri

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        protocol = ClientProtocol(parse_uri(f"ws://127.0.0.1:{server.port}/ws"))
        request = protocol.connect()
        request.headers["Authorization"] = (
            "Bearer " + json.loads(discovery_path(project).read_text())["token"]
        )
        protocol.send_request(request)
        envelope = b"".join(protocol.data_to_send())
        ping = Frame(Opcode.PING, b"first").serialize(mask=True)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        try:
            writer.write(envelope[:11])
            await writer.drain()
            # Separate writes with an event-loop turn; first frame shares the final header write.
            await asyncio.sleep(0)
            writer.write(envelope[11:] + ping)
            await writer.drain()
            pongs: list[bytes] = []
            async with asyncio.timeout(2):
                while not pongs:
                    protocol.receive_data(await reader.read(16384))
                    pongs.extend(
                        bytes(event.data)
                        for event in protocol.events_received()
                        if isinstance(event, Frame) and event.opcode == Opcode.PONG
                    )
            protocol.send_ping(b"second")
            for data in protocol.data_to_send():
                writer.write(data)
            await writer.drain()
            async with asyncio.timeout(2):
                while b"second" not in pongs:
                    protocol.receive_data(await reader.read(16384))
                    pongs.extend(
                        bytes(event.data)
                        for event in protocol.events_received()
                        if isinstance(event, Frame) and event.opcode == Opcode.PONG
                    )
            assert pongs == [b"first", b"second"]
            protocol.send_close(1000)
            for data in protocol.data_to_send():
                writer.write(data)
            await writer.drain()
            await asyncio.wait_for(reader.read(), 2)
        finally:
            writer.close()
            await writer.wait_closed()
            await server.close()

    asyncio.run(scenario())


def test_header_limits_malformed_framing_and_deadline(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console import server as transport

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        base = f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n".encode()
        try:
            for framing, status in (
                (b"Content-Length: 1\r\nContent-Length: 2", 400),
                (b"Content-Length: invalid", 400),
                (b"Transfer-Encoding: invalid", 501),
            ):
                assert (
                    await _raw(server.port, base + framing + b"\r\n\r\n")
                ).startswith(f"HTTP/1.1 {status} ".encode())
            assert (
                await _raw(
                    server.port, base + b"X-Large: " + b"x" * 17000 + b"\r\n\r\n"
                )
            ).startswith(b"HTTP/1.1 431 ")
            with monkeypatch.context() as patch:
                patch.setattr(transport, "HEADER_TIMEOUT", 0.03)
                assert (await _raw(server.port, b"GET / HTTP/1.1\r\n")).startswith(
                    b"HTTP/1.1 408 "
                )
        finally:
            await server.close()

    asyncio.run(scenario())


def test_debug_logging_never_contains_credentials(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from harness.console.discovery import discovery_path
    import logging

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        token = json.loads(discovery_path(project).read_text())["token"]
        caplog.set_level(logging.DEBUG)
        try:
            headers = f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: invalid\r\nAuthorization: Bearer {token}\r\n\r\n".encode()
            assert (await _raw(server.port, headers)).startswith(b"HTTP/1.1 400 ")
            assert token not in caplog.text
            assert "Authorization:" not in caplog.text and "Cookie:" not in caplog.text
        finally:
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "masked,opcode,code", [(False, "PING", 1002), (True, "BINARY", 1003)]
)
def test_frame_errors_and_unanswered_close_have_bounded_cleanup(
    project: Path, masked: bool, opcode: str, code: int
) -> None:
    from harness.console.discovery import discovery_path
    from websockets.client import ClientProtocol
    from websockets.frames import Close, Frame, Opcode
    from websockets.uri import parse_uri

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        protocol = ClientProtocol(parse_uri(f"ws://127.0.0.1:{server.port}/ws"))
        request = protocol.connect()
        request.headers["Authorization"] = (
            "Bearer " + json.loads(discovery_path(project).read_text())["token"]
        )
        protocol.send_request(request)
        envelope = b"".join(protocol.data_to_send())
        try:
            # The peer never answers Close: the server must release the connection itself.
            answer = await _raw(
                server.port,
                envelope + Frame(Opcode[opcode], b"data").serialize(mask=masked),
            )
            assert b"HTTP/1.1 101 " in answer
            assert b"HTTP/1.1 408 " not in answer
            protocol.receive_data(answer)
            closes = [
                event
                for event in protocol.events_received()
                if isinstance(event, Frame) and event.opcode == Opcode.CLOSE
            ]
            assert len(closes) == 1 and Close.parse(closes[0].data).code == code
        finally:
            await server.close()

    asyncio.run(scenario())


def test_idle_server_exits_after_grace_and_removes_discovery(project: Path) -> None:
    from harness.console.discovery import discovery_path

    async def scenario() -> None:
        server = ConsoleServer(project, idle_grace_seconds=0.2)
        await server.start()
        assert discovery_path(project).exists()
        await asyncio.wait_for(server.wait_closed(), 3)
        assert not discovery_path(project).exists()

    asyncio.run(scenario())


def test_active_job_keeps_server_alive_until_it_finishes(project: Path) -> None:
    async def scenario() -> None:
        jobs = 1
        server = ConsoleServer(
            project, idle_grace_seconds=0.2, active_jobs=lambda: jobs
        )
        await server.start()
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(server.wait_closed()), 0.8)
            jobs = 0
            await asyncio.wait_for(server.wait_closed(), 3)
        finally:
            await server.close()

    asyncio.run(scenario())


def test_connected_client_keeps_server_alive_until_it_disconnects(
    project: Path,
) -> None:
    from harness.console.discovery import discovery_path
    from websockets.asyncio.client import connect

    async def scenario() -> None:
        server = ConsoleServer(project, idle_grace_seconds=0.2)
        await server.start()
        record = json.loads(discovery_path(project).read_text())
        try:
            async with connect(
                f"ws://127.0.0.1:{server.port}/ws",
                proxy=None,
                additional_headers={"Authorization": "Bearer " + record["token"]},
                open_timeout=2,
                close_timeout=1,
            ):
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(server.wait_closed()), 0.8)
            await asyncio.wait_for(server.wait_closed(), 3)
        finally:
            await server.close()

    asyncio.run(scenario())


DATA = DashboardData(
    ok=1,
    warn=0,
    fail=0,
    skipped=0,
    repo_map_tier="не установлен",
    harness_version="1.0.0",
    drift_state="clean",
    active_batches=None,
)


class GatedCollect:
    """A collect seam that waits on a gate, so a request can stay unfinished on purpose."""

    def __init__(self, *, open_gate: bool = False) -> None:
        self.gate = threading.Event()
        if open_gate:
            self.gate.set()
        self.calls = 0

    def __call__(self, repo: Path) -> HomeSnapshot:
        self.calls += 1
        assert self.gate.wait(10)
        return build_home_snapshot(DATA, HomeContext())


def _connect(server: ConsoleServer, repo: Path):  # type: ignore[no-untyped-def]
    from harness.console.discovery import discovery_path
    from websockets.asyncio.client import connect

    token = json.loads(discovery_path(repo).read_text())["token"]
    return connect(
        f"ws://127.0.0.1:{server.port}/ws",
        proxy=None,
        additional_headers={"Authorization": "Bearer " + token},
        open_timeout=2,
        close_timeout=1,
    )


def _request(id: object = 1, **fields: object) -> str:
    return json.dumps({"id": id, "type": "home.snapshot", **fields})


def test_home_snapshot_is_answered_on_the_websocket(project: Path) -> None:
    async def scenario() -> None:
        collect = GatedCollect(open_gate=True)
        server = ConsoleServer(
            project, snapshots=SnapshotService(project, collect=collect)
        )
        await server.start()
        try:
            async with _connect(server, project) as client:
                await client.send(_request("a"))
                reply = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert (reply["id"], reply["ok"], reply["cached"]) == ("a", True, False)
                assert reply["snapshot"]["harness_version"] == "1.0.0"
                await client.send(_request("b"))
                reply = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert (reply["id"], reply["cached"], collect.calls) == ("b", True, 1)
        finally:
            await server.close()

    asyncio.run(scenario())


def test_a_server_without_an_injected_service_owns_its_own_cache(
    project: Path,
) -> None:
    first, second = ConsoleServer(project), ConsoleServer(project)
    assert first._snapshots is not second._snapshots


def test_text_that_is_not_json_is_answered_and_the_connection_stays_open(
    project: Path,
) -> None:
    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        try:
            async with _connect(server, project) as client:
                await client.send("data")
                reply = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert reply["ok"] is False and reply["error"]["code"] == "bad_json"
                assert reply["id"] is None and reply["type"] is None
                await asyncio.wait_for(await client.ping(b"alive"), 2)
        finally:
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "frames,code",
    [
        ([("TEXT", False, b"da"), ("CONT", True, b"ta")], 1003),
        ([("TEXT", True, b"\xff\xfe")], 1007),
    ],
    ids=["fragmented-text", "invalid-utf8"],
)
def test_fragmented_and_malformed_text_frames_close_the_connection(
    project: Path, frames: list[tuple[str, bool, bytes]], code: int
) -> None:
    from harness.console.discovery import discovery_path
    from websockets.client import ClientProtocol
    from websockets.frames import Close, Frame, Opcode
    from websockets.uri import parse_uri

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        protocol = ClientProtocol(parse_uri(f"ws://127.0.0.1:{server.port}/ws"))
        request = protocol.connect()
        request.headers["Authorization"] = (
            "Bearer " + json.loads(discovery_path(project).read_text())["token"]
        )
        protocol.send_request(request)
        envelope = b"".join(protocol.data_to_send())
        body = b"".join(
            Frame(Opcode[name], data, fin=fin).serialize(mask=True)
            for name, fin, data in frames
        )
        try:
            answer = await _raw(server.port, envelope + body)
            protocol.receive_data(answer)
            closes = [
                event
                for event in protocol.events_received()
                if isinstance(event, Frame) and event.opcode == Opcode.CLOSE
            ]
            assert len(closes) == 1 and Close.parse(closes[0].data).code == code
            assert b'"ok"' not in answer
        finally:
            await server.close()

    asyncio.run(scenario())


def test_ping_is_answered_while_a_slow_check_is_still_running(project: Path) -> None:
    async def scenario() -> None:
        collect = GatedCollect()
        server = ConsoleServer(
            project, snapshots=SnapshotService(project, collect=collect)
        )
        await server.start()
        try:
            async with _connect(server, project) as client:
                await client.send(_request())
                await asyncio.wait_for(await client.ping(b"alive"), 2)
                assert not collect.gate.is_set()
                collect.gate.set()
                reply = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert reply["ok"] is True
        finally:
            collect.gate.set()
            await server.close()

    asyncio.run(scenario())


def test_the_fifth_unfinished_request_gets_busy(project: Path) -> None:
    async def scenario() -> None:
        collect = GatedCollect()
        server = ConsoleServer(
            project, snapshots=SnapshotService(project, collect=collect)
        )
        await server.start()
        try:
            async with _connect(server, project) as client:
                for id in range(1, 6):
                    await client.send(_request(id))
                refused = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert refused["id"] == 5 and refused["ok"] is False
                assert refused["error"]["code"] == "busy"
                collect.gate.set()
                answered = [
                    json.loads(await asyncio.wait_for(client.recv(), 5))
                    for _ in range(4)
                ]
                assert all(reply["ok"] for reply in answered)
                assert {reply["id"] for reply in answered} == {1, 2, 3, 4}
                # Finished requests free their slots.
                await client.send(_request(6))
                assert json.loads(await asyncio.wait_for(client.recv(), 5))["ok"]
        finally:
            collect.gate.set()
            await server.close()

    asyncio.run(scenario())


def test_a_lone_surrogate_id_is_echoed_and_the_connection_stays_open(
    project: Path,
) -> None:
    """json.loads accepts the id "\\ud800", which UTF-8 cannot encode: it is echoed escaped."""

    async def scenario() -> None:
        collect = GatedCollect()
        server = ConsoleServer(
            project, snapshots=SnapshotService(project, collect=collect)
        )
        await server.start()
        try:
            async with _connect(server, project) as client:
                for id in (1, 2, 3):
                    await client.send(_request(id))
                await client.send(_request("\ud800"))
                await client.send(_request("\ud800"))  # the fifth: busy
                refused = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert (refused["id"], refused["error"]["code"]) == ("\ud800", "busy")
                await asyncio.wait_for(await client.ping(b"alive"), 2)
                collect.gate.set()
                answered = [
                    json.loads(await asyncio.wait_for(client.recv(), 5))
                    for _ in range(4)
                ]
                assert all(reply["ok"] for reply in answered)
                assert {reply["id"] for reply in answered} == {1, 2, 3, "\ud800"}
                await client.send(_request("\ud800"))
                reply = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert (reply["id"], reply["ok"]) == ("\ud800", True)
        finally:
            collect.gate.set()
            await server.close()

    asyncio.run(scenario())


def test_a_disconnect_with_an_unfinished_request_releases_the_connection(
    project: Path,
) -> None:
    async def scenario() -> None:
        collect = GatedCollect()
        server = ConsoleServer(
            project, snapshots=SnapshotService(project, collect=collect)
        )
        await server.start()
        try:
            async with _connect(server, project) as client:
                await client.send(_request())
                await asyncio.sleep(0.1)
                assert collect.calls == 1
            await asyncio.wait_for(server.close(), 3)
            assert not server._connections
        finally:
            collect.gate.set()
            await server.close()

    asyncio.run(scenario())


async def _home(
    project: Path, service: SnapshotService | None = None, **fields: object
) -> dict[str, object]:
    """One `home.snapshot` request to a real server over a real WebSocket."""
    server = ConsoleServer(project, snapshots=service)
    await server.start()
    try:
        async with _connect(server, project) as client:
            await client.send(_request("n", **fields))
            reply: dict[str, object] = json.loads(
                await asyncio.wait_for(client.recv(), 120)
            )
            return reply
    finally:
        await server.close()


def test_a_project_without_harness_is_reported_as_not_installed(
    project: Path,
) -> None:
    reply = asyncio.run(_home(project))
    snapshot = reply["snapshot"]
    assert isinstance(snapshot, dict)
    assert (reply["ok"], reply["cached"], reply["id"]) == (True, False, "n")
    assert snapshot["drift"] == "missing"
    assert snapshot["active_batches"] is None
    assert snapshot["repo_map"]["tier"] is None
    assert snapshot["repo_map"]["head_map"] is None
    policy = snapshot["memory"]["policy"]
    assert (policy["enabled"], policy["top_k"], policy["max_tokens"]) == (
        False,
        5,
        1000,
    )
    expected = collect_dashboard(project)
    health = snapshot["health"]
    assert health == {
        "ok": expected.ok,
        "warn": expected.warn,
        "fail": expected.fail,
        "skipped": expected.skipped,
    }
    assert sum(health.values()) > 0


def test_a_failing_health_check_is_listed_among_the_problems(project: Path) -> None:
    (project / ".harness").mkdir()
    (project / ".harness" / "harness.lock").write_text("not json")
    snapshot = asyncio.run(_home(project))["snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["health"]["fail"] >= 1
    assert any(
        problem["status"] == "fail" and problem["id"] == "files.lock"
        for problem in snapshot["problems"]
    )
    assert snapshot["problems_total"] >= len(snapshot["problems"]) >= 1
    assert isinstance(snapshot["drift"], str)


def test_orchestration_that_is_not_connected_is_null_not_zero(project: Path) -> None:
    (project / ".harness").mkdir()
    snapshot = asyncio.run(_home(project))["snapshot"]
    assert isinstance(snapshot, dict) and snapshot["active_batches"] is None


def test_connected_orchestration_reports_its_open_batches(project: Path) -> None:
    coordinator = project / ".harness" / "orchestration" / "coordinator.py"
    coordinator.parent.mkdir(parents=True)
    coordinator.write_text('import json\nprint(json.dumps({"batches": [{}, {}]}))\n')
    snapshot = asyncio.run(_home(project))["snapshot"]
    assert isinstance(snapshot, dict) and snapshot["active_batches"] == 2


def test_a_failed_collect_is_an_error_reply_and_the_connection_survives(
    project: Path,
) -> None:
    calls = 0

    def flaky(repo: Path) -> HomeSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("/private/path must not leak")
        return build_home_snapshot(DATA, HomeContext())

    async def scenario() -> None:
        server = ConsoleServer(
            project, snapshots=SnapshotService(project, collect=flaky)
        )
        await server.start()
        try:
            async with _connect(server, project) as client:
                await client.send(_request("one"))
                raw = await asyncio.wait_for(client.recv(), 5)
                assert "private" not in str(raw)
                error = json.loads(raw)["error"]
                assert error == {
                    "code": "collect_failed",
                    "message": "OSError",
                }
                await client.send(_request("two"))
                reply = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert (reply["id"], reply["ok"]) == ("two", True)
        finally:
            await server.close()

    asyncio.run(scenario())


def test_expensive_checks_are_not_rerun_within_the_ttl_and_force_rebuilds(
    project: Path,
) -> None:
    runs = 0

    def dashboard(repo: Path) -> DashboardData:
        nonlocal runs
        runs += 1
        return DATA

    clock = FakeClock()
    service = SnapshotService(
        project,
        clock=clock,
        collect=functools.partial(
            collect_home, dashboard=dashboard, context=lambda repo: HomeContext()
        ),
    )

    async def scenario() -> list[dict[str, object]]:
        server = ConsoleServer(project, snapshots=service)
        await server.start()
        replies: list[dict[str, object]] = []
        try:
            async with _connect(server, project) as client:
                for step in ("first", "within-ttl", "after-ttl", "forced"):
                    if step == "after-ttl":
                        clock.advance(HOME_TTL_SECONDS)
                    await client.send(_request(step, force=step == "forced"))
                    replies.append(json.loads(await asyncio.wait_for(client.recv(), 5)))
                    if step == "first":
                        clock.advance(HOME_TTL_SECONDS / 3)
        finally:
            await server.close()
        return replies

    first, within, after, forced = asyncio.run(scenario())
    assert (first["cached"], within["cached"]) == (False, True)
    assert within["age_seconds"] == 5.0
    assert (after["cached"], after["age_seconds"]) == (False, 0.0)
    assert (forced["cached"], forced["age_seconds"]) == (False, 0.0)
    assert runs == 3
