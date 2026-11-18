"""Тесты бэкапов базы Life OS и команды ``/backup`` (задача t28).

Проверяется контракт:

* горячая копия снимается при открытой базе (WAL, идёт запись) и проходит
  ``PRAGMA integrity_check``; копия — один самодостаточный файл без -wal/-shm;
* имя копии — ``YYYY-MM-DD-HH.db``, одна на час;
* ротация: 14 последних копий не удаляются, за каждый более старый месяц
  остаётся самая свежая копия;
* ``tools/backup.sh`` и ``tools/restore-check.sh`` работают как заявлено
  (копия создаётся, проверка печатает «бэкап годен», битая копия — «НЕ годен»);
* ``/backup`` отправляет файл документом, проверяет размер и молчит для чужих;
* второй экземпляр бота не запускается (замок ``data/bot.lock``);
* без токена бот завершается с кодом 2 и понятным сообщением.

Всё во временных каталогах: настоящие ``data/``, ``config/config.json`` и
Telegram не затрагиваются.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendDocument
from aiogram.types import Chat, Document, Message, Update, User

from app.bot import backup as backup_module
from app.bot import main as bot_main
from app.server import db as app_db
from app.server.settings import load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = PROJECT_ROOT / "tools" / "backup.sh"
RESTORE_SCRIPT = PROJECT_ROOT / "tools" / "restore-check.sh"


# --------------------------------------------------------------------------
# Помощники
# --------------------------------------------------------------------------


def make_db(path: Path, rows: int = 5, journal_mode: str = "WAL") -> sqlite3.Connection:
    """Создаёт маленькую базу с теми таблицами, которые считает restore-check."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=%s" % journal_mode)
    conn.execute("CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, text TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY, weight REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, title TEXT)")
    for index in range(rows):
        conn.execute("INSERT INTO entries(text) VALUES (?)", ("запись %d" % index,))
        conn.execute("INSERT INTO metrics(weight) VALUES (?)", (80 + index * 0.1,))
    conn.commit()
    return conn


