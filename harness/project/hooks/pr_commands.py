#!/usr/bin/env python3
"""Разбор Bash-команды PreToolUse-хуков: merge-блок и ветка создаваемого PR/MR.

`python pr_commands.py merge` читает payload хука из stdin и завершается с кодом 2, если в
`tool_input.command` есть merge PR/MR (`gh pr merge`, `glab mr merge|accept`) и команда не
состоит целиком из инертных simple commands строгого лексера (fail closed, `merge_blocked`).
`qa-gate-state.py` импортирует модуль, чтобы найти ветку создаваемого PR/MR (`create_heads`).
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
# No word boundaries: a strict superset of the substring the hook matched before #443.
MERGE_FLOOR = re.compile(r"gh\s+pr\s+merge|glab\s+mr\s+(?:merge|accept)")
# The hook's check before #443, on the raw payload: whatever it blocked stays blocked.
LEGACY_MERGE = re.compile(r'"command"\s*:\s*"[^"]*gh pr merge')
# Exempt from the merge floor: programs that only print, count or search their text...
INERT_PROGRAMS = frozenset(
    {
        ":",
        "cat",
        "echo",
        "egrep",
        "fgrep",
        "grep",
        "head",
        "printf",
        "tail",
        "true",
        "wc",
    }
)
# ...and subcommands that carry text (a message, a title, a body) without running it.
TEXT_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {("git", "commit")}
    | {
        ("gh", group, verb)
        for group in ("issue", "pr")
        for verb in ("create", "edit", "comment", "view")
    }
    | {
        ("glab", group, verb)
        for group in ("issue", "mr")
        for verb in ("create", "update", "note", "view")
    }
)
# The strict lexer rejects these in command position: they open compound commands.
RESERVED_WORDS = frozenset(
    {
        "!",
        "{",
        "}",
        "[[",
        "]]",
        "case",
        "coproc",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "fi",
        "for",
        "function",
        "if",
        "in",
        "select",
        "then",
        "time",
        "until",
        "while",
    }
)
STRICT_WORD_END = " \t\n;&|<>()"
# Longest first: each tuple is matched in order.
REDIRECTIONS = ("<<<", "<<-", "<<", "<>", "<&", "<", ">>", ">&", ">|", ">", "&>>", "&>")
COMMAND_SEPARATORS = ("&&", "||", "|&", ";", "&", "|")
ASSIGNMENT_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\+?=|\[)")
FD_NUMBER = re.compile(r"[0-9]+")
CREATE_TEXT = re.compile(r"\bgh\s+pr\s+(?:create|new)\b|\bglab\s+mr\s+(?:create|new)\b")
# A create fragment that tokens cannot decide may name its branch with one of these flags.
HEAD_OPTION_TEXT = re.compile(r"--head|--source-branch|(?<![^\s'\"])-[A-Za-z]*[sH]")
# `gh pr create` and `glab mr create` shorthands that take a value; in a pflag cluster such
# a flag ends it.
GH_VALUE_SHORTS = frozenset("aBbFHlmprRtT")
GLAB_VALUE_SHORTS = frozenset("abdHilmRst")
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


class OutsideSubset(Exception):
    """Команда выходит за подмножество bash, которое разбирает строгий лексер."""


class StrictLexer:
    """Строгий лексер небольшого подмножества bash: argv simple commands без тел heredoc.

    Поддержаны слова с `'…'`, `"…"` (экранирование `\\"`, `\\\\`, `` \\` ``), `\\`-экранирование
    и перенос `\\` + перевод строки, разделители (перевод строки, `;`, `&`, `&&`, `|`, `||`,
    `|&`), перенаправления с номером дескриптора и `>|`, here-string `<<<` и heredoc `<<`/`<<-`.
    Всё остальное — `$`, обратная кавычка, комментарий, скобки, `!`, `{`, `}` и другие
    зарезервированные слова в позиции команды, `;;`, присваивания перед программой,
    незакрытые кавычки, heredoc без разделителя или тело без кавычек с `$`, `` ` `` или `\\` —
    даёт `OutsideSubset`.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.index = 0
        # (delimiter, strip leading tabs, literal body) of operators whose body is not read yet.
        self.heredocs: list[tuple[str, bool, bool]] = []

    def commands(self) -> list[list[str]]:
        """argv каждой simple command текста по порядку."""
        text = self.text
        commands: list[list[str]] = []
        argv: list[str] = []
        while True:
            self._skip_blanks()
            if self.index >= len(text):
                break
            ends = True
            if text[self.index] in "()":
                raise OutsideSubset
            if text[self.index] == "\n":
                self.index += 1
                self._read_bodies()
            elif (operator := self._operator(REDIRECTIONS)) is not None:
                self._redirection(operator)
                ends = False
            elif (operator := self._operator(COMMAND_SEPARATORS)) is not None:
                if operator == ";" and text.startswith((";", "&"), self.index):
                    raise OutsideSubset  # `;;`, `;&`: case terminators
            else:
                self._add_word(argv)
                ends = False
            if ends and argv:
                commands.append(argv)
                argv = []
        if self.heredocs:
            raise OutsideSubset
        return [*commands, argv] if argv else commands

    def _skip_blanks(self) -> None:
        """Пропустить пробелы, табуляции и переносы `\\` + перевод строки."""
        while True:
            if self.index < len(self.text) and self.text[self.index] in " \t":
                self.index += 1
            elif self.text.startswith("\\\n", self.index):
                self.index += 2
            else:
                return

    def _operator(self, operators: tuple[str, ...]) -> str | None:
        """Прочитать первый из `operators`, с которого начинается текст в позиции."""
        for operator in operators:
            if self.text.startswith(operator, self.index):
                self.index += len(operator)
                return operator
        return None

    def _add_word(self, argv: list[str]) -> None:
        """Прочитать слово simple command в `argv`; номер дескриптора перед `<`/`>` пропустить."""
        start = self.index
        word = self._word()
        raw = self.text[start : self.index]
        if FD_NUMBER.fullmatch(raw) and self.text.startswith(("<", ">"), self.index):
            return
        if not argv and (word in RESERVED_WORDS or ASSIGNMENT_WORD.match(raw)):
            raise OutsideSubset
        argv.append(word)

    def _redirection(self, operator: str) -> None:
        """Прочитать цель перенаправления; для heredoc запомнить разделитель."""
        self._skip_blanks()
        if self.index >= len(self.text) or self.text[self.index] in STRICT_WORD_END:
            raise OutsideSubset  # no target word, as in `<(…)` and `>(…)`
        start = self.index
        word = self._word()
        if operator in ("<<", "<<-"):
            literal = any(char in self.text[start : self.index] for char in "'\"\\")
            self.heredocs.append((word, operator == "<<-", literal))

    def _word(self) -> str:
        """Прочитать слово с позиции и вернуть его значение без кавычек."""
        text = self.text
        if text[self.index] == "#":
            raise OutsideSubset  # a comment
        chars: list[str] = []
        while self.index < len(text) and text[self.index] not in STRICT_WORD_END:
            char = text[self.index]
            if char == "'":
                end = text.find("'", self.index + 1)
                if end < 0 or "`" in text[self.index : end]:
                    raise OutsideSubset
                chars.append(text[self.index + 1 : end])
                self.index = end + 1
            elif char == '"':
                self.index += 1
                chars.append(self._double_quoted())
            elif char == "\\":
                escaped = text[self.index + 1 : self.index + 2]
                if not escaped or escaped in "$`":
                    raise OutsideSubset
                chars.append("" if escaped == "\n" else escaped)
                self.index += 2
            elif char in "$`":
                raise OutsideSubset
            else:
                chars.append(char)
                self.index += 1
        return "".join(chars)

    def _double_quoted(self) -> str:
        """Прочитать текст в двойных кавычках после открывающей кавычки."""
        text = self.text
        chars: list[str] = []
        while self.index < len(text):
            char = text[self.index]
            if char == '"':
                self.index += 1
                return "".join(chars)
            if char in "$`":
                raise OutsideSubset
            escaped = text[self.index + 1 : self.index + 2] if char == "\\" else ""
            if escaped == "\n":
                self.index += 2
            elif escaped in ('"', "\\", "`"):
                chars.append(escaped)
                self.index += 2
            else:
                chars.append(char)
                self.index += 1
        raise OutsideSubset  # an unterminated quote

    def _read_bodies(self) -> None:
        """Прочитать тела ожидающих heredoc в порядке операторов со строки после перевода."""
        text = self.text
        for delimiter, strip_tabs, literal in self.heredocs:
            while True:
                if self.index >= len(text):
                    raise OutsideSubset  # no delimiter line
                end = text.find("\n", self.index)
                end = len(text) if end < 0 else end
                line = text[self.index : end]
                self.index = end + 1
                if strip_tabs:
                    line = line.lstrip("\t")
                if line == delimiter:
                    break
                if not literal and any(char in line for char in "$`\\"):
                    raise OutsideSubset
        self.heredocs = []


def strict_commands(command: str) -> list[list[str]] | None:
    """argv simple commands строгого лексера; None — команда вне его подмножества bash."""
    try:
        return StrictLexer(command).commands()
    except OutsideSubset:
        return None


def fully_inert(command: str) -> bool:
    """Строгий лексер принял команду, и каждая её simple command — инертная из allowlist."""
    commands = strict_commands(command)
    return commands is not None and all(
        argv[0] in INERT_PROGRAMS
        or tuple(argv[:2]) in TEXT_COMMANDS
        or tuple(argv[:3]) in TEXT_COMMANDS
        for argv in commands
    )


def _merge_floor(command: str) -> bool:
    """Есть ли merge-текст в команде или в ней же без переносов `\\` + перевод строки."""
    return bool(
        MERGE_FLOOR.search(command) or MERGE_FLOOR.search(command.replace("\\\n", ""))
    )


def _shlex_words(command: str) -> list[str]:
    """Слова команды по shlex, прочитанные до первой ошибки разбора."""
    lexer = shlex.shlex(command.replace("\\\n", ""), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    words: list[str] = []
    try:
        for word in lexer:
            words.append(word)
    except ValueError:
        pass  # an unterminated quote: the words before it still decide
    return words


def _names_merge(words: list[str]) -> bool:
    """Образуют ли слова merge: gh,pr,merge, glab,mr,merge|accept или merge-текст в одном слове."""
    for index, word in enumerate(words):
        name, action = program(word), words[index + 1 : index + 3]
        if (
            MERGE_FLOOR.search(word)
            or (name == "gh" and action == ["pr", "merge"])
            or (name == "glab" and action in (["mr", "merge"], ["mr", "accept"]))
        ):
            return True
    return False


def merge_blocked(command: str) -> bool:
    """Запретить ли команду: merge-текст вне полностью инертной команды или merge по словам.

    Слова берутся у строгого лексера, а если он команду не принял — у shlex; так ловится
    merge, разбитый кавычками, которого нет в тексте команды.
    """
    if _merge_floor(command):
        return not fully_inert(command)
    commands = strict_commands(command)
    groups = [_shlex_words(command)] if commands is None else commands
    return any(_names_merge(words) for words in groups)


def _option(
    args: list[str], long: str, short: str, value_shorts: frozenset[str]
) -> str | None:
    """pflag-значение флага в `args`: `--long X`, `--long=X`, `-sX`, `-s=X`, `-fs X`.

    None — флага нет, "" — флаг без значения. Разбор останавливается на `--`; значение
    другого короткого флага (`-t -s`) флагом не считается.
    """
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            break
        if arg == long:
            return args[index] if index < len(args) else ""
        if arg.startswith(long + "="):
            return arg[len(long) + 1 :]
        if not short or not arg.startswith("-") or arg.startswith("--"):
            continue
        for position, char in enumerate(arg[1:], start=2):
            if char not in value_shorts:
                continue
            value = arg[position:]
            if char == short:
                value = value.removeprefix("=")
                return value or (args[index] if index < len(args) else "")
            if not value:
                index += 1  # this flag's value is the next argument
            break
    return None


def create_heads(command: str) -> set[str | None]:
    """Ветки PR/MR, которые создаёт команда: None — ветка checkout, "" — ветку не определить.

    Ветка берётся из argv самой create-команды (или её алиаса `new`): у gh — `-H`/`--head`
    (без префикса `owner:`), у glab — `-s`/`--source-branch`; glab `-H`/`--head` задаёт
    репозиторий, а не ветку. Create-текст в непрозрачном фрагменте даёт "", если во фрагменте
    есть флаг ветки, иначе None.
    """
    parsed = parse(command)
    heads: set[str | None] = set()
    for argv in parsed.commands:
        for start in positions(argv):
            name = program(argv[start])
            action = argv[start + 1 : start + 3]
            if name == "gh" and action in (["pr", "create"], ["pr", "new"]):
                head = _option(argv[start + 3 :], "--head", "H", GH_VALUE_SHORTS)
                heads.add(None if head is None else head.rsplit(":", 1)[-1])
            elif name == "glab" and action in (["mr", "create"], ["mr", "new"]):
                args = argv[start + 3 :]
                heads.add(_option(args, "--source-branch", "s", GLAB_VALUE_SHORTS))
    heads.update(
        "" if HEAD_OPTION_TEXT.search(fragment) else None
        for fragment in parsed.opaque
        if CREATE_TEXT.search(fragment)
    )
    return heads


def merge_exit_code(raw: str) -> int:
    """Код merge-блока для payload хука: 2 — запрет, 0 — разрешение.

    Решает `tool_input.command`; payload без строковой команды проверяется по сырому тексту,
    чтобы нераспознанный ввод с merge-текстом блокировался (fail closed). Payload, который
    блокировала проверка до #443, блокируется, даже если в команде merge-текста нет.
    """
    try:
        data: object = json.loads(raw)
    except ValueError:
        data = None
    tool_input = data.get("tool_input") if isinstance(data, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 2 if MERGE_FLOOR.search(raw) else 0
    if LEGACY_MERGE.search(raw) and not _merge_floor(command):
        return 2
    return 2 if merge_blocked(command) else 0


def main(argv: list[str]) -> int:
    """CLI для shell-хуков: `pr_commands.py merge < payload.json`.

    Разрешение — код 0 и слово `allow` в stdout: частичная копия модуля, которая завершается
    без решения, слова не печатает, и хук блокирует команду.
    """
    if argv[1:] != ["merge"]:
        print("usage: pr_commands.py merge < hook-payload.json", file=sys.stderr)
        return 2
    code = merge_exit_code(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    if not code:
        print("allow")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
