# Обнаружение skills в Claude Code и Codex

Agent Harness v1.0.0 предназначен для Claude Code и Codex. Установленный набор физически
хранится в `.harness/skills/`; ссылки в проекте указывают на эти же файлы. Проверенная
совместимость и инструкции установки относятся к двум runtime ниже.

| Runtime | Проектный путь | Глобальный `start-project` |
| --- | --- | --- |
| Claude Code | `.claude/skills` → `.harness/skills` | `~/.claude/skills/start-project` |
| Codex | `.agents/skills` → `.harness/skills` | `~/.agents/skills/start-project` |

`harness init`, `adopt` и `update` создают или проверяют проектные ссылки и записывают
`.harness/skills/REGISTRY.md` с именами, путями и описаниями установленного набора.
`harness health` проверяет целостность файлов и ссылок. `bin/install-global.py` создаёт
глобальный профиль и ссылку на `global-skills/start-project` для выбранного runtime.

Обнаружение skill не загружает его полное тело в каждую сессию: runtime использует
метаданные для выбора и затем читает соответствующий `SKILL.md`. Закреплённые
upstream файлы сохраняются побайтно; их frontmatter и настройки вызова остаются частью
исходного skill. Project-owned integrations описаны в `.harness/integrations.json` с
путём, SHA-256, runtime-целью и проверкой активации; значения секретов там не хранятся.

CLI и исходники могут содержать маршруты для других runtime. Они не входят в заявленную
совместимость v1.0.0, поскольку их работа не была проверена в этом проекте.
