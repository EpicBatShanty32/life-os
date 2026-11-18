"""Точка входа Telegram-бота Life OS v3 (aiogram 3.x, long-polling).

Запуск::

    LIFEOS_DB_PATH=data/lifeos-v3.db python3 -m app.bot.main

или одной командой из корня проекта::

    bash tools/run-bot.sh

Токен берётся из ``config/config.json`` (``telegram.token``) или из env
``LIFEOS_TELEGRAM_TOKEN``; в лог он не попадает. База v3 — отдельный файл:
старую ``data/lifeos.db`` (v0.x) :func:`app.server.db.migrate` намеренно не
открывает, бот завершится с подсказкой задать ``LIFEOS_DB_PATH``.

Локальный Bot API Server подключается через ``LIFEOS_TELEGRAM_API_BASE`` /
``TELEGRAM_API_BASE`` (поле ``Settings.telegram_api_base``): тогда лимит файла
2 ГБ вместо 50 МБ. Импорт модуля не запускает поллинг и не трогает БД —
это важно для проверки ``python3 -c "import app.bot.main"``.

Два экземпляра одновременно не запускаются: ``data/bot.lock`` держит flock,
второй процесс выходит с кодом 3 и понятным сообщением. Это не придирка:
Telegram отдаёт апдейты только одному поллеру, второй получает 409 и
«съедает» часть сообщений.
"""

import asyncio
import fcntl
import logging
import os
import re
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, IO, Optional, Tuple

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import agent as agent_module
from app.bot import backup as backup_module
from app.bot import handlers
from app.server import db
from app.server.settings import ConfigError, Settings, get_settings

logger = logging.getLogger("lifeos.bot")

PUBLIC_API_BASE = "https://api.telegram.org"
#: Ручной запуск (tools/run-bot.sh или nohup) пишет сюда свой pid. По этому
#: файлу служба понимает, что мешает именно ручной экземпляр, и аккуратно
#: меняет его на себя. Экземпляр самой службы так не останавливается никогда.
MANUAL_PID_FILE = "bot-manual.pid"
TAKEOVER_TIMEOUT_SECONDS = 20
DB_HINT = (
    "База Life OS v0.x не открывается в v3. Укажите отдельный файл, например: "
    "LIFEOS_DB_PATH=data/lifeos-v3.db python3 -m app.bot.main"
)
#: Как часто цикл напоминаний проверяет расписание (секунды).
REMINDER_TICK_S = 30.0
#: Переменная окружения для тестов и живого прогона «со сдвигом времени».
REMINDER_TICK_ENV = "LIFEOS_REMINDER_TICK"

TOKEN_HINT = (
    "Не задан токен Telegram — бот не запущен. Впишите токен от @BotFather в "
    "config/config.json → telegram.token (или задайте LIFEOS_TELEGRAM_TOKEN) "
    "и запустите снова: bash tools/run-bot.sh"
)
TOKEN_REJECTED_HINT = (
    "Telegram не принял токен (401 Unauthorized) — бот не запущен. Проверьте "
    "telegram.token в config/config.json: токен отозван или скопирован с ошибкой. "
    "Новый токен выдаёт @BotFather."
)
OWNER_HINT = (
    "telegram.allowed_chat_id = 0: бот не ответит никому. Впишите свой Telegram ID "
    "(число) в config/config.json → telegram.allowed_chat_id и перезапустите бота."
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
    # main() уже вызвал basicConfig и дал консольный обработчик. Второй такой
    # же печатал бы каждую строку дважды — добавляем только если его нет.
    if not _has_console_handler(root):
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


def _has_console_handler(root: logging.Logger) -> bool:
    """Есть ли у логгера обработчик, пишущий в консоль (не в файл)."""
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler):
            continue
        if isinstance(handler, logging.StreamHandler):
            return True
    return False


def _process_state(pid: int) -> str:
    """Состояние процесса из ``ps`` (``Z`` — зомби, уже завершился)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Завершившийся, но ещё не «похороненный» родителем процесс (зомби) для
    # нас мёртв: замок он уже отпустил, место свободно.
    return not _process_state(pid).startswith("Z")


def _process_command(pid: int) -> str:
    """Командная строка процесса (для ответа «а это точно наш бот?»)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _looks_like_bot_process(pid: int) -> bool:
    command = _process_command(pid)
    return "app.bot.main" in command or "app/bot/main.py" in command


