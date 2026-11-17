"""``GET/PUT /api/settings`` — настройки сервера и предпочтения пользователя.

GET отдаёт только безопасные поля (``Settings.safe_dict()``): токен Telegram
и ключ LLM заменены флагами ``token_set`` / ``api_key_set``. PUT меняет лишь
белый список предпочтений (тема, цели по воде/весу/сну, тренировки) — они
лежат в ``settings_kv`` с префиксом ``pref.`` и переживают перезапуск.
Неизвестные поля тела отвергаются 422, чтобы опечатка не терялась молча.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app import __version__
from app.server.api.aggregates import read_preferences, write_preferences
from app.server.api.deps import ConnectionDep, SettingsDep

__all__ = ["PreferencesIn", "router"]

router = APIRouter(prefix="/api", tags=["settings"])


class PreferencesIn(BaseModel):
    """Белый список изменяемых предпочтений; ``None`` — «не менять»."""

    model_config = ConfigDict(extra="forbid")

    theme: Literal["light", "dark", "system"] | None = None
    water_goal_ml: int | None = Field(default=None, ge=500, le=10000)
    weight_goal_kg: float | None = Field(default=None, ge=30, le=300)
    sleep_goal_h: float | None = Field(default=None, ge=4, le=12)
    workouts_per_week: int | None = Field(default=None, ge=1, le=14)


@router.get("/settings")
def get_settings(conn: ConnectionDep, settings: SettingsDep) -> dict[str, Any]:
    """Безопасный снимок настроек + текущие предпочтения."""
    return _settings_payload(conn, settings)


@router.put("/settings")
def update_settings(
    payload: PreferencesIn,
    conn: ConnectionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Обновляет переданные предпочтения и возвращает новый снимок."""
    values = payload.model_dump(exclude_none=True)
    if values:
        with conn:
            write_preferences(conn, values)
    result = _settings_payload(conn, settings)
    return result


def _settings_payload(conn, settings) -> dict[str, Any]:
    safe = settings.safe_dict()
    return {
        "app": safe["app"],
        "paths": safe["paths"],
        "vault": safe["vault"],
        "telegram": safe["telegram"],
        "llm": safe["llm"],
        "preferences": read_preferences(conn),
        "version": __version__,
    }
