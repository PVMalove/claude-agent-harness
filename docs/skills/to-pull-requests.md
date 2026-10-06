# To Pull Requests — К pull request'ам

## 1. Название и локализация

- **Английское название:** `to-pull-requests`
- **Русский перевод:**

```text
К pull request'ам
```

## 2. Полный перевод текста скила

```text
---
name: to-pull-requests
description: Подготовьте и откройте pull request для уже отправленной issue-ветки. Используйте после предложения `/implement` или `/fast-implement` или когда разработчик явно просит открыть PR.
disable-model-invocation: true
---

# К pull request'ам

Создайте pull request для указанного тикета. Это ручная команда: никогда не запускайте её лишь потому, что завершился `/implement`.

В GitLab называйте проект явно: каждая команда `glab` получает `-R <project-url>`, а каждый вызов `glab api` — `--hostname <host>` и путь `projects/<project-id>/...`. `<host>`, `<project-url>` и `<project-id>` определены в `docs/agents/issue-tracker.md` → GitLab → Conventions. Merge request GitLab обозначается `!<iid>`, а issue — `#<iid>`; в GitHub оба обозначаются `#<n>`.

1. Разрешите тикет и его точную целевую ветку PR из раздела тикета `## Integration Branch`, его родительского epic или `base_branch` для тикета без epic. Если это невозможно разрешить, остановитесь и спросите разработчика.
2. Проверьте, что текущая ветка соответствует `branch_pattern`, не является ни `base_branch`, ни `integration/*`, не имеет незакоммиченных изменений и не имеет коммитов, отсутствующих в upstream-ветке. Если ветка не отправлена, остановитесь и попросите разработчика вернуться к `/implement` или `/fast-implement` — по маршруту тикета. Получите её upstream и потребуйте, чтобы `git rev-parse HEAD` был равен SHA upstream-ветки; если они различаются, выполните fast-forward чистой локальной ветки до продолжения.
3. Для действительного opt-in `backend-orchestration` после синхронизации разрешите полный текущий SHA через `git rev-parse HEAD` и проверьте его до любого действия с PR. Каждая команда ниже — `python .harness/orchestration/coordinator.py --repo . <команда>`, а `<ticket-branch>` означает `--ticket "#<ID>" --branch "<current-issue-branch>"`.
   Сначала прочитайте integration-запись, только на чтение: `integration status <ticket-branch>`. Отказ «integration-записи нет» PR не блокирует: пропустите integration-шаги этого скила и продолжите проверкой доказательств и прежним потоком. Если она сообщает `unavailable`, integration ref не прочитан: остановитесь и сообщите разработчику как об операционной остановке, но не как о дефекте кода. Если `refreshes` не пуст, ветка уже обновлена: ниже проверяйте `original_candidate_sha`, а не текущий SHA, потому что старое принятое QA лишь допускает вход в подготовку PR и никогда не является QA обновлённого candidate.
   Затем проверьте принятое QA-доказательство этого SHA:
   `python .harness/orchestration/coordinator.py --repo . qa evidence --ticket "#<ID>" --branch "<current-issue-branch>" --candidate-commit "<full-SHA>"`.
   CLI выбирает единственный batch с принятым QA-доказательством для этого SHA, поэтому abandoned
   history с тем же тикетом и веткой не влияет на результат. Если один SHA принят в нескольких batch,
   остановитесь и повторите команду с явным `--batch <batch-id>`. Остановитесь, если команда отклоняет
   отсутствующие, непринятые или не совпадающие с SHA доказательства, если только тикет не помечен
   явно `pipeline::fast`. Для тикетов `pipeline::fast` проверку доказательств координатора пропустите.
   Затем запросите следующий шаг, только на чтение: `integration next <ticket-branch>`. Команда ничего не пишет в Git, ledger, dispatch и PR; действуйте по её `step`:
   - `refresh`: integration ref сдвинулся. Выполните `integration refresh <ticket-branch>`; чистый rebase обновляет только вашу issue-ветку (повторный review не нужен, цикл resolver не тратится), поэтому убедитесь, что `git rev-parse HEAD` равен новому `candidate_sha`, и повторите `integration next`. При `state: conflict` выполните `integration resolve <ticket-branch>` (он лишь создаёт batch resolver) и передайте его `batch_id` координаторной сессии (`batch approve`, затем `dispatch create --role conflict-resolver`). При любом другом отказе (грязный worktree, изменившийся remote) остановитесь и передайте данные разработчику по маршруту rebase из ADR 0012; не создавайте dispatch и не перезапускайте QA сами.
   - `resolver-open`: ветку ведёт открытый resolver batch. Остановитесь и дождитесь координаторной сессии; ожидание цикл не тратит.
   - `route-failure` или `human-decision`: проверка текущей пары провалилась. Остановитесь и передайте данные координаторной сессии. Провал обновлённой пары берёт тот же resolver в пределах бюджета (`integration resolve`); провал исходной пары — собственный дефект задачи и уходит обычному developer с review и QA: координаторная сессия планирует новый batch того же тикета и issue-ветки обычным маршрутом `/implement` (`batch create --ticket <ticket> --branch <branch> --worktree <worktree> --integration-ref <integration ref>`, затем architect, developer, code-review, QA и publish; завершённый исходный batch терминален, решение по нему больше не принимается и новый batch он не блокирует), а после принятого publish выполняет `integration prepare --ticket <ticket> --branch <branch> --batch <новый batch>`, поэтому повторите `integration next --record <новая запись> --pull-request <n>`. `human-decision` означает, что два цикла resolver или правки на том же target исчерпаны: продлить их может только решение разработчика (`integration resolver-event --kind human-decision --extends-budget`), ответ бюджет никогда не сбрасывает.
   - `confirm-pr`: продолжайте. `qa_source` — `original-qa` (исходная пара), `verification-pending-after-pr` (обновлённая пара, которую ещё не покрывают ни CI, ни local-QA) либо `ci` / `local-qa` (обновлённая пара, у которой такое проверенное evidence уже есть).
   Не запускайте `/qa-gate` повторно, когда эта проверка успешна; вместо этого из checkout ветки PR,
   проверенного на шаге 2, выполните `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/record-qa-gate-pass.sh"`,
   чтобы PR-hook принял этот SHA. Для обновлённого SHA этот маркер лишь разрешает открыть PR и не
   является QA нового candidate: его проверяет шаг 6a, когда PR уже существует. Если проект не является
   действительным opt-in или тикет помечен `pipeline::fast`, запустите `/qa-gate`, если этот
   репозиторий его предоставляет, и остановитесь при ошибке.
