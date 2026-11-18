"""Практики и очки: каталог, сессии плеера, баланс, уровни и серии.

Дизайн — ``studio/PRACTICES.md``, постановка — задача t33. Логика начисления
живёт в :mod:`app.server.practices` (её же зовёт бот и сидер), а роутер
отвечает только за HTTP: валидация входа на границе, камелКейс-JSON, коды
4xx в формате ``{"detail": "..."}``.

Эндпоинты:

* ``GET  /api/practices?includeArchived=false`` — каталог с личными цифрами
  (сколько раз, серия дней, рекорд задержки, очки практики) плюс состояние
  очков для шапки экрана: баланс, уровень, серия, кап дня;
* ``GET  /api/practices/{slug}`` — карточка практики: шаги, уровни
  (``defaultLevel`` — что включать по умолчанию), ``safetyNote`` и последние
  10 сессий; неизвестный слаг — 404;
* ``POST /api/practice-sessions`` — сессия из плеера: ``{slug, roundsDone,
  bestHoldS, source, note?, startedAt?, finishedAt?}`` → ``{pointsEarned,
  total, streak, level, recordUpdated, awards[], session, cap}``. Одна сессия
  на практику в день: повтор (ретрай, двойной тап) обновляет максимумы и
  очков второй раз не даёт;
* ``GET  /api/points?days=30`` — баланс, уровень, серия, история по дням
  (с разбивкой по видам), итоги по видам и последние начисления;
* ``POST /api/points/award`` — начисление изнутри (бот, веб-действия):
  ``{kind, refId, points?, reason?, source?}``, идемпотентно по ``refId``;
* ``GET/PUT /api/points/levels`` — пороги уровней (Новичок / Устойчивый /
  В форме / Мастер, по умолчанию 0/1000/5000/15000) в ``settings_kv``.

Что важно знать снаружи:

* **сумма очков всегда считается из журнала** (``points_ledger``), поэтому
  ``total``/``balance`` — это ``SUM(points)``, а не отдельный счётчик;
* **кап дня — 60 очков**: ``cap`` в ответах показывает, сколько уже начислено
  и сколько осталось; частичное начисление возвращается как ``capped: true``;
* **``pointsEarned`` 0 — это нормально**: повторная сессия за день, уже
  начисленный ``refId`` или исчерпанный кап дня. ``duplicate`` отличает
  повтор от капа — UI говорит разное («уже учтено» против «на сегодня хватит»);
* каталог практик заводится лениво при первом обращении, если база пустая:
  свежая установка сразу имеет что показать в плеере.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.server.api.deps import ConnectionDep, SettingsDep
from app.server.practices import (
    DAY_CAP,
    DEFAULT_LEVELS,
    LEDGER_KINDS,
    SESSION_SOURCES,
    award,
    ensure_practice_catalog,
    get_practice,
    level_for,
    levels_with_default,
    list_practices,
    load_levels,
    points_summary,
    save_levels,
    save_practice_session,
    total_points,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["practices"])

SessionSource = Literal["web", "telegram"]
LedgerKind = Literal["practice", "record", "streak", "habit", "task", "checkin", "journal"]
PointsSource = Literal["app", "demo"]


# --------------------------------------------------------------------------
# Схемы входа
# --------------------------------------------------------------------------


class PracticeSessionIn(BaseModel):
    """Тело ``POST /api/practice-sessions`` — итог прохождения практики."""

    model_config = ConfigDict(extra="ignore")

    slug: str = Field(..., min_length=1, max_length=60)
    roundsDone: int = Field(0, ge=0, le=100)  # noqa: N815 - контракт API в camelCase
    bestHoldS: int = Field(0, ge=0, le=3600)  # noqa: N815 - контракт API в camelCase
    source: SessionSource = "web"
    note: str = Field("", max_length=500)
    startedAt: str = Field("", max_length=40)  # noqa: N815 - контракт API в camelCase
    finishedAt: str = Field("", max_length=40)  # noqa: N815 - контракт API в camelCase


class AwardIn(BaseModel):
    """Тело ``POST /api/points/award`` — начисление изнутри приложения."""

    model_config = ConfigDict(extra="ignore")

    kind: LedgerKind
    refId: str = Field(..., min_length=1, max_length=120)  # noqa: N815 - camelCase JSON
    points: int | None = Field(None, ge=0, le=DAY_CAP)
    reason: str = Field("", max_length=200)
    source: PointsSource = "app"


class LevelIn(BaseModel):
    """Один порог уровня в ``PUT /api/points/levels``."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=40)
    title: str = Field(..., min_length=1, max_length=60)
    min: int = Field(..., ge=0, le=10_000_000)


class LevelsIn(BaseModel):
    """Тело ``PUT /api/points/levels`` — полный список порогов."""

    model_config = ConfigDict(extra="forbid")

    levels: list[LevelIn] = Field(..., min_length=2, max_length=8)


# --------------------------------------------------------------------------
# Помощники
# --------------------------------------------------------------------------


