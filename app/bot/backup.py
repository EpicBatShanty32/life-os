"""Резервные копии базы Life OS и команда ``/backup`` в Telegram.

Зачем: бот — основной вход, база живёт в одном файле SQLite. Копию надо уметь
снять, не останавливая бота, проверить и отправить владельцу прямо в чат.

Что здесь есть:

* :func:`create_backup` — горячая копия через встроенный в SQLite backup API
  (``sqlite3.Connection.backup``): бот продолжает писать, транзакции не рвутся;
* :func:`rotate_backups` — хранит 14 последних копий плюс по одной самой
  свежей за каждый более старый месяц;
* :func:`integrity_check` — ``PRAGMA integrity_check`` по копии;
* :func:`startup_backup` — копия при старте бота (не чаще раза в 30 минут,
  чтобы перезапуски не забивали архив);
* команда ``/backup`` (:func:`handle_backup_command` + :func:`build_router`):
  снимает копию, проверяет размер и отправляет файл документом в чат владельца.

Имена файлов: ``data/backups/YYYY-MM-DD-HH.db`` — одна копия на час, как и у
``tools/backup.sh`` (тот же формат, те же правила ротации). Скрипт нужен для
расписания (launchd), модуль — для работы внутри бота.

Токен бота здесь не читается и не логируется. Все сообщения об ошибках —
по-русски и без внутренних деталей: их видит владелец в Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import FSInputFile

from app.bot.media import is_local_api_server

logger = logging.getLogger("lifeos.bot.backup")

__all__ = [
    "BackupError",
    "BackupResult",
    "KEEP_BACKUPS",
    "PUBLIC_UPLOAD_LIMIT",
    "LOCAL_UPLOAD_LIMIT",
    "STARTUP_MIN_INTERVAL_MINUTES",
    "backup_dir",
    "build_router",
    "create_backup",
    "format_caption",
    "handle_backup_command",
    "human_size",
    "integrity_check",
    "list_backups",
    "max_upload_bytes",
    "rotate_backups",
    "startup_backup",
]

#: Сколько последних копий хранить (плюс месячные архивы).
KEEP_BACKUPS = 14

#: Лимит отправки документа ботом: 50 МБ у публичного Bot API, 2 ГБ у своего.
PUBLIC_UPLOAD_LIMIT = 50 * 1024 * 1024
LOCAL_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024

#: Если копия уже свежее этого, бэкап при старте не повторяется.
STARTUP_MIN_INTERVAL_MINUTES = 30

BACKUP_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}\.db$")
SQLITE_HEADER = b"SQLite format 3\x00"


class BackupError(Exception):
    """Бэкап не получился: сообщение пригодно для показа владельцу."""


@dataclass(frozen=True)
class BackupResult:
    """Итог одной копии: путь, размер, целостность, сколько старых удалено."""

    path: Path
    size: int
    integrity: str
    removed: int
    db_path: Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def human_size(self) -> str:
        return human_size(self.size)


def human_size(size: int) -> str:
    """Размер по-человечески: «1,1 МБ», «820 КБ», «512 Б»."""
    try:
        value = int(size)
    except (TypeError, ValueError):
        return "0 Б"
    if value >= 1024 * 1024:
        text = "%.1f" % (value / (1024 * 1024))
        return text.replace(".", ",") + " МБ"
    if value >= 1024:
        return "%d КБ" % round(value / 1024)
    return "%d Б" % value


def backup_dir(settings: Any) -> Path:
    """Каталог копий: ``LIFEOS_BACKUP_DIR`` или ``<data>/backups``."""
    env_value = str(os.environ.get("LIFEOS_BACKUP_DIR", "") or "").strip()
    if env_value:
        return Path(env_value).expanduser()
    data_dir = Path(getattr(settings, "data_dir"))
    return data_dir / "backups"


def max_upload_bytes(settings: Any) -> int:
    """Сколько бот может отправить документом: свой Bot API — 2 ГБ, иначе 50 МБ."""
    api_base = getattr(settings, "telegram_api_base", "")
    return LOCAL_UPLOAD_LIMIT if is_local_api_server(api_base) else PUBLIC_UPLOAD_LIMIT


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == SQLITE_HEADER
    except OSError:
        return False


def _table_count(path: Path) -> int:
    with sqlite3.connect(str(path), timeout=30) as conn:
        row = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()
    return int(row[0]) if row else 0


def integrity_check(path: Path) -> str:
    """Результат ``PRAGMA integrity_check``: ``ok`` или текст проблемы."""
    try:
        conn = sqlite3.connect(str(path), timeout=30)
    except sqlite3.Error as exc:
        return "не открывается: %s" % exc
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "нет ответа"
    except sqlite3.Error as exc:
        return "ошибка проверки: %s" % exc
    finally:
        conn.close()


def list_backups(directory: Path) -> list[Path]:
    """Копии в каталоге, свежие первыми."""
    try:
        entries = [entry for entry in Path(directory).iterdir() if entry.is_file()]
    except OSError:
        return []
    backups = [entry for entry in entries if BACKUP_NAME_RE.match(entry.name)]
    return sorted(backups, key=lambda item: item.name, reverse=True)


def rotate_backups(directory: Path, keep: int = KEEP_BACKUPS) -> list[str]:
    """Ротация: 14 последних + по одной самой свежей за каждый старый месяц.

    Возвращает имена удалённых файлов. Последние ``keep`` копий не удаляются
    никогда, даже если их больше, чем дней в месяце.
    """
    if keep < 1:
        raise ValueError("keep должен быть >= 1")
    backups = list_backups(directory)
    kept_months = {item.name[:7] for item in backups[:keep]}
    removed: list[str] = []
    for item in backups[keep:]:
        month = item.name[:7]
        if month in kept_months:
            try:
                item.unlink()
            except OSError as exc:  # не смогли удалить — не повод ронять бэкап
                logger.warning("не удалось удалить старую копию %s: %s", item.name, exc)
                continue
            removed.append(item.name)
        else:
            # Самая свежая копия месяца остаётся месячным архивом.
            kept_months.add(month)
    return removed


def _log_to_backup_log(settings: Any, message: str) -> None:
    """Дописывает строку в общий ``data/logs/backup.log`` (как tools/backup.sh).

    Ошибки записи не влияют на бэкап: файл лога — удобство, не условие.
    """
    logs_dir = Path(getattr(settings, "logs_dir", "") or "")
    if not str(logs_dir):
        return
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(logs_dir / "backup.log", "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (stamp, message))
    except OSError:
        logger.debug("не удалось записать строку в backup.log", exc_info=True)


def create_backup(
    db_path: Path,
    backups_dir: Path,
    *,
    keep: int = KEEP_BACKUPS,
    rotate: bool = True,
    now: Optional[datetime] = None,
) -> BackupResult:
    """Снимает горячую копию базы и кладёт её в ``backups_dir``.

    Бот при этом продолжает работать: SQLite backup API читает согласованный
    снимок базы, не блокируя запись. Готовая копия проверяется целиком
    (``PRAGMA integrity_check``) и превращается в самодостаточный файл
    (``journal_mode=DELETE``), чтобы при восстановлении не нужны были -wal/-shm.
    """
    db_path = Path(db_path)
    backups_dir = Path(backups_dir)
    if not db_path.is_file():
        raise BackupError("база не найдена: %s" % db_path)
    if not _is_sqlite(db_path):
        raise BackupError("файл базы не похож на SQLite: %s" % db_path)
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError("не удалось создать каталог копий %s: %s" % (backups_dir, exc)) from exc

    moment = now or datetime.now()
    destination = backups_dir / ("%s.db" % moment.strftime("%Y-%m-%d-%H"))
    temporary = backups_dir / (".%s.tmp-%d" % (destination.stem, os.getpid()))
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(temporary) + suffix).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    try:
        source = sqlite3.connect(str(db_path), timeout=30)
        target = sqlite3.connect(str(temporary), timeout=30, isolation_level=None)
        try:
            source.backup(target)
            # Сворачиваем WAL в файл: копия должна быть одним файлом.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
            source.close()

        integrity = integrity_check(temporary)
        if integrity != "ok":
            raise BackupError(
                "копия не прошла проверку целостности (%s) — в архив не попала" % integrity
            )
        tables = _table_count(temporary)
        if tables < 1:
            raise BackupError("в копии нет ни одной таблицы — это не база Life OS")

        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
    except sqlite3.Error as exc:
        raise BackupError("не удалось скопировать базу: %s" % exc) from exc
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(temporary) + suffix).unlink()
            except OSError:
                pass

    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass

    size = destination.stat().st_size
    if size <= 0:
        raise BackupError("копия весит 0 байт — в архив не попала")

    removed: list[str] = []
    if rotate:
        removed = rotate_backups(backups_dir, keep=keep)

    result = BackupResult(
        path=destination,
        size=size,
        integrity="ok",
        removed=len(removed),
        db_path=db_path,
    )
    logger.info(
        "бэкап базы: %s (%d байт, целостность ok, удалено старых: %d)",
        destination,
        size,
        len(removed),
    )
    return result


def startup_backup(
    settings: Any,
    *,
    min_interval_minutes: int = STARTUP_MIN_INTERVAL_MINUTES,
    now: Optional[datetime] = None,
) -> Optional[BackupResult]:
    """Копия при старте бота. Никогда не бросает: бот важнее бэкапа.

    Если свежая копия уже есть (например, её сделал launchd в 04:00),
    повторно не копируем — иначе серия перезапусков забьёт архив.
    """
    try:
        directory = backup_dir(settings)
        existing = list_backups(directory)
        moment = now or datetime.now()
        if existing:
            age_minutes = (moment - datetime.fromtimestamp(existing[0].stat().st_mtime)).total_seconds() / 60
            if age_minutes < min_interval_minutes:
                logger.info(
                    "свежая копия уже есть (%s, %.0f мин назад) — при старте не повторяю",
                    existing[0].name,
                    age_minutes,
                )
                return None
        result = create_backup(Path(getattr(settings, "db_path")), directory, now=moment)
        _log_to_backup_log(
            settings,
            "бот при старте: %s (%d B, integrity=ok)" % (result.path, result.size),
        )
        return result
    except BackupError as exc:
        logger.warning("копия при старте не сделана: %s", exc)
        return None
    except Exception:  # pragma: no cover - защита от любых сбоев при старте
        logger.exception("неожиданная ошибка при бэкапе на старте")
        return None


# --------------------------------------------------------------------------
# Команда /backup в Telegram
# --------------------------------------------------------------------------


def _chat_allowed(settings: Any, chat_id: Any, user_id: Any) -> bool:
    """Тот же whitelist, что у остальных команд: пустой id — никому нельзя."""
    try:
        allowed = int(getattr(settings, "allowed_chat_id", 0) or 0)
    except (TypeError, ValueError):
        return False
    if allowed == 0:
        return False
    return chat_id == allowed or (user_id is not None and user_id == allowed)


def format_caption(result: BackupResult) -> str:
    """Подпись к файлу: имя, размер, проверка. Без путей и секретов."""
    return "Бэкап базы Life OS\n%s · %s · целостность ok" % (result.name, result.human_size)


async def _answer(message: Any, text: str) -> None:
    try:
        await message.answer(text)
    except Exception:  # pragma: no cover - сеть/Telegram могут подвести
        logger.exception("не удалось отправить ответ в чат")


async def handle_backup_command(settings: Any, message: Any, service: Any = None) -> None:
    """``/backup``: копия сейчас + файл документом в чат.

    Логика нарочно простая и без магии: сделать копию, проверить размер и
    целостность, отправить. Если что-то не так — короткое объяснение, что
    именно, и где искать копию на маке.

    ``service`` — необязательный :class:`app.bot.handlers.BotService`: через
    него команда попадает в статистику использования (как /today и /help).
    """
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    if not _chat_allowed(settings, chat_id, user_id):
        logger.warning("/backup: чужой chat_id=%r — игнорирую", chat_id)
        return
    if service is not None:
        try:
            await asyncio.to_thread(service.record_command, "/backup")
        except Exception:  # статистика не должна ломать бэкап
            logger.debug("не удалось записать /backup в статистику", exc_info=True)

    try:
        result = await asyncio.to_thread(
            create_backup,
            Path(getattr(settings, "db_path")),
            backup_dir(settings),
        )
    except BackupError as exc:
        await _answer(message, "⚠️ Бэкап не получился: %s" % exc)
        return
    except Exception:
        logger.exception("/backup: неожиданная ошибка")
        await _answer(message, "⚠️ Бэкап не получился. Подробности — в data/logs/bot.log.")
        return

    if result.integrity != "ok":
        await _answer(message, "⚠️ Копия не прошла проверку целостности. В архив не отправляю.")
        return
    if result.size <= 0:
        await _answer(message, "⚠️ Копия получилась пустой. Проверьте базу на маке.")
        return

    limit = max_upload_bytes(settings)
    if result.size > limit:
        await _answer(
            message,
            "Копия готова, но она больше %s — Telegram такой файл не примет.\n"
            "Файл на маке: %s" % (human_size(limit), result.path),
        )
        return

    try:
        await message.answer_document(
            FSInputFile(str(result.path), filename=result.path.name),
            caption=format_caption(result),
            # Копия может быть тяжёлой: не обрываем отправку на 60 секундах.
            request_timeout=300,
        )
    except Exception as exc:
        logger.exception("не удалось отправить бэкап в Telegram")
        await _answer(
            message,
            "Копия готова, но отправить файл не смог: %s\nФайл на маке: %s"
            % (exc, result.path),
        )
        return

    logger.info("/backup отправлен: %s (%d байт)", result.name, result.size)
    _log_to_backup_log(
        settings,
        "по команде /backup: %s (%d B, отправлен в Telegram)" % (result.path, result.size),
    )


def build_router(settings: Any, service: Any = None) -> Router:
    """Роутер с командой ``/backup``.

    Подключается в ``app/bot/main.py`` ДО общего роутера, иначе общий
    обработчик текста перехватит команду и ответит подсказкой.
    """
    router = Router(name="lifeos-backup")

    @router.message(Command("backup"))
    async def on_backup(message: Any) -> None:
        await handle_backup_command(settings, message, service)

    return router
