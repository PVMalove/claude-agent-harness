#!/usr/bin/env python3
"""Разбор Bash-команды PreToolUse-хука на simple commands, которые bash действительно выполнит.

`python pr_commands.py merge` читает payload хука из stdin и завершается с кодом 2, если
`tool_input.command` выполняет merge PR/MR (`gh pr merge`, `glab mr merge|accept`).
"""

import json
import re
import shlex
import sys
from dataclasses import dataclass, field

SEPARATORS = "();<>|&\n"
SHELLS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
# Any word after one of these may be a command string it runs (`bash -c`, `eval`, `ssh host`).
EVALUATORS = SHELLS | {"eval", "runuser", "script", "ssh", "su", "watch"}
INTERPRETER = re.compile(r"python[0-9.]*|perl|ruby|node|php")
# The word after one of these interpreter options is program text (`python -c`, `perl -le`).
CODE_OPTION = re.compile(r"-[A-Za-z]*[ceEpr]|--eval|--print")
KEYWORDS = frozenset(
    {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until"}
)
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(\\?)([\x27\"]?)([A-Za-z_][A-Za-z0-9_.-]*)\3")
MERGE_TEXT = re.compile(r"\bgh\s+pr\s+merge\b|\bglab\s+mr\s+(?:merge|accept)\b")
MAX_DEPTH = 8


@dataclass
class Parsed:
    """Simple commands, которые выполнит bash, и фрагменты, которые по токенам не решить.

    Фрагмент `opaque` bash исполнит, но его разбор недоступен (незакрытая кавычка, код
    интерпретатора, stdin оболочки); вызывающий код решает по нему только в сторону блокировки.
    """

    commands: list[list[str]] = field(default_factory=list)
    opaque: list[str] = field(default_factory=list)


def program(word: str) -> str:
    """Имя программы из слова команды: без каталога и суффикса `.exe`."""
    name = re.split(r"[\\/]", word)[-1]
    return name[:-4] if name.lower().endswith(".exe") else name


def positions(argv: list[str]) -> range:
    """Индексы argv, с которых может стартовать программа.

    Каждый суффикс argv может оказаться запускаемой командой (`sudo`, `timeout`, `xargs`,
    `uv run`, `find -exec`).
    """
    return range(len(argv))


def parse(command: str) -> Parsed:
    """Разобрать текст Bash-команды; исключения наружу не выходят."""
    parsed = Parsed()
    _parse(command, parsed, 0)
    return parsed


def _parse(text: str, out: Parsed, depth: int) -> None:
    """Добавить в `out` simple commands и непрозрачные фрагменты текста `text`."""
    if depth > MAX_DEPTH:
        out.opaque.append(text)
        return
    source, scripts = _split_heredocs(text.replace("\\\n", ""))
    for script in scripts:
        _parse(script, out, depth + 1)
    substitutions, broken = _scan(source)
    if broken:
        out.opaque.append(source)
    for inner in substitutions:
        _parse(inner, out, depth + 1)
    lexer = shlex.shlex(source, posix=True, punctuation_chars=SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    # `#` stays an ordinary character (fail closed): shlex would end a word at any `#`.
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        out.opaque.append(source)
        return
    words: list[str] = []
    for token in [*tokens, ";"]:
        if token and set(token) <= set(SEPARATORS):
            _add_command(words, source, out, depth)
            words = []
        else:
            words.append(token)


def _split_heredocs(text: str) -> tuple[str, list[str]]:
    """Текст без тел heredoc и сами тела: каждое разбирается как команды (fail closed)."""
    kept: list[str] = []
    scripts: list[str] = []
    # (strip leading tabs, delimiter)
    pending: list[tuple[bool, str]] = []
    body: list[str] = []
    for line in text.split("\n"):
        if pending:
            strip_tabs, delimiter = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) != delimiter:
                body.append(line)
                continue
            pending.pop(0)
            scripts.append("\n".join(body))
            body = []
            continue
        kept.append(line)
        pending.extend(
            (match.group(1) == "-", match.group(4)) for match in HEREDOC.finditer(line)
        )
    if pending:
        # bash reads an unterminated heredoc to the end of input: treat the rest as commands.
        scripts.append("\n".join(body))
    return "\n".join(kept), scripts


def _scan(text: str) -> tuple[list[str], bool]:
    """Тела `$(…)`, `` `…` ``, `<(…)`, `>(…)` вне одинарных кавычек и признак незакрытой конструкции."""
    bodies: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        pair = text[index : index + 2]
        if quote == "'":
            quote = "" if char == "'" else quote
        elif char == "\\":
            index += 2
            continue
        elif char == "`" or pair == "$(" or (not quote and pair in ("<(", ">(")):
            start = index + (1 if char == "`" else 2)
            end = (
                _backquote_end(text, start) if char == "`" else _paren_end(text, start)
            )
            if end < 0:
                return bodies, True
            bodies.append(text[start:end])
            index = end + 1
            continue
        elif char == '"':
            quote = "" if quote else '"'
        elif not quote and char == "'":
            quote = "'"
        index += 1
    return bodies, bool(quote)


def _backquote_end(text: str, start: int) -> int:
    """Индекс закрывающей обратной кавычки или -1."""
    index = start
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "`":
            return index
        index += 1
    return -1


def _paren_end(text: str, start: int) -> int:
    """Индекс скобки, закрывающей подстановку, с учётом вложенных скобок и кавычек, или -1."""
    depth = 1
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if quote == "'":
            quote = "" if char == "'" else quote
        elif char == "\\":
            index += 1
        elif char == '"':
            quote = "" if quote else '"'
        elif quote:
            pass
        elif char == "'":
            quote = "'"
        elif char in "()":
            depth += 1 if char == "(" else -1
            if not depth:
                return index
        index += 1
    return -1


def _add_command(words: list[str], source: str, out: Parsed, depth: int) -> None:
    """Записать simple command и разобрать то, что она исполняет сама."""
    start = 0
    while start < len(words) and (
        words[start] in KEYWORDS or ASSIGNMENT.fullmatch(words[start])
    ):
        start += 1
    argv = words[start:]
    if not argv:
        return
    out.commands.append(argv)
    names = [program(word) for word in argv]
    evaluator = next((i for i, name in enumerate(names) if name in EVALUATORS), None)
    if evaluator is not None:
        # Every later word was already parsed once here, so later evaluators add nothing.
        for word in argv[evaluator + 1 :]:
            _parse(word, out, depth + 1)
    if _reads_stdin(argv, names):
        out.opaque.append(source)
    interpreter = next(
        (i for i, name in enumerate(names) if INTERPRETER.fullmatch(name)), None
    )
    if interpreter is not None:
        options = argv[interpreter + 1 :]
        out.opaque.extend(
            text
            for option, text in zip(options, options[1:])
            if CODE_OPTION.fullmatch(option)
        )


def _reads_stdin(argv: list[str], names: list[str]) -> bool:
    """Оболочка без `-c`, у которой нет файла-скрипта или есть `-s`, читает команды из stdin."""
    flags_only, has_c, has_s = True, False, False
    for word, name in zip(reversed(argv), reversed(names)):
        if name in SHELLS and not has_c and (flags_only or has_s):
            return True
        if not word.startswith("-"):
            flags_only = False
        elif not word.startswith("--"):
            has_c = has_c or "c" in word[1:]
            has_s = has_s or word == "-s"
    return False


def _is_merge(argv: list[str], start: int) -> bool:
    """Начинается ли с позиции `start` вызов `gh pr merge` или `glab mr merge|accept`."""
    name = program(argv[start])
    action = argv[start + 1 : start + 3]
    return (name == "gh" and action == ["pr", "merge"]) or (
        name == "glab" and action in (["mr", "merge"], ["mr", "accept"])
    )


def merge_requested(command: str) -> bool:
    """Выполняет ли команда merge PR/MR; непрозрачный фрагмент с merge-текстом — тоже да."""
    parsed = parse(command)
    return any(
        _is_merge(argv, start) for argv in parsed.commands for start in positions(argv)
    ) or any(MERGE_TEXT.search(fragment) for fragment in parsed.opaque)


def merge_exit_code(raw: str) -> int:
    """Код merge-блока для payload хука: 2 — запрет, 0 — разрешение.

    Решает только `tool_input.command`; payload без строковой команды проверяется по сырому
    тексту, чтобы нераспознанный ввод с merge-текстом блокировался (fail closed).
    """
    try:
        data: object = json.loads(raw)
    except ValueError:
        data = None
    tool_input = data.get("tool_input") if isinstance(data, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 2 if MERGE_TEXT.search(raw) else 0
    return 2 if merge_requested(command) else 0


def main(argv: list[str]) -> int:
    """CLI для shell-хуков: `pr_commands.py merge < payload.json`."""
    if argv[1:] != ["merge"]:
        print("usage: pr_commands.py merge < hook-payload.json", file=sys.stderr)
        return 2
    return merge_exit_code(sys.stdin.buffer.read().decode("utf-8", errors="replace"))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
