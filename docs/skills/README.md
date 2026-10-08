# Карта навыков и ролей

Этот каталог — короткая статическая память харнесса. Каждый документ фиксирует локализованное название, триггеры, ответственность, Input/Output-контракт и PNG-схему Archify. Подробные первоисточники остаются в `skills/first-party/pvmalove/`, `harness/orchestration/roles/`, `harness/orchestration/playbook.md` и `harness/orchestration/pilot.md`.

## Командные навыки

- [ask-matt](./ask-matt.md), [wayfinder](./wayfinder.md), [triage](./triage.md)
- [diagnosing-bugs](./diagnosing-bugs.md)
- [architect](./architect.md#интерактивный-скилл-architect) — ручное сравнение архитектурных вариантов
- [grill-me](./grill-me.md), [grilling](./grilling.md), [grill-with-docs](./grill-with-docs.md)
- [to-spec](./to-spec.md), [to-tickets](./to-tickets.md), [to-guide](./to-guide.md)
- [implement](./implement.md), [fast-implement](./fast-implement.md), [code-review](./code-review.md), [qa-gate](./qa-gate.md), [to-pull-requests](./to-pull-requests.md)
- [start-project](./start-project.md), [integrate-project](./integrate-project.md), [setup-labels](./setup-labels.md), [delivery-stats](./delivery-stats.md)

## Роли конвейера

- [Coordinator](./coordinator.md), [Architect](./architect.md), [Developer](./developer.md)
- [Code Review](./code-review-role.md), [QA](./qa.md)
- [Messaging Integration](./messaging-integration.md), [Database Migrations](./database-migrations.md), [Conflict Resolver](./conflict-resolver.md)
- [Playbook](./playbook.md), [Pilot](./pilot.md)

## Закреплённые upstream skills

Русские описания всех 25 skills из `skills/vendor/mattpocock/` находятся в
[vendor/](./vendor/). Их исходные `SKILL.md` закреплены в upstream snapshot. Не правьте их
вручную. First-party overrides с совпадающими именами описаны здесь отдельно по действующей
версии `pvmalove-suite`.

## Связанные документы

- Discovery Context начинается в `/grilling` через opt-in `Live Artifact` и проходит через
  `Relevant Files` и ticket-specific Path inventory. Backend batch использует LLM-free
  `Context Package`, checkpoint/continuation для write-роли и base-commit gate. Операционные
  правила собраны в [backend-orchestration](../../harness/docs/backend-orchestration.md).
  [ADR 0003](../adr/0003-orchestration-core.md) описывает актуальное устройство,
  [ADR 0005](../adr/0005-implement-pipeline.md) — конвейер.
- Внутренние агенты: [`docs/agents/`](../agents/)
- Политики-хуки: [`docs/hooks/`](../hooks/)
- Редактируемая Archify-спецификация: [`docs/diagrams/skill-contract-fill.workflow.json`](../diagrams/skill-contract-fill.workflow.json)
