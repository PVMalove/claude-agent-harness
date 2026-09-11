---
name: setup-labels
description: Create or update this repo's GitHub labels (workflow::*, hitl/afk, task-report::required, out-of-scope, wayfinder:*) to match docs/agents/triage-labels.md. Run once per repo before first use of triage, to-spec, to-tickets, implement, to-guide, or wayfinder.
disable-model-invocation: true
---

# Setup Labels

`gh issue edit --add-label`/`gh issue create --label` fail on a label that doesn't exist yet — `gh` never auto-creates one. This skill creates every label this repo's triage taxonomy needs, so those calls never fail on a missing label.

## Process

1. **Read the tables.** Pull every `Label` / `Color` row from `docs/agents/triage-labels.md` — the taxonomy tables (category, execution mode, workflow state, context labels) and the Wayfinder addendum. Name and hex color only, skip the `Meaning`/`Applied by` columns. If the file doesn't exist, tell the user to run `/setup-matt-pocock-skills` first and stop.
2. **Show the plan.** List every label about to be created or updated, with its color. Confirm with the maintainer before touching GitHub — this mutates shared repo state, same discipline as any other tracker-mutating step in this repo (see `docs/agents/issue-tracker.md`).
3. **Apply.** For each label, run `gh label create "<name>" --color "<hex>" --force`. `--force` makes this idempotent — it updates the color of a label that already exists instead of erroring, and touches nothing else about it (issues already carrying it are unaffected).
4. **Report.** One line per label: created, updated (color changed), or already correct (no-op). Don't re-print the full plan from step 2.

<!--
Краткое описание (Summary): Этот навык предназначен для автоматического создания и обновления необходимых меток GitHub в репозитории на основе файла документации, гарантируя, что команды управления задачами не будут завершаться с ошибками из-за отсутствующих меток.

Перевод:
---
name: setup-labels
description: Создает или обновляет метки (labels) GitHub для этого репозитория (workflow::*, hitl/afk, task-report::required, out-of-scope, wayfinder:*), чтобы они соответствовали docs/agents/triage-labels.md. Запускайте один раз для каждого репозитория перед первым использованием triage, to-spec, to-tickets, implement, to-guide или wayfinder.
disable-model-invocation: true
---

# Настройка Меток (Setup Labels)

Команды `gh issue edit --add-label` / `gh issue create --label` завершаются ошибкой, если метки еще не существует — `gh` никогда не создает их автоматически. Этот навык создает каждую метку, необходимую для таксономии сортировки (triage) этого репозитория, чтобы эти вызовы никогда не завершались ошибкой из-за отсутствующей метки.

## Процесс

1. **Чтение таблиц.** Извлеките каждую строку с `Label` (Метка) / `Color` (Цвет) из `docs/agents/triage-labels.md` — таблиц таксономии (категория, режим выполнения, состояние рабочего процесса, контекстные метки) и дополнения Wayfinder. Только название и шестнадцатеричный код цвета, пропустите столбцы `Meaning` (Значение) / `Applied by` (Кем применяется). Если файл не существует, скажите пользователю сначала запустить `/setup-matt-pocock-skills` и остановитесь.
2. **Показ плана.** Перечислите каждую метку, которая будет создана или обновлена, вместе с ее цветом. Подтвердите у мейнтейнера, прежде чем вносить изменения в GitHub — это изменяет общее состояние репозитория, здесь та же дисциплина, что и для любого другого шага в этом репозитории, изменяющего трекер (см. `docs/agents/issue-tracker.md`).
3. **Применение.** Для каждой метки выполните `gh label create "<name>" --color "<hex>" --force`. Флаг `--force` делает это действие идемпотентным — он обновляет цвет уже существующей метки вместо того, чтобы выдавать ошибку, и больше ничего в ней не меняет (проблемы, к которым она уже прикреплена, остаются без изменений).
4. **Отчет.** По одной строке на каждую метку: создана, обновлена (цвет изменен) или уже корректна (нет действий). Не печатайте заново полный план из шага 2.
-->
