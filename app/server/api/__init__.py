"""HTTP-роутеры Life OS v3.

Каждый модуль — отдельный ``APIRouter``; :func:`api_routers` возвращает их в
порядке подключения в :mod:`app.server.main`. Аутентификация и CORS живут в
``main.py`` (middleware), а не в роутерах.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.server.api import analytics, analytics_summary, compass, courses, export, finance, health, journal, library, overview, personal, quick, search, sections, tasks
from app.server.api import settings as settings_api

__all__ = ["api_routers"]


def api_routers() -> tuple[APIRouter, ...]:
    """Роутеры API в порядке регистрации (специфичные — раньше общих)."""
    return (
        health.router,
        overview.router,
        journal.router,
        quick.router,
        courses.router,
        library.router,
        finance.router,
        compass.router,
        tasks.router,
        personal.router,
        analytics_summary.router,
        search.router,
        export.router,
        sections.router,
        analytics.router,
        settings_api.router,
    )
