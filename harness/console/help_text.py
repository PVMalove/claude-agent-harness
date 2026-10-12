"""The console's Help section: a short Russian guide to the sections, keys and safety rules.

Plain Markdown, stdlib-only, so it is testable without textual; screens/help.py renders it."""

from __future__ import annotations

HELP_MARKDOWN = """\
# Справка по пульту

Пульт — интерактивная оболочка над CLI `harness` и coordinator оркестрации. Рядом с каждым
действием показан CLI-эквивалент: пульт запускает тот же процесс и не повторяет его логику.

## Разделы

| Раздел | Что внутри |
| --- | --- |
| Diagnostics | Полный отчёт `harness health`, онлайн-проверки, `--fix` и экспорт в Markdown. |
| Harness | Состояние установки и использование пайплайна; команды CLI: init, update (в том числе `--force-managed-files` и `--force-seed-files`), diff, adopt, очистка soft/hard (план и применение), удаление харнесса, registry, list, health, Repo Map, ledger. |
| Orchestration | Статистика пайплайна, история batch по состояниям (Enter открывает хронологию) и команды coordinator. |
| Reports | Отчёты ролей из леджера с фильтрами, хронология batch и QA-логи. |
| Repo Map | Карта репозитория для HEAD: сводка, дерево файлов, символы, связи и хабы. |
| Настройка конфигурации | Шаги `project.json` и `orchestration.json`, черновик, проверка и diff, версии и откат, необязательные CLI-подсказки. |
| Help | Эта справка. |

## Клавиши

Заголовок показывает версию консоли, ветку и commit выбранного проекта; commit обновляется
при смене цели. «Открыть проект» (Open project) на главном экране проверяет путь и план
init/adopt/update. Установка требует подтверждения и делает выбранный Git-проект целью всех
экранов. Конфликты discovery-путей и управляемых файлов нужно разобрать до установки.

| Клавиша | Действие |
| --- | --- |
| `↑` `↓` `Enter` | Выбор пункта меню |
| `1` | Открыть или подключить проект |
| `2` | Открыть экран обновления Harness: версии, дрейф, diff, режим и подтверждённый запуск |
| `3`–`9` | Открыть раздел на главном экране: Diagnostics, Harness, Orchestration, Reports, Repo Map, Configuration, Help |
| `r` / `o` | Офлайн- / онлайн-проверки на главном экране |
| `p` | Открыть или подключить проект |
| `y` | Скопировать полную CLI-строку выбранной команды (Harness, Orchestration) |
| `Tab` | Следующий элемент экрана |
| `Esc` | Назад или отмена |
| `e` | Экспорт в Markdown |
| `j` | Экспорт Repo Map в JSON |
| `b` | Хронология batch в Reports, построение карты в Repo Map |
| `F3` | QA-логи в Reports |
| `F1` | Эта справка с любого экрана |
| `F2` | Язык меню: русский / English (по умолчанию из `HARNESS_CONSOLE_LANG`) |
| `Ctrl+P` | Палитра команд |
| `Ctrl+Q` | Выход |

## Безопасность

- Команды из «Как исправить» пульт только показывает — запускайте их вручную.
- Перед необратимыми действиями пульт переспрашивает; отмена ничего не запускает.
- `ledger reset`, hard cleanup и удаление харнесса требуют ввести `RESET`, `HARD` или `UNINSTALL`.
- «Исправить» (Diagnostics) выполняет только `health --fix` и требует повторного нажатия.
- Экспорты сохраняются в `docs/tasks/` и никогда не перезаписываются.
- Мастер пишет JSON только после итогового подтверждения и сохраняет предыдущую версию.
- Версии находятся в `.harness/config_versions/`; они переживают cleanup. Откат восстанавливает оба файла, включая отсутствие файла.
- ИИ по умолчанию выключен. Проверка CLI не обращается к модели; запрос запускается отдельной кнопкой через существующий login Codex/Claude Code CLI.
- Недоступный CLI не блокирует мастер. ИИ-рекомендация принимается в черновик; автоматического запуска предложенных команд нет.

## Документация

- Команды и флаги: `harness --help`
- Контракт оркестрации и роли: `.harness/orchestration/playbook.md`
- Настройки `orchestration.json`: `.harness/orchestration/README.md`
"""


