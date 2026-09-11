---
name: implement
description: "Implement a piece of work as a gated coordinator pipeline: architect, developer, code review, and independent QA."
disable-model-invocation: true
---

# Implement

**Objective:** Take one ticket through architect → developer → code-review → QA, pausing for the
developer's explicit approval at every gate, and hand the finished candidate to `/to-pull-requests`.

This session **is** the coordinator. It drives `coordinator.py` — batch, dispatch, report, decide —
and never edits implementation files itself. Work is done by dispatched roles.

It is step 5 of the delivery chain — `/grill-with-docs` → `/to-spec` → `/to-tickets` →
**`/implement <id>`** → `/to-pull-requests` — and it takes exactly one ticket from that
decomposition.

## Context and effort budget

Start `/implement` in a new, clean context. If this session has completed unrelated exploration or
another ticket, retain the ticket reference in the tracker and ask the developer to run `/clear`
before beginning; do not carry old dispatches, reports, or exploratory output into this batch.

Use `medium` effort for the coordinator and architect by default. A project assignment plan may
declare its own effort; otherwise pass `--effort medium` when creating the architect dispatch. Use a
higher effort only when the developer explicitly approves it for a named hard-to-reverse decision.

## Route selection

`/implement` needs the coordinator CLI, which the harness installs into the project itself. Every
command in this skill is invoked exactly like this — there is no `harness` executable to find, and no
`coordinator.py` on PATH:

```bash
python .harness/orchestration/coordinator.py --repo . <команда>
```

Prove the route with one command, which doubles as the inventory of what is already in flight:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch status
```

Exit 0 means the CLI runs and its state directory is readable. Do not go looking for a `harness`
command: the packager CLI lives in the harness repository, not in a harnessed project, so
`harness health` is not runnable here and its absence says nothing about this route.

`.harness/orchestration.json` is **optional**. Without it the coordinator defaults the zone to the
whole repository (`repository`) and takes the role's model and effort from this session, passed as
`--model`/`--effort` on `dispatch create`. With it, the project owns zones, models, effort, and the
per-role `transport`.

If that command is missing or exits non-zero, do not repair or infer an opt-in: stop and tell the
user to run `/fast-implement` instead, which is the ungated single-session path.

Process one ticket to a terminal batch state before starting another. Never track overlapping
approvals for unrelated tickets.

## Phase 1: Pre-flight

1. **Resolve the ticket.**
    - **A specific ticket is named** (an issue number, URL, or a `.scratch/<feature>/issues/NN-*.md` path): use it.
        - **Fail fast on `hitl`:** if it carries `hitl` (see `docs/agents/triage-labels.md`), stop immediately and tell the user to run `/to-guide` instead — don't check anything else on this ticket, this agent only implements `afk` work.
    - **An epic is named** (carries `workflow::specs`, or is otherwise the parent of a decomposition) rather than a specific ticket: pick the next ticket yourself instead of asking. Never auto-pick a `hitl` ticket — that execution mode always routes through `/to-guide`, not this agent.
        - **GitHub/GitLab:** run the same frontier query `/wayfinder` uses (`docs/agents/issue-tracker.md#wayfinding-operations`), scoped to the epic's sub-issues and filtered to `afk` — open, unblocked, unclaimed, first in decomposition order. Claim the chosen ticket (`gh issue edit <n> --add-assignee @me` on GitHub, `glab issue update <n> --assignee @me` on GitLab) before any other write, the same way `/wayfinder` claims a ticket, so a concurrent session (e.g. a parallel worktree) doesn't pick the same one. If the filtered frontier is empty, stop and tell the user why: if open, unblocked, unclaimed tickets remain but all are `hitl`, say so and point at `/to-guide`; otherwise explain that nothing is unblocked yet, or everything is already claimed.
        - **Local tracker:** read each `.scratch/<feature>/issues/NN-*.md` file in filename order — a purely linear chain — and take the first one that is both `**Workflow:** workflow::ready` and `**Execution:** afk`. If tickets remain but every `workflow::ready` one is `hitl`, say so — naming them — and point at `/to-guide` instead of picking one.
        - Once chosen this way, treat the ticket exactly like one named explicitly for the rest of this process.
    - **Nothing is named:** when this repo defines a git workflow doc (e.g. `docs/agents/git-workflow.md`) with an "Issue First" rule, stop and ask the user to name an existing ticket or run `/to-spec`/`/to-tickets` first — don't start the work. Otherwise, skip this check entirely.