4. Подготовьте тело PR по `docs/agents/git-workflow.md` §3. Храните его только в `.harness/.sandboxes/pr_body/pr-body-<issue>-<slug>.md`, никогда в `docs/tasks/`. Разработчик может вручную запустить `pr-composer` в coding application; иначе заполните шаблон напрямую. Разрешите default branch репозитория — GitHub: `gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`; GitLab: поле `default_branch` из `glab api --hostname <host> projects/<project-id>`. Используйте `Closes #<ID>` только когда целевая ветка из шага 1 и есть default branch, иначе `Related to #<ID>`. GitLab закрывает issue по `Closes #<ID>` только когда MR попадает в default branch, а проект может отключить это автозакрытие, поэтому шаг 7 всегда проверяет состояние тикета.
5. Попросите разработчика явно и отдельно подтвердить, что ветка готова стать PR; этот вопрос никогда не объединяется с другим. Для opt-in-проекта назовите в нём точные `candidate_sha` и `target_sha` из `integration next`, целевую ветку PR и `qa_source`. Подтверждение относится только к этой паре. Остановитесь для его ответа.
6. После одобрения откройте PR/MR из checkout ветки, проверенного на шаге 2. Для opt-in-проекта непосредственно перед этим ещё раз выполните `integration next <ticket-branch>`: если изменились `candidate_sha`, `target_sha` или `step` (`confirm-pr`), подтверждение недействительно — повторите шаги выше и спросите заново. Заголовок и тело PR, как и каждый коммит, несут только проектные метаданные: без атрибуции автоматизированного агента, имён моделей и `Co-Authored-By`. `<target-branch>` — цель из шага 1; без неё оба CLI открыли бы PR/MR в default branch. `<title>` — заголовок тикета в одинарных кавычках, каждый апостроф записывается как `'\''` в POSIX-shell или как `''` в PowerShell. Выполняйте одну команду публикации на вызов shell:
   - **GitHub:** `gh pr create --base <target-branch> --title '<title>' --body-file <path>`.
   - **GitLab:** `glab mr create -R <project-url> --target-branch <target-branch> --title '<title>' --description-file <path> --yes`. MR — это `!<iid>`, где `<iid>` — последний сегмент URL `.../-/merge_requests/<iid>`, который печатает команда.

   Удаляйте файл тела `.harness/.sandboxes/pr_body/` только после успеха этой команды; при ошибке сохраните его для повторной попытки. Если тикет несёт `task-report::required`, опубликуйте его completion report в тикете, если разработчик не попросил пропустить это: запишите отчёт в `.harness/.sandboxes/pr_body/issue-comment-<issue>-<slug>.md`, затем опубликуйте его через `gh issue comment <ID> --body-file <path>` в GitHub или `glab api --hostname <host> projects/<project-id>/issues/<ID>/notes -F body=@<path>` в GitLab.
   **6a. Проверьте открытый или обновлённый PR (opt-in-проект).** Выполняйте после открытия PR и после любого последующего обновления его ветки (refresh или push resolver):
   - `integration collect-ci <ticket-branch> --pull-request <n>` запрашивает у трекера CI комбинированного результата. Если CI принят, он заменяет повтор полного локального QA (`qa_replacement.applies`) и больше ничего не запускается.
   - `next.action: wait` (проверка ещё идёт): повторите команду после паузы около минуты, не более десяти раз, затем спросите разработчика, ждать ли дальше или использовать локальный QA. Ожидание цикл resolver не тратит.
   - `next.action: local-qa` (CI отсутствует или непригоден): запустите существующий запасной путь `integration local-qa --record <integration_record_id> --ci-condition <next.ci_condition> --reason "<причина из collect-ci>"` (полный локальный QA точной пары через общую очередь QA). Результат `unavailable` или `exhausted` — операционная остановка, о которой сообщают как о ней, а не как о дефекте кода.
   - `next.action: route` (CI именно этой пары упал и записан): следуйте действию маршрута через `integration next` ниже; локальный QA не запускайте. `local_qa_required: true`, которое collect-ci возвращает рядом, — унаследованная семантика для любого непринятого результата, и `route` имеет приоритет.
   - Затем выполните `integration next <ticket-branch> --pull-request <n>` и действуйте по `step`, как на шаге 3: `verify` (повторите этот шаг), `route-failure`/`human-decision` (остановитесь и передайте данные; операционные сбои здесь не появляются), `refresh` (target сдвинулся: повторите refresh, обновите PR и этот шаг) либо `handoff`.
