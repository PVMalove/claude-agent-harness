"""Egress proxy of the docker executor's sidecar: HTTP(S) to the pinned plan hosts only (issue #706).

The sidecar sits on the default bridge and on the worker's ``--internal`` network, so it is the
worker's only route out. It speaks plain HTTP forwarding and ``CONNECT`` and nothing else: ssh,
raw TCP and any other protocol are not proxied. Standard library only, because it ships as a
drift-checked payload and its image is built by the operator, never by the harness.

Rules, applied to every request before any upstream connection:

* the target host must equal one entry of ``HARNESS_EGRESS_ALLOWED_HOSTS`` (exact match, no
  wildcards or subdomains), and an IP-literal target is refused even when it is listed;
* the target port must be 80 or 443;
* the host is resolved once, and a host with any loopback, link-local, unspecified or multicast
  answer is refused as a whole; the connection then uses the checked address, never a second lookup.

Everything else gets ``403``. The resolver, the address policy and the port set are injectable so a
test can reach a local server without weakening the defaults.
"""

from __future__ import annotations

import ipaddress
import os
import select
import signal
import socket
import socketserver
import sys
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from types import FrameType
from urllib.parse import urlsplit

ALLOWED_HOSTS_ENV = "HARNESS_EGRESS_ALLOWED_HOSTS"
LISTEN_PORT = 3128
DEFAULT_PORTS = frozenset({80, 443})
CONNECT_TIMEOUT_SECONDS = 10
# A tunnel or a response that stays silent this long is closed.
IDLE_TIMEOUT_SECONDS = 120
MAX_HEAD_BYTES = 64 * 1024
_CHUNK = 64 * 1024
# Headers that describe the client-to-proxy hop: they never reach the upstream.
_HOP_HEADERS = frozenset(
    {"connection", "proxy-connection", "proxy-authorization", "keep-alive"}
)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Sequence[str]]
AddressPolicy = Callable[[IPAddress], bool]
Log = Callable[[str], None]


class Refused(Exception):
    """The request breaks the allowlist policy: ``403``."""


class Unreachable(Exception):
    """The allowed target cannot be resolved or reached: ``502``."""


class BadRequest(Exception):
    """The request head is not a well-formed proxy request: ``400``."""


def resolve(host: str, port: int) -> list[str]:
    """The distinct addresses ``host`` resolves to, in answer order."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def default_address_allowed(address: IPAddress) -> bool:
    """Refuse loopback, link-local (cloud metadata), unspecified and multicast addresses."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    )


def normalize_host(host: str) -> str:
    """The comparison form of a host name: lower case without one trailing dot."""
    return host.strip().lower().removesuffix(".")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


class EgressPolicy:
    """Decides and opens the one upstream connection a request may make."""

    def __init__(
        self,
        hosts: Iterable[str],
        *,
        ports: Collection[int] = DEFAULT_PORTS,
        resolver: Resolver = resolve,
        address_allowed: AddressPolicy = default_address_allowed,
    ) -> None:
        self.hosts = frozenset(normalize_host(host) for host in hosts if host.strip())
        self.ports = frozenset(ports)
        self._resolver = resolver
        self._address_allowed = address_allowed

    def _usable(self, address: str) -> bool:
        try:
            return self._address_allowed(ipaddress.ip_address(address))
        except ValueError:
            return False

    def connect(self, host: str, port: int) -> socket.socket:
        """Open the upstream connection, or raise ``Refused`` or ``Unreachable``."""
        host = normalize_host(host)
        if _is_ip_literal(host):
            raise Refused("an IP-literal target is not allowed")
        if host not in self.hosts:
            raise Refused(f"host {host} is not on the allowlist")
        if port not in self.ports:
            raise Refused(f"port {port} is not allowed")
        try:
            addresses = list(self._resolver(host, port))
        except OSError as exc:
            raise Unreachable(f"cannot resolve {host}: {exc}") from exc
        if not addresses:
            raise Unreachable(f"cannot resolve {host}: no address")
        if not all(self._usable(address) for address in addresses):
            raise Refused(f"host {host} resolves to a refused address")
        failure: OSError | None = None
        for address in addresses:
            try:
                return socket.create_connection(
                    (address, port), timeout=CONNECT_TIMEOUT_SECONDS
                )
            except OSError as exc:
                failure = exc
        raise Unreachable(f"cannot connect to {host}:{port}: {failure}")


def _stderr_log(message: str) -> None:
    print(f"egress-proxy: {message}", file=sys.stderr, flush=True)