2. **Check blockers**, on the resolved ticket. If it carries `workflow::blocked` (see `docs/agents/triage-labels.md`), check its blockers using this repo's tracker (native GitHub/GitLab dependency links, or the `Blocked by:`/`**Blocked by:**` field — see `docs/agents/issue-tracker.md`).
    - Any blocker still open → stop and tell the user which ones. Don't start the work.
    - All blockers closed/resolved → clear the block before proceeding:
        - **GitHub/GitLab:** replace the `workflow::blocked` label with `workflow::ready`.
        - **Local tracker:** set the file's `**Workflow:**` line to `workflow::ready`.
3. **Mark it in progress**, once you actually start work:
    - **GitHub/GitLab:** set the `workflow::in-progress` label.
    - **Local tracker:** set the file's `**Workflow:**` line to `workflow::in-progress`.
4. **Git pre-flight, before any dispatch:**
    - Resolve the exact integration branch from the ticket's `## Integration Branch` section or,
      for a child ticket that omits it, from its parent epic. An absent value is a blocker; do not
      infer a branch from memory, the current checkout, or a service name.
    - Create the batch's issue branch `feature/issue-<ID>-<slug>` from that integration branch. It
      must match `branch_pattern`; the coordinator refuses a base or `integration/*` branch.
    - Create the batch's worktree for that branch with plain `git worktree add`, and **stay in the
      main checkout**. This session is the coordinator, not a worker: its state lives in
      `.harness/orchestration/state/` relative to `--repo`, so entering the worktree would fork that
      state into a second copy and the batch you create there would be invisible from the repository
      root. You only need the worktree's path — `batch create` takes it as `--worktree`.
    - Do not use a worktree tool that switches this session into the tree, and do not edit
      `.claude/settings.local.json` or any other tool configuration to make one behave. Changing a
      project's configuration is not part of implementing a ticket.

## Phase 2: The batch

**First look at what already exists for this ticket.** The `dispatch status` output from route
selection lists every dispatch the coordinator knows, each with its `ticket`, `batch_id`, `state` and
`stale` flag. If any entry names this ticket, stop before proposing anything and show the developer
what you found: which batch, which role, what state, and how long it has been silent.

A leftover dispatch from an earlier attempt is the exact situation this pipeline exists to catch, so
treat it as evidence, not as debris. Never reuse it, never delete it, and never open a second batch
beside it — the coordinator refuses an overlapping zone anyway, and a silent retry is how the
original incident burned a usage window. The developer decides what happens.

Close the old batch through the CLI, never by editing anything under
`.harness/orchestration/state/`. Those records are the audit trail; hand-editing them destroys the
one thing this pipeline produces.

- **Its report is in and awaiting a decision** — `batch decide` with `accept`, `override-warning`,
  `retry`, `block` or `fail`, as usual.
- **It can no longer report at all** — a worker that died before its model self-report never will,
  and a batch with no pending report cannot be decided. That is what `batch abandon` is for:

  ```bash
  python .harness/orchestration/coordinator.py --repo . batch abandon \
    --batch <batch-id> --approved-by '<кто>' --approved-at <ISO-8601> \
    --reason '<почему решение больше недостижимо>'
  ```

  It requires an explicit approval and a reason, marks the batch `failed`, closes every open
  dispatch, and deletes nothing. `batch list --open` shows what is still unclosed; `--ticket <id>`
  narrows it to one ticket.

Only once nothing is open for the ticket do you continue.

Then read `.harness/orchestration/roles/` and `.harness/orchestration/playbook.md`. Propose one
batch — ticket, issue branch/worktree, zone, Definition of Done, prohibitions, checks, dependencies,
risk gates — and show it to the developer.

