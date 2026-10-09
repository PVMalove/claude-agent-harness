#!/usr/bin/env python3
"""Zero Direct Commits: каждый `git commit`/`git push` проверяется в том checkout, где он выполнится.

`python direct_commits.py < payload.json` печатает `allow` и завершается с кодом 0, если ни один
вызов commit/push не идёт из защищённой ветки (`master`, `main`, `base_branch`, `integration/*`)
и ни один push не обновляет защищённый реф. Иначе причина выводится в stderr, код — 2.
Исключения для /to-spec: push, который только создаёт отсутствующие на remote `integration/*`,
и отдельный коммит документов прожарки в ещё не опубликованную `integration/*`.

Вызовы находит разбор `pr_commands.parse`: упоминание commit/push в аргументах печатающих
команд, в кавычках и в теле heredoc вызовом не считается. Checkout вызова — каталог запуска
(`cd` раньше в команде, иначе `cwd` из payload, иначе `CLAUDE_PROJECT_DIR`) вместе с опциями
git `-C`, `--git-dir`, `--work-tree`, `--bare`: ветку спрашивает git с теми же опциями.
`cd` учитывается только в простой цепочке строгого лексера (`pr_commands.strict_steps`).
Commit/push в тексте, который не разобрать, checkout, который не определить, git, который не
ответил, и push, способный обновить все ветки (`--all`, `--mirror`, refspec с `*`), блокируются.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.project.hooks import pr_commands
else:
    # .claude/hooks must stay prunable by uninstall: never leave __pycache__ next to it.
    sys.dont_write_bytecode = True
    import pr_commands  # sibling in the installed hooks directory (sys.path[0])

GUIDE = "(docs/agents/git-workflow.md)"
ISSUE_BRANCH = f"работай на issue-ветке {GUIDE}."
CALL_TEXT = re.compile(r"\bgit\b[\s\S]*\b(?:commit|push)\b")
# `commit`/`push` as a whole token: not a part of an option (`--commit-plan-file`) or a name.
ACTION_TOKEN = r"(?<![\w-])(?:commit|push)(?![\w-])"
ACTION_WORD = re.compile(ACTION_TOKEN)
GIT_ACTION_TEXT = re.compile(rf"\bgit\b[\s\S]*{ACTION_TOKEN}")
# Environment variables that point git at a repository other than its working directory's.
GIT_ENV = re.compile(r"\bGIT_(?:DIR|WORK_TREE)\b")
DIR_COMMANDS = frozenset({"cd", "pushd", "popd"})
PIPES = frozenset({"|", "|&"})
# Wrapper options that start the program in another directory (`env -C`, `sudo -D`).
CHDIR_OPTIONS = frozenset({"-C", "--chdir", "-D"})
# Global git options that choose the repository: replayed when asking for the branch.
LOCATION_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree"})
# Other global options whose value is the next word, not the subcommand.
VALUE_OPTIONS = frozenset(
    {"-c", "--config-env", "--namespace", "--super-prefix", "--attr-source"}
)
# The only `git commit` options of the /to-spec grill docs commit: a message and output flags.
# Paths, `-a`, `--amend` and every other option commit more than the staged index.
COMMIT_MESSAGE_OPTIONS = frozenset({"-m", "--message", "-F", "--file"})
COMMIT_FLAGS = frozenset({"-q", "--quiet", "-s", "--signoff"})
# Push options that update every matching branch, protected ones included.
PUSH_EVERY_BRANCH_OPTIONS = frozenset({"--all", "--branches", "--mirror"})
# Bound of every git call, `ls-remote` included: a git that does not answer blocks (fail closed).
GIT_TIMEOUT_SECONDS = 20
UNPARSED = (
    "Zero Direct Commits: команду с git commit/push не удалось разобрать (код интерпретатора, "
    "команда из переменной или подстановки, незакрытая кавычка) — она заблокирована (fail "
    f"closed). Выполни commit или push отдельной простой командой {GUIDE}."
)
UNRESOLVED = (
    "Zero Direct Commits: не удалось определить checkout для git commit/push — команда "
    "заблокирована (fail closed). cd учитывается только в простой цепочке через && или ; без $, "
    "обратных кавычек, скобок, | и ||; путь должен существовать; GIT_DIR, GIT_WORK_TREE и env "
    f"-C не поддерживаются. Укажи путь явно: git -C <path> commit {GUIDE}."
)
GIT_NO_ANSWER = (
    "Zero Direct Commits: git не запустился или не ответил за "
    f"{GIT_TIMEOUT_SECONDS} с — ветку checkout не определить, команда заблокирована (fail "
    f"closed). Проверь git в этом checkout и повтори {GUIDE}."
)


@dataclass(frozen=True)
class Call:
    """Вызов `git commit` или `git push`: каталог запуска (None — не определить) и argv git."""

    cwd: Path | None
    # (option, value) of LOCATION_OPTIONS and `--bare`, in the order git applies them.
    location: tuple[tuple[str, str], ...]
    action: str
    args: tuple[str, ...]


def _git(call: Call, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Запустить git в том же репозитории, что и вызов; None — checkout не определён или git
    не запустился либо не ответил за GIT_TIMEOUT_SECONDS. Вызывающий код блокирует команду."""
    if call.cwd is None:
        return None
    argv = ["git", "-C", str(call.cwd)]
    for option, value in call.location:
        argv += [option] if option == "--bare" else [option, value]
    try:
        return subprocess.run(
            [*argv, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def calls(command: str, start: Path) -> list[Call] | None:
    """Вызовы git commit/push по порядку; None — commit/push в тексте, который не разобрать."""
    parsed = pr_commands.parse(command)
    if any(CALL_TEXT.search(fragment) for fragment in parsed.opaque):
        return None
    if _unparsed_computed(command, parsed.commands):
        return None
    found = _calls_in(parsed.commands, start)
    if GIT_ENV.search(command):
        return [replace(call, cwd=None) for call in found]
    if not any(_dir_change(argv) for argv in parsed.commands):
        return found
    walked = _walk(command, start)
    # The walk must account for every call: one in a heredoc a shell reads is not a step.
    if walked is None or len(walked) != len(found):
        return [replace(call, cwd=None) for call in found]
    return walked


def _dynamic(word: str) -> bool:
    """Значение слова bash вычислит при запуске: в нём есть `$` или обратная кавычка."""
    return "$" in word or "`" in word


def _computed(argv: list[str]) -> bool:
    """Программу или текст для оболочки команда получит только при запуске (`$X`, `sh -c "$(…)"`)."""
    if _dynamic(argv[0]):
        return True
    names = [pr_commands.program(word) for word in argv]
    evaluator = next(
        (i for i, name in enumerate(names) if name in pr_commands.EVALUATORS), None
    )
    return evaluator is not None and any(map(_dynamic, argv[evaluator + 1 :]))


def _unparsed_computed(command: str, commands: list[list[str]]) -> bool:
    """commit/push может оказаться git-подкомандой в вычисляемой команде, текст которой не разобрать.

    Часть опции (`--commit-plan-file`) и имени файла к отказу не ведёт; слово `commit` в другой
    команде цепочки ведёт, только если вычисляемая команда может получить его из переменной.
    """
    computed = [argv for argv in commands if _computed(argv)]
    if not computed:
        return False
    # The value of `$X` is unknown: `git … commit` anywhere in the text may end up in it.
    if GIT_ACTION_TEXT.search(command):
        return True
    # `$GIT commit`: the program is unknown, so a `commit`/`push` argument is its subcommand.
    if any(ACTION_WORD.search(word) for argv in computed for word in argv):
        return True
    # `SUB=commit; $GIT $SUB`: a computed argument may carry the word from another command.
    return bool(ACTION_WORD.search(command)) and any(
        _dynamic(word) for argv in computed for word in argv[1:]
    )


def _dir_change(argv: list[str]) -> list[str]:
    """argv `cd`/`pushd`/`popd` команды, в том числе после `builtin`/`command`; [] — не они."""
    while len(argv) > 1 and pr_commands.program(argv[0]) in ("builtin", "command"):
        argv = argv[1:]
    return argv if pr_commands.program(argv[0]) in DIR_COMMANDS else []


def _calls_in(commands: list[list[str]], cwd: Path | None) -> list[Call]:
    """Вызовы commit/push в simple commands, запущенных в каталоге `cwd`."""
    found: list[Call] = []
    for argv in commands:
        for index in pr_commands.positions(argv):
            if pr_commands.program(argv[index]) != "git":
                continue
            call = _git_call(argv[index + 1 :], cwd)
            if call is None:
                continue
            if any(
                word in CHDIR_OPTIONS or word.startswith("--chdir=")
                for word in argv[:index]
            ):
                call = replace(call, cwd=None)
            found.append(call)
    return found


def _walk(command: str, start: Path) -> list[Call] | None:
    """Вызовы простой цепочки с учётом `cd`; None — команда вне подмножества строгого лексера.

    `cd` меняет каталог последующих вызовов, только если выполнится наверняка: не в конвейере
    и не в and-or списке с `||`. `cd` не первым в списке `&&` (`x && cd p`) действует до конца
    списка; список, запущенный в фоне (`&`), каталог оболочки не меняет. Вызов в тексте, который
    исполняет команда шага (`bash -c`), получает каталог шага, если в тексте нет своего `cd`.
    """
    steps = pr_commands.strict_steps(command)
    if steps is None:
        return None
    lists: list[
        list[tuple[str, list[str]]]
    ] = []  # and-or lists: (operator before, argv)
    starts: list[str] = []  # the separator that starts each list
    for link, argv in steps:
        kind = link.replace("\n", "")
        if not lists or kind in ("", ";", "&"):
            lists.append([])
            starts.append(kind)
            kind = ""
        elif kind not in ("&&", "||", *PIPES):
            return None
        lists[-1].append((kind, argv))
    found: list[Call] = []
    current: Path | None = start
    for number, items in enumerate(lists):
        background = starts[number + 1 : number + 2] == ["&"]
        has_or = any(kind == "||" for kind, _ in items)
        entry = after = current
        for position, (kind, argv) in enumerate(items):
            nested = pr_commands.parse(shlex.join(argv)).commands
            inner = nested[1:] if nested[:1] == [argv] else nested
            found += _calls_in(
                nested, None if any(map(_dir_change, inner)) else current
            )
            change = _dir_change(argv)
            if not change:
                continue
            following = items[position + 1][0] if position + 1 < len(items) else ""
            if (
                pr_commands.program(change[0]) == "popd"
                or has_or
                or {kind, following} & PIPES
            ):
                current = after = None
                continue
            current = _cd(current, change[1:])
            after = current if position == 0 else None
        current = entry if background else after
    return found


def _cd(current: Path | None, args: list[str]) -> Path | None:
    """Каталог после `cd args`; None — его не определить или `cd` не сработает."""
    while args and args[0].startswith("-") and args[0] != "-":
        option, args = args[0], args[1:]
        if option == "--":
            break
    if not args:
        return Path.home()
    target = args[0]
    if target == "-" or _dynamic(target):
        return None
    path = Path(os.path.expanduser(target))
    if not path.is_absolute():
        if current is None:
            return None
        path = current / path
    return path if path.is_dir() else None


def _git_call(args: list[str], cwd: Path | None) -> Call | None:
    """Вызов commit/push из аргументов git после имени программы; None — другая подкоманда."""
    location: list[tuple[str, str]] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in LOCATION_OPTIONS or arg in VALUE_OPTIONS:
            if index + 1 == len(args):
                return None
            if arg in LOCATION_OPTIONS:
                location.append((arg, os.path.expanduser(args[index + 1])))
            index += 2
        elif arg.startswith(("--git-dir=", "--work-tree=")):
            option, value = arg.split("=", 1)
            location.append((option, value))
            index += 1
        elif arg == "--bare":
            location.append((arg, ""))
            index += 1
        elif arg.startswith("-"):
            index += 1
        elif arg in ("commit", "push"):
            if any(_dynamic(value) for _, value in location):
                cwd = None
            return Call(cwd, tuple(location), arg, tuple(args[index + 1 :]))
        else:
            return None
    return None


def _protected(branch: str, base_branch: str) -> bool:
    """Защищённая ли ветка: master, main, base_branch проекта или integration/*."""
    return branch in {"master", "main", base_branch} - {""} or branch.startswith(
        "integration/"
    )


def _push_destinations(call: Call) -> tuple[str, list[str]]:
    """Remote push-вызова и целевые ветки его refspec без `+` и `refs/heads/`."""
    positional = [arg for arg in call.args if not arg.startswith("-")]
    remote, refspecs = (positional[0], positional[1:]) if positional else ("", [])
    return remote, [
        re.sub(r"^refs/heads/", "", refspec.lstrip("+").rsplit(":", 1)[-1])
        for refspec in refspecs
    ]


def _push_block_reason(call: Call, base_branch: str) -> str:
    """Причина запретить push по целевым рефам; "" — нет."""
    remote, destinations = _push_destinations(call)
    every_branch = [arg for arg in call.args if arg in PUSH_EVERY_BRANCH_OPTIONS] + [
        dest for dest in destinations if "*" in dest
    ]
    if every_branch:
        return (
            f"Zero Direct Commits: push с '{every_branch[0]}' может обновить защищённую ветку — "
            f"запрещён; укажи целевую ветку явно и {ISSUE_BRANCH}"
        )
    for dest in destinations:
        if not dest.startswith("integration/"):
            if _protected(dest, base_branch):
                return (
                    f"Zero Direct Commits: push с целевым рефом '{dest}' запрещён — "
                    f"{ISSUE_BRANCH}"
                )
            continue
        code = _ls_remote(call, remote, dest)
        if code == 0:
            return (
                f"Zero Direct Commits: push в существующую ветку '{dest}' запрещён — "
                f"{ISSUE_BRANCH}"
            )
        if code != 2:
            return (
                f"Zero Direct Commits: не удалось проверить на remote '{remote}', существует "
                f"ли '{dest}', — push заблокирован. Проверь доступ к remote и повтори {GUIDE}."
            )
    return ""


def _creates_integration_only(call: Call) -> bool:
    """Push только создаёт `integration/*`: его можно выполнить из защищённой ветки (/to-spec).

    Отсутствие этих веток на remote проверяет `_push_block_reason`, вызванный раньше.
    """
    _, destinations = _push_destinations(call)
    return bool(destinations) and all(
        dest.startswith("integration/") for dest in destinations
    )


def _ls_remote(call: Call, remote: str, branch: str) -> int | None:
    """Код `git ls-remote --exit-code` для ветки: 0 — она есть, 2 — нет, иначе не проверить."""
    result = _git(call, "ls-remote", "--exit-code", remote, "refs/heads/" + branch)
    return None if result is None else result.returncode


def _grill_doc(path: str) -> bool:
    """Путь документации прожарки: `CONTEXT.md`, `CONTEXT-MAP.md`, `**/CONTEXT.md`,
    `docs/adr/**`, `**/docs/adr/**`."""
    parts = path.split("/")
    return (
        parts[-1] == "CONTEXT.md"
        or path == "CONTEXT-MAP.md"
        or any(parts[i : i + 2] == ["docs", "adr"] for i in range(len(parts) - 2))
    )


def _message_only(args: tuple[str, ...]) -> bool:
    """У `git commit` только опции сообщения и флаги вывода: он коммитит один индекс."""
    words = iter(args)
    for arg in words:
        if arg in COMMIT_MESSAGE_OPTIONS:
            if next(words, None) is None:
                return False
        elif not (
            arg in COMMIT_FLAGS
            or arg.startswith(("--message=", "--file="))
            or (arg[:2] in ("-m", "-F") and len(arg) > 2)
        ):
            return False
    return True


def _grill_docs_commit(command: str, call: Call, branch: str) -> bool:
    """Коммит документов прожарки в новую `integration/*` до её публикации (/to-spec).

    Разрешён, только если команда — один простой `git commit` с сообщением, индекс содержит
    только пути документации прожарки, а ветки нет на `origin`. Другая команда в том же вызове
    могла бы изменить индекс после проверки, поэтому она отменяет исключение.
    """
    if call.action != "commit" or not branch.startswith("integration/"):
        return False
    steps = pr_commands.strict_steps(command)
    if steps is None or len(steps) != 1:
        return False
    _, argv = steps[0]
    if argv[:1] != ["git"] or not _message_only(call.args):
        return False
    staged = _git(call, "diff", "--cached", "--name-only", "--no-renames", "-z")
    if staged is None or staged.returncode:
        return False
    paths = [path for path in staged.stdout.split("\0") if path]
    if not paths or not all(map(_grill_doc, paths)):
        return False
    return _ls_remote(call, "origin", branch) == 2


def block_reason(command: str, start: Path, project: Path) -> str:
    """Причина запретить команду, запущенную в `start`; "" — команду можно выполнить."""
    found = calls(command, start)
    if found is None:
        return UNPARSED
    base_branch = _base_branch(project)
    for call in found:
        if call.cwd is None:
            return UNRESOLVED
        if call.action == "push":
            reason = _push_block_reason(call, base_branch)
            if reason:
                return reason
            if _creates_integration_only(call):
                continue
        result = _git(call, "rev-parse", "--abbrev-ref", "HEAD")
        if result is None:
            return GIT_NO_ANSWER
        # Outside a repository git fails as the command would; an explicit path that does not
        # resolve yet (`git -C {} commit` from xargs) cannot be checked.
        if result.returncode and call.location:
            return UNRESOLVED
        branch = result.stdout.strip()
        if _protected(branch, base_branch) and not _grill_docs_commit(
            command, call, branch
        ):
            return (
                f"Zero Direct Commits: коммит/push в защищённую ветку '{branch}' запрещён — "
                f"{ISSUE_BRANCH}"
            )
    return ""


def _base_branch(project: Path) -> str:
    """`base_branch` из `.harness/project.json` проекта; "" — его нет."""
    try:
        data = json.loads(
            (project / ".harness" / "project.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ""
    branch = data.get("base_branch", "") if isinstance(data, dict) else ""
    return branch if isinstance(branch, str) else ""


def main() -> int:
    """CLI для shell-хука: `direct_commits.py < payload.json`.

    Разрешение — код 0 и слово `allow` в stdout: частичная копия модуля, которая завершается
    без решения, слова не печатает, и хук блокирует команду.
    """
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        data: object = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = None
    tool_input = data.get("tool_input") if isinstance(data, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
    if isinstance(command, str):
        cwd = data.get("cwd") if isinstance(data, dict) else None
        start = Path(cwd) if isinstance(cwd, str) else project
        reason = block_reason(command, start, project)
    else:
        # A payload without a string command is checked by its raw text (fail closed).
        reason = UNPARSED if CALL_TEXT.search(raw) else ""
    if reason:
        print(reason, file=sys.stderr)
        return 2
    print("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
