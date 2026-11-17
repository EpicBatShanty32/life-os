"""Конфигурация Life OS v3: пути, секреты, часовой пояс.

Источники (старший побеждает младшего):

1. переменные окружения ``LIFEOS_*`` — удобно для тестов, launchd и запуска
   без файла конфига;
2. ``<project_root>/config/config.json`` — структура совместима с v0.x
   (секции ``telegram`` / ``obsidian`` / ``llm`` / ``paths`` / ``app`` /
   ``schedule``), поэтому существующий конфиг продолжает работать;
3. значения по умолчанию из :data:`DEFAULTS`.

Секреты (токен Telegram, ключ LLM) хранятся только в памяти: они исключены
из ``repr`` через ``field(repr=False)`` и из :meth:`Settings.safe_dict`,
поэтому их нельзя случайно отправить в лог или вернуть через API.

Файл ``config/config.json`` в репозиторий не попадает (в нём токен);
образец лежит в ``config/config.example.json``.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "DEFAULTS",
    "DEFAULT_TIMEZONE",
    "DEFAULT_VAULT_PATH",
    "ConfigError",
    "Settings",
    "SENSITIVE_KEYS",
    "get_settings",
    "load_settings",
    "project_root",
    "reset_settings_cache",
    "resolve_path",
]

DEFAULT_TIMEZONE = "Europe/Moscow"

# Личная база Obsidian владельца. Значение по умолчанию для v3: если файла
# config/config.json нет, vault считается настроенным. Запись в него выполняет
# только vault.py, и только когда ``Settings.vault_write`` истинно. Тесты
# обязаны подменять путь через LIFEOS_VAULT и ничего не писать по этому адресу.
DEFAULT_VAULT_PATH = "/Users/user/Documents/чисто лайфик обсидиан/чисто лайфик"

# Ключи, значения которых нельзя показывать в API/логах (сравнение без учёта
# регистра, применяется рекурсивно к словарю конфига).
SENSITIVE_KEYS = frozenset(
    {
        "token",
        "bot_token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "access_token",
        "refresh_token",
    }
)

# Базовые значения v3. Секции telegram/obsidian/llm/paths/app/schedule —
# зеркало config.example.json v0.x, чтобы старый конфиг читался как есть.
DEFAULTS: dict[str, Any] = {
    "app": {
        "name": "Life OS",
        "timezone": DEFAULT_TIMEZONE,
        "lang": "ru",
        "host": "127.0.0.1",
        "port": 8787,
        "onboarding": True,
        "quiet_hours": ["23:00", "08:00"],
    },
    "telegram": {
        "token": "",
        "allowed_chat_id": 0,
        "api_base": "https://api.telegram.org",
    },
    "obsidian": {
        "enabled": True,
        "vault_path": DEFAULT_VAULT_PATH,
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
        "data": "data",
        "db": "data/lifeos.db",
        "media": "data/media",
        "logs": "data/logs",
        "dashboard_out": "dashboard/index.html",
        "web_dist": "app/web/dist",
    },
}

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "да"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "нет"})


class ConfigError(Exception):
    """Ошибка конфигурации: файла нет, битый JSON, неверный тип значения."""


def project_root() -> Path:
    """Корень проекта (родитель каталога ``app/``)."""
    return Path(__file__).resolve().parents[2]


def resolve_path(value: str | Path, root: str | Path) -> Path:
    """Приводит путь к абсолютному: ``~`` раскрывается, относительный — от root."""
    raw = str(value).strip()
    if not raw:
        raise ConfigError("Пустой путь в конфигурации")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(root).expanduser() / path
    return path.resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Настройки процесса (кэш на всё время жизни приложения)."""
    return load_settings()


def reset_settings_cache() -> None:
    """Сбрасывает кэш :func:`get_settings` (нужно тестам и перечитыванию env)."""
    get_settings.cache_clear()


