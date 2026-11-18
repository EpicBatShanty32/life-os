"""Сборка FastAPI-приложения Life OS v3 (``app/server/main.py``).

Что здесь есть:

* ``create_app()`` — фабрика приложения: настройки, CORS для localhost,
  проверка токена владельца, роутеры API, WebSocket, медиа с Range и раздача
  собранной SPA (``settings.web_dist``) по ``/``;
* lifespan: миграции БД при старте (база v0.x не перезаписывается — см.
  :class:`app.server.db.LegacySchemaError`), привязка брокера событий к loop,
  опциональный запуск воркера очереди и корректная остановка;
* ``app`` — готовый ASGI-объект для ``uvicorn app.server.main:app``.

Опциональные настройки окружения (не секреты):

* ``LIFEOS_API_TOKEN`` — токен владельца. Если задан, ``/api/*`` (кроме
  ``/api/health``) и ``/media/*`` требуют ``X-LifeOS-Token`` или cookie
  ``lifeos_token``; WebSocket принимает ещё ``?token=``. Пусто = без auth
  (сервер слушает localhost);
* ``LIFEOS_WORKER_ENABLED=1`` — запустить воркер очереди. Он стартует только
  если фабрике передали обработчики задач, поэтому серверный процесс не
  перехватывает ``transcribe`` у отдельного worker-процесса.

Импорт модуля ничего не открывает и не создаёт: БД, каталоги и потоки
появляются только в lifespan при старте ASGI-приложения.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.server import usage as usage_stats
from app.server.api import api_routers
from app.server.db import init_db
from app.server.jobs import JobHandler, WorkerHandle, start_worker as start_job_worker
from app.server.media import router as media_router
from app.server.settings import Settings, get_settings
from app.server.ws import Broker
from app.server.ws import router as ws_router

__all__ = ["LOCAL_ORIGIN_REGEX", "app", "create_app"]

logger = logging.getLogger("lifeos.api")

#: CORS разрешён только локальным адресам (любой порт — dev-сервер Vite тоже).
LOCAL_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

_PUBLIC_PATHS = frozenset({"/api/health"})
_PROTECTED_PREFIXES = ("/api", "/media")

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "да"})


def create_app(
    settings: Settings | None = None,
    *,
    start_worker: bool | None = None,
    handlers: Mapping[str, JobHandler] | None = None,
    api_token: str | None = None,
) -> FastAPI:
    """Собирает приложение; ``settings``/``handlers`` можно подменить в тестах.

    ``start_worker=None`` — решает окружение (``LIFEOS_WORKER_ENABLED``),
    причём воркер не стартует без обработчиков.
    """
    resolved = settings if settings is not None else get_settings()
    token = _resolve_api_token(resolved) if api_token is None else str(api_token or "")
    handler_map = dict(handlers or {})
    worker_enabled = _resolve_worker_flag(start_worker) and bool(handler_map)
    broker = Broker()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        resolved.ensure_dirs()
        # Миграции при старте: создаёт схему или поднимает версию, а базу
        # v0.x не трогает (LegacySchemaError с понятным текстом).
        bootstrap = init_db(resolved.db_path)
        bootstrap.close()

        broker.bind_loop(asyncio.get_running_loop())
        worker: WorkerHandle | None = None
        if worker_enabled:
            worker = start_job_worker(resolved, handler_map, broker=broker)
            logger.info("воркер очереди запущен: %s", ", ".join(sorted(handler_map)))
        application.state.worker = worker
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()
            application.state.worker = None
            broker.close()

    application = FastAPI(title="Life OS", version=__version__, lifespan=lifespan)
    application.state.settings = resolved
    application.state.api_token = token
    application.state.broker = broker
    application.state.worker = None
    application.state.worker_enabled = worker_enabled

    # Порядок middleware: auth ставится раньше, CORS — последним, чтобы CORS
    # оказался внешним и отвечал на preflight раньше проверки токена.
    _install_auth(application)
    _install_cache_headers(application)
    _install_usage_tracking(application)
    _install_cors(application)

    for router in api_routers():
        application.include_router(router)
    application.include_router(media_router)
    application.include_router(ws_router)

    _install_web(application, resolved)
    return application


def _resolve_api_token(settings: Settings) -> str:
    env_token = os.environ.get("LIFEOS_API_TOKEN", "").strip()
    if env_token:
        return env_token
    app_config = settings.config.get("app")
    if isinstance(app_config, dict):
        return str(app_config.get("api_token") or "").strip()
    return ""


def _resolve_worker_flag(explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return os.environ.get("LIFEOS_WORKER_ENABLED", "").strip().lower() in _TRUE_VALUES


def _install_auth(application: FastAPI) -> None:
    @application.middleware("http")
    async def require_owner_token(request: Request, call_next):
        token = str(application.state.api_token or "")
        path = request.url.path
        if (
            not token
            or request.method == "OPTIONS"
            or path in _PUBLIC_PATHS
            or not path.startswith(_PROTECTED_PREFIXES)
        ):
            return await call_next(request)

        supplied = (
            request.headers.get("x-lifeos-token")
            or request.cookies.get("lifeos_token")
            or ""
        )
        if supplied and secrets.compare_digest(str(supplied), token):
            return await call_next(request)
        return JSONResponse(
            {"detail": "Требуется токен владельца"},
            status_code=401,
            headers={"WWW-Authenticate": "LifeOS-Token"},
        )


#: Пути оболочки SPA: их нельзя кешировать, иначе после деплоя браузер
#: держит старый index.html и просит уже удалённые файлы сборки.
_NO_CACHE_PATHS = frozenset({"/", "/index.html", "/sw.js", "/manifest.webmanifest", "/offline.html"})


def _install_cache_headers(application: FastAPI) -> None:
    """Разделяет кеширование: оболочка — no-cache, хешированные ассеты — навсегда."""

    @application.middleware("http")
    async def cache_policy(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if request.method == "GET":
            if path in _NO_CACHE_PATHS:
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            elif path.startswith("/assets/"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _install_usage_tracking(application: FastAPI) -> None:
    """Пишет событие использования для успешных ``GET /api/...`` (веб-экраны).

    Статистика не должна влиять на ответ: любые ошибки записи логируются, а
    ответ уходит как обычно. Сама статистика и health-check не считаются.
    """

    @application.middleware("http")
    async def track_usage(request: Request, call_next):
        response = await call_next(request)
        try:
            if (
                request.method == "GET"
                and response.status_code == 200
                and request.url.path.startswith("/api/")
            ):
                target = request.url.path
                if request.url.query:
                    target += "?" + request.url.query
                usage_stats.record_web_request(application.state.settings, target)
        except Exception:  # pragma: no cover - статистика не ломает продукт
            logger.exception("не удалось записать событие использования")
        return response


def _install_cors(application: FastAPI) -> None:
    application.add_middleware(
        CORSMiddleware,
        allow_origin_regex=LOCAL_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "Accept-Ranges",
            "Content-Length",
            "Content-Range",
            "Content-Disposition",
        ],
    )


def _install_web(application: FastAPI, settings: Settings) -> None:
    """Отдаёт собранную SPA; пока ``dist`` нет — короткий JSON на ``/``."""
    index_file = settings.web_dist / "index.html"

    @application.get("/", include_in_schema=False)
    def index() -> Any:
        if index_file.is_file():
            return FileResponse(index_file)
        return JSONResponse(
            {
                "app": settings.app_name,
                "status": "ok",
                "web": "SPA не собрана: %s" % settings.web_dist,
                "api": "/api/health",
                "docs": "/docs",
            }
        )

    if settings.web_dist.is_dir():
        application.mount(
            "/",
            StaticFiles(directory=str(settings.web_dist), html=True),
            name="web",
        )


app = create_app()
