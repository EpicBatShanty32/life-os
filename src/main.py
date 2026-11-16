"""Точка входа Life OS: long polling Telegram + тики планировщика.

Запуск:
    python3 src/main.py                     # обычная работа (launchd/вручную)
    python3 src/main.py --once --dry-run    # один цикл без Telegram (проверка)
    python3 src/main.py --config config/config.json

Режимы аргументов:
* ``--dry-run`` — Telegram не трогаем, все исходящие сообщения пишем в лог;
* ``--once`` — один проход (getUpdates + tick) и выход;
* ``--config PATH`` — явный путь к JSON-конфигу;
* ``--mock-url URL`` — подменить ``telegram.api_base`` (для мок-сервера qa/e2e).

Процесс устойчив: любое исключение в цикле ловится и логируется, секреты в лог
не попадают, двойной запуск исключён атомарным ``flock``-локом на файле
``data/lifeos.pid`` (pid + started_at; второй экземпляр выходит с кодом 3 ещё
до любой записи в vault/БД), SIGTERM/SIGINT завершают работу аккуратно.
"""

from __future__ import annotations

import argparse
import atexit
import errno
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

# project_root в sys.path — чтобы работал и `python3 -m src.main`, и `python3 src/main.py`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config as config_mod  # noqa: E402
from src import __version__ as APP_VERSION  # noqa: E402

__all__ = ["main"]

#: Лог крутится после 5 МБ, архив ровно один — ``agent.log.1`` (v0.3): лишние
#: копии не нужны, а одна даёт «что было до сбоя» без роста диска.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 1
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: Пауза между повторами после сетевой ошибки: 2, 4, 8, 16, 32, 60, 60… секунд.
#: Потолок нужен, чтобы бот не «засыпал» навсегда при долгом обрыве связи.
BACKOFF_BASE_S = 2.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP_S = 60.0
#: Сколько секунд максимум ждём по подсказке сервера (``retry_after`` у 429):
#: его просьбу уважаем, но не больше 15 минут.
RETRY_AFTER_MAX_S = 900.0
#: Таймаут long polling. Telegram держит запрос до этого времени, поэтому оно же
#: ограничивает задержку завершения по SIGTERM (launchd ждёт 20 с до SIGKILL).
POLL_TIMEOUT_S = 10

PID_RELATIVE_PATH = "data/lifeos.pid"
#: Права pid-файла: как у остальных личных данных (только владельцу).
LOCK_FILE_MODE = 0o600
#: Сколько секунд допускается при сверке ``started_at`` из pid-файла с реальным
#: временем старта процесса (``ps -o etime=``): нужно, чтобы переиспользованный
#: после ребута pid не считался живым владельцем лока.
LOCK_START_TOLERANCE_SEC = 180.0
#: Подстроки командной строки, по которым видно, что pid — это наш агент
#: (``ps -p PID -o command=``; в macOS нет ``/proc``).
LOCK_CMDLINE_MARKERS = ("lifeos", "telegram-life-agent", "src/main.py")
#: Сколько байт pid-файла читаем при разборе (защита от мусора в файле).
LOCK_READ_LIMIT = 65536

#: Права «только владельцу»: новые файлы 0600, каталоги 0700. База и логи
#: содержат личные записи, поэтому umask выставляется первым делом.
PRIVATE_UMASK = 0o077
#: Права каталога с личными данными и самой SQLite-базы.
DATA_DIR_MODE = 0o700
DATA_FILE_MODE = 0o600
#: Сколько символов текста сообщения попадает в лог в режиме --dry-run.
DRY_RUN_PREVIEW_CHARS = 60

#: Все code point'ы UTF-16-суррогатов. В корректном Python-str их быть не может
#: (эмодзи — один code point), поэтому любой такой символ — «lone surrogate»:
#: он ломает encode("utf-8") при записи в SQLite, лог и тело запроса Telegram.
_SURROGATE_TABLE = {code: None for code in range(0xD800, 0xE000)}


def _strip_surrogates(text) -> str:
    """Убирает lone surrogate (U+D800–U+DFFF) из текста.

    Такие символы приходят из «битых» апдейтов и из внешних источников; без
    очистки они дают UnicodeEncodeError на первом же encode("utf-8").
    """
    value = "" if text is None else (text if isinstance(text, str) else str(text))
    if not value:
        return ""
    return value.translate(_SURROGATE_TABLE)


# ============================================================== PID-ЛОК
def _pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким pid (POSIX, без внешних утилит)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # процесс есть, но он чужой
        return True
    except OSError:
        return False
    return True