def load_settings(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    root: str | Path | None = None,
) -> Settings:
    """Собирает :class:`Settings` из env, файла конфига и значений по умолчанию.

    ``env`` и ``root`` можно подменить в тестах: при ``env={}`` переменные
    окружения процесса не читаются, при ``root=tmp_path`` не читается конфиг
    реального проекта.
    """
    environment: Mapping[str, str] = os.environ if env is None else env
    base = _resolve_root(root, environment)
    config_file = _locate_config(base, config_path, environment)

    merged = copy.deepcopy(DEFAULTS)
    if config_file is not None:
        _deep_merge(merged, _read_json(config_file))
    _validate(merged)
    return _build(merged, base, config_file, environment)


# --------------------------------------------------------------------------
# Внутренние помощники загрузки
# --------------------------------------------------------------------------


def _resolve_root(root: str | Path | None, env: Mapping[str, str]) -> Path:
    if root is not None:
        return resolve_path(root, Path.cwd())
    env_root = _env(env, "LIFEOS_ROOT")
    if env_root is not None:
        return resolve_path(env_root, Path.cwd())
    return project_root()


def _locate_config(
    base: Path,
    config_path: str | Path | None,
    env: Mapping[str, str],
) -> Path | None:
    if config_path is not None:
        return _require_config(config_path, base)
    env_config = _env(env, "LIFEOS_CONFIG")
    if env_config is not None:
        return _require_config(env_config, base)
    candidate = base / "config" / "config.json"
    return candidate.resolve() if candidate.is_file() else None


def _require_config(path: str | Path, root: Path) -> Path:
    resolved = resolve_path(path, root)
    if not resolved.is_file():
        raise ConfigError("Файл конфигурации не найден: %s" % resolved)
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("Не удалось прочитать конфиг %s: %s" % (path, exc)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "Некорректный JSON в конфиге %s (строка %d, колонка %d): %s"
            % (path, exc.lineno, exc.colno, exc.msg)
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError("Конфиг %s должен содержать JSON-объект {...}" % path)
    return data


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _env(env: Mapping[str, str], *names: str) -> str | None:
    """Первая непустая переменная окружения из списка (пустая = не задана)."""
    for name in names:
        raw = env.get(name)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            return value
    return None


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool):
        raise ConfigError("%s: ожидалось целое число, получено bool" % where)
    try:
        text = str(value).strip() if isinstance(value, str) else value
        return int(text)
    except (TypeError, ValueError) as exc:
        raise ConfigError("%s: ожидалось целое число, получено %r" % (where, value)) from exc


def _as_float(value: Any, where: str) -> float:
    if isinstance(value, bool):
        raise ConfigError("%s: ожидалось число, получено bool" % where)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("%s: ожидалось число, получено %r" % (where, value)) from exc


def _as_bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_VALUES:
            return True
        if text in _FALSE_VALUES:
            return False
    raise ConfigError("%s: ожидалось true/false, получено %r" % (where, value))


