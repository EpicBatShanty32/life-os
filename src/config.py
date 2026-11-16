"""Конфигурация Life OS.

Загрузка JSON-конфига, рекурсивное слияние с DEFAULTS и валидация типов.

Порядок поиска файла (первый найденный побеждает):
1. явный аргумент ``path`` (например, ``--config PATH``);
2. переменная окружения ``LIFEOS_CONFIG``;
3. ``<project_root>/config/config.json`` (локальный, обычно с токеном);
4. ``<project_root>/config/config.example.json`` (образец из репозитория).

Возвращаемый словарь — это DEFAULTS, дополненные содержимым файла, плюс
служебные поля ``_root`` (абсолютный путь проекта) и ``_config_path``
(откуда прочитан конфиг — удобно для логов, секретов в нём нет).
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

__all__ = ["DEFAULTS", "ConfigError", "load_config", "resolve_path", "project_root"]


class ConfigError(Exception):
    """Ошибка конфигурации: файл не найден, битый JSON, неверные типы и т.п."""


# Базовые значения — зеркало ТЗ §6. Локальный config/config.json может
# переопределить любой ключ, недостающие ключи берутся отсюда.
DEFAULTS: dict = {
    "telegram": {
        "token": "",
        "allowed_chat_id": 0,
        "api_base": "https://api.telegram.org",
    },
    "obsidian": {
        "enabled": True,
        # Пусто = база не настроена: запись в Obsidian выключена, бот работает.
        # Личный путь сюда не подставляем — конфиг должен быть переносимым, а
        # чужой (несуществующий) путь только маскировал бы ошибку настройки.
        "vault_path": "",
        "daily_folder": "01 Daily",
        "template_path": "Шаблоны/Шаблон - День.md",
        "goals_path": "Проекты/Life OS/Цели.md",
        "data_folder": "Life OS/data",
        "write_mirror": True,
    },
    "llm": {
        "enabled": True,
        "base_url": "https://api.samp.team/v1",
        "model": "deepseek-v4.1-flash-1017",
        "api_key": "",
        "timeout": 60,
        "max_followups": 2,
    },
    "schedule": {
        "morning": {"time": "08:30", "enabled": True},
        "midday": {"time": "14:00", "enabled": True},
        "evening": {"time": "21:30", "enabled": True},
        "weekly": {"time": "21:00", "weekday": 6, "enabled": False},
    },
    "paths": {
        "db": "data/lifeos.db",
        "logs": "data/logs",
        "dashboard_out": "dashboard/index.html",
    },
    "app": {
        "name": "Life OS",
        "timezone": "Europe/Moscow",
        "lang": "ru",
        # Онбординг и тихие часы — включаются в config/config.example.json;
        # по умолчанию выключены, чтобы не мешать тестам и dev-запускам.
        "onboarding": False,
        "quiet_hours": [],
    },
}

# Порядок известных слотов — используется и в планировщике.
SLOT_NAMES = ("morning", "midday", "evening", "weekly")

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def project_root() -> Path:
    """Абсолютный путь project_root (родитель каталога src/)."""
    return Path(__file__).resolve().parent.parent


def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно вливает override в base (base изменяется на месте)."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _read_json(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("Не удалось прочитать конфиг %s: %s" % (path, exc)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "Некорректный JSON в конфиге %s (строка %s, колонка %s): %s"
            % (path, exc.lineno, exc.colno, exc.msg)
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError("Конфиг %s должен содержать JSON-объект {...}" % path)
    return data


def _expect_dict(cfg: dict, key: str, where: str) -> dict:
    value = cfg.get(key)
    if not isinstance(value, dict):
        raise ConfigError("Конфиг: «%s» (%s) должен быть объектом {...}" % (where, key))
    return value


def _expect_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("Конфиг: «%s» должен быть true/false, а не %r" % (where, value))
    return value


def _expect_str(value: Any, where: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ConfigError("Конфиг: «%s» должен быть строкой, а не %r" % (where, value))
    if not allow_empty and not value.strip():
        raise ConfigError("Конфиг: «%s» не может быть пустым" % where)
    return value


def _expect_int(value: Any, where: str, minimum: int | None = None,
                maximum: int | None = None) -> int:
    # bool — подкласс int, но как число в конфиге он бессмыслен.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("Конфиг: «%s» должен быть целым числом, а не %r" % (where, value))
    if minimum is not None and value < minimum:
        raise ConfigError("Конфиг: «%s» должен быть >= %d (сейчас %r)" % (where, minimum, value))
    if maximum is not None and value > maximum:
        raise ConfigError("Конфиг: «%s» должен быть <= %d (сейчас %r)" % (where, maximum, value))
    return value


def _validate_time(value: Any, where: str) -> str:
    text = _expect_str(value, where, allow_empty=False).strip()
    if not _TIME_RE.match(text):
        raise ConfigError("Конфиг: «%s» должно быть временем ЧЧ:ММ (например 08:30), а не %r"
                          % (where, value))
    return text


def _validate(cfg: dict, source: Path | None = None) -> None:
    """Проверяет типы известных ключей. Неизвестные ключи разрешены."""
    path_hint = " в %s" % source if source else ""

    telegram = _expect_dict(cfg, "telegram", "telegram")
    _expect_str(telegram.get("token"), "telegram.token")
    chat_id = telegram.get("allowed_chat_id")
    if isinstance(chat_id, str) and chat_id.strip().isdigit():
        # Пользователь мог вписать chat_id строкой — это допустимо.
        telegram["allowed_chat_id"] = int(chat_id.strip())
    elif isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise ConfigError(
            "Конфиг: «telegram.allowed_chat_id»%s должен быть числом (id чата), а не %r"
            % (path_hint, chat_id)
        )
    if telegram["allowed_chat_id"] < 0:
        raise ConfigError("Конфиг: «telegram.allowed_chat_id» не может быть отрицательным")
    _expect_str(telegram.get("api_base"), "telegram.api_base", allow_empty=False)

    obsidian = _expect_dict(cfg, "obsidian", "obsidian")
    _expect_bool(obsidian.get("enabled"), "obsidian.enabled")
    _expect_str(obsidian.get("vault_path"), "obsidian.vault_path")
    for key in ("daily_folder", "template_path", "goals_path", "data_folder"):
        _expect_str(obsidian.get(key), "obsidian.%s" % key)
    _expect_bool(obsidian.get("write_mirror"), "obsidian.write_mirror")

    llm = _expect_dict(cfg, "llm", "llm")
    _expect_bool(llm.get("enabled"), "llm.enabled")
    _expect_str(llm.get("base_url"), "llm.base_url", allow_empty=False)
    _expect_str(llm.get("model"), "llm.model", allow_empty=False)
    _expect_str(llm.get("api_key"), "llm.api_key")
    timeout = llm.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("Конфиг: «llm.timeout» должен быть положительным числом секунд")
    llm["timeout"] = int(timeout)
    max_followups = llm.get("max_followups")
    if isinstance(max_followups, str) and max_followups.strip().isdigit():
        llm["max_followups"] = int(max_followups.strip())
    _expect_int(llm.get("max_followups"), "llm.max_followups", minimum=0, maximum=10)

    schedule = _expect_dict(cfg, "schedule", "schedule")
    for slot in ("morning", "midday", "evening"):
        spec = _expect_dict(schedule, slot, "schedule.%s" % slot)
        _validate_time(spec.get("time"), "schedule.%s.time" % slot)
        _expect_bool(spec.get("enabled"), "schedule.%s.enabled" % slot)
    weekly = _expect_dict(schedule, "weekly", "schedule.weekly")
    _validate_time(weekly.get("time"), "schedule.weekly.time")
    _expect_bool(weekly.get("enabled"), "schedule.weekly.enabled")
    _expect_int(weekly.get("weekday"), "schedule.weekly.weekday", minimum=0, maximum=6)

    paths = _expect_dict(cfg, "paths", "paths")
    for key in ("db", "logs", "dashboard_out"):
        _expect_str(paths.get(key), "paths.%s" % key, allow_empty=False)

    app = _expect_dict(cfg, "app", "app")
    _expect_str(app.get("name"), "app.name", allow_empty=False)
    timezone = _expect_str(app.get("timezone"), "app.timezone", allow_empty=False)
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
    except Exception as exc:  # неверная таймзона или нет tzdata
        raise ConfigError(
            "Конфиг: «app.timezone» = %r — неизвестная таймзона (%s)" % (timezone, exc)
        ) from exc
    _expect_str(app.get("lang"), "app.lang", allow_empty=False)


def load_config(path: str | None = None) -> dict:
    """Читает конфиг, дополняет DEFAULTS и валидирует.

    Args:
        path: явный путь к JSON-конфигу. Если не задан — см. порядок поиска
            в docstring модуля.

    Returns:
        dict с ключами ТЗ §6 + ``_root`` и ``_config_path``.

    Raises:
        ConfigError: файл не найден, битый JSON или неверные типы.
    """
    root = project_root()
    source: Path

    if path:
        source = Path(path).expanduser()
        if not source.is_absolute():
            source = (Path.cwd() / source).resolve()
        if not source.is_file():
            raise ConfigError("Конфиг не найден: %s (указан явно)" % source)
    else:
        env_path = os.environ.get("LIFEOS_CONFIG", "").strip()
        if env_path:
            source = Path(env_path).expanduser()
            if not source.is_absolute():
                source = (Path.cwd() / source).resolve()
            if not source.is_file():
                raise ConfigError(
                    "Конфиг не найден: %s (из переменной окружения LIFEOS_CONFIG)" % source
                )
        else:
            local = root / "config" / "config.json"
            example = root / "config" / "config.example.json"
            if local.is_file():
                source = local
            elif example.is_file():
                source = example
            else:
                raise ConfigError(
                    "Не найден файл конфигурации: нет ни %s, ни %s. "
                    "Создайте config/config.json по образцу config/config.example.json."
                    % (local, example)
                )

    data = _read_json(source)
    cfg = copy.deepcopy(DEFAULTS)
    _deep_merge(cfg, data)

    cfg["_root"] = str(root)
    cfg["_config_path"] = str(source.resolve())

    _validate(cfg, source)

    return cfg


def resolve_path(cfg: dict, value: str) -> str:
    """Превращает путь из конфига в абсолютный (относительно project_root)."""
    p = Path(value).expanduser()
    if p.is_absolute():
        return str(p)
    root = Path(cfg.get("_root") or project_root())
    return str((root / p).resolve())
