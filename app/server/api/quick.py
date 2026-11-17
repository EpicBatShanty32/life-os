"""Быстрые записи с дашборда: ``POST /api/quick`` и ``POST /api/water``.

Контракт ``/api/quick`` совпадает с SPA (``QuickEntryInput``):
``{"type": "note" | "event" | "metric", "text": "..."}``. Для ``metric`` можно
передать ``value``/``kind``/``unit``/``date``; если числа нет в поле, оно
вытаскивается из текста («вес 79,4» → 79.4). После записи в WebSocket уходит
``entry.created`` (журнал) или ``sync.updated`` (метрика/вода), чтобы дашборд
обновился без перезагрузки.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.server.api.aggregates import pref_int
from app.server.api.deps import BrokerDep, ConnectionDep, SettingsDep
from app.server.db import add_entry, add_metric
from app.server.models import Entry, Metric
from app.server.ws import EVENT_ENTRY_CREATED, EVENT_SYNC_UPDATED

__all__ = ["QuickEntryIn", "WaterIn", "router"]

router = APIRouter(prefix="/api", tags=["quick"])

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

_UNIT_KINDS = {
    "кг": "weight",
    "kg": "weight",
    "мл": "water",
    "ml": "water",
    "ч": "sleep",
    "час": "sleep",
    "h": "sleep",
    "ккал": "calories",
    "kcal": "calories",
}


class QuickEntryIn(BaseModel):
    """Тело ``POST /api/quick``; лишние поля игнорируются."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["note", "event", "metric"] = "note"
    text: str = Field(default="", max_length=4000)
    kind: str | None = Field(default=None, max_length=40)
    value: float | None = None
    unit: str = Field(default="", max_length=16)
    date: str | None = Field(default=None, max_length=10)
    meta: dict[str, Any] = Field(default_factory=dict)


class WaterIn(BaseModel):
    """Тело ``POST /api/water``: миллилитры за один стакан/порцию."""

    ml: int = Field(gt=0, le=5000, description="Сколько миллилитров добавить")
    date: str | None = Field(default=None, max_length=10, description="Дата YYYY-MM-DD")


@router.post("/quick")
def quick_entry(
    payload: QuickEntryIn,
    conn: ConnectionDep,
    broker: BrokerDep,
) -> dict[str, Any]:
    """Создаёт запись журнала или измерение; возвращает созданный объект."""
    if payload.type == "metric":
        return _create_metric(conn, broker, payload)
    return _create_entry(conn, broker, payload)


@router.post("/water")
def add_water(
    payload: WaterIn,
    conn: ConnectionDep,
    settings: SettingsDep,
    broker: BrokerDep,
) -> dict[str, Any]:
    """Добавляет воду в метрики за дату и возвращает сумму за сегодня."""
    date_text = (payload.date or settings.today()).strip()
    try:
        with conn:
            add_metric(conn, "water", float(payload.ml), date=date_text, unit="мл", source="api")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    today = settings.today()
    row = conn.execute(
        "SELECT COALESCE(SUM(value), 0) AS total FROM metrics "
        "WHERE kind IN ('water', 'water_ml') AND date = ?",
        (today,),
    ).fetchone()
    today_ml = float(row["total"] or 0.0) if row is not None else 0.0
    goal_ml = max(1, pref_int(conn, "water_goal_ml"))

    broker.publish(
        EVENT_SYNC_UPDATED,
        {
            "reason": "water",
            "ml": payload.ml,
            "today_ml": round(today_ml, 1),
            "goal_ml": goal_ml,
        },
    )
    return {
        "ok": True,
        "date": date_text,
        "ml": payload.ml,
        "today_ml": round(today_ml, 1),
        "goal_ml": goal_ml,
        "percent": round(today_ml / goal_ml * 100.0, 1),
    }


def _create_entry(conn, broker, payload: QuickEntryIn) -> dict[str, Any]:
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Нужен текст записи")
    kind = (payload.kind or ("event" if payload.type == "event" else "thought")).strip()
    try:
        with conn:
            entry_id = add_entry(conn, kind, text, meta=payload.meta, source="api")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    entry = Entry.from_row(row).model_dump()
    broker.publish(EVENT_ENTRY_CREATED, {"entry": entry})
    return {"ok": True, "entry": entry, "metric": None}


def _create_metric(conn, broker, payload: QuickEntryIn) -> dict[str, Any]:
    value = payload.value if payload.value is not None else _number_from_text(payload.text)
    if value is None:
        raise HTTPException(
            status_code=422,
            detail="Для метрики нужно число: передайте value или число в тексте",
        )
    kind = (payload.kind or _guess_metric_kind(payload.text, payload.unit)).strip()
    try:
        with conn:
            metric_id = add_metric(
                conn,
                kind,
                value,
                date=payload.date,
                unit=payload.unit,
                source="api",
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = conn.execute("SELECT * FROM metrics WHERE id = ?", (metric_id,)).fetchone()
    metric = Metric.from_row(row).model_dump()
    broker.publish(EVENT_SYNC_UPDATED, {"reason": "metric", "metric": metric})
    return {"ok": True, "entry": None, "metric": metric}


def _number_from_text(text: str) -> float | None:
    match = _NUMBER_RE.search(text or "")
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _guess_metric_kind(text: str, unit: str) -> str:
    unit_key = (unit or "").strip().lower()
    if unit_key in _UNIT_KINDS:
        return _UNIT_KINDS[unit_key]
    lowered = (text or "").lower()
    if "вес" in lowered or "кг" in lowered:
        return "weight"
    if "вод" in lowered or "мл" in lowered:
        return "water"
    if "сон" in lowered or "спал" in lowered:
        return "sleep"
    if "настроен" in lowered:
        return "mood"
    return "custom"
