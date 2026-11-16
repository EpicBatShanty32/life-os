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
не попадают, двойной запуск защищён pid-файлом ``data/lifeos.pid``,
SIGTERM/SIGINT завершают работу аккуратно.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

# project_root в sys.path — чтобы работал и `python3 -m src.main`, и `python3 src/main.py`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config as config_mod  # noqa: E402

__all__ = ["main"]

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

PID_RELATIVE_PATH = "data/lifeos.pid"


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


class PidLock:
    """Простейший pid-лок: не даёт запустить второй экземпляр агента."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.pid = os.getpid()
        self.acquired = False

    def acquire(self) -> tuple[bool, str]:
        """Возвращает (успех, причина отказа)."""
        existing_pid = 0
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            existing_pid = int(raw) if raw else 0
        except FileNotFoundError:
            existing_pid = 0
        except (OSError, ValueError):
            existing_pid = 0  # битый файл не должен блокировать запуск
        if existing_pid and existing_pid != self.pid and _pid_alive(existing_pid):
            return False, (
                "уже запущен другой экземпляр агента (pid %d, файл %s). "
                "Если это не так — удалите pid-файл." % (existing_pid, self.path)
            )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("%d\n" % self.pid, encoding="utf-8")
        except OSError as exc:
            return False, "не удалось создать pid-файл %s: %s" % (self.path, exc)
        self.acquired = True
        return True, ""

    def release(self) -> None:
        """Удаляет pid-файл, если он наш."""
        if not self.acquired:
            return
        try:
            if self.path.read_text(encoding="utf-8").strip() == str(self.pid):
                self.path.unlink()
        except OSError:
            pass
        self.acquired = False


# ============================================================== ЛОГИРОВАНИЕ
def _setup_logging(cfg: dict, console_level: int = logging.WARNING) -> logging.Logger:
    """Логи в <root>/<paths.logs>/agent.log с ротацией 5 МБ."""
    logs_dir = Path(config_mod.resolve_path(cfg, cfg["paths"]["logs"]))
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # не фатально: останутся только stderr-логи
        print("Не удалось создать каталог логов %s: %s" % (logs_dir, exc), file=sys.stderr)
    log_path = logs_dir / "agent.log"

    logger = logging.getLogger("lifeos")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):  # повторный вызов main в тестах
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    try:
        file_handler = RotatingFileHandler(
            str(log_path), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
    except OSError as exc:
        print("Не удалось открыть лог-файл %s: %s" % (log_path, exc), file=sys.stderr)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(console_level)
    logger.addHandler(console)
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


class DryRunTelegram:
    """Заглушка Telegram для --dry-run: сообщения попадают в лог и в ``sent``."""

    def __init__(self, logger: logging.Logger) -> None:
        self.log = logger
        self.sent: list[str] = []

    def send(self, chat_id: int, text: str) -> None:
        self.sent.append(str(text))
        self.log.info("[DRY-RUN] -> chat %s: %s", chat_id, text)

    def get_updates(self, offset: int | None, timeout: int = 20) -> list[dict]:
        self.log.info("[DRY-RUN] getUpdates пропущен (dry-run)")
        return []

    def call(self, method: str, payload: dict | None = None,
             timeout: int = 60) -> dict:
        self.log.info("[DRY-RUN] вызов Telegram API «%s» пропущен", method)
        return {}


# ================================================================== ЦИКЛ
def _sleep(stop: dict, seconds: float) -> None:
    """Сон с реакцией на сигнал остановки (дробим на короткие куски)."""
    if seconds <= 0:
        return
    deadline = time.time() + seconds
    while not stop["flag"] and time.time() < deadline:
        time.sleep(min(0.25, max(0.0, deadline - time.time())))


def _handle_updates(updates: list, offset: int | None, scheduler,
                    tz: ZoneInfo, logger: logging.Logger) -> int | None:
    """Разбирает апдейты Telegram, возвращает новый offset."""
    for update in updates:
        try:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            chat_id = chat.get("id") if isinstance(chat, dict) else None
            if chat_id is None:
                continue
            text = message.get("text")
            scheduler.on_message(int(chat_id), text if isinstance(text, str) else "",
                                 datetime.now(tz))
        except Exception:  # noqa: BLE001 — один кривой апдейт не должен ронять цикл
            logger.exception("Ошибка обработки обновления Telegram")
    return offset


def main(argv: list[str] | None = None) -> int:
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

    # 4. Хранилище.
    db_path = config_mod.resolve_path(cfg, cfg["paths"]["db"])
    try:
        storage = storage_mod.Storage(db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось открыть базу данных %s: %s", db_path, exc)
        return 2
    logger.info("База данных: %s", db_path)

    # 5. LLM (мозг). Без модуля работать нечему — это ошибка сборки проекта.
    try:
        from src import llm as llm_mod

        llm = llm_mod.LLM(cfg)
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
    vault = None
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

    # 9. Pid-лок: только для длительной работы (--once не мешает живому агенту).
    # Дневной ролловер (sync_pending + rewrite_goals + дашборд) выполняет
    # scheduler.tick() — по контракту INTERFACES §8 п.3; main вызывает tick()
    # на каждом проходе цикла.
    lock = PidLock(_ROOT / PID_RELATIVE_PATH)
    if not args.once:
        ok, reason = lock.acquire()
        if not ok:
            logger.error("Запуск отменён: %s", reason)
            storage.close()
            return 3
        logger.info("Pid-лок: %s (pid %d)", lock.path, lock.pid)

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

    tz_name = str((cfg.get("app") or {}).get("timezone") or "Europe/Moscow")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — config уже проверил, это страховка
        tz = ZoneInfo("Europe/Moscow")

    offset: int | None = None
    auth_errors = 0
    poll_timeout = 0 if args.once else 20
    logger.info("Цикл запущен (timeout=%s c, тик каждые %s c)", poll_timeout,
                0 if args.once else 1)

    exit_code = 0
    try:
        while not stop["flag"]:
            # --- 1. Приём обновлений -------------------------------------
            try:
                updates = tg.get_updates(offset, timeout=poll_timeout)
            except telegram_mod.TelegramError as exc:
                status = exc.status
                if status == 409:
                    logger.warning("%s", exc)
                    _sleep(stop, 0 if args.once else 5)
                elif status == 429:
                    pause = exc.retry_after if exc.retry_after is not None else 5
                    pause = min(max(float(pause), 1.0), 60.0)
                    logger.warning("Telegram rate limit (429): пауза %.0f с", pause)
                    _sleep(stop, 0 if args.once else pause)
                elif status in (401, 403):
                    auth_errors += 1
                    logger.error("Ошибка авторизации Telegram (%s): %s", status, exc)
                    if auth_errors >= 3:
                        logger.error(
                            "Три ошибки авторизации подряд — останавливаюсь. "
                            "Проверьте telegram.token в config/config.json."
                        )
                        exit_code = 1
                        break
                    _sleep(stop, 0 if args.once else 10)
                else:
                    logger.warning("Ошибка Telegram: %s", exc)
                    _sleep(stop, 0 if args.once else 2)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Неожиданная ошибка при опросе Telegram: %s", exc)
                _sleep(stop, 0 if args.once else 2)
            else:
                auth_errors = 0
                offset = _handle_updates(updates, offset, scheduler, tz, logger)

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
        logger.info("Life OS остановлен")

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
