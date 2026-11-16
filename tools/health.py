#!/usr/bin/env python3
"""Самопроверка Life OS одной командой.

Запуск из папки проекта:

    python3 tools/health.py
    python3 tools/health.py --config ~/Library/Application\\ Support/LifeOS/config/config.json
    python3 tools/health.py --rotate     # убрать в .1 логи больше 5 МБ

Что проверяется:

1. python3 не старее 3.9 (на более старом проект просто не запустится);
2. конфиг: файл найден, разбирается, обязательные поля заполнены;
3. база: файл открывается, запись и чтение работают;
4. планировщик: pid-лок ``data/lifeos.pid`` и состояние LaunchAgent;
5. последний успешный тик (kv ``tick:last_ok``) и последняя ошибка тика;
6. размеры логов и их архивы (ротация после 5 МБ, архив один — ``.1``).

Вывод человекочитаемый: ``[ OK ]``, ``[WARN]`` (замечание, работа не ломается)
и ``[FAIL]`` (проблема). Код выхода: 0 — проблем нет, 1 — есть хотя бы один
``[FAIL]``. Секреты (токен, api_key) не печатаются никогда.

Только стандартная библиотека, совместимо с Python 3.9.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: Минимальная версия Python, на которой работает проект.
MIN_PYTHON = (3, 9)
#: Порог ротации лога: больше — переименовываем в ``.1`` и пишем с начала.
LOG_ROTATE_BYTES = 5 * 1024 * 1024
#: Планировщик берёт лок на этом файле (см. src/main.py, PID_RELATIVE_PATH).
PID_RELATIVE_PATH = "data/lifeos.pid"
#: Метка LaunchAgent из ops/com.lifeos.agent.plist.
LAUNCHD_LABEL = "com.lifeos.agent"
#: Ключи «пульса» планировщика (см. src/scheduler.py).
KV_HEARTBEAT = "tick:last_ok"
KV_TICK_ERROR = "tick:last_error"
#: Ключ-проба для проверки записи в БД; после проверки удаляется.
KV_PROBE = "health:probe"
#: Сколько минут без успешного тика считаем зависанием запущенного бота.
STALE_TICK_MINUTES = 10.0
#: Подстроки командной строки процесса, по которым видно, что это наш агент.
AGENT_MARKERS = ("lifeos", "telegram-life-agent", "src/main.py")

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

_LABELS = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


# ===========================================================================
# Результаты проверок
# ===========================================================================
class Check:
    """Итог одной проверки: статус, короткое имя и подробность."""

    __slots__ = ("status", "name", "detail")

    def __init__(self, status, name, detail=""):
        self.status = status
        self.name = name
        self.detail = detail

    def __repr__(self):
        return "Check(%r, %r, %r)" % (self.status, self.name, self.detail)

    @property
    def failed(self):
        return self.status == FAIL

    def line(self):
        label = _LABELS.get(self.status, self.status)
        if self.detail:
            return "%s %s — %s" % (label, self.name, self.detail)
        return "%s %s" % (label, self.name)


def human_size(size):
    """Размер в человекочитаемом виде (байты → КБ/МБ с одной десятой)."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "?"
    if value < 1024:
        return "%d Б" % int(value)
    if value < 1024 * 1024:
        return "%.1f КБ" % (value / 1024.0)
    return "%.1f МБ" % (value / (1024.0 * 1024.0))


def _short_time(seconds):
    """Сколько прошло: «12 с», «5 мин», «3 ч 20 мин» (без секунд у больших)."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if value < 0:
        value = 0.0
    if value < 90:
        return "%d с" % int(value)
    minutes = value / 60.0
    if minutes < 90:
        return "%d мин" % int(minutes)
    hours = int(minutes // 60)
    rest = int(minutes - hours * 60)
    if hours < 48:
        return "%d ч %d мин" % (hours, rest) if rest else "%d ч" % hours
    return "%d дн" % int(hours // 24)


def _parse_moment(value):
    """Разбирает ISO-время из kv в datetime с таймзоной (или None)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        try:
            moment = moment.astimezone()
        except (OSError, ValueError):  # pragma: no cover — экзотические TZ
            return None
    return moment