Keep this Gate 0 proposal compact: derive it from the current ticket, its direct dependencies, the
role manifests, and the named project contract. Do not inspect old batch state or previous-role
templates to reconstruct a new brief. State only observable acceptance criteria, the narrowest
credible zone, explicit prohibitions, and the commands that developer and QA must later run.

The brief is the only channel a dispatched role has, so this repo's own delivery rules must be
written into the batch rather than assumed. Read `docs/agents/git-workflow.md` and carry its
implementation contract into the `--definition-of-done` entries verbatim enough to be checkable. In
particular, when that doc mandates TDD, one Definition-of-Done entry must say so — for example
`write the failing test first at the seams the architect named, then make it pass` — because a
dispatched developer inherits nothing from `/tdd`: it is not this session, and `verification_commands`
only run tests afterwards, they never require that the test came first.

**Gate 0 — the plan.** Stop for explicit approval. Only after it, run `batch create` and then
`batch approve`.

## Phase 3: The five gates

The sequence below is fixed. Run every step, in this order, for every ticket — a low-risk change
does not earn a shorter path here; a ticket that does not deserve the ceremony belongs in
`/fast-implement` instead.

```text
architect ─► HUMAN approve ─► developer ─► code-review ─► HUMAN approve ─► qa ─┐
                                  ▲                                            │
                                  └────── qa findings: developer fixes ◄────────┤
                                                                                │
                            final report ─► publish ─► HUMAN runs /to-pull-requests
```

Every gate is the same shape: propose → **stop for the developer's explicit approval** →
`dispatch create` → `dispatch send` → watch → `report submit` → `batch decide`. Never create a brief
before its approval, and never decide on a report for the developer. A worker report is evidence
only: accept, override, retry, block, and fail remain coordinator decisions.

1. **Architect.** `--role architect`. Read-only: it analyses the architecture as it exists today in
   this repository and proposes the plan — boundaries, viable options, the selected option,
   trade-offs, risks, acceptance criteria, and the seams the tests should sit on — with repository
   evidence for each. **This report is what the human approves before any code is written.** The
   coordinator enforces the order: `dispatch create --role developer` fails until an architect
   report for this batch has been accepted, so the step cannot be skipped from the CLI either. Ask
   for one concise decision brief; its completion report links to that brief rather than duplicating
   it. The architect uses targeted evidence only and must not run the batch's full verification suite
   merely to establish a baseline; developer and independent QA own those checks.
2. **Developer.** `--role developer`, after the accepted architect report. It implements on the
   batch's own issue branch in its isolated worktree test-first — the failing test at the architect's
   seams before the code that satisfies it, when the batch's Definition of Done requires TDD — runs
   the checks, commits, and pushes that issue branch, never a base or `integration/*` branch. Its
   commit SHA is the candidate. Its report's `changed_files` is where you see whether tests actually
   came with the change: an implementation-only diff against a TDD Definition of Done is a
   `--decision retry`, not an accept. Then run `risk assess` on that SHA.
3. **Code review.** `--role code-review --candidate-commit <sha>`, pinned to the assessed candidate,
   read-only, run for every candidate — the risk assessment decides only whether review is
   *mandatory*, never whether it is allowed. Standards and Spec stay separate evidence. A blocker
   requires `--decision retry`; a warning requires `--decision override-warning` with a recorded note.
   **Show the human the reviewed changes and the two axes, and stop for their approval to spend QA.**
4. **QA.** `--role qa --candidate-commit <sha>` after the accepted review. QA is independent: it runs
   in the clean-room lane (`qa run`) against the pinned SHA, not through a transport, and not on the
   developer's own checks. A QA finding is not a defeat: `batch decide --decision retry` sends the
   work back to the developer with the findings, and the loop re-runs risk assessment, review, and QA
   on the new candidate once the fix reports. Repeat until QA is green.