def make_settings(tmp_path: Path, *, chat_id: int = 123, token: str = "1:SECRET") -> Any:
    """Настройки из временного конфига: свои пути, никаких реальных данных."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "data" / "lifeos-v3.db"
    config = {
        "telegram": {"token": token, "allowed_chat_id": chat_id},
        "paths": {"db": str(db_path)},
    }
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    settings = load_settings(config_path=config_dir / "config.json", env={}, root=tmp_path)
    settings.ensure_dirs()
    return settings


def _script_env(tmp_path: Path) -> dict[str, str]:
    """Окружение для скриптов: журналы во временном каталоге, не в data/ проекта."""
    env = dict(os.environ)
    env["LIFEOS_LOGS_DIR"] = str(tmp_path / "logs")
    env.pop("LIFEOS_DB_PATH", None)
    env.pop("LIFEOS_BACKUP_DIR", None)
    return env


class FakeMessage:
    """Минимальное сообщение aiogram для вызова обработчиков без сети."""

    def __init__(self, chat_id: int = 123, user_id: int = 123) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []
        self.documents: list[tuple[Any, str]] = []

    async def answer(self, text: str, **_: Any) -> None:
        self.answers.append(text)

    async def answer_document(self, document: Any, caption: str | None = None, **_: Any) -> None:
        self.documents.append((document, caption or ""))


class RecordingSession(BaseSession):
    """Сессия aiogram, которая записывает вызовы Bot API вместо отправки в сеть."""

    def __init__(self) -> None:
        super().__init__()
        self.methods: list[Any] = []

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: Any, timeout: Any = None) -> Any:
        self.methods.append(method)
        if isinstance(method, SendDocument):
            return Message(
                message_id=1,
                date=datetime.now(),
                chat=Chat(id=123, type="private"),
                document=Document(file_id="file", file_unique_id="unique"),
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        yield b""


# --------------------------------------------------------------------------
# Копия базы
# --------------------------------------------------------------------------


def test_backup_name_is_hourly(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "lifeos-v3.db"
    conn = make_db(db_path)
    try:
        result = backup_module.create_backup(
            db_path, tmp_path / "backups", now=datetime(2026, 11, 18, 4, 15)
        )
    finally:
        conn.close()
    assert result.path.name == "2026-11-18-04.db"
    assert result.path.is_file()
    assert result.integrity == "ok"
    assert result.size > 0


def test_hot_backup_with_open_wal_connection(tmp_path: Path) -> None:
    """Копия снимается, пока база открыта и в ней есть несохранённые -wal страницы."""
    db_path = tmp_path / "data" / "lifeos-v3.db"
    conn = make_db(db_path, rows=7)
    try:
        conn.execute("INSERT INTO entries(text) VALUES ('после создания')")
        conn.commit()
        result = backup_module.create_backup(db_path, tmp_path / "backups")
        assert result.integrity == "ok"
        # Копия самодостаточна: никаких -wal/-shm рядом.
        sidecars = [p.name for p in (tmp_path / "backups").iterdir() if p.name != result.path.name]
        assert sidecars == []
        check = sqlite3.connect(str(result.path))
        try:
            assert check.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
            assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            rows = check.execute("SELECT count(*) FROM entries").fetchone()[0]
        finally:
            check.close()
        assert rows == 8
    finally:
        conn.close()


def test_hot_backup_while_writer_keeps_writing(tmp_path: Path) -> None:
    """Пока бот пишет, копия снимается без ошибок, база остаётся целой."""
    db_path = tmp_path / "data" / "lifeos-v3.db"
    conn = make_db(db_path, rows=2)
    stop = threading.Event()

    def writer() -> None:
        writer_conn = sqlite3.connect(str(db_path), timeout=30)
        try:
            while not stop.is_set():
                writer_conn.execute("INSERT INTO metrics(weight) VALUES (81.0)")
                writer_conn.commit()
                time.sleep(0.002)
        finally:
            writer_conn.close()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        time.sleep(0.05)
        result = backup_module.create_backup(db_path, tmp_path / "backups")
        assert result.integrity == "ok"
        stop.set()
        thread.join(timeout=5)
        # Записи не потерялись: копия читается, база цела.
        assert backup_module.integrity_check(result.path) == "ok"
        assert backup_module.integrity_check(db_path) == "ok"
        check = sqlite3.connect(str(result.path))
        try:
            assert check.execute("SELECT count(*) FROM metrics").fetchone()[0] >= 2
        finally:
            check.close()
    finally:
        stop.set()
        conn.close()


def test_backup_rejects_missing_and_broken_database(tmp_path: Path) -> None:
    with pytest.raises(backup_module.BackupError):
        backup_module.create_backup(tmp_path / "нет.db", tmp_path / "backups")

    broken = tmp_path / "data" / "broken.db"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a sqlite database at all")
    with pytest.raises(backup_module.BackupError):
        backup_module.create_backup(broken, tmp_path / "backups")


def test_integrity_check_detects_garbage(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"\x00" * 4096)
    assert backup_module.integrity_check(garbage) != "ok"


# --------------------------------------------------------------------------
# Ротация
# --------------------------------------------------------------------------


def _touch_backups(directory: Path, names: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"x")


def test_rotation_keeps_last_14_and_one_per_old_month(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    names = []
    start = datetime(2026, 9, 20, 4)
    for index in range(40):
        names.append((start + timedelta(days=index)).strftime("%Y-%m-%d-%H.db"))
    _touch_backups(directory, names)

    removed = backup_module.rotate_backups(directory, keep=14)

    remaining = sorted(path.name for path in directory.iterdir())
    assert len(removed) == 25  # 26 «лишних» минус один месячный архив сентября
    assert len(remaining) == 14 + 1  # 14 последних + один месячный архив (сентябрь)
    # Последние 14 копий на месте — их ротация не трогает.
    for name in sorted(names)[-14:]:
        assert name in remaining
    assert "2026-09-30-04.db" in remaining  # самая свежая копия сентября
    assert "2026-09-29-04.db" not in remaining


def test_rotation_does_not_touch_last_14(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    names = [
        (datetime(2026, 11, 1, 4) + timedelta(hours=index)).strftime("%Y-%m-%d-%H.db")
        for index in range(14)
    ]
    _touch_backups(directory, names)
    assert backup_module.rotate_backups(directory, keep=14) == []
    assert len(list(directory.iterdir())) == 14


def test_rotation_keeps_newest_copy_of_each_old_month(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    names = []
    for year, month in ((2026, 7), (2026, 8), (2026, 9)):
        for day in (1, 10, 20, 28):
            names.append(datetime(year, month, day, 4).strftime("%Y-%m-%d-%H.db"))
    for hour in range(14):
        names.append((datetime(2026, 10, 20, 4) + timedelta(hours=hour)).strftime("%Y-%m-%d-%H.db"))
    _touch_backups(directory, names)

    backup_module.rotate_backups(directory, keep=14)

    remaining = sorted(path.name for path in directory.iterdir())
    assert len(remaining) == 14 + 3
    for month in ("2026-07", "2026-08", "2026-09"):
        monthly = [name for name in remaining if name.startswith(month)]
        assert len(monthly) == 1, monthly


def test_create_backup_rotates_old_copies(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    db_path = tmp_path / "data" / "lifeos-v3.db"
    conn = make_db(db_path)
    try:
        old_names = [
            (datetime(2026, 9, 1, 4) + timedelta(days=index)).strftime("%Y-%m-%d-%H.db")
            for index in range(20)
        ]
        _touch_backups(directory, old_names)
        result = backup_module.create_backup(
            db_path, directory, now=datetime(2026, 11, 18, 4)
        )
    finally:
        conn.close()
    remaining = sorted(path.name for path in directory.iterdir())
    assert result.path.name in remaining
    # Сентябрь уже представлен копией 2026-09-20 из последних 14 — отдельный
    # месячный архив не нужен, поэтому всего ровно 14 файлов.
    assert len(remaining) == 14
    assert "2026-09-20-04.db" in remaining
    assert "2026-09-01-04.db" not in remaining


# --------------------------------------------------------------------------
# Бэкап при старте бота
# --------------------------------------------------------------------------


def test_startup_backup_creates_then_skips_fresh(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    make_db(Path(settings.db_path), rows=3).close()

    first = backup_module.startup_backup(settings, now=datetime(2026, 11, 18, 4, 0))
    assert first is not None and first.integrity == "ok"

    fresh_moment = datetime.fromtimestamp(first.path.stat().st_mtime) + timedelta(minutes=5)
    again = backup_module.startup_backup(settings, now=fresh_moment)
    assert again is None  # свежая копия есть — не дублируем

    later = backup_module.startup_backup(settings, now=fresh_moment + timedelta(hours=3))
    assert later is not None


def test_startup_backup_never_raises_without_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert backup_module.startup_backup(settings) is None


# --------------------------------------------------------------------------
# Команда /backup
# --------------------------------------------------------------------------


def _prepare_settings_with_db(tmp_path: Path) -> Any:
    settings = make_settings(tmp_path)
    make_db(Path(settings.db_path), rows=4).close()
    return settings


def test_backup_command_sends_document_with_size_check(tmp_path: Path) -> None:
    settings = _prepare_settings_with_db(tmp_path)
    message = FakeMessage(chat_id=123, user_id=123)

    asyncio.run(backup_module.handle_backup_command(settings, message))

    assert message.answers == []  # ошибок нет
    assert len(message.documents) == 1
    document, caption = message.documents[0]
    path = Path(document.path)
    assert path == backup_module.list_backups(backup_module.backup_dir(settings))[0]
    assert path.stat().st_size > 0
    assert "целостность ok" in caption
    assert path.name in caption


def test_backup_command_records_usage(tmp_path: Path) -> None:
    """Команда попадает в статистику использования, как /today и /help."""
    settings = _prepare_settings_with_db(tmp_path)

    class FakeService:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def record_command(self, command: str) -> None:
            self.commands.append(command)

    service = FakeService()
    asyncio.run(backup_module.handle_backup_command(settings, FakeMessage(), service))
    assert service.commands == ["/backup"]


def test_backup_command_ignores_stranger(tmp_path: Path) -> None:
    settings = _prepare_settings_with_db(tmp_path)
    message = FakeMessage(chat_id=999, user_id=999)
    asyncio.run(backup_module.handle_backup_command(settings, message))
    assert message.documents == []
    assert message.answers == []


def test_backup_command_reports_too_large_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _prepare_settings_with_db(tmp_path)
    monkeypatch.setattr(backup_module, "max_upload_bytes", lambda _settings: 10)
    message = FakeMessage()
    asyncio.run(backup_module.handle_backup_command(settings, message))
    assert message.documents == []
    assert message.answers and "больше" in message.answers[0]


def test_backup_command_reports_backup_error(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)  # база ещё не создана
    message = FakeMessage()
    asyncio.run(backup_module.handle_backup_command(settings, message))
    assert message.documents == []
    assert message.answers and "не получился" in message.answers[0]


def test_backup_command_does_not_leak_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = _prepare_settings_with_db(tmp_path)
    message = FakeMessage()
    with caplog.at_level(logging.INFO, logger="lifeos.bot.backup"):
        asyncio.run(backup_module.handle_backup_command(settings, message))
    assert "1:SECRET" not in caplog.text


def test_backup_command_through_dispatcher_sends_senddocument(tmp_path: Path) -> None:
    """Реальный роутер aiogram: /backup → метод sendDocument с файлом копии."""
    settings = _prepare_settings_with_db(tmp_path)
    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(backup_module.build_router(settings))
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=123, type="private"),
            from_user=User(id=123, is_bot=False, first_name="Владелец"),
            text="/backup",
        ),
    )

    async def run() -> None:
        try:
            await dispatcher.feed_update(bot, update)
        finally:
            await session.close()

    asyncio.run(run())
    sends = [method for method in session.methods if isinstance(method, SendDocument)]
    assert len(sends) == 1
    document = sends[0].document
    assert Path(document.path).is_file()
    assert Path(document.path).stat().st_size > 0
    assert sends[0].caption and "целостность ok" in sends[0].caption


# --------------------------------------------------------------------------
# Скрипты для launchd и ручного запуска
# --------------------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("sqlite3"), reason="нет sqlite3")
@pytest.mark.skipif(not BACKUP_SCRIPT.is_file(), reason="нет tools/backup.sh")
def test_backup_script_creates_copy_and_checks_integrity(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "lifeos-v3.db"
    make_db(db_path, rows=6).close()
    backups = tmp_path / "backups"

    run = subprocess.run(
        ["/bin/bash", str(BACKUP_SCRIPT), "--db", str(db_path), "--dir", str(backups)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=_script_env(tmp_path),
    )
    assert run.returncode == 0, run.stderr
    assert "бэкап готов" in run.stdout
    assert "целостность ok" in run.stdout
    copies = backup_module.list_backups(backups)
    assert len(copies) == 1
    assert copies[0].name.endswith(".db")
    check = sqlite3.connect(str(copies[0]))
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT count(*) FROM entries").fetchone()[0] == 6
    finally:
        check.close()


@pytest.mark.skipif(not shutil.which("sqlite3"), reason="нет sqlite3")
@pytest.mark.skipif(not BACKUP_SCRIPT.is_file(), reason="нет tools/backup.sh")
def test_backup_script_rotation_keeps_last_14(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "lifeos-v3.db"
    make_db(db_path, rows=1).close()
    backups = tmp_path / "backups"
    old_names = [
        (datetime(2026, 9, 1, 4) + timedelta(days=index)).strftime("%Y-%m-%d-%H.db")
        for index in range(30)
    ]
    _touch_backups(backups, old_names)

    run = subprocess.run(
        ["/bin/bash", str(BACKUP_SCRIPT), "--db", str(db_path), "--dir", str(backups), "--quiet"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=_script_env(tmp_path),
    )
    assert run.returncode == 0, run.stderr
    remaining = sorted(path.name for path in backups.iterdir())
    # Последние 14 включают 2026-09-20 — сентябрь уже представлен, отдельного
    # месячного архива не будет.
    assert len(remaining) == 14
    new_backup = [name for name in remaining if not name.startswith("2026-09")]
    assert len(new_backup) == 1  # свежая копия
    for name in sorted(old_names)[-13:]:  # 13 самых свежих старых + новая = 14
        assert name in remaining


@pytest.mark.skipif(not shutil.which("sqlite3"), reason="нет sqlite3")
@pytest.mark.skipif(not RESTORE_SCRIPT.is_file(), reason="нет tools/restore-check.sh")
def test_restore_check_script_reports_good_and_bad_copy(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "lifeos-v3.db"
    make_db(db_path, rows=3).close()
    backups = tmp_path / "backups"
    backup_module.create_backup(db_path, backups)

    good = subprocess.run(
        ["/bin/bash", str(RESTORE_SCRIPT), "--dir", str(backups)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=_script_env(tmp_path),
    )
    assert good.returncode == 0, good.stderr
    assert "бэкап годен" in good.stdout
    assert "entries: 3" in good.stdout

    broken = tmp_path / "backups" / "2026-01-01-04.db"
    broken.write_bytes(b"definitely not sqlite")
    bad = subprocess.run(
        ["/bin/bash", str(RESTORE_SCRIPT), str(broken)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=_script_env(tmp_path),
    )
    assert bad.returncode == 1
    assert "НЕ годен" in (bad.stderr + bad.stdout)


# --------------------------------------------------------------------------
# Замок второго экземпляра и запуск без токена
# --------------------------------------------------------------------------


def test_instance_lock_blocks_second_instance(tmp_path: Path) -> None:
    lock_path = tmp_path / "data" / "bot.lock"
    first, pid = bot_main.acquire_instance_lock(lock_path)
    assert first is not None and pid == ""
    try:
        second, other_pid = bot_main.acquire_instance_lock(lock_path)
        assert second is None
        assert other_pid == str(os.getpid())
    finally:
        first.close()


def test_instance_lock_ignores_stale_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "data" / "bot.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("999999", encoding="utf-8")
    handle, pid = bot_main.acquire_instance_lock(lock_path)
    try:
        assert handle is not None and pid == ""
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        assert handle is not None
        handle.close()


def test_serve_refuses_without_token(tmp_path: Path) -> None:
    settings = load_settings(env={}, root=tmp_path)  # токена нет
    assert settings.token == ""
    exit_code = asyncio.run(bot_main.serve(settings=settings))
    assert exit_code == 2


def test_serve_refuses_second_instance(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    make_db(Path(settings.db_path)).close()
    lock = tmp_path / "data" / "bot.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle, _ = bot_main.acquire_instance_lock(lock)
    try:
        exit_code = asyncio.run(bot_main.serve(settings=settings))
        assert exit_code == 3
    finally:
        assert handle is not None
        handle.close()

class _NoPollingDispatcher(Dispatcher):
    """Диспетчер, который не уходит в long-polling: проверяем путь старта бота."""

    async def start_polling(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None


def test_bot_start_makes_backup_and_releases_lock(tmp_path: Path) -> None:
    """Старт бота: копия базы создаётся в том же вызове, замок снимается."""
    settings = make_settings(tmp_path)
    # Настоящая схема Life OS: bot_main.serve открывает базу через db.init_db.
    conn = app_db.init_db(Path(settings.db_path))
    conn.close()

    exit_code = asyncio.run(
        bot_main.serve(settings=settings, bot=SimpleNamespace(), dispatcher=_NoPollingDispatcher())
    )
    assert exit_code == 0
    copies = backup_module.list_backups(backup_module.backup_dir(settings))
    assert len(copies) == 1
    assert backup_module.integrity_check(copies[0]) == "ok"
    # Замок после выхода свободен: следующий запуск не заблокирован.
    handle, other = bot_main.acquire_instance_lock(tmp_path / "data" / "bot.lock")
    try:
        assert handle is not None and other == ""
    finally:
        assert handle is not None
        handle.close()

# --------------------------------------------------------------------------
# Один экземпляр: лог не дублируется, служба заменяет ручной запуск
# --------------------------------------------------------------------------


def test_configure_logging_does_not_duplicate_console(tmp_path: Path) -> None:
    """main() дал basicConfig — configure_logging не должен добавлять второй консольный лог."""
    settings = make_settings(tmp_path)
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_flag = getattr(root, "_lifeos_configured", None)
    try:
        root.handlers = []
        if hasattr(root, "_lifeos_configured"):
            delattr(root, "_lifeos_configured")
        logging.basicConfig(level=logging.INFO)
        bot_main.configure_logging(settings)
        console_handlers = [
            handler for handler in root.handlers
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        ]
        assert len(console_handlers) == 1
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers = saved_handlers
        if saved_flag is None:
            if hasattr(root, "_lifeos_configured"):
                delattr(root, "_lifeos_configured")
        else:
            root._lifeos_configured = saved_flag  # type: ignore[attr-defined]


def _start_locking_process(tmp_path: Path, lock_path: Path, *, bot_like: bool = True) -> subprocess.Popen:
    """Процесс-притворщик: держит замок бота, в командной строке есть app/bot/main.py.

    Файл-притворщик лежит по пути ``app/bot/main.py`` во временном каталоге,
    поэтому ``ps`` показывает его как настоящий экземпляр бота. Готовность
    сообщает файлом-флагом: с пайпами на macOS легко получить блокировку.
    """
    if bot_like:
        script = tmp_path / "app" / "bot" / "main.py"
    else:
        script = tmp_path / "not-a-bot.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "import app.bot.main as m\n"
        "handle, other = m.acquire_instance_lock(Path(%r))\n"
        "Path(%r).write_text('locked')\n"
        "time.sleep(120)\n" % (str(PROJECT_ROOT), str(lock_path), str(tmp_path / "locked.flag")),
        encoding="utf-8",
    )
    child = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    flag = tmp_path / "locked.flag"
    for _ in range(200):
        if flag.is_file() and flag.read_text(encoding="utf-8") == "locked":
            return child
        if child.poll() is not None:
            raise AssertionError("процесс-притворщик завершился, не заняв замок")
        time.sleep(0.05)
    _stop(child)
    raise AssertionError("процесс-притворщик не занял замок за 10 секунд")


def _stop(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def test_service_takes_over_manual_instance(tmp_path: Path) -> None:
    """Служба останавливает ручной экземпляр из bot-manual.pid и запускается сама."""
    settings = make_settings(tmp_path)
    conn = app_db.init_db(Path(settings.db_path))
    conn.close()
    lock_path = tmp_path / "data" / "bot.lock"
    manual = _start_locking_process(tmp_path, lock_path)
    (tmp_path / "data" / "bot-manual.pid").write_text(str(manual.pid), encoding="utf-8")
    try:
        exit_code = asyncio.run(
            bot_main.serve(
                settings=settings,
                bot=SimpleNamespace(),
                dispatcher=_NoPollingDispatcher(),
            )
        )
        assert exit_code == 0
        assert manual.poll() is not None  # ручной экземпляр остановлен
        assert not (tmp_path / "data" / "bot-manual.pid").exists()
        assert backup_module.list_backups(backup_module.backup_dir(settings))
    finally:
        _stop(manual)


def test_service_does_not_kill_its_own_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Свой процесс службы (pid из launchctl) не трогаем — код 3, без 409-моргания."""
    settings = make_settings(tmp_path)
    conn = app_db.init_db(Path(settings.db_path))
    conn.close()
    lock_path = tmp_path / "data" / "bot.lock"
    own = _start_locking_process(tmp_path, lock_path)
    monkeypatch.setattr(bot_main, "_service_pid", lambda: own.pid)
    try:
        exit_code = asyncio.run(
            bot_main.serve(
                settings=settings,
                bot=SimpleNamespace(),
                dispatcher=_NoPollingDispatcher(),
            )
        )
        assert exit_code == 3
        assert own.poll() is None  # процесс жив
    finally:
        _stop(own)


def test_takeover_ignores_process_that_is_not_bot(tmp_path: Path) -> None:
    """Не наш процесс (нет app.bot.main в командной строке) не убиваем — код 3."""
    settings = make_settings(tmp_path)
    conn = app_db.init_db(Path(settings.db_path))
    conn.close()
    lock_path = tmp_path / "data" / "bot.lock"
    other = _start_locking_process(tmp_path, lock_path, bot_like=False)
    (tmp_path / "data" / "bot-manual.pid").write_text(str(other.pid), encoding="utf-8")
    try:
        exit_code = asyncio.run(
            bot_main.serve(
                settings=settings,
                bot=SimpleNamespace(),
                dispatcher=_NoPollingDispatcher(),
            )
        )
        assert exit_code == 3
        assert other.poll() is None
    finally:
        _stop(other)
