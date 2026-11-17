"""Универсальные разделы дашборда (v3).

Одна форма ответа на все страницы SPA: КПИ, дневные ряды, последние записи,
привычки и цели. Так фронтенд не зависит от того, сколько таблиц за разделом:
«Сон» и «Настроение» читают ``metrics``, «Журнал» и «Питание» — ``entries``,
«Привычки»/«Цели» — свои таблицы, а страница просто рисует то, что пришло.

Раздел определяется данными (:data:`SECTIONS`), а не кодом: чтобы добавить
экран, достаточно дописать спеку и не трогать роутер.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.server.api.deps import ConnectionDep

__all__ = ["SECTION_NAMES", "SECTIONS", "build_section", "router"]

router = APIRouter(prefix="/api", tags=["sections"])

#: Сколько дней отдаём по умолчанию и сколько разрешаем запросить.
DEFAULT_DAYS = 30
MAX_DAYS = 365
#: Сколько последних записей отдаём в списках.
DEFAULT_LIMIT = 40
MAX_LIMIT = 200

#: Метрики, которые при суммировании за день складываются, а не усредняются.
SUM_METRICS = frozenset({"water", "calories", "protein", "steps", "workout"})

#: Человеческие подписи типов записей и метрик.
KIND_LABELS = {
    "food": "Еда",
    "water": "Вода",
    "workout": "Тренировка",
    "journal": "Дневник",
    "note": "Заметка",
    "thought": "Мысль",
    "mood": "Настроение",
    "sleep": "Сон",
    "energy": "Энергия",
    "weight": "Вес",
    "fat": "Жир",
    "muscle": "Мышцы",
    "body_water": "Вода в теле",
    "body": "Тело",
}

#: Спека раздела: какие метрики/записи читать и как подписать экран.
SECTIONS: dict[str, dict[str, Any]] = {
    "journal": {
        "title": "Журнал",
        "note": "Записи, поиск по тексту и экспорт в Obsidian.",
        "entries": ("journal", "note", "thought"),
        "metrics": (),
    },
    "analytics": {
        "title": "Аналитика",
        "note": "Динамика показателей и связи между ними.",
        "entries": ("journal",),
        "metrics": ("sleep", "mood", "energy", "weight", "water"),
    },
    "sleep": {
        "title": "Сон",
        "note": "Часы, качество и регулярность.",
        "entries": (),
        "metrics": ("sleep",),
    },
    "mood": {
        "title": "Настроение",
        "note": "Настроение, энергия и заметки о дне.",
        "entries": ("mood",),
        "metrics": ("mood", "energy"),
    },
    "nutrition": {
        "title": "Питание",
        "note": "Дневник еды, калории и вода.",
        "entries": ("food", "water"),
        "metrics": ("water", "calories", "protein"),
    },
    "workouts": {
        "title": "Тренировки",
        "note": "История тренировок и нагрузка за неделю.",
        "entries": ("workout",),
        "metrics": ("steps",),
    },
    "body": {
        "title": "Тело",
        "note": "Вес, состав тела и обхваты.",
        "entries": ("body",),
        "metrics": ("weight", "fat", "muscle", "body_water"),
    },
    "habits": {
        "title": "Привычки",
        "note": "Отметки за неделю и серии дней подряд.",
        "entries": (),
        "metrics": (),
    },
    "goals": {
        "title": "Цели",
        "note": "Прогресс и сроки.",
        "entries": (),
        "metrics": (),
    },
    "today": {
        "title": "Сегодня",
        "note": "Один экран на день: что уже сделано и что дальше.",
        "entries": ("journal", "thought", "food", "workout", "water", "note"),
        "metrics": ("sleep", "mood", "water", "weight"),
    },
    "settings": {
        "title": "Настройки",
        "note": "Тема, часовой пояс, экспорт и подключения.",
        "entries": (),
        "metrics": (),
    },
}

#: Имена разделов, которые умеет отдавать роутер.
SECTION_NAMES = tuple(SECTIONS)


def _iso(day: date) -> str:
    """Дата в формате БД (ISO-день)."""
    return day.isoformat()


def _shift(day: date, delta: int) -> date:
    return day + timedelta(days=delta)


def _day_range(today: date, days: int) -> list[str]:
    """Список дней от старого к новому, включая сегодня."""
    return [_iso(_shift(today, -offset)) for offset in range(days - 1, -1, -1)]


def _metric_unit(conn: sqlite3.Connection, kind: str) -> str:
    row = conn.execute(
        "SELECT unit FROM metrics WHERE kind = ? AND unit <> '' ORDER BY date DESC LIMIT 1",
        (kind,),
    ).fetchone()
    return str(row[0]) if row else ""


def _metric_rows(
    conn: sqlite3.Connection, kind: str, since: str
) -> list[tuple[str, float]]:
    """Значения метрики по дням; несколько замеров в день сводятся в один."""
    rows = conn.execute(
        "SELECT date, value FROM metrics WHERE kind = ? AND date >= ? ORDER BY date",
        (kind, since),
    ).fetchall()
    buckets: dict[str, list[float]] = {}
    for day, value in rows:
        buckets.setdefault(str(day), []).append(float(value))
    combine = sum if kind in SUM_METRICS else (lambda values: sum(values) / len(values))
    return [(day, round(combine(values), 2)) for day, values in sorted(buckets.items())]


def _series(
    conn: sqlite3.Connection, kind: str, since: str, days: int
) -> dict[str, Any] | None:
    """Ряд по одному показателю; ``None``, если данных не было вовсе."""
    rows = _metric_rows(conn, kind, since)
    if not rows:
        return None
    values = [value for _, value in rows]
    last = values[-1]
    tail = values[-7:]
    avg = round(sum(tail) / len(tail), 2)
    return {
        "key": kind,
        "label": KIND_LABELS.get(kind, kind),
        "unit": _metric_unit(conn, kind),
        "points": [{"date": day, "value": value} for day, value in rows],
        "last": last,
        "avg": avg,
        "total": round(sum(values), 2),
        "days": days,
    }


def _kpi(label: str, value: Any, unit: str = "", hint: str = "") -> dict[str, Any]:
    return {"label": label, "value": value, "unit": unit, "hint": hint}


def _entries(
    conn: sqlite3.Connection,
    kinds: Sequence[str],
    since: str,
    limit: int,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """Последние записи выбранных типов (свежие сверху)."""
    if not kinds:
        return []
    placeholders = ", ".join("?" for _ in kinds)
    sql = (
        "SELECT id, ts, kind, text, source FROM entries "
        "WHERE kind IN (%s) AND date >= ?" % placeholders
    )
    params: list[Any] = list(kinds) + [since]
    if search:
        sql += " AND text LIKE ?"
        params.append("%" + search.strip() + "%")
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    items: list[dict[str, Any]] = []
    for entry_id, ts, kind, text, source in rows:
        stamp = str(ts)
        items.append(
            {
                "id": int(entry_id),
                "date": stamp[:10],
                "time": stamp[11:16],
                "kind": str(kind),
                "kindLabel": KIND_LABELS.get(str(kind), str(kind)),
                "text": str(text),
                "source": str(source),
            }
        )
    return items


def _habits(conn: sqlite3.Connection, today: date) -> list[dict[str, Any]]:
    """Привычки с отметками за 7 дней (старое → новое) и серией дней подряд."""
    week = _day_range(today, 7)
    lookback = _iso(_shift(today, -400))
    marks: dict[int, set[str]] = {}
    for habit_id, day in conn.execute(
        "SELECT habit_id, date FROM habit_marks WHERE date >= ? AND value > 0",
        (lookback,),
    ).fetchall():
        marks.setdefault(int(habit_id), set()).add(str(day))

    rows = conn.execute(
        "SELECT id, name, emoji, target_per_week FROM habits "
        "WHERE archived = 0 ORDER BY id"
    ).fetchall()
    habits: list[dict[str, Any]] = []
    for habit_id, name, emoji, target in rows:
        done = marks.get(int(habit_id), set())
        streak = 0
        cursor = today
        while streak < len(done) + 1 and _iso(cursor) in done:
            streak += 1
            cursor = _shift(cursor, -1)
        habits.append(
            {
                "id": int(habit_id),
                "name": str(name),
                "emoji": str(emoji),
                "target": int(target),
                "marks": [day in done for day in week],
                "week": list(week),
                "doneWeek": sum(1 for day in week if day in done),
                "total": len(done),
                "streak": streak,
            }
        )
    return habits


def _goals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Цели с процентом выполнения (только актуальные и недавно закрытые)."""
    rows = conn.execute(
        "SELECT id, title, target, current, unit, deadline, status FROM goals "
        "WHERE status IN ('active', 'done') ORDER BY status, id"
    ).fetchall()
    goals: list[dict[str, Any]] = []
    for goal_id, title, target, current, unit, deadline, status in rows:
        target_value = float(target) if target is not None else None
        current_value = float(current or 0)
        percent = 0
        if target_value:
            percent = max(0, min(100, round(current_value / target_value * 100)))
        goals.append(
            {
                "id": int(goal_id),
                "title": str(title),
                "current": current_value,
                "target": target_value,
                "unit": str(unit or ""),
                "deadline": str(deadline) if deadline else None,
                "status": str(status),
                "percent": percent,
            }
        )
    return goals


