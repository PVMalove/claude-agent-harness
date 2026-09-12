# Code Review — Ревью кода

## 1. Название и локализация

- **English:** Code Review
- **Русский (1:1):**

```text
Ревью кода
```

## 2. Полное описание

Code Review — независимая read-only роль, обязательная для API/public contracts, schema/data migrations, outbox/queues, transactions, authorization/security, concurrency/retry и retry/DLQ. Для остальных изменений координатор запускает её после явной оценки риска. Роль не меняет production code и не интегрирует проверяемую ветку.

Она закрепляет fixed diff и исходное требование, затем возвращает две независимые оси: Standards и Spec. Их нельзя сводить в единый балл или заменять одной из них. Отсутствующий отчёт по любой обязательной оси блокирует завершение high-risk batch.

## 3. Контракты

- **Вход (Input/Brief):** immutable brief, fixed diff/commit range, исходная спецификация, стандарты проекта и перечень применимых риск-триггеров.
- **Выход (Output/Report):** раздельные findings Standards и Spec с severity; зафиксированные доказательства diff и требования; остаточные риски, блокеры и отсутствие production changes.

## 4. Архитектурная схема

![Контракт скила: вход, работа, результат](../diagrams/previews/skill-contract-fill.workflow.png)
