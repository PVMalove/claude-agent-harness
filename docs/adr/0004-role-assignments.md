# Роли, назначения и неизменяемые handoff

## Контекст системы

Роль описывает поведение и границу записи, а проект выбирает доступный профиль провайдера,
модель, effort, транспорт и зону. Совместимость этих решений нужно проверить до отправки
работы. Отчёт автора кода не может быть единственным доказательством качества его изменения.

## Действующий контракт

Markdown manifests в `harness/orchestration/roles/` задают `name`, `mode`,
`required_capabilities` и `risk_triggers`; `_common.md` задаёт общие правила handoff,
completion, commit proof и escalation. Конкретный provider и model не зашиваются в manifest.
Проектная `.harness/orchestration.json` содержит provider profiles, ordered assignment plans,
backend zones, бюджеты и команды проверки. `contract.py` проверяет capability профиля,
обязательные model/effort, транспорт `in-process` либо `external`, допустимые overrides и
точное соответствие verification commands. Каждый dispatch получает неизменяемый brief с
разрешённым assignment. Изменение scope, DoD, зоны, назначения или proof требует нового
dispatch и approval.

`architect` выдаёт read-only архитектурный brief, `code-review` проверяет Standards и Spec,
`qa` даёт независимое воспроизводимое QA evidence. Writer-роли `developer`,
`database-migrations` и `messaging-integration` работают в объявленных зонах и возвращают
commit SHA с verification evidence. Специализированные writer-роли используются по своим
risk triggers и передают работу последовательно. Batch привязан к issue-ветке и worktree;
в нём одновременно активен один writer. Независимые batches не должны делить сервис,
bounded context или инфраструктурную зону. Тяжёлые integration и quality checks идут через
одну сериализованную lane.

## Операционные последствия

Coordinator проверяет зону, assignment, отчёт, risk triggers и обязательные gates до
следующего перехода. API contracts, migrations, messaging/outbox, transactions, security и
concurrency требуют review по правилам роли. Writer не расширяет зону через сообщение в
чате и не заменяет commit SHA текстовым «готово»; read-only роль не меняет тесты и fixtures.
