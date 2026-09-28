# Временные артефакты харнесса

## Контекст системы

Публикационные файлы, inbox ролей, кэши и отчёты имеют разный срок жизни. Они должны
оставаться внутри общего хранилища основного checkout, чтобы работа в связанных worktrees
не создавала независимые копии. Локальные документы человека и ledger batch сохраняются
отдельно от данных, на которые распространяется очистка.

## Действующий контракт

`harness/storage.py` находит общий `.harness/.sandboxes/` через Git common directory и
разрешает только категории `cache`, `logs`, `scratch`, `pr_body`, `runs`, `reports` и
`worktrees`. `storage_path` отклоняет `..`, составные компоненты и выход через symlink.
Доступность хранилища и его категорий проверяет `harness health`; `.harness/.gitignore`
исключает runtime-данные из Git.

`pr_body/` предназначен только для одноразового тела PR и комментариев к PR или issue,
включая completion report как issue comment. Файл располагается непосредственно в каталоге
и получает имя с `pr-body`, `pr-comment` или `issue-comment`. Хук
`block-scratch-outside-docs-tasks.sh` отклоняет другие имена и пути, включая `docs/tasks/`
и `scratch/tmp/`. Создавшая файл сессия передаёт его в `gh`/`glab` через `--body-file` и
удаляет после успешной публикации. Ошибка команды оставляет файл для повторной попытки.

`scratch/inbox/` используется для сообщений ролей. `SCRATCH_REL` и `AGENT_INBOX_REL` в
`harness/orchestration/core/constants.py` относятся только к этой категории. `runs/`
хранит временные тестовые и QA окружения, `cache/` — перестраиваемые данные Repo Map,
`logs/` — журналы, `reports/` — генерируемые отчёты, `worktrees/` — управляемые checkout.
Черновики спецификаций и тикетов остаются в gitignored `docs/tasks/` целевого проекта;
долговечное состояние batch — в `.harness/orchestration/state/`.

## Операционные последствия

Шаблоны `harness/project/agents/` и `docs-agents/` направляют автора PR к одной директории,
а hook проверяет путь перед записью. `harness cleanup --mode soft` лишь показывает
подлежащие удалению старые файлы `pr_body/`, `scratch/`, `logs/` и `runs/`; фактическая
очистка требует `--apply`. Режим `hard` дополнительно охватывает кэши, отчёты и
неиспользуемые worktrees. Ledger и `docs/tasks/` не входят в эти категории.
