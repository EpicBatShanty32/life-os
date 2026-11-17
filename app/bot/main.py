"""Точка входа Telegram-бота Life OS v3 (aiogram 3.x, long-polling).

Запуск::

    LIFEOS_DB_PATH=data/lifeos-v3.db python3 -m app.bot.main

Токен берётся из ``config/config.json`` (``telegram.token``) или из env
``LIFEOS_TELEGRAM_TOKEN``; в лог он не попадает. База v3 — отдельный файл:
старую ``data/lifeos.db`` (v0.x) :func:`app.server.db.migrate` намеренно не
открывает, бот завершится с подсказкой задать ``LIFEOS_DB_PATH``.

Локальный Bot API Server подключается через ``LIFEOS_TELEGRAM_API_BASE`` /
``TELEGRAM_API_BASE`` (поле ``Settings.telegram_api_base``): тогда лимит файла
2 ГБ вместо 20 МБ. Импорт модуля не запускает поллинг и не трогает БД —
это важно для проверки ``python3 -c "import app.bot.main"``.
"""

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from app.bot import handlers
from app.server import db
from app.server.settings import ConfigError, Settings, get_settings

logger = logging.getLogger("lifeos.bot")

PUBLIC_API_BASE = "https://api.telegram.org"
DB_HINT = (
    "База Life OS v0.x не открывается в v3. Укажите отдельный файл, например: "
    "LIFEOS_DB_PATH=data/lifeos-v3.db python3 -m app.bot.main"
)


def configure_logging(settings: Settings, level: int = logging.INFO) -> None:
    """Логи в консоль и ``data/logs/bot.log`` (ротация 1 МБ × 3).

    Повторный вызов не дублирует обработчики: полезно в тестах и при
    перезапуске внутри одного процесса.
    """
    root = logging.getLogger()
    if getattr(root, "_lifeos_configured", False):
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(settings.logs_dir / "bot.log"),
            maxBytes=1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        logger.warning("не удалось открыть файл лога в %s — пишу только в консоль", settings.logs_dir)
    root._lifeos_configured = True  # type: ignore[attr-defined]


def build_session(api_base: Optional[str]) -> Optional[AiohttpSession]:
    """Сессия для локального Bot API Server; ``None`` — обычный api.telegram.org."""
    base = str(api_base or "").strip().rstrip("/")
    if not base or base == PUBLIC_API_BASE:
        return None
    logger.info("Bot API: локальный сервер %s (лимит файла 2 ГБ)", base)
    return AiohttpSession(api=TelegramAPIServer.from_base(base))


def build_bot(settings: Settings) -> Bot:
    """Создаёт Bot; токен не логируется и не попадает в repr настроек."""
    if not settings.token:
        raise ConfigError(
            "Не задан токен Telegram: заполните telegram.token в config/config.json "
            "или переменную LIFEOS_TELEGRAM_TOKEN."
        )
    session = build_session(getattr(settings, "telegram_api_base", ""))
    return Bot(
        token=settings.token,
        session=session,
        default=DefaultBotProperties(parse_mode=None),
    )


def open_database(settings: Settings) -> Any:
    """Открывает БД v3; на базе v0.x бросает :class:`db.LegacySchemaError`."""
    conn = db.init_db(settings.db_path)
    logger.info("база: %s (схема v%d)", settings.db_path, db.user_version(conn))
    return conn


async def serve(
    settings: Optional[Settings] = None,
    bot: Optional[Bot] = None,
    dispatcher: Optional[Dispatcher] = None,
) -> int:
    """Поднимает long-polling до SIGTERM/SIGINT; возвращает код выхода."""
    settings = settings or get_settings()
    configure_logging(settings)
    if not settings.token:
        logger.error("не задан токен Telegram — бот не запущен")
        return 2
    if int(settings.allowed_chat_id or 0) == 0:
        logger.warning(
            "telegram.allowed_chat_id = 0: бот не ответит никому. "
            "Укажите свой Telegram ID в конфиге."
        )

    settings.ensure_dirs()
    try:
        conn = open_database(settings)
    except db.LegacySchemaError:
        logger.error(DB_HINT)
        return 2
    except db.SchemaVersionError as exc:
        logger.error("файл БД несовместим с этой версией: %s", exc)
        return 2

    own_bot = bot is None
    bot = bot or build_bot(settings)
    dispatcher = dispatcher or Dispatcher()
    service = handlers.BotService(settings, conn)
    dispatcher.include_router(handlers.build_router(service))

    try:
        await dispatcher.start_polling(
            bot,
            handle_signals=True,
            close_bot_session=False,
        )
    finally:
        conn.close()
        if own_bot:
            try:
                await bot.session.close()
            except Exception:
                logger.debug("не удалось закрыть сессию бота", exc_info=True)
    return 0


def main(argv: Optional[list] = None) -> int:
    """CLI-обёртка; при ошибке конфигурации печатает понятное сообщение."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = get_settings()
    except ConfigError as exc:
        logger.error("конфигурация: %s", exc)
        return 2
    try:
        return asyncio.run(serve(settings=settings))
    except KeyboardInterrupt:
        logger.info("остановка по Ctrl+C")
        return 0
    except ConfigError as exc:
        logger.error("конфигурация: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