def _proc_command(pid: int) -> str | None:
    """Командная строка процесса через ``ps`` (в macOS нет ``/proc``).

    Returns:
        Строка команды или ``None``, если ``ps`` недоступен/не ответил.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    text = (result.stdout or "").strip()
    return text or None


def _proc_elapsed_sec(pid: int) -> float | None:
    """Сколько секунд живёт процесс (``ps -p PID -o etime=``).

    Форматы вывода ``ps``: ``SS``, ``MM:SS``, ``HH:MM:SS``, ``DD-HH:MM:SS``.
    ``None`` — если процесс уже умер или ``ps`` не ответил.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "etime="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    days = 0
    if "-" in raw:
        head, _, raw = raw.partition("-")
        try:
            days = int(head)
        except ValueError:
            return None
    try:
        chunks = [int(part) for part in raw.split(":")]
    except ValueError:
        return None
    if len(chunks) == 1:
        seconds = chunks[0]
    elif len(chunks) == 2:
        seconds = chunks[0] * 60 + chunks[1]
    elif len(chunks) == 3:
        seconds = chunks[0] * 3600 + chunks[1] * 60 + chunks[2]
    else:
        return None
    return float(days * 86400 + seconds)


def _looks_like_agent(command: str) -> bool:
    """Похожа ли командная строка процесса на Life OS (а не на чужой процесс)."""
    low = (command or "").lower()
    return any(marker in low for marker in LOCK_CMDLINE_MARKERS)


