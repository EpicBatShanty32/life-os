"""``GET /api/analytics/findings`` — находки о связях вместо порога (§12.6 v2).

Возвращает готовый к показу результат: окно, сколько гипотез реально
протестировано, top-5 находок (прошли FDR) и честные отказы (мало дней, нет
разброса, не прошло FDR). Тяжёлая статистика живёт в
:func:`app.server.api.aggregates.build_correlation_report`.

У каждой находки есть ``title`` — короткая нейтральная формулировка без
стрелок-причин («Сон и тренировки идут вместе», «Сон вчера, энергия
сегодня»), и ``leads`` — какой ряд идёт первым при ``lag > 0``. Эти же поля
попадают в Обзор, поэтому экраны не могут сформулировать связь по-разному.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.server.api.aggregates import MIN_CORRELATION_DAYS, build_correlation_report
from app.server.api.deps import ConnectionDep, SettingsDep

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["analytics"])


@router.get("/analytics/findings")
def findings(
    conn: ConnectionDep,
    settings: SettingsDep,
    days: int = Query(90, ge=MIN_CORRELATION_DAYS, le=730, description="Окно в днях"),
    lags: int = Query(2, ge=0, le=7, description="Максимальный лаг в днях"),
) -> dict[str, Any]:
    """Находки: пары × лаги 0..lags, FDR 10 %, top-5 по |r|."""
    report = build_correlation_report(
        conn, settings.now().date(), days=days, lags=lags
    )
    return {
        "window": report["window"],
        "tested": report["tested"],
        "found": report["found"],
        "notReady": report["notReady"],
    }
