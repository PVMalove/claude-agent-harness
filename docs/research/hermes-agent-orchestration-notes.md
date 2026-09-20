# Что взять из Hermes Agent для orchestration coordinator

Дата исследования: 2026-09-20. Источники — только материалы и исходный код
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).

## Вывод

Hermes полезен не как готовая state machine для delivery: его retry и rollback относятся к
диалогу/файловой системе, а не к проверяемому candidate commit. Заимствовать стоит узкие
технические паттерны: явный набор инструментов на dispatch, bound approval request, устойчивые
checkpoint-и и разделение инфраструктурной ошибки с ошибкой задачи. Нельзя переносить его
разрешительную модель команд или заменять ей существующие immutable evidence, risk gates и
human-governed routing.

| Область | Факт Hermes | Рекомендация для coordinator |
| --- | --- | --- |
| Набор инструментов | Toolsets — именованные core/composite/platform bundles; их можно выбрать для session или platform. Каждый инструмент состоит ровно в одном toolset. [Документация](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/toolsets-reference.md), [реестр](https://github.com/NousResearch/hermes-agent/blob/main/toolsets.py) | **Adopt:** добавить в immutable brief `allowed_toolsets`/`allowed_tools` и хеш разрешённого набора. Для read-only review/QA выдавать минимальный policy-набор. Это — ограничение worker-а, а не отключение глобальных runtime-инструментов. |
| Инструменты и контекст | `execute_code` предназначен для цепочек из 3+ вызовов, фильтрации больших outputs и условных веток; delegation создаёт изолированный контекст и возвращает только final summary. [Tools reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/tools-reference.md) | **Adopt частично:** вынести детерминированное сжатие логов/diff и построение Context Package во внешние не-ролевые helpers. **Avoid:** не принимать LLM summary как evidence и не использовать финальную сводку вместо полного immutable report. |
| Compaction | Порог и сохраняемый tail конфигурируются; `lean` сохраняет session log, anchor index и pointers для восстановления. Документация предупреждает: summarizer с малым context window может молча потерять middle turns. [Configuration](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/configuration.md) | **Adopt:** в checkpoint хранить compact, schema-validated recovery index (Context Package id/hash, проверенные команды, DoD, risks, blockers), а источник/объём context telemetry — отдельно. **Avoid:** не считать compaction успешной continuation без checkpoint и нового self-report; не доверять summary как единственному источнику фактов. |
| Checkpoint/rollback | Opt-in checkpoints делаются перед модификацией, максимум раз на turn/directory, в shadow Git store; реальный `.git` проекта не трогается. [Checkpoints guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/checkpoints-and-rollback.md) | **Adopt:** лимиты частоты/объёма checkpoint-ов и отдельное хранилище для recovery artifacts. **Avoid:** rollback не должен переписывать candidate, evidence или ledger: у coordinator новый candidate требует нового risk assessment и review. |
| Возобновление batch | Batch runner пишет checkpoint после batch, на resume сопоставляет завершённые prompts по содержанию, а не индексу; failed items не помечаются готовыми и повторяются. [Batch-processing guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/batch-processing.md) | **Adopt:** idempotency key для retry read-only dispatch: `(role, candidate_sha, context_package_hash, retry_reason, previous_dispatch_id)` и отдельный новый dispatch ID. Так infrastructure/transport retry не путается с уже выполненным review. **Avoid:** нельзя считать старое review evidence применимым после изменения candidate. |
| Approval | Опасные команды используют request-bound choice (`once`/`session`/`always`/`deny`); approval transport получает immutable request c opaque ID/digest и отвергает stale/подменённые ответы. [Security guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/security.md), [plugin approval contract](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/plugins.md) | **Adopt:** human approval на dispatch/abandon/retry сделать bound к digest конкретного proposed transition: batch, previous dispatch, candidate SHA, reason category и target role. Отклонять stale approval после любого изменения этих полей. **Avoid:** не добавлять постоянный `always` approval к lifecycle-переходам. |
| Наблюдаемость и транспорт | Gateway сообщает lifecycle/error events, а server-to-client approvals — JSON-RPC requests с correlation ID. Неподдерживающий requests клиент получает immediate error вместо ожидания deadline. [Programmatic integration](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/programmatic-integration.md) | **Adopt:** adapter должен сообщать структурированные `transport`/`verification-infrastructure` termination facts и request ID, а coordinator классифицирует их отдельно от findings. Если reply/transport отсутствует — `unknown` и безопасный `developer-retry`, не молчаливый повтор. |

## Конкретное правило retry

Добавить `reason_category` в completion/decision facts: `code`, `requirements`,
`candidate-change`, `verification-infrastructure`, `transport`, `unknown`. Значение должно
выводиться из структурированных blockers, command exit/evidence, findings и SHA, а не из текста
отчёта. Только при `verification-infrastructure` или `transport`, пустых findings, двух чистых
Standards/Spec verdicts и неизменном SHA coordinator может после нового bound human approval
создать **новый** read-only `code-review`/`qa`/`publish` dispatch. Он обязан заново пройти
base-commit gate и fresh Context Package; старые brief/report остаются immutable audit evidence.
Во всех остальных случаях (включая `unknown`) маршрут остаётся `developer-retry` и требует
новый candidate с новой risk assessment.

## Сопоставление с текущим проектом

Проект уже строже Hermes в ключевых точках: ADR-0016 требует ledger-owned Context Package,
checkpoint только для write-role, повторный self-report при resume, базовый commit gate и новый
независимый review вместо продолжения reviewer session. Поэтому изменения должны быть
дополнением к этому контракту, а не переносом интерактивных команд Hermes `/retry`, `/undo` или
`/rollback`. См. [ADR-0016](../adr/0016-context-package-checkpoint-continuation-and-base-commit-gate.md)
и [backend-orchestration guide](../agents/backend-orchestration.md).
