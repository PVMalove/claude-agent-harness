---
name: wayfinder
description: Plan a huge chunk of work — more than one agent session can hold — as a shared map of decision tickets on your issue tracker, and resolve them one at a time until the way to the destination is clear.
disable-model-invocation: true
---

A loose idea has arrived — too big for one agent session, and wrapped in fog: the way from here to the **destination** isn't visible yet. Wayfinding is about finding that way, not charging at the destination. This skill charts the way as a **shared map** on the repo's issue tracker, then works its **decision tickets** — questions whose resolution is a decision, not slices of a build to execute — one at a time until the route is clear.

The destination varies per effort, and naming it is the first act of charting — it shapes every ticket. It might be a spec to hand off and iterate on, a decision to lock before planning starts, or a change made in place like a data-structure migration. The map is domain-agnostic — engineering work, course content, whatever fits the shape.

## Plan, don't do

Wayfinder is **planning** by default: each ticket resolves a decision, and the map is done when the way is clear — nothing left to decide before someone goes and does the thing. The pull to just do the work is usually the signal you've reached the edge of the map and it's time to hand off. An effort can override this in its **Notes** — carrying execution into the map itself — but absent that, produce decisions, not deliverables.

## Refer by name

Every map and ticket is an issue, so it has a **name** — its title. In everything the human reads — narration, the map's Decisions-so-far — refer to it by that name, never by a bare id, number, or slug. A wall of `#42, #43, #44` is illegible; names read at a glance. The id and URL don't vanish — a name wraps its link — but they ride _inside_ the name, never stand in for it.

## The Map

The map is a single issue on this repo's issue tracker, labelled `wayfinder:map` — the canonical artifact. Its tickets are child issues of the map.

The map is an **index**, not a store. It lists the decisions made and points at the tickets that hold their detail; a decision lives in exactly one place — its ticket — so the map never restates it, only gists it and links.

**Where the map, its child tickets, blocking, and frontier queries physically live is tracker-specific.** The issue tracker should have been provided to you — run `/setup-matt-pocock-skills` if not. Consult the tracker doc's "Wayfinding operations" section for how _this_ repo expresses them. If no tracker has been provided, default to the local-markdown tracker.

### The map body

The whole map at low resolution, loaded once per session. Open tickets are **not** listed — they are open child issues, found by query.

```markdown
## Destination

<what reaching the end of this map looks like — the spec, decision, or change this effort is finding its way to. One or two lines; every session orients to it before choosing a ticket.>

## Notes

<domain; skills every session should consult; standing preferences for this effort>

## Decisions so far

<!-- the index — one line per closed ticket: enough to judge relevance, then zoom the link for the detail the ticket holds -->

- [<closed ticket title>](link) — <one-line gist of the answer>

## Not yet specified

<!-- see "Fog of war": in-scope fog you can't ticket yet; graduates as the frontier advances -->

## Out of scope

<!-- see "Out of scope": work ruled beyond the destination; closed, never graduates -->
```

### Tickets

Each ticket is a **child issue** of the map; the tracker's issue id is its identity. Its body is the question, sized to one 100K token agent session:

```markdown
## Question

<the decision or investigation this ticket resolves>
```

