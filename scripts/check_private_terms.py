#!/usr/bin/env python3
"""Проверка публикуемого текста на приватные термины мейнтейнера (#438).

Список берётся из непустой переменной HARNESS_PRIVATE_TERMS или из gitignored файла
`.private-terms.txt` в корне основного checkout. Вывод называет только место и номер термина
(номер строки в источнике), но никогда сам термин или проверенную строку. Только stdlib: hook
запускает модуль системным Python. В целевые проекты не попадает: scripts/ не входит в архив.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

ENV_VAR = "HARNESS_PRIVATE_TERMS"
TERMS_FILE = ".private-terms.txt"
SKIP_NOTICE = (
    f"private-terms: skipped: no term list (set {ENV_VAR} or create {TERMS_FILE} "
    "in the main checkout; see docs/agents/releases.md)"
)
# Same patch shape for staged changes and commits: every added line with its new line number.
DIFF_OPTIONS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--unified=0",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

Finding = tuple[str, int]


class CheckError(Exception):
    """Ошибка проверки; сообщение не содержит ни терминов, ни проверяемого текста."""


class Terms(NamedTuple):
    """Активный список: имя источника и пары (номер строки в источнике, термин)."""

    source: str
    entries: tuple[tuple[int, str], ...]


def _git(repo: Path, *args: str) -> str:
    """Выполнить git и вернуть stdout как UTF-8; stderr git перехватывается и не печатается."""
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise CheckError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout.decode("utf-8", errors="replace")


def fold(text: str) -> str:
    """Нормализовать текст для сравнения без учёта регистра в латинице и кириллице.

    NFKC собирает разложенные и полноширинные символы, casefold снимает регистр, а ё сводится к е,
    потому что в русском тексте ё часто пишут как е. Применяется и к терминам, и к тексту.
    """
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def _main_checkout(repo: Path) -> Path | None:
    """Корень основного checkout, общий для всех linked worktree; вне git-репозитория — None."""
    try:
        common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except CheckError:
        return None
    return Path(common.strip()).parent


def load_terms(repo: Path | None, environ: Mapping[str, str]) -> Terms | None:
    """Загрузить список: непустая переменная окружения, иначе файл основного checkout.

    Пустая переменная считается незаданной: так GitHub подставляет отсутствующий секрет. Источники
    не объединяются, чтобы номер термина однозначно указывал строку. Без терминов — None.
    """
    text = environ.get(ENV_VAR, "")
    source = ENV_VAR
    checkout = None if text or repo is None else _main_checkout(repo)
    if checkout is not None and (checkout / TERMS_FILE).is_file():
        source = TERMS_FILE
        try:
            text = (checkout / TERMS_FILE).read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CheckError(f"{TERMS_FILE} is not valid UTF-8") from None
    entries = []
    for number, line in enumerate(text.split("\n"), 1):
        term = line.strip()
        if term and not term.startswith("#"):
            entries.append((number, fold(term)))
    return Terms(source, tuple(entries)) if entries else None


def _line_terms(line: str, terms: Terms) -> list[int]:
    """Номера терминов, которые встречаются в строке как подстрока после `fold`."""
    folded = fold(line)
    return [number for number, term in terms.entries if term in folded]


def _label(component: str, terms: Terms, ordinal: str) -> str:
    """Нечисловой компонент локации или его порядковая метка, если в нём есть термин."""
    return ordinal if _line_terms(component, terms) else component


def _text_findings(text: str, terms: Terms, location: str) -> list[Finding]:
    """Находки в строках текста с локацией `<location>:<номер строки>`."""
    findings = []
    for line_number, line in enumerate(text.split("\n"), 1):
        findings += [(f"{location}:{line_number}", n) for n in _line_terms(line, terms)]
    return findings


def _patch_findings(patch: str, terms: Terms, prefix: str, label: str) -> list[Finding]:
    """Находки в добавленных строках и именах неудалённых файлов патча с `--unified=0`.

    Путь берётся из `diff --git a/P b/P`: при `--no-renames` половины равны, а строки `+++` у
    пустого нового файла нет. Путь в кавычках или с термином печатается меткой `<label> #k`.
    """
    findings: list[Finding] = []
    for index, chunk in enumerate(("\n" + patch).split("\ndiff --git ")[1:], 1):
        header, *lines = chunk.split("\n")
        quoted = header.startswith('"')
        path = (
            header[3 : 3 + (len(header) - 9) // 2]
            if quoted
            else header[2 : 2 + (len(header) - 5) // 2]
        )
        ordinal = f"{label} #{index}"
        shown = ordinal if quoted else _label(f"{prefix}{path}", terms, ordinal)
        if not any(line.startswith("deleted file mode ") for line in lines):
            findings += [(f"{ordinal} (name)", n) for n in _line_terms(path, terms)]
        new_line, in_hunk = 0, False
        for line in lines:
            hunk = HUNK.match(line)
            if hunk:
                new_line, in_hunk = int(hunk.group(1)), True
            elif in_hunk and line.startswith("+"):
                location = f"{shown}:{new_line}"
                findings += [(location, n) for n in _line_terms(line[1:], terms)]
                new_line += 1
            elif in_hunk and line.startswith(" "):
                new_line += 1
    return findings


def _staged_findings(repo: Path, terms: Terms, revision: str) -> list[Finding]:
    """Находки в индексе (`--cached`) или в рабочем дереве относительно ревизии."""
    patch = _git(repo, "diff", *DIFF_OPTIONS, revision, "--")
    return _patch_findings(patch, terms, "", "staged file")


def _has_head(repo: Path) -> bool:
    """Есть ли у репозитория хотя бы один коммит."""
    try:
        _git(repo, "rev-parse", "--verify", "-q", "HEAD")
    except CheckError:
        return False
    return True


def _commit_findings(repo: Path, commit_range: str, terms: Terms) -> list[Finding]:
    """Находки в сообщениях и добавленных строках каждого коммита диапазона.

    Без диапазона проверяются непубликованные коммиты `HEAD --not --remotes`. Каждый коммит
    проверяется отдельно: термин, добавленный и удалённый внутри диапазона, остаётся в истории.
    """
    if commit_range:
        revisions: tuple[str, ...] = ("--end-of-options", commit_range)
    elif _has_head(repo):
        revisions = ("HEAD", "--not", "--remotes")
    else:
        return []
    output = _git(
        repo,
        "log",
        *DIFF_OPTIONS,
        "--no-show-signature",
        "-p",
        "--format=%x00%H%x00%B%x00",
        *revisions,
        "--",
    )
    fields = output.split("\0")
    findings = []
    for position, start in enumerate(range(1, len(fields) - 2, 3), 1):
        sha, message, patch = fields[start : start + 3]
        prefix = _label(f"commit {sha[:12]}", terms, f"commit #{position}")
        findings += _text_findings(message, terms, prefix)
        findings += _patch_findings(patch, terms, f"{prefix} ", f"{prefix} file")
    return findings


def _branch_findings(repo: Path, name: str, terms: Terms) -> list[Finding]:
    """Находки в имени ветки; без имени — текущая ветка, detached HEAD не проверяется."""
    branch = name or _git(repo, "branch", "--show-current").strip()
    return [("branch:1", n) for n in _line_terms(branch, terms)]


def _body_findings(path: str, ordinal: int, terms: Terms) -> list[Finding]:
    """Находки в файле тела issue, PR или комментария; путь с термином — метка `body file #k`."""
    shown = _label(path, terms, f"body file #{ordinal}")
    try:
        text = Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        raise CheckError(f"cannot read body file {shown}") from None
    return _text_findings(text, terms, shown)


def skip_notice(environ: Mapping[str, str]) -> str:
    """Явное уведомление о пропуске; в GitHub Actions — аннотация workflow."""
    if environ.get("GITHUB_ACTIONS") == "true":
        return f"::notice title=private-terms::{SKIP_NOTICE}"
    return SKIP_NOTICE


def _report(findings: list[Finding], terms: Terms) -> int:
    """Напечатать итог: только локации и номера терминов, никогда сами термины."""
    if not findings:
        print(f"private-terms: no matches ({terms.source}, {len(terms.entries)} terms)")
        return 0
    print(
        f"private-terms: {len(findings)} match(es); term numbers are line numbers in "
        f"{terms.source}:",
        file=sys.stderr,
    )
    for location, number in findings:
        print(f"  {location}: term #{number}", file=sys.stderr)
    print(
        "private-terms: remove or generalize the matched text before publishing.",
        file=sys.stderr,
    )
    return 1


def _parser() -> argparse.ArgumentParser:
    """Аргументы CLI: источники складываются в одном запуске."""
    parser = argparse.ArgumentParser(
        description="Check text about to be published against the private term list."
    )
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--staged", action="store_true", help="check the staged diff")
    parser.add_argument(
        "--commits",
        nargs="?",
        const="",
        metavar="RANGE",
        help="check messages and added lines of each commit (default: HEAD --not --remotes)",
    )
    parser.add_argument(
        "--branch",
        nargs="?",
        const="",
        metavar="NAME",
        help="check a branch name (default: the current branch)",
    )
    parser.add_argument(
        "--body-file",
        action="append",
        default=[],
        metavar="PATH",
        help="check an issue, PR or comment body file (repeatable)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    """Точка входа CLI: 0 — совпадений нет, 1 — есть совпадения, 2 — ошибка."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            # A Cyrillic path must not crash the output on a cp1252 console.
            stream.reconfigure(errors="backslashreplace")
    parser = _parser()
    args = parser.parse_args(argv)
    if not (
        args.staged
        or args.commits is not None
        or args.branch is not None
        or args.body_file
    ):
        parser.error(
            "choose at least one of --staged, --commits, --branch, --body-file"
        )
    env = os.environ if environ is None else environ
    try:
        terms = load_terms(args.repo, env)
        if terms is None:
            print(skip_notice(env))
            return 0
        findings = []
        if args.staged:
            findings += _staged_findings(args.repo, terms, "--cached")
        if args.commits is not None:
            findings += _commit_findings(args.repo, args.commits, terms)
        if args.branch is not None:
            findings += _branch_findings(args.repo, args.branch, terms)
        for ordinal, path in enumerate(args.body_file, 1):
            findings += _body_findings(path, ordinal, terms)
    except CheckError as error:
        print(f"private-terms: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # an exception message may quote the checked text
        print(
            f"private-terms: internal error ({type(error).__name__})", file=sys.stderr
        )
        return 2
    return _report(findings, terms)


if __name__ == "__main__":
    raise SystemExit(main())
