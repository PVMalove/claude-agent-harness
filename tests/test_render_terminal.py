"""Characterize the delivery statistics terminal interface before moving its renderer."""

from harness.reporting.delivery_stats import MISSING, render_terminal


def test_missing_evidence_is_rendered_without_invented_zeroes() -> None:
    report = {
        "epic": {"number": 7, "title": "Поставка"},
        "tickets_closed": 1,
        "tickets_total": 2,
        "volume": {"totals": {"insertions": 1200, "deletions": 30, "files": 4, "commits": 3}},
        "adr_added": 0,
        "claude": {"status": MISSING, "reason": "нет логов"},
        "codex": {"status": MISSING, "reason": "нет сессий"},
        "cache": MISSING,
        "cost": {"status": MISSING, "reason": "нет ставок"},
        "orchestration": {"status": MISSING, "reason": "нет ledger"},
    }

    assert render_terminal(report) == (
        "Эпик #7 — Поставка\n"
        "Тикетов закрыто: 1 из 2\n"
        "Код: +1 200 / -30 строк, 4 файлов, 3 коммитов\n"
        "ADR добавлено: 0\n"
        "  Claude: нет логов\n"
        "  Codex: нет сессий\n"
        "Кеш Claude: нет данных\n"
        "Стоимость: нет ставок\n"
        "Оркестрация: нет ledger"
    )
