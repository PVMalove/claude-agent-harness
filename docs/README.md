# Документация Harness next

Корневой `docs/` — документация для разработчика исходного репозитория. Она не устанавливается в
целевые проекты, включая справочник харнесса и руководство по backend-оркестрации. Агентам
доставляются только их собственные контракты: проектные руководства из `harness/project/docs-agents/`,
контракты `harness/docs/project-memory.md` и `harness/docs/technical-english.md`, роли и playbook
оркестрации, скиллы и hooks. Даже когда текст проектного руководства совпадает с документом здесь,
корневой файл не становится источником установки.

| Раздел | Содержимое |
| --- | --- |
| [Архитектура](./ARCHITECTURE.md) | Слои, discovery, backend-оркестрация и границы процессов. |
| [ADR](./adr/) | Девять действующих архитектурных контрактов по доменам. |
| [Диаграммы](./diagrams/README.md) | Archify JSON, интерактивные HTML и PNG. |
| [HTML-макеты консоли](./design/console-mockups/README.md) | Принятые интерактивные макеты Harness console (дизайн-источник эпика #720). |
| [Справочник харнесса](./harness-guide.md) | Установка, команды CLI, `health`, `console`, skills и hooks. |
| [Настройка в консоли](./console-configuration.md) | Выбор целевого проекта, мастер JSON, версии, откат и необязательные CLI-подсказки. |
| [Backend-оркестрация](./backend-orchestration.md) | Конфиг, coordinator, роли и lifecycle batch. |
| [Technical English](../harness/docs/technical-english.md) | Общий контракт английской координации агентов, доставляемый в каждую установку. |
| [Technical Russian](./technical-russian.md) | Правила технического русского языка и формат Keep a Changelog. |
| [Руководства и агенты](./agents/README.md) | Git, тикеты, артефакты, worktrees, релизы и русские описания агентов code-review-spec, code-review-standards, pr-composer и test-author. |
| [Skills](./skills/README.md) | Русские описания first-party, global, role и vendor skills. |
| [Hooks](./hooks/) | Русские описания проектных hooks и вспомогательных скриптов, включая `run-test.sh`, `test-author-guard.py` и `test-author-preflight.sh`. |
| [Базовый замер 1.x](./perf/1.x-baseline/methodology.md) | Методика и сырые данные замера производительности до Harness Next. |
| [Runtime discovery](./runtime-discovery.md) | Обнаружение skills в Claude Code и Codex. |

[releases.md](./agents/releases.md) описывает порядок выпуска версии, а также проверку приватных
терминов мейнтейнера: локальный файл `.private-terms.txt` и секрет репозитория
`HARNESS_PRIVATE_TERMS`. Операционные правила целевого проекта — в
[harness-guide.md](./harness-guide.md) и [git-workflow.md](./agents/git-workflow.md).
