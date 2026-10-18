"""Закреплённые версии tree-sitter и грамматик offline parser bundle (ADR 0008).

Один источник для сборщика `scripts/build_parser_bundle.py` и установки парсеров из консоли.
Версии меняются только вместе с релизом харнесса, `parser_wheels.json` и ADR 0008."""

from __future__ import annotations

from dataclasses import dataclass

CORE_DISTRIBUTION = "tree_sitter"
CORE_VERSION = "0.26.0"
CORE_ABI_RANGE = "13-15"


@dataclass(frozen=True)
class GrammarPin:
    """Закреплённая грамматика: имя в lock, дистрибутив, версия, ABI и расширения файлов."""

    name: str
    distribution: str
    version: str
    abi: int
    extensions: tuple[str, ...]


GRAMMARS = (
    GrammarPin("python", "tree_sitter_python", "0.25.0", 15, (".py",)),
    GrammarPin("typescript", "tree_sitter_typescript", "0.23.2", 14, (".ts",)),
    GrammarPin("tsx", "tree_sitter_typescript", "0.23.2", 14, (".tsx",)),
    GrammarPin("javascript", "tree_sitter_javascript", "0.25.0", 15, (".js", ".jsx")),
    GrammarPin("go", "tree_sitter_go", "0.25.0", 15, (".go",)),
    GrammarPin("java", "tree_sitter_java", "0.23.5", 14, (".java",)),
    GrammarPin("csharp", "tree_sitter_c_sharp", "0.23.5", 15, (".cs",)),
)


@dataclass(frozen=True)
class Language:
    """Язык проекта и грамматики, которые его разбирают."""

    key: str
    label: str
    grammars: tuple[str, ...]


# TypeScript is one choice: its dialects (.ts, .tsx) share a wheel and are used together.
LANGUAGES = (
    Language("python", "Python", ("python",)),
    Language("typescript", "TypeScript", ("typescript", "tsx")),
    Language("javascript", "JavaScript", ("javascript",)),
    Language("go", "Go", ("go",)),
    Language("java", "Java", ("java",)),
    Language("csharp", "C#", ("csharp",)),
)
