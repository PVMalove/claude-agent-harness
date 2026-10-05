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
3. Для действительного opt-in `backend-orchestration` после синхронизации разрешите полный текущий SHA через `git rev-parse HEAD` и проверьте его до любого действия с PR:
   `python .harness/orchestration/coordinator.py --repo . qa evidence --ticket "#<ID>" --branch "<current-issue-branch>" --candidate-commit "<full-current-SHA>"`.
   CLI выбирает единственный batch с принятым QA-доказательством для этого SHA, поэтому abandoned
   history с тем же тикетом и веткой не влияет на результат. Если один SHA принят в нескольких batch,
   остановитесь и повторите команду с явным `--batch <batch-id>`. Остановитесь, если команда отклоняет
   отсутствующие, непринятые или не совпадающие с SHA доказательства, если только тикет не помечен
   явно `pipeline::fast`. Для тикетов `pipeline::fast` проверку доказательств координатора пропустите.
   Затем прочитайте integration-запись, только на чтение: `python .harness/orchestration/coordinator.py --repo . integration status --ticket "#<ID>" --branch "<current-issue-branch>"`. Если она сообщает `unavailable`, integration ref не прочитан: остановитесь и сообщите разработчику. Если `stale`, integration ref сдвинулся, и старое QA не покрывает новую пару candidate/target: выполните `python .harness/orchestration/coordinator.py --repo . integration refresh --ticket "#<ID>" --branch "<current-issue-branch>"`. Чистый rebase обновляет только вашу issue-ветку и требует CI или local-QA новой пары (`verification.required` в `integration status`); повторный review не нужен. При `state: conflict` или отказе (грязный worktree, изменившийся remote) остановитесь и передайте данные разработчику по маршруту rebase из ADR 0012. Не создавайте dispatch и не перезапускайте QA сами. Отказ «integration-записи нет» PR не блокирует.
   Не запускайте `/qa-gate` повторно, когда эта проверка успешна; вместо этого из checkout ветки PR,
   проверенного на шаге 2, выполните `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/record-qa-gate-pass.sh"`,
   чтобы PR-hook принял этот SHA. Если проект не является действительным opt-in или тикет помечен
   `pipeline::fast`, запустите `/qa-gate`, если этот репозиторий его предоставляет, и остановитесь при
   ошибке.
4. Подготовьте тело PR по `docs/agents/git-workflow.md` §3. Храните его только в `.harness/.sandboxes/pr_body/pr-body-<issue>-<slug>.md`, никогда в `docs/tasks/`. Разработчик может вручную запустить `pr-composer` в coding application; иначе заполните шаблон напрямую. Разрешите default branch репозитория — GitHub: `gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`; GitLab: поле `default_branch` из `glab api --hostname <host> projects/<project-id>`. Используйте `Closes #<ID>` только когда целевая ветка из шага 1 и есть default branch, иначе `Related to #<ID>`. GitLab закрывает issue по `Closes #<ID>` только когда MR попадает в default branch, а проект может отключить это автозакрытие, поэтому шаг 7 всегда проверяет состояние тикета.
5. Попросите разработчика явно подтвердить, что ветка готова стать PR. Остановитесь для его ответа.
6. После одобрения откройте PR/MR из checkout ветки, проверенного на шаге 2. `<target-branch>` — цель из шага 1; без неё оба CLI открыли бы PR/MR в default branch. `<title>` — заголовок тикета в одинарных кавычках, каждый апостроф записывается как `'\''` в POSIX-shell или как `''` в PowerShell. Выполняйте одну команду публикации на вызов shell:
   - **GitHub:** `gh pr create --base <target-branch> --title '<title>' --body-file <path>`.
   - **GitLab:** `glab mr create -R <project-url> --target-branch <target-branch> --title '<title>' --description-file <path> --yes`. MR — это `!<iid>`, где `<iid>` — последний сегмент URL `.../-/merge_requests/<iid>`, который печатает команда.

   Удаляйте файл тела `.harness/.sandboxes/pr_body/` только после успеха этой команды; при ошибке сохраните его для повторной попытки. Если тикет несёт `task-report::required`, опубликуйте его completion report в тикете, если разработчик не попросил пропустить это: запишите отчёт в `.harness/.sandboxes/pr_body/issue-comment-<issue>-<slug>.md`, затем опубликуйте его через `gh issue comment <ID> --body-file <path>` в GitHub или `glab api --hostname <host> projects/<project-id>/issues/<ID>/notes -F body=@<path>` в GitLab.
7. Верните ссылку на PR/MR в основной сессии (в GitLab также как `!<iid>`) и спросите, хочет ли разработчик его проверить. Никогда не выполняйте merge. Действуйте с тикетом только после того, как разработчик подтвердит merge:
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
