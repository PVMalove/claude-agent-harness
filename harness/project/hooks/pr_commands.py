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
# Their arguments are text they print or read, never a program they start.
PRINTERS = frozenset(
    {
        "cat",
        "diff",
        "echo",
        "egrep",
        "fgrep",
        "grep",
        "head",
        "jq",
        "ls",
        "printf",
        "rg",
        "sed",
        "tail",
        "wc",
    }
)
INTERPRETER = re.compile(r"python[0-9.]*|perl|ruby|node|php")
# The word after one of these interpreter options is program text (`python -c`, `perl -le`).
CODE_OPTION = re.compile(r"-[A-Za-z]*[ceEpr]|--eval|--print")
KEYWORDS = frozenset(
    {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until"}
)
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(\\?)([\x27\"]?)([A-Za-z_][A-Za-z0-9_.-]*)\3")
WORD_BREAK = re.compile(r"[\s;&|()<>]+")
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

    Аргументы печатающих команд — только текст. У любой другой программы каждый суффикс argv
    может оказаться запускаемой командой (`sudo`, `timeout`, `xargs`, `uv run`, `find -exec`).
    """
    return range(1) if program(argv[0]) in PRINTERS else range(len(argv))


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
    joined = text.replace("\\\n", "")
    source, scripts, expanded, broken = _split_heredocs(joined)
    if broken:
        # Heredoc bodies cannot be told from commands: the whole text decides, towards a block.
        out.opaque.append(joined)
    for script in scripts:
        _parse(script, out, depth + 1)
    for body in expanded:
        _, substitutions, broken = _scan(body, quoting=False)
        if broken:
            out.opaque.append(body)
        for inner in substitutions:
            _parse(inner, out, depth + 1)
    text, substitutions, broken = _scan(source, quoting=True)
    if broken:
        out.opaque.append(source)
    for inner in substitutions:
        _parse(inner, out, depth + 1)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    # _scan already dropped comments: shlex would also end a word at a `#` inside it.
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


# Contexts of the heredoc scanner, named by what opened them. Commands run at the top level (""),
# in `$(…)`/`(…)` (both "$(") and in backquotes: only there `<<` is an operator and `#` starts a
# comment. In arithmetic (`((`, `$((`, `$[`, a subscript `[`, and "a(" for a group inside them)
# `<<` is a shift, which the scanner cannot tell from an operator.
COMMAND_CONTEXTS = frozenset({"", "$(", "`"})
ARITHMETIC_CONTEXTS = frozenset({"((", "[", "a("})
CLOSERS = {
    "'": "'",
    "$'": "'",
    '"': '"',
    "${": "}",
    "$(": ")",
    "`": "`",
    "((": ")",
    "[": "]",
    "a(": ")",
}
# A word starts after one of these; a heredoc delimiter word ends at one or at the end of text.
WORD_EDGE = " \t\r\n;&|()<>"


def _split_heredocs(text: str) -> tuple[str, list[str], list[str], bool]:
    """Текст без операторов и тел heredoc, тела-скрипты, тела с подстановками и признак сбоя.

    Один проход со стеком контекстов: `<<` — оператор только вне кавычек, `${…}`, арифметики
    и комментариев, то есть на верхнем уровне и внутри `$(…)`, `(…)`, `` `…` ``; `<<<` —
    here-string. Тела читаются со строки после перевода строки вне кавычек и вместе с текстом
    оператора удаляются из текста. Тело с кавычками в разделителе (`<<'EOF'`, `<<"EOF"`,
    `<<\\EOF`) — литерал: оно отбрасывается, если строка не передаёт его оболочке.
    Признак сбоя — оператор, который не разобрать (разделитель, сдвиг в арифметике, `case`
    в подстановке, перевод строки или конец подстановки до тела), или незакрытая конструкция;
    тогда возвращается исходный текст, и решает вызывающий код, только в сторону блокировки.
    """
    kept: list[str] = []
    scripts: list[str] = []
    expanded: list[str] = []
    stack: list[str] = []
    # (strip leading tabs, delimiter, literal body, stack depth at the operator)
    pending: list[tuple[bool, str, bool, int]] = []
    broken: tuple[str, list[str], list[str], bool] = (text, [], [], True)
    index = line_start = 0
    while index < len(text):
        top = stack[-1] if stack else ""
        char = text[index]
        word_start = index == 0 or text[index - 1] in WORD_EDGE
        step = 1
        if char == "\n" and pending:
            if top not in COMMAND_CONTEXTS or {op[3] for op in pending} != {len(stack)}:
                return broken
            line = text[line_start:index]
            to_shell = bool(SHELLS & {program(word) for word in WORD_BREAK.split(line)})
            kept.append(char)
            lines = text[index + 1 :].split("\n")
            used = 0
            for strip_tabs, delimiter, literal, _ in pending:
                start = used
                while used < len(lines):
                    row = lines[used].lstrip("\t") if strip_tabs else lines[used]
                    if row == delimiter:
                        break
                    used += 1
                body = "\n".join(lines[start:used])
                if used == len(lines):
                    # bash reads an unterminated heredoc to the end of input: the rest is commands.
                    scripts.append(body)
                    return "".join(kept), scripts, expanded, False
                used += 1
                target = scripts if to_shell else None if literal else expanded
                if target is not None:
                    target.append(body)
            pending = []
            index = line_start = index + 1 + sum(len(row) + 1 for row in lines[:used])
            continue
        if top in ("'", "$'"):
            if char == "'":
                stack.pop()
            elif char == "\\" and top == "$'":
                step = 2
        elif char == "\\":
            step = 2
        elif top and char == CLOSERS[top]:
            if top == "((" and text[index + 1 : index + 2] != ")":
                return broken
            step = 2 if top == "((" else 1
            stack.pop()
            if any(op[3] > len(stack) for op in pending):
                return broken
        elif top in ARITHMETIC_CONTEXTS and text.startswith("<<", index):
            return broken
        elif top in COMMAND_CONTEXTS and text.startswith("<<<", index):
            step = 3
        elif top in COMMAND_CONTEXTS and text.startswith("<<", index):
            match = HEREDOC.match(text, index)
            # A delimiter that runs on into quotes or `$` is not decided ("" is in WORD_EDGE).
            if match is None or text[match.end() : match.end() + 1] not in WORD_EDGE:
                return broken
            literal = bool(match.group(2) or match.group(3))
            pending.append((match.group(1) == "-", match.group(4), literal, len(stack)))
            kept.append(" ")
            index = match.end()
            continue
        elif top in COMMAND_CONTEXTS and char == "#" and word_start:
            # A comment ends at the newline, inside backquotes also at the closing backquote.
            ends = [text.find(stop, index) for stop in ("\n`" if top == "`" else "\n")]
            step = min([end for end in ends if end >= 0], default=len(text)) - index
        elif (
            top == "$("
            and word_start
            and text.startswith("case", index)
            and text[index + 4 : index + 5] in WORD_EDGE
        ):
            # A `case` pattern's `)` would end the substitution early in this scan.
            return broken
        else:
            if char == "\n" and top in COMMAND_CONTEXTS:
                line_start = index + 1
            context, step = _heredoc_context(text, index, top)
            if context:
                stack.append(context)
        kept.append(text[index : index + step])
        index += step
    return ("".join(kept), scripts, expanded, False) if not stack else broken


def _heredoc_context(text: str, index: int, top: str) -> tuple[str, int]:
    """Контекст heredoc-сканера, открытый в `index` внутри `top`, и длина открывающего текста.

    `("", 1)` — в этой позиции ничего не открывается.
    """
    if text[index] not in "$`'\"([":
        return "", 1
    openers = [("$((", "(("), ("$(", "$("), ("${", "${"), ("$[", "["), ("`", "`")]
    if top != '"':
        openers += [("$'", "$'"), ("'", "'"), ('"', '"')]
    if top in COMMAND_CONTEXTS:
        openers += [("((", "(("), ("(", "$("), ("[", "[")]
    elif top in ARITHMETIC_CONTEXTS:
        openers += [("(", "a("), ("[", "[")]
    for opener, context in openers:
        if text.startswith(opener, index):
            return context, len(opener)
    return "", 1


def _scan(text: str, quoting: bool) -> tuple[str, list[str], bool]:
    """Текст без комментариев, тела `$(…)`, `` `…` ``, `<(…)`, `>(…)` и признак незакрытой конструкции.

    `quoting=False` — тело heredoc: кавычки и `#` в нём не особые, подстановки bash выполняет.
    """
    kept: list[str] = []
    bodies: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        pair = text[index : index + 2]
        if quote == "'":
            quote = "" if char == "'" else quote
        elif char == "\\":
            kept.append(pair)
            index += 2
            continue
        elif (
            char == "`"
            or pair == "$("
            or (quoting and not quote and pair in ("<(", ">("))
        ):
            start = index + (1 if char == "`" else 2)
            end = (
                _backquote_end(text, start) if char == "`" else _paren_end(text, start)
            )
            if end < 0:
                return "".join(kept) + text[index:], bodies, True
            bodies.append(text[start:end])
            kept.append(text[index : end + 1])
            index = end + 1
            continue
        elif not quoting:
            pass
        elif char == '"':
            quote = "" if quote else '"'
        elif quote:
            pass
        elif char == "'":
            quote = "'"
        elif char == "#" and (index == 0 or text[index - 1] in " \t\r\n;&|()<>"):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        kept.append(char)
        index += 1
    return "".join(kept), bodies, bool(quote)


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
    if program(argv[0]) in PRINTERS:
        return
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
