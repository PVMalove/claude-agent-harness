# Clean rebase при подготовке PR вместо перезапуска developer

## Контекст системы

Integration base уходит вперёд, пока batch проходит review, QA и publish. Раньше coordinator
перед каждым `code-review`- и `publish`-dispatch сверял закреплённую базу с вершиной integration
ref и при расхождении принудительно возвращал batch к developer-у. Другие batch это не
останавливало, но каждый сдвиг чужой базы стоил обычного developer-цикла, хотя в большинстве
случаев issue-ветка перебазируется без конфликта и работы для developer нет. Проверка нужна ровно
в тот момент, когда ветка превращается в PR, а не на каждом этапе конвейера.

## Действующий контракт

ADR 0012 сохраняет маршрут rebase для developer-retry; этот ADR переносит финальное обновление
базы в подготовку PR и заменяет им обязательную проверку свежести базы.

- Review, QA и publish проверяют закреплённый candidate, пока integration base ушла вперёд.
  Обязательной проверки свежести базы и принудительного перехода к developer больше нет; batch не
  блокирует остальные batch.
- `integration refresh --record <id>` (или `--ticket` и `--branch`) читает текущий SHA integration
  ref на remote. Если он равен target текущей пары, rebase не запускается (`state: unchanged`).
- Если target сдвинулся, команда перебазирует только issue-ветку своего batch, в её worktree, на
  точный SHA target. Rebase идёт на detached HEAD; локальная ветка переносится на результат
  только после публикации.
- Публикация переписанной истории идёт через `git push --force-with-lease=refs/heads/<ветка>:<старый
  SHA>`: чужой коммит, появившийся на remote, не теряется. Перед rebase команда требует, чтобы
  worktree стоял на issue-ветке на записанном candidate, не содержал незакоммиченных изменений и
  операций в процессе, а remote-ветка равнялась записанному candidate. Любое расхождение — отказ с
  remedy; stash, reset и принудительный обход не применяются. Protected и `integration/*` ветки
  никогда не становятся целью записи.
- Чистый rebase не вызывает resolver и не тратит его два цикла. Текстовый конфликт возвращает
  `state: conflict` с данными для resolver (конфликтные файлы, candidate, target, worktree),
  отменяет rebase и оставляет ветку и worktree как были; чужие worktree не затрагиваются.
- Каждый rebase пишет immutable `IntegrationRefreshRecord` в `reports/integration-refresh/`: прежний
  и новый candidate, прежний и новый target, коммиты до и после, отметки «resolver не вызывался».
  Последовательные refresh образуют цепочку от исходной пары записи.
- Старое QA остаётся историческим evidence и новый candidate не подтверждает. `integration status`
  показывает текущую пару после refresh и требует `verification` — зелёный CI или local-QA именно
  этой пары (`integration link-evidence`); resolver-evidence подтверждением не считается.
- Повторный review не нужен только из-за этого маршрута. Новая функциональная работа вне scope
  интеграции проходит обычные review и QA.

## Операционные последствия

- Подготовка PR (`/to-pull-requests`) при `stale` вызывает `integration refresh`, а при
  `state: conflict` передаёт данные resolver-у по маршруту rebase из ADR 0012; завершённый refresh
  требует нового CI или local-QA пары до открытия PR.
- Маршрут не зависит от developer-retry и от его бюджета `max_developer_retries`.
- `base_rebase_required` и `rebase_target_commit` остаются только для чтения старых записей:
  новые batch их не получают.
