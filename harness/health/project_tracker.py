"""Резолвер трекера проекта (docs/adr/0010): единственное место, где из `.harness/project.json` и URL
`origin` выводится тройка (тип, хост, проект) вместе с источником значения.

Явное корректное поле `tracker` побеждает; без него тройка выводится из `origin` с учётом схемы,
userinfo, порта, подгрупп и точки в имени репозитория. Модуль использует только stdlib и
относительные импорты: он поставляется в `.harness/health/` целевого проекта вместе с остальным
пакетом health и не читает пути `.claude/*`, поэтому одинаково работает в любом runtime. Сырой URL
`origin` никогда не возвращается и не попадает в сообщения: в userinfo могут быть учётные данные.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from .process import run_tool
from .project_files import TRACKER_HOSTED_TYPES, tracker_field_problems

TrackerType = Literal["github", "gitlab", "local"]
# config - the explicit tracker field; origin - parsed from the origin URL; default - there is
# neither a valid field nor an origin remote.
TrackerSource = Literal["config", "origin", "default"]
ProjectJsonState = Literal["absent", "no_field", "valid", "invalid"]

PROJECT_JSON_REL = Path(".harness/project.json")
_GIT_TIMEOUT_SECONDS = 10
_WEB_DEFAULT_PORTS = {"https": 443, "http": 80}
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git", "git"})
# Git's own SCP-like syntax, [user@]host:path. As in git, `host:2222/x` means the path `2222/x`.
_SCP_FORM = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.*)$")
_LOCAL_PATH = re.compile(r"^(?:/|\./|\.\./|~|[A-Za-z]:[\\/])")


@dataclass(frozen=True)
class ProjectTracker:
    """The project tracker triple plus where it came from."""

    type: TrackerType
    # Lower-case hostname plus an optional ":port", without scheme or userinfo.
    host: str | None
    # Full project path with subgroups, without surrounding "/" and without ".git".
    project: str | None
    source: TrackerSource

    def field(self) -> dict[str, str]:
        """This tracker as a `tracker` field value; an unknown host or project is left out."""
        value: dict[str, str] = {"type": self.type}
        if self.host is not None:
            value["host"] = self.host
        if self.project is not None:
            value["project"] = self.project
        return value

    def snippet(self) -> str:
        """A ready-to-paste `"tracker": {...}` entry for .harness/project.json."""
        return '"tracker": ' + json.dumps(self.field(), ensure_ascii=False)


@dataclass(frozen=True)
class RemoteLocation:
    """What a remote URL says about its web host and project - never the raw URL itself."""

    hostname: str
    # Only an explicit non-default http(s) port; always None for ssh/scp/git URLs.
    port: int | None
    # None when the path has fewer than two segments or an empty segment.
    project: str | None
    # True for http(s) only: an SSH port says nothing about the web host's port.
    web_port_known: bool

    @property
    def host(self) -> str:
        """hostname[:port]."""
        return self.hostname if self.port is None else f"{self.hostname}:{self.port}"


@dataclass(frozen=True)
class TrackerResolution:
    """The resolved project tracker together with what it was resolved from."""

    # The winner: the valid tracker field, otherwise from_origin.
    effective: ProjectTracker
    # The valid tracker field (source "config"), if any.
    declared: ProjectTracker | None
    # The classification of origin alone (source "origin", or "default" without an origin).
    from_origin: ProjectTracker
    origin: RemoteLocation | None
    project_json: ProjectJsonState
    # A subset of ("type", "host", "project") where the declared field and origin disagree.
    mismatches: tuple[str, ...]


def parse_remote_url(url: str) -> RemoteLocation | None:
    """Parse an https://, http://, ssh:// (git+ssh://, git://) or SCP-form remote URL; None for a
    local path, a file:// or other unsupported URL, or an unparsable one."""
    url = url.strip()
    if not url or _LOCAL_PATH.match(url):
        return None
    if "://" in url:
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        if not hostname:
            return None
        if scheme in _WEB_DEFAULT_PORTS:
            web_port_known = True
            if port == _WEB_DEFAULT_PORTS[scheme]:
                port = None
        elif scheme in _SSH_SCHEMES:
            web_port_known = False
            port = None
        else:
            return None
        path = parts.path
    else:
        match = _SCP_FORM.match(url)
        if match is None:
            return None
        hostname, port, web_port_known, path = match["host"], None, False, match["path"]
    return RemoteLocation(
        hostname=hostname.lower(),
        port=port,
        project=_project_path(path),
        web_port_known=web_port_known,
    )


