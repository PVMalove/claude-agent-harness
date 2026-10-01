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
import json
import os
import re
import shlex
import subprocess
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple, TextIO

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
# Hook: shell text is parsed only far enough to find publication commands and their files.
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
DYNAMIC = re.compile(r"[$`*?\[]")
SEPARATORS = frozenset(";&|()\n")
PUBLICATION = re.compile(
    r"\bgit\b[^\n;&|]*\b(?:commit|push)\b|\bgh\s+(?:issue|pr)\s+(?:create|edit|comment)\b"
)
GIT_VALUE_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
)
COMMIT_VALUE_OPTIONS = frozenset(
    {
        "--file",
        "--message",
        "--template",
        "--reuse-message",
        "--reedit-message",
        "--author",
        "--date",
        "--trailer",
        "--fixup",
        "--squash",
        "--cleanup",
    }
)

Finding = tuple[str, int]


class CheckError(Exception):
    """Ошибка проверки; сообщение не содержит ни терминов, ни проверяемого текста."""


class Terms(NamedTuple):
    """Активный список: имя источника и пары (номер строки в источнике, термин)."""

    source: str
    entries: tuple[tuple[int, str], ...]


class Step(NamedTuple):
    """Публикационный шаг команды Bash; `cwd` равен None, если shell-текст его не определяет."""

    kind: str  # "commit", "push" or "gh"
    cwd: Path | None
    files: tuple[str, ...] = ()
    all_tracked: bool = False


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


def _body_findings(
    path: str, ordinal: int, terms: Terms, cwd: Path | None = None
) -> list[Finding]:
    """Находки в файле тела issue, PR или комментария; путь с термином — метка `body file #k`.

    В hook (`cwd` задан) путь из текста команды разрешается от каталога шага и должен быть
    литеральным: результат подстановки shell проверка не видит.
    """
    shown = _label(path, terms, f"body file #{ordinal}")
    try:
        if cwd is not None and DYNAMIC.search(path):
            raise OSError(path)
        file = Path(path) if cwd is None else cwd / Path(path).expanduser()
        text = file.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        hint = "" if cwd is None else "; pass a readable literal path"
        raise CheckError(f"cannot read body file {shown}{hint}") from None
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


def _strip_heredocs(command: str) -> str:
    """Удалить тела heredoc до разбора: их текст проверяется как текст команды, а не как shell."""
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.split("\n"):
        if pending:
            word, tabs = pending[0]
            if (line.lstrip("\t") if tabs else line) == word:
                pending.pop(0)
            continue
        kept.append(line)
        pending = [(m.group(3), m.group(1) == "-") for m in HEREDOC.finditer(line)]
    return "\n".join(kept)


def _segments(command: str) -> list[list[str]]:
    """Слова простых команд: `;`, `&`, `|`, скобки и перевод строки разделяют команды."""
    lexer = shlex.shlex(
        _strip_heredocs(command).replace("\\\n", ""),
        posix=True,
        punctuation_chars=";&|()\n",
    )
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return segments


def _join(cwd: Path | None, value: str) -> Path | None:
    """Каталог после `cd` или `git -C`; None, если shell-текст его не определяет."""
    if value == "-" or DYNAMIC.search(value):
        return None
    target = Path(value).expanduser()
    if target.is_absolute():
        return target
    return None if cwd is None else cwd / target


def _commit_step(cwd: Path | None, args: list[str]) -> Step:
    """Шаг `git commit`: файлы сообщения из -F/--file и флаг -a/--all.

    Опция со значением забирает следующий токен, поэтому `-m '-Fancy'` не даёт файла.
    """
    files: list[str] = []
    all_tracked = False
    index = 0
    while index < len(args) and args[index] != "--":
        word = args[index]
        index += 1
        if word.startswith("--"):
            name, equals, value = word.partition("=")
            if name == "--all":
                all_tracked = True
            elif name in COMMIT_VALUE_OPTIONS:
                if not equals and index < len(args):
                    value = args[index]
                    index += 1
                if name == "--file" and value:
                    files.append(value)
        elif word.startswith("-"):
            for position, letter in enumerate(word[1:], 2):
                if letter == "a":
                    all_tracked = True
                elif letter in "mFCct":
                    value = word[position:]
                    if not value and index < len(args):
                        value = args[index]
                        index += 1
                    if letter == "F" and value:
                        files.append(value)
                    break
    return Step("commit", cwd, tuple(files), all_tracked)


def _git_step(cwd: Path | None, args: list[str]) -> list[Step]:
    """Шаг для `git [глобальные опции] commit|push`; `-C` накапливается, как в git."""
    foreign = False
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option = args[index]
        # --git-dir/--work-tree name another repository than the directory of the command.
        foreign = foreign or option.split("=", 1)[0] in ("--git-dir", "--work-tree")
        if option in GIT_VALUE_OPTIONS and index + 1 < len(args):
            index += 1
            if option == "-C":
                cwd = _join(cwd, args[index])
        index += 1
    cwd = None if foreign else cwd
    subcommand = args[index] if index < len(args) else ""
    if subcommand == "push":
        return [Step("push", cwd)]
    if subcommand == "commit":
        return [_commit_step(cwd, args[index + 1 :])]
    return []