# ===========================================================================
# 1. Python
# ===========================================================================
def check_python():
    """Версия интерпретатора: старее 3.9 проект не запустится."""
    version = "%d.%d.%d" % sys.version_info[:3]
    exe = sys.executable or "python3"
    if sys.version_info[:2] < MIN_PYTHON:
        return Check(FAIL, "python",
                     "%s (%s) — нужен Python %d.%d или новее"
                     % (version, exe, MIN_PYTHON[0], MIN_PYTHON[1]))
    return Check(OK, "python", "%s (%s)" % (version, exe))


# ===========================================================================
# 2. Конфиг
# ===========================================================================
def check_config(config_path=None):
    """Загружает конфиг и проверяет обязательные поля.

    Returns:
        (Check, cfg или None). cfg передаётся дальше — остальные проверки
        берут из него пути к базе и логам.
    """
    from src import config as config_mod

    try:
        cfg = config_mod.load_config(config_path)
    except config_mod.ConfigError as exc:
        return Check(FAIL, "конфиг", "%s" % exc), None
    except Exception as exc:  # noqa: BLE001 — битый модуль не должен ронять health
        return Check(FAIL, "конфиг", "не читается (%s)" % type(exc).__name__), None

    where = str(cfg.get("_config_path") or "?")
    problems = []
    hints = []

    telegram = cfg.get("telegram") or {}
    token = str(telegram.get("token") or "").strip()
    try:
        chat_id = int(telegram.get("allowed_chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if not token:
        problems.append("не задан telegram.token")
    if chat_id <= 0:
        problems.append("не задан telegram.allowed_chat_id")

    tz_name = str((cfg.get("app") or {}).get("timezone") or "").strip()
    if not tz_name:
        problems.append("не задан app.timezone")
    else:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            problems.append("неизвестная таймзона app.timezone=%s" % tz_name)

    schedule = cfg.get("schedule") or {}
    enabled = []
    for name, spec in schedule.items():
        if isinstance(spec, dict) and spec.get("enabled"):
            enabled.append(name)
    if not enabled:
        problems.append("ни один слот расписания не включён")

    if where.endswith(".example.json"):
        hints.append("используется образец конфига — заполните config/config.json")

    llm_cfg = cfg.get("llm") or {}
    llm_on = bool(llm_cfg.get("enabled", True)) and bool(str(llm_cfg.get("api_key") or "").strip())
    if not llm_on:
        hints.append("LLM выключен или без api_key — бот работает в скриптованном режиме")

    if problems:
        return Check(FAIL, "конфиг", "%s: %s" % (where, "; ".join(problems))), cfg

    detail = "%s; token задан, chat_id %d, таймзона %s, слоты: %s" % (
        where, chat_id, tz_name, ", ".join(sorted(enabled)),
    )
    if hints:
        return Check(WARN, "конфиг", detail + "; " + "; ".join(hints)), cfg
    return Check(OK, "конфиг", detail), cfg


# ===========================================================================
# 3. База
# ===========================================================================
def _db_path(cfg):
    from src import config as config_mod

    value = str((cfg.get("paths") or {}).get("db") or "").strip()
    if not value:
        return ""
    return config_mod.resolve_path(cfg, value)


def check_storage(cfg):
    """Открывает БД и проверяет запись с чтением (без потери данных).

    Returns:
        (Check, Storage или None). Открытую базу закрывает вызывающий: она
        нужна проверке «последний тик».
    """
    from src import storage as storage_mod

    db_path = _db_path(cfg)
    if not db_path:
        return Check(FAIL, "база", "в конфиге нет paths.db"), None

    if not os.path.exists(db_path):
        parent = os.path.dirname(db_path) or "."
        if os.path.isdir(parent) and os.access(parent, os.W_OK):
            return Check(
                WARN, "база",
                "%s ещё нет — создастся при первом запуске бота (каталог доступен)"
                % db_path,
            ), None
        return Check(FAIL, "база", "%s нет, и каталог %s недоступен для записи"
                                   % (db_path, parent)), None

    size = human_size(os.path.getsize(db_path)) if os.path.isfile(db_path) else "?"
    try:
        storage = storage_mod.Storage(db_path)
    except Exception as exc:  # noqa: BLE001 — битый/чужой файл, права, занятость
        return Check(FAIL, "база", "%s не открывается (%s)" % (db_path, type(exc).__name__)), None

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        storage.kv_set(KV_PROBE, stamp)
        read_back = storage.kv_get(KV_PROBE)
        storage.kv_del(KV_PROBE)
    except Exception as exc:  # noqa: BLE001
        try:
            storage.close()
        except Exception:  # noqa: BLE001
            pass
        return Check(FAIL, "база", "%s: запись/чтение не работают (%s)"
                                   % (db_path, type(exc).__name__)), None

    if read_back != stamp:
        try:
            storage.close()
        except Exception:  # noqa: BLE001
            pass
        return Check(FAIL, "база", "%s: записанное значение не прочиталось" % db_path), None

    return Check(OK, "база", "%s (%s), запись и чтение ок" % (db_path, size)), storage


# ===========================================================================
# 4. Планировщик: pid-лок и launchd
# ===========================================================================
def parse_pid_file(raw):
    """Достаёт pid из содержимого ``data/lifeos.pid`` (JSON или просто число)."""
    text = (raw or "").strip()
    if not text:
        return 0
    if text.startswith("{"):
        import json

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return 0
        if isinstance(data, dict):
            try:
                pid = int(data.get("pid") or 0)
            except (TypeError, ValueError):
                return 0
            return pid if pid > 0 else 0
    first = text.splitlines()[0].strip()
    try:
        pid = int(first)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


def pid_alive(pid):
    """Жив ли процесс с таким pid (POSIX, без внешних утилит)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
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


def _proc_command(pid):
    """Командная строка процесса через ``ps`` (в macOS нет ``/proc``)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def looks_like_agent(command):
    """Похожа ли командная строка процесса на Life OS (а не на чужой pid)."""
    low = (command or "").lower()
    return any(marker in low for marker in AGENT_MARKERS)


def agent_running(pid):
    """Работает ли агент именно с этим pid (защита от переиспользованного pid)."""
    if not pid_alive(pid):
        return False
    command = _proc_command(pid)
    if command is None:
        # ps недоступен — доверяем факту живого pid (как и PidLock).
        return True
    return looks_like_agent(command)


def launchd_state():
    """Состояние LaunchAgent: ``(загружен, pid, код прошлого выхода)`` или None."""
    if not shutil.which("launchctl"):
        return None
    try:
        result = subprocess.run(
            ["launchctl", "list", LAUNCHD_LABEL],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        if "could not find" in output.lower() or "no such process" in output.lower():
            return (False, 0, None)
        return None  # нет GUI-сессии/нет прав — состояние неизвестно
    pid_match = re.search(r'"PID"\s*=\s*(\d+)', output)
    exit_match = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', output)
    pid = int(pid_match.group(1)) if pid_match else 0
    last_exit = int(exit_match.group(1)) if exit_match else None
    return (True, pid, last_exit)


def check_scheduler(root):
    """Проверяет pid-лок и LaunchAgent.

    Returns:
        (Check, running: bool) — running нужен проверке «последний тик»,
        чтобы не ругать остановленного бота.
    """
    lock_path = Path(root) / PID_RELATIVE_PATH
    running = False
    status = OK
    parts = []

    if not lock_path.exists():
        status = WARN
        parts.append("pid-файла нет (%s) — агент ни разу не запускался" % lock_path)
    else:
        try:
            raw = lock_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return Check(FAIL, "планировщик",
                         "pid-файл %s не читается (%s)" % (lock_path, exc)), False
        pid = parse_pid_file(raw)
        if pid <= 0:
            status = WARN
            parts.append("pid-файл %s битый (pid не найден)" % lock_path)
        elif agent_running(pid):
            running = True
            parts.append("агент работает, pid %d" % pid)
        else:
            status = WARN
            parts.append("процесс %d не работает — pid-файл устарел (бот остановлен "
                         "или упал)" % pid)

    state = launchd_state()
    if state is None:
        parts.append("launchd: состояние неизвестно (нет launchctl/сессии)")
    else:
        loaded, lpid, last_exit = state
        if not loaded:
            parts.append("launchd: %s не загружен" % LAUNCHD_LABEL)
            if running:
                parts.append("запущен вручную (автозапуска нет)")
        else:
            if lpid:
                parts.append("launchd: загружен, pid %d" % lpid)
            else:
                parts.append("launchd: загружен, процесса нет")
            if last_exit not in (None, 0):
                parts.append("последний выход с кодом %d" % last_exit)
                if not running:
                    status = FAIL  # launchd перезапускает, но бот снова падает

    return Check(status, "планировщик", "; ".join(parts)), running


# ===========================================================================
# 5. Последний успешный тик
# ===========================================================================
def check_last_tick(storage, running):
    """Показывает, когда планировщик последний раз тикал без ошибок."""
    if storage is None:
        return Check(WARN, "последний тик", "нет доступа к базе — проверить нельзя")

    try:
        heartbeat = storage.kv_get(KV_HEARTBEAT)
        error = storage.kv_get(KV_TICK_ERROR)
    except Exception as exc:  # noqa: BLE001
        return Check(WARN, "последний тик", "kv не читается (%s)" % type(exc).__name__)

    moment = _parse_moment(heartbeat)
    if moment is None:
        detail = "отметок нет — бот ещё не тикал (или база от старой версии)"
        return Check(WARN, "последний тик", detail)

    now = datetime.now(moment.tzinfo or None)
    age = (now - moment).total_seconds()
    stamp = moment.strftime("%d.%m %H:%M")
    if age < 0:
        age = 0.0
    detail = "%s (%s назад)" % (stamp, _short_time(age))
    if error:
        detail += "; последняя ошибка тика: %s" % str(error).strip()[:120]

    if age <= STALE_TICK_MINUTES * 60:
        return Check(OK, "последний тик", detail)
    if running:
        return Check(FAIL, "последний тик",
                     "нет успешных тиков %s — планировщик завис? %s"
                     % (_short_time(age), detail))
    return Check(WARN, "последний тик", "бот сейчас не работает; " + detail)


# ===========================================================================
# 6. Логи
# ===========================================================================
def log_candidates(cfg):
    """Пути логов, которые нас интересуют: handler и вывод launchd."""
    from src import config as config_mod

    paths = []
    logs_value = os.environ.get("LIFEOS_LOG_DIR", "").strip() or \
        str((cfg.get("paths") or {}).get("logs") or "").strip()
    if logs_value:
        logs_dir = Path(config_mod.resolve_path(cfg, logs_value))
        paths.append(logs_dir / "agent.log")
        paths.append(logs_dir / "agent.log.1")
    launchd_dir = Path(os.path.expanduser("~/Library/Logs/LifeOS"))
    paths.append(launchd_dir / "agent.log")
    paths.append(launchd_dir / "agent.log.1")
    paths.append(launchd_dir / "agent.out.log")
    paths.append(launchd_dir / "agent.out.log.1")
    unique = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def check_logs(cfg):
    """Размеры логов и наличие архива; ругается, если лог перерос порог."""
    files = []
    for path in log_candidates(cfg):
        try:
            if path.is_file():
                files.append((path, path.stat().st_size))
        except OSError:
            continue
    if not files:
        return Check(WARN, "логи", "логов пока нет — бот ещё не писал"), []

    # Архивы (``*.1``) по определению больше порога — их в «перерос» не считаем.
    over = [(path, size) for path, size in files
            if size > LOG_ROTATE_BYTES and not path.name.endswith(".1")]
    biggest_path, biggest_size = max(files, key=lambda item: item[1])
    detail = "%d файл(а), самый большой %s (%s)" % (
        len(files), biggest_path, human_size(biggest_size),
    )
    archives = [path for path, _size in files if path.name.endswith(".1")]
    if archives:
        detail += "; архив: %s" % ", ".join(str(path.name) for path in archives)
    if over:
        names = ", ".join("%s (%s)" % (path.name, human_size(size)) for path, size in over)
        return Check(WARN, "логи",
                     "%s; больше 5 МБ: %s — запустите с --rotate (бот должен быть "
                     "остановлен) или bash install.sh --reload" % (detail, names)), over
    return Check(OK, "логи", detail), []


def rotate_log(path, max_bytes=LOG_ROTATE_BYTES):
    """Переименовывает переросший лог в ``.1`` (один архив) и создаёт пустой.

    Старый ``.1`` заменяется: архив ровно один. Возвращает True, если файл
    действительно повёрнут. Вызывать только когда писатель остановлен —
    иначе он продолжит писать в переименованный файл.
    """
    target = Path(path)
    try:
        if not target.is_file():
            return False
        size = target.stat().st_size
    except OSError:
        return False
    if size <= max_bytes:
        return False
    archive = target.with_name(target.name + ".1")
    try:
        os.replace(str(target), str(archive))
    except OSError:
        return False
    try:
        # Новый лог — сразу «только владельцу»: в нём личные записи, а umask
        # самого процесса может быть 022 (как у запуска из терминала).
        fd = os.open(str(target), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    except OSError:  # pragma: no cover — каталог стал недоступен
        return False
    return True


def perform_rotation(cfg, running):
    """Крутит переросшие логи, если бот остановлен.

    Returns:
        Список строк-действий для вывода (пустой — ничего не делали).
    """
    actions = []
    for path in log_candidates(cfg):
        if path.name.endswith(".1"):
            continue  # это уже архив: второй раз не крутим
        try:
            if not path.is_file() or path.stat().st_size <= LOG_ROTATE_BYTES:
                continue
        except OSError:
            continue
        name = str(path)
        if running:
            actions.append("%s — не трогаю: бот работает (остановите агента)" % name)
            continue
        if rotate_log(path):
            actions.append("%s → %s.1, новый файл создан" % (name, name))
        else:
            actions.append("%s — повернуть не удалось (права?)" % name)
    return actions


# ===========================================================================
# Запуск
# ===========================================================================
def collect(config_path=None, rotate=False):
    """Выполняет все проверки.

    Returns:
        (список Check, cfg или None, список строк про ротацию).
    """
    checks = [check_python()]
    cfg_check, cfg = check_config(config_path)
    checks.append(cfg_check)
    if cfg is None:
        return checks, None, []

    root = cfg.get("_root") or str(PROJECT_ROOT)
    scheduler_check, running = check_scheduler(root)
    checks.append(scheduler_check)

    rotations = perform_rotation(cfg, running) if rotate else []

    storage_check, storage = check_storage(cfg)
    checks.append(storage_check)
    try:
        checks.append(check_last_tick(storage, running))
    finally:
        if storage is not None:
            try:
                storage.close()
            except Exception:  # noqa: BLE001
                pass

    logs_check, _over = check_logs(cfg)
    checks.append(logs_check)
    return checks, cfg, rotations


def main(argv=None):
    """Точка входа: печатает отчёт и возвращает 0/1."""
    parser = argparse.ArgumentParser(
        prog="health.py",
        description="Самопроверка Life OS: python, конфиг, база, планировщик, тики, логи.",
    )
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="путь к JSON-конфигу (по умолчанию config/config.json)")
    parser.add_argument("--rotate", action="store_true",
                        help="переименовать в .1 логи больше 5 МБ (бот должен быть остановлен)")
    args = parser.parse_args(argv)

    print("Life OS — самопроверка")
    print("Папка проекта: %s" % PROJECT_ROOT)
    print()

    checks, cfg, rotations = collect(args.config, rotate=args.rotate)

    for action in rotations:
        print("ротация: %s" % action)
    if rotations:
        print()

    for check in checks:
        print(check.line())

    failed = [check for check in checks if check.failed]
    warned = [check for check in checks if check.status == WARN]
    print()
    if failed:
        print("ПРОБЛЕМЫ: %d из %d проверок, замечаний: %d" % (
            len(failed), len(checks), len(warned)))
        return 1
    if warned:
        print("OK, но есть замечания: %d (работе не мешают)" % len(warned))
        return 0
    print("OK: %d проверок, всё в порядке" % len(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