7. Верните ссылку на PR/MR в основной сессии (в GitLab также как `!<iid>`). Для opt-in-проекта перед просьбой выполнить merge покажите `handoff` из `integration next --pull-request <n>`: проверенные `candidate_sha` и `target_sha`, `qa_source` (`original-qa`, `ci` или `local-qa`) с его `reference` и признак обновления ветки. Укажите, что проверка относится ровно к этой паре и не обещает, что до merge ничего не изменится. Не включайте server branch protection и merge queue. Спросите, хочет ли разработчик проверить PR, и непосредственно перед merge повторите `integration next --pull-request <n>`: новый target означает refresh и новую проверку, а не повторное использование старой. Никогда не выполняйте merge. Действуйте с тикетом только после того, как разработчик подтвердит merge:
   - **Проверьте merge.** GitHub: `gh pr view <n> --json state,baseRefName` (`state` равен `MERGED`, `baseRefName` — цель из шага 1). GitLab: `glab mr view <iid> -R <project-url> -F json` (`state` равен `merged`, `target_branch` — цель из шага 1). Если merge в эту цель не выполнен, оставьте тикет открытым и сообщите разработчику.
   - **`Related to #<ID>`:** явно закройте тикет. GitHub: `gh issue close <ID> --reason completed`. GitLab: `glab issue close <ID> -R <project-url>`; команда не принимает комментарий, поэтому нужный комментарий сначала опубликуйте через notes API, как на шаге 6.
   - **`Closes #<ID>`:** убедитесь, что tracker закрыл тикет. GitHub: `gh issue view <ID> --json state` (`state` равен `CLOSED`). GitLab: `glab issue view <ID> -R <project-url> -F json` (`state` равен `closed`). GitLab закрывает его асинхронно, поэтому если тикет ещё `opened`, прочитайте его ещё раз; если он всё ещё открыт, закройте его явной командой выше и сообщите разработчику, почему tracker этого не сделал.
