"""``GET /api/analytics/correlations`` — корреляции из MASTER_SPEC §2.5.

Пары: сон↔настроение, вода↔настроение, тренировки↔вес. Корреляция считается
только при ``n >= 14`` и ``|r| >= 0.3``; иначе возвращается честный статус
(``insufficient_data`` / ``no_variance`` / ``weak``) и понятный текст, а не
выдуманное число.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.server.api.aggregates import (
    MIN_CORRELATION_DAYS,
    MIN_CORRELATION_R,
    build_correlations,
)
from app.server.api.deps import ConnectionDep, SettingsDep

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["analytics"])


@router.get("/analytics/correlations")
def correlations(
    conn: ConnectionDep,
    settings: SettingsDep,
    days: int = Query(90, ge=MIN_CORRELATION_DAYS, le=730),
) -> dict[str, Any]:
    """Корреляции по дневным рядам за период ``days``."""
    pairs = build_correlations(conn, settings.now().date(), days=days)
    return {
        "days": days,
        "min_days": MIN_CORRELATION_DAYS,
        "min_r": MIN_CORRELATION_R,
        "pairs": pairs,
    }