Each ticket carries a `wayfinder:<type>` label — one of `research`, `prototype`, `grilling`, `task` (see [Ticket Types](#ticket-types)) — plus the matching `hitl`/`afk` label and `workflow::ready`, this repo's own taxonomy for who does the work and where it sits in the pipeline (see `docs/agents/triage-labels.md`).

A session **claims** a ticket by assigning it to the dev driving the map, **first**, before any work, so concurrent sessions skip it. That assignee _is_ the claim: an open, unassigned ticket is unclaimed.

Blocking uses the tracker's **native** dependency relationship — essential because it renders the frontier _visually_ in the tracker's own UI, so the human sees what's takeable without opening the map. Only a tracker that lacks native blocking falls back to a body convention. A ticket is **unblocked** when every ticket blocking it is closed; the **frontier** is the open, unblocked, unclaimed children — the edge of the known.

The answer isn't part of the body — it's recorded on resolution (see [Work through the map](#work-through-the-map)). Assets created while resolving a ticket are linked from the issue, not pasted in.

## Ticket Types

Every ticket is either **HITL** — human in the loop, worked _with_ a human who speaks for themselves — or **AFK**, driven by the agent alone. A HITL ticket only resolves through that live exchange; the agent never stands in for the human's side of it (a grilling agent that answers its own questions has broken this).

- **Research** (AFK): Reading documentation, third-party APIs, or local resources like knowledge bases to surface a fact a decision waits on. Resolved by a `/research` **subagent**. Use when knowledge outside the current working directory is required.
- **Prototype** (HITL): Raise the fidelity of the discussion by making a cheap, rough, concrete artifact to react to — an outline, a rough take, a stub, or UI/logic code via the /prototype skill. Links the prototype as an asset. Use when "how should it look" or "how should it behave" is the key question.
- **Grilling** (HITL): Conversation. The default case. Always invoke the /grilling and /domain-modeling skills.
- **Task** (HITL or AFK): Manual work that must happen before a _decision_ can be made — nothing to decide, prototype, or research, but the discussion is blocked until it's done. Signing up for a service so its API can be judged, provisioning access, moving data so its shape can be seen. This is the one type that _does_ rather than decides — and it earns its place by unblocking a decision, not by delivering the destination. The agent drives it alone where it can (AFK); otherwise it hands the human a precise checklist (HITL). Resolved when the work is done; the answer records what was done and any resulting facts (credentials location, new URLs, row counts) later tickets depend on.

## Fog of war

The map is _deliberately_ incomplete: don't chart what you can't yet see. Beyond the live tickets lies the **fog of war** — the dim view of decisions and investigations you can tell are coming but can't yet pin down, because they hang on questions still open. Resolving a ticket clears the fog ahead of it, graduating whatever's now specifiable into fresh tickets — one at a time, until the way to the destination is clear and no tickets remain.

The map's **Not yet specified** section is where that dim view is written down: the suspected question, the area to revisit later. It's the undiscovered frontier _toward_ the destination — everything here is in scope, just not sharp enough to ticket. Write as loosely or as fully as the view allows; it doubles as a signpost for collaborators reading where the effort is headed.

**Fog or ticket?** The test is whether you can state the question precisely now — _not_ whether you can answer it now.

- **Ticket when** the question is already sharp — even if it's blocked and you can't act on it yet.
- **Not yet specified when** you can't yet phrase it that sharply. Don't pre-slice the fog into ticket-sized pieces: it's coarser than a ticket, and one patch may graduate into several tickets, or none, once the frontier reaches it.

**Not yet specified** excludes what's already decided (Decisions so far), what's already a live ticket, and what's out of scope (the next section).

## Out of scope

Fog only ever gathers _toward_ the destination. The destination fixes the scope, so work beyond it is **out of scope** — it isn't fog, and it doesn't belong in **Not yet specified**. It gets its own **Out of scope** section on the map: work you've consciously ruled out of _this_ effort. Scope, not sharpness, lands it here.

Out-of-scope work never graduates — the frontier stops at the destination — so it returns only if the destination is redrawn, and then as a fresh effort, not a resumption.

Ruling something out of scope is a scoping act, not a step on the route. When a ticket that already exists turns out to sit past the destination — mis-scoped in while charting, or exposed by a resolution — **close it** (a closed ticket is unambiguously off the frontier) and leave one line in the **Out of scope** section: the gist plus why it's out of scope, linking the closed ticket. It stays out of **Decisions so far**, which records the route actually walked — a scope boundary isn't a step on it.

## Invocation

Two modes. Either way, **never resolve more than one ticket per session** — with the exception of research tickets.

### Chart the map

User invokes with a loose idea.

1. **Name the destination.** Run a `/grilling` and `/domain-modeling` session to pin down what this map is finding its way to — the spec, decision, or change. The destination fixes the scope, so it's settled first.
2. **Map the frontier.** Grill again, **breadth-first** this time: fan out across the whole space rather than deep on any one thread, surfacing the open decisions and the first steps takeable now. **If this surfaces no fog** — the way to the destination is already clear, the whole journey small enough for one session — you don't need a map. Stop and ask the user how they'd like to proceed.
3. **Create the map** (label `wayfinder:map`): Destination and Notes filled in, Decisions-so-far empty, the fog sketched into **Not yet specified**.
4. **Create the tickets you can specify now** as child issues of the map, each carrying its `wayfinder:<type>` label, the matching `hitl`/`afk` label, and `workflow::ready` (see [Tickets](#tickets)) — then wire blocking edges in a **second pass** (issues need ids before they can reference each other). Wiring sorts them into the frontier and the blocked; everything you can't yet specify stays in the fog — the **Not yet specified** section.
5. **Fire the research subagents.** For each `research` ticket you just created, spin up a `/research` subagent to resolve it in parallel, capturing its findings on a throwaway `research/<name>` branch with a context pointer from the ticket.
6. Stop — charting is one session's work; it hand-resolves nothing.

### Work through the map

User invokes with a map (URL or number). A ticket is **optional** — without one, you pick the next decision, not the user.

1. Load the **map** — the low-res view, not every ticket body.
2. Choose the ticket. If the user named one, use it. Otherwise take the first frontier ticket in order. **Claim it**: assign it to yourself before any work, and set `workflow::in-progress` — the same convention `/implement` and `/to-guide` use (`docs/agents/triage-labels.md`).
3. Resolve it — **zoom as needed**: fetch the full body of any related or closed ticket on demand; invoke the skills the `## Notes` block names. If in doubt, use `/grilling` and `/domain-modeling`.
4. Record the resolution: post the answer as a **resolution comment**, **close** the issue, and **append a context pointer** to the map's Decisions-so-far.
5. Add newly-surfaced tickets (create-then-wire); graduate any fog the answer has made specifiable, clearing each graduated patch from **Not yet specified** so it lives only as its new ticket. If the answer reveals a ticket — this one or another — sits beyond the destination, **rule it out of scope** rather than resolving it on the route. If the decision invalidates other parts of the map, update or delete those tickets.

The user may run unblocked tickets in parallel, so expect other sessions to be editing the tracker concurrently.

<!--
Краткое описание (Summary): Навык "Wayfinder" помогает планировать и управлять крупными, комплексными задачами, которые невозможно выполнить за одну сессию. Он создает общую карту решений в трекере задач в виде дерева тикетов. Вместо того чтобы сразу выполнять работу, этот навык концентрируется на принятии последовательных решений, продвигаясь сквозь "туман войны" к ясно поставленной цели, оставляя за собой документированный след принятых решений и исключенных из рамок задач.

Перевод:
---
name: wayfinder
description: Планируйте огромный объем работы — больше, чем может вместить одна сессия агента — в виде общей карты тикетов с решениями в вашем трекере задач, и решайте их по одному, пока путь к цели не станет ясным.
disable-model-invocation: true
---

Появилась размытая идея — слишком большая для одной сессии агента и окутанная туманом: путь отсюда до **цели (destination)** еще не виден. Wayfinding (поиск пути) — это поиск этого пути, а не слепое движение к цели. Этот навык прокладывает путь в виде **общей карты (shared map)** в трекере задач репозитория, а затем прорабатывает её **тикеты решений (decision tickets)** — вопросы, разрешением которых является принятие решения, а не части сборки для выполнения — по одному, пока маршрут не станет ясным.

Цель варьируется в зависимости от задачи, и её название — это первый шаг составления карты, который определяет каждый тикет. Это может быть спецификация для передачи и итерации, решение, которое нужно зафиксировать перед началом планирования, или изменение на месте, например, миграция структуры данных. Карта не зависит от предметной области — будь то инженерная работа, содержание курса или всё, что подходит по форме.

## Планируйте, а не делайте (Plan, don't do)

Wayfinder по умолчанию занимается **планированием**: каждый тикет разрешает какое-то решение, и карта считается готовой, когда путь ясен — больше нечего решать, прежде чем кто-то пойдет и сделает дело. Тяга к тому, чтобы просто выполнить работу, обычно является сигналом того, что вы достигли края карты, и пришло время передать эстафету. Задача может переопределить это в своих **Заметках (Notes)** — перенося выполнение в саму карту — но при отсутствии этого, производите решения, а не результаты.

## Обращайтесь по имени (Refer by name)

Каждая карта и тикет — это задача (issue), поэтому у неё есть **имя** — её заголовок. Во всём, что читает человек — повествовании, разделе карты «Решения до сих пор» — ссылайтесь на неё по этому имени, никогда по голому идентификатору, номеру или слагу. Стена из `#42, #43, #44` нечитаема; имена считываются с первого взгляда. Идентификатор и URL-адрес никуда не исчезают — имя оборачивает свою ссылку — но они находятся _внутри_ имени, никогда не заменяя его.

## Карта (The Map)

Карта — это единая задача в трекере задач этого репозитория с меткой `wayfinder:map` — канонический артефакт. Её тикеты — это дочерние задачи карты.

Карта — это **индекс**, а не хранилище. Она перечисляет принятые решения и указывает на тикеты, в которых содержатся их подробности; решение находится ровно в одном месте — в своём тикете — поэтому карта никогда не пересказывает его, а только даёт суть и ссылку.

**Где физически находятся карта, её дочерние тикеты, блокировки и запросы фронтира — зависит от трекера.** Трекер задач должен был быть вам предоставлен — выполните `/setup-matt-pocock-skills`, если это не так. Ознакомьтесь с разделом «Wayfinding operations» документации трекера, чтобы узнать, как _этот_ репозиторий их выражает. Если трекер не был предоставлен, по умолчанию используется локальный трекер на markdown.

### Тело карты (The map body)

Вся карта в низком разрешении, загружается один раз за сессию. Открытые тикеты **не** перечисляются — они являются открытыми дочерними задачами, которые находятся по запросу.

```markdown
## Destination (Цель)

<как выглядит достижение конца этой карты — спецификация, решение или изменение, к которому прокладывает путь это усилие. Одна-две строки; каждая сессия ориентируется на это перед выбором тикета.>

## Notes (Заметки)

<предметная область; навыки, к которым должна обращаться каждая сессия; постоянные предпочтения для этой задачи>

## Decisions so far (Решения до сих пор)

<!-- индекс — одна строка на каждый закрытый тикет: достаточно, чтобы оценить релевантность, затем перейти по ссылке для получения подробностей, содержащихся в тикете -->

- [<заголовок закрытого тикета>](ссылка) — <краткая суть ответа в одну строку>

## Not yet specified (Ещё не уточнено)

<!-- см. "Туман войны": туман в рамках области видимости, для которого пока нельзя создать тикет; становится конкретным по мере продвижения фронтира -->

## Out of scope (Вне рамок)

<!-- см. "Вне рамок": работа, признанная выходящей за пределы цели; закрыто, никогда не становится конкретным -->
```

### Тикеты (Tickets)

Каждый тикет — это **дочерняя задача (child issue)** карты; идентификатор задачи в трекере является его идентификатором. Его тело — это вопрос, размер которого рассчитан на одну сессию агента с лимитом в 100 тысяч токенов:

```markdown
## Question (Вопрос)

<решение или исследование, которое разрешает этот тикет>
```

Каждый тикет имеет метку `wayfinder:<type>` — один из `research`, `prototype`, `grilling`, `task` (см. [Типы тикетов](#типы-тикетов-ticket-types)) — плюс соответствующую метку `hitl`/`afk` и `workflow::ready`, собственную таксономию этого репозитория для того, кто выполняет работу и где она находится в пайплайне (см. `docs/agents/triage-labels.md`).

Сессия **заявляет права (claims)** на тикет, назначая его разработчику, который ведёт карту, **первым делом**, до начала любой работы, чтобы параллельные сессии пропустили его. Это назначение и _есть_ заявление прав: открытый, неназначенный тикет является свободным (unclaimed).

Блокировка использует **нативную** зависимость трекера — это важно, поскольку это отображает фронтир _визуально_ в собственном пользовательском интерфейсе трекера, чтобы человек видел, за что можно взяться, не открывая карту. Только трекер, у которого отсутствует нативная блокировка, возвращается к соглашению в теле задачи. Тикет считается **разблокированным (unblocked)**, когда каждый тикет, блокирующий его, закрыт; **фронтир (frontier)** — это открытые, разблокированные, свободные дочерние элементы — край изведанного.

Ответ не является частью тела — он записывается при разрешении (см. [Работа с картой](#работа-с-картой-work-through-the-map)). Активы, созданные при разрешении тикета, привязываются ссылкой из задачи, а не вставляются в неё.

## Типы тикетов (Ticket Types)

Каждый тикет является либо **HITL** (человек в контуре) — выполняется _вместе_ с человеком, который говорит сам за себя, — либо **AFK**, управляемым исключительно агентом. Тикет HITL разрешается только через этот живой обмен; агент никогда не заменяет человека с его стороны (агент для опроса, отвечающий на собственные вопросы, нарушает это правило).

- **Research (Исследование)** (AFK): Чтение документации, сторонних API или локальных ресурсов, таких как базы знаний, чтобы найти факт, от которого зависит решение. Разрешается **субагентом** `/research`. Используйте, когда требуются знания за пределами текущей рабочей директории.
- **Prototype (Прототип)** (HITL): Повышение качества обсуждения путём создания дешёвого, грубого, конкретного артефакта для реакции — наброска, черновика, заглушки или кода UI/логики с помощью навыка /prototype. Привязывает прототип как актив. Используйте, когда ключевым вопросом является «как это должно выглядеть» или «как это должно себя вести».
- **Grilling (Расспрос)** (HITL): Разговор. Случай по умолчанию. Всегда вызывайте навыки /grilling и /domain-modeling.
- **Task (Задача)** (HITL или AFK): Ручная работа, которая должна произойти до того, как будет принято _решение_ — нечего решать, прототипировать или исследовать, но обсуждение заблокировано, пока это не будет сделано. Регистрация в сервисе, чтобы можно было оценить его API, предоставление доступа, перемещение данных, чтобы увидеть их структуру. Это единственный тип, который скорее _делает_, чем решает — и он оправдывает своё существование тем, что разблокирует решение, а не тем, что достигает цели. Агент выполняет его самостоятельно, где это возможно (AFK); в противном случае он передает человеку точный контрольный список (HITL). Считается разрешённым, когда работа выполнена; ответ фиксирует то, что было сделано, и любые полученные факты (местоположение учётных данных, новые URL-адреса, количество строк), от которых зависят последующие тикеты.

## Туман войны (Fog of war)

Карта _намеренно_ неполная: не наносите на карту то, чего вы ещё не видите. За живыми тикетами лежит **туман войны** — смутное видение решений и расследований, которые вы предвидите, но пока не можете точно определить, потому что они зависят от всё ещё открытых вопросов. Разрешение тикета рассеивает туман перед ним, превращая всё, что теперь можно уточнить, в новые тикеты — по одному, пока путь к цели не станет ясным и не останется ни одного тикета.

Раздел карты **Not yet specified (Ещё не уточнено)** — это место, где записывается это смутное видение: предполагаемый вопрос, область, к которой следует вернуться позже. Это неизведанный фронтир _по направлению к_ цели — всё, что здесь находится, входит в рамки, просто пока недостаточно чётко, чтобы создать тикет. Пишите так свободно или так полно, как позволяет видение; это также служит указателем для соавторов, читающих, к чему направлены усилия.

**Туман или тикет?** Критерий заключается в том, можете ли вы сейчас точно сформулировать вопрос — а _не_ в том, можете ли вы сейчас на него ответить.

- **Тикет, когда** вопрос уже чёткий — даже если он заблокирован и вы пока не можете действовать по нему.
- **Ещё не уточнено, когда** вы пока не можете сформулировать его так чётко. Не нарезайте туман заранее на куски размером с тикет: он крупнее тикета, и один его участок может превратиться в несколько тикетов или ни в один, когда фронтир достигнет его.

Раздел **Not yet specified** исключает то, что уже решено (Решения до сих пор), то, что уже является живым тикетом, и то, что выходит за рамки (следующий раздел).

## Вне рамок (Out of scope)

Туман собирается только _по направлению к_ цели. Цель фиксирует рамки, поэтому работа за её пределами находится **вне рамок (out of scope)** — это не туман, и она не относится к разделу **Not yet specified**. Для неё на карте есть отдельный раздел **Out of scope**: работа, которую вы сознательно исключили из _текущего_ усилия. Она попадает сюда из-за рамок, а не из-за отсутствия чёткости.

Работа вне рамок никогда не переходит в статус конкретной — фронтир останавливается на цели — поэтому она возвращается только в том случае, если цель пересматривается, и тогда уже как новая задача, а не как возобновление.

Исключение чего-либо из рамок — это акт определения границ, а не шаг на маршруте. Когда оказывается, что уже существующий тикет выходит за рамки цели — ошибочно включен при составлении карты или выявлен в результате разрешения — **закройте его** (закрытый тикет однозначно снимается с фронтира) и оставьте одну строку в разделе **Out of scope**: суть плюс причина, почему это вне рамок, ссылаясь на закрытый тикет. Это не попадает в раздел **Decisions so far (Решения до сих пор)**, который фиксирует фактически пройденный маршрут — граница рамок не является шагом на нём.

## Вызов (Invocation)

Два режима. В любом случае, **никогда не разрешайте больше одного тикета за сессию** — за исключением исследовательских (research) тикетов.

### Составление карты (Chart the map)

Пользователь вызывает с размытой идеей.

1. **Определите цель (Name the destination).** Запустите сессию `/grilling` и `/domain-modeling`, чтобы точно определить, к чему эта карта прокладывает путь — спецификация, решение или изменение. Цель фиксирует рамки, поэтому она определяется первой.
2. **Нанесите на карту фронтир (Map the frontier).** Расспросите снова, на этот раз **в ширину (breadth-first)**: развернитесь по всему пространству, а не углубляйтесь в какую-то одну ветвь, выявляя открытые решения и первые шаги, которые можно предпринять прямо сейчас. **Если при этом туман не обнаруживается** — путь к цели уже ясен, весь маршрут достаточно мал для одной сессии — вам не нужна карта. Остановитесь и спросите пользователя, как он хотел бы продолжить.
3. **Создайте карту (Create the map)** (метка `wayfinder:map`): Destination и Notes заполнены, Decisions-so-far пусто, туман набросан в **Not yet specified**.
4. **Создайте тикеты, которые вы можете конкретизировать сейчас (Create the tickets you can specify now)**, как дочерние задачи карты, каждый со своей меткой `wayfinder:<type>`, соответствующей меткой `hitl`/`afk` и `workflow::ready` (см. [Тикеты](#тикеты-tickets)) — затем проложите связи блокировок на **втором проходе** (задачам нужны идентификаторы, прежде чем они смогут ссылаться друг на друга). Прокладывание связей сортирует их на фронтир и заблокированные; всё, что вы пока не можете конкретизировать, остаётся в тумане — раздел **Not yet specified**.
5. **Запустите исследовательских субагентов (Fire the research subagents).** Для каждого только что созданного тикета `research` запустите субагента `/research` для его параллельного разрешения, собирая его находки в одноразовую ветку `research/<name>` с указателем контекста из тикета.
6. Остановитесь — составление карты — это работа на одну сессию; оно ничего не разрешает вручную.

### Работа с картой (Work through the map)

Пользователь вызывает с картой (URL или номер). Тикет является **необязательным** — без него вы выбираете следующее решение, а не пользователь.

1. Загрузите **карту (map)** — вид в низком разрешении, а не каждое тело тикета.
2. Выберите тикет. Если пользователь назвал его, используйте его. В противном случае возьмите первый тикет фронтира по порядку. **Заявите права на него (Claim it)**: назначьте его себе до начала любой работы и установите `workflow::in-progress` — то же соглашение, которое используют `/implement` и `/to-guide` (`docs/agents/triage-labels.md`).
3. Разрешите его — **приближайте по необходимости (zoom as needed)**: извлекайте полное тело любого связанного или закрытого тикета по требованию; вызывайте навыки, указанные в блоке `## Notes`. Если сомневаетесь, используйте `/grilling` и `/domain-modeling`.
4. Зафиксируйте разрешение: опубликуйте ответ в виде **комментария с решением (resolution comment)**, **закройте** задачу и **добавьте указатель контекста** к разделу карты Decisions-so-far.
5. Добавьте недавно выявленные тикеты (создать-затем-связать); конкретизируйте любой туман, который ответ позволил уточнить, очищая каждый уточнённый фрагмент из **Not yet specified**, чтобы он существовал только в виде своего нового тикета. Если ответ показывает, что тикет — этот или другой — находится за пределами цели, **исключите его из рамок**, а не разрешайте его на маршруте. Если решение делает недействительными другие части карты, обновите или удалите эти тикеты.

Пользователь может запускать разблокированные тикеты параллельно, поэтому ожидайте, что другие сессии будут редактировать трекер одновременно.
-->
