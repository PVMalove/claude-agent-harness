# Markdown role manifests с project-bound assignments

## Контекст системы

Поведенческий контракт роли должен быть читаемым, переносимым и отделённым от runtime-specific
выбора provider и model.

## Действующий контракт

Каждая роль имеет Markdown manifest и общий `harness/orchestration/roles/_common.md` для handoff,
completion, commit proof и escalation. YAML frontmatter содержит только `name`, `mode`,
`required_capabilities` и `risk_triggers`. Project configuration содержит упорядоченный,
capability-validated provider-profile plan для роли; immutable brief фиксирует разрешённый profile,
model и effort.

## Операционные последствия

Manifest является источником поведенческой authority, а конфигурация задаёт runtime choice.
Проект не ослабляет write boundary, proof или risk gate роли. Общие процессные правила меняются в
одном общем контракте.