def _parse_lock_ts(value) -> datetime | None:
    """Разбирает ISO-время из pid-файла (naive трактуем как локальное)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def _lock_now() -> datetime:
    """Текущее локальное время с таймзоной (для поля ``started_at``)."""
    return datetime.now().astimezone()


def _parse_lock_payload(raw: str) -> dict:
    """Разбирает содержимое pid-файла в ``{"pid": int, "started_at": str}``.

    Поддерживаются все исторические форматы, чтобы апгрейд не блокировал запуск:

    * JSON ``{"pid": 123, "started_at": "..."}`` — текущий формат;
    * две строки ``pid`` и ``started_at`` из переходной версии;
    * просто число — pid-файл самой первой версии.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            payload: dict = {}
            try:
                pid = int(data.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid > 0:
                payload["pid"] = pid
            started = str(data.get("started_at") or "").strip()
            if started:
                payload["started_at"] = started
            return payload
    first, _, rest = text.partition("\n")
    try:
        pid = int(first.strip())
    except ValueError:
        return {}
    if pid <= 0:
        return {}
    payload = {"pid": pid}
    started = (rest.strip().splitlines() or [""])[0].strip()
    if started:
        payload["started_at"] = started
    return payload


def _read_fd_text(fd: int) -> str:
    """Читает содержимое открытого pid-файла (не больше ``LOCK_READ_LIMIT``)."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        return ""
    chunks: list[bytes] = []
    total = 0
    while total < LOCK_READ_LIMIT:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def _owner_is_live(payload: dict) -> bool:
    """Живой ли владелец из pid-файла — именно наш агент, а не чужой pid.

    Лок считается занятым, только если pid существует **и** подтверждается
    хотя бы одно из двух:

    * командная строка процесса содержит признаки Life OS
      (``ps -p PID -o command=``);
    * ``started_at`` из pid-файла совпадает с реальным временем старта процесса
      (``ps -p PID -o etime=``) в пределах ``LOCK_START_TOLERANCE_SEC``.

    Иначе файл устаревший (pid переиспользован после ребута, или там pid чужого
    процесса — например, 1/launchd) и его нужно перезаписать с предупреждением,
    а не блокировать запуск навсегда.
    """
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid() or not _pid_alive(pid):
        return False
    command = _proc_command(pid)
    if command is not None and _looks_like_agent(command):
        return True
    started_at = _parse_lock_ts(payload.get("started_at"))
    if started_at is None:
        return False
    elapsed = _proc_elapsed_sec(pid)
    if elapsed is None:
        return False
    real_start = _lock_now() - timedelta(seconds=elapsed)
    return abs((real_start - started_at).total_seconds()) <= LOCK_START_TOLERANCE_SEC


class PidLock:
    """Атомарный pid-лок: не даёт запустить второй экземпляр агента.

    Владение определяется ``fcntl.flock(LOCK_EX|LOCK_NB)`` на открытом файле
    ``data/lifeos.pid``, а не содержимым файла: гонки «прочитал pid → записал
    свой» (TOCTOU) больше нет — из двух одновременных стартов лок возьмёт ровно
    один процесс, второй получает отказ, не сделав ни одной записи. Содержимое
    файла (``pid`` + ``started_at``) нужно для диагностики и для случая живого
    процесса старой версии, который flock ещё не держит.

    Файл при выходе не удаляется: удаление пути, пока на inode висит flock,
    открывает гонку с новым файлом по тому же пути. Пустой файл без flock —
    безопасный признак «свободно»; устаревший pid перезаписывается с
    предупреждением (см. :func:`_owner_is_live`).
    """

    def __init__(self, path: str | Path, logger: logging.Logger | None = None) -> None:
        self.path = Path(path)
        self.pid = os.getpid()
        self.acquired = False
        self.payload: dict = {}
        self.log = logger
        self._fd: int | None = None

    def _warn(self, message: str, *args) -> None:
        if self.log is not None:
            self.log.warning(message, *args)

    def acquire(self) -> tuple[bool, str]:
        """Атомарно берёт лок. Возвращает (успех, причина отказа)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, "не удалось создать каталог для pid-файла %s: %s" % (
                self.path.parent, exc,
            )
        try:
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, LOCK_FILE_MODE)
        except OSError as exc:
            return False, "не удалось открыть pid-файл %s: %s" % (self.path, exc)
        try:
            os.fchmod(fd, LOCK_FILE_MODE)  # файл старой версии мог быть 0644
        except OSError:
            pass

        # 1. Атомарный захват: единственное место, где решается владение.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # Владелец пишет свой pid уже после захвата лока, поэтому первые
            # миллисекунды файл может быть пустым — перечитываем чуть-чуть.
            holder: dict = {}
            for _attempt in range(4):
                holder = _parse_lock_payload(_read_fd_text(fd))
                if holder.get("pid"):
                    break
                time.sleep(0.01)
            try:
                os.close(fd)
            except OSError:
                pass
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                pid = holder.get("pid")
                who = "pid %s, " % pid if pid else ""
                return False, (
                    "уже запущен другой экземпляр агента (%sлок %s занят). "
                    "Второй экземпляр не запускается: он дублировал бы сессии и "
                    "записи. Остановите первый процесс (launchctl bootout или "
                    "SIGTERM) и повторите запуск." % (who, self.path)
                )
            return False, "не удалось взять pid-лок %s: %s" % (self.path, exc)

        # 2. Лок наш. В файле мог остаться pid предыдущего запуска.
        stale = _parse_lock_payload(_read_fd_text(fd))
        if _owner_is_live(stale):
            # Процесс старой версии (без flock) жив и работает с этим проектом.
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False, (
                "уже запущен экземпляр агента (pid %s, файл %s): процесс жив, "
                "хотя лок ядром не удерживается (старая версия без flock). "
                "Остановите его и повторите запуск." % (stale.get("pid"), self.path)
            )
        if stale:
            self._warn(
                "Pid-лок: устаревший pid-файл %s (pid %s, started_at %s) — "
                "процесс мёртв или pid чужой, перезаписываю своим.",
                self.path, stale.get("pid", "—"), stale.get("started_at", "—"),
            )

        # 3. Пишем свой pid+started_at (только под flock).
        payload = {
            "pid": self.pid,
            "started_at": _lock_now().isoformat(timespec="seconds"),
            "app": "lifeos",
        }
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError as exc:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False, "не удалось записать pid-файл %s: %s" % (self.path, exc)

        self._fd = fd
        self.acquired = True
        self.payload = payload
        return True, ""

    def release(self) -> None:
        """Освобождает лок: обнуляет содержимое файла и снимает flock."""
        fd, self._fd = self._fd, None
        self.acquired = False
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# ============================================================== ЛОГИРОВАНИЕ
def _setup_logging(cfg: dict, console_level: int = logging.WARNING) -> logging.Logger:
    """Логи в ``<root>/<paths.logs>/agent.log`` (ротация 5 МБ) — на корне.

    Файловый и консольный handlers вешаются на корневой logger, чтобы в лог
    попадали не только ``lifeos.*``, но и ``src.llm`` / ``src.parsing`` /
    ``src.dashboard_data`` (REVIEW P1-4). Повторный вызов не плодит handlers:
    добавленные здесь помечаются и снимаются.
    """
    env_logs = os.environ.get("LIFEOS_LOG_DIR", "").strip()
    logs_value = env_logs or cfg["paths"]["logs"]
    logs_dir = Path(config_mod.resolve_path(cfg, logs_value))
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # не фатально: останутся только stderr-логи
        print("Не удалось создать каталог логов %s: %s" % (logs_dir, exc), file=sys.stderr)
    log_path = logs_dir / "agent.log"

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    root = logging.getLogger()

    # Снимаем только те handlers, которые поставили мы сами (иначе при
    # повторном вызове в одном процессе файл/open дублировались бы).
    for handler in list(root.handlers):
        if getattr(handler, "_lifeos_managed", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass

    root.setLevel(logging.INFO)
    try:
        file_handler = RotatingFileHandler(
            str(log_path), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        file_handler._lifeos_managed = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)
    except OSError as exc:
        print("Не удалось открыть лог-файл %s: %s" % (log_path, exc), file=sys.stderr)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(console_level)
    console._lifeos_managed = True  # type: ignore[attr-defined]
    root.addHandler(console)

    # lifeos.* пишет в корень; своих handlers у него больше нет.
    logger = logging.getLogger("lifeos")
    logger.setLevel(logging.INFO)
    logger.propagate = True
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
    return logger


def _vault_call(vault, method_name: str, logger: logging.Logger, *args, **kwargs):
    """Безопасно вызывает метод Vault: vault может быть None или урезанным.

    Нужен, чтобы отсутствие/поломка Obsidian не мешали боту работать.
    """
    if vault is None:
        return None
    method = getattr(vault, method_name, None)
    if not callable(method):
        return None
    try:
        return method(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("Vault.%s завершился ошибкой — продолжаю работу", method_name)
        return None


def _app_timezone(cfg: dict) -> ZoneInfo:
    """Таймзона приложения из ``app.timezone`` (fallback — Москва)."""
    name = str((cfg.get("app") or {}).get("timezone") or "Europe/Moscow").strip()
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — config уже проверил, это страховка
        return ZoneInfo("Europe/Moscow")


def _startup_banner(cfg: dict) -> str:
    """Строка «кто и где запустился» для лога: версия, pid, таймзона.

    Без неё по ``agent.log`` нельзя понять, какой именно экземпляр и с какой
    таймзоной работает: после обновления/перезапуска launchd строки старых и
    новых запусков перемешаны. Секретов в строке нет.
    """
    tz_name = str((cfg.get("app") or {}).get("timezone") or "Europe/Moscow").strip()
    try:
        moment = datetime.now(_app_timezone(cfg))
        offset = moment.strftime("%z") or "?"
        stamp = moment.isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001 — баннер не должен мешать запуску
        offset = "?"
        stamp = datetime.now().isoformat(timespec="seconds")
    py_version = sys.version.split()[0] if sys.version else "?"
    return "Life OS v%s: pid %d, python %s, таймзона %s (UTC%s), старт %s" % (
        APP_VERSION, os.getpid(), py_version, tz_name or "?", offset, stamp,
    )


def _kv_get_int(storage, key: str) -> int | None:
    """Безопасно читает неотрицательное int из kv (пусто/битое → None)."""
    if storage is None:
        return None
    try:
        raw = storage.kv_get(key)
    except Exception:  # noqa: BLE001 — БД может быть закрыта
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _kv_set_int(storage, key: str, value: int, logger: logging.Logger) -> None:
    """Пишет int в kv, не роняя цикл из-за ошибки БД."""
    if storage is None or value is None:
        return
    try:
        storage.kv_set(key, str(int(value)))
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось сохранить %s в kv", key)


def _is_within(candidate: Path, base) -> bool:
    """Лежит ли ``candidate`` внутри ``base`` (без учёта симлинков в хвосте)."""
    if not base:
        return False
    try:
        root = Path(base).resolve()
        target = Path(candidate).resolve()
    except (OSError, RuntimeError):  # pragma: no cover — экзотические ФС
        return False
    return target == root or root in target.parents


def _harden_paths(db_path: str, logger: logging.Logger, root=None) -> None:
    """Права на каталог БД (0700) и файлы базы (0600, включая ``-wal``/``-shm``).

    ``umask(077)`` уже не даёт новым файлам стать доступными другим, но
    каталог мог быть создан раньше с 0755 (например, установщиком старой
    версии), а файлы БД — до появления umask. Поэтому права выставляем явно
    и идемпотентно. Каталог ужимаем до 700 только если он внутри проекта
    (``root``): чужой общий каталог (``~/Documents``) трогать нельзя.
    """
    if not db_path or db_path == ":memory:":
        return
    path = Path(db_path)
    parent = path.parent
    try:
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True, mode=DATA_DIR_MODE)
        if parent.is_dir() and _is_within(parent, root):
            # Каталог данных проекта — доводим права до «только владельцу».
            os.chmod(str(parent), DATA_DIR_MODE)
    except OSError as exc:
        logger.warning(
            "Не удалось выставить права 700 на каталог данных %s: %s", parent, exc
        )
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        try:
            if candidate.exists():
                os.chmod(str(candidate), DATA_FILE_MODE)
        except OSError as exc:
            logger.warning("Не удалось выставить 600 на %s: %s", candidate, exc)


def _preview_text(text, limit: int = DRY_RUN_PREVIEW_CHARS) -> str:
    """Короткое представление сообщения для лога: длина + первые ``limit`` символов.

    В режиме ``--dry-run`` в лог попадает личный текст, поэтому полный текст
    не пишем: только длину и начало.
    """
    value = _strip_surrogates(text)
    flat = " ".join(value.split())
    head = flat[:limit]
    tail = "…" if len(flat) > limit else ""
    return "%d симв.: %s%s" % (len(value), head, tail)


class DryRunTelegram:
    """Заглушка Telegram для --dry-run: сообщения попадают в лог и в ``sent``."""

    def __init__(self, logger: logging.Logger) -> None:
        self.log = logger
        self.sent: list[str] = []

    def send(self, chat_id: int, text: str) -> None:
        self.sent.append(str(text))
        # В лог — не весь текст: длина и первые 60 символов (см. _preview_text).
        self.log.info(
            "[dry-run] → чат %s, сообщение %s", chat_id, _preview_text(text)
        )

    def get_updates(self, offset: int | None, timeout: int = 20) -> list[dict]:
        self.log.info("[dry-run] getUpdates пропущен")
        return []

    def call(self, method: str, payload: dict | None = None,
             timeout: int = 60) -> dict:
        self.log.info("[dry-run] вызов Telegram API «%s» пропущен", method)
        return {}


# ============================================================== УСТОЙЧИВОСТЬ
class Backoff:
    """Экспоненциальная пауза между повторами с потолком.

    Ряд пауз: ``2, 4, 8, 16, 32, 60, 60…`` секунд (``BACKOFF_BASE_S`` ×
    ``BACKOFF_FACTOR``, не больше ``BACKOFF_CAP_S``). Если сервер сам сказал,
    сколько ждать (``retry_after`` у 429), пауза берётся не меньше подсказки —
    но и не больше ``RETRY_AFTER_MAX_S``, чтобы бот не замер на часы.

    После успешного шага вызывается :meth:`reset` — следующий сбой снова
    начинается с короткой паузы, а не с «накопленной» длинной.
    """

    def __init__(self, base=None, factor=None, cap=None) -> None:
        self.base = BACKOFF_BASE_S if base is None else max(0.0, float(base))
        self.factor = BACKOFF_FACTOR if factor is None else max(1.0, float(factor))
        self.cap = BACKOFF_CAP_S if cap is None else max(self.base, float(cap))
        self.attempt = 0

    def __repr__(self) -> str:
        return "Backoff(base=%s, factor=%s, cap=%s, attempt=%s)" % (
            self.base, self.factor, self.cap, self.attempt,
        )

    def reset(self) -> None:
        """Сбрасывает счётчик (успешный шаг: снова начинаем с короткой паузы)."""
        self.attempt = 0

    def peek(self) -> float:
        """Пауза, которую вернул бы следующий :meth:`next` (без изменений)."""
        return min(self.cap, self.base * (self.factor ** self.attempt))

    def next(self, retry_after=None) -> float:
        """Возвращает паузу и сдвигает счётчик.

        Args:
            retry_after: сколько секунд подождать по мнению сервера
                (``TelegramError.retry_after``); мусор игнорируется.

        Returns:
            Пауза в секундах: не меньше подсказки сервера, не больше
            ``RETRY_AFTER_MAX_S``.
        """
        wait = self.peek()
        self.attempt += 1
        if retry_after is None:
            return wait
        try:
            hinted = float(retry_after)
        except (TypeError, ValueError):
            return wait
        if hinted <= 0 or hinted != hinted:  # NaN тоже отбрасываем
            return wait
        return max(wait, min(hinted, RETRY_AFTER_MAX_S))


# ================================================================== ЦИКЛ
def _sleep(stop: dict, seconds: float) -> None:
    """Сон с реакцией на сигнал остановки (дробим на короткие куски)."""
    if seconds <= 0:
        return
    deadline = time.time() + seconds
    while not stop["flag"] and time.time() < deadline:
        time.sleep(min(0.25, max(0.0, deadline - time.time())))


def _handle_updates(updates: list, offset: int | None, scheduler,
                    tz: ZoneInfo, logger: logging.Logger, storage=None,
                    last_update_id: int | None = None
                    ) -> tuple[int | None, int | None]:
    """Разбирает апдейты Telegram, возвращает ``(offset, last_update_id)``.

    Offset сохраняется в kv после обработки пачки, ``last_update_id`` — после
    каждого апдейта. Второй нужен для дедупликации, если Telegram переотдаст
    уже обработанный апдейт после рестарта/сбоя (REVIEW P1-1).
    """
    advanced = False
    for update in updates:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        uid = update_id if isinstance(update_id, int) and not isinstance(update_id, bool) else None

        # Дедупликация: всё, что уже обрабатывали, повторно не исполняем.
        if uid is not None and last_update_id is not None and uid <= last_update_id:
            logger.info("Пропускаю повторный update_id=%d (уже обработан)", uid)
            offset = max(offset or 0, uid + 1)
            advanced = True
            continue

        if uid is not None:
            offset = uid + 1
        try:
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            chat_id = chat.get("id") if isinstance(chat, dict) else None
            if chat_id is None:
                continue
            text = message.get("text")
            # Lone surrogate из «битого» апдейта вычищаем до сохранения:
            # иначе первый же encode("utf-8") (SQLite/лог/Telegram) падает.
            scheduler.on_message(
                int(chat_id),
                _strip_surrogates(text) if isinstance(text, str) else "",
                datetime.now(tz),
            )
        except Exception:  # noqa: BLE001 — один кривой апдейт не должен ронять цикл
            logger.exception("Ошибка обработки обновления Telegram")
        finally:
            # Отмечаем апдейт обработанным даже при ошибке: иначе «ядовитый»
            # апдейт будет вечно переотдаваться и блокировать очередь.
            if uid is not None:
                advanced = True
                if last_update_id is None or uid > last_update_id:
                    last_update_id = uid
                    _kv_set_int(storage, "last_update_id", last_update_id, logger)

    if advanced and offset is not None:
        _kv_set_int(storage, "tg_offset", offset, logger)
    return offset, last_update_id


def main(argv: list[str] | None = None) -> int:
    # 0. Права «только владельцу» — до создания любых файлов и каталогов.
    #    Иначе БД, логи и дашборд появляются с umask 022 (0644/0755).
    os.umask(PRIVATE_UMASK)

    parser = argparse.ArgumentParser(
        prog="lifeos",
        description="Life OS — персональный Telegram-агент (диалоги, Obsidian, дашборд).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="не отправлять в Telegram, писать сообщения в лог")
    parser.add_argument("--once", action="store_true",
                        help="один цикл (getUpdates + tick) и выход")
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="путь к JSON-конфигу (по умолчанию config/config.json)")
    parser.add_argument("--mock-url", metavar="URL", default=None,
                        help="подменить telegram.api_base (для мок-сервера, e2e)")
    args = parser.parse_args(argv)

    # 1. Конфиг — до логгера, пути логов задаются в конфиге.
    try:
        cfg = config_mod.load_config(args.config)
    except config_mod.ConfigError as exc:
        print("Ошибка конфигурации: %s" % exc, file=sys.stderr)
        return 2

    if args.mock_url:
        cfg["telegram"]["api_base"] = args.mock_url.strip()

    logger = _setup_logging(
        cfg, console_level=logging.INFO if args.dry_run else logging.WARNING
    )
    logger.info(
        "Life OS запускается: root=%s, dry_run=%s, once=%s",
        cfg.get("_root"), args.dry_run, args.once,
    )
    logger.info("Конфиг: %s", cfg.get("_config_path"))
    # Версия, pid и таймзона — первая строка каждого запуска (v0.3): по логу
    # видно, какой экземпляр работает и с какой зоной считаются слоты.
    logger.info("%s", _startup_banner(cfg))

    # 1a. --dry-run полностью изолирован от боевого состояния (REVIEW P1-2):
    # своя временная БД (вместе с kv), без Obsidian и без платных вызовов LLM.
    dry_run_dir: str | None = None
    if args.dry_run:
        try:
            dry_run_dir = tempfile.mkdtemp(prefix="lifeos-dryrun-")
        except OSError as exc:
            logger.error("Dry-run: не удалось создать временный каталог: %s", exc)
            return 2
        cfg["paths"]["db"] = os.path.join(dry_run_dir, "lifeos.db.dryrun")
        cfg["paths"]["dashboard_out"] = os.path.join(
            dry_run_dir, "dashboard", "index.html"
        )
        cfg.setdefault("obsidian", {})["enabled"] = False
        cfg.setdefault("llm", {})["enabled"] = False
        # На случай аварийного выхода (SIGKILL/ранний return) — уберёт temp.
        atexit.register(shutil.rmtree, dry_run_dir, True)
        logger.info(
            "DRY-RUN: состояние не сохраняется — боевые БД/kv и Obsidian не трогаю; "
            "временные данные: %s",
            dry_run_dir,
        )

    # 1b. Pid-лок — ДО любой работы с vault, БД и дашбордом (F5 адверсариального
    # теста). Раньше дозапись в Obsidian (sync_pending) и открытие БД шли до
    # проверки лока, поэтому проигравший второй экземпляр успевал добавить
    # запись в дневник, прежде чем выйти с кодом 3. Лок берём и в --dry-run:
    # два одновременных прогона всё равно конкурируют за один проект, а
    # проигравший должен получить понятный отказ, а не молча работать вторым.
    lock = PidLock(_ROOT / PID_RELATIVE_PATH, logger)
    ok, reason = lock.acquire()
    if not ok:
        if args.dry_run:
            # Бот уже работает: dry-run ничего не проверяет повторно, но и
            # пугать пользователя кодом 3 не нужно — это безопасная проверка.
            logger.warning("Пропускаю --dry-run: %s", reason)
            return 0
        logger.error("Запуск отменён: %s", reason)
        return 3
    # Страховка от незакрытого лока на любом раннем return или исключении.
    atexit.register(lock.release)
    logger.info("Pid-лок взят: %s (pid %d)", lock.path, lock.pid)

    # 2. Модули проекта (свои и смежников — по контрактам INTERFACES).
    try:
        from src import scheduler as scheduler_mod
        from src import storage as storage_mod
        from src import telegram as telegram_mod
    except Exception as exc:  # noqa: BLE001 — ImportError и ошибки уровня модулей
        logger.error(
            "Не удалось загрузить модули проекта: %s. "
            "Проверьте, что в src/ лежат все файлы (llm.py, prompts.py, parsing.py и т.д.).",
            exc,
        )
        return 2

    # 3. Проверка «живого» режима: без токена и chat_id работать некуда.
    token = str((cfg.get("telegram") or {}).get("token") or "").strip()
    try:
        chat_id = int((cfg.get("telegram") or {}).get("allowed_chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if not args.dry_run:
        if not token:
            logger.error(
                "Не задан telegram.token в config/config.json. "
                "Создайте бота у @BotFather и вставьте токен (см. README). "
                "Для проверки без Telegram запустите с --dry-run."
            )
            return 2
        if not chat_id:
            logger.error(
                "Не задан telegram.allowed_chat_id в config/config.json — "
                "бот не знает, кому писать. Узнать свой id: @userinfobot."
            )
            return 2

    # 4. Хранилище: время пишется в таймзоне приложения (а не жёстко МСК).
    tz = _app_timezone(cfg)
    db_path = config_mod.resolve_path(cfg, cfg["paths"]["db"])
    # Каталог с базой — только для владельца, ещё до открытия БД (см. P2 аудита:
    # storage.py создаёт каталог с umask по умолчанию).
    _harden_paths(db_path, logger, cfg.get("_root"))
    try:
        storage = storage_mod.Storage(db_path, tz=tz)
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось открыть базу данных %s: %s", db_path, exc)
        return 2
    # Ещё раз: после первого обращения уже существуют lifeos.db, -wal и -shm.
    _harden_paths(db_path, logger, cfg.get("_root"))
    logger.info("База данных: %s (таймзона: %s)", db_path, tz)

    # Восстанавливаем позицию в очереди Telegram и защиту от повторной
    # доставки (REVIEW P1-1). Битое/пустое значение молча считаем отсутствием.
    offset = _kv_get_int(storage, "tg_offset")
    if offset is not None and offset <= 0:
        offset = None
    last_update_id = _kv_get_int(storage, "last_update_id")
    if offset is not None and last_update_id is None:
        # offset уже подтверждает всё, что меньше него.
        last_update_id = offset - 1
    if offset is not None or last_update_id is not None:
        logger.info(
            "Telegram: восстановлено из kv offset=%s, last_update_id=%s",
            offset, last_update_id,
        )

    # 5. LLM (мозг). Без модуля работать нечему — это ошибка сборки проекта.
    try:
        from src import llm as llm_mod

        llm = llm_mod.LLM(cfg)
        if args.dry_run:
            logger.info("LLM: выключен (dry-run) — платных вызовов не будет")
        if llm.available():
            logger.info("LLM доступен: %s", (cfg.get("llm") or {}).get("model") or "—")
        else:
            logger.warning(
                "LLM выключен или нет api_key — работаю в скриптованном режиме "
                "(вопросы по расписанию, разбор ответов эвристикой)."
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось подключить LLM (src/llm.py): %s", exc)
        storage.close()
        return 2

    # 6. Obsidian и дашборд — опциональные части: без них бот живёт.
    # В dry-run Vault вообще не создаём: ни чтения, ни записи, ни каталогов.
    vault = None
    if args.dry_run:
        logger.info("Obsidian: выключен (dry-run) — vault не читается и не пишется")
    else:
        try:
            from src import obsidian as obsidian_mod

            vault = obsidian_mod.Vault(cfg, storage=storage)
            obsidian_cfg = cfg.get("obsidian") or {}
            vault_path = config_mod.resolve_path(cfg, str(obsidian_cfg.get("vault_path") or "."))
            if obsidian_cfg.get("enabled", True) and not Path(vault_path).is_dir():
                logger.warning(
                    "Папка Obsidian не найдена: %s — записи будут копиться в БД "
                    "и допишутся, когда база появится.",
                    vault_path,
                )
            logger.info(
                "Obsidian: %s (%s)",
                vault_path, "включён" if obsidian_cfg.get("enabled", True) else "выключен",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Модуль Obsidian недоступен — запись в дневник отключена: %s", exc)

    # Дозапись того, что не дошло до Obsidian в прошлом запуске (после сбоя, без сети и т.п.).
    synced = _vault_call(vault, "sync_pending", logger)
    if synced:
        logger.info("При старте досинхронизировано записей в Obsidian: %s", synced)

    dash_build = None
    try:
        from src import dashboard_data as dash_mod

        dash_build = dash_mod.build_dashboard
    except Exception as exc:  # noqa: BLE001
        logger.warning("Модуль дашборда недоступен: %s", exc)

    # 7. Telegram-клиент.
    if args.dry_run:
        tg = DryRunTelegram(logger)
        logger.info("Telegram: DRY-RUN (сообщения только в лог)")
    else:
        try:
            tg = telegram_mod.Telegram(token, api_base=cfg["telegram"]["api_base"])
        except telegram_mod.TelegramError as exc:
            logger.error("%s", exc)
            storage.close()
            return 2
        logger.info("Telegram: боевой режим, chat_id=%s, api_base=%s",
                    chat_id, cfg["telegram"]["api_base"])

    # 8. Планировщик.
    scheduler = scheduler_mod.Scheduler(
        cfg, storage, tg, llm, vault, dash_build, logging.getLogger("lifeos.scheduler")
    )

    # 9. Дневной ролловер (sync_pending, rewrite_goals, дашборд) выполняет
    # scheduler.tick() — по контракту INTERFACES §8 п.3; main вызывает tick()
    # на каждом проходе цикла. Сам pid-лок взят раньше (шаг 1b), до любых
    # записей: второй экземпляр не успевает ничего записать в vault/БД.

    # 10. Graceful shutdown по SIGTERM/SIGINT.
    stop = {"flag": False}

    def _handle_signal(signum, _frame) -> None:
        stop["flag"] = True
        logger.info("Получен сигнал %s — завершаюсь после текущего шага", signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):  # не главный поток (например, тесты)
            pass

    auth_errors = 0
    # 0 — Telegram не держит запрос (--once); иначе короче 20 c, чтобы SIGTERM
    # успевал до «жёсткого» убийства от launchd (ExitTimeOut по умолчанию 20 c).
    poll_timeout = 0 if args.once else POLL_TIMEOUT_S
    backoff = Backoff()
    logger.info("Цикл запущен (timeout=%s c, тик каждые %s c, максимум паузы %.0f c)",
                poll_timeout, 0 if args.once else 1, BACKOFF_CAP_S)

    exit_code = 0
    try:
        while not stop["flag"]:
            # --- 1. Приём обновлений -------------------------------------
            try:
                updates = tg.get_updates(offset, timeout=poll_timeout)
            except telegram_mod.TelegramError as exc:
                status = exc.status
                if status == 409:
                    # Второй экземпляр держит тот же getUpdates: ждём по
                    # нарастающей и не трогаем лок/данные.
                    pause = backoff.next()
                    logger.warning("%s Пауза %.0f с.", exc, pause)
                    _sleep(stop, 0 if args.once else pause)
                elif status == 429:
                    pause = backoff.next(exc.retry_after)
                    logger.warning("Telegram rate limit (429): пауза %.0f с", pause)
                    _sleep(stop, 0 if args.once else pause)
                elif status in (401, 403):
                    auth_errors += 1
                    logger.error(
                        "Ошибка авторизации Telegram (%s), попытка %d из 3: %s",
                        status, auth_errors, exc,
                    )
                    if auth_errors >= 3:
                        logger.error(
                            "Три ошибки авторизации подряд — останавливаюсь. "
                            "Проверьте telegram.token в config/config.json."
                        )
                        exit_code = 1
                        break
                    pause = backoff.next()
                    _sleep(stop, 0 if args.once else pause)
                else:
                    pause = backoff.next()
                    logger.warning("Ошибка Telegram: %s Пауза %.0f с.", exc, pause)
                    _sleep(stop, 0 if args.once else pause)
            except Exception as exc:  # noqa: BLE001
                pause = backoff.next()
                logger.exception("Неожиданная ошибка при опросе Telegram: %s", exc)
                _sleep(stop, 0 if args.once else pause)
            else:
                auth_errors = 0
                backoff.reset()  # связь восстановилась — снова короткие паузы
                offset, last_update_id = _handle_updates(
                    updates, offset, scheduler, tz, logger,
                    storage=storage, last_update_id=last_update_id,
                )

            # --- 2. Тик планировщика ------------------------------------
            try:
                scheduler.tick(datetime.now(tz))
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка в планировщике — продолжаю работу")

            if args.once:
                break
            _sleep(stop, 1)
    except KeyboardInterrupt:
        logger.info("Прервано пользователем (Ctrl+C)")
    finally:
        logger.info("Останавливаюсь: закрываю БД и снимаю pid-лок")
        try:
            storage.close()
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка при закрытии БД")
        lock.release()
        if dry_run_dir:
            try:
                shutil.rmtree(dry_run_dir, ignore_errors=True)
                logger.info("DRY-RUN: временные данные удалены (боевое состояние не менялось)")
            except Exception:  # noqa: BLE001
                logger.exception("Dry-run: не удалось удалить временный каталог")
        logger.info("Life OS остановлен")

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
