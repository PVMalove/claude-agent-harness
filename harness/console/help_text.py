"""The console's Help section: a short Russian guide to the sections, keys and safety rules.

Plain Markdown, stdlib-only, so it is testable without textual; screens/help.py renders it."""

from __future__ import annotations

HELP_MARKDOWN = """\
# Справка по пульту

Пульт — интерактивная оболочка над CLI `harness` и coordinator оркестрации. Рядом с каждым
действием показан CLI-эквивалент: пульт запускает тот же процесс и не повторяет его логику.

## Окно консоли

- Верхняя панель показывает версию консоли, проект и ветку.
- Главное меню слева открывает разделы клавишами `0`–`9` или стрелками и `Enter`.
  Текущий раздел отмечен линией слева; подчёркивание показывает фокус клавиатуры.
- В узком терминале меню открывается кнопкой `≡`; контекст следует под содержимым.
- Хлебные крошки показывают путь. Все сегменты, кроме текущего, открываются как ссылки.
- Контекстная панель содержит факты выбранного проекта; длинные значения доступны с прокруткой.
- Нижняя строка показывает клавиши текущего экрана. `Tab` и `Shift+Tab` перемещают фокус,
  `Esc` возвращает назад или отменяет действие. На главной `Esc` ничего не закрывает.

Выбор пункта меню, вкладки или сессии не запускает команду.

## Разделы

| Клавиша | Раздел | Что внутри |
| --- | --- | --- |
| `0` | Главная | Проблемы проекта, рабочие сессии, проверки и контекст проекта. |
| `1` | Открыть проект | Путь, проверка Git-проекта и план init/adopt/update перед подтверждением. |
| `2` | Обновить Harness | Установленная и доступная версии, дрейф, diff, режим обновления и подтверждённый запуск. |
| `3` | Диагностика | Отчёт `harness health`, онлайн-проверки, `--fix` и экспорт в Markdown. |
| `4` | Harness | Установка и команды CLI: init, update (в том числе `--force-managed-files` и `--force-seed-files`), diff, adopt, очистка soft/hard (план и применение), удаление харнесса, registry, list, health, Repo Map, ledger. |
| `5` | Оркестрация | Статистика пайплайна, история batch по состояниям (Enter открывает хронологию) и команды coordinator. |
| `6` | Отчёты | Стадии и отчёты ролей из леджера с фильтрами (на узком терминале фильтры и батчи свёрнуты), хронология batch и QA-логи. |
| `7` | Repo Map | Карта для HEAD: сводка, дерево файлов со статусом парсера, символы, связи (на узком терминале — раскрывающийся блок) и хабы. Вкладка «Диагностики» проверяет полноту карты; «Настройки»: режим minimal / full, парсеры языков и память проекта. |
| `8` | Настройка | Шаги `project.json` и `orchestration.json`, черновик, проверка и diff, версии и откат, необязательные CLI-подсказки. |
| `9` | Справка | Окно консоли, разделы, клавиши, статусы, безопасность и документация. |

## Клавиши

Заголовок показывает версию консоли, ветку и commit выбранного проекта; commit обновляется
при смене цели. «Открыть проект» (Open project) на главном экране проверяет путь и план
init/adopt/update. Установка требует подтверждения и делает выбранный Git-проект целью всех
экранов. Конфликты discovery-путей и управляемых файлов нужно разобрать до установки.

| Клавиша | Действие |
| --- | --- |
| `↑` `↓` `Enter` | Выбор пункта меню |
| `0` | Вернуться на главный экран |
| `1` | Открыть или подключить проект |
| `2` | Открыть экран обновления Harness: версии, дрейф, diff, режим и подтверждённый запуск |
| `3`–`9` | Открыть соответствующий раздел из таблицы выше с любого экрана общего каркаса |
| `r` / `o` | Офлайн- / онлайн-проверки на главном экране; `r` в Reports обновляет списки |
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

## Статусы

Статус всегда показан символом и подписью. Цвет только дополняет их.

| Символ | Сессии и действия |
| --- | --- |
| ✔ | Принято или успешно; «Отчёт получен» ещё не означает, что он принят. |
| ▶ | Выполняется. |
| Ⅱ | Приостановлено, запускается, отменено или завершено — смотрите подпись. |
| ○ | Не начато или готово к запуску. |
| ✖ | Ошибка или работа заблокирована. Причина доступна в деталях. |
| ⚠ | Предупреждение, нет подтверждения или требуется разрешение. |

Статусы проверок: ✔ ok — успешно; ⚠ warn — предупреждение; ✖ fail — ошибка;
○ skipped — проверка пропущена, а не завершилась успешно. Отсутствие данных не заменяется нулём.

## Безопасность

- Команды из «Как исправить» пульт только показывает — запускайте их вручную.
- Перед необратимыми действиями пульт переспрашивает; отмена ничего не запускает.
- Harness показывает фактический план очистки: размер и число файлов по категориям, пропуски и дополнения hard. Изменившийся после подтверждения список удаления требует нового плана.
- Строки worktree показывают dirty counts и причины защиты. Удаление повторно проверяет активные batch, возраст, Git и сохранность коммитов на origin; ветка и PR остаются.
- stdout/stderr команд Harness поступают по мере чтения. Уход со страницы не останавливает уже запущенную команду; повторное открытие показывает тот же запуск и полный результат. Новый запуск доступен после фактического завершения.
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

## Console window

- The header shows the console version, project and branch.
- The main menu on the left opens sections with `0`–`9` or the arrow keys and `Enter`.
  A line on the left marks the current section; an underline marks keyboard focus.
- On a narrow terminal, `≡` opens the menu and context follows the content.
- Breadcrumbs show the path. Every segment except the current one is a link.
- The context panel shows facts about the selected project; long values remain accessible by scrolling.
- The footer shows the current screen's keys. `Tab` and `Shift+Tab` move focus;
  `Esc` goes back or cancels. On Home, `Esc` closes nothing.

Selecting a menu item, tab or session never runs a command.

## Sections

| Key | Section | What is inside |
| --- | --- | --- |
| `0` | Home | Project issues, working sessions, checks and project context. |
| `1` | Open project | Path, Git project checks and an init/adopt/update plan before confirmation. |
| `2` | Update Harness | Installed and available versions, drift, diff, update mode and a confirmed run. |
| `3` | Diagnostics | The full `harness health` report, online checks, `--fix` and export to Markdown. |
| `4` | Harness | Installation state and CLI commands: init, update (including `--force-managed-files` and `--force-seed-files`), diff, adopt, soft/hard cleanup (plan and apply), Harness removal, registry, list, health, Repo Map, ledger. |
| `5` | Orchestration | Pipeline statistics, batch history by state (Enter opens the timeline) and coordinator commands. |
| `6` | Reports | Stages and role reports from the ledger with filters (on a narrow terminal the filters and batches are folded), the batch timeline and QA logs. |
| `7` | Repo Map | The map for HEAD: summary, file tree with parser status, symbols, relations (a foldable block on a narrow terminal) and hubs. Diagnostics checks map completeness; Settings covers minimal / full mode, language parsers and project memory. |
| `8` | Configuration | `project.json` and `orchestration.json` steps, draft, check and diff, versions and restore, optional CLI hints. |
| `9` | Help | Console window, sections, keys, statuses, safety and documentation. |

## Keys

The header shows the console version, the branch and the commit of the selected project; the commit
follows the target when it changes. “Open project” on the home screen checks the path and the
init/adopt/update plan. Installation needs confirmation and makes the chosen Git project the target of
every screen. Conflicts in discovery paths and managed files must be resolved before installing.

| Key | Action |
| --- | --- |
| `↑` `↓` `Enter` | Choose a menu item |
| `0` | Back to the home screen |
| `1` | Open or connect a project |
| `2` | Open the Harness update screen: versions, drift, diff, mode and a confirmed run |
| `3`–`9` | Open the corresponding section in the table above from any shared-shell screen |
| `r` / `o` | Offline / online checks on the home screen; `r` in Reports refreshes the lists |
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

## Statuses

A status always has a symbol and a label. Colour is supplementary.

| Symbol | Sessions and actions |
| --- | --- |
| ✔ | Accepted or successful; “Report received” does not mean the report has been accepted. |
| ▶ | Running. |
| Ⅱ | Paused, starting, cancelled or finished; read the label. |
| ○ | Not started or ready to start. |
| ✖ | Error or work blocked. Details explain the reason. |
| ⚠ | Warning, unconfirmed state or permission pending. |

Check statuses: ✔ ok — successful; ⚠ warn — warning; ✖ fail — error;
○ skipped — the check was skipped, not successful. Missing data is never replaced with zero.

## Safety

- The console only shows the “How to fix” commands — run them yourself.
- Before irreversible actions the console asks again; cancelling runs nothing.
- Harness shows the actual cleanup plan: bytes and file counts by category, skipped paths and hard-mode additions. A deletion set changed after confirmation requires a new preview.
- Worktree rows show dirty counts and protection reasons. Removal rechecks active batches, age, Git and commits preserved on origin; it keeps the branch and PR.
- Harness stdout/stderr appear as they are read. Leaving the page keeps an already running command alive; reopening shows the same invocation and complete result. A new invocation is admitted only after physical completion.
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
