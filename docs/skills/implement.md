# Implement — Реализация

## 1. Название и локализация

- **Английское название:** `Implement`
- **Русский перевод:**

```text
Реализация
```

## 2. Полный перевод текста скила

````text
---
name: implement
description: Провести один backend-тикет через утверждённые handoff архитектора, разработчика, review, QA и publish.
disable-model-invocation: true
---

# Реализация

**Цель:** Провести ровно один тикет через `architect → developer → code-review → qa → publish`,
затем предложить `/to-pull-requests`. Эта сессия — coordinator: она создаёт и наблюдает dispatch,
но не реализует тикет сама.

## Маршрут

Это opt-in маршрут `backend-orchestration`. Подтвердите, что установленный coordinator доступен:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch status
```

Если команда недоступна или не читает state, остановитесь и предложите `/fast-implement`. Не
исправляйте и не подразумевайте opt-in capability. Завершите один batch до старта следующего.

## Контракт coordinator-а

Разрешите тикет, integration-ветку и блокеры по tracker/Git guidance. Статус тикета — часть этого
pre-flight, до `batch create` и до создания issue-ветки: проверьте блокеры при любой текущей метке
(есть открытые → стоп, назовите их, убедитесь, что стоит `status::blocked`); иначе замените метку
`status::*` на `status::in-progress` так же, как шаги 2–3 фазы 1 `/fast-implement`, и проверьте, что
она единственная `status::*`. Неудачная запись метки — блокер, а не предупреждение. Используйте isolated
issue-ветку и worktree; protected и `integration/*` — не write targets. Открытый batch — evidence,
которое показывают разработчику, а не запись для повторного использования или замены.

Предлагайте каждый handoff и останавливайтесь до явного approval разработчика перед созданием или
отправкой dispatch. Report — evidence, а не authority продвигать batch. Architect обязателен перед
developer dispatch; review хранит отдельные Standards и Spec evidence; независимый QA проверяет
candidate commit; publish отправляет только accepted SHA. Final report предшествует отдельно
одобренному publish dispatch.

Дефект, найденный в чистом developer report, чей DoD выполнен внутри своих allowed paths, не повод для retry:
примите report через `batch decide --findings-file <path>`, а после policy auto-accept выполните
`batch carry-over --batch <id> --findings-file <path>`, пока code-review dispatch не создан. Находка
уходит в brief code-review как перенесённый пункт (маршрут `carry-over`), и единственный developer
retry тратится после review. Retry developer report без accept — только при невыполненном пункте DoD
или изменении вне scope.

Каждая dispatched role сначала пишет model self-report относительно immutable brief и посылает
heartbeat. Между send и report coordinator опрашивает watchdog. Mismatch или stale dispatch —
blocker; recovery создаёт новый approved dispatch, а не редактирует brief или state.

Для `in-process` transport send фиксирует immutable handoff, после чего coordinator немедленно
запускает role subagent в указанном worktree. External adapter является только transport и сохраняет
тот же handoff, liveness, approval и report contract.

Перед первым dispatch coordinator может зарегистрировать immutable Context Package, собранный
`context_builder.py` без LLM из pinned `base_commit`/`candidate_commit`. Он содержит exact diff,
5–10 стартовых файлов с причинами, bounded graph, связанные тесты, карточки ADR/precedent и SHA-256
включённых файлов. Прямые локальные импорты разворачиваются на один уровень; неизвестные форматы
получают первые 30 строк. Перед каждым dispatch freshness package проверяется в shadow-режиме.

Write-роли могут записать checkpoint и продолжить тот же dispatch в новой worker session; checkpoint
не является completion report и переносит только SHA, changed files, остаточный DoD, проверки, риски,
blockers и ссылку на package. Read-only роли не могут checkpoint/resume. Rate-limit resume разрешён
автоматически, остальные planned triggers требуют coordinator decision.

## Промпт воркера

Каждый промпт воркера — этот шаблон, заполненный из результата `dispatch send` и вызова CLI,
которым его отправили. Brief и Context Package несут задачу; промпт также несёт обязательный
lifecycle. В `<coordinator CLI>` подставляются Python executable, абсолютный путь `coordinator.py`
и абсолютный `--repo` (плюс `--state-dir`, если он задан) исходного dispatch. Пути экранируются для
shell воркера. Адрес ledger сохраняется и при работе из другого worktree. Интервал heartbeat
копируется дословно из `dispatch send.heartbeat.every_seconds`.

```text
You are the <role> worker for dispatch <dispatch_id>.
Brief: <brief path from dispatch send>
Report staging path: <report_staging_path from dispatch send, verbatim>
Coordinator CLI: <coordinator CLI>
Before task work, run git rev-parse --show-toplevel, git branch --show-current and git rev-parse HEAD
in your runtime's current directory. Confirm your actually active model and the probed Git top-level:
<coordinator CLI> dispatch self-report --dispatch <dispatch_id> --model "<actual active model>" --worktree "<probed Git top-level>"
Proceed only after a successful self-report; escalate a mismatch or unavailable model identity.
Immediately after self-report and at least every <heartbeat.every_seconds> seconds while working, run:
<coordinator CLI> dispatch heartbeat --dispatch <dispatch_id>
Context Package <context_package_id>: start from its starting_files, symbol_graph and
related_tests. For a starting file with non-empty sections, read only the start_line–end_line
ranges the task needs.
Work within the brief; escalate a blocker for anything the brief and the package leave out.
Before your final reply, write the completion report JSON to the exact report staging path using
the common and role-specific report contract, with report_language: ru, then run:
<coordinator CLI> report submit --file "<report_staging_path>"
Completion means the report is recorded in the ledger. Include the submit result in your final reply.
If submission fails before recording, return the command and error as a blocker; keep the report file.
If the result includes completion, relay it to the coordinator for report complete; the report is
already recorded, so submit it only once. Chat text alone does not complete the dispatch.
```

Этот lifecycle обязателен для каждой новой или resumed worker session, включая developer-retry.
Сессия, остановившаяся на checkpoint, следует отдельному checkpoint-протоколу вместо completion
report. Финальный текст без записанного completion или валидного checkpoint — незавершённый
handoff; coordinator запрашивает недостающий протокол у той же доступной worker session.

Developer-retry добавляет две строки из `retry_start` своего `dispatch preflight`:

```text
Retry handoff: <retry_start.handoff, verbatim JSON>
Retry starting files: <paths from retry_start.starting_files>
```

Большие документы (каталог ролей, `playbook.md`, `backend-orchestration.md`, `git-workflow.md`) и
прежние отчёты попадают к воркеру только диапазонами `sections` Context Package или через retry
handoff. Промпт поручает ровно свой brief и, для retry, блокирующие findings из handoff.

Developer-retry всегда стартует новой сессией из компактного handoff и не продолжает историю прежней
developer-сессии. Decision packet preflight-а несёт `retry_context_estimate` и
`retry_context_warning` как evidence; они никогда не блокируют dispatch. Code-review и QA остаются новыми независимыми сессиями; по шаблону
меняется только их промпт. Handoff, порог smart zone и компакт определены в разделе
"Developer-retry handoff" playbook.

## Авторитетные guidance

Это короткий coordinator contract, а не вторая orchestration-процедура. Полные правила —
module-owned guidance:

- `.harness/orchestration/playbook.md` владеет lifecycle, authority, immutable brief, completion
  evidence, parallelism и metric rules.
- `.harness/orchestration/roles/` владеет boundary, required proof и specialist trigger каждой роли.
- `harness/docs/backend-orchestration.md` владеет setup, project configuration, CLI procedure и
  operational recovery.
- `docs/agents/git-workflow.md` владеет issue-branch, commit, push и PR boundaries.

Следуйте этим файлам, не дублируя и не ослабляя их правила. Не придумывайте token metrics:
используйте только provider- или runtime-observed telemetry и сохраняйте missing-data notes.

## Завершение

После accepted publish предложите `/to-pull-requests <ticket>`. Не запускайте его автоматически,
не открывайте и не мержьте PR, не пишите в integration-ветку и не закрывайте тикет этим скилом. Оставьте на тикете
`status::in-progress`: закрытие и перевод разблокированных зависимых в `status::ready` делает
`/to-pull-requests` после merge.
````

## 3. Контракты

- **Вход:** AFK-тикет, установленная opt-in capability и явные approvals разработчика.
- **Процесс:** Coordinator проводит пять handoff, проверяет model self-report и liveness, принимая
  только доказанные reports.
- **Выход:** Accepted candidate SHA, QA evidence и предложение `/to-pull-requests`.

## 4. Архитектурная схема

![Контракт скила: вход, работа, результат](../diagrams/previews/skill-contract-fill.workflow.png)

## Источник

[SKILL.md](../../skills/first-party/pvmalove/implement/SKILL.md)
