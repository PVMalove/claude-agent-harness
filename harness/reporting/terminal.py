"""Render delivery statistics for the human terminal interface."""

from __future__ import annotations

from harness.reporting.common import CLAUDE_FIELDS, MISSING, JsonObject


def _thousands(value: object) -> str:
    if not isinstance(value, int):
        return str(value)
    return f"{value:,}".replace(",", " ")


def _compact(value: object) -> str:
    if not isinstance(value, int):
        return str(value)
    for limit, suffix in ((1_000_000_000, "млрд"), (1_000_000, "млн"), (1_000, "тыс")):
        if value >= limit:
            return (
                f"{value / limit:.2f}".rstrip("0").rstrip(".").replace(".", ",")
                + f" {suffix}"
            )
    return str(value)


def _ru(value: object, places: int = 2) -> str:
    """Russian decimal comma for a report the reader sees in Russian."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    return f"{value:,.{places}f}".replace(",", " ").replace(".", ",")


def render_terminal(report: JsonObject) -> str:
    epic = report["epic"]
    lines = [
        f"Эпик #{epic['number']} — {epic['title']}",
        f"Тикетов закрыто: {report['tickets_closed']} из {report['tickets_total']}",
    ]
    totals = report["volume"]["totals"]
    lines.append(
        f"Код: +{_thousands(totals['insertions'])} / -{_thousands(totals['deletions'])} строк, "
        f"{totals['files']} файлов, {totals['commits']} коммитов"
    )
    adr = report["adr_added"]
    lines.append(f"ADR добавлено: {adr}")

    claude = report["claude"]
    if claude.get("status") == "ok":
        for model, bucket in sorted(
            claude["models"].items(), key=lambda kv: -kv[1]["output_tokens"]
        ):
            lines.append(
                f"  Claude {model}: вход {_compact(sum(bucket[f] for f in CLAUDE_FIELDS[:3]))}, "
                f"выход {_compact(bucket['output_tokens'])}, ходов {bucket['turns']}"
            )
        session_stats = claude.get("session_stats", [])
        if session_stats:
            lines.append("Топ-3 сессий по объему контекста:")
            for s in session_stats[:3]:
                lines.append(
                    f"  Ветка {s['branch']}: ходов {s['turns']}, макс. контекст {_compact(s['max_input'])}, всего входных {_compact(s['total_input'])}"
                )
    else:
        lines.append(f"  Claude: {claude.get('reason', MISSING)}")

    codex = report["codex"]
    if codex.get("status") == "ok":
        for model, bucket in sorted(
            codex["models"].items(), key=lambda kv: -kv[1]["output_tokens"]
        ):
            lines.append(
                f"  Codex {model}: вход {_compact(bucket['input_tokens'])}, "
                f"выход {_compact(bucket['output_tokens'])}, ходов {bucket['turns']} (оценка)"
            )
    else:
        lines.append(f"  Codex: {codex.get('reason', MISSING)}")

    comparison = report.get("comparison")
    if isinstance(comparison, dict):
        baseline = comparison.get("baseline", {})
        current = comparison.get("current", {})
        deltas = comparison.get("delta", {}).get("providers", {})
        lines.append(
            f"Сравнение: baseline эпика #{baseline.get('epic', {}).get('number', '?')} "
            f"→ текущий эпик #{current.get('epic', {}).get('number', '?')}"
        )
        for provider, label in (("claude", "Claude"), ("codex", "Codex")):
            before = baseline.get("providers", {}).get(provider, {})
            after = current.get("providers", {}).get(provider, {})
            delta = deltas.get(provider, MISSING)
            if isinstance(before, dict) and before.get("status") == "ok":
                before_text = f"{_compact(before.get('total_tokens'))} ({before.get('attribution', MISSING)})"
            else:
                before_text = MISSING
            if isinstance(after, dict) and after.get("status") == "ok":
                after_text = f"{_compact(after.get('total_tokens'))} ({after.get('attribution', MISSING)})"
            else:
                after_text = MISSING
            delta_text = (
                _compact(delta.get("total_tokens"))
                if isinstance(delta, dict)
                else MISSING
            )
            lines.append(
                f"  {label}: {before_text} → {after_text}; разница {delta_text}"
            )
            cache_write_delta = (
                delta.get("cache_write_tokens") if isinstance(delta, dict) else MISSING
            )
            cache_read_delta = (
                delta.get("cache_read_tokens") if isinstance(delta, dict) else MISSING
            )
            if isinstance(cache_write_delta, int) or isinstance(cache_read_delta, int):
                lines.append(
                    f"    кеш: запись {_compact(cache_write_delta) if isinstance(cache_write_delta, int) else MISSING}, "
                    f"чтение {_compact(cache_read_delta) if isinstance(cache_read_delta, int) else MISSING}"
                )

    cache = report["cache"]
    if isinstance(cache, dict):
        lines.append(
            f"Кеш Claude: чтение {_ru(cache['cache_read_percent'], 3)}%, запись {_ru(cache['cache_write_percent'], 3)}%, "
            f"свежий вход {_ru(cache['fresh_percent'], 3)}%"
        )
    else:
        lines.append(f"Кеш Claude: {MISSING}")

    cost = report["cost"]
    if cost.get("status") == "ok":
        lines.append(
            f"Стоимость по ставкам {cost['rates_effective']}: {_ru(cost['total'])} {cost['currency']}; "
            f"без кеша было бы {_ru(cost['uncached_total'])} (экономия {_ru(cost['cache_saving'])})"
        )
        if cost["unpriced_models"]:
            lines.append(f"  Без ставок в тарифе: {', '.join(cost['unpriced_models'])}")
    else:
        lines.append(f"Стоимость: {cost.get('reason', MISSING)}")

    orchestration = report["orchestration"]
    if orchestration.get("status") == "ok":
        for number, ticket in sorted(orchestration["tickets"].items()):
            lines.append(f"Оркестрация тикета #{number}:")
            for session in ticket["worker_sessions"]:
                lines.append(
                    f"  {session['role']} {session['dispatch_id']}: сессий {session['sessions']}"
                )
                for restart in session["restarts"]:
                    lines.append(
                        f"    рестарт ({restart['decision']}): {restart['reason']}"
                    )
            rate = ticket["qa_failure_rate"]
            lines.append(
                f"  QA failure rate: {_ru(rate * 100, 1) + '%' if isinstance(rate, float) else rate}"
            )
            scope = ticket["review_scope"]
            if isinstance(scope, list):
                for entry in scope:
                    lines.append(
                        f"  Review {entry['dispatch_id']}: вне зоны {entry['files_out_of_scope']}"
                        f" из {entry['files_total']} файлов ({_ru(entry['share'] * 100, 1)}%)"
                    )
            else:
                lines.append(f"  Review diff scope excess: {scope}")
    else:
        lines.append(f"Оркестрация: {orchestration.get('reason', MISSING)}")
    return "\n".join(lines)
