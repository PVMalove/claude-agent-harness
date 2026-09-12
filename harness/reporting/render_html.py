#!/usr/bin/env python3
"""Render a delivery-stats report as one standalone HTML dashboard.

The page embeds everything it needs: no scripts, no fonts, no network requests. A figure the report
marked as missing or estimated is rendered as such — the dashboard never fills a gap with zero.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Optional

MISSING = "нет данных"

CLAUDE_INPUT_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

STYLE = """
:root{--bg:#0d0f12;--panel:#14171c;--line:#2a2f38;--ink:#e8e6e1;--dim:#8b93a1;
--accent:#e08a3c;--accent2:#5bbfa5;--accent3:#c9b58a;--bar:#20242c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
font-size:14px;line-height:1.5}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 64px}
.eyebrow{color:var(--accent);letter-spacing:.18em;text-transform:uppercase;font-size:11px;margin-bottom:18px}
h1{font-size:26px;margin:0 0 6px;font-weight:600;line-height:1.25}
.sub{color:var(--dim);margin:0 0 28px}
.hero{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:28px;align-items:start;margin-bottom:32px}
.figure{font-size:clamp(38px,7vw,68px);font-weight:700;color:var(--accent);line-height:1;letter-spacing:-.02em}
.figure-note{color:var(--dim);text-transform:uppercase;letter-spacing:.12em;font-size:11px;margin-top:10px}
.lede{color:var(--ink);opacity:.85}
.grid{display:grid;gap:16px;margin-bottom:16px}
.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.g3{grid-template-columns:repeat(3,minmax(0,1fr))}
.panel{border:1px solid var(--line);border-radius:6px;background:var(--panel);padding:18px 20px}
.panel h2{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);margin:0 0 16px;font-weight:600}
.row{display:grid;grid-template-columns:minmax(84px,auto) minmax(0,1fr) auto;gap:12px;align-items:center;margin-bottom:9px}
.row:last-child{margin-bottom:0}
.name{color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.track{background:var(--bar);height:15px;border-radius:2px;overflow:hidden}
.fill{display:block;height:100%;background:var(--accent)}
.fill.b{background:var(--accent3)}
.fill.c{background:var(--accent2)}
.value{white-space:nowrap}
.stat{font-size:28px;font-weight:700;margin:2px 0 4px}
.stat-label{color:var(--dim);text-transform:uppercase;letter-spacing:.12em;font-size:11px}
.stat-note{color:var(--dim);font-size:12px}
.split{display:flex;height:22px;border-radius:3px;overflow:hidden;margin-bottom:14px}
.split span{display:block}
.legend{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.swatch{width:10px;height:10px;border-radius:2px;display:inline-block;margin-right:7px;vertical-align:middle}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--line);font-weight:400}
th{color:var(--dim);font-size:11px;letter-spacing:.1em;text-transform:uppercase}
td.num,th.num{text-align:right;padding-right:0}
.tag{display:inline-block;border:1px solid var(--line);border-radius:3px;padding:1px 7px;font-size:11px;color:var(--dim)}
.tag.est{border-color:var(--accent3);color:var(--accent3)}
.tag.closed{border-color:var(--accent2);color:var(--accent2)}
.missing{color:var(--dim);font-style:italic}
.note{color:var(--dim);font-size:12px;margin-top:14px}
footer{color:var(--dim);font-size:12px;margin-top:32px;border-top:1px solid var(--line);padding-top:16px}
.scroll{overflow-x:auto}
@media(max-width:820px){.hero,.g2,.g3,.legend{grid-template-columns:minmax(0,1fr)}}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _thousands(value: Any) -> str:
    if not isinstance(value, int):
        return _esc(value)
    return f"{value:,}".replace(",", " ")


def _decimal(value: Any, places: int = 2) -> str:
    """Russian copy uses a comma for the decimal separator and a space for thousands."""
    if not isinstance(value, (int, float)):
        return _esc(value)
    return f"{value:,.{places}f}".replace(",", " ").replace(".", ",")


def _compact(value: Any) -> str:
    if not isinstance(value, int):
        return _esc(value)
    for limit, suffix in ((1_000_000_000, "млрд"), (1_000_000, "млн"), (1_000, "тыс")):
        if value >= limit:
            return f"{value / limit:.2f}".rstrip("0").rstrip(".").replace(".", ",") + f" {suffix}"
    return str(value)


