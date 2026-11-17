"""Личное: сводка дня, чек-ин энергии и итоги недели/месяца (§12.5).

Эндпоинты:

* ``GET  /api/personal/summary?date=YYYY-MM-DD`` — чек-ин дня, привычки с
  отметками за неделю и серией, счётчики задач дня, страницы чтения, энергия
  за 7 дней;
* ``POST /api/personal/checkin`` — upsert по дате (повторный чек-ин
  перезаписывает энергию, состояние и заметку);
* ``GET  /api/personal/review?period=week|month&start=YYYY-MM-DD`` — факты
  периода, незавершённые задачи и уже сохранённый итог;
* ``POST /api/personal/review`` — сохранить итог (upsert по ``period`` +
  ``startDate``);
* ``GET  /api/personal/reviews?limit=12`` — последние сохранённые итоги.

Факты итога считаются так же, как в демо-сидере (§10.4): задачи с дедлайном
внутри периода (закрытые/всего, без отменённых), страницы книг, завершённых в
периоде, средняя энергия чек-инов и доля отметок привычек. Отдельного журнала
прочитанных страниц в схеме нет, поэтому «страницы» — это объём книг,
законченных в периоде; в документации поля это оговорено.
"""

from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.server.api.deps import ConnectionDep, SettingsDep
from app.server.settings import Settings

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["personal"])

Period = Literal["week", "month"]
PERIODS: tuple[str, ...] = ("week", "month")

WEEK_DAYS = 7
ENERGY_MIN, ENERGY_MAX = 1, 5


# --------------------------------------------------------------------------
# Даты и периоды
# --------------------------------------------------------------------------


def _parse_day(value: str | None, settings: Settings) -> date:
    """``YYYY-MM-DD``; пусто — сегодня; иначе 422."""
    text = (value or "").strip()
    if not text:
        return settings.now().date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Дата в формате YYYY-MM-DD") from exc


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _period_range(period: str, start: date) -> tuple[date, date]:
    """Границы периода: неделя — 7 дней с ``start``, месяц — до конца месяца."""
    if period == "week":
        return start, start + timedelta(days=WEEK_DAYS - 1)
    last_day = monthrange(start.year, start.month)[1]
    return start, start.replace(day=last_day)


def _default_start(period: str, today: date) -> date:
    if period == "week":
        return _week_start(today)
    return today.replace(day=1)


# --------------------------------------------------------------------------
# Входные модели
# --------------------------------------------------------------------------


class CheckinIn(BaseModel):
    """Тело ``POST /api/personal/checkin``; ``date`` по умолчанию — сегодня."""

    model_config = ConfigDict(extra="ignore")

    date: str | None = None
    energy: int = Field(ge=ENERGY_MIN, le=ENERGY_MAX)
    mood: str = Field("", max_length=60)
    note: str = Field("", max_length=2000)