def _ensure_catalog(conn: Any) -> None:
    """Заводит стартовый каталог, если таблица практик пуста.

    Пустая таблица — это свежая установка (или база, где практики ещё не
    заводили). Проверка дешёвая: один ``SELECT COUNT(*)``, запись — только
    когда каталога действительно нет.
    """
    row = conn.execute("SELECT COUNT(*) FROM practices").fetchone()
    if int(row[0] or 0) == 0:
        with conn:
            ensure_practice_catalog(conn)


def _points_header(conn: Any, day: Any) -> dict[str, Any]:
    """Короткий блок очков для шапки экрана: баланс, уровень, сегодня, кап."""
    total = total_points(conn)
    levels = load_levels(conn)
    summary = points_summary(conn, day, days=1, recent=0)
    return {
        "balance": total,
        "level": level_for(total, levels),
        "streak": summary["streak"],
        "cap": summary["cap"],
    }


def _require_practice(conn: Any, slug: str, day: Any) -> dict[str, Any]:
    _ensure_catalog(conn)
    practice = get_practice(conn, slug, today=day)
    if practice is None:
        raise HTTPException(status_code=404, detail="Практика %r не найдена" % slug)
    return practice


# --------------------------------------------------------------------------
# Каталог
# --------------------------------------------------------------------------


@router.get("/practices")
def list_practice_catalog(
    conn: ConnectionDep,
    settings: SettingsDep,
    includeArchived: bool = Query(  # noqa: N815 - контракт API в camelCase
        False, description="Показать архивные практики"
    ),
) -> dict[str, Any]:
    """Каталог практик с личными цифрами и состоянием очков."""
    day = settings.now()
    _ensure_catalog(conn)
    practices = list_practices(conn, today=day, include_archived=includeArchived)
    return {
        "practices": practices,
        "count": len(practices),
        "points": _points_header(conn, day),
        "dayCap": DAY_CAP,
    }


@router.get("/practices/{slug}")
def practice_detail(
    slug: str, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Карточка практики: шаги, уровни, безопасность и последние сессии."""
    day = settings.now()
    practice = _require_practice(conn, slug, day)
    return {
        "practice": practice,
        "points": _points_header(conn, day),
        "dayCap": DAY_CAP,
    }


# --------------------------------------------------------------------------
# Сессии
# --------------------------------------------------------------------------


@router.post("/practice-sessions", status_code=201)
def create_practice_session(
    payload: PracticeSessionIn, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Сохраняет сессию, начисляет очки и возвращает итог для плеера."""
    _ensure_catalog(conn)
    try:
        with conn:
            result = save_practice_session(
                conn,
                slug=payload.slug,
                rounds_done=payload.roundsDone,
                best_hold_s=payload.bestHoldS,
                source=payload.source,
                note=payload.note,
                started_ts=payload.startedAt,
                finished_ts=payload.finishedAt,
                now=settings.now(),
            )
    except KeyError:
        raise HTTPException(
            status_code=404, detail="Практика %r не найдена" % payload.slug
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


# --------------------------------------------------------------------------
# Очки
# --------------------------------------------------------------------------


@router.get("/points")
def get_points(
    conn: ConnectionDep,
    settings: SettingsDep,
    days: int = Query(30, ge=1, le=365, description="Окно истории по дням"),
    recent: int = Query(10, ge=0, le=50, description="Сколько последних начислений"),
) -> dict[str, Any]:
    """Баланс, уровень, серия, история по дням и последние начисления."""
    return points_summary(conn, settings.now(), days=days, recent=recent)


@router.post("/points/award")
def award_points(
    payload: AwardIn, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Начисляет очки один раз на ``refId`` (бот и веб-действия изнутри)."""
    try:
        with conn:
            result = award(
                conn,
                kind=payload.kind,
                ref_id=payload.refId,
                points=payload.points,
                reason=payload.reason,
                source=payload.source,
                now=settings.now(),
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    total = total_points(conn)
    result["total"] = total
    result["level"] = level_for(total, load_levels(conn))
    result["cap"] = {
        "day": DAY_CAP,
        "today": result["dayPoints"],
        "remaining": result["remaining"],
    }
    return result


@router.get("/points/levels")
def get_levels(conn: ConnectionDep, settings: SettingsDep) -> dict[str, Any]:
    """Пороги уровней и текущий уровень по балансу."""
    levels = load_levels(conn)
    total = total_points(conn)
    return {
        "levels": levels_with_default(levels),
        "defaults": [dict(level) for level in DEFAULT_LEVELS],
        "level": level_for(total, levels),
        "total": total,
    }


@router.put("/points/levels")
def put_levels(
    payload: LevelsIn, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Меняет пороги уровней (первый должен начинаться с 0)."""
    try:
        with conn:
            levels = save_levels(conn, [item.model_dump() for item in payload.levels])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    total = total_points(conn)
    return {
        "levels": levels_with_default(levels),
        "level": level_for(total, levels),
        "total": total,
    }


#: Полный список видов начислений — по нему бот и веб сверяют ``kind``.
LEDGER_KIND_CHOICES: tuple[str, ...] = LEDGER_KINDS
SESSION_SOURCE_CHOICES: tuple[str, ...] = SESSION_SOURCES
