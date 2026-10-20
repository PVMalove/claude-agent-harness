# Консолидация integration-веток Harness Next и Release Gate

**Статус: ACCEPTED (2026-10-20).** Эпик #784, ADR 0019–0024. Документ задаёт, какие существующие
integration-ветки входят в `integration/harness-runtime-reliability` и как, что из них заменяет
Harness Next и какое evidence разрешает влить кандидата в целевую ветку.

## Контекст системы

На 2026-10-20 (`origin`, база — `master` 3556b16, Harness Next — 948c980) от `master` отходят
четыре integration-ветки с работой, которой нет в `master`:

| Ветка | Коммитов вне `master` | Содержание |
|---|---|---|
| `integration/orchestration-ledger` | 1 | 7cef1d7 — #780: QA-lease освобождается после сбоя на занятом ledger |
| `integration/container-executor` | 9 | ADR 0018 `0018-container-executor.md`, Python-исполнитель Docker/Podman (#704–#708, #752, #766, #770, #771, #775, #782) |
| `integration/orchestration-system-fields` | 9 | эпик #681: `systemFields.v1` и normalizer (#690), Python store `runtimeState.v1` (#699), durable intent и reconcile (#701, #703), доработки файлового ledger (#712, #728, #735, b69eed1), совместимость recovery с консолью (#746) |
| `integration/console-redesign` | 9 | общий каркас консоли (#729–#773), наблюдение запуска worker-а (#716) |

ADR 0019–0024 учитывали только первые две ветки. Python store `runtimeState.v1` из
`integration/orchestration-system-fields` — второй writer operational state, который запрещают
ADR 0020 и ADR 0021. Пробное слияние (`git merge-tree`) даёт: Harness Next с
`orchestration-ledger` и с `container-executor` — без конфликтов;
`container-executor` с `orchestration-system-fields` — конфликты в `CHANGELOG.md`,
`harness/configuration_schema.py`, `harness/orchestration/contract.py` и
`harness/orchestration/core/constants.py`.

PR в integration-ветки и релизы вливаются squash-merge (так влиты #785 и релиз 1.5.1 #755).
Поэтому SHA коммита в целевой ветке никогда не равен SHA кандидата, на котором прошёл QA.

## Действующий контракт

### 1. Что входит в Harness Next

Все четыре ветки входят в `integration/harness-runtime-reliability` и выходят в `master` только
релизом 2.0.0 (ADR 0020, раздел 6). Импортируется только то, чем по матрице authority ADR 0021
владеет Python и что не пишет operational state:

| Источник | Решение |
|---|---|
| `integration/orchestration-ledger` | импортируется целиком; правило #780 переходит в fencing QA-lane (ADR 0020, раздел 3) |
| `integration/container-executor` | импортируется целиком; граница движка `run_engine` переходит к Rust до 2.0.0 (ADR 0022) |
| `integration/console-redesign` | импортируется целиком |
| `orchestration-system-fields`: ccc1d1e (#690) — реестр `systemFields.v1`, normalizer конфигурации, pinned configuration snapshot | импортируется |
| `orchestration-system-fields`: файлы реестра из 968ab8d (#728) — `core/system_fields.py`, `system_fields.v1.json` | импортируются |
| `orchestration-system-fields`: 38ae9d7 (#699), c8f5774 (#701), b095142 (#703) — store, intent, reconcile, effect | **Superseded-by-Harness-Next**: критерии переходят на Rust store (ADR 0020), идемпотентность (ADR 0021, раздел 4) и reconcile (ADR 0022, раздел 5) |
| `orchestration-system-fields`: bf3def1 (#712), ledger-часть 968ab8d (#728), 35f76bd (#735), b69eed1 | **Superseded-by-Harness-Next**: файловый ledger в 2.0.0 не пишется (ADR 0020, раздел 1); quarantine неизвестных значений переходит в repair импорта, цепочка хэшей — в audit-цепочку store (ADR 0020, разделы 4–5) |
| `orchestration-system-fields`: 7719682 (#746) | импортируется только CLI-поверхность и контракт консоли, не опирающиеся на `runtime_state_store`/`runtime_intent`; команды восстановления поверх store — отдельный тикет Harness Next |
| #685 (canonical CI, не реализован) | критерии остаются; CI проверяет реестр против Python и Rust (ADR 0021, раздел 3) |

Классификацию по коммитам импорт сверяет по файлам: модуль, который пишет operational state или
читает `runtime_state_store`/`runtime_intent`, не импортируется. Спорный файл — блокер импорта,
а не решение исполнителя.

### 2. Процедура импорта

- Один squash-PR на источник в `integration/harness-runtime-reliability`, по порядку:
  `orchestration-ledger` → `container-executor` → часть `orchestration-system-fields` →
  `console-redesign`. Следующий импорт начинается от головы Harness Next после предыдущего merge.
- Ветка импорта — `feature/issue-<ID>-import-<источник>` по `branch_pattern`:
  - для целого источника создаётся от его головы, затем в неё вливается (`git merge`, не rebase)
    голова Harness Next, и конфликты разрешаются в этом merge-коммите;
  - для части `orchestration-system-fields` создаётся от головы Harness Next, и импортируемые
    коммиты переносятся `git cherry-pick -x` с последующим удалением неимпортируемых файлов.
- Исходные integration-ветки не переписываются и не удаляются; force push запрещён. Тело PR
  перечисляет SHA источника и решение из раздела 1 для каждого коммита.
- Каждый импорт-PR — кандидат: полный clean-room QA на его голове (раздел 4).
- В PR импорта `container-executor` файл `docs/adr/0018-container-executor.md` переименовывается
  в `docs/adr/0025-container-executor.md`, а ссылки в ADR 0019–0024, `CONTEXT.md` и коде
  исправляются. Номер 0018 остаётся за `0018-ci-windows-subset.md`.
- Issue, чья Python-реализация стала Superseded, не закрываются молча: тикет обновления трекера
  (HN-01) добавляет в них раздел Harness Next со ссылкой на этот ADR.

### 3. Тикеты Harness Next

Дочерние тикеты #784 создаются от `integration/harness-runtime-reliability` и направляют PR в неё
(`Related to #<ID>`). Тикеты Rust-фундамента начинаются после импортов раздела 2, чтобы вся работа
шла от одного дерева.

### 4. Release Gate

Release Gate расширяет существующую проверку пары `integration next --pull-request <n>`
(`harness/orchestration/workflow/integration.py`): пара `{candidate_sha, target_sha}`, источник QA
`original-qa`, `local-qa` или `ci` и обязательная новая проверка при новом `target_sha`.

- Пару подтверждает только полный прогон `qa_gate_commands`: clean-room QA (ADR 0023, раздел 5)
  или CI, принятый `integration collect-ci` для этой же пары. Focused-, failed-only- и
  кэшированные результаты пару не подтверждают.
- Запись пары дополнительно хранит `candidate_tree_sha` (`git rev-parse <candidate>^{tree}`).
- Push ветки разрешён, только если её голова равна `candidate_sha`.
- Merge в целевую ветку разрешён, только если её текущая голова равна `target_sha` пары и является
  предком кандидата. Тогда squash-merge даёт дерево, равное `candidate_tree_sha`.
- После подтверждённого merge `to-pull-requests` сверяет дерево нового коммита целевой ветки с
  `candidate_tree_sha`. Расхождение — блокер: следующий merge от этой ветки требует нового полного
  QA на её голове.
- Если целевая ветка ушла вперёд, кандидат обновляется merge-ем целевой ветки; это новая пара и
  новый полный прогон.
- Релиз 2.0.0 — тот же контракт для PR `integration/harness-runtime-reliability` → `master`.
- После cutover запись пары хранит Rust store (ADR 0021, раздел 1); решение о merge остаётся у
  человека, а проверку пары — у Python.

## Операционные последствия

- **Approval.** Импорт не меняет утверждённые brief, approval и evidence исходных веток: они
  остаются в истории источника. Merge каждого PR выполняет человек; auto-merge запрещён.
- **Проверки импорта:** `git merge-tree --write-tree` перед созданием ветки; `make verify` на
  голове каждого импорт-PR; после импорта `container-executor` — `rg -n "0018-container-executor"`
  без совпадений вне истории.
- **Негативные проверки Release Gate:** голова целевой ветки ушла вперёд после QA → merge отказан;
  дерево после squash-merge не равно `candidate_tree_sha` → блокер; QA на SHA, отличном от головы
  push → push отказан; CI, привязанный вручную без `integration collect-ci`, пару не подтверждает.
- **Риск.** Пока импорты не завершены, тикеты Rust-фундамента не стартуют; разрешение конфликтов
  `contract.py`, `constants.py` и `configuration_schema.py` — работа импорта
  `orchestration-system-fields`, а не тикетов Rust.