5. **Final report, then publish.** After accepted green QA, give the human the closing report
   *before* anything is published: ticket, candidate SHA, what each role concluded, accepted QA
   evidence and its artifact path, residual risks, and what is deliberately left out. Then create the
   publish dispatch (`--role developer --purpose publish --candidate-commit <sha>`). Never hand a
   publish brief to `dispatch send`/an adapter; use `dispatch publish`, the coordinator's own
   verified boundary, which pushes exactly the accepted SHA.

## Phase 4: Watching a dispatch

Between `dispatch send` and the report, this session is the watchdog. Poll:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch status --batch <batch-id>
```

- **Model self-report.** Every dispatched role, on any transport, confirms its actually active model
  as its first action (`dispatch self-report --dispatch <id> --model <model>`). A mismatch against
  the immutable brief blocks the dispatch immediately, and the coordinator refuses its completion
  report. Surface it to the developer as a blocker; the fix is a new dispatch, never an edited brief.
- **Heartbeat.** A role calls `dispatch heartbeat --dispatch <id>` while it works. When `dispatch
  status` reports `stale` for a dispatch, stop waiting and tell the developer which dispatch went
  silent and for how long. Do not silently keep waiting: a stalled dispatch spends the usage window
  and produces nothing.

For an `in-process` transport, `dispatch send` takes no adapter: it records the handoff, then this
session must immediately launch the role subagent against the same immutable brief in the batch's
worktree. Launch is the next action after `dispatch send`; do not inspect old templates, reports, or
configuration before it. The subagent performs the self-report and heartbeat calls itself. For
`orca`, pass `--adapter .harness/orchestration/orca_adapter.py`.

## Phase 5: PR & wrap-up

Offer `/to-pull-requests <ticket>` as the next command. Do not invoke it automatically, open or merge
a PR, write to an integration branch, or close the ticket in this skill.
\n
<!--
Краткое описание (Summary): Навык `/implement` реализует задачу как конвейер, управляемый координатором. Процесс разделен на гейты: архитектор, разработчик, код-ревью и QA. Координатор не пишет код, а распределяет задачи по ролям и останавливается для явного одобрения человеком на каждом этапе, прежде чем отправить финального кандидата в `/to-pull-requests`.

Перевод:
---
name: implement
description: "Реализация части работы в виде конвейера координатора с гейтами: архитектор, разработчик, ревью кода и независимое QA."
disable-model-invocation: true
---

# Implement

**Objective:** Провести один тикет через этапы архитектор → разработчик → ревью кода → QA, делая паузы для явного утверждения разработчиком на каждом гейте, и передать готового кандидата в `/to-pull-requests`.

Эта сессия **является** координатором. Она управляет `coordinator.py` — создание батчей (пакетов работ), диспетчеризация, отчеты, решения — и никогда сама не редактирует файлы реализации. Работа выполняется назначенными ролями.

Это шаг 5 в цепочке поставки — `/grill-with-docs` → `/to-spec` → `/to-tickets` →
**`/implement <id>`** → `/to-pull-requests` — и он берет ровно один тикет из этой декомпозиции.

## Контекст и бюджет усилий (Context and effort budget)

Запускайте `/implement` в новом, чистом контексте. Если эта сессия завершила не связанное исследование или другой тикет, сохраните ссылку на тикет в трекере и попросите разработчика запустить `/clear` перед началом; не переносите старые диспетчеризации, отчеты или результаты исследований в этот батч.

По умолчанию используйте `medium` усилия (effort) для координатора и архитектора. План назначения проекта может объявлять свои собственные усилия; в противном случае передавайте `--effort medium` при создании диспетчеризации архитектора. Используйте более высокие усилия только тогда, когда разработчик явно одобряет это для названного трудноотменяемого решения.

## Выбор маршрута (Route selection)

`/implement` нуждается в CLI координатора, который harness устанавливает в сам проект. Каждая команда в этом навыке вызывается именно так — здесь нет исполняемого файла `harness` для поиска, и нет `coordinator.py` в PATH:

```bash
python .harness/orchestration/coordinator.py --repo . <команда>
```

Подтвердите маршрут одной командой, которая также служит инвентаризацией того, что уже находится в работе:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch status
```