def _project_path(path: str) -> str | None:
    """The full project path out of a URL path: no surrounding "/", one trailing ".git" removed,
    dots inside names kept, no URL decoding."""
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    segments = path.split("/")
    if len(segments) < 2 or not all(segments):
        return None
    return path


def _tracker_type(value: object) -> TrackerType | None:
    if value == "github":
        return "github"
    if value == "gitlab":
        return "gitlab"
    if value == "local":
        return "local"
    return None


def _classify(origin: RemoteLocation | None, *, has_origin: bool) -> ProjectTracker:
    """Classify one origin: github.com and its subdomains, a host with "gitlab." in its name, and
    anything else local - keeping its host and project for the snippet."""
    if origin is None:
        return ProjectTracker(
            "local", None, None, "origin" if has_origin else "default"
        )
    hostname = origin.hostname
    if hostname == "github.com" or hostname.endswith(".github.com"):
        return ProjectTracker("github", "github.com", origin.project, "origin")
    if "gitlab." in hostname:
        return ProjectTracker("gitlab", origin.host, origin.project, "origin")
    return ProjectTracker("local", origin.host, origin.project, "origin")


def _origin_url(repo: Path) -> str | None:
    """The origin URL from `git remote -v`; None without git, on a git error or timeout, or
    without an origin remote."""
    git = shutil.which("git")
    if git is None:
        return None
    result = run_tool([git, "remote", "-v"], timeout=_GIT_TIMEOUT_SECONDS, cwd=repo)
    if result is None or result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "origin":
            return parts[1]
    return None


def _declared_tracker(repo: Path) -> tuple[ProjectJsonState, ProjectTracker | None]:
    """The tracker field of .harness/project.json; only a valid field is returned. The validity of
    the other project.json fields does not matter here - files.project_json reports it."""
    path = repo / PROJECT_JSON_REL
    if not path.is_file():
        return "absent", None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "invalid", None
    if not isinstance(data, dict):
        return "invalid", None
    if "tracker" not in data:
        return "no_field", None
    value: object = data["tracker"]
    if tracker_field_problems(value) or not isinstance(value, dict):
        return "invalid", None
    tracker_type = _tracker_type(value.get("type"))
    if tracker_type is None:
        return "invalid", None
    host, project = value.get("host"), value.get("project")
    return "valid", ProjectTracker(
        tracker_type,
        host if isinstance(host, str) else None,
        project if isinstance(project, str) else None,
        "config",
    )


def _split_host(host: str) -> tuple[str, int | None]:
    """(casefolded hostname, port); the default web ports 443 and 80 count as no port."""
    hostname, _, port = host.partition(":")
    number = int(port) if port.isdigit() else None
    return hostname.casefold(), None if number in (443, 80) else number


def _mismatches(
    declared: ProjectTracker | None,
    from_origin: ProjectTracker,
    origin: RemoteLocation | None,
) -> tuple[str, ...]:
    """Where a hosted tracker field disagrees with a known origin. An origin classified local is
    unknown rather than contradicting, and a `local` field never disagrees: code on GitHub with a
    local markdown tracker is a legitimate configuration."""
    if declared is None or declared.type not in TRACKER_HOSTED_TYPES or origin is None:
        return ()
    found: list[str] = []
    if from_origin.type in TRACKER_HOSTED_TYPES and from_origin.type != declared.type:
        found.append("type")
    if declared.host is not None and from_origin.host is not None:
        declared_name, declared_port = _split_host(declared.host)
        origin_name, origin_port = _split_host(from_origin.host)
        if declared_name != origin_name or (
            origin.web_port_known and declared_port != origin_port
        ):
            found.append("host")
    if (
        declared.project is not None
        and from_origin.project is not None
        and declared.project.casefold() != from_origin.project.casefold()
    ):
        found.append("project")
    return tuple(found)


def resolve_project_tracker(repo: Path) -> TrackerResolution:
    """Resolve the project tracker of `repo`: a valid explicit tracker field wins, otherwise the
    classification of the origin URL is used."""
    state, declared = _declared_tracker(repo)
    url = _origin_url(repo)
    origin = parse_remote_url(url) if url else None
    from_origin = _classify(origin, has_origin=bool(url))
    return TrackerResolution(
        effective=declared if declared is not None else from_origin,
        declared=declared,
        from_origin=from_origin,
        origin=origin,
        project_json=state,
        mismatches=_mismatches(declared, from_origin, origin),
    )