def _validate(cfg: dict[str, Any]) -> None:
    """Проверяет типы известных полей и нормализует значения на месте."""
    for section in ("app", "telegram", "obsidian", "llm", "paths", "schedule"):
        if not isinstance(cfg.get(section), dict):
            raise ConfigError("Секция %r конфига должна быть объектом {...}" % section)

    app = cfg["app"]
    if not isinstance(app.get("name"), str) or not app["name"].strip():
        raise ConfigError("app.name должен быть непустой строкой")
    if not isinstance(app.get("lang"), str) or not app["lang"].strip():
        raise ConfigError("app.lang должен быть непустой строкой")
    if not isinstance(app.get("timezone"), str) or not app["timezone"].strip():
        raise ConfigError("app.timezone должен быть непустой строкой (например, Europe/Moscow)")
    if not isinstance(app.get("host"), str) or not app["host"].strip():
        raise ConfigError("app.host должен быть непустой строкой")
    port = _as_int(app.get("port", DEFAULTS["app"]["port"]), "app.port")
    if not 1 <= port <= 65535:
        raise ConfigError("app.port вне диапазона 1..65535: %d" % port)
    app["port"] = port
    app["onboarding"] = _as_bool(app.get("onboarding", True), "app.onboarding")
    quiet_hours = app.get("quiet_hours") or []
    if not isinstance(quiet_hours, list) or not all(isinstance(item, str) for item in quiet_hours):
        raise ConfigError("app.quiet_hours должен быть списком строк HH:MM")
    app["quiet_hours"] = quiet_hours

    telegram = cfg["telegram"]
    token = telegram.get("token", "")
    if token is None:
        token = ""
    if not isinstance(token, str):
        raise ConfigError("telegram.token должен быть строкой")
    telegram["token"] = token
    telegram["allowed_chat_id"] = _as_int(
        telegram.get("allowed_chat_id", 0), "telegram.allowed_chat_id"
    )
    api_base = telegram.get("api_base", DEFAULTS["telegram"]["api_base"])
    if not isinstance(api_base, str) or not api_base.strip():
        raise ConfigError("telegram.api_base должен быть непустой строкой")
    telegram["api_base"] = api_base

    obsidian = cfg["obsidian"]
    vault_path = obsidian.get("vault_path", "")
    if vault_path is None:
        vault_path = ""
    if not isinstance(vault_path, str):
        raise ConfigError("obsidian.vault_path должен быть строкой (пустая = vault выключен)")
    obsidian["vault_path"] = vault_path
    obsidian["enabled"] = _as_bool(obsidian.get("enabled", True), "obsidian.enabled")

    llm = cfg["llm"]
    for key in ("base_url", "model", "api_key"):
        value = llm.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ConfigError("llm.%s должен быть строкой" % key)
        llm[key] = value
    llm["enabled"] = _as_bool(llm.get("enabled", True), "llm.enabled")
    timeout = _as_float(llm.get("timeout", 60), "llm.timeout")
    if timeout <= 0:
        raise ConfigError("llm.timeout должен быть больше нуля")
    llm["timeout"] = timeout

    paths = cfg["paths"]
    for key in ("data", "db", "media", "logs", "dashboard_out", "web_dist"):
        value = paths.get(key, DEFAULTS["paths"][key])
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("paths.%s должен быть непустой строкой" % key)
        paths[key] = value


