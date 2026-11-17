"""WebSocket-хаб Life OS v3: in-process брокер событий и эндпоинт ``/ws``.

События (контракт для SPA):

* ``entry.created`` — появилась запись журнала или метрика (payload: ``entry``
  или ``metric``);
* ``job.progress`` — воркер двигает задачу (``job_id``, ``status``, ``progress``,
  ``message``);
* ``sync.updated`` — изменились данные, влияющие на обзор (вода, настройки).

Брокер живёт в памяти процесса: API и воркер публикуют события из разных
потоков, поэтому доставка идёт через ``loop.call_soon_threadsafe``. Очередь
каждого подписчика ограничена: медленный клиент теряет самые старые события,
но не блокирует сервер. При обрыве соединения подписка снимается в ``finally``
эндпоинта — утечек подписчиков нет.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

__all__ = [
    "EVENT_ENTRY_CREATED",
    "EVENT_JOB_PROGRESS",
    "EVENT_SYNC_UPDATED",
    "Broker",
    "router",
]

logger = logging.getLogger("lifeos.ws")

EVENT_ENTRY_CREATED = "entry.created"
EVENT_JOB_PROGRESS = "job.progress"
EVENT_SYNC_UPDATED = "sync.updated"

_MAX_QUEUE = 200


class Broker:
    """Простой in-process pub/sub для WebSocket-клиентов."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Запоминает loop приложения (вызывается в lifespan)."""
        with self._lock:
            self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_QUEUE)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event_type: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Публикует событие; безопасно из любого потока и без подписчиков."""
        event: dict[str, Any] = dict(payload or {})
        event["type"] = event_type
        event["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        loop = self._loop
        if loop is None or loop.is_closed():
            return event
        try:
            loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:  # loop уже закрывается при остановке приложения
            logger.debug("брокер: loop закрыт, событие %s пропущено", event_type)
        return event

    def close(self) -> None:
        """Снимает всех подписчиков (вызывается при остановке приложения)."""
        with self._lock:
            self._subscribers.clear()
            self._loop = None

    def _deliver(self, event: dict[str, Any]) -> None:
        with self._lock:
            queues = list(self._subscribers)
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - защита от гонки
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - защита от гонки
                logger.debug("брокер: очередь подписчика переполнена")


router = APIRouter()


@router.websocket("/ws")
async def websocket_events(websocket: WebSocket) -> None:
    """Отдаёт клиенту поток событий; токен проверяется, если он настроен."""
    token = str(getattr(websocket.app.state, "api_token", "") or "")
    if token and not _authorized(websocket, token):
        await websocket.close(code=1008, reason="Требуется токен владельца")
        return
    broker: Broker | None = getattr(websocket.app.state, "broker", None)
    if broker is None:
        await websocket.close(code=1011, reason="Брокер событий не запущен")
        return

    await websocket.accept()
    queue = broker.subscribe()
    try:
        await _pump(websocket, queue)
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # Клиент мог закрыться между событиями — это штатное завершение.
        pass
    except asyncio.CancelledError:
        # Клиент закрыл соединение (или приложение останавливается): отмена —
        # штатный путь завершения WS-эндпоинта, а не ошибка. Пробрасывать её
        # наружу нельзя: ASGI-сервер воспримет это как падение обработчика.
        return
    finally:
        broker.unsubscribe(queue)


def _authorized(websocket: WebSocket, token: str) -> bool:
    supplied = (
        websocket.headers.get("x-lifeos-token")
        or websocket.cookies.get("lifeos_token")
        or websocket.query_params.get("token")
        or ""
    )
    return bool(supplied) and secrets.compare_digest(str(supplied), token)


async def _pump(websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Пересылает события из очереди в сокет, замечая обрыв соединения."""
    receiver = asyncio.create_task(websocket.receive())
    try:
        while True:
            getter = asyncio.create_task(queue.get())
            done, _pending = await asyncio.wait(
                {getter, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if getter in done:
                await websocket.send_json(getter.result())
            else:
                getter.cancel()
                await asyncio.gather(getter, return_exceptions=True)
            if receiver in done:
                message = receiver.result()
                if message.get("type") == "websocket.disconnect":
                    return
                receiver = asyncio.create_task(websocket.receive())
    finally:
        if not receiver.done():
            receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
