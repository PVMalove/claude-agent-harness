"""Menu language of the console: Russian or English labels for the section menu, the command
groups and the menu frame titles. Command titles come from the catalogs and stay Russian.

Stdlib-only like the rest of the textual-free seam. The default comes from `HARNESS_CONSOLE_LANG`
(`ru` or `en`); the running console switches it with F2."""

from __future__ import annotations

import os
import re

from .i18n_strings import EN, EN_PHRASES, EN_PREFIXES, FACT_LABELS_EN, RU_FROM_EN

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


# A jump names the section it opens; a few sections are named by the action instead.
_JUMPS = {
    "Configuration": {"ru": "Открыть настройку", "en": "Open setup"},
}


def jump_label(language: str, section: str) -> str:
    """The label of a jump to `section`, e.g. `Обновить Harness` or `Открыть настройку`."""
    return _JUMPS.get(section, {}).get(language) or section_label(language, section)


def count_noun(
    language: str, count: int, ru: tuple[str, str, str], en: tuple[str, str]
) -> str:
    """`count` with its noun in the right form: `21 проверка`, `3 проверки`, `27 проверок`,
    `1 check`, `27 checks`. `ru` holds the forms for 1, 2-4 and 5+, `en` for 1 and the rest."""
    if language == "en":
        return f"{count} {en[0] if count == 1 else en[1]}"
    if count % 10 == 1 and count % 100 != 11:
        noun = ru[0]
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        noun = ru[1]
    else:
        noun = ru[2]
    return f"{count} {noun}"


def checks_count(language: str, total: int, skipped: int) -> str:
    """`27 проверок · 11 пропущено` / `27 checks · 11 skipped`."""
    checks = count_noun(
        language, total, ("проверка", "проверки", "проверок"), ("check", "checks")
    )
    if language == "en":
        return f"{checks} · {skipped} skipped"
    return f"{checks} · {skipped} пропущено"


def fact_label(language: str, label: str) -> str:
    """The label of a fact in a context card (`Ветка`, `Объём`) in `language`."""
    return FACT_LABELS_EN.get(label, label) if language == "en" else label


def group_title(language: str, group: str) -> str:
    return group if language == "ru" else _GROUPS_EN.get(group, group)


def ui(language: str, key: str) -> str:
    return _UI[key][language]


def palette_command(language: str, title: str, help_text: str) -> tuple[str, str]:
    """A palette entry's title and help text in `language`; unknown entries are kept as they are."""
    if language == "ru" and title in _PALETTE_RU:
        return _PALETTE_RU[title]
    return title, help_text


# Section titles are drawn in capitals: a capitalised source finds its English in capitals too.
_EN_UPPER = {russian.upper(): english.upper() for russian, english in EN.items()}


def _english(text: str) -> str | None:
    if text in EN:
        return EN[text]
    return _EN_UPPER.get(text)


def tr(language: str, text: str) -> str:
    """A fixed interface string in `language`. Unknown text (data, paths, CLI output) is returned
    as it is, so only the strings listed in `i18n_strings` ever change."""
    if language == "en":
        english = _english(text)
        if english is not None:
            return english
        head, separator, rest = text.partition(" · ")
        english = _english(head) if separator else None
        if english is not None:
            return english + separator + rest
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