8. **Разблокируйте зависимые после каждого закрытия, в той же сессии.** Зависимость снимает только подтверждённый merge и штатное закрытие предшественника; push, publish, принятый QA и открытый PR — никогда. Метку `status::in-progress` закрытого тикета не трогайте: закрытое состояние и есть его терминальный `done`. Затем найдите все открытые тикеты, которые он блокировал, и переведите те, у которых теперь закрыты все блокеры:
   - **GitHub:** перечислите открытые issue со `status::blocked` (`gh issue list --state open --label status::blocked --json number`). Для каждого прочитайте нативные блокеры (`gh api repos/<owner>/<repo>/issues/<n>/dependencies/blocked_by --jq '[.[] | select(.state=="open")] | length'`) и строку `Blocked by:` в теле. Если открытых блокеров нет, выполните `gh issue edit <n> --remove-label status::blocked --add-label status::ready`.
   - **GitLab:** перечислите открытые issue со `status::blocked` (`glab issue list -R <project-url> --label status::blocked --output json`). Для каждого прочитайте нативные blocking links (`glab api --hostname <host> projects/<project-id>/issues/<n>/links`): запись с `link_type` `is_blocked_by` и `state` `opened` — открытый блокер; в GitLab Free таких links нет. Также прочитайте каждый `#<M>` в строках `Blocked by` описания (`glab issue view <n> -R <project-url> -F json`): такой блокер открыт, пока `glab issue view <M> -R <project-url> -F json` показывает `state` `opened`. Если открытых блокеров нет, выполните `glab issue update <n> -R <project-url> --unlabel status::blocked --label status::ready`.
   - **Локальный трекер** (`.scratch/<feature>/issues/*.md` или `docs/tasks/*.md`): проверьте `**Blocked by:** #<ID>` и установите `**Workflow:** status::ready`, когда все перечисленные блокеры разрешены.
   - Сообщите, какие тикеты перешли в `status::ready`, а какие остались заблокированы и чем.
9. **Очистка:** после подтверждения, что PR смержен и тикет закрыт, очистите локальное окружение, чтобы не накапливался мусор. При необходимости выйдите из worktree, удалите его через `git worktree remove <path>` и удалите локальную ветку через `git branch -d <branch-name>`.
```

## 3. Контракты

- **Вход (Input/Brief):** отправленная issue-ветка, тикет и точная целевая ветка PR.
- **Выход (Output/Report):** ссылка на созданный PR/MR либо конкретный blocker.

## 4. Архитектурная схема

![Контракт скила: вход, работа, результат](../diagrams/previews/skill-contract-fill.workflow.png)

## Источник

[SKILL.md](../../skills/first-party/pvmalove/to-pull-requests/SKILL.md)