def _manual_pid(settings: Settings) -> int:
    """pid ручного экземпляра из ``data/bot-manual.pid`` (0 — файла нет).

    Терпим и формат ``pid=123``: так файл пишут вручную из терминала.
    """
    try:
        raw = (settings.data_dir / MANUAL_PID_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    match = re.search(r"(\d+)", raw)
    return int(match.group(1)) if match else 0


def _service_pid() -> int:
    """pid процесса, которым сейчас управляет launchd-служба com.lifeos.bot.

    0 — служба не запущена. Нужен, чтобы служба никогда не приняла собственный
    процесс за «ручной» и не устроила перезапуск по кругу с ошибкой 409.
    """
    try:
        uid = os.getuid()
        result = subprocess.run(
            ["launchctl", "print", "gui/%d/com.lifeos.bot" % uid],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    match = re.search(r"^\s*pid = (\d+)", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else 0


def _takeover_manual_instance(settings: Settings, competing_pid: str) -> bool:
    """Вежливо освободить место от ручного экземпляра бота.

    Возвращает True, только если конкурент — живой процесс ``app.bot.main``,
    который НЕ является процессом нашей службы. Такой экземпляр мог быть
    запущен из терминала (tools/run-bot.sh или nohup) — служба аккуратно
    останавливает его и занимает место. Собственный процесс службы не трогаем
    никогда: иначе launchd и бот будут «моргать» с ошибкой 409.
    """
    try:
        pid = int(str(competing_pid or "").strip())
    except (TypeError, ValueError):
        return False
    if not _process_alive(pid) or not _looks_like_bot_process(pid):
        return False
    service_pid = _service_pid()
    if service_pid and pid == service_pid:
        logger.error(
            "экземпляр бота уже под управлением службы com.lifeos.bot (pid %d) — "
            "второй не запускаю.",
            pid,
        )
        return False
    manual_pid = _manual_pid(settings)
    if manual_pid and manual_pid != pid:
        logger.warning(
            "в %s записан pid %d, а замок держит %d — считаю запись устаревшей",
            settings.data_dir / MANUAL_PID_FILE,
            manual_pid,
            pid,
        )
    logger.warning("служба аккуратно останавливает ручной экземпляр бота (pid %d)", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        logger.error("не удалось отправить SIGTERM процессу %d: %s", pid, exc)
        return False
    deadline = time.monotonic() + TAKEOVER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            try:
                (settings.data_dir / MANUAL_PID_FILE).unlink()
            except OSError:
                pass
            logger.info("ручной экземпляр остановлен — служба займёт его место")
            return True
        time.sleep(0.25)
    logger.error(
        "ручной экземпляр (pid %d) не остановился за %d секунд",
        pid,
        TAKEOVER_TIMEOUT_SECONDS,
    )
    return False


def acquire_instance_lock(lock_path: Path) -> Tuple[Optional[IO[str]], str]:
    """Занять ``bot.lock``; вернуть ``(файл, "")`` или ``(None, чужой pid)``.

    flock снимается ядром при завершении процесса, поэтому «вечных» замков не
    бывает: если файл остался от упавшего бота, новый запуск его переиспользует.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        other_pid = (handle.read() or "").strip()
        handle.close()
        return None, other_pid
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle, ""


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


def reminder_interval() -> float:
    """Период проверки расписания: по умолчанию 30 с, из окружения — для тестов."""
    raw = os.environ.get(REMINDER_TICK_ENV, "").strip()
    if not raw:
        return REMINDER_TICK_S
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        logger.warning("%s=%r — не число, беру %g с", REMINDER_TICK_ENV, raw, REMINDER_TICK_S)
        return REMINDER_TICK_S
    return max(5.0, min(300.0, value))


def reminder_markup(buttons: Any) -> Optional[InlineKeyboardMarkup]:
    """Кнопки напоминания (список рядов «подпись → callback») в разметку Telegram."""
    rows = []
    for row in buttons or []:
        line = []
        for label, data in row:
            text = str(label or "").strip()
            payload = str(data or "")
            if not text or not payload or len(payload.encode("utf-8")) > 64:
                continue
            line.append(InlineKeyboardButton(text=text, callback_data=payload))
        if line:
            rows.append(line)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _reminders_loop(bot: Bot, service: handlers.BotService) -> None:
    """Фоновая петля напоминаний: проверяет расписание и отправляет в Telegram.

    Живёт рядом с поллингом в том же event loop. Работа с SQLite уходит в
    рабочий поток (``asyncio.to_thread``), поэтому проверка расписания не
    задерживает обработку сообщений владельца.
    """
    interval = reminder_interval()
    while True:
        try:
            outgoing = await asyncio.to_thread(service.reminders.tick)
        except asyncio.CancelledError:  # pragma: no cover - остановка службы
            raise
        except Exception:
            logger.exception("напоминания: проверка расписания упала — продолжаю")
            outgoing = []
        for item in outgoing:
            try:
                sent = await bot.send_message(
                    item.chat_id,
                    item.text,
                    reply_markup=reminder_markup(item.buttons),
                )
            except Exception:
                logger.exception("напоминания: не удалось отправить (%s)", item.kind)
                try:
                    await asyncio.to_thread(
                        service.reminders.mark_failed, item.event_id
                    )
                except Exception:
                    logger.exception("напоминания: не удалось отметить сбой отправки")
                continue
            message_id = int(getattr(sent, "message_id", 0) or 0)
            try:
                await asyncio.to_thread(
                    service.reminders.mark_sent, item.event_id, message_id
                )
            except Exception:
                logger.exception("напоминания: не удалось отметить отправку")
            logger.info(
                "напоминание отправлено: %s (event %d, message_id %s)",
                item.kind,
                item.event_id,
                message_id,
            )
            try:
                service.record_usage(item.kind, "reminder", {"event": item.event_id})
            except Exception:  # pragma: no cover - статистика не ломает цикл
                logger.debug("напоминания: статистика не записалась", exc_info=True)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:  # pragma: no cover - остановка службы
            raise


async def serve(
    settings: Optional[Settings] = None,
    bot: Optional[Bot] = None,
    dispatcher: Optional[Dispatcher] = None,
) -> int:
    """Поднимает long-polling до SIGTERM/SIGINT; возвращает код выхода."""
    settings = settings or get_settings()
    configure_logging(settings)
    if not settings.token:
        logger.error(TOKEN_HINT)
        return 2
    if int(settings.allowed_chat_id or 0) == 0:
        logger.warning(OWNER_HINT)

    settings.ensure_dirs()
    lock_path = settings.data_dir / "bot.lock"
    lock_handle, other_pid = acquire_instance_lock(lock_path)
    if lock_handle is None and _takeover_manual_instance(settings, other_pid):
        # Ручной экземпляр получил SIGTERM; забираем замок, как только он выйдет.
        for _ in range(TAKEOVER_TIMEOUT_SECONDS * 4):
            lock_handle, other_pid = acquire_instance_lock(lock_path)
            if lock_handle is not None:
                break
            time.sleep(0.25)
    if lock_handle is None:
        logger.error(
            "бот уже запущен (pid %s) — второй экземпляр не запускаю: Telegram отдаёт "
            "сообщения только одному поллеру, второй получит ошибку 409 и потеряет апдейты. "
            "Остановите его: launchctl bootout gui/$(id -u)/com.lifeos.bot или pkill -f app.bot.main",
            other_pid or "неизвестен",
        )
        return 3

    try:
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
        # Агент с инструментами — основная голова бота. Без ключа LLM он
        # неактивен, и сообщения разбирает прежний роутер правилами.
        agent = agent_module.build_agent(settings)
        if agent.available():
            logger.info("агент включён: %d инструментов", len(agent_module.TOOLS))
        else:
            logger.info("агент выключен (нет ключа LLM) — работают правила")
        service = handlers.BotService(settings, conn, agent=agent)
        # /backup идёт первым: общий роутер перехватывает любой текст и ответил
        # бы подсказкой вместо бэкапа.
        dispatcher.include_router(backup_module.build_router(settings, service))
        dispatcher.include_router(handlers.build_router(service))

        # Копия при каждом старте (но не чаще раза в 30 минут) — в отдельном
        # потоке, чтобы не задерживать поллинг. Сбой бэкапа бота не останавливает.
        await asyncio.to_thread(backup_module.startup_backup, settings)

        # Напоминания и ритуалы: бот сам начинает разговор по расписанию.
        reminder_task: Optional[asyncio.Task] = None
        try:
            schedule = service.reminders.load_settings()
            logger.info(
                "напоминания: %s, утро %s, итог дня %s, привычки %s, тихие часы %s",
                "включены" if schedule.enabled else "выключены",
                schedule.morning.strftime("%H:%M"),
                schedule.evening.strftime("%H:%M"),
                schedule.habits.strftime("%H:%M"),
                schedule.quiet_label(),
            )
        except Exception:
            logger.exception("напоминания: не удалось прочитать расписание")
        reminder_task = asyncio.create_task(
            _reminders_loop(bot, service), name="lifeos-reminders"
        )

        try:
            await dispatcher.start_polling(
                bot,
                handle_signals=True,
                close_bot_session=False,
            )
        except TelegramUnauthorizedError:
            logger.error(TOKEN_REJECTED_HINT)
            return 2
        finally:
            if reminder_task is not None:
                reminder_task.cancel()
                try:
                    await reminder_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # pragma: no cover - остановка уже после сбоя
                    logger.exception("напоминания: петля завершилась с ошибкой")
            conn.close()
            if own_bot:
                try:
                    await bot.session.close()
                except Exception:
                    logger.debug("не удалось закрыть сессию бота", exc_info=True)
        return 0
    finally:
        lock_handle.close()


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
