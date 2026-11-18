"""``GET /api/analytics/usage`` — статистика использования Life OS (§12 v4.1).

Один ответ на экран «Статистика»: сколько событий и активных дней, какие
каналы (веб/бот) и экраны работают, покрытие функций, тепловая карта
«день недели × час», тренд относительно прошлого окна и блок «Мысли» — частые
темы журнала, что растёт и что появилось впервые.

События пишутся не здесь: веб — middleware в :mod:`app.server.main`,
бот — :mod:`app.bot.handlers`, оба через
:func:`app.server.usage.record_event` в таблицу ``usage_events``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.server.api.deps import ConnectionDep, SettingsDep
from app.server.usage import collect_usage

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["analytics"])

DEFAULT_DAYS = 30
DEFAULT_THOUGHTS_DAYS = 90


@router.get("/analytics/usage")
def analytics_usage(
    conn: ConnectionDep,
    settings: SettingsDep,
    days: int = Query(DEFAULT_DAYS, ge=1, le=365, description="Окно статистики"),
    thoughtsDays: int = Query(  # noqa: N815 - контракт API в camelCase
        DEFAULT_THOUGHTS_DAYS, ge=7, le=730, description="Окно тем мыслей"
    ),
) -> dict[str, Any]:
    """Сводка использования: каналы, экраны, покрытие, карта и темы мыслей."""
    return collect_usage(
        conn,
        settings.now().date(),
        days=days,
        thoughts_days=thoughtsDays,
    )
