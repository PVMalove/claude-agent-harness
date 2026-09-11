---
name: code-review
description: Review the changes since a fixed point (commit, branch, tag, or merge-base) along two axes — Standards (does the code follow this repo's documented coding standards?) and Spec (does the code match what the originating issue/spec asked for?). Runs both reviews in parallel sub-agents and reports them side by side. Use when the user wants to review a branch, a PR, work-in-progress changes, or asks to "review since X".
---

Two-axis review of the diff between `HEAD` and a fixed point the user supplies:

- **Standards** — does the code conform to this repo's documented coding standards?
- **Spec** — does the code faithfully implement the originating issue / spec?

Both axes run as **parallel sub-agents** so they don't pollute each other's context, then this skill aggregates their findings.

### Runtime boundary

This workflow is runtime-neutral. Keep the output language driven by `language` from
`.harness/project.json`, and use the existing manually configured review mechanism. The workflow
must not launch a runtime-specific adapter, choose a provider or model, or change the reviewed
branch.

The issue tracker should have been provided to you. If `docs/agents/issue-tracker.md` is missing, tell the user to run `/setup-matt-pocock-skills`.

## Process

### 1. Pin the fixed point

Whatever the user said is the fixed point — a commit SHA, branch name, tag, `main`, `HEAD~5`, etc. If they didn't specify one, ask for it.

Capture the diff command once: `git diff <fixed-point>...HEAD` (three-dot, so the comparison is against the merge-base). Also note the list of commits via `git log <fixed-point>..HEAD --oneline`.

Before going further, confirm the fixed point resolves (`git rev-parse <fixed-point>`) and the diff is non-empty. A bad ref or empty diff should fail here — not inside two parallel sub-agents.

### 2. Identify the spec source

Look for the originating spec, in this order:

1. Issue references in the commit messages (`#123`, `Closes #45`, GitLab `!67`, etc.) — fetch via the workflow in `docs/agents/issue-tracker.md`.
2. A path the user passed as an argument.
3. A spec file under `docs/`, `specs/`, or `.scratch/` matching the branch name or feature.
4. If nothing is found, ask the user where the spec is. If they say there isn't one, the **Spec** sub-agent will skip and report "no spec available".

### 3. Identify the standards sources

Anything in the repo that documents how code should be written, such as `CODING_STANDARDS.md` or `CONTRIBUTING.md`.

On top of whatever the repo documents, the Standards axis always carries the **smell baseline** below — a fixed set of Fowler code smells (_Refactoring_, ch.3) that applies even when a repo documents nothing. Two rules bind it:

- **The repo overrides.** A documented repo standard always wins; where it endorses something the baseline would flag, suppress the smell.
- **Always a judgement call.** Each smell is a labelled heuristic ("possible Feature Envy"), never a hard violation — and, like any standard here, skip anything tooling already enforces.

Each smell reads _what it is_ → _how to fix_; match it against the diff:

- **Mysterious Name** — a function, variable, or type whose name doesn't reveal what it does or holds. → rename it; if no honest name comes, the design's murky.
- **Duplicated Code** — the same logic shape appears in more than one hunk or file in the change. → extract the shared shape, call it from both.
- **Feature Envy** — a method that reaches into another object's data more than its own. → move the method onto the data it envies.
- **Data Clumps** — the same few fields or params keep travelling together (a type wanting to be born). → bundle them into one type, pass that.
- **Primitive Obsession** — a primitive or string standing in for a domain concept that deserves its own type. → give the concept its own small type.
- **Repeated Switches** — the same `switch`/`if`-cascade on the same type recurs across the change. → replace with polymorphism, or one map both sites share.
- **Shotgun Surgery** — one logical change forces scattered edits across many files in the diff. → gather what changes together into one module.
- **Divergent Change** — one file or module is edited for several unrelated reasons. → split so each module changes for one reason.
- **Speculative Generality** — abstraction, parameters, or hooks added for needs the spec doesn't have. → delete it; inline back until a real need shows.
- **Message Chains** — long `a.b().c().d()` navigation the caller shouldn't depend on. → hide the walk behind one method on the first object.
- **Middle Man** — a class or function that mostly just delegates onward. → cut it, call the real target direct.
- **Refused Bequest** — a subclass or implementer that ignores or overrides most of what it inherits. → drop the inheritance, use composition.

### 4. Review both axes

Launch the `code-review-standards` and `code-review-spec` subagents defined in `.claude/agents/` through the coding application's manually configured mechanism. Wait for both reports, then return them to this primary session for aggregation. Do not replace either axis with an inline review.

**`code-review-standards` prompt** — include:

- The full diff command and commit list.
- The list of standards-source files you found in step 3, **plus the smell baseline from step 3** pasted in full — the sub-agent has no other access to it.

**`code-review-spec` prompt** — include:

- The diff command and commit list.
- The path or fetched contents of the spec.

If the spec is missing, skip the `code-review-spec` sub-agent and note this in the final report.

### 5. Aggregate

Present the two reports under `## Standards` and `## Spec` headings, verbatim or lightly cleaned. Do **not** merge or rerank findings — the two axes are deliberately separate (see _Why two axes_).

End with a one-line summary: total findings per axis, and the worst issue _within each axis_ (if any). Don't pick a single winner across axes — that's the reranking the separation exists to prevent.

## Why two axes

A change can pass one axis and fail the other:

- Code that follows every standard but implements the wrong thing → **Standards pass, Spec fail.**
- Code that does exactly what the issue asked but breaks the project's conventions → **Spec pass, Standards fail.**

Reporting them separately stops one axis from masking the other.

### Output language

Read `language` from `.harness/project.json` (default `ru` if the file or field is absent). If `ru`, write the entire report — both sub-agent briefs and the aggregated output — in Russian regardless of the prompt's language. If `en`, write in English.

<!--
Краткое описание (Summary): Этот навык проводит двустороннее ревью кода между текущим состоянием (HEAD) и заданной точкой отсчета. Оценка проходит по двум осям в параллельных подагентах: "Стандарты" (соответствие кода задокументированным стандартам проекта) и "Спецификация" (соответствует ли код исходным требованиям/задачам). После завершения результаты объединяются в общий отчет.

Перевод:
---
name: code-review
description: Обзор изменений с определенного момента (коммит, ветка, тег или база слияния) по двум осям — Стандарты (соответствует ли код задокументированным стандартам кодирования этого репозитория?) и Спецификация (соответствует ли код тому, что требовалось в исходной проблеме/спецификации?). Запускает обе проверки в параллельных подагентах и сообщает о них рядом. Используйте, когда пользователь хочет проверить ветку, PR, изменения в процессе работы или просит "сделать ревью с момента X".
---

Двухосевой обзор различий между `HEAD` и фиксированной точкой, которую предоставляет пользователь:

- **Стандарты** — соответствует ли код задокументированным стандартам кодирования этого репозитория?
- **Спецификация** — добросовестно ли код реализует исходную проблему / спецификацию?

Обе оси работают как **параллельные подагенты**, чтобы они не загрязняли контекст друг друга, затем этот навык объединяет их результаты.

### Граница среды выполнения

Этот рабочий процесс не зависит от среды выполнения. Язык вывода должен определяться параметром `language` из `.harness/project.json`, и используйте существующий настроенный вручную механизм обзора. Рабочий процесс не должен запускать адаптер, специфичный для среды выполнения, выбирать провайдера или модель, или изменять проверяемую ветку.

Трекер задач должен быть вам предоставлен. Если `docs/agents/issue-tracker.md` отсутствует, скажите пользователю запустить `/setup-matt-pocock-skills`.

## Процесс

### 1. Закрепить фиксированную точку

То, что сказал пользователь, и есть фиксированная точка — SHA коммита, имя ветки, тег, `main`, `HEAD~5` и т. д. Если он не указал ее, спросите об этом.

Один раз зафиксируйте команду для diff: `git diff <fixed-point>...HEAD` (три точки, чтобы сравнение шло с базой слияния). Также отметьте список коммитов через `git log <fixed-point>..HEAD --oneline`.

Прежде чем идти дальше, убедитесь, что фиксированная точка разрешается (`git rev-parse <fixed-point>`) и diff не пуст. Плохая ссылка или пустой diff должны вызвать ошибку здесь — а не внутри двух параллельных подагентов.

### 2. Определить источник спецификации

Ищите исходную спецификацию в следующем порядке:

1. Ссылки на задачи в сообщениях коммитов (`#123`, `Closes #45`, GitLab `!67` и т. д.) — получите через рабочий процесс в `docs/agents/issue-tracker.md`.
2. Путь, который пользователь передал в качестве аргумента.
3. Файл спецификации в `docs/`, `specs/` или `.scratch/`, соответствующий имени ветки или функции.
4. Если ничего не найдено, спросите пользователя, где находится спецификация. Если он скажет, что ее нет, подагент **Спецификации** пропустит работу и сообщит "спецификация недоступна".

### 3. Определить источники стандартов

Всё в репозитории, что документирует, как должен быть написан код, например `CODING_STANDARDS.md` или `CONTRIBUTING.md`.

В дополнение к тому, что документирует репозиторий, ось Стандартов всегда несет **базовую линию запахов** ниже — фиксированный набор запахов кода по Фаулеру (Fowler, _Рефакторинг_, гл. 3), который применяется даже тогда, когда репозиторий ничего не документирует. Это связано двумя правилами:

- **Репозиторий имеет приоритет.** Задокументированный стандарт репозитория всегда побеждает; если он одобряет то, что базовая линия пометила бы, подавите запах.
- **Всегда субъективное суждение.** Каждый запах — это помеченная эвристика ("возможно, Завистливые функции"), а не жесткое нарушение — и, как и любой стандарт здесь, пропускайте все, что уже контролируется инструментами.

Каждый запах читается как _что это_ → _как исправить_; сопоставьте это с изменениями (diff):

- **Мистическое Имя (Mysterious Name)** — функция, переменная или тип, имя которого не раскрывает, что он делает или хранит. → переименуйте его; если честное имя не приходит в голову, дизайн неясен.
- **Дублирующийся Код (Duplicated Code)** — одна и та же логическая структура появляется более чем в одном фрагменте или файле в изменении. → извлеките общую структуру, вызывайте ее из обоих мест.
- **Завистливые Функции (Feature Envy)** — метод, который обращается к данным другого объекта больше, чем к своим собственным. → переместите метод в те данные, которым он завидует.
- **Сгустки Данных (Data Clumps)** — одни и те же несколько полей или параметров постоянно путешествуют вместе (тип, который хочет родиться). → объедините их в один тип, передавайте его.
- **Одержимость Примитивами (Primitive Obsession)** — примитив или строка, заменяющая концепцию предметной области, которая заслуживает своего собственного типа. → дайте концепции ее собственный небольшой тип.
- **Повторяющиеся Переключатели (Repeated Switches)** — один и тот же каскад `switch`/`if` для одного и того же типа повторяется в разных местах изменения. → замените полиморфизмом или одной картой, которую используют оба места.
- **Стрельба Дробью (Shotgun Surgery)** — одно логическое изменение требует разрозненных правок во многих файлах в diff. → соберите то, что изменяется вместе, в один модуль.
- **Расходящаяся Модификация (Divergent Change)** — один файл или модуль редактируется по нескольким несвязанным причинам. → разделите так, чтобы каждый модуль менялся по одной причине.
- **Спекулятивная Общность (Speculative Generality)** — абстракция, параметры или хуки добавлены для нужд, которых нет в спецификации. → удалите это; встраивайте обратно, пока не появится реальная потребность.
- **Цепочки Вызовов (Message Chains)** — длинная навигация `a.b().c().d()`, от которой не должен зависеть вызывающий. → спрячьте этот путь за одним методом на первом объекте.
- **Посредник (Middle Man)** — класс или функция, которые в основном просто делегируют дальше. → избавьтесь от этого, вызывайте реальную цель напрямую.
- **Отказ от Наследства (Refused Bequest)** — подкласс или реализатор, который игнорирует или переопределяет большую часть того, что он наследует. → откажитесь от наследования, используйте композицию.

### 4. Обзор по обеим осям

Запустите подагенты `code-review-standards` и `code-review-spec`, определенные в `.claude/agents/`, через вручную настроенный механизм приложения для написания кода. Дождитесь обоих отчетов, затем верните их в эту основную сессию для объединения. Не заменяйте ни одну из осей встроенным (inline) ревью.

**Промпт для `code-review-standards`** — включите:

- Полную команду diff и список коммитов.
- Список файлов источников стандартов, которые вы нашли на шаге 3, **плюс базовую линию запахов из шага 3**, вставленную полностью — подагент не имеет к ней другого доступа.

**Промпт для `code-review-spec`** — включите:

- Команду diff и список коммитов.
- Путь или полученное содержимое спецификации.

Если спецификация отсутствует, пропустите подагент `code-review-spec` и отметьте это в финальном отчете.

### 5. Объединение

Представьте два отчета под заголовками `## Стандарты` и `## Спецификация`, дословно или слегка очищенными. **Не** объединяйте и не переранжируйте находки — эти две оси намеренно разделены (см. _Почему две оси_).

Завершите однострочным резюме: общее количество находок по каждой оси и самая серьезная проблема _в рамках каждой оси_ (если есть). Не выбирайте одного победителя между осями — это то самое переранжирование, ради предотвращения которого и существует разделение.

## Почему две оси

Изменение может пройти по одной оси и провалить другую:

- Код, который следует всем стандартам, но реализует не то, что нужно → **Стандарты пройдены, Спецификация провалена.**
- Код, который делает именно то, что просилось в задаче, но нарушает соглашения проекта → **Спецификация пройдена, Стандарты провалены.**

Раздельное информирование о них не дает одной оси замаскировать другую.

### Язык вывода

Прочтите `language` из `.harness/project.json` (по умолчанию `ru`, если файл или поле отсутствует). Если `ru`, пишите весь отчет — как сводки подагентов, так и объединенный вывод — на русском языке, независимо от языка промпта. Если `en`, пишите на английском.
-->