def build_section(
    conn: sqlite3.Connection,
    name: str,
    *,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    search: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Собирает payload раздела; бросает :class:`KeyError` на неизвестное имя."""
    if name not in SECTIONS:
        raise KeyError(name)
    spec = SECTIONS[name]
    today = today or date.today()
    days = max(1, min(MAX_DAYS, int(days)))
    limit = max(1, min(MAX_LIMIT, int(limit)))
    since = _iso(_shift(today, -(days - 1)))

    series: list[dict[str, Any]] = []
    for kind in spec["metrics"]:
        item = _series(conn, kind, since, days)
        if item is not None:
            series.append(item)

    entries = _entries(conn, spec["entries"], since, limit, search)

    kpis: list[dict[str, Any]] = []
    for item in series:
        hint = "среднее за 7 дней"
        if item["key"] in SUM_METRICS:
            hint = "сумма за 7 дней"
        kpis.append(_kpi(item["label"], item["last"], item["unit"], hint))
    if entries:
        kpis.append(_kpi("Записей", len(entries), "шт", "в выбранном периоде"))

    payload: dict[str, Any] = {
        "name": name,
        "title": spec["title"],
        "note": spec["note"],
        "days": days,
        "since": since,
        "today": _iso(today),
        "kpis": kpis,
        "series": series,
        "items": entries,
        "empty": not series and not entries,
    }
    if name == "habits":
        payload["habits"] = _habits(conn, today)
        payload["empty"] = not payload["habits"]
    if name == "goals":
        payload["goals"] = _goals(conn)
        payload["empty"] = not payload["goals"]
    if name == "journal":
        payload["count"] = len(entries)
    return payload


@router.get("/sections/{name}")
def get_section(
    name: str,
    conn: ConnectionDep,
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    q: str | None = Query(None, max_length=120),
) -> dict[str, Any]:
    """Раздел дашборда: КПИ, ряды, записи, привычки или цели."""
    if name not in SECTIONS:
        raise HTTPException(status_code=404, detail="Неизвестный раздел: %s" % name)
    return build_section(conn, name, days=days, limit=limit, search=q)


# --------------------------------------------------------------------------
# Запись: отметка привычки, новая привычка, цель
# --------------------------------------------------------------------------


class HabitMarkIn(BaseModel):
    """Отметка привычки: дата по умолчанию — сегодня, value 0 снимает."""

    day: str | None = Field(None, description="Дата ISO, по умолчанию сегодня")
    value: float = Field(1.0, ge=0, le=24)


class HabitIn(BaseModel):
    """Новая привычка."""

    name: str = Field(..., min_length=1, max_length=80)
    emoji: str = Field("", max_length=8)
    target_per_week: int = Field(7, ge=1, le=7)


class GoalIn(BaseModel):
    """Новая цель."""

    title: str = Field(..., min_length=1, max_length=120)
    target: float | None = Field(None, ge=0)
    unit: str = Field("", max_length=16)
    deadline: str | None = Field(None, max_length=10)


class GoalPatch(BaseModel):
    """Правка цели: текущее значение и/или статус."""

    current: float | None = Field(None, ge=0)
    status: str | None = Field(None, pattern="^(active|done|dropped)$")


@router.post("/habits/{habit_id}/mark")
def mark_habit(habit_id: int, payload: HabitMarkIn, conn: ConnectionDep) -> dict[str, Any]:
    """Ставит или снимает отметку привычки за день."""
    row = conn.execute("SELECT id FROM habits WHERE id = ?", (habit_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Привычка не найдена")
    day = payload.day or _iso(date.today())
    with conn:
        if payload.value > 0:
            conn.execute(
                "INSERT INTO habit_marks (habit_id, date, value) VALUES (?, ?, ?) "
                "ON CONFLICT(habit_id, date) DO UPDATE SET value = excluded.value",
                (habit_id, day, payload.value),
            )
        else:
            conn.execute(
                "DELETE FROM habit_marks WHERE habit_id = ? AND date = ?", (habit_id, day)
            )
    return {"habitId": habit_id, "day": day, "value": payload.value}


@router.post("/habits", status_code=201)
def create_habit(payload: HabitIn, conn: ConnectionDep) -> dict[str, Any]:
    """Создаёт привычку; повтор имени возвращает существующую."""
    with conn:
        conn.execute(
            "INSERT INTO habits (name, emoji, target_per_week) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO NOTHING",
            (payload.name.strip(), payload.emoji, payload.target_per_week),
        )
    row = conn.execute(
        "SELECT id, name, emoji, target_per_week FROM habits WHERE name = ?",
        (payload.name.strip(),),
    ).fetchone()
    return {
        "id": int(row[0]),
        "name": str(row[1]),
        "emoji": str(row[2]),
        "target": int(row[3]),
    }


@router.post("/goals", status_code=201)
def create_goal(payload: GoalIn, conn: ConnectionDep) -> dict[str, Any]:
    """Создаёт цель."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO goals (title, target, current, unit, deadline, status) "
            "VALUES (?, ?, 0, ?, ?, 'active')",
            (payload.title.strip(), payload.target, payload.unit, payload.deadline),
        )
    return {
        "id": int(cursor.lastrowid or 0),
        "title": payload.title.strip(),
        "target": payload.target,
        "current": 0.0,
        "unit": payload.unit,
        "deadline": payload.deadline,
        "status": "active",
    }


@router.patch("/goals/{goal_id}")
def patch_goal(goal_id: int, payload: GoalPatch, conn: ConnectionDep) -> dict[str, Any]:
    """Обновляет текущее значение или статус цели."""
    row = conn.execute("SELECT id FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Цель не найдена")
    sets: list[str] = []
    params: list[Any] = []
    if payload.current is not None:
        sets.append("current = ?")
        params.append(payload.current)
    if payload.status is not None:
        sets.append("status = ?")
        params.append(payload.status)
    if not sets:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    params.append(goal_id)
    with conn:
        conn.execute("UPDATE goals SET %s WHERE id = ?" % ", ".join(sets), params)
    updated = conn.execute(
        "SELECT id, title, target, current, unit, deadline, status FROM goals WHERE id = ?",
        (goal_id,),
    ).fetchone()
    return {
        "id": int(updated[0]),
        "title": str(updated[1]),
        "target": float(updated[2]) if updated[2] is not None else None,
        "current": float(updated[3] or 0),
        "unit": str(updated[4] or ""),
        "deadline": str(updated[5]) if updated[5] else None,
        "status": str(updated[6]),
    }