class ReviewIn(BaseModel):
    """Тело ``POST /api/personal/review``."""

    model_config = ConfigDict(extra="ignore")

    period: Period
    startDate: str  # noqa: N815 - camelCase JSON
    wins: str = Field("", max_length=4000)
    misses: str = Field("", max_length=4000)
    focus: str = Field("", max_length=4000)
    care: str = Field("", max_length=4000)

    @field_validator("startDate")
    @classmethod
    def _start_required(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("startDate обязателен")
        return text


# --------------------------------------------------------------------------
# Строки БД → JSON
# --------------------------------------------------------------------------


def _checkin_dict(row: Any) -> dict[str, Any]:
    return {
        "date": str(row["date"]),
        "energy": int(row["energy"]),
        "mood": str(row["mood"] or ""),
        "note": str(row["note"] or ""),
        "created": str(row["created"] or ""),
    }


def _review_dict(row: Any) -> dict[str, Any]:
    try:
        facts = json.loads(str(row["facts_json"] or "{}"))
    except ValueError:  # pragma: no cover - битый JSON не должен ломать API
        facts = {}
    if not isinstance(facts, dict):
        facts = {}
    return {
        "id": int(row["id"]),
        "period": str(row["period"]),
        "startDate": str(row["start_date"]),
        "endDate": str(row["end_date"]),
        "wins": str(row["wins"] or ""),
        "misses": str(row["misses"] or ""),
        "focus": str(row["focus"] or ""),
        "care": str(row["care"] or ""),
        "facts": facts,
        "created": str(row["created"] or ""),
    }


def _iso(day: date) -> str:
    return day.isoformat()


def _day_range(day: date, days: int) -> list[str]:
    return [_iso(day - timedelta(days=offset)) for offset in range(days - 1, -1, -1)]


# --------------------------------------------------------------------------
# Привычки, задачи, чтение, энергия
# --------------------------------------------------------------------------


def _habits_summary(conn: Any, day: date) -> list[dict[str, Any]]:
    """Активные привычки: серия, отметки за 7 дней и норма в неделю."""
    week = _day_range(day, WEEK_DAYS)
    lookback = _iso(day - timedelta(days=400))
    marks: dict[int, set[str]] = {}
    for habit_id, mark_day in conn.execute(
        "SELECT habit_id, date FROM habit_marks WHERE date >= ? AND value > 0",
        (lookback,),
    ).fetchall():
        marks.setdefault(int(habit_id), set()).add(str(mark_day))

    rows = conn.execute(
        "SELECT id, name, emoji, color, target_per_week FROM habits "
        "WHERE archived = 0 ORDER BY id"
    ).fetchall()
    habits: list[dict[str, Any]] = []
    for habit_id, name, emoji, color, target in rows:
        done = marks.get(int(habit_id), set())
        streak = 0
        cursor = day
        while streak < len(done) + 1 and _iso(cursor) in done:
            streak += 1
            cursor -= timedelta(days=1)
        week_marks = {mark: 1 for mark in week if mark in done}
        habits.append(
            {
                "id": int(habit_id),
                "name": str(name),
                "emoji": str(emoji or ""),
                "color": str(color or "#A18BB7"),
                "targetPerWeek": int(target or 0),
                "streak": streak,
                "marks": week_marks,
                "week": list(week),
                "doneWeek": len(week_marks),
            }
        )
    return habits


def _task_counts(conn: Any, day: str) -> dict[str, int]:
    """Задачи дня: открытые по ресурсу, закрытые и всего (без отменённых)."""
    counts = {"must": 0, "important": 0, "low": 0, "done": 0, "total": 0}
    rows = conn.execute(
        "SELECT resource, status, COUNT(*) AS total FROM tasks "
        "WHERE status <> 'cancelled' "
        "  AND (due_date = ? OR substr(COALESCE(start_at, ''), 1, 10) = ?) "
        "GROUP BY resource, status",
        (day, day),
    ).fetchall()
    for row in rows:
        amount = int(row["total"] or 0)
        counts["total"] += amount
        if str(row["status"]) == "done":
            counts["done"] += amount
        elif str(row["resource"]) in counts:
            counts[str(row["resource"])] += amount
    return counts


def _pages_in_period(conn: Any, start: str, end: str) -> int:
    """Страницы книг, завершённых в периоде (журнала чтения в схеме нет)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(pages_read), 0) FROM books "
        "WHERE shelf = 'done' AND finished_at BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def _reading_summary(conn: Any, day: date) -> dict[str, int]:
    week_start = _week_start(day)
    return {
        "pagesToday": _pages_in_period(conn, _iso(day), _iso(day)),
        "pagesWeek": _pages_in_period(conn, _iso(week_start), _iso(day)),
    }


def _energy_series(conn: Any, day: date, days: int = WEEK_DAYS) -> list[dict[str, Any]]:
    """Энергия за ``days`` дней; пропуски — ``value: null`` (честно, без нулей)."""
    window = _day_range(day, days)
    values: dict[str, int] = {}
    for row in conn.execute(
        "SELECT date, energy FROM checkins WHERE date BETWEEN ? AND ?",
        (window[0], window[-1]),
    ).fetchall():
        values[str(row["date"])] = int(row["energy"])
    return [{"date": key, "value": values.get(key)} for key in window]


# --------------------------------------------------------------------------
# Факты итогов (как в сидере §10.4)
# --------------------------------------------------------------------------


def _review_facts(conn: Any, start: date, end: date) -> dict[str, Any]:
    start_s, end_s = _iso(start), _iso(end)
    tasks_total = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status <> 'cancelled' "
        "AND due_date IS NOT NULL AND due_date BETWEEN ? AND ?",
        (start_s, end_s),
    ).fetchone()[0]
    tasks_done = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'done' "
        "AND due_date IS NOT NULL AND due_date BETWEEN ? AND ?",
        (start_s, end_s),
    ).fetchone()[0]
    pages_read = _pages_in_period(conn, start_s, end_s)
    energy_row = conn.execute(
        "SELECT AVG(energy) FROM checkins WHERE date BETWEEN ? AND ?", (start_s, end_s)
    ).fetchone()
    energy_avg = float(energy_row[0]) if energy_row and energy_row[0] is not None else None
    habits = int(
        conn.execute("SELECT COUNT(*) FROM habits WHERE archived = 0").fetchone()[0]
    )
    marks = int(
        conn.execute(
            "SELECT COUNT(*) FROM habit_marks WHERE date BETWEEN ? AND ?", (start_s, end_s)
        ).fetchone()[0]
    )
    days = max(1, (end - start).days + 1)
    habits_pct = round(100.0 * marks / (habits * days)) if habits else 0
    return {
        "tasksDone": int(tasks_done),
        "tasksTotal": int(tasks_total),
        "pagesRead": int(pages_read),
        "energyAvg": round(energy_avg, 1) if energy_avg is not None else None,
        "habitsPct": min(100, int(habits_pct)),
        "days": days,
    }


def _open_tasks(conn: Any, end: date, limit: int = 100) -> list[dict[str, Any]]:
    """Незавершённые задачи к концу периода, включая просроченные."""
    rows = conn.execute(
        "SELECT id, title, due_date FROM tasks "
        "WHERE status = 'todo' AND due_date IS NOT NULL AND due_date <= ? "
        "ORDER BY due_date, id LIMIT ?",
        (_iso(end), limit),
    ).fetchall()
    return [
        {"id": int(row["id"]), "title": str(row["title"]), "dueDate": str(row["due_date"])}
        for row in rows
    ]


def _load_review(conn: Any, period: str, start_s: str) -> Any:
    return conn.execute(
        "SELECT * FROM reviews WHERE period = ? AND start_date = ?", (period, start_s)
    ).fetchone()


# --------------------------------------------------------------------------
# Сводка дня
# --------------------------------------------------------------------------


@router.get("/personal/summary")
def personal_summary(
    conn: ConnectionDep,
    settings: SettingsDep,
    date: str | None = Query(None, max_length=10, description="YYYY-MM-DD; пусто — сегодня"),
) -> dict[str, Any]:
    """Ресурс дня: чек-ин, привычки, задачи, чтение и энергия за неделю."""
    day = _parse_day(date, settings)
    day_s = _iso(day)

    checkin_row = conn.execute("SELECT * FROM checkins WHERE date = ?", (day_s,)).fetchone()
    return {
        "date": day_s,
        "checkin": _checkin_dict(checkin_row) if checkin_row is not None else None,
        "habits": _habits_summary(conn, day),
        "tasks": _task_counts(conn, day_s),
        "reading": _reading_summary(conn, day),
        "energy7": _energy_series(conn, day),
    }


@router.post("/personal/checkin")
def save_checkin(
    payload: CheckinIn, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Чек-ин энергии: повторный в тот же день перезаписывает предыдущий."""
    day_s = _iso(_parse_day(payload.date, settings))
    with conn:
        conn.execute(
            "INSERT INTO checkins (date, energy, mood, note) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET energy = excluded.energy, "
            "mood = excluded.mood, note = excluded.note",
            (day_s, payload.energy, payload.mood, payload.note),
        )
    row = conn.execute("SELECT * FROM checkins WHERE date = ?", (day_s,)).fetchone()
    return _checkin_dict(row)


# --------------------------------------------------------------------------
# Итоги
# --------------------------------------------------------------------------


@router.get("/personal/review")
def get_review(
    conn: ConnectionDep,
    settings: SettingsDep,
    period: Period = Query("week"),
    start: str | None = Query(None, max_length=10, description="YYYY-MM-DD; пусто — текущий"),
) -> dict[str, Any]:
    """Факты периода, незавершённые задачи и сохранённый итог (если есть)."""
    today = settings.now().date()
    start_day = _parse_day(start, settings) if start else _default_start(period, today)
    start_day, end_day = _period_range(period, start_day)
    existing = _load_review(conn, period, _iso(start_day))
    return {
        "period": period,
        "startDate": _iso(start_day),
        "endDate": _iso(end_day),
        "existing": _review_dict(existing) if existing is not None else None,
        "facts": _review_facts(conn, start_day, end_day),
        "openTasks": _open_tasks(conn, end_day),
    }


@router.post("/personal/review")
def save_review(
    payload: ReviewIn, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Сохраняет итог; повторное сохранение того же периода обновляет текст."""
    start_day = _parse_day(payload.startDate, settings)
    start_day, end_day = _period_range(payload.period, start_day)
    facts = _review_facts(conn, start_day, end_day)
    with conn:
        conn.execute(
            "INSERT INTO reviews "
            "(period, start_date, end_date, wins, misses, focus, care, facts_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(period, start_date) DO UPDATE SET end_date = excluded.end_date, "
            "wins = excluded.wins, misses = excluded.misses, focus = excluded.focus, "
            "care = excluded.care, facts_json = excluded.facts_json",
            (
                payload.period,
                _iso(start_day),
                _iso(end_day),
                payload.wins,
                payload.misses,
                payload.focus,
                payload.care,
                json.dumps(facts, ensure_ascii=False, sort_keys=True),
            ),
        )
    row = _load_review(conn, payload.period, _iso(start_day))
    return _review_dict(row)


@router.get("/personal/reviews")
def list_reviews(
    conn: ConnectionDep,
    limit: int = Query(12, ge=1, le=100, description="Сколько последних итогов вернуть"),
) -> dict[str, Any]:
    """История итогов: свежие периоды сверху."""
    rows = conn.execute(
        "SELECT * FROM reviews ORDER BY start_date DESC, created DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {
        "items": [_review_dict(row) for row in rows],
        "limit": limit,
        "total": len(rows),
    }