Код возврата 0 означает, что CLI работает, и его каталог состояния доступен для чтения. Не ищите команду `harness`: CLI упаковщика находится в репозитории harness, а не в проекте с harness, поэтому `harness health` здесь не запустится, и его отсутствие ничего не говорит об этом маршруте.

`.harness/orchestration.json` является **необязательным**. Без него координатор по умолчанию назначает зону на весь репозиторий (`repository`) и берет модель и усилия роли из этой сессии, переданные как `--model`/`--effort` в `dispatch create`. С ним проект владеет зонами, моделями, усилиями и параметром `transport` для каждой роли.

Если эта команда отсутствует или завершается с ненулевым кодом, не пытайтесь исправить это или вывести согласие (opt-in): остановитесь и скажите пользователю запустить `/fast-implement` вместо этого, что является путем одной сессии без гейтов.

Доведите один тикет до конечного состояния батча перед запуском другого. Никогда не отслеживайте перекрывающиеся утверждения для несвязанных тикетов.

## Фаза 1: Предполетная подготовка (Phase 1: Pre-flight)

1. **Разрешение тикета (Resolve the ticket).**
    - **Назван конкретный тикет** (номер проблемы, URL или путь `.scratch/<feature>/issues/NN-*.md`): используйте его.
        - **Fail fast для `hitl`:** если он имеет метку `hitl` (см. `docs/agents/triage-labels.md`), немедленно остановитесь и скажите пользователю запустить `/to-guide` вместо этого — не проверяйте ничего другого в этом тикете, этот агент реализует только `afk` работу.
    - **Назван эпик** (имеет `workflow::specs` или иным образом является родителем декомпозиции), а не конкретный тикет: выберите следующий тикет самостоятельно, вместо того чтобы спрашивать. Никогда не выбирайте автоматически тикет `hitl` — этот режим выполнения всегда маршрутизируется через `/to-guide`, а не через этого агента.
        - **GitHub/GitLab:** выполните тот же фронтирный запрос, который использует `/wayfinder` (`docs/agents/issue-tracker.md#wayfinding-operations`), ограниченный подзадачами эпика и отфильтрованный по `afk` — открытый, незаблокированный, нераспределенный, первый в порядке декомпозиции. Назначьте выбранный тикет себе (`gh issue edit <n> --add-assignee @me` на GitHub, `glab issue update <n> --assignee @me` на GitLab) перед любой другой записью, так же, как `/wayfinder` забирает тикет, чтобы параллельная сессия (например, параллельное рабочее дерево) не выбрала тот же самый. Если отфильтрованный фронтир пуст, остановитесь и скажите пользователю почему: если открытые, незаблокированные, неназначенные тикеты остались, но все они `hitl`, скажите об этом и укажите на `/to-guide`; в противном случае объясните, что ничего еще не разблокировано, или все уже назначено.
        - **Локальный трекер:** прочитайте каждый файл `.scratch/<feature>/issues/NN-*.md` в порядке имен файлов — чисто линейная цепочка — и возьмите первый, который имеет и `**Workflow:** workflow::ready`, и `**Execution:** afk`. Если тикеты остаются, но каждый `workflow::ready` является `hitl`, скажите об этом — назвав их — и укажите на `/to-guide`, вместо того чтобы выбирать один.
        - После того, как он выбран таким образом, относитесь к тикету точно так же, как к явно названному для остальной части этого процесса.
    - **Ничего не названо:** когда этот репозиторий определяет документ git workflow (например, `docs/agents/git-workflow.md`) с правилом "Сначала проблема" (Issue First), остановитесь и попросите пользователя назвать существующий тикет или сначала запустить `/to-spec`/`/to-tickets` — не начинайте работу. В противном случае пропустите эту проверку полностью.
