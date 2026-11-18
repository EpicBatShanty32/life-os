"""Запуск бота под launchd-службой ``com.lifeos.bot`` (t24).

Зачем прослойка, если бот и так умеет читать ``config/config.json``:

* **Токен не копируется в plist.** Служба стартует без шелла, поэтому нужные
  переменные окружения ей надо передать заранее. Здесь они берутся из
  ``config/config.json`` (единственный экземпляр секрета на диске) и попадают
  в окружение процесса: ``LIFEOS_TELEGRAM_TOKEN`` и ``LIFEOS_DB_PATH``.
  Если оператор задал их сам (в plist или в шелле), значения не перетираются:
  окружение старше файла, как и в :mod:`app.server.settings`.
* **Нет токена — нет бесконечного перезапуска.** launchd перезапускает только
  упавший процесс (``KeepAlive.SuccessExit=false``). Отсутствие токена —
  не падение, а состояние конфигурации: печатаем одну понятную строку и
  выходим с кодом 0, чтобы служба не крутила цикл «упал — перезапустись».
  Терминальный запуск (``tools/run-bot.sh``) по-прежнему даёт код 2.

Сам бот запускается в этом же процессе через :func:`runpy.run_module`, поэтому
поведение, сигналы и коды выхода ничем не отличаются от
``python -m app.bot.main``.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

#: Фраза, которую ждут и человек, и smoke-проверка. Не менять без причины.
TOKEN_MISSING_MESSAGE = (
    "токен не задан — создай бота в @BotFather и впиши token в "
    "config/config.json → telegram.token (или задай LIFEOS_TELEGRAM_TOKEN)"
)
BROKEN_CONFIG_MESSAGE = (
    "не читается config/config.json (%s) — бот не запущен. Исправьте файл или "
    "задайте LIFEOS_TELEGRAM_TOKEN и LIFEOS_DB_PATH в окружении службы"
)
FALLBACK_DB = ("data/lifeos-v3.db", "data/lifeos.db")


def project_root() -> Path:
    """Корень проекта: на уровень выше ``tools/`` (или ``LIFEOS_ROOT``)."""
    env_root = os.environ.get("LIFEOS_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _read_config(root: Path) -> Tuple[Optional[dict], str]:
    """Возвращает ``(конфиг, "")`` или ``(None, причина)`` — без падений."""
    path = root / "config" / "config.json"
    if not path.is_file():
        return None, "файла config/config.json нет"
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "в корне JSON не объект"
    return data, ""


def _resolve(path_value: str, root: Path) -> Path:
    path = Path(str(path_value)).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _db_path(config: Optional[dict], root: Path) -> str:
    """Путь к базе: из конфига, иначе первая существующая база v3/v0.x."""
    paths = (config or {}).get("paths") or {}
    raw = str(paths.get("db") or "").strip() if isinstance(paths, dict) else ""
    if raw:
        return str(_resolve(raw, root))
    for candidate in FALLBACK_DB:
        path = root / candidate
        if path.exists():
            return str(path)
    return str((root / FALLBACK_DB[0]).resolve())


def prepare_environment(
    root: Path, environ: Optional[Dict[str, str]] = None
) -> Tuple[Dict[str, str], str]:
    """Дополняет окружение службы значениями из конфига.

    Возвращает ``(окружение, сообщение)``. Сообщение непустое, когда запускать
    бота незачем: нет токена или конфиг битый. Значения, заданные снаружи,
    не перетираются.
    """
    env = dict(os.environ if environ is None else environ)
    config, error = _read_config(root)

    if not env.get("LIFEOS_DB_PATH", "").strip():
        env["LIFEOS_DB_PATH"] = _db_path(config, root)
    if not env.get("LIFEOS_LOGS_DIR", "").strip():
        env["LIFEOS_LOGS_DIR"] = str((root / "data" / "logs").resolve())

    if env.get("LIFEOS_TELEGRAM_TOKEN", "").strip():
        return env, ""
    token = ""
    if config is not None:
        telegram = config.get("telegram") or {}
        if isinstance(telegram, dict):
            token = str(telegram.get("token") or "").strip()
    if token:
        env["LIFEOS_TELEGRAM_TOKEN"] = token
        return env, ""
    if config is None and "нет" not in error:
        return env, BROKEN_CONFIG_MESSAGE % error
    # Файла нет или токен пуст — для человека это одно и то же: «впиши токен».
    return env, TOKEN_MISSING_MESSAGE


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = project_root()
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    env, problem = prepare_environment(root)
    os.environ.update(env)
    if problem:
        # Одна строка, без трейсбека: её видно в data/logs/bot.launchd.log.
        print(problem, file=sys.stderr, flush=True)
        return 0

    try:
        runpy.run_module("app.bot.main", run_name="__main__", alter_sys=True)
    except SystemExit as exc:  # бот сам решает, с каким кодом выйти
        return int(exc.code or 0) if isinstance(exc.code, int) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
