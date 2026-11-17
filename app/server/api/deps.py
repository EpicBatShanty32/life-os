"""Общие зависимости FastAPI-роутеров Life OS v3.

Каждый запрос получает своё подключение к SQLite: так потоковые эндпоинты
FastAPI (sync def в threadpool) и воркер не делят один ``sqlite3.Connection``
между потоками. WAL + ``busy_timeout=5000`` из :func:`app.server.db.connect`
позволяют читать и писать параллельно без «database is locked».
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Iterator

from fastapi import Depends, Request

from app.server.db import connect
from app.server.settings import Settings
from app.server.ws import Broker

__all__ = [
    "BrokerDep",
    "ConnectionDep",
    "SettingsDep",
    "app_settings",
    "db_connection",
    "event_broker",
]


def app_settings(request: Request) -> Settings:
    """Настройки процесса, положенные в ``app.state`` при сборке приложения."""
    return request.app.state.settings


def db_connection(request: Request) -> Iterator[sqlite3.Connection]:
    """Соединение с БД на время запроса (закрывается в ``finally``)."""
    settings: Settings = request.app.state.settings
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def event_broker(request: Request) -> Broker:
    """In-process брокер WebSocket-событий."""
    return request.app.state.broker


SettingsDep = Annotated[Settings, Depends(app_settings)]
ConnectionDep = Annotated[sqlite3.Connection, Depends(db_connection)]
BrokerDep = Annotated[Broker, Depends(event_broker)]
