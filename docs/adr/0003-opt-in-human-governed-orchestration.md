# Делать multi-role orchestration opt-in и сохранять решения за человеком

## Контекст

Некоторым backend-задачам нужны несколько изолированных ролей, повторяемые handoff и независимое
QA. Остальные проекты не должны получать scheduler или менять обычный `/implement`.

## Решение

`backend-orchestration` — необязательная capability поверх `pvmalove-suite`. Manifest'ы ролей и
проектный конфиг задают доступ, зоны записи, назначение и проверки. Runtime-neutral coordinator
ведёт локальные санитизированные state, immutable brief и report: batch содержит dispatch, а каждый
dispatch требует отдельного approval. Для candidate SHA coordinator выполняет детерминированную
оценку риска, при необходимости двухосевое review и затем clean-room QA. Runtime adapter только
доставляет уже одобренный dispatch; он не принимает отчёты, не планирует следующий шаг и не создаёт
или не мержит PR.

## Альтернативы

- Включать orchestration во всех установках харнесса.
- Доверить runtime adapter планирование и переходы lifecycle.
- Автоматически публиковать SHA или мержить PR после QA.

## Почему не они

Обязательная orchestration увеличивает риск и сложность обычных задач. Runtime-специфичный
scheduler лишает capability переносимости. QA доказывает состояние кода, но не заменяет решение
владельца о выпуске.

## Последствия

Проект, выбирающий capability, обязан поддерживать валидный `.harness/orchestration.json` и явно
разрешать каждый dispatch и результат. Evidence остаётся локальным до решения coordinator-а;
восстановление stale QA lease также выполняется явно.
