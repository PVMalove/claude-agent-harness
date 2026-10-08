# Документация Agent Harness

Корневой `docs/` описывает исходный репозиторий и не устанавливается в целевые проекты.
Проектные руководства поставляются отдельно из `harness/project/docs-agents/`; совпадение
их текста с документами здесь не делает корневой файл источником установки. Руководства по
харнессу и backend-оркестрации лежат в `harness/docs/` и устанавливаются в `.harness/docs/` как
часть управляемого снимка.

| Раздел | Содержимое |
| --- | --- |
| [Архитектура](./ARCHITECTURE.md) | Слои, discovery, backend orchestration и границы процессов. |
| [ADR](./adr/) | Девять действующих архитектурных контрактов по доменам. |
| [Диаграммы](./diagrams/README.md) | Archify JSON, интерактивные HTML и PNG. |
| [Справочник харнесса](../harness/docs/harness-guide.md) | Установка, команды CLI, `health`, `console`, скилы и hooks. |
| [Backend-оркестрация](../harness/docs/backend-orchestration.md) | Конфиг, coordinator, роли и lifecycle batch. |
| [Technical English](../harness/docs/technical-english.md) | Общий контракт английской координации агентов, доставляемый в каждую установку. |
| [Technical Russian](./technical-russian.md) | Правила стандарта ASD-STE100 и Keep a Changelog для документации на русском языке. |
| [Руководства и агенты](./agents/README.md) | Git, тикеты, артефакты, worktrees, релизы и русские описания агентов code-review-spec, code-review-standards и pr-composer. |
| [Skills](./skills/README.md) | Русские описания first-party, global, role и vendor skills. |
| [Hooks](./hooks/) | Русские описания двенадцати проектных hooks. |
| [Runtime discovery](./runtime-discovery.md) | Обнаружение skills в Claude Code и Codex. |

Порядок выпуска версии описан в [releases.md](./agents/releases.md). Там же описана проверка
приватных терминов мейнтейнера: локальный файл `.private-terms.txt` и секрет репозитория
`HARNESS_PRIVATE_TERMS`. Операционные правила
целевого проекта — в [harness-guide.md](../harness/docs/harness-guide.md) и
[git-workflow.md](./agents/git-workflow.md).
