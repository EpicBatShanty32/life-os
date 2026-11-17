"""Тесты конфигурации Life OS v3 (app/server/settings.py).

Все тесты работают на временных каталогах: реальный vault Obsidian и рабочая
база ``data/lifeos.db`` не читаются и не изменяются.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.server.settings import (
    DEFAULT_TIMEZONE,
    DEFAULT_VAULT_PATH,
    ConfigError,
    get_settings,
    load_settings,
    project_root,
    reset_settings_cache,
    resolve_path,
)


def _write_config(root: Path, data: dict) -> Path:
    directory = root / "config"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_defaults_without_config_file(tmp_path: Path) -> None:
    settings = load_settings(env={}, root=tmp_path)

    assert settings.root == tmp_path.resolve()
    assert settings.config_path is None
    assert settings.data_dir == (tmp_path / "data").resolve()
    assert settings.db_path == (tmp_path / "data" / "lifeos.db").resolve()
    assert settings.media_dir == (tmp_path / "data" / "media").resolve()
    assert settings.logs_dir == (tmp_path / "data" / "logs").resolve()
    assert settings.timezone == DEFAULT_TIMEZONE
    assert settings.vault_path == Path(DEFAULT_VAULT_PATH)
    assert settings.vault_ready is (settings.vault_path.is_dir() and settings.vault_write)
    assert settings.vault_write is True
    assert settings.token == ""
    assert settings.llm_api_key == ""
    assert settings.allowed_chat_id == 0
    assert settings.port == 8787
    assert settings.host == "127.0.0.1"
    assert settings.lang == "ru"
    assert settings.app_name == "Life OS"


def test_reads_legacy_config_json(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_config(
        tmp_path,
        {
            "telegram": {"token": "123:telegram-token", "allowed_chat_id": 777},
            "obsidian": {"enabled": True, "vault_path": str(vault)},
            "llm": {"enabled": False, "api_key": "sk-test-key", "timeout": 15},
            "paths": {"db": "data/custom.db"},
            "app": {"timezone": "UTC", "port": 9001},
        },
    )

    settings = load_settings(env={}, root=tmp_path)

    assert settings.config_path == (tmp_path / "config" / "config.json").resolve()
    assert settings.token == "123:telegram-token"
    assert settings.allowed_chat_id == 777
    assert settings.vault_path == vault.resolve()
    assert settings.vault_write is True
    assert settings.llm_enabled is False
    assert settings.llm_api_key == "sk-test-key"
    assert settings.llm_timeout == 15.0
    assert settings.db_path == (tmp_path / "data" / "custom.db").resolve()
    assert settings.timezone == "UTC"
    assert settings.tz.utcoffset(None) == timedelta(0)
    assert settings.port == 9001


def test_env_overrides_config(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "telegram": {"token": "from-config"},
            "obsidian": {"vault_path": str(tmp_path / "old-vault")},
            "llm": {"api_key": "from-config-key", "model": "cfg-model"},
            "paths": {"db": "data/custom.db"},
        },
    )
    env = {
        "LIFEOS_CONFIG": str(tmp_path / "config" / "config.json"),
        "LIFEOS_DB_PATH": str(tmp_path / "db" / "v3.db"),
        "LIFEOS_VAULT": str(tmp_path / "new-vault"),
        "LIFEOS_TZ": "UTC",
        "LIFEOS_TELEGRAM_TOKEN": "from-env",
        "LIFEOS_LLM_API_KEY": "from-env-key",
        "LIFEOS_LLM_MODEL": "env-model",
        "LIFEOS_ADMIN_TELEGRAM_ID": "42",
        "LIFEOS_HOST": "0.0.0.0",
        "LIFEOS_PORT": "9100",
        "LIFEOS_DEBUG": "1",
    }

    settings = load_settings(env=env, root=tmp_path)

    assert settings.token == "from-env"
    assert settings.db_path == (tmp_path / "db" / "v3.db").resolve()
    assert settings.vault_path == (tmp_path / "new-vault").resolve()
    assert settings.timezone == "UTC"
    assert settings.llm_api_key == "from-env-key"
    assert settings.llm_model == "env-model"
    assert settings.allowed_chat_id == 42
    assert settings.host == "0.0.0.0"
    assert settings.port == 9100
    assert settings.debug is True


def test_ddd_api_key_env_is_supported(tmp_path: Path) -> None:
    settings = load_settings(env={"DDD_API_KEY": "ddd-key"}, root=tmp_path)
    assert settings.llm_api_key == "ddd-key"


def test_data_dir_env_moves_default_paths(tmp_path: Path) -> None:
    store = tmp_path / "store"
    settings = load_settings(env={"LIFEOS_DATA_DIR": str(store)}, root=tmp_path)

    assert settings.data_dir == store.resolve()
    assert settings.db_path == (store / "lifeos.db").resolve()
    assert settings.media_dir == (store / "media").resolve()
    assert settings.logs_dir == (store / "logs").resolve()


def test_config_paths_win_over_data_dir_env(tmp_path: Path) -> None:
    _write_config(tmp_path, {"paths": {"db": "other/place.db"}})
    settings = load_settings(
        env={"LIFEOS_DATA_DIR": str(tmp_path / "store")},
        root=tmp_path,
    )

    assert settings.data_dir == (tmp_path / "store").resolve()
    assert settings.db_path == (tmp_path / "other" / "place.db").resolve()
    assert settings.media_dir == (tmp_path / "store" / "media").resolve()


def test_secrets_are_not_exposed(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "telegram": {"token": "super-secret-token"},
            "llm": {"api_key": "sk-very-secret"},
        },
    )

    settings = load_settings(env={}, root=tmp_path)

    assert settings.token == "super-secret-token"
    assert settings.llm_api_key == "sk-very-secret"

    representation = repr(settings)
    assert "super-secret-token" not in representation
    assert "sk-very-secret" not in representation

    public = settings.safe_dict()
    blob = json.dumps(public, ensure_ascii=False)
    assert "super-secret-token" not in blob
    assert "sk-very-secret" not in blob
    assert public["telegram"] == {"token_set": True, "allowed_chat_id": 0}
    assert public["llm"]["api_key_set"] is True
    assert public["config"]["telegram"]["token"] == "***"
    assert public["config"]["llm"]["api_key"] == "***"


def test_explicit_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "missing.json", env={}, root=tmp_path)


def test_env_config_missing_raises(tmp_path: Path) -> None:
    env = {"LIFEOS_CONFIG": str(tmp_path / "missing.json")}
    with pytest.raises(ConfigError):
        load_settings(env=env, root=tmp_path)


def test_broken_json_raises(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"app": {', encoding="utf-8")

    with pytest.raises(ConfigError, match="JSON"):
        load_settings(env={}, root=tmp_path)


def test_non_object_config_raises(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ConfigError, match="JSON-объект"):
        load_settings(env={}, root=tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        {"app": {"port": 0}},
        {"app": {"port": "not-a-number"}},
        {"app": {"timezone": ""}},
        {"telegram": {"token": 123}},
        {"llm": {"timeout": 0}},
        {"paths": {"db": ""}},
        {"obsidian": {"enabled": "maybe"}},
    ],
)
def test_invalid_values_raise(tmp_path: Path, payload: dict) -> None:
    _write_config(tmp_path, payload)
    with pytest.raises(ConfigError):
        load_settings(env={}, root=tmp_path)


def test_timezone_and_today(tmp_path: Path) -> None:
    settings = load_settings(env={}, root=tmp_path)

    assert settings.timezone == "Europe/Moscow"
    assert settings.now().utcoffset() == timedelta(hours=3)
    assert settings.today() == settings.now().date().isoformat()


def test_unknown_timezone_falls_back_to_utc(tmp_path: Path) -> None:
    _write_config(tmp_path, {"app": {"timezone": "Mars/Olympus"}})
    settings = load_settings(env={}, root=tmp_path)

    assert settings.timezone == "Mars/Olympus"
    assert settings.tz.utcoffset(None) == timedelta(0)


def test_ensure_dirs_never_touches_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault-untouched"
    settings = load_settings(
        env={
            "LIFEOS_DATA_DIR": str(tmp_path / "store"),
            "LIFEOS_VAULT": str(vault),
        },
        root=tmp_path,
    )

    settings.ensure_dirs()

    assert (tmp_path / "store").is_dir()
    assert (tmp_path / "store" / "media").is_dir()
    assert (tmp_path / "store" / "logs").is_dir()
    assert not vault.exists()


def test_vault_can_be_disabled(tmp_path: Path) -> None:
    _write_config(tmp_path, {"obsidian": {"vault_path": ""}})
    settings = load_settings(env={}, root=tmp_path)
    assert settings.vault_path is None
    assert settings.vault_write is False
    assert settings.vault_ready is False

    _write_config(tmp_path, {"obsidian": {"enabled": False}})
    settings = load_settings(env={}, root=tmp_path)
    assert settings.vault_write is False
    assert settings.vault_path == Path(DEFAULT_VAULT_PATH)

    env = {"LIFEOS_VAULT": str(tmp_path / "vault"), "LIFEOS_VAULT_WRITE": "0"}
    settings = load_settings(env=env, root=tmp_path)
    assert settings.vault_path == (tmp_path / "vault").resolve()
    assert settings.vault_write is False


def test_empty_env_values_are_ignored(tmp_path: Path) -> None:
    _write_config(tmp_path, {"telegram": {"token": "keep-me"}})
    env = {"LIFEOS_TELEGRAM_TOKEN": "   ", "LIFEOS_PORT": ""}
    settings = load_settings(env=env, root=tmp_path)

    assert settings.token == "keep-me"
    assert settings.port == 8787


def test_get_settings_is_cached_and_resettable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIFEOS_ROOT", str(tmp_path))
    reset_settings_cache()
    try:
        first = get_settings()
        second = get_settings()
        assert first is second
        assert first.root == tmp_path.resolve()

        reset_settings_cache()
        third = get_settings()
        assert third is not first
    finally:
        reset_settings_cache()


def test_project_root_and_resolve_path(tmp_path: Path) -> None:
    root = project_root()
    assert (root / "app" / "server" / "settings.py").is_file()

    assert resolve_path("data/x.db", tmp_path) == (tmp_path / "data" / "x.db").resolve()
    absolute = tmp_path / "abs" / "x.db"
    assert resolve_path(str(absolute), tmp_path) == absolute.resolve()
    with pytest.raises(ConfigError):
        resolve_path("", tmp_path)


def test_vault_ready_requires_existing_directory(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    env = {"LIFEOS_VAULT": str(vault)}

    assert load_settings(env=env, root=tmp_path).vault_ready is False

    vault.mkdir()
    assert load_settings(env=env, root=tmp_path).vault_ready is True
