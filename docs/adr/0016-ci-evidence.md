# CI-доказательство для комбинированного результата PR

## Контекст системы

После refresh (ADR 0014) и resolver (ADR 0015) новая пара candidate/target требует проверки, а
`link-evidence` принимал любой CI-результат со слов вызывающего: ничто не доказывало, что проверка
шла в нужном репозитории, на нужном PR и на результате слияния именно этой пары. Повторять полный
локальный QA ради каждого сдвига дорого, а принять CI на голой голове PR — значит принять
непроверенную совместимость с target.

## Действующий контракт

- **Поле проекта.** Необязательное `ci_required_checks` в `.harness/project.json` — список
  уникальных непустых имён CI-проверок. Пусто или нет поля — CI не настроен, действует запасной путь.
- **Правило комбинированного результата (GitHub).** Для `pull_request`-workflow `GITHUB_SHA` —
  последний merge commit ветки `refs/pull/<n>/merge`, а до слияния `merge_commit_sha` PR хранит
  тестовый merge commit (документация GitHub). Проверка относится к комбинированному результату,
  только если `head_sha` check run равен `merge_commit_sha`, а родители этого коммита — ровно
  `{candidate, target}`. Всё, что подтвердить нельзя (PR не открыт, `mergeable` не `true`, SHA не
  совпал, ответ неполный), — `unknown_checkout`, то есть запасной путь.
- **Порт.** `core/ci_source.py`: нормализованный `CiObservation` (только факты), порт `CiSource`,
  чистая `evaluate()` и read-only адаптер GitHub поверх подменяемого runner (`gh api`, только GET).
  Адаптер не открывает и не сливает PR, не трогает branch protection, не возвращает токены, тела
  ответов и stderr.
- **Вердикт.** `accepted` — все обязательные проверки `success` на комбинированном результате
  нужной пары, репозитория и PR. `failed` — завершённый `failure` обязательной проверки на уже
  подтверждённой паре. Остальное — `fallback` с причиной (`not_configured`, `unavailable`,
  `wrong_repository`, `wrong_pull_request`, `wrong_base`, `stale_candidate`, `stale_target`,
  `unknown_checkout`, `head_only`, `missing_check`, `pending_check`, `inconclusive_check`).
  Отмена, таймаут, пропуск и сбой инфраструктуры не считаются ни находкой в коде, ни успехом.
- **Команда.** `integration collect-ci --record <id> --pull-request <n>` проверяет трекер (только
  `github`; иначе `unsupported_tracker`), берёт репозиторий из поля `tracker`, текущую пару из
  записи, запрашивает CI и записывает доказательство только при `accepted` или `failed`. Запись —
  та же immutable pair-check запись, что у `link-evidence`, с `verification: collector-accepted`
  (или `collector-failed`) и очищенным блоком `collector` (источник, репозиторий, PR, SHA пары,
  merge commit, имя, id и URL каждого check run). Повтор идемпотентен. Fallback ничего не пишет и
  возвращает `local_qa_required: true`.
- **Статус.** Проверку пары закрывает только `local-qa` либо `ci` с `verification:
  collector-accepted` для точной текущей пары: вручную привязанный CI остаётся `unverified`.
  Блок `qa_replacement` показывает, заменяет ли принятый CI повтор полного QA, и при `applies: true`
  — источник, репозиторий, PR, SHA пары, merge commit и идентичность check run. Если integration ref
  ушёл вперёд, `applies: false`, `reason: integration_moved`, `re_refresh_required: true`. Исходные
  QA-отчёты не меняются и CI их не заменяет.
- **Границы.** Ни collector, ни валидатор не открывают и не сливают PR и не меняют правила веток.
  GitLab не поддерживается: контракт `CiObservation` переносим, адаптера нет.

## Операционные последствия

- Ledger schema не меняется: используется `reports/integration-evidence/`.
- Решение трудно откатывать частично: условия `accepted` и отметка `collector-accepted` связаны с
  `integration status`, поэтому их изменение требует нового ADR.
