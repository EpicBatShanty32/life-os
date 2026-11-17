"""Эндпоинты состояния сервера и ряда веса с EMA.

``GET /api/health`` — не требует токена: по нему проверяют живость сервиса
(в том числе launchd и внешние healthcheck'и). ``GET /api/health/weight`` —
ряд измерений веса за период и сглаженная EMA; при отсутствии данных
возвращается пустой ряд, а не ошибка.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app import __version__
from app.server.api.aggregates import ema_series
from app.server.api.deps import ConnectionDep, SettingsDep

__all__ = ["WeightPoint", "WeightSeries", "router"]

router = APIRouter(prefix="/api", tags=["health"])


class WeightPoint(BaseModel):
    """Точка ряда веса: среднее за день и EMA на эту дату."""

    date: str
    value: float
    ema: float


class WeightSeries(BaseModel):
    """Ряд веса за ``days`` дней и EMA с окном ``window``."""

    days: int
    window: int
    unit: str = "кг"
    current: float | None = None
    ema: float | None = None
    delta: float | None = None
    target_kg: float | None = None
    points: list[WeightPoint] = Field(default_factory=list)


@router.get("/health")
def health(conn: ConnectionDep, settings: SettingsDep) -> dict[str, Any]:
    """Живость сервера, доступ к БД и число задач в очереди."""
    try:
        conn.execute("SELECT 1").fetchone()
        db_status = "ok"
    except sqlite3.Error:
        db_status = "error"

    queued = 0
    try:
        row = conn.execute("SELECT count(*) FROM jobs WHERE status = 'queued'").fetchone()
        queued = int(row[0]) if row is not None else 0
    except sqlite3.Error:
        pass

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "app": settings.app_name,
        "version": __version__,
        "time": settings.now().isoformat(timespec="seconds"),
        "tz": settings.timezone,
        "db": db_status,
        "jobs_queued": queued,
    }


@router.get("/health/weight")
def weight_series(
    conn: ConnectionDep,
    settings: SettingsDep,
    days: int = Query(14, ge=1, le=365, description="Сколько дней показать"),
    window: int = Query(7, ge=2, le=60, description="Окно EMA"),
) -> WeightSeries:
    """Ряд веса за ``days`` дней + EMA; при отсутствии данных — пустой ряд."""
    today = settings.now().date()
    since = (today - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT date, value, unit FROM metrics WHERE kind = 'weight' AND date >= ? "
        "ORDER BY date, id",
        (since,),
    ).fetchall()

    daily: dict[str, list[float]] = {}
    unit = "кг"
    for row in rows:
        daily.setdefault(str(row["date"]), []).append(float(row["value"]))
        unit = str(row["unit"] or unit)

    target = conn.execute(
        "SELECT 1 FROM goals WHERE status = 'active' AND unit = 'кг' "
        "ORDER BY deadline IS NULL, deadline LIMIT 1"
    ).fetchone()

    if not daily:
        latest = conn.execute(
            "SELECT value, unit FROM metrics WHERE kind = 'weight' "
            "ORDER BY date DESC, id DESC LIMIT 1"
        ).fetchone()
        return WeightSeries(
            days=days,
            window=window,
            unit=str(latest["unit"] or "кг") if latest is not None else unit,
            current=float(latest["value"]) if latest is not None else None,
            target_kg=None if target is None else _active_weight_goal(conn),
            points=[],
        )

    dates = sorted(daily)
    averages = [sum(daily[day]) / len(daily[day]) for day in dates]
    smoothed = ema_series(averages, window)
    points = [
        WeightPoint(date=day, value=round(value, 2), ema=round(ema, 2))
        for day, value, ema in zip(dates, averages, smoothed)
    ]
    return WeightSeries(
        days=days,
        window=window,
        unit=unit,
        current=round(averages[-1], 2),
        ema=round(smoothed[-1], 2),
        delta=round(averages[-1] - averages[0], 2),
        target_kg=None if target is None else _active_weight_goal(conn),
        points=points,
    )


def _active_weight_goal(conn) -> float | None:
    row = conn.execute(
        "SELECT target FROM goals WHERE status = 'active' AND unit = 'кг' AND target IS NOT NULL "
        "ORDER BY deadline IS NULL, deadline LIMIT 1"
    ).fetchone()
    return float(row["target"]) if row is not None and row["target"] is not None else None
