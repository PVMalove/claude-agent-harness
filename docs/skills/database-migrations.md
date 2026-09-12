# Database Migrations — Миграции базы данных

## 1. Название и локализация

- **English:** Database Migrations
- **Русский (1:1):**

```text
Миграции базы данных
```

## 2. Полное описание

Database Migrations — write-роль только для schema и data boundaries. Она запускается при risk-триггере `schema-change` или `data-migration`, работает в объявленной database/infrastructure зоне и не берёт на себя несвязанные service-изменения.

Роль создаёт миграцию и фиксирует условия rollout и rollback. Перед handoff проверяет миграцию и совместимость проектными командами и выполняет требуемый risk review. Необратимость, неполное rollback-доказательство или пересечение зоны требуют решения координатора.

## 3. Контракты

- **Вход (Input/Brief):** immutable brief с изменением схемы/данных, разрешёнными путями, стратегией совместимости, DoD, запретами и migration checks.
- **Выход (Output/Report):** commit SHA миграции; changed files; результаты migration/compatibility checks; условия rollout/rollback, риски, блокеры и следующий gate.

## 4. Архитектурная схема

![Контракт скила: вход, работа, результат](../diagrams/previews/skill-contract-fill.workflow.png)