2. **Проверьте блокировщики (Check blockers)** на разрешенном тикете. Если он имеет метку `workflow::blocked` (см. `docs/agents/triage-labels.md`), проверьте его блокировщики, используя трекер этого репозитория (нативные ссылки зависимостей GitHub/GitLab или поле `Blocked by:`/`**Blocked by:**` — см. `docs/agents/issue-tracker.md`).
    - Любой блокировщик всё ещё открыт → остановитесь и скажите пользователю какие именно. Не начинайте работу.
    - Все блокировщики закрыты/решены → снимите блокировку перед продолжением:
        - **GitHub/GitLab:** замените метку `workflow::blocked` на `workflow::ready`.
        - **Локальный трекер:** установите строку `**Workflow:**` в файле на `workflow::ready`.
3. **Отметьте его в процессе (Mark it in progress)**, как только вы фактически начнете работу:
    - **GitHub/GitLab:** установите метку `workflow::in-progress`.
    - **Локальный трекер:** установите строку `**Workflow:**` в файле на `workflow::in-progress`.
4. **Предполетная подготовка Git, перед любой диспетчеризацией (Git pre-flight, before any dispatch):**
    - Определите точную интеграционную ветку (integration branch) из раздела тикета `## Integration Branch` или, для дочернего тикета, в котором он опущен, из его родительского эпика. Отсутствующее значение является блокировщиком; не выводите ветку по памяти, из текущего checkout или имени сервиса.
    - Создайте ветку проблемы (issue branch) для батча `feature/issue-<ID>-<slug>` из этой интеграционной ветки. Она должна соответствовать `branch_pattern`; координатор отказывается от ветки base или `integration/*`.
    - Создайте рабочее дерево (worktree) батча для этой ветки с помощью простого `git worktree add`, и **оставайтесь в главном checkout**. Эта сессия является координатором, а не рабочим (worker): ее состояние живет в `.harness/orchestration/state/` относительно `--repo`, поэтому вход в рабочее дерево разветвит это состояние во вторую копию, и батч, который вы там создадите, будет невидим из корня репозитория. Вам нужен только путь рабочего дерева — `batch create` принимает его как `--worktree`.
    - Не используйте инструмент рабочего дерева, который переключает эту сессию в дерево, и не редактируйте `.claude/settings.local.json` или любую другую конфигурацию инструмента, чтобы заставить его вести себя иначе. Изменение конфигурации проекта не является частью реализации тикета.

## Фаза 2: Батч (Phase 2: The batch)

**Сначала посмотрите на то, что уже существует для этого тикета.** Вывод `dispatch status` из выбора маршрута перечисляет каждую диспетчеризацию, которую знает координатор, каждую с её `ticket`, `batch_id`, `state` и флагом `stale`. Если какая-либо запись называет этот тикет, остановитесь перед предложением чего-либо и покажите разработчику, что вы нашли: какой батч, какая роль, какое состояние и как долго он молчал.

Оставшаяся диспетчеризация от предыдущей попытки — это именно та ситуация, для выявления которой существует этот конвейер, поэтому относитесь к ней как к доказательству, а не как к мусору. Никогда не переиспользуйте её, никогда не удаляйте её, и никогда не открывайте второй батч рядом с ней — координатор в любом случае отказывается от перекрывающейся зоны, и скрытая повторная попытка — это то, как первоначальный инцидент сжег окно использования. Разработчик решает, что произойдет.

Закрывайте старый батч через CLI, никогда не редактируя ничего в
`.harness/orchestration/state/`. Эти записи являются аудиторским следом; ручное их редактирование разрушает единственную вещь, которую производит этот конвейер.

- **Её отчет получен и ожидает решения** — `batch decide` с `accept`, `override-warning`, `retry`, `block` или `fail`, как обычно.
- **Она больше вообще не может отчитываться** — рабочий, который умер до того, как его модель отчиталась о себе, никогда этого не сделает, и батч без ожидающего отчета не может быть решен. Для этого и нужен `batch abandon`:

  ```bash
  python .harness/orchestration/coordinator.py --repo . batch abandon     --batch <batch-id> --approved-by '<кто>' --approved-at <ISO-8601>     --reason '<почему решение больше недостижимо>'
  ```

  Это требует явного утверждения и причины, отмечает батч как `failed`, закрывает каждую открытую диспетчеризацию и ничего не удаляет. `batch list --open` показывает то, что всё еще не закрыто; `--ticket <id>` сужает это до одного тикета.

