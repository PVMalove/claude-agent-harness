# Conflict Resolver — Разрешение конфликтов

Источник: [`harness/orchestration/roles/conflict-resolver.md`](../../harness/orchestration/roles/conflict-resolver.md) и общий контракт [`_common.md`](../../harness/orchestration/roles/_common.md).

## 1. Название и локализация

- **Английское название:** `Conflict resolver`
- **Русский перевод:**

```text
Разрешение конфликтов
```

## 2. Полное описание

Conflict Resolver — write-роль для текстового конфликта между веткой тикета (candidate) и сдвинувшимся integration SHA (target), а также для провалившейся проверки CI или local-QA уже обновлённой пары (`resolver.trigger: verification-failure`: есть `failed_evidence_ids`, конфликтных файлов нет, правка остаётся в scope brief). Её нельзя назначить вручную: единственный путь к ней — `integration resolve`, который создаёт отдельный resolver batch рядом с завершённым batch тикета.

Роль работает в issue-ветке и worktree своего batch, открывает skill `resolving-merge-conflicts`, сохраняет требования обеих сторон из секции `resolver` brief и не добавляет функциональность вне них. Она никогда не делает `--abort` и force-push. При несовместимых требованиях она не угадывает, а пишет checkpoint с конкретным описанием и вариантами; ответ человека фиксируется отдельным событием `human-decision`, и та же сессия продолжается. Собственный дефект тикета отправляется обычному developer-у.

## 3. Контракты

- **Вход (Input/Brief):** immutable brief с секцией `resolver`: тикет, требования обеих сторон, SHA candidate и target, scope, запреты, план коммита, проверки, остаток бюджета (два автоматических target SHA) и `report_staging_path`.
- **Процесс (Process):** rebase на точный SHA target, разрешение каждого конфликта с сохранением обеих сторон, проверки проекта, коммит резолюции; checkpoint при несовместимости.
- **Выход (Output/Report):** commit SHA резолюции, точные changed files, проверки и блок `resolver` (сохранённые требования, решения человека, причина, коммиты по плану). После принятия идёт узкий маршрут: без повторного review, но с новыми QA и CI либо local-QA.

## Источник

[conflict-resolver.md](../../harness/orchestration/roles/conflict-resolver.md)
