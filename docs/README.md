# Документация Agent Harness

Корневой `docs/` описывает исходный репозиторий и не устанавливается в целевые проекты.
Проектные руководства поставляются отдельно из `harness/project/docs-agents/`; совпадение
их текста с документами здесь не делает корневой файл источником установки.

| Раздел | Содержимое |
| --- | --- |
| [Архитектура](./ARCHITECTURE.md) | Слои, discovery, backend orchestration и границы процессов. |
| [ADR](./adr/) | Девять действующих архитектурных контрактов по доменам. |
| [Диаграммы](./diagrams/README.md) | Archify JSON, интерактивные HTML и PNG. |
| [Руководства](./agents/) | Установка, Git, тикеты, временные артефакты, оркестрация и релизы. |
| [Skills](./skills/README.md) | Русские описания first-party, global, role и vendor skills. |
| [Hooks](./hooks/) | Русские описания двенадцати проектных hooks. |
| [Агенты](./agents-ru/) | Русские описания code-review-spec, code-review-standards и pr-composer. |
| [Runtime discovery](./runtime-discovery.md) | Обнаружение skills в Claude Code и Codex. |

Порядок выпуска версии описан в [releases.md](./agents/releases.md). Операционные правила
целевого проекта — в [harness-guide.md](./agents/harness-guide.md) и
[git-workflow.md](./agents/git-workflow.md).
