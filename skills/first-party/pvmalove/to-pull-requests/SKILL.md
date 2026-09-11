---
name: to-pull-requests
description: Prepare and open a pull request for an already pushed issue branch. Use after `/implement` offers it, or when the developer explicitly asks to open a PR.
disable-model-invocation: true
---

# To Pull Requests

Create the pull request for the named ticket. This is a manual command: never run it merely because `/implement` completed.

1. Resolve the ticket and its exact PR target branch from the ticket's `## Integration Branch` section, its parent epic, or `base_branch` for an epic-less ticket. If this cannot be resolved, stop and ask the developer.
2. Verify that the current branch matches `branch_pattern`, is neither `base_branch` nor `integration/*`, has no uncommitted changes, and has no commits absent from its upstream branch. If the branch has not been pushed, stop and ask the developer to return to `/implement`. Fetch its upstream and require `git rev-parse HEAD` to equal the upstream branch SHA; if it differs, fast-forward the clean local branch before continuing.
3. For a valid `backend-orchestration` opt-in, resolve the full current SHA with `git rev-parse HEAD` after that synchronization and validate it before any PR action:
   `python .harness/orchestration/coordinator.py --repo . qa evidence --ticket "#<ID>" --branch "<current-issue-branch>" --candidate-commit "<full-current-SHA>"`.
   Stop if the command rejects missing, unaccepted, or SHA-mismatched evidence. Do not rerun `/qa-gate` when this validation succeeds. If the project is not a valid opt-in, run `/qa-gate` if this repository provides it and stop on failure.
4. Prepare the PR body using `docs/agents/git-workflow.md` §3. A developer may manually run `pr-composer` in the coding application; otherwise fill in the template directly. Resolve the repository default branch and use `Closes #<ID>` only for that target, otherwise `Related to #<ID>`.
5. Ask the developer for explicit confirmation that the branch is ready to become a PR. Stop for their answer.
6. After approval, open the PR/MR with the tracker CLI and `--body-file <path>`. If the ticket carries `task-report::required`, publish its completion report on the ticket, unless the developer asked to skip it.
7. Return the PR/MR link in the primary session and ask whether the developer wants to review it. Never merge it. After the developer confirms the merge, close a `Related to #<ID>` ticket explicitly; for `Closes #<ID>`, verify that the tracker closed it.
\n<!--
Краткое описание (Summary): Навык создания pull request (PR) для отправленной ветки задачи. Включает проверку целевой ветки и коммитов, валидацию пройденного QA/доказательств (evidence), формирование тела PR и ожидание подтверждения от разработчика перед открытием PR.

Перевод:
---
name: to-pull-requests
description: Подготавливает и открывает pull request для уже отправленной ветки задачи. Использовать после того, как `/implement` предлагает это, или когда разработчик явно просит открыть PR.
disable-model-invocation: true
---

# К Pull Requests

Создать pull request для указанного тикета. Это команда, выполняемая вручную: никогда не запускайте её только потому, что завершился `/implement`.

1. Определите тикет и его точную целевую ветку для PR из раздела `## Integration Branch` тикета, его родительского эпика или `base_branch` для тикета без эпика. Если это не удается определить, остановитесь и спросите разработчика.
2. Убедитесь, что текущая ветка соответствует `branch_pattern`, не является ни `base_branch`, ни `integration/*`, не имеет незафиксированных изменений и не имеет коммитов, отсутствующих в её ветке upstream. Если ветка не была отправлена, остановитесь и попросите разработчика вернуться к `/implement`. Получите изменения из её upstream и потребуйте, чтобы `git rev-parse HEAD` был равен SHA ветки upstream; если они различаются, выполните fast-forward чистой локальной ветки перед продолжением.
3. Для валидного согласия (opt-in) на `backend-orchestration`, определите полный текущий SHA с помощью `git rev-parse HEAD` после этой синхронизации и проверьте его перед любым действием с PR:
   `python .harness/orchestration/coordinator.py --repo . qa evidence --ticket "#<ID>" --branch "<текущая-ветка-задачи>" --candidate-commit "<полный-текущий-SHA>"`.
   Остановитесь, если команда отклоняет недостающие, непринятые или не совпадающие по SHA доказательства. Не перезапускайте `/qa-gate`, если эта проверка проходит успешно. Если проект не является валидным согласием (opt-in), запустите `/qa-gate`, если этот репозиторий предоставляет его, и остановитесь при неудаче.
4. Подготовьте тело PR, используя `docs/agents/git-workflow.md` §3. Разработчик может вручную запустить `pr-composer` в приложении для программирования; в противном случае заполните шаблон напрямую. Определите ветку по умолчанию репозитория и используйте `Closes #<ID>` только для этой цели (target), в противном случае `Related to #<ID>`.
5. Запросите у разработчика явное подтверждение того, что ветка готова стать PR. Остановитесь в ожидании его ответа.
6. После одобрения откройте PR/MR с помощью CLI трекера и `--body-file <путь>`. Если тикет содержит `task-report::required`, опубликуйте отчет о его завершении в тикете, если только разработчик не просил пропустить это.
7. Верните ссылку на PR/MR в основной сессии и спросите, хочет ли разработчик просмотреть его. Никогда не объединяйте (merge) его. После того, как разработчик подтвердит объединение (merge), закройте тикет `Related to #<ID>` явно; для `Closes #<ID>` убедитесь, что трекер его закрыл.
-->\n