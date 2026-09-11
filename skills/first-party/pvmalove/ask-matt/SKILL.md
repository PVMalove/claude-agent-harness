---
name: ask-matt
description: Ask which skill or flow fits your situation. A router over the skills in this repo.
disable-model-invocation: true
---

# Ask Matt

You don't remember every skill, so ask.

A **flow** is a path through the skills. Most paths run along one **main flow**, and two **on-ramps** merge onto it. Everything else is standalone, or a vocabulary layer that runs underneath.

## The main flow: idea → ship

The route most work travels. You have an idea and want it built.

1. **`/grill-with-docs`** — sharpen the idea by interview. Start here whenever you are **working in a working directory**: it's stateful, retaining what it learns in `CONTEXT.md` and ADRs. (No working directory? Use `/grill-me` — see Standalone. Both run the same `/grilling` primitive; `grill-with-docs` is the one that leaves a paper trail, which makes it the better of the two whenever a repo is there to leave it in.)
2. **Branch — can you settle every question in conversation?** If a question needs a runnable answer (state, business logic, a UI you have to see), detour through a prototype, bridged by **`/handoff`** in both directions (a prototype lives in its own directory, which is exactly what `/handoff` is for — see Phase boundaries):
   - **`/handoff`** out, then open a fresh session against that file,
   - **`/prototype`** to answer the question with throwaway code,
   - **`/handoff`** back what you learned, and reference it from the original idea thread.
3. **Branch — is this a multi-session build?**
   - **Yes** → **`/to-spec`** (turn the thread into a spec), then **`/to-tickets`** to split it into tracer-bullet tickets, each declaring its **blocking edges**. On a local tracker that's one file per ticket under `.scratch/<feature>/issues/`, worked blockers-first by hand; on a real tracker the edges become native blocking links, so any ticket whose blockers are done can be grabbed — kick off **`/implement`** per ticket, **`/clear`ing context between each one**. Each ticket is self-contained, so the last one's context is disposable.
   - **No** → **`/implement`** right here, in the same context window.

   Either way, **`/implement`** builds each issue by driving **`/tdd`** internally — one red-green slice at a time — then asks the developer whether to run **`/code-review`**, a two-axis review (Standards + Spec) of the diff. After the report returns, or the developer declines review, it asks for approval to commit and push the current issue branch, then offers manual **`/to-pull-requests`**. Reach for **`/tdd`** on its own when you just want to build a concrete behaviour test-first without a full spec, and **`/code-review`** on its own whenever you want to review a branch or PR against a fixed point.

### Context hygiene

Keep steps 1–3 in **one unbroken context window** — don't compact or clear until after `/to-tickets` — so the grilling, spec, and tickets all build on the same thinking. Each `/implement` then starts fresh, working from the ticket.

