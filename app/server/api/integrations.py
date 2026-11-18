"""Вес из внешних источников: честный импорт файла и ручной ввод.

Реальных OAuth-интеграций здесь нет и не притворяемся, что они есть: все
источники — это либо ручная загрузка файла (CSV/JSON), либо ручной ввод в боте
и на экране «Тело». ``integrations`` хранит список возможностей, их состояние
и заметку о том, как пользоваться каждой.

Эндпоинты (camelCase-JSON, ошибки — ``{"detail": "..."}``):

* ``POST /api/health/weight/import`` — JSON ``{source, points:[{date, weight}]}``
  до 2000 точек, вес 30..350 кг. Пишет в ``metrics`` (``kind='weight'``,
  ``source`` импорта) и дедуплицирует по паре ``(date, source)``: повторный
  импорт того же файла ничего не плодит. Возвращает
  ``{imported, skipped, firstDate, lastDate}``;
* ``POST /api/health/weight/import/csv`` — тот же результат для ``text/csv``
  с колонками ``date,weight``. Разделитель — запятая или точка с запятой,
  заголовок опционален (``date``/``дата``, ``weight``/``вес``), десятичная
  запятая в весе допустима при разделителе «;»;
* ``GET /api/integrations`` — список источников (``key, title, kind, enabled,
  lastSync, notes``);
* ``PATCH /api/integrations/{key}`` — включить/выключить приём импорта
  (``enabled``). Выключенный источник отвечает 409 на импорт.

Дедупликация намеренно не обновляет старые строки: если пользователь уже
ввёл вес за день вручную, импорт за этот день пропускается, а не затирает
его. ``lastSync`` обновляется после успешной проверки файла, даже если все
точки оказались дублями, — синхронизация состоялась.
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import date as date_cls
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from app.server.api.deps import ConnectionDep, SettingsDep
from app.server.settings import Settings

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["integrations"])

#: Ключи источников из контракта схемы. ``withings`` разрешён схемой, но не
#: заводится по умолчанию: это честный импорт, а не готовое подключение.
IntegrationKey = Literal["scale", "apple-health", "withings", "manual"]

#: Разрешённые видовые значения интеграции.
IntegrationKind = Literal["import", "manual"]

_MAX_POINTS = 2000
_MIN_WEIGHT = 30.0
_MAX_WEIGHT = 350.0
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEADER_DATE = frozenset({"date", "дата"})
_HEADER_WEIGHT = frozenset({"weight", "вес", "kg", "кг"})


# --------------------------------------------------------------------------
# Модели
# --------------------------------------------------------------------------


class WeightPointIn(BaseModel):
    """Одна точка импорта: ISO-дата и вес в килограммах."""

    model_config = ConfigDict(extra="forbid")

    date: date_cls
    weight: float = Field(ge=_MIN_WEIGHT, le=_MAX_WEIGHT)

    @field_validator("date", mode="before")
    @classmethod
    def _iso_date(cls, value: Any) -> Any:
        """Дата строго ``YYYY-MM-DD``; «17.11.2026» и время — 422."""
        if isinstance(value, str) and not _ISO_DATE_RE.match(value.strip()):
            raise ValueError("дата должна быть в формате YYYY-MM-DD")
        return value.strip() if isinstance(value, str) else value

    @field_validator("weight")
    @classmethod
    def _finite_weight(cls, value: float) -> float:
        number = float(value)
        if not math.isfinite(number) or not _MIN_WEIGHT <= number <= _MAX_WEIGHT:
            raise ValueError("вес должен быть числом от 30 до 350 кг")
        return round(number, 2)


class WeightImportIn(BaseModel):
    """Тело JSON-импорта: источник и до 2000 точек."""

    model_config = ConfigDict(extra="forbid")

    source: IntegrationKey
    points: list[WeightPointIn] = Field(min_length=1, max_length=_MAX_POINTS)


class WeightImportOut(BaseModel):
    """Итог импорта: сколько записей добавлено и какие даты они покрыли."""

    imported: int
    skipped: int
    firstDate: str | None = None
    lastDate: str | None = None


class IntegrationOut(BaseModel):
    """Источник веса в списке интеграций."""

    key: str
    title: str
    kind: IntegrationKind
    enabled: StrictBool
    lastSync: str | None = None
    notes: str = ""


class IntegrationPatch(BaseModel):
    """PATCH интеграции: только включение/выключение приёма импорта."""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


# --------------------------------------------------------------------------
# Помощники
# --------------------------------------------------------------------------


def _integration_json(row: Any) -> IntegrationOut:
    return IntegrationOut(
        key=str(row["key"]),
        title=str(row["title"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        enabled=bool(row["enabled"]),
        lastSync=None if row["last_sync"] is None else str(row["last_sync"]),
        notes=str(row["notes"] or ""),
    )


def _require_integration(conn: Any, source: str) -> Any:
    """Строка интеграции или 422/409: неизвестный источник / выключен."""
    row = conn.execute(
        "SELECT key, title, kind, enabled, last_sync, notes FROM integrations WHERE key = ?",
        (source,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=422, detail="Неизвестный источник импорта: %s" % source)
    if not int(row["enabled"]):
        raise HTTPException(
            status_code=409,
            detail="Интеграция «%s» выключена — включите её в настройках" % row["title"],
        )
    return row


def _store_points(
    conn: Any,
    settings: Settings,
    source: str,
    points: list[WeightPointIn],
) -> WeightImportOut:
    """Пишет точки в ``metrics`` с дедупом по ``(date, source)``.

    Вставка — ``INSERT ... SELECT ... WHERE NOT EXISTS``: проверка и запись
    идут одной инструкцией, поэтому параллельный импорт не создаёт дубль.
    Всё в одной транзакции: файл либо применяется целиком, либо не применяется.
    """
    _require_integration(conn, source)
    imported_dates: list[str] = []
    skipped = 0
    with conn:
        for point in points:
            day = point.date.isoformat()
            cursor = conn.execute(
                "INSERT INTO metrics (date, kind, value, unit, source) "
                "SELECT ?, 'weight', ?, 'кг', ? "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM metrics WHERE kind = 'weight' AND source = ? AND date = ?"
                ")",
                (day, float(point.weight), source, source, day),
            )
            if cursor.rowcount:
                imported_dates.append(day)
            else:
                skipped += 1
        conn.execute(
            "UPDATE integrations SET last_sync = ? WHERE key = ?",
            (settings.now().isoformat(timespec="seconds"), source),
        )
    return WeightImportOut(
        imported=len(imported_dates),
        skipped=skipped,
        firstDate=min(imported_dates) if imported_dates else None,
        lastDate=max(imported_dates) if imported_dates else None,
    )


def _parse_csv(raw: bytes) -> list[WeightPointIn]:
    """Разбирает CSV ``date,weight``; ошибка в любой строке — 422 с номером.

    Заголовок опционален и распознаётся по именам колонок, поэтому файл
    «Дата;Вес» и файл без заголовка одинаково валидны.
    """
    if not raw or not raw.strip():
        raise HTTPException(status_code=422, detail="Пустое тело CSV: нужны строки date,weight")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="CSV должен быть в UTF-8") from None

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=422, detail="Пустое тело CSV: нужны строки date,weight")
    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","

    points: list[WeightPointIn] = []
    header_checked = False
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for line_no, row in enumerate(reader, start=1):
        cells = [cell.strip() for cell in row]
        while cells and cells[-1] == "":
            cells.pop()
        if not cells:
            continue
        if not header_checked:
            header_checked = True
            first = cells[0].lower()
            second = cells[1].lower() if len(cells) > 1 else ""
            if first in _HEADER_DATE or second in _HEADER_WEIGHT:
                continue
        if len(cells) != 2:
            raise HTTPException(
                status_code=422,
                detail="Строка %d: нужны ровно две колонки date,weight" % line_no,
            )
        date_text, weight_text = cells
        if not _ISO_DATE_RE.match(date_text):
            raise HTTPException(
                status_code=422,
                detail="Строка %d: дата должна быть YYYY-MM-DD" % line_no,
            )
        try:
            day = date_cls.fromisoformat(date_text)
        except ValueError:
            raise HTTPException(
                status_code=422, detail="Строка %d: некорректная дата" % line_no
            ) from None
        try:
            weight = float(weight_text.replace(",", "."))
        except ValueError:
            raise HTTPException(
                status_code=422, detail="Строка %d: вес не число" % line_no
            ) from None
        if not math.isfinite(weight) or not _MIN_WEIGHT <= weight <= _MAX_WEIGHT:
            raise HTTPException(
                status_code=422,
                detail="Строка %d: вес должен быть от 30 до 350 кг" % line_no,
            )
        points.append(WeightPointIn(date=day, weight=round(weight, 2)))
        if len(points) > _MAX_POINTS:
            raise HTTPException(
                status_code=422,
                detail="Слишком много точек: максимум %d" % _MAX_POINTS,
            )
    if not points:
        raise HTTPException(status_code=422, detail="В CSV нет ни одной точки веса")
    return points


# --------------------------------------------------------------------------
# Эндпоинты
# --------------------------------------------------------------------------


@router.get("/integrations", response_model=list[IntegrationOut])
def list_integrations(conn: ConnectionDep) -> list[IntegrationOut]:
    """Все источники веса: три базовые записи и включённые/выключенные состояния."""
    rows = conn.execute(
        "SELECT key, title, kind, enabled, last_sync, notes FROM integrations ORDER BY id"
    ).fetchall()
    return [_integration_json(row) for row in rows]


@router.patch("/integrations/{key}", response_model=IntegrationOut)
def patch_integration(key: str, payload: IntegrationPatch, conn: ConnectionDep) -> IntegrationOut:
    """Включает или выключает приём импорта у источника; неизвестный ключ — 404."""
    cursor = conn.execute(
        "UPDATE integrations SET enabled = ? WHERE key = ?",
        (1 if payload.enabled else 0, key),
    )
    if not cursor.rowcount:
        raise HTTPException(status_code=404, detail="Интеграция не найдена: %s" % key)
    conn.commit()
    row = conn.execute(
        "SELECT key, title, kind, enabled, last_sync, notes FROM integrations WHERE key = ?",
        (key,),
    ).fetchone()
    return _integration_json(row)


@router.post("/health/weight/import", response_model=WeightImportOut)
def import_weight(
    payload: WeightImportIn,
    conn: ConnectionDep,
    settings: SettingsDep,
) -> WeightImportOut:
    """Импорт взвешиваний из файла: JSON-точки, дедуп по ``(date, source)``."""
    return _store_points(conn, settings, payload.source, payload.points)


@router.post("/health/weight/import/csv", response_model=WeightImportOut)
async def import_weight_csv(
    request: Request,
    conn: ConnectionDep,
    settings: SettingsDep,
    source: IntegrationKey = Query("scale", description="Источник импорта"),
) -> WeightImportOut:
    """Импорт CSV ``date,weight``; разделитель «,» или «;», заголовок необязателен."""
    raw = await request.body()
    points = _parse_csv(raw)
    return _store_points(conn, settings, source, points)
