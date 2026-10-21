#!/usr/bin/env python3
"""protect-hook-policy: критичный guard политики hooks и конфигурации hooks (issue #794).

`python protect_hook_policy.py < payload.json` печатает `allow` и завершается с кодом 0 либо пишет
причину в stderr с префиксом `[reason_code]` и завершается с кодом 2. Решение не зависит ни от
профиля, ни от роли, ни от окружения: guard блокирует во всех профилях.

Инструменты файлов (Write, Edit, MultiEdit, NotebookEdit):
- `.claude/settings*.json`, `.codex/hooks.json` и всё внутри каталогов `.claude/hooks` и
  `.codex/hooks` — блок;
- `.harness/project.json` — правка разрешена, только если разобранный ключ `hook_policy` до и после
  неё совпадает; JSON, который не разбирается до или после правки, и правка, результат которой
  нельзя вычислить, — блок.

Bash: любая запись в `.harness/project.json` — блок. Записью считается перенаправление в этот файл
(цели перенаправлений записывает строгий лексер `pr_commands`) и упоминание файла аргументом любой
программы, кроме читающих (`cat`, `grep`, `sed` без `-i`, безопасных подкоманд git и т. п.): `tee`,
`cp`, `mv`, `dd of=`, `sed -i`, `git checkout --`, код интерпретатора. Команда вне строгого подмножества
разбирается общим разбором `pr_commands.parse`; цель перенаправления в ней ищется по тексту.

Пути сравниваются после `realpath` по хвосту (`.harness/project.json`), поэтому symlink на защищённый
файл и другой checkout или worktree решение не меняют. Остаточные ограничения: запись через скрипт в
файле, косвенность через переменные и программы, которых в команде нет.
"""

from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from harness.project.hooks import pr_commands
else:
    # .claude/hooks must stay prunable by uninstall: never leave __pycache__ next to it.
    sys.dont_write_bytecode = True
    import pr_commands  # sibling in the installed hooks directory (sys.path[0])

PROJECT = "project"
CONFIG = "config"
HARNESS_DIR = "harness-dir"
DEFAULT_REASON = "hook-policy-protected"
PATH_FIELDS = ("file_path", "notebook_path")
# Redirection operators that open the target for writing.
WRITE_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>", ">&"})
# Programs that only read their file arguments; `sed` and `git` are decided by their options.
READERS = frozenset(
    {
        ":",
        "[",
        "basename",
        "cat",
        "cd",
        "cmp",
        "diff",
        "dirname",
        "echo",
        "egrep",
        "fgrep",
        "file",
        "grep",
        "head",
        "jq",
        "less",
        "ls",
        "md5sum",
        "more",
        "popd",
        "printf",
        "pushd",
        "pwd",
        "readlink",
        "realpath",
        "rg",
        "sha256sum",
        "stat",
        "tail",
        "test",
        "true",
        "wc",
    }
)
# Programs that move or delete a directory together with the file inside it.
DESTROYERS = frozenset({"mv", "rm", "rmdir", "rsync", "unlink"})
GIT_READ = frozenset(
    {
        "add",
        "blame",
        "cat-file",
        "check-ignore",
        "commit",
        "diff",
        "diff-index",
        "diff-tree",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "rev-parse",
        "show",
        "show-ref",
        "status",
    }
)
GIT_VALUE_OPTIONS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--attr-source",
        "--config-env",
    }
)
SED_IN_PLACE = re.compile(r"-[A-Za-z]*i.*|--in-place(?:=.*)?")
GLOB_CHARACTERS = re.compile(r"[*?\[]")
PROJECT_NAME = re.compile(r"project\.json", re.IGNORECASE)
# Outside the strict subset the redirection targets are not recorded: look for them in the text.
RAW_REDIRECT = re.compile(r">[>|]?\s*[\"']?[^\s;&|<>]*project\.json", re.IGNORECASE)
RAW_ASSIGNMENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*=\S*project\.json", re.IGNORECASE)


