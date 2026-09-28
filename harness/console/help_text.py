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
| Harness | Состояние установки и использование пайплайна; команды CLI: init, update, diff, adopt, registry, list, health, cleanup, Repo Map, ledger. |
| Orchestration | Статистика пайплайна, история batch по состояниям (Enter открывает хронологию) и команды coordinator. |
| Reports | Отчёты ролей из леджера с фильтрами, хронология batch и QA-логи. |
| Repo Map | Карта репозитория для HEAD: сводка, дерево файлов, символы, связи и хабы. |
| Help | Эта справка. |

## Клавиши

| Клавиша | Действие |
| --- | --- |
| `↑` `↓` `Enter` | Выбор пункта меню |
| `Tab` | Следующий элемент экрана |
| `Esc` | Назад или отмена |
| `e` | Экспорт в Markdown |
| `j` | Экспорт Repo Map в JSON |
| `b` | Хронология batch в Reports, построение карты в Repo Map |
| `F3` | QA-логи в Reports |
| `F1` | Эта справка с любого экрана |
| `Ctrl+P` | Палитра команд |
| `Ctrl+Q` | Выход |

## Безопасность

- Команды из «Как исправить» пульт только показывает — запускайте их вручную.
- Перед необратимыми действиями пульт переспрашивает; отмена ничего не запускает.
- `ledger reset` и hard cleanup требуют ввести `RESET` или `HARD`.
- «Apply fixes» выполняет только `health --fix` и требует повторного нажатия.
- Экспорты сохраняются в `docs/tasks/` и никогда не перезаписываются.

## Документация

- Справочник харнесса: `.harness/docs/harness-guide.md`
- Backend-оркестрация: `.harness/docs/backend-orchestration.md`
- Настройки `orchestration.json`: `.harness/orchestration/README.md`
"""
