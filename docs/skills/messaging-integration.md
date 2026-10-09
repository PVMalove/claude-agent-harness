# Messaging Integration — Интеграция сообщений

## 1. Название и локализация

- **English:** Messaging Integration
- **Русский (1:1):**

```text
Интеграция сообщений
```

## 2. Полное описание

Messaging Integration — write-роль для границ outbox, message schema/routing, retry и dead-letter queue. Она запускается только когда соответствующий риск-триггер объявлен в brief; не поглощает несвязанные service-изменения.

Роль реализует совместимое изменение в allowed paths brief для messaging/infrastructure и явно описывает последствия доставки и отказов. До handoff она выполняет контрактные, routing и failure-path проверки, а также необходимый risk review. Неясность scope или отсутствие доказательства — эскалация, а не расширение scope.

## 3. Контракты

- **Вход (Input/Brief):** immutable brief с allowed paths сообщений, контрактом, правилами маршрутизации и retry/DLQ, DoD и required checks.
- **Выход (Output/Report):** commit SHA совместимого изменения; перечень файлов и проверок; последствия delivery/failure, риски, блокеры и следующий gate.

## Источник

[messaging-integration.md](../../harness/orchestration/roles/messaging-integration.md)
