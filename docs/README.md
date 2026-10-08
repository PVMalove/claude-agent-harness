# Документация Agent Harness

Корневой `docs/` описывает исходный репозиторий и не устанавливается в целевые проекты.
Проектные руководства поставляются отдельно из `harness/project/docs-agents/`. Если их текст
совпадает с документами здесь, корневой файл всё равно не становится источником установки.
Руководства по харнессу и backend-оркестрации лежат в `harness/docs/` и устанавливаются в
`.harness/docs/` как часть управляемого снимка.

| Раздел | Содержимое |
| --- | --- |
| [Архитектура](./ARCHITECTURE.md) | Слои, discovery, backend-оркестрация и границы процессов. |
| [ADR](./adr/) | Девять действующих архитектурных контрактов по доменам. |
| [Диаграммы](./diagrams/README.md) | Archify JSON, интерактивные HTML и PNG. |
| [Справочник харнесса](../harness/docs/harness-guide.md) | Установка, команды CLI, `health`, `console`, skills и hooks. |
| [Backend-оркестрация](../harness/docs/backend-orchestration.md) | Конфиг, coordinator, роли и lifecycle batch. |
| [Technical English](../harness/docs/technical-english.md) | Общий контракт английской координации агентов, доставляемый в каждую установку. |
| [Technical Russian](./technical-russian.md) | Правила стандарта ASD-STE100 и Keep a Changelog для документации на русском языке. |
| [Руководства и агенты](./agents/README.md) | Git, тикеты, артефакты, worktrees, релизы и русские описания агентов code-review-spec, code-review-standards и pr-composer. |
| [Skills](./skills/README.md) | Русские описания first-party, global, role и vendor skills. |
| [Hooks](./hooks/) | Русские описания двенадцати проектных hooks. |
| [Runtime discovery](./runtime-discovery.md) | Обнаружение skills в Claude Code и Codex. |

[releases.md](./agents/releases.md) описывает порядок выпуска версии. Он же описывает проверку
приватных терминов мейнтейнера: локальный файл `.private-terms.txt` и секрет репозитория
`HARNESS_PRIVATE_TERMS`. Операционные правила целевого проекта — в
[harness-guide.md](../harness/docs/harness-guide.md) и
[git-workflow.md](./agents/git-workflow.md).
