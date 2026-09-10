# Независимые контракты backend-ролей

## Контекст системы

Evidence архитектуры, QA и review должно быть независимо от автора изменения, а специализированные
writer-роли должны применяться только в своих зонах ответственности.

## Действующий контракт

`architect` выдаёт read-only decision brief. `qa` возвращает воспроизводимые findings, не изменяя
тесты или fixtures. `code-review` является high-risk gate и выдаёт отдельные Standards и Spec
reports. `database-migrations` и `messaging-integration` заменяют `developer` только при своих
specialist triggers; смешанная работа проходит последовательными handoff внутри одного batch с
одним active writer.

## Операционные последствия

Автор изменения не является единственным источником архитектурного, QA или review evidence.
Manifest каждой роли фиксирует output contract и risk triggers, а coordinator блокирует
simultaneous writers и completion без обязательных review reports.