class Refusal(Exception):
    """Отказ с зарегистрированным reason_code и сообщением для агента."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def classify(real: str) -> Optional[str]:
    """Класс защищаемого пути по хвосту уже разрешённого пути (регистр не различается)."""
    parts = [part.casefold() for part in Path(real).parts]
    if parts[-2:] == [".harness", "project.json"]:
        return PROJECT
    if len(parts) >= 2 and parts[-2] == ".claude":
        if fnmatch.fnmatchcase(parts[-1], "settings*.json"):
            return CONFIG
    if parts[-2:] == [".codex", "hooks.json"]:
        return CONFIG
    for index in range(len(parts) - 1):
        if parts[index] in (".claude", ".codex") and parts[index + 1] == "hooks":
            return CONFIG
    if parts and parts[-1] == ".harness":
        return HARNESS_DIR
    return None


def _real(path: str, cwd: str) -> str:
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def _policy(text: Optional[str], when: str) -> Optional[str]:
    """Канонический `hook_policy` текста (None — ключа нет); неразбираемое — отказ."""

    def pairs(items: List[Tuple[str, object]]) -> Dict[str, object]:
        keys = [key for key, _ in items]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate key")
        return dict(items)

    if text is None:
        return None
    try:
        data = json.loads(text, object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise Refusal(
            "hook-policy-unparseable",
            f".harness/project.json {when} не разбирается как JSON-объект без повторяющихся "
            "ключей: ключ hook_policy нельзя сравнить. Исправь файл вручную.",
        )
    if "hook_policy" not in data:
        return None
    return json.dumps(data["hook_policy"], sort_keys=True, ensure_ascii=True)


def _edited(
    tool: str, tool_input: Dict[str, object], text: Optional[str]
) -> Optional[str]:
    """Содержимое файла после правки; если вычислить его нельзя — отказ."""
    unknown = Refusal(
        "hook-policy-unparseable",
        "результат правки .harness/project.json нельзя вычислить заранее: перечитай файл и "
        "задай old_string, который встречается ровно один раз, либо правь файл вручную.",
    )
    if tool == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            raise unknown
        return content
    if tool == "Edit":
        edits: object = [tool_input]
    elif tool == "MultiEdit":
        edits = tool_input.get("edits")
    else:
        raise unknown
    if not isinstance(edits, list) or not edits:
        raise unknown
    for edit in edits:
        if not isinstance(edit, dict):
            raise unknown
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            raise unknown
        if old == "" and not text:
            text = new
            continue
        if text is None or old == "":
            raise unknown
        count = text.count(old)
        if count == 0 or (count > 1 and not edit.get("replace_all")):
            raise unknown
        text = (
            text.replace(old, new)
            if edit.get("replace_all")
            else text.replace(old, new, 1)
        )
    return text


def check_file_tool(tool: str, tool_input: Dict[str, object], cwd: str) -> None:
    """Правка файла инструментом агента: отказ, если она трогает защищаемый путь."""
    raw = next(
        (tool_input[f] for f in PATH_FIELDS if isinstance(tool_input.get(f), str)), None
    )
    if not isinstance(raw, str) or not raw:
        raise Refusal(
            DEFAULT_REASON, "в вызове нет пути файла: правку нельзя проверить."
        )
    real = _real(raw, cwd)
    kind = classify(real)
    if kind == CONFIG:
        raise Refusal(
            "agent-config-edit",
            "настройки hooks (.claude/settings*.json, .codex/hooks.json) и каталоги hooks "
            f"агент не правит: {real}. Такие изменения делает человек.",
        )
    if kind != PROJECT:
        return
    try:
        with open(real, "rb") as handle:
            before_text: Optional[str] = handle.read().decode("utf-8")
    except FileNotFoundError:
        before_text = None
    except (OSError, UnicodeError) as exc:
        raise Refusal(
            "hook-policy-unparseable",
            f".harness/project.json не читается как UTF-8 ({type(exc).__name__}): ключ hook_policy "
            "нельзя сравнить.",
        ) from exc
    before = _policy(before_text, "до правки")
    after = _policy(_edited(tool, tool_input, before_text), "после правки")
    if before != after:
        raise Refusal(
            "hook-policy-changed",
            "правка меняет ключ hook_policy в .harness/project.json: политику hooks меняет "
            "только человек. Правка других ключей файла разрешена.",
        )


def _git_subcommand(argv: Sequence[str]) -> Optional[str]:
    index = 1
    while index < len(argv):
        word = argv[index]
        if word in GIT_VALUE_OPTIONS:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            return word
    return None


def _writes(argv: Sequence[str]) -> bool:
    """Может ли программа команды изменить файл, названный в её аргументах."""
    name = pr_commands.program(argv[0])
    if name in READERS:
        return False
    if name == "sed":
        return any(SED_IN_PLACE.fullmatch(word) for word in argv[1:])
    if name == "git":
        return _git_subcommand(argv) not in GIT_READ | {None}
    return True


def _mentions(word: str, bases: Sequence[str], destroys: bool) -> bool:
    """Называет ли слово `.harness/project.json`: по имени, по пути после realpath или по glob."""
    if PROJECT_NAME.search(word):
        return True
    wanted = (PROJECT, HARNESS_DIR) if destroys else (PROJECT,)
    for base in bases:
        candidate = os.path.join(base, word)
        hits = glob.glob(candidate) if GLOB_CHARACTERS.search(word) else [candidate]
        if any(classify(os.path.realpath(hit)) in wanted for hit in hits):
            return True
    return False


def _bases(argvs: Sequence[Sequence[str]], cwd: str, root: str) -> List[str]:
    """Каталоги, от которых слово команды может читаться как путь (cwd, корень, цели `cd`)."""
    bases = [cwd, root]
    for argv in argvs:
        if pr_commands.program(argv[0]) in ("cd", "pushd") and len(argv) > 1:
            bases.append(os.path.join(cwd, argv[1]))
    return list(dict.fromkeys(bases))


def check_bash(command: str, cwd: str, root: str) -> None:
    """Shell-запись в `.harness/project.json` — отказ."""
    steps = pr_commands.strict_steps(command)
    redirects = pr_commands.strict_redirections(command)
    refusal = Refusal(
        "project-json-shell-write",
        "запись в .harness/project.json через shell (перенаправление, tee, sed -i, cp, mv, dd, "
        "git checkout и подобные) запрещена: результат нельзя вычислить до исполнения. "
        "Правь файл инструментом Edit/Write без изменения ключа hook_policy.",
    )
    if steps is not None and redirects is not None:
        argvs = [argv for _, argv in steps]
        opaque: List[str] = []
    else:
        parsed = pr_commands.parse(command)
        argvs, opaque, redirects = parsed.commands, parsed.opaque, []
        if (
            RAW_REDIRECT.search(command)
            or RAW_ASSIGNMENT.search(command)
            or any(PROJECT_NAME.search(text) for text in opaque)
        ):
            raise refusal
    bases = _bases(argvs, cwd, root)
    for operator, target in redirects or []:
        if operator in WRITE_REDIRECTS and _mentions(target, bases, False):
            raise refusal
    for argv in argvs:
        destroys = pr_commands.program(argv[0]) in DESTROYERS
        if _writes(argv) and any(_mentions(word, bases, destroys) for word in argv[1:]):
            raise refusal


def decide(payload: object, root: str) -> None:
    """Проверить payload PreToolUse; отказ — `Refusal`."""
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    if not isinstance(tool_input, dict):
        raise Refusal(
            DEFAULT_REASON,
            "payload hook не содержит tool_input: вызов нельзя проверить.",
        )
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    command = tool_input.get("command")
    if isinstance(command, str):
        check_bash(command, cwd, root)
    elif any(isinstance(tool_input.get(field), str) for field in PATH_FIELDS):
        tool = payload.get("tool_name") if isinstance(payload, dict) else None
        check_file_tool(tool if isinstance(tool, str) else "Write", tool_input, cwd)
    else:
        raise Refusal(
            DEFAULT_REASON,
            "в вызове нет ни command, ни пути файла: вызов нельзя проверить.",
        )


def main() -> int:
    """Прочитать payload из stdin и вынести решение: код 0 и `allow` либо код 2 и причина."""
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except ValueError:
        payload = None
    root = str(Path(__file__).resolve().parents[2])
    try:
        decide(payload, root)
    except Refusal as refusal:
        print(
            f"[{refusal.code}] protect-hook-policy: {refusal.message}", file=sys.stderr
        )
        return 2
    print("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
