"""Сводная аналитика: ``GET /api/analytics/summary?days=30`` (§12.6).

Одна точка для экрана «Аналитика»: энергия, баланс сфер, чтение, деньги,
привычки, задачи и корреляции. Все ряды ограничены окном ``days`` (7..365),
деньги — рубли, доли — 0..1. Корреляции считаются тем же движком v2, что и
``GET /api/analytics/correlations``/:func:`build_correlation_report`: в поле
``correlations`` отдаются лучшие пары окна с лагами, p, q, силой и советом.

Честность данных:

* энергия — только реальные чек-ины; ``avg: null``, если их не было;
* чтение — страницы книг, завершённых в окне (журнала чтения в схеме нет),
  плюс справочные суммы по всей библиотеке;
* деньги — транзакции окна в рублях и дневной ряд доход/расход;
* привычки — доля отметок от нормы «активные привычки × дни окна»;
* сферы — все сферы (и «Без сферы», если такие задачи есть), ``share`` —
  доля задач сферы среди всех, ``pct`` — доля закрытых.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Query

from app.server.api.aggregates import SERIES_LABELS, build_correlation_report
from app.server.api.deps import ConnectionDep, SettingsDep

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["analytics"])

DEFAULT_DAYS = 30
MIN_DAYS = 7
MAX_DAYS = 365
DEFAULT_COLOR = "#A18BB7"


def _iso(day: date) -> str:
    return day.isoformat()


def _rub(kopecks: int | float | None) -> float:
    return round(int(kopecks or 0) / 100, 2)


def _energy(conn: Any, since: str, today: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT date, energy FROM checkins WHERE date BETWEEN ? AND ? ORDER BY date",
        (since, today),
    ).fetchall()
    series = [{"date": str(row["date"]), "value": int(row["energy"])} for row in rows]
    avg = round(sum(point["value"] for point in series) / len(series), 1) if series else None
    return {"series": series, "avg": avg, "daysWithData": len(series)}


def _spheres(conn: Any, today: str) -> list[dict[str, Any]]:
    # Активные сферы — всегда; архивные — только если к ним ещё привязаны задачи
    # (иначе «баланс сфер» показывал бы пустые архивные строки).
    rows = conn.execute(
        "SELECT s.id AS id, s.name AS name, s.color AS color, "
        " COUNT(t.id) AS total, "
        " COALESCE(SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END), 0) AS done "
        "FROM spheres AS s "
        "LEFT JOIN tasks AS t ON t.sphere_id = s.id AND t.status <> 'cancelled' "
        "WHERE s.archived = 0 OR EXISTS ("
        "  SELECT 1 FROM tasks AS t2 WHERE t2.sphere_id = s.id AND t2.status <> 'cancelled') "
        "GROUP BY s.id ORDER BY s.sort, s.id"
    ).fetchall()
    items: list[dict[str, Any]] = [
        {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "color": str(row["color"] or DEFAULT_COLOR),
            "done": int(row["done"] or 0),
            "total": int(row["total"] or 0),
        }
        for row in rows
    ]
    loose = conn.execute(
        "SELECT COUNT(*) AS total, "
        " COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS done "
        "FROM tasks WHERE sphere_id IS NULL AND status <> 'cancelled'"
    ).fetchone()
    if loose is not None and int(loose["total"] or 0) > 0:
        items.append(
            {
                "id": None,
                "name": "Без сферы",
                "color": DEFAULT_COLOR,
                "done": int(loose["done"] or 0),
                "total": int(loose["total"] or 0),
            }
        )
    overall = sum(item["total"] for item in items)
    for item in items:
        item["share"] = round(item["total"] / overall, 4) if overall else 0.0
        item["pct"] = round(item["done"] / item["total"] * 100, 1) if item["total"] else 0.0
    return items


def _reading(conn: Any, since: str, today: str) -> dict[str, Any]:
    window = conn.execute(
        "SELECT COALESCE(SUM(pages_read), 0) AS pages, COUNT(*) AS books "
        "FROM books WHERE shelf = 'done' AND finished_at BETWEEN ? AND ?",
        (since, today),
    ).fetchone()
    library = conn.execute(
        "SELECT COALESCE(SUM(pages_read), 0) AS pages, COUNT(*) AS books "
        "FROM books WHERE shelf = 'done'"
    ).fetchone()
    reading_now = conn.execute(
        "SELECT COUNT(*) FROM books WHERE shelf = 'reading'"
    ).fetchone()[0]
    return {
        "pages": int(window["pages"] or 0) if window is not None else 0,
        "booksDone": int(window["books"] or 0) if window is not None else 0,
        "pagesAll": int(library["pages"] or 0) if library is not None else 0,
        "booksDoneAll": int(library["books"] or 0) if library is not None else 0,
        "booksReading": int(reading_now or 0),
    }


def _finance(conn: Any, since: str, today: str) -> dict[str, Any]:
    totals = {"income": 0, "expense": 0}
    for row in conn.execute(
        "SELECT kind, COALESCE(SUM(amount_kopecks), 0) AS total FROM transactions "
        "WHERE substr(ts, 1, 10) BETWEEN ? AND ? GROUP BY kind",
        (since, today),
    ):
        totals[str(row["kind"])] = int(row["total"] or 0)
    income, expense = totals["income"], totals["expense"]

    daily: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT substr(ts, 1, 10) AS day, kind, SUM(amount_kopecks) AS total "
        "FROM transactions WHERE substr(ts, 1, 10) BETWEEN ? AND ? "
        "GROUP BY day, kind ORDER BY day",
        (since, today),
    ):
        day = str(row["day"])
        bucket = daily.setdefault(day, {"date": day, "income": 0.0, "expense": 0.0})
        bucket[str(row["kind"])] = _rub(int(row["total"] or 0))

    return {
        "income": _rub(income),
        "expense": _rub(expense),
        "balance": _rub(income - expense),
        "incomeKopecks": income,
        "expenseKopecks": expense,
        "balanceKopecks": income - expense,
        "series": [daily[day] for day in sorted(daily)],
    }


def _habits(conn: Any, since: str, today: str, days: int) -> dict[str, Any]:
    active = int(
        conn.execute("SELECT COUNT(*) FROM habits WHERE archived = 0").fetchone()[0]
    )
    marks: dict[str, int] = {}
    for row in conn.execute(
        "SELECT date, COUNT(*) AS total FROM habit_marks "
        "WHERE date BETWEEN ? AND ? AND value > 0 GROUP BY date",
        (since, today),
    ):
        marks[str(row["date"])] = int(row["total"])
    series: list[dict[str, Any]] = []
    cursor = date.fromisoformat(since)
    last = date.fromisoformat(today)
    while cursor <= last:
        key = cursor.isoformat()
        covered = marks.get(key, 0)
        series.append(
            {
                "date": key,
                "marks": covered,
                "pct": round(min(100.0, covered / active * 100), 1) if active else 0.0,
            }
        )
        cursor += timedelta(days=1)
    total_marks = sum(marks.values())
    pct = round(min(100.0, total_marks / (active * max(1, days)) * 100), 1) if active else 0.0
    return {"pct": pct, "series": series, "habits": active, "marks": total_marks}


def _tasks(conn: Any, since: str, today: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        " COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS done "
        "FROM tasks WHERE status <> 'cancelled' AND due_date BETWEEN ? AND ?",
        (since, today),
    ).fetchone()
    total = int(row["total"] or 0) if row is not None else 0
    done = int(row["done"] or 0) if row is not None else 0
    return {
        "done": done,
        "total": total,
        "pct": round(done / total * 100, 1) if total else 0.0,
    }



def _summary_pair(item: dict[str, Any]) -> dict[str, Any]:
    """Пара в форме, которую уже понимает экран «Аналитика» (плюс поля v2)."""
    ok = item["status"] == "ok"
    return {
        "left": item["left"],
        "right": item["right"],
        "label": item["label"],
        "leftLabel": item["leftLabel"],
        "rightLabel": item["rightLabel"],
        "left_label": item["leftLabel"],
        "right_label": item["rightLabel"],
        "lag": item["lag"],
        "n": item["n"],
        "r": item["r"],
        "rho": item["rho"],
        "p": item["p"],
        "q": item["q"],
        "ci": item["ci"],
        "effectPct": item["effectPct"],
        "strength": item["strength"],
        "status": item["status"],
        "text": item["text"],
        "sentence": item["sentence"],
        "advice": item["advice"],
        "enough_data": ok,
        "enoughData": ok,
    }


def _summary_correlations(conn: Any, today_day: date, days: int) -> list[dict[str, Any]]:
    """Лучшие (по |r|) пары окна; если считать нечего — честные отказы."""
    report = build_correlation_report(conn, today_day, days=days, lags=2)
    computed = [item for item in report["pairs"] if item["r"] is not None]
    rows = [_summary_pair(item) for item in computed[:8]]
    if rows:
        return rows
    for item in report["notReady"][:3]:
        left = str(item["left"])
        right = str(item["right"])
        left_label = SERIES_LABELS.get(left, left)
        right_label = SERIES_LABELS.get(right, right)
        no_variance = item["reason"] == "no_variance"
        text = (
            "Показатель не меняется — корреляцию не посчитать"
            if no_variance
            else "Мало реальных дней: %d из %d"
            % (int(item["daysHave"]), int(item["needDays"]))
        )
        rows.append(
            {
                "left": left,
                "right": right,
                "label": "%s ↔ %s" % (left_label, right_label),
                "leftLabel": left_label,
                "rightLabel": right_label,
                "left_label": left_label,
                "right_label": right_label,
                "lag": 0,
                "n": int(item["daysHave"]),
                "r": None,
                "rho": None,
                "p": None,
                "q": None,
                "ci": None,
                "effectPct": None,
                "strength": None,
                "status": "no_variance" if no_variance else "insufficient_data",
                "text": text,
                "sentence": "",
                "advice": "Продолжайте записывать оба показателя: без данных связи не проверить.",
                "enough_data": False,
                "enoughData": False,
            }
        )
    return rows


@router.get("/analytics/summary")
def analytics_summary(
    conn: ConnectionDep,
    settings: SettingsDep,
    days: int = Query(DEFAULT_DAYS, ge=MIN_DAYS, le=MAX_DAYS, description="Окно в днях"),
) -> dict[str, Any]:
    """Сводная аналитика за окно ``days`` (по умолчанию 30)."""
    today_day = settings.now().date()
    since_day = today_day - timedelta(days=days - 1)
    since, today = _iso(since_day), _iso(today_day)
    return {
        "days": days,
        "from": since,
        "to": today,
        "energy": _energy(conn, since, today),
        "spheres": _spheres(conn, today),
        "reading": _reading(conn, since, today),
        "finance": _finance(conn, since, today),
        "habits": _habits(conn, since, today, days),
        "tasks": _tasks(conn, since, today),
        "correlations": _summary_correlations(conn, today_day, days),
    }