Продолжайте только после того, как для тикета ничего не будет открыто.

Затем прочитайте `.harness/orchestration/roles/` и `.harness/orchestration/playbook.md`. Предложите один батч — тикет, ветка проблемы/рабочее дерево, зона, Определение Выполненного (Definition of Done), запреты, проверки, зависимости, гейты рисков — и покажите это разработчику.

Держите это предложение Гейта 0 компактным: выведите его из текущего тикета, его прямых зависимостей, манифестов ролей и названного контракта проекта. Не инспектируйте состояние старого батча или шаблоны предыдущих ролей, чтобы реконструировать новый бриф. Указывайте только наблюдаемые критерии приемки, самую узкую достоверную зону, явные запреты и команды, которые разработчик и QA должны будут запустить позже.

Бриф — это единственный канал, который есть у назначенной роли, поэтому собственные правила поставки этого репозитория должны быть записаны в батч, а не предполагаться. Прочитайте `docs/agents/git-workflow.md` и перенесите его контракт реализации в записи `--definition-of-done` достаточно дословно, чтобы их можно было проверить. В частности, когда этот документ требует TDD, одна запись Definition-of-Done должна говорить об этом — например, `сначала напишите падающий тест на швах (seams), названных архитектором, затем заставьте его проходить` — потому что назначенный разработчик ничего не наследует от `/tdd`: это не эта сессия, а `verification_commands` только запускают тесты после этого, они никогда не требуют, чтобы тест был первым.

**Гейт 0 — план (Gate 0 — the plan).** Остановитесь для явного утверждения. Только после него запустите `batch create`, а затем `batch approve`.

## Фаза 3: Пять гейтов (Phase 3: The five gates)

Последовательность ниже фиксирована. Выполняйте каждый шаг, в этом порядке, для каждого тикета — изменение с низким уровнем риска не заслуживает здесь более короткого пути; тикету, который не заслуживает этой церемонии, место вместо этого в `/fast-implement`.

```text
architect ─► HUMAN approve ─► developer ─► code-review ─► HUMAN approve ─► qa ─┐
                                  ▲                                            │
                                  └────── qa findings: developer fixes ◄────────┤
                                                                                │
                            final report ─► publish ─► HUMAN runs /to-pull-requests
```

Каждый гейт имеет одинаковую форму: предложить → **остановиться для явного утверждения разработчиком** → `dispatch create` → `dispatch send` → смотреть → `report submit` → `batch decide`. Никогда не создавайте бриф до его утверждения и никогда не принимайте решение по отчету за разработчика. Отчет рабочего — это только доказательство: accept, override, retry, block и fail остаются решениями координатора.

