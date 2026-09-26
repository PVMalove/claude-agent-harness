# Архитектура Agent Harness

Agent Harness — переносимый runtime-native snapshot skills, правил, hooks и project-owned
конфигурации. Исходный репозиторий поставляет capability, а целевой проект получает независимый
`.harness`-слой: во время работы исходный клон харнесса не нужен.

## Слои и ownership

| Слой | Источник истины | Ответственность |
| --- | --- | --- |
| Capability catalog | `harness/CAPABILITIES.json` | Разрешение состава snapshot и зависимостей capability. |
| Skill packages | `skills/vendor/`, `skills/first-party/` | Инструкции, переводы и overrides; vendor snapshot вручную не редактируется. |
| Project snapshot | `.harness/skills/`, `harness.lock`, `REGISTRY.md` | Независимая поставка в целевой репозиторий, drift/provenance. |
| Project contract | `.harness/project.json` | Язык, base branch, branch pattern и QA-команды. |
| Orchestration core | `contract.py`, `core/`, `ledger/`, `workflow/`, `coordinator.py` | Policy, approvals, immutable records, lifecycle, audit и dispatch; `coordinator.py` — только CLI-фасад. |
| Evidence execution | `gate_runner.py`, `delivery_stats.py` | Проверки, санитизированное evidence и source-backed telemetry. |
| Runtime boundary | `orca_adapter.py` или `in-process` | Только доставка уже approved brief; не меняет scope и state. |

## Сквозная модель поставки

`harness init/adopt/update` разрешает capability из каталога, копирует пакеты и фиксирует lock.
Рантайм находит один и тот же snapshot через native skill roots; для Hermes Agent используется
`.harness/skills/REGISTRY.md`. Project-owned overlays и runtime integrations проходят отдельную
проверку provenance и inventory. Секреты не входят ни в lock, ни в brief, ни в reports.

## Discovery Pipeline

Кодовый и проектный контекст передаётся между сессиями через проверяемые артефакты:

1. `/grilling` предлагает кандидатные пути и записывает в `Live Artifact` только явно одобренные
   пользователем файлы.
2. `/to-spec` сохраняет список в эпике в `## Relevant Files (Discovery Context)`.
3. `/to-tickets` назначает пути tracer-bullet тикетам, строит Path inventory и один раз
   вызывает cheap advisory. Advisory может только добавить exact dependency из Path inventory.
4. `context_builder.py` читает pinned `base_commit`/`candidate_commit` без LLM и строит immutable
   Context Package: exact diff, 5–10 стартовых файлов с причинами, bounded graph, связанные тесты,
   ADR/precedent cards, размер и SHA-256. Локальные импорты раскрываются на один уровень; для
   неподдержанных форматов используются первые 30 строк.
5. Coordinator регистрирует package в ledger и перед каждым новым dispatch записывает его freshness
   в shadow-режиме. Stale package surfaced coordinator-у, но пока не блокирует dispatch.

![Discovery Pipeline](./docs/diagrams/previews/discovery-pipeline.workflow.png)

[Открыть интерактивную Discovery Pipeline-схему](./docs/diagrams/discovery-pipeline.workflow.html)

## Backend orchestration

`backend-orchestration` — opt-in capability поверх `pvmalove-suite`. Coordinator владеет batch,
approval, scope changes, QA lane и принятием reports. Role manifest владеет режимом роли, write zone,
proof и risk triggers; `.harness/orchestration.json` только разрешает project-owned provider/model,
transport, zone, budget и команды проверки.

Обычный `/implement` проходит `architect → developer → code-review → qa → publish`, с отдельным
approval каждого handoff. Каждая роль получает immutable brief, подтверждает model self-report и
передаёт heartbeat. Review и QA работают с pinned candidate SHA; PR остаётся отдельным ручным шагом
`/to-pull-requests`, merge не автоматизируется.

Batch lifecycle: `planned → awaiting-approval ↔ active → completed | blocked | failed`. Completion
report оставляет dispatch в `reported` до coordinator decision. Только write-роли могут записать
non-terminal checkpoint и продолжить тот же dispatch под новым worker session ID; новая сессия снова
проходит self-report и heartbeat и не получает старый chat/traceback. Rate-limit resume разрешён
автоматически, planned trigger требует существующего coordinator decision. Read-only роли не
растягиваются через checkpoint.

`batch create` сначала выполняет `git fetch origin <integration_ref>` и фиксирует актуальную вершину.
Перед review/publish base-commit gate повторяется; drift устраняется новым developer/rebase dispatch,
после чего candidate заново проходит risk assessment. Delta-review после Warning разрешён только для
test-only diff и всегда является новым независимым review dispatch.

![Backend batch lifecycle](./docs/diagrams/previews/backend-batch.lifecycle.png)

[Открыть интерактивную lifecycle-схему](./docs/diagrams/backend-batch.lifecycle.html)

## Диаграммы и проверка

Канонические исходники и интерактивные артефакты находятся в [docs/diagrams/](./docs/diagrams/README.md):

- [полный pipeline](./docs/diagrams/delivery-pipeline.workflow.html);
- [Discovery Pipeline](./docs/diagrams/discovery-pipeline.workflow.html);
- [архитектура harness](./docs/diagrams/harness-topology.architecture.html);
- [implement с гейтами](./docs/diagrams/implement-pipeline.workflow.html) и [sequence](./docs/diagrams/implement-dispatch.sequence.html);
- [runtime/dispatch](./docs/diagrams/backend-runtime.workflow.html), [QA/PR](./docs/diagrams/qa-call-path.workflow.html);
- [capability dataflow](./docs/diagrams/capability-delivery.dataflow.html), [skill contract](./docs/diagrams/skill-contract-fill.workflow.html).

Правится только `*.json`; после изменения запускаются `validate`, `deliver` и `visual-check`. Код
проверяется `scripts/test_clean_room.py`, unit-тестами и командами из `.harness/project.json`. Для
telemetry `delivery-stats` сохраняет cache read/write tokens, worker sessions/restart reasons,
review diff scope excess и QA failure rate только при наличии наблюдаемого источника; отсутствующие
значения остаются `нет данных`.

Подробные правила находятся в [current-state.md](./docs/agents/current-state.md),
[backend-orchestration.md](./docs/agents/backend-orchestration.md), [harness-guide.md](./docs/agents/harness-guide.md)
и [ADR 0016](./docs/adr/0016-context-package-checkpoint-continuation-and-base-commit-gate.md).
