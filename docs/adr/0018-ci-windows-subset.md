# Набор проверок CI на Windows в pull request

## Контекст системы

Прогон `make verify` на `windows-latest` с полным набором из ~2 700 тестов занимает больше
часа: процессы на Windows запускаются на порядок медленнее, чем на Linux, а тесты координатора
(`tests/orchestration/test_coordinator.py`) вызывают `git` ~90 раз каждый. Такой джоб упирается в
лимит времени и не доходит до clean-room прогона. Логика координатора не зависит от платформы, а
Linux-джоб уже проверяет её целиком.

## Действующий контракт

- **Два параллельных джоба Windows.** `verify-windows (tests)` выполняет статические проверки,
  mypy и pytest; `verify-windows (clean-room)` — статические проверки и clean-room прогон. Время
  одного не расходует лимит другого. Разбиение задают `scripts/verify.py --only tests` и
  `--only clean-room`; `make verify` передаёт их через `VERIFY_ARGS`.
- **Подмножество в pull request.** В событии `pull_request` Windows-джоб pytest получает
  `--platform-subset`: на Windows из набора исключаются файлы `WINDOWS_PR_IGNORED_TESTS` в
  `scripts/verify.py` — платформенно-нейтральные и медленные. Сейчас это
  `tests/orchestration/test_coordinator.py`.
- **Полный прогон на `master`.** Событие `push` на `master` и вызов workflow не передают
  `--platform-subset`, поэтому полный набор на Windows проверяется после слияния.
- **Linux не сужается.** Джоб `verify` на Linux всегда выполняет все тесты.
- **Diff-coverage отдельно.** Джоб `diff-coverage` (только `pull_request`) измеряет покрытие своим
  прогоном pytest параллельно с `verify`, а не после него.
- **Git на Windows.** Джоб задаёт `core.autocrlf=false`, `core.fscache=true`,
  `core.preloadindex=true` до checkout.

## Операционные последствия

- Новый тест, зависящий от платформы (пути, блокировки файлов, процессы, оболочка), не добавляется
  в `WINDOWS_PR_IGNORED_TESTS`; в список попадает только файл, у которого нет платформенной логики.
- Сбой, найденный только полным прогоном на `master`, исправляется отдельным тикетом: pull request
  уже слит.
- Имена обязательных проверок ветки изменились: вместо `verify-windows` теперь
  `verify-windows (tests)` и `verify-windows (clean-room)`.
