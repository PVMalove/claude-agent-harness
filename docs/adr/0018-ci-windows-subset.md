# Набор проверок CI на Windows в pull request (снят с CI)

**Статус: Windows-проверки сняты с CI (#753).** Задания `verify-windows (tests)` и
`verify-windows (clean-room)` удалены из `verify.yml`, `windows-latest` убран из матрицы
`memory-vector-probe.yml`. Пункты ниже про Windows-задания, подмножество в pull request, полный
прогон на `master`, Git на Windows и имена обязательных проверок больше не действуют и сохранены
как история решения. Остальное (Linux, diff-coverage, быстрая обратная связь, сводка и дашборд)
действует. Ключ `--platform-subset` и список `WINDOWS_PR_IGNORED_TESTS` в `scripts/verify.py`
остаются для локального запуска.

## Контекст системы

Прогон `make verify` на `windows-latest` с полным набором из ~2 700 тестов занимает больше
часа: процессы на Windows запускаются на порядок медленнее, чем на Linux, а тесты координатора
(`tests/orchestration/test_coordinator.py`) вызывают `git` ~90 раз каждый. Такое задание упирается в
лимит времени и не доходит до clean-room прогона. Логика координатора не зависит от платформы, а
Linux-задание уже проверяет её целиком.

## Контракт на момент решения

- **Два параллельных задания Windows.** `verify-windows (tests)` выполняет статические проверки,
  mypy и pytest; `verify-windows (clean-room)` — статические проверки и clean-room прогон. Время
  одного не расходует лимит другого. Разбиение задают `scripts/verify.py --only tests` и
  `--only clean-room`; `make verify` передаёт их через `VERIFY_ARGS`.
- **Подмножество в pull request.** В событии `pull_request` Windows-задание pytest получает
  `--platform-subset`: на Windows из набора исключаются файлы `WINDOWS_PR_IGNORED_TESTS` в
  `scripts/verify.py` — платформенно-нейтральные и медленные. Сейчас это
  `tests/orchestration/test_coordinator.py`.
- **Полный прогон на `master`.** Событие `push` на `master` и вызов workflow не передают
  `--platform-subset`, поэтому полный набор на Windows проверяется после слияния.
- **Linux не сужается.** Набор тестов задания `verify` на Linux не урезается; в pull request он лишь
  останавливается на первом падении (см. ниже).
- **Diff-coverage отдельно.** Задание `diff-coverage` (только `pull_request`) измеряет покрытие своим
  прогоном pytest параллельно с `verify`, а не после него.
- **Git на Windows.** Задание задаёт `core.autocrlf=false`, `core.fscache=true`,
  `core.preloadindex=true` до checkout.
- **Быстрая обратная связь в pull request.** `verify.py --fail-fast` останавливает pytest на первом
  падении; `--failed-first <каталог>` хранит кэш pytest в `.harness/.sandboxes/pytest-cache`,
  который workflow сохраняет и восстанавливает по ключу ветки, и запускает ранее упавшие тесты
  первыми. Повторное падение видно в первые минуты, а не после 20 минут полного прогона. `master`
  и вызов workflow запускают всё без остановки. Каждое новое обновление ветки pull request отменяет
  прежний запуск (`concurrency` с `cancel-in-progress` только для `pull_request`).
- **Сводка и дашборд.** `--junit-xml` пишет отчёт pytest; `scripts/ci_summary.py tests` кладёт в
  сводку задания итоги, упавшие тесты с причиной и самые медленные тесты, а задание `dashboard`
  (всегда после остальных) собирает одну страницу: результаты всех заданий, тесты по заданиям и
  все упавшие тесты.

## Операционные последствия на момент решения

- Новый тест, зависящий от платформы (пути, блокировки файлов, процессы, оболочка), не добавляется
  в `WINDOWS_PR_IGNORED_TESTS`; в список попадает только файл, у которого нет платформенной логики.
- Сбой, найденный только полным прогоном на `master`, исправляется отдельным тикетом: pull request
  уже слит.
- Имена обязательных проверок ветки изменились: вместо `verify-windows` теперь
  `verify-windows (tests)` и `verify-windows (clean-room)`.
