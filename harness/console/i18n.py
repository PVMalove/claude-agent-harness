"""Menu language of the console: Russian or English labels for the section menu, the command
groups and the menu frame titles. Command titles come from the catalogs and stay Russian.

Stdlib-only like the rest of the textual-free seam. The default comes from `HARNESS_CONSOLE_LANG`
(`ru` or `en`); the running console switches it with F2."""

from __future__ import annotations

import os
import re

from .i18n_strings import EN, EN_PHRASES, EN_PREFIXES, RU_FROM_EN

LANGUAGES = ("ru", "en")
LANGUAGE_ENV = "HARNESS_CONSOLE_LANG"

_SECTIONS = {
    "Home": {"ru": "Главная", "en": "Home"},
    "Open project": {"ru": "Открыть проект", "en": "Open project"},
    "Update Harness": {"ru": "Обновить Harness", "en": "Update Harness"},
    "Diagnostics": {"ru": "Диагностика", "en": "Diagnostics"},
    "Harness": {"ru": "Harness", "en": "Harness"},
    "Orchestration": {"ru": "Оркестрация", "en": "Orchestration"},
    "Reports": {"ru": "Отчёты", "en": "Reports"},
    "Repo Map": {"ru": "Repo Map", "en": "Repo Map"},
    "Configuration": {"ru": "Настройка", "en": "Configuration"},
    "Help": {"ru": "Справка", "en": "Help"},
}

# Command groups are keyed by their Russian title, the id the screens already use.
_GROUPS_EN = {
    "Установка и обновление": "Install & update",
    "Проверки": "Checks",
    "Скиллы": "Skills",
    "Repo Map": "Repo Map",
    "Очистка и удаление": "Cleanup & removal",
    "Ledger": "Ledger",
    "Прочее": "Other",
    "Batch": "Batch",
    "Решения": "Decisions",
    "Dispatch": "Dispatch",
    "QA": "QA",
    "Риск": "Risk",
    "Context Package": "Context Package",
}

_UI = {
    "commands": {"ru": "Команды", "en": "Commands"},
    "language": {"ru": "Язык: русский", "en": "Language: English"},
    "offline": {"ru": "Офлайн", "en": "Offline"},
    "online": {"ru": "Онлайн", "en": "Online"},
    "palette": {"ru": "Меню", "en": "Menu"},
    "palette_placeholder": {"ru": "Найти команду…", "en": "Search commands…"},
}

# Textual's built-in palette commands and the console's own: title -> (Russian title, Russian help).
_PALETTE_RU = {
    "Keys": ("Клавиши", "Справка по клавишам выбранного виджета"),
    "Maximize": ("Развернуть", "Развернуть выбранный виджет"),
    "Minimize": ("Свернуть", "Вернуть выбранному виджету обычный размер"),
    "Quit": ("Выход", "Закрыть консоль"),
    "Screenshot": ("Снимок экрана", "Сохранить SVG-снимок текущего экрана"),
    "Theme": ("Тема", "Сменить цветовую тему"),
    "Language / Язык": (
        "Language / Язык",
        "Переключить язык меню: русский / English (F2)",
    ),
    "Restart console": (
        "Перезапустить консоль",
        "Перезагрузить код консоли, оставив выбранный проект",
    ),
}


def default_language() -> str:
    value = os.environ.get(LANGUAGE_ENV, "").strip().lower()
    return value if value in LANGUAGES else "ru"


def next_language(language: str) -> str:
    return LANGUAGES[(LANGUAGES.index(language) + 1) % len(LANGUAGES)]


def section_label(language: str, key: str) -> str:
    return _SECTIONS.get(key, {}).get(language, key)


def group_title(language: str, group: str) -> str:
    return group if language == "ru" else _GROUPS_EN.get(group, group)


def ui(language: str, key: str) -> str:
    return _UI[key][language]


def palette_command(language: str, title: str, help_text: str) -> tuple[str, str]:
    """A palette entry's title and help text in `language`; unknown entries are kept as they are."""
    if language == "ru" and title in _PALETTE_RU:
        return _PALETTE_RU[title]
    return title, help_text


def tr(language: str, text: str) -> str:
    """A fixed interface string in `language`. Unknown text (data, paths, CLI output) is returned
    as it is, so only the strings listed in `i18n_strings` ever change."""
    if language == "en":
        if text in EN:
            return EN[text]
        head, separator, rest = text.partition(" · ")
        if separator and head in EN:
            return EN[head] + separator + rest
        for prefix, english in EN_PREFIXES.items():
            if text.startswith(prefix):
                return english + text[len(prefix) :]
        if "›" in text:
            # Breadcrumbs: translate the parts, keep whatever spacing surrounds each separator.
            parts = re.split(r"(\s*›\s*)", text)
            return "".join(
                part if index % 2 else EN.get(part, part)
                for index, part in enumerate(parts)
            )
        return text
    return RU_FROM_EN.get(text, text)


def _pattern(phrase: str) -> re.Pattern[str]:
    """The phrase as a whole word(s): `нет` must not match inside `нетронутый`."""
    start = r"(?<!\w)" if phrase[0].isalnum() else ""
    end = r"(?!\w)" if phrase[-1].isalnum() else ""
    return re.compile(start + re.escape(phrase) + end)


_PHRASES = [
    (_pattern(russian), english)
    for russian, english in sorted(EN_PHRASES.items(), key=lambda item: -len(item[0]))
]


def tr_text(language: str, text: str) -> str:
    """Dynamic text (status lines, summaries): the whole string if it is a known one, otherwise
    its known phrases. Russian is the source language, so only English rewrites anything."""
    if language != "en":
        return text
    exact = tr(language, text)
    if exact != text:
        return exact
    for pattern, english in _PHRASES:
        text = pattern.sub(english, text)
    return text