def _build(
    cfg: dict[str, Any],
    root: Path,
    config_file: Path | None,
    env: Mapping[str, str],
) -> Settings:
    app = cfg["app"]
    telegram = cfg["telegram"]
    obsidian = cfg["obsidian"]
    llm = cfg["llm"]
    paths = cfg["paths"]

    data_dir = resolve_path(_env(env, "LIFEOS_DATA_DIR") or paths["data"], root)

    env_db = _env(env, "LIFEOS_DB_PATH")
    if env_db is not None:
        db_path = resolve_path(env_db, root)
    elif paths["db"] != DEFAULTS["paths"]["db"]:
        db_path = resolve_path(paths["db"], root)
    else:
        db_path = data_dir / "lifeos.db"

    env_media = _env(env, "LIFEOS_MEDIA_DIR")
    if env_media is not None:
        media_dir = resolve_path(env_media, root)
    elif paths["media"] != DEFAULTS["paths"]["media"]:
        media_dir = resolve_path(paths["media"], root)
    else:
        media_dir = data_dir / "media"

    env_logs = _env(env, "LIFEOS_LOGS_DIR")
    if env_logs is not None:
        logs_dir = resolve_path(env_logs, root)
    elif paths["logs"] != DEFAULTS["paths"]["logs"]:
        logs_dir = resolve_path(paths["logs"], root)
    else:
        logs_dir = data_dir / "logs"

    web_dist = resolve_path(paths["web_dist"], root)

    env_vault = _env(env, "LIFEOS_VAULT")
    vault_raw = env_vault if env_vault is not None else str(obsidian.get("vault_path") or "")
    vault_path = resolve_path(vault_raw, root) if vault_raw.strip() else None

    env_vault_write = _env(env, "LIFEOS_VAULT_WRITE")
    vault_write = (
        _as_bool(env_vault_write, "LIFEOS_VAULT_WRITE")
        if env_vault_write is not None
        else bool(obsidian["enabled"])
    )
    vault_write = bool(vault_write and vault_path is not None)

    env_tz = _env(env, "LIFEOS_TZ", "LIFEOS_TIMEZONE")
    timezone_name = env_tz if env_tz is not None else app["timezone"]

    env_host = _env(env, "LIFEOS_HOST")
    host = env_host if env_host is not None else app["host"]

    env_port = _env(env, "LIFEOS_PORT")
    port = _as_int(env_port, "LIFEOS_PORT") if env_port is not None else app["port"]
    if not 1 <= port <= 65535:
        raise ConfigError("LIFEOS_PORT вне диапазона 1..65535: %d" % port)

    env_token = _env(env, "LIFEOS_TELEGRAM_TOKEN")
    token = env_token if env_token is not None else telegram["token"]

    env_chat = _env(env, "LIFEOS_ADMIN_TELEGRAM_ID", "LIFEOS_ALLOWED_CHAT_ID")
    allowed_chat_id = (
        _as_int(env_chat, "LIFEOS_ADMIN_TELEGRAM_ID")
        if env_chat is not None
        else telegram["allowed_chat_id"]
    )

    # Локальный Bot API Server (docker-сервис telegram-bot-api) поднимает лимит
    # файла с 20 МБ до 2 ГБ. Compose передаёт имя TELEGRAM_API_BASE; LIFEOS_*
    # вариант — для единообразия с остальными настройками.
    env_api_base = _env(
        env, "LIFEOS_TELEGRAM_API_BASE", "LIFEOS_API_BASE", "TELEGRAM_API_BASE"
    )
    telegram_api_base = (
        env_api_base if env_api_base is not None else telegram["api_base"]
    ).rstrip("/")
    if not telegram_api_base:
        raise ConfigError("telegram.api_base должен быть непустой строкой")

    env_llm_enabled = _env(env, "LIFEOS_LLM_ENABLED")
    llm_enabled = (
        _as_bool(env_llm_enabled, "LIFEOS_LLM_ENABLED")
        if env_llm_enabled is not None
        else bool(llm["enabled"])
    )
    env_llm_base = _env(env, "LIFEOS_LLM_BASE_URL")
    llm_base_url = env_llm_base if env_llm_base is not None else llm["base_url"]
    env_llm_model = _env(env, "LIFEOS_LLM_MODEL")
    llm_model = env_llm_model if env_llm_model is not None else llm["model"]

    # DDD_API_KEY — имя ключа из старого install.sh v0.x: ключ подхватывается
    # из окружения, но никогда не пишется в конфиг/лог.
    env_llm_key = _env(env, "LIFEOS_LLM_API_KEY", "DDD_API_KEY")
    llm_api_key = env_llm_key if env_llm_key is not None else llm["api_key"]

    env_llm_timeout = _env(env, "LIFEOS_LLM_TIMEOUT")
    llm_timeout = (
        _as_float(env_llm_timeout, "LIFEOS_LLM_TIMEOUT")
        if env_llm_timeout is not None
        else float(llm["timeout"])
    )
    if llm_timeout <= 0:
        raise ConfigError("LIFEOS_LLM_TIMEOUT должен быть больше нуля")

    env_debug = _env(env, "LIFEOS_DEBUG")
    debug = _as_bool(env_debug, "LIFEOS_DEBUG") if env_debug is not None else False

    return Settings(
        root=root,
        config_path=config_file,
        data_dir=data_dir,
        db_path=db_path,
        media_dir=media_dir,
        logs_dir=logs_dir,
        web_dist=web_dist,
        vault_path=vault_path,
        vault_write=vault_write,
        timezone=timezone_name,
        lang=app["lang"],
        app_name=app["name"],
        host=host,
        port=port,
        token=token,
        allowed_chat_id=allowed_chat_id,
        telegram_api_base=telegram_api_base,
        llm_enabled=llm_enabled,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_timeout=llm_timeout,
        debug=debug,
        onboarding=bool(app["onboarding"]),
        quiet_hours=tuple(app["quiet_hours"]),
        config=cfg,
    )