def _gh_step(cwd: Path | None, args: list[str]) -> list[Step]:
    """Шаг для `gh issue|pr create|edit|comment` с файлами из точных `-F` и `--body-file`."""
    if (
        len(args) < 2
        or args[0] not in ("issue", "pr")
        or args[1] not in ("create", "edit", "comment")
    ):
        return []
    files = [
        value
        for option, value in zip(args, args[1:])
        if option in ("-F", "--body-file")
    ]
    files += [word.split("=", 1)[1] for word in args if word.startswith("--body-file=")]
    return [Step("gh", cwd, tuple(files))]


def analyse_command(command: str, cwd: Path) -> list[Step]:
    """Публикационные шаги команды Bash: git commit/push и gh issue|pr create|edit|comment.

    Упоминание в кавычках остаётся одним аргументом другой команды и шагом не считается; `cd`
    меняет каталог следующих команд. ValueError — текст не разбирается как shell.
    """
    steps: list[Step] = []
    current: Path | None = cwd
    for words in _segments(command):
        while words and ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            continue
        name = words[0].rsplit("/", 1)[-1].removesuffix(".exe")
        if name == "cd":
            current = _join(current, words[1]) if len(words) == 2 else None
        elif name == "git":
            steps += _git_step(current, words[1:])
        elif name == "gh":
            steps += _gh_step(current, words[1:])
    return steps


def _step_findings(command: str, step: Step, repo: Path, terms: Terms) -> list[Finding]:
    """Находки шага: текст команды и body-файлы; для commit — staged diff (при -a — diff HEAD)
    и ветка, для push — непубликованные коммиты и ветка."""
    findings = _text_findings(command, terms, "command")
    for ordinal, path in enumerate(step.files, 1):
        # `-` is stdin: a heredoc or a pipe is already part of the command text.
        if path != "-":
            findings += _body_findings(path, ordinal, terms, repo)
    if step.kind == "commit":
        revision = "HEAD" if step.all_tracked and _has_head(repo) else "--cached"
        findings += _staged_findings(repo, terms, revision)
    elif step.kind == "push":
        findings += _commit_findings(repo, "", terms)
    if step.kind != "gh":
        findings += _branch_findings(repo, "", terms)
    return findings


def _hook(payload_text: str, environ: Mapping[str, str]) -> int:
    """PreToolUse(Bash): 0 — разрешить, 2 — заблокировать публикационную команду.

    Не публикационная команда разрешается без вызова git и без чтения списка: hook срабатывает на
    каждый Bash. Публикационную команду, которую нельзя проверить, блокирует только при списке.
    """
    try:
        payload = json.loads(payload_text)
        command = payload["tool_input"]["command"]
    except (ValueError, KeyError, TypeError):
        command = None
    if not isinstance(command, str):
        raise CheckError("hook payload has no tool_input.command")
    cwd = Path(payload["cwd"]) if isinstance(payload.get("cwd"), str) else Path.cwd()
    try:
        steps = analyse_command(command, cwd)
    except ValueError:
        if not PUBLICATION.search(command):
            return 0
        if load_terms(cwd, environ) is None:
            print(skip_notice(environ), file=sys.stderr)
            return 0
        raise CheckError(
            "cannot parse this command; split it or pass the text through a readable body file"
        ) from None
    if not steps:
        return 0
    findings: list[Finding] = []
    active: Terms | None = None
    for step in steps:
        terms = load_terms(cwd if step.cwd is None else step.cwd, environ)
        if terms is None:
            continue
        if step.cwd is None:
            raise CheckError(
                "cannot resolve the repository of this command; use a literal path"
            )
        active = active or terms
        findings += _step_findings(command, step, step.cwd, terms)
    if active is None:
        print(skip_notice(environ), file=sys.stderr)
        return 0
    return 2 if _report(list(dict.fromkeys(findings)), active) else 0


def _check(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """CLI: проверить все источники, выбранные в одном запуске."""
    terms = load_terms(args.repo, environ)
    if terms is None:
        print(skip_notice(environ))
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
    return _report(findings, terms)


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
    parser.add_argument(
        "--hook",
        action="store_true",
        help="read a Claude Code PreToolUse(Bash) payload from stdin; exit 2 blocks",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Точка входа. CLI: 0 — совпадений нет, 1 — есть, 2 — ошибка; `--hook`: 0 или 2 (блок)."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            # A Cyrillic path must not crash the output on a cp1252 console.
            stream.reconfigure(errors="backslashreplace")
    parser = _parser()
    args = parser.parse_args(argv)
    sources = bool(
        args.staged
        or args.commits is not None
        or args.branch is not None
        or args.body_file
    )
    if args.hook == sources:
        parser.error(
            "use --hook alone or at least one of --staged, --commits, --branch, --body-file"
        )
    env = os.environ if environ is None else environ
    try:
        if args.hook:
            if stdin is None:
                return _hook(sys.stdin.buffer.read().decode("utf-8", "replace"), env)
            return _hook(stdin.read(), env)
        return _check(args, env)
    except CheckError as error:
        print(f"private-terms: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # an exception message may quote the checked text
        print(
            f"private-terms: internal error ({type(error).__name__})", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
