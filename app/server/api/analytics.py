"""Аналитика связей: ``/api/analytics/correlations`` и находки ``findings``.

``/api/analytics/correlations`` сохраняет прежний путь и прежние поля пар
(``left``, ``right``, ``label``, ``r``, ``n``, ``status``, ``text``) для старых
экранов, но своего порогового расчёта больше не ведёт: ``pairs`` — это срез
того же отчёта v2 на ``lag=0``. Ответ дополнительно отдаёт окно, число
протестированных гипотез, top-5 находок после FDR и честные ``notReady``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.server.api.aggregates import (
    MIN_CORRELATION_DAYS,
    MIN_CORRELATION_R,
    build_correlation_report,
    legacy_pairs_from_report,
)
from app.server.api.deps import ConnectionDep, SettingsDep

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["analytics"])

#: Лаги по умолчанию для этого экрана (0, 1, 2 дня).
DEFAULT_LAGS = 2


@router.get("/analytics/correlations")
def correlations(
    conn: ConnectionDep,
    settings: SettingsDep,
    days: int = Query(90, ge=MIN_CORRELATION_DAYS, le=730),
    lags: int = Query(DEFAULT_LAGS, ge=0, le=7),
) -> dict[str, Any]:
    """Связи за период ``days``: legacy-пары (срез v2) плюс находки v2."""
    today = settings.now().date()
    report = build_correlation_report(conn, today, days=days, lags=lags)
    pairs = legacy_pairs_from_report(report)
    return {
        "days": days,
        "min_days": MIN_CORRELATION_DAYS,
        "min_r": MIN_CORRELATION_R,
        "window": report["window"],
        "tested": report["tested"],
        "found": report["found"],
        "notReady": report["notReady"],
        "pairs": pairs,
    }