@lru_cache(maxsize=16)
def _zone(name: str) -> tzinfo:
    """Часовой пояс по имени; при отсутствии tzdata — предсказуемый fallback."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name in {"Europe/Moscow", "MSK", "Moscow"}:
            return timezone(timedelta(hours=3), name)
        return timezone.utc


@dataclass(frozen=True)
class Settings:
    """Настройки Life OS v3.

    Секреты (``token``, ``llm_api_key``, ``config``) помечены ``repr=False``:
    ``repr(settings)`` и стандартное логирование датакласса их не печатают.
    """

    root: Path
    config_path: Path | None
    data_dir: Path
    db_path: Path
    media_dir: Path
    logs_dir: Path
    web_dist: Path
    vault_path: Path | None
    vault_write: bool
    timezone: str
    lang: str
    app_name: str
    host: str
    port: int
    token: str = field(repr=False)
    allowed_chat_id: int
    telegram_api_base: str
    llm_enabled: bool
    llm_base_url: str
    llm_model: str
    llm_api_key: str = field(repr=False)
    llm_timeout: float
    debug: bool
    onboarding: bool
    quiet_hours: tuple[str, ...]
    config: dict[str, Any] = field(repr=False, compare=False)

    @property
    def tz(self) -> tzinfo:
        """Часовой пояс приложения (Europe/Moscow по умолчанию)."""
        return _zone(self.timezone)

    @property
    def vault_ready(self) -> bool:
        """Можно ли писать в Obsidian: путь задан, существует и запись разрешена."""
        if self.vault_path is None or not self.vault_write:
            return False
        return self.vault_path.is_dir()

    def now(self) -> datetime:
        """Текущее время с часовым поясом приложения."""
        return datetime.now(self.tz)

    def today(self) -> str:
        """Локальная дата ``YYYY-MM-DD`` (границы суток — по часовому поясу)."""
        return self.now().date().isoformat()

    def ensure_dirs(self) -> None:
        """Создаёт рабочие каталоги данных. Vault не создаётся и не пишется."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        for directory in (self.data_dir, self.media_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def safe_dict(self) -> dict[str, Any]:
        """Снимок настроек без секретов — для ``GET /api/settings`` и логов."""
        return {
            "app": {
                "name": self.app_name,
                "timezone": self.timezone,
                "lang": self.lang,
                "host": self.host,
                "port": self.port,
                "debug": self.debug,
                "onboarding": self.onboarding,
                "quiet_hours": list(self.quiet_hours),
            },
            "paths": {
                "root": str(self.root),
                "data": str(self.data_dir),
                "db": str(self.db_path),
                "media": str(self.media_dir),
                "logs": str(self.logs_dir),
                "web_dist": str(self.web_dist),
            },
            "vault": {
                "path": str(self.vault_path) if self.vault_path is not None else "",
                "write": self.vault_write,
            },
            "telegram": {
                "token_set": bool(self.token),
                "allowed_chat_id": self.allowed_chat_id,
            },
            "llm": {
                "enabled": self.llm_enabled,
                "base_url": self.llm_base_url,
                "model": self.llm_model,
                "api_key_set": bool(self.llm_api_key),
                "timeout": self.llm_timeout,
            },
            "config_path": str(self.config_path) if self.config_path is not None else None,
            "config": _mask_tree(self.config),
        }


def _mask_tree(value: Any) -> Any:
    """Рекурсивно заменяет значения секретных ключей на ``***``."""
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in SENSITIVE_KEYS and item:
                masked[key] = "***"
            else:
                masked[key] = _mask_tree(item)
        return masked
    if isinstance(value, list):
        return [_mask_tree(item) for item in value]
    return value
