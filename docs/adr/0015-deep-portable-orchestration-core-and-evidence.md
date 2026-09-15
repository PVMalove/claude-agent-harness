# Глубокое переносимое ядро оркестрации и evidence

## Контекст системы

Backend-orchestration выросла из role manifest'ов в lifecycle ledger, общий execution policy и
gate-runner. Если `/implement`, runtime adapter и документация независимо повторяют lifecycle,
handoff, model checks и QA, правила расходятся при следующем изменении. Особенно опасно выдавать
самоотчёт роли за telemetry: worker может честно сообщить свою модель для liveness-проверки, но не
является достоверным источником token usage.

## Действующий контракт

Переносимое ядро остаётся глубоким модулем с узкими границами ownership:

- `ledger.py` владеет versioned lifecycle, immutable audit и migration policy. Legacy state
  переносится только явной командой `ledger migrate`: полный candidate generation валидируется до
  переключения pointer, а прежние записи сохраняются как evidence. Совместимость не реализуется
  неявным чтением старых layouts на каждом запуске.
- `contract.py` и `coordinator.py` валидируют policy, approval, immutable handoff и lifecycle.
  Coordinator владеет очередью QA, lease, решением о finding и publish; эти полномочия не переходят
  в runner.
- `gate_runner.py` исполняет commands по выбранной execution policy и возвращает единую форму
  checks и sanitised evidence. Local `qa-gate` и clean-room QA — его разные adapters; runner не
  выбирает candidate, не управляет FIFO и не меняет batch state.
- `playbook.md` и `roles/` являются module-owned behavioral guidance: первый владеет общими
  lifecycle/evidence правилами, вторые — границей и доказательством конкретной роли. Runtime adapter
  только переводит уже approved brief в среду запуска и сохраняет те же инварианты.
- `/implement` остаётся коротким coordinator contract: порядок `architect → developer →
  code-review → qa → publish`, отдельный approval каждого handoff, model self-report и watchdog.
  Полная процедура не копируется в skill; он направляет к устанавливаемым модулям и project guidance.

Model self-report сравнивает фактически активную модель с immutable brief и служит только liveness/
assignment evidence. Token metrics принимаются исключительно из provider- или runtime-observed
telemetry с указанным источником. Completion report, self-report и оценка coordinator-а не могут
добавить, оценить или заполнить отсутствующие tokens.

## Операционные последствия

Изменение lifecycle требует обновить ledger и его migration/evidence tests; изменение выполнения
gate — gate-runner contract tests; изменение поведения роли — её manifest или playbook. `implement`
и runtime adapters не получают второй authority для этих правил. Clean-room verification проверяет
установленный harness: короткий skill сохраняет observable coordinator contract, а установленное
project guidance совпадает с source template. Pilot и delivery reports помечают отсутствие telemetry
как missing data вместо invented token value.

Связанные последующие решения: [ADR 0016 — Context Package, checkpoint/continuation и
base-commit gate](./0016-context-package-checkpoint-continuation-and-base-commit-gate.md).