HELP_MARKDOWN_EN = """\
# Console guide

The console is an interactive shell over the `harness` CLI and the orchestration coordinator. Next
to each action the CLI equivalent is shown: the console runs the same process and does not repeat
its logic.

## Sections

| Section | What is inside |
| --- | --- |
| Diagnostics | The full `harness health` report, online checks, `--fix` and export to Markdown. |
| Harness | Installation state and pipeline usage; CLI commands: init, update (including `--force-managed-files` and `--force-seed-files`), diff, adopt, soft/hard cleanup (plan and apply), Harness removal, registry, list, health, Repo Map, ledger. |
| Orchestration | Pipeline statistics, batch history by state (Enter opens the timeline) and coordinator commands. |
| Reports | Role reports from the ledger with filters, the batch timeline and QA logs. |
| Repo Map | The repository map for HEAD: summary, file tree, symbols, relations and hubs. |
| Configuration | `project.json` and `orchestration.json` steps, draft, check and diff, versions and restore, optional CLI hints. |
| Help | This guide. |

## Keys

The header shows the console version, the branch and the commit of the selected project; the commit
follows the target when it changes. “Open project” on the home screen checks the path and the
init/adopt/update plan. Installation needs confirmation and makes the chosen Git project the target of
every screen. Conflicts in discovery paths and managed files must be resolved before installing.

| Key | Action |
| --- | --- |
| `↑` `↓` `Enter` | Choose a menu item |
| `1` | Open or connect a project |
| `2` | Open the Harness update screen: versions, drift, diff, mode and a confirmed run |
| `3`–`9` | Open a section from the home screen: Diagnostics, Harness, Orchestration, Reports, Repo Map, Configuration, Help |
| `r` / `o` | Offline / online checks on the home screen |
| `p` | Open or connect a project |
| `y` | Copy the full CLI line of the selected command (Harness, Orchestration) |
| `Tab` | Next element on the screen |
| `Esc` | Back or cancel |
| `e` | Export to Markdown |
| `j` | Export the Repo Map to JSON |
| `b` | Batch timeline in Reports, build the map in Repo Map |
| `F3` | QA logs in Reports |
| `F1` | This guide from any screen |
| `F2` | Menu language: Russian / English (default from `HARNESS_CONSOLE_LANG`) |
| `Ctrl+P` | Command palette |
| `Ctrl+Q` | Quit |

## Safety

- The console only shows the “How to fix” commands — run them yourself.
- Before irreversible actions the console asks again; cancelling runs nothing.
- `ledger reset`, hard cleanup and Harness removal require typing `RESET`, `HARD` or `UNINSTALL`.
- “Fix” (Diagnostics) runs only `health --fix` and needs a second press.
- Exports are saved to `docs/tasks/` and are never overwritten.
- The wizard writes JSON only after the final confirmation and keeps the previous version.
- Versions live in `.harness/config_versions/`; they survive cleanup. Restore brings back both files, including a missing one.
- AI is off by default. The CLI check does not call the model; a request runs from a separate button through the existing Codex/Claude Code CLI login.
- An unavailable CLI does not block the wizard. An AI recommendation is accepted into the draft; suggested commands are never run automatically.

## Documentation

- Commands and flags: `harness --help`
- Orchestration contract and roles: `.harness/orchestration/playbook.md`
- `orchestration.json` settings: `.harness/orchestration/README.md`
"""


def help_markdown(language: str) -> str:
    """The guide in `language`."""
    return HELP_MARKDOWN_EN if language == "en" else HELP_MARKDOWN