1. **Архитектор (Architect).** `--role architect`. Только для чтения: он анализирует архитектуру в том виде, в каком она существует сегодня в этом репозитории, и предлагает план — границы, жизнеспособные варианты, выбранный вариант, компромиссы, риски, критерии приемки и швы (seams), на которых должны сидеть тесты — с доказательствами из репозитория для каждого из них. **Этот отчет — это то, что человек утверждает перед написанием какого-либо кода.** Координатор обеспечивает порядок: `dispatch create --role developer` завершается неудачей, пока отчет архитектора для этого батча не будет принят, поэтому этот шаг также нельзя пропустить из CLI. Запросите один краткий бриф решения; его отчет о завершении ссылается на этот бриф, а не дублирует его. Архитектор использует только целевые доказательства и не должен запускать полный набор проверок батча только для установления базового уровня (baseline); разработчик и независимый QA владеют этими проверками.
2. **Разработчик (Developer).** `--role developer`, после принятого отчета архитектора. Он реализует в собственной ветке проблемы батча в его изолированном рабочем дереве сначала тесты (test-first) — падающий тест на швах архитектора перед кодом, который его удовлетворяет, когда Definition of Done батча требует TDD — запускает проверки, коммитит и пушит эту ветку проблемы, никогда ветку base или `integration/*`. Его SHA коммита является кандидатом. В `changed_files` его отчета вы видите, действительно ли тесты появились вместе с изменением: диф (diff) только с реализацией против TDD Definition of Done — это `--decision retry`, а не accept. Затем запустите `risk assess` на этом SHA.
3. **Ревью кода (Code review).** `--role code-review --candidate-commit <sha>`, прикрепленный к оцененному кандидату, только для чтения, запускается для каждого кандидата — оценка рисков решает только то, является ли ревью *обязательным*, а не то, разрешено ли оно. Стандарты и Спецификации остаются отдельными доказательствами. Блокировщик требует `--decision retry`; предупреждение требует `--decision override-warning` с записанной заметкой. **Покажите человеку проверенные изменения и две оси, и остановитесь для его утверждения, чтобы потратить ресурсы на QA.**
4. **QA.** `--role qa --candidate-commit <sha>` после принятого ревью. QA независим: он запускается на полосе чистой комнаты (`qa run`) против закрепленного SHA, не через транспорт и не на собственных проверках разработчика. Находка QA не является поражением: `batch decide --decision retry` отправляет работу обратно разработчику с находками, и цикл повторно запускает оценку рисков, ревью и QA на новом кандидате после отчета об исправлении. Повторяйте, пока QA не станет зеленым.
5. **Финальный отчет, затем публикация (Final report, then publish).** После принятого зеленого QA предоставьте человеку закрывающий отчет *перед* публикацией чего-либо: тикет, SHA кандидата, выводы каждой роли, принятые доказательства QA и путь к его артефакту, остаточные риски и то, что намеренно оставлено за рамками. Затем создайте диспетчеризацию публикации (`--role developer --purpose publish --candidate-commit <sha>`). Никогда не передавайте бриф публикации в `dispatch send`/адаптер; используйте `dispatch publish`, собственную верифицированную границу координатора, которая пушит именно принятый SHA.

## Фаза 4: Наблюдение за диспетчеризацией (Phase 4: Watching a dispatch)

Между `dispatch send` и отчетом эта сессия является сторожевым псом (watchdog). Опрашивайте:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch status --batch <batch-id>
```

- **Отчет о себе от модели (Model self-report).** Каждая назначенная роль, на любом транспорте, подтверждает свою фактически активную модель в качестве первого действия (`dispatch self-report --dispatch <id> --model <model>`). Несоответствие с неизменяемым брифом немедленно блокирует диспетчеризацию, и координатор отклоняет её отчет о завершении. Покажите это разработчику как блокировщик; исправлением является новая диспетчеризация, а не отредактированный бриф.
- **Пульс (Heartbeat).** Роль вызывает `dispatch heartbeat --dispatch <id>`, пока она работает. Когда `dispatch status` сообщает `stale` (устаревший) для диспетчеризации, перестаньте ждать и скажите разработчику, какая диспетчеризация замолчала и на как долго. Не продолжайте молча ждать: застопорившаяся диспетчеризация тратит окно использования и ничего не производит.

Для транспорта `in-process` `dispatch send` не принимает адаптер: он записывает передачу (handoff), затем эта сессия должна немедленно запустить субагента роли против того же неизменяемого брифа в рабочем дереве батча. Запуск — это следующее действие после `dispatch send`; не инспектируйте старые шаблоны, отчеты или конфигурацию перед этим. Субагент сам выполняет вызовы отчета о себе и пульса. Для `orca` передайте `--adapter .harness/orchestration/orca_adapter.py`.

## Фаза 5: PR и завершение (Phase 5: PR & wrap-up)

Предложите `/to-pull-requests <ticket>` как следующую команду. Не вызывайте её автоматически, не открывайте и не сливайте PR, не пишите в интеграционную ветку и не закрывайте тикет в этом навыке.
-->
\n