def _bars(rows: list, css: str = "") -> str:
    """Rows are (label, numeric value, display value); the widest row sets the scale."""
    if not rows:
        return '<p class="missing">нет данных</p>'
    top = max((value for _, value, _ in rows), default=0) or 1
    out = []
    for label, value, shown in rows:
        width = max(1.0, value * 100 / top)
        out.append(
            f'<div class="row"><span class="name">{_esc(label)}</span>'
            f'<span class="track"><span class="fill {css}" style="width:{width:.1f}%"></span></span>'
            f'<span class="value">{shown}</span></div>'
        )
    return "".join(out)


def _panel(title: str, body: str) -> str:
    return f'<section class="panel"><h2>{_esc(title)}</h2>{body}</section>'


def _stat(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="stat-note">{_esc(note)}</div>' if note else ""
    return (
        f'<section class="panel"><div class="stat-label">{_esc(label)}</div>'
        f'<div class="stat">{value}</div>{note_html}</section>'
    )


def _claude_input(bucket: dict) -> int:
    return sum(int(bucket.get(field, 0)) for field in CLAUDE_INPUT_FIELDS)


def _hero(report: dict) -> str:
    claude, codex = report["claude"], report["codex"]
    total = 0
    parts = []
    if claude.get("status") == "ok":
        value = sum(_claude_input(b) + int(b["output_tokens"]) for b in claude["models"].values())
        total += value
        parts.append(f"Claude — {_compact(value)}")
    if codex.get("status") == "ok":
        value = sum(int(b["input_tokens"]) + int(b["output_tokens"]) for b in codex["models"].values())
        total += value
        parts.append(f"Codex — {_compact(value)} (оценка)")
    figure = _compact(total) if total else '<span class="missing">нет данных</span>'
    epic = report["epic"]
    lede = (
        f"Тикетов закрыто: {report['tickets_closed']} из {report['tickets_total']}. "
        + ("Из них: " + ", ".join(parts) + "." if parts else "Данных сессий по этим веткам нет.")
    )
    return (
        '<div class="hero"><div>'
        f'<div class="figure">{figure}</div>'
        '<div class="figure-note">токенов на эпик и его тикеты</div>'
        "</div>"
        f'<div class="lede">{_esc(lede)}<br><br>'
        f'Эпик #{_esc(epic["number"])} — {_esc(epic["title"])}</div></div>'
    )


def _models_panel(title: str, usage: dict, input_of, css: str) -> str:
    if usage.get("status") != "ok":
        return _panel(title, f'<p class="missing">{_esc(usage.get("reason", MISSING))}</p>')
    rows = []
    for model, bucket in sorted(usage["models"].items(), key=lambda kv: -int(kv[1]["output_tokens"])):
        rows.append((model, int(bucket["output_tokens"]), _compact(int(bucket["output_tokens"]))))
    note = ""
    if usage.get("attribution") == "estimated":
        note = f'<p class="note"><span class="tag est">оценка</span> {_esc(usage.get("attribution_note", ""))}</p>'
    table_rows = "".join(
        f"<tr><td>{_esc(model)}</td><td class='num'>{_compact(input_of(bucket))}</td>"
        f"<td class='num'>{_compact(int(bucket['output_tokens']))}</td>"
        f"<td class='num'>{_thousands(int(bucket['turns']))}</td></tr>"
        for model, bucket in sorted(usage["models"].items(), key=lambda kv: -int(kv[1]["output_tokens"]))
    )
    return _panel(
        title,
        _bars(rows, css)
        + '<div class="scroll"><table><thead><tr><th>модель</th><th class="num">вход</th>'
        '<th class="num">выход</th><th class="num">ходов</th></tr></thead>'
        f"<tbody>{table_rows}</tbody></table></div>{note}",
    )


def _comparison_panel(comparison: object) -> str:
    if not isinstance(comparison, dict):
        return ""
    baseline = comparison.get("baseline")
    current = comparison.get("current")
    deltas = comparison.get("delta", {}).get("providers", {})
    if not isinstance(baseline, dict) or not isinstance(current, dict) or not isinstance(deltas, dict):
        return ""
    rows = []
    for provider, label in (("claude", "Claude"), ("codex", "Codex")):
        before = baseline.get("providers", {}).get(provider, {})
        after = current.get("providers", {}).get(provider, {})
        delta = deltas.get(provider, MISSING)

        def shown(value: object) -> str:
            if not isinstance(value, dict) or value.get("status") != "ok":
                reason = value.get("reason", MISSING) if isinstance(value, dict) else MISSING
                return f'<span class="missing">{_esc(reason)}</span>'
            attribution = value.get("attribution", MISSING)
            tag = "est" if attribution == "estimated" else ""
            return (
                f'{_thousands(value.get("total_tokens"))} '
                f'<span class="tag {tag}">{_esc(attribution)}</span>'
            )

        if isinstance(delta, dict):
            total = int(delta.get("total_tokens", 0))
            delta_text = f"{total:+,}".replace(",", " ")
        else:
            delta_text = f'<span class="missing">{_esc(MISSING)}</span>'
        rows.append(
            f"<tr><td>{label}</td><td class='num'>{shown(before)}</td>"
            f"<td class='num'>{shown(after)}</td><td class='num'>{delta_text}</td></tr>"
        )
    baseline_epic = baseline.get("epic", {}).get("number", "?")
    current_epic = current.get("epic", {}).get("number", "?")
    return _panel(
        f"Сравнение baseline #{_esc(baseline_epic)} → эпик #{_esc(current_epic)}",
        "<div class='scroll'><table><thead><tr><th>provider</th><th class='num'>baseline</th>"
        "<th class='num'>текущий</th><th class='num'>разница</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        "<p class='note'>Attribution показан для каждой стороны. Разница вычисляется только при telemetry "
        "в обеих сторонах.</p>",
    )


def _cache_panel(cache: Any) -> str:
    if not isinstance(cache, dict):
        return _panel("Из чего состоял вход Claude", f'<p class="missing">{_esc(MISSING)}</p>')
    read, write, fresh = cache["cache_read_percent"], cache["cache_write_percent"], cache["fresh_percent"]
    bar = (
        '<div class="split">'
        f'<span style="width:{max(read, 0.4):.3f}%;background:var(--accent)"></span>'
        f'<span style="width:{max(write, 0.4):.3f}%;background:var(--accent3)"></span>'
        f'<span style="width:{max(fresh, 0.4):.3f}%;background:var(--accent2)"></span>'
        "</div>"
    )
    cells = "".join(
        f'<div><div class="stat-label"><span class="swatch" style="background:{color}"></span>{_esc(label)}</div>'
        f'<div class="stat">{_decimal(percent, 3)} %</div>'
        f'<div class="stat-note">{_thousands(tokens)} токенов</div></div>'
        for label, percent, tokens, color in (
            ("чтение кеша", read, cache["cache_read"], "var(--accent)"),
            ("запись кеша", write, cache["cache_write"], "var(--accent3)"),
            ("свежий вход", fresh, cache["fresh"], "var(--accent2)"),
        )
    )
    total = _thousands(cache["total_input"])
    return _panel(
        f"Из чего состоял вход Claude — {total} токенов",
        bar + f'<div class="legend">{cells}</div>',
    )


def _cost_panel(cost: dict) -> str:
    if cost.get("status") != "ok":
        return _panel("Деньги", f'<p class="missing">{_esc(cost.get("reason", MISSING))}</p>')
    currency = cost["currency"]
    rows = [
        ("как вышло", cost["total"], f"{_decimal(cost['total'])} {currency}"),
        ("без кеша", cost["uncached_total"], f"{_decimal(cost['uncached_total'])} {currency}"),
    ]
    per_model = "".join(
        f"<tr><td>{_esc(item['model'])}</td>"
        f"<td class='num'>{_decimal(item['cost'])}</td>"
        f"<td class='num'>{_decimal(item['uncached_cost'])}</td></tr>"
        for item in cost["per_model"]
    )
    unpriced = ""
    if cost["unpriced_models"]:
        unpriced = (
            f'<p class="note">Нет ставок в тарифе, поэтому не посчитаны: '
            f'{_esc(", ".join(cost["unpriced_models"]))}</p>'
        )
    return _panel(
        f"Деньги — экономия на кеше {_decimal(cost['cache_saving'])} {currency}",
        _bars(rows)
        + '<div class="scroll"><table><thead><tr><th>модель</th><th class="num">как вышло</th>'
        '<th class="num">без кеша</th></tr></thead>'
        f"<tbody>{per_model}</tbody></table></div>"
        f'<p class="note">Ставки: {_esc(cost["rates_effective"])} · {_esc(cost["rates_source"])}</p>'
        + unpriced,
    )


def _window_panel(report: dict) -> str:
    rows = []
    quota = report["claude"].get("quota") if isinstance(report["claude"], dict) else None
    if isinstance(quota, dict):
        rows.append(f"<tr><td>Claude</td><td class='num'>{_esc(json.dumps(quota, ensure_ascii=False)[:120])}</td></tr>")
    limits = report["codex"].get("rate_limits") if isinstance(report["codex"], dict) else None
    if isinstance(limits, dict):
        primary = limits.get("primary") or {}
        if isinstance(primary, dict) and primary.get("used_percent") is not None:
            window = primary.get("window_minutes")
            label = f"окно {window} мин" if window else "окно"
            rows.append(
                f"<tr><td>Codex ({_esc(label)})</td><td class='num'>{_decimal(primary['used_percent'], 1)} %</td></tr>"
            )
    if not rows:
        return _panel("Съедено окон подписки", f'<p class="missing">{_esc(MISSING)}</p>')
    return _panel(
        "Съедено окон подписки",
        f"<div class='scroll'><table><tbody>{''.join(rows)}</tbody></table></div>"
        '<p class="note">Значения на момент последнего записанного хода, не за весь эпик.</p>',
    )


def _tickets_panel(report: dict) -> str:
    rows = []
    by_ticket: dict = {}
    for entry in report["volume"]["entries"]:
        if entry.get("status") != "ok":
            continue
        bucket = by_ticket.setdefault(entry.get("ticket"), {"insertions": 0, "deletions": 0, "commits": 0})
        for field in bucket:
            bucket[field] += int(entry.get(field, 0))
    for ticket in report["tickets"]:
        entry = by_ticket.get(ticket["number"])
        state = str(ticket.get("state", "")).upper()
        tag = (
            '<span class="tag closed">closed</span>'
            if state == "CLOSED"
            else f'<span class="tag">{_esc(state.lower() or "?")}</span>'
        )
        if entry:
            volume = f"+{_thousands(entry['insertions'])} / −{_thousands(entry['deletions'])}"
            commits = _thousands(entry["commits"])
        else:
            volume = '<span class="missing">нет данных</span>'
            commits = "—"
        rows.append(
            f"<tr><td>#{_esc(ticket['number'])}</td><td>{_esc(ticket['title'][:58])}</td>"
            f"<td>{tag}</td><td class='num'>{volume}</td><td class='num'>{commits}</td></tr>"
        )
    return _panel(
        "Тикеты эпика",
        "<div class='scroll'><table><thead><tr><th>№</th><th>заголовок</th><th>статус</th>"
        "<th class='num'>строки</th><th class='num'>коммиты</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>",
    )


def build_html(report: dict) -> str:
    totals = report["volume"]["totals"]
    claude, codex = report["claude"], report["codex"]
    comparison_panel = _comparison_panel(report.get("comparison"))
    sessions = 0
    for source in (claude, codex):
        if source.get("status") == "ok":
            sessions += int(source.get("sessions", 0))

    body = [
        '<div class="wrap">',
        '<div class="eyebrow">Статистика доставки</div>',
        f'<h1>{_esc(report["repository"])} · эпик #{_esc(report["epic"]["number"])}</h1>',
        f'<p class="sub">Отчёт собран {_esc(report["generated_at"][:19].replace("T", " "))} UTC '
        "по локальным логам сессий и истории git.</p>",
        _hero(report),
        '<div class="grid g3">',
        _stat("строк вставлено", _thousands(totals["insertions"]), f"удалено {_thousands(totals['deletions'])}"),
        _stat("файлов затронуто", _thousands(totals["files"]), f"{_thousands(totals['commits'])} коммитов"),
        _stat(
            "тикетов закрыто",
            f"{report['tickets_closed']}",
            f"из {report['tickets_total']}"
            + (f", ADR {report['adr_added']}" if isinstance(report["adr_added"], int) else ""),
        ),
        "</div>",
        '<div class="grid g2">',
        _models_panel("Claude · по моделям", claude, _claude_input, ""),
        _models_panel("Codex · по моделям", codex, lambda b: int(b["input_tokens"]), "b"),
        "</div>",
        f'<div class="grid">{comparison_panel}</div>' if comparison_panel else "",
        '<div class="grid">',
        _cache_panel(report["cache"]),
        "</div>",
        '<div class="grid g2">',
        _cost_panel(report["cost"]),
        _window_panel(report),
        "</div>",
        '<div class="grid">',
        _tickets_panel(report),
        "</div>",
        f'<footer>Сессий учтено: {sessions}. Claude приписан к эпику точно — по ветке каждой записи. '
        "Codex приписан оценочно — по репозиторию и временному окну, потому что в его логах ветки нет. "
        "Всё, что не удалось получить, помечено как «нет данных», а не занулено.</footer>",
        "</div>",
    ]
    return (
        "<!doctype html>\n<html lang=\"ru\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(report['repository'])} · эпик #{_esc(report['epic']['number'])}</title>"
        f"<style>{STYLE}</style></head><body>{''.join(body)}</body></html>\n"
    )


def write_dashboard(report: dict, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(build_html(report), encoding="utf-8", newline="\n")
    return destination