The limit on this is the **[smart zone](https://www.aihero.dev/ai-coding-dictionary/smart-zone)**: the window (~150k tokens on state-of-the-art models) within which the model still reasons sharply. If a session approaches it before `/to-tickets`, don't push on degraded — `/compact` at the nearest phase boundary and carry on (see Phase boundaries).

## On-ramps

A starting situation that generates work, then merges onto the main flow.

- **Bugs and requests piling up** → **`/triage`**. It moves issues through triage roles and produces agent-ready issues, which **`/implement`** later picks up.

  Triage is only for issues **you didn't create** — bug reports, incoming feature requests, anything that arrives raw. Tickets that `/to-tickets` produced are already agent-ready, so **don't triage them**.

- **Something's broken** → **`/diagnosing-bugs`**. For the hard ones: the bug that resists a first glance, the intermittent flake, the regression that crept in between two known-good states. It refuses to theorise until it has a **tight feedback loop** — one command that already goes red on *this* bug — then fixes with a regression test. Its post-mortem hands off to **`/improve-codebase-architecture`** when the real finding is that there's no good seam to lock the bug down.

- **A huge, foggy effort — a greenfield project or a huge feature build, too big for one session** → **`/wayfinder`**, the most cognitively demanding flow here. When the way from here to the destination isn't visible yet, it charts a **shared map** of **decision tickets** on the issue tracker and resolves them one at a time — producing **decisions, not deliverables** — until the fog is pushed back and the way is clear. Where **`/grill-with-docs`** sharpens an idea you can hold in one session, wayfinder is for the idea you can't — and it's slower and denser, so save it for exactly that, never a well-scoped feature.

  When the map clears, **it hands off, it doesn't build**: merge onto the main flow at **`/to-spec`**, which collapses the map's linked decisions into a buildable plan, then `/to-tickets` and `/implement` as usual. Looping the map straight into `/implement` skips that collapse and throws the linked detail away — go straight to `/implement` only when the effort turned out genuinely small.

## Codebase health

Not feature work — upkeep.

- **`/improve-codebase-architecture`** — run whenever you have a spare moment to keep the codebase good for agents to operate in. It surfaces **deepening opportunities**; picking one _generates an idea_ you can take into the main flow at `/grill-with-docs`. It's the survey that finds the candidates; **`/codebase-design`** (below) is the bench you design the chosen one on.

## Vocabulary underneath

Two model-invoked references that run *beneath* the other skills — each the single source of truth for its vocabulary. Reach for them directly when the **words**, not the process, are the problem; or let the skills above pull them in.

- **`/domain-modeling`** — sharpen the project's *domain* language: challenge a fuzzy term, resolve an overloaded word ("account" doing three jobs), record a hard-to-reverse decision as an ADR. It's the active discipline `/grill-with-docs` drives to keep `CONTEXT.md` a clean glossary.
- **`/codebase-design`** — the deep-module vocabulary (module, interface, depth, seam, adapter, leverage, locality) for designing a module's *shape*: a lot of behaviour behind a small interface at a clean seam. `/tdd` and `/improve-codebase-architecture` both speak it.

## Phase boundaries

A **phase** is a chunk of work inside a session — the grilling, the implementation, the QA. At the **boundary** between two of them you have five options, and picking between them is the fuzziest decision in this whole map:

- **Continue** — stay put. Costs nothing, loses nothing.
- **`/clear`** — empty the window, when nothing here matters to what's next.
- **`/handoff`** — write a portable markdown file. Narrow: only for a **new harness**, a **new directory**, a **colleague**, or forking a side task **mid-phase**. What it buys is portability.
- **Subagent** — send a tightly-scoped task to its own window and get a report back.
- **`/compact`** — compress this context and seed a fresh session with it. The **default**, at the bottom of the tree rather than the first reach.

Read [PHASE-BOUNDARIES.md](PHASE-BOUNDARIES.md) for the ordered tree — the five questions, the reasoning behind each branch, and why the primary-source cost makes **Continue** the one to rule out first. Make the decision **at** a boundary; mid-phase, continue or split the rest into subagents.

## Standalone

Off the main flow entirely.

- **`/grill-me`** — the same relentless interview as `/grill-with-docs`, but **stateless**: it saves nothing locally and builds no `CONTEXT.md`. Reach for it when you are **not working in a working directory** — sharpening a plan, a design, a piece of writing, anything with no repo under it. If you are in a working directory, use `/grill-with-docs` instead: it runs the same interview and leaves a paper trail, so it is strictly the better one.
- **`/grilling`** — the interview primitive itself: rounds, the frontier, facts are the agent's job and decisions are yours. `/grill-me` and `/grill-with-docs` are the two named ways in, and `/triage`, `/wayfinder` and `/improve-codebase-architecture` all run it internally. Reach for it directly only when you want the interview with no wrapper around it.
- **`/resolving-merge-conflicts`** — work an in-progress merge or rebase conflict hunk by hunk, resolving by **intent** traced to each side's primary source rather than by picking lines, then finish the operation. It never runs `--abort`. Standalone and off every flow: reach for it when you are already mid-conflict.
- **`/prototype`** — a small, throwaway program that answers one design question: does this state model feel right, or what should this UI look like. Throwaway is a constraint on how the code is written, not a promise to destroy it: the answer folds into the real code, and the prototype itself is kept as a **primary source** on a `prototype/<name>` branch out of main, pointed at from the implementation issue. It's the detour in step 2 of the main flow, but reach for it any time a design question is hard to settle on paper.
- **`/research`** — delegate reading legwork to a **background agent**: it investigates a question against **primary sources**, then leaves a cited Markdown file in the repo. Keep working while it reads. The file it produces is something to take *into* the main flow at `/grill-with-docs` — research feeds the thinking, it doesn't replace it.
- **`/to-questionnaire`** — when the thing blocking you isn't in your head or the codebase but in **someone else's**, this writes them a questionnaire to fill in. It's the inverse of `/grill-me`: instead of interviewing you about the subject, it interviews you about the **send** — who it's going to, what you need back — and aims the questions at the gap. What comes back is material for `/grill-with-docs` or `/to-spec`.
- **`/wizard`** — for the steps only a **human** can take: provisioning infrastructure, setting up credentials or CI secrets, clicking through an unfamiliar third-party dashboard, running a one-off migration or cutover. It generates an interactive bash script that opens each URL, captures each value, and writes it into `.env` and GitHub secrets — so the procedure stops being something you re-explain to an agent every time. Model-invoked, so the agent reaches for it the moment it hits a wall only you can pass. If the agent could just do it itself, it should; this is for where a human is genuinely in the loop.
- **`/wait-what`** — the corrective for a message that didn't land. Use it mid-conversation, inside any other skill, and the agent re-pitches what it just said with the context you were missing, in plain English, using the `CONTEXT.md` vocabulary. It works after the fact; `/grill-with-docs` is the upfront cure, because a shared language agreed early is what stops the jargon arriving at all.
- **`/teach`** — learn a concept over multiple sessions, using the current directory as a stateful workspace.
- **`/writing-for-agents`** — reference for writing documents agents consume: skills, AGENTS.md, pointed-at docs.

## Precondition

**`/setup-matt-pocock-skills`** — run before your first engineering flow to configure the issue tracker, triage labels, and doc layout the other skills assume. Custom issue trackers also work.
\n
<!--
Краткое описание (Summary): Этот документ описывает различные 'навыки' (skills) или рабочие процессы (flows), доступные в репозитории, и помогает выбрать подходящий маршрут для вашей задачи, от проработки идеи до ее реализации.

Перевод:
---
name: ask-matt
description: Спросите, какой навык или процесс подходит для вашей ситуации. Маршрутизатор по навыкам в этом репозитории.
disable-model-invocation: true
---

# Спроси Мэтта (Ask Matt)

Вы не помните каждый навык, поэтому спросите.

**Процесс** (flow) — это путь через навыки. Большинство путей проходят по одному **основному процессу** (main flow), к которому примыкают два **въезда** (on-ramps). Все остальное является либо автономным, либо слоем словаря, который работает под ними.

## Основной процесс: идея → выпуск (ship)

Маршрут, по которому проходит большая часть работы. У вас есть идея, и вы хотите ее реализовать.

1. **`/grill-with-docs`** — оттачивание идеи посредством интервью. Начинайте отсюда всякий раз, когда вы **работаете в рабочей директории**: это процесс с сохранением состояния, который удерживает то, что узнает, в `CONTEXT.md` и ADR (записях архитектурных решений). (Нет рабочей директории? Используйте `/grill-me` — см. Автономные. Оба используют один и тот же примитив `/grilling`; `grill-with-docs` — это тот, который оставляет бумажный след, что делает его лучшим из двух, когда есть репозиторий, где его можно оставить.)
2. **Ветвление — можете ли вы решить каждый вопрос в разговоре?** Если вопрос требует ответа, который можно запустить (состояние, бизнес-логика, пользовательский интерфейс, который нужно увидеть), сделайте обходной путь через прототип, соединенный через **`/handoff`** в обоих направлениях (прототип живет в своей собственной директории, что как раз и является назначением `/handoff` — см. Границы фаз):
   - **`/handoff`** (передача) наружу, затем откройте новую сессию для этого файла,
   - **`/prototype`** (прототип), чтобы ответить на вопрос с помощью одноразового кода,
   - **`/handoff`** обратно того, что вы узнали, и сошлитесь на это из исходной ветки идеи.
3. **Ветвление — это многосессионная сборка?**
   - **Да** → **`/to-spec`** (превратить ветку в спецификацию), затем **`/to-tickets`** (разбить на тикеты), чтобы разделить ее на тикеты типа "трассирующая пуля", каждый из которых объявляет свои **блокирующие связи** (blocking edges). В локальном трекере это один файл на тикет в `.scratch/<feature>/issues/`, которые обрабатываются вручную, начиная с блокирующих; в реальном трекере связи становятся нативными блокирующими ссылками, так что любой тикет, чьи блокираторы выполнены, может быть взят в работу — запускайте **`/implement`** для каждого тикета, **очищая (`/clear`) контекст между каждым из них**. Каждый тикет самодостаточен, поэтому контекст предыдущего можно отбросить.
   - **Нет** → **`/implement`** (реализовать) прямо здесь, в том же контекстном окне.

В любом случае, **`/implement`** создает каждую задачу, внутренне управляя **`/tdd`** — по одному красно-зеленому срезу за раз — затем спрашивает разработчика, следует ли запустить **`/code-review`** (проверку кода), двухосевую проверку (Стандарты + Спецификация) диффа. После возвращения отчета или отказа разработчика от проверки, он просит одобрения на коммит и пуш текущей ветки задачи, затем предлагает ручной **`/to-pull-requests`**. Используйте **`/tdd`** сам по себе, когда вы просто хотите создать конкретное поведение сначала через тесты без полной спецификации, и **`/code-review`** сам по себе всякий раз, когда вы хотите проверить ветку или PR относительно фиксированной точки.

### Гигиена контекста

Держите шаги 1–3 в **одном непрерывном контекстном окне** — не сжимайте и не очищайте до окончания `/to-tickets` — чтобы интервью (grilling), спецификация и тикеты строились на одном и том же мышлении. Затем каждый `/implement` начинается с чистого листа, работая на основе тикета.

Пределом для этого является **[умная зона](https://www.aihero.dev/ai-coding-dictionary/smart-zone)** (smart zone): окно (~150 тыс. токенов в самых современных моделях), в пределах которого модель все еще рассуждает четко. Если сессия приближается к нему до `/to-tickets`, не продолжайте работать в деградировавшем состоянии — сожмите (`/compact`) на ближайшей границе фазы и продолжайте (см. Границы фаз).

## Въезды (On-ramps)

Начальная ситуация, которая порождает работу, а затем вливается в основной процесс.

- **Накапливающиеся баги и запросы** → **`/triage`** (сортировка). Перемещает задачи по ролям сортировки и выдает готовые для агента задачи, которые позже подхватывает **`/implement`**.

  Сортировка предназначена только для задач, **которые вы не создавали** — отчеты об ошибках, входящие запросы функций, все, что поступает в сыром виде. Тикеты, созданные `/to-tickets`, уже готовы для агента, поэтому **не сортируйте их**.

- **Что-то сломалось** → **`/diagnosing-bugs`** (диагностика ошибок). Для сложных случаев: ошибка, которая сопротивляется с первого взгляда, периодически плавающий баг, регрессия, которая закралась между двумя заведомо исправными состояниями. Он отказывается строить теории, пока у него не появится **жесткий цикл обратной связи** — одна команда, которая уже "краснеет" (выдает ошибку) на *этом* баге — затем исправляет с помощью регрессионного теста. Его постмортем передает управление **`/improve-codebase-architecture`**, когда реальная находка заключается в том, что нет хорошего шва (seam), чтобы локализовать баг.

- **Огромное, туманное начинание — проект с нуля (greenfield) или создание огромной функции, слишком большой для одной сессии** → **`/wayfinder`** (поиск пути), самый когнитивно сложный процесс здесь. Когда путь отсюда до пункта назначения еще не виден, он составляет **общую карту** **тикетов решений** (decision tickets) в трекере задач и разрешает их один за другим — выдавая **решения, а не результаты разработки** — пока туман не рассеется и путь не станет ясным. Если **`/grill-with-docs`** оттачивает идею, которую можно удержать в одной сессии, то wayfinder предназначен для идей, для которых это невозможно — он медленнее и плотнее, поэтому приберегите его именно для этого, а не для хорошо ограниченной функции.

  Когда карта проясняется, **он передает управление, он не строит**: вливайтесь в основной процесс на **`/to-spec`**, который сворачивает связанные решения карты в план для сборки, затем `/to-tickets` и `/implement`, как обычно. Зацикливание карты прямо в `/implement` пропускает это сворачивание и отбрасывает связанные детали — переходите прямо к `/implement` только тогда, когда задача оказалась действительно небольшой.

## Здоровье кодовой базы

Не разработка функций — поддержание (upkeep).

- **`/improve-codebase-architecture`** — запускайте всякий раз, когда у вас есть свободная минута, чтобы поддерживать кодовую базу в хорошем состоянии для работы агентов. Он выявляет **возможности для углубления**; выбор одной из них *генерирует идею*, которую вы можете взять в основной процесс через `/grill-with-docs`. Это исследование, которое находит кандидатов; **`/codebase-design`** (ниже) — это верстак, на котором вы проектируете выбранного.

## Словарь (Vocabulary) под капотом

Два справочника, вызываемых моделью, которые работают *под* другими навыками — каждый является единственным источником истины для своего словаря. Обращайтесь к ним напрямую, когда проблемой являются **слова**, а не процесс; или позвольте вышеуказанным навыкам подтягивать их.

- **`/domain-modeling`** (моделирование предметной области) — оттачивает язык *предметной области* (domain) проекта: оспаривает нечеткий термин, разрешает перегруженное слово ("account", выполняющее три работы), записывает труднообратимое решение как ADR. Это активная дисциплина, которой управляет `/grill-with-docs`, чтобы сохранить `CONTEXT.md` в виде чистого глоссария.
- **`/codebase-design`** (проектирование кодовой базы) — словарь глубоких модулей (deep-module) (модуль, интерфейс, глубина, шов, адаптер, рычаг (leverage), локальность) для проектирования *формы* модуля: много поведения за небольшим интерфейсом на чистом шве. И `/tdd`, и `/improve-codebase-architecture` говорят на нем.

## Границы фаз (Phase boundaries)

**Фаза** — это кусок работы внутри сессии — интервью, реализация, QA. На **границе** между двумя из них у вас есть пять вариантов, и выбор между ними — самое нечеткое решение на всей этой карте:

- **Продолжить (Continue)** — оставаться на месте. Ничего не стоит, ничего не теряет.
- **`/clear`** (очистить) — очистить окно, когда ничто здесь не имеет значения для того, что будет дальше.
- **`/handoff`** (передать) — написать переносимый markdown-файл. Узкое применение: только для **нового инструмента (harness)**, **новой директории**, **коллеги** или ветвления побочной задачи **в середине фазы**. То, что он покупает, — это переносимость.
- **Субагент (Subagent)** — отправить жестко ограниченную задачу в собственное окно и получить отчет обратно.
- **`/compact`** (сжать) — сжать этот контекст и начать с ним новую сессию. **Вариант по умолчанию**, находится внизу дерева, а не первый, к которому следует тянуться.

Прочтите [PHASE-BOUNDARIES.md](PHASE-BOUNDARIES.md) для ознакомления с упорядоченным деревом — пять вопросов, обоснование для каждой ветви, и почему стоимость первичного источника делает **Продолжить** вариантом, который следует исключить первым. Принимайте решение **на** границе; в середине фазы продолжайте или разделите остальное между субагентами.

## Автономные (Standalone)

Полностью вне основного процесса.

- **`/grill-me`** — такое же неустанное интервью, как и `/grill-with-docs`, но **без сохранения состояния**: он ничего не сохраняет локально и не строит `CONTEXT.md`. Обращайтесь к нему, когда вы **не работаете в рабочей директории** — оттачивая план, дизайн, текст, что угодно, под чем нет репозитория. Если вы находитесь в рабочей директории, используйте вместо этого `/grill-with-docs`: он проводит такое же интервью и оставляет бумажный след, поэтому он строго лучше.
- **`/grilling`** — сам примитив интервью: раунды, рубеж, факты — это работа агента, а решения — ваши. `/grill-me` и `/grill-with-docs` — это два именованных входа в него, а `/triage`, `/wayfinder` и `/improve-codebase-architecture` запускают его внутренне. Обращайтесь к нему напрямую только тогда, когда хотите провести интервью без обертки вокруг него.
- **`/resolving-merge-conflicts`** — прорабатывайте текущий конфликт слияния (merge) или перебазирования (rebase) кусок за куском, разрешая по **намерению**, прослеженному до первичного источника каждой стороны, а не путем выбора строк, а затем завершайте операцию. Он никогда не запускает `--abort`. Автономный и вне любого процесса: обращайтесь к нему, когда вы уже находитесь в середине конфликта.
- **`/prototype`** (прототип) — небольшая, одноразовая программа, которая отвечает на один вопрос проектирования: кажется ли правильной эта модель состояния, или как должен выглядеть этот пользовательский интерфейс. Одноразовый — это ограничение на то, как пишется код, а не обещание его уничтожить: ответ вплетается в реальный код, а сам прототип сохраняется как **первичный источник** в ветке `prototype/<name>` от main, на который ссылаются из задачи реализации. Это обходной путь на шаге 2 основного процесса, но обращайтесь к нему в любое время, когда вопрос проектирования трудно решить на бумаге.
- **`/research`** (исследование) — делегируйте рутинную работу по чтению **фоновому агенту**: он исследует вопрос по **первичным источникам**, затем оставляет Markdown-файл с цитатами в репозитории. Продолжайте работать, пока он читает. Файл, который он производит — это то, что нужно взять *в* основной процесс на `/grill-with-docs` — исследования питают мышление, а не заменяют его.
- **`/to-questionnaire`** — когда то, что вас блокирует, находится не в вашей голове или кодовой базе, а в **чьей-то еще**, он пишет им опросник для заполнения. Это инверсия `/grill-me`: вместо того, чтобы брать интервью у вас о предмете, он берет интервью у вас об **отправке** — кому это идет, что вам нужно получить обратно — и направляет вопросы в этот пробел. То, что возвращается, является материалом для `/grill-with-docs` или `/to-spec`.
- **`/wizard`** (мастер) — для шагов, которые может предпринять только **человек**: настройка инфраструктуры, настройка учетных данных или секретов CI, прокликивание незнакомой сторонней панели управления, запуск одноразовой миграции или переключения. Он генерирует интерактивный bash-скрипт, который открывает каждый URL, захватывает каждое значение и записывает его в `.env` и секреты GitHub — чтобы процедура перестала быть тем, что вы каждый раз заново объясняете агенту. Вызывается моделью, поэтому агент тянется к нему в тот момент, когда сталкивается со стеной, которую можете пройти только вы. Если бы агент мог просто сделать это сам, он бы это сделал; это для тех случаев, когда человек действительно нужен в цикле.
- **`/wait-what`** (подожди, что) — исправление для сообщения, которое не было понято. Используйте его в середине разговора, внутри любого другого навыка, и агент заново изложит то, что только что сказал, с контекстом, которого вам не хватало, на простом английском языке, используя словарь `CONTEXT.md`. Это работает постфактум; `/grill-with-docs` — это предварительное лечение, потому что общий язык, согласованный заранее — это то, что останавливает появление жаргона вообще.
- **`/teach`** — изучение концепции за несколько сессий, используя текущую директорию как рабочую область с сохранением состояния.
- **`/writing-for-agents`** — справочник по написанию документов, которые потребляют агенты: навыки, AGENTS.md, документы, на которые есть ссылки.

## Предварительное условие (Precondition)

**`/setup-matt-pocock-skills`** — запустите перед вашим первым инженерным процессом, чтобы настроить трекер задач, метки сортировки и макет документа, которые предполагают другие навыки. Пользовательские трекеры задач также работают.
-->
\n