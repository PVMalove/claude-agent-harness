# Approval привязан к digest перехода; attention — флаг batch, а не состояние

## Контекст системы

Coordinator обязан быть предсказуемым и аудируемым при retry, нехватке контекста и повторном
запуске read-only ролей. До этого решения approval подтверждал лишь «кто и когда», а не *что*:
между показом человеку и созданием dispatch мог измениться candidate, scope, verification command или
Context Package; infrastructure/transport retry не имел предела и мог крутиться бесконечно; замер
контекста существовал только как `context_advisory` без записи и обязанностей worker-а. Исследование
Hermes Agent (`docs/research/hermes-agent-orchestration-notes.md`) дало переносимые паттерны
(request-bound approval, idempotency ключ повтора, checkpoint, разделение инфраструктурной ошибки и
ошибки задачи); его state machine и интерактивные `/retry`, `/rollback` не переносятся.

## Действующий контракт

- **Digest перехода.** `dispatch propose` (dry run) возвращает канонический переход и
  `transition_digest`; `dispatch create` с явным approval принимает только этот digest и пересчитывает
  его из ledger. Digest хранится в approval и в immutable brief вместе с переходом
  (`transition`, `transition_digest`, `retry_idempotency_key`, `orchestration_policy` — группа из
  четырёх полей: либо все, либо ни одного, чтобы старые brief оставались валидными). Опциональный
  `approval_ttl_seconds` делает просроченное approval fail-closed.
- **Idempotency.** У `architect`/`code-review`/`qa`/publish есть `retry_idempotency_key`; активных
  dispatch с одним ключом не бывает, завершённый повтор получает новый immutable ID.
- **Context pressure** — отдельная запись-наблюдение (источник только provider/runtime); она не входит
  ни в routing, ни в approval. Категория retry `context-pressure` допустима лишь при её `critical`
  записи.
- **Attention** — булев флаг batch с причиной, временем, последним безопасным действием и
  рекомендацией; он запрещает создание следующего dispatch, но не переводит batch в новое состояние,
  поэтому lifecycle-таблица `planned → … → terminal` и `ledger._validate_batch_transition` не
  меняются. Снимает его только человек через `batch attention resolve`.
- **Узкая талия.** Ядро хранит lifecycle, ledger, routing, проверку approval, инварианты
  candidate/base/Context Package и idempotency. Health транспорта и verification-среды, классификатор
  причины retry, провайдер контекстной телеметрии и адаптер уведомления — интерфейсы
  (`harness/orchestration/extensions.py`) с инертным дефолтом `none`; они не добавляют model tools и
  не меняют prompt.

## Операционные последствия

- Версия ledger не меняется: новые поля batch и группа полей brief опциональны и проходят
  `_validate_batch_integrity`/`_validate_dispatch`; `ledger migrate` не требуется.
- CLI: `dispatch create --approved-by …` требует `--transition-digest` (получить его — `dispatch
  propose`); policy-approval привязывается к собственному digest автоматически.
- Оператор видит причину остановки в `needs_attention`/`attention_events`, а не по молчанию цикла;
  сбой адаптера уведомления записывается и флаг не отменяет.