class EgressServer(socketserver.ThreadingTCPServer):
    """One thread per connection; each connection carries exactly one request."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        policy: EgressPolicy,
        log: Log = _stderr_log,
    ) -> None:
        super().__init__(address, _Handler)
        self.policy = policy
        self.log = log


def _read_head(client: socket.socket) -> tuple[bytes, bytes]:
    """The request head and the bytes already read behind it."""
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        if len(buffer) > MAX_HEAD_BYTES:
            raise BadRequest("request head is too large")
        chunk = client.recv(_CHUNK)
        if not chunk:
            raise BadRequest("connection closed inside the request head")
        buffer += chunk
    head, _, rest = buffer.partition(b"\r\n\r\n")
    return head, rest


def _parse_authority(target: str) -> tuple[str, int]:
    """``host:port`` of a CONNECT target; the port is mandatory."""
    host, separator, port = target.rpartition(":")
    if not separator or not host or not port.isdecimal():
        raise BadRequest("CONNECT needs host:port")
    return host.strip("[]"), int(port)


def _rewrite_http(
    method: str, target: str, version: str, headers: list[tuple[str, str]]
) -> tuple[str, int, bytes]:
    """The upstream host, its port and the origin-form request head for an absolute-URI request."""
    parts = urlsplit(target)
    if parts.scheme.lower() != "http" or not parts.hostname:
        raise Refused("only absolute http:// requests and CONNECT are proxied")
    if parts.username is not None or parts.password is not None:
        raise Refused("credentials in the target are not allowed")
    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise BadRequest("invalid port") from exc
    port = explicit_port or 80
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    authority = parts.hostname + (f":{explicit_port}" if explicit_port else "")
    lines = [f"{method} {path} {version}", f"Host: {authority}"]
    lines += [
        f"{name}: {value}"
        for name, value in headers
        if name.lower() not in _HOP_HEADERS and name.lower() != "host"
    ]
    lines.append("Connection: close")
    return parts.hostname, port, ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _relay(first: socket.socket, second: socket.socket) -> None:
    """Copy bytes both ways until both sides finish or the pair goes idle."""
    peers = {first: second, second: first}
    while peers:
        readable, _, _ = select.select(list(peers), [], [], IDLE_TIMEOUT_SECONDS)
        if not readable:
            return
        for source in readable:
            data = source.recv(_CHUNK)
            if data:
                peers[source].sendall(data)
                continue
            target = peers.pop(source)
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass


class _Handler(socketserver.BaseRequestHandler):
    server: EgressServer

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(IDLE_TIMEOUT_SECONDS)
        try:
            self._serve(client)
        except OSError:
            # A peer that vanished mid-request has nobody left to answer.
            pass

    def _serve(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        subject = "-"
        try:
            head, rest = _read_head(client)
            lines = head.decode("latin-1").split("\r\n")
            request = lines[0].split(" ")
            if len(request) != 3 or not request[2].startswith("HTTP/1."):
                raise BadRequest("malformed request line")
            method, target, version = request
            headers = []
            for line in lines[1:]:
                name, separator, value = line.partition(":")
                if not separator or not name or name != name.strip():
                    raise BadRequest("malformed header")
                headers.append((name, value.strip()))
            if method.upper() == "CONNECT":
                host, port = _parse_authority(target)
                subject = f"CONNECT {host}:{port}"
                upstream = self.server.policy.connect(host, port)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                forwarded = rest
            else:
                host, port, forwarded = _rewrite_http(method, target, version, headers)
                subject = f"{method.upper()} {host}:{port}"
                upstream = self.server.policy.connect(host, port)
                forwarded += rest
            self.server.log(f"allow {subject}")
            if forwarded:
                upstream.sendall(forwarded)
            _relay(client, upstream)
        except BadRequest as exc:
            self.server.log(f"bad request: {exc}")
            self._reply(client, 400, "Bad Request")
        except Refused as exc:
            self.server.log(f"deny {subject}: {exc}")
            self._reply(client, 403, "Forbidden")
        except Unreachable as exc:
            self.server.log(f"unreachable {subject}: {exc}")
            self._reply(client, 502, "Bad Gateway")
        finally:
            if upstream is not None:
                upstream.close()

    @staticmethod
    def _reply(client: socket.socket, status: int, reason: str) -> None:
        client.sendall(
            f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n"
            "Connection: close\r\n\r\n".encode("ascii")
        )


def allowed_hosts_from_env(environ: Mapping[str, str]) -> list[str]:
    """The allowlist the sidecar was started with; an empty one is a launch error."""
    hosts = [
        host.strip()
        for host in environ.get(ALLOWED_HOSTS_ENV, "").split(",")
        if host.strip()
    ]
    if not hosts:
        raise SystemExit(f"{ALLOWED_HOSTS_ENV} lists no host; refusing to start")
    return hosts


def _stop(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(0)


def main() -> int:
    """Serve on every interface of the container: the internal network reaches it by name."""
    policy = EgressPolicy(allowed_hosts_from_env(os.environ))
    # PID 1 ignores SIGTERM without a handler, which would make ``docker stop`` wait for a kill.
    signal.signal(signal.SIGTERM, _stop)
    with EgressServer(("0.0.0.0", LISTEN_PORT), policy) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
