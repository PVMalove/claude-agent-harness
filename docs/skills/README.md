# Карта навыков и ролей

Эта папка — короткая статическая память харнесса. Каждый документ фиксирует локализованное название, триггеры, ответственность, Input/Output-контракт и PNG-схему Archify. Подробные первоисточники остаются в `skills/first-party/pvmalove/`, `harness/orchestration/roles/`, `harness/orchestration/playbook.md` и `harness/orchestration/pilot.md`.

## Командные навыки

- [ask-matt](./ask-matt.md), [wayfinder](./wayfinder.md), [triage](./triage.md)
- [grill-me](./grill-me.md), [grilling](./grilling.md), [grill-with-docs](./grill-with-docs.md)
- [to-spec](./to-spec.md), [to-tickets](./to-tickets.md), [to-guide](./to-guide.md)
- [implement](./implement.md), [fast-implement](./fast-implement.md), [code-review](./code-review.md), [qa-gate](./qa-gate.md), [to-pull-requests](./to-pull-requests.md)
- [start-project](./start-project.md), [integrate-project](./integrate-project.md), [setup-labels](./setup-labels.md), [delivery-stats](./delivery-stats.md)

## Роли конвейера

- [Coordinator](./coordinator.md), [Architect](./architect.md), [Developer](./developer.md)
- [Code Review](./code-review-role.md), [QA](./qa.md)
- [Messaging Integration](./messaging-integration.md), [Database Migrations](./database-migrations.md)
- [Playbook](./playbook.md), [Pilot](./pilot.md)

## Связанные документы

- Discovery Context начинается в `/grilling` через opt-in `Live Artifact`, проходит через
  `Relevant Files` и ticket-specific filtered Repo Map, а backend batch использует LLM-free
  `Context Package`, checkpoint/continuation для write-роли и base-commit gate. Операционные
  правила собраны в [backend-orchestration](../agents/backend-orchestration.md), актуальное
  состояние — в [current-state](../agents/current-state.md), решение записано в
  [ADR 0016](../adr/0016-context-package-checkpoint-continuation-and-base-commit-gate.md).
- Внутренние агенты: [`docs/agents/`](../agents/)
- Политики-хуки: [`docs/hooks/`](../hooks/)
- Редактируемая Archify-спецификация: [`docs/diagrams/skill-contract-fill.workflow.json`](../diagrams/skill-contract-fill.workflow.json)
