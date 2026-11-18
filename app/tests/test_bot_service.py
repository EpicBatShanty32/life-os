"""Тесты службы бота Life OS (задача t24).

Проверяется контракт «бот как служба»:

* шаблон ``ops/com.lifeos.bot.plist`` — валидный launchd-агент: RunAtLoad,
  перезапуск только после падения, WorkingDirectory проекта, база в окружении,
  логи в ``data/logs``;
* ``tools/bot-service.sh`` синтаксически цел (bash 3.2) и знает команды
  install / start / stop / restart / status / logs / uninstall;
* без токена установка не падает трейсбеком, а печатает «токен не задан —
  создай бота в @BotFather и впиши token» и выходит с кодом 1;
* ``tools/bot_launcher.py`` берёт токен и путь к базе из config/config.json,
  не перетирает явно заданное окружение, при отсутствии токена пишет одну
  строку и выходит с кодом 0 (чтобы launchd не крутил цикл перезапусков);
* лог бота пишется в ``data/logs/bot.log`` и ротируется;
* токена нет ни в шаблоне plist, ни в ``config/config.json`` внутри git.

Всё на временных каталогах: настоящая служба, Telegram и рабочая база не
затрагиваются.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.bot import main as bot_main
from app.server.settings import load_settings

PROJECT = Path(__file__).resolve().parents[2]
PLIST = PROJECT / "ops" / "com.lifeos.bot.plist"
SERVICE = PROJECT / "tools" / "bot-service.sh"
LAUNCHER = PROJECT / "tools" / "bot_launcher.py"
TOKEN_HINT = "токен не задан — создай бота в @BotFather и впиши token"
TOKEN_PATTERN = re.compile(r"[0-9]{8,10}:[A-Za-z0-9_-]{30,}")
# Форма, которая НЕ похожа на настоящий токен: сканер секретов в check.sh
# ловит «8–10 цифр : 30+ символов», а здесь после двоеточия короткий текст.
FAKE_TOKEN = "123456789:fake-token-for-tests"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("lifeos_bot_launcher", LAUNCHER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_config(root: Path, data: dict) -> Path:
    directory = root / "config"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _fake_launchctl(directory: Path) -> Path:
    """Подставной launchctl: службы нет, чтобы тест не зависел от этого мака."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "launchctl"
    path.write_text(
        "#!/bin/bash\n"
        'if [ "${1:-}" = "print" ]; then echo "Could not find service" >&2; exit 113; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _run_script(
    *args: str, root: Path, extra_env: dict | None = None, path_prefix: Path | None = None
) -> subprocess.CompletedProcess:
    path = os.environ.get("PATH", "/usr/bin:/bin")
    if path_prefix is not None:
        path = f"{path_prefix}:{path}"
    env = {
        "PATH": path,
        "HOME": str(root),
        "LIFEOS_ROOT": str(root),
    }
    for key in ("LIFEOS_TELEGRAM_TOKEN", "LIFEOS_DB_PATH", "LIFEOS_LOGS_DIR", "LIFEOS_PYTHON"):
        env.pop(key, None)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SERVICE), *args],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Шаблон plist
# ---------------------------------------------------------------------------


def test_plist_template_is_valid_launchd_agent() -> None:
    """Шаблон — настоящий plist, а не текст с подстановками."""
    if not shutil.which("plutil"):
        pytest.skip("plutil доступен только на macOS")
    result = subprocess.run(["plutil", "-lint", str(PLIST)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout

    with PLIST.open("rb") as handle:
        data = plistlib.load(handle)

    assert data["Label"] == "com.lifeos.bot"
    assert data["RunAtLoad"] is True
    # Перезапуск только после падения: чистый выход не должен крутить цикл.
    assert data["KeepAlive"] == {"SuccessfulExit": False}

    arguments = data["ProgramArguments"]
    assert arguments[0] == "/usr/bin/caffeinate"  # мак не засыпает, пока бот нужен
    assert arguments[-1] == "__REPO__/tools/bot_launcher.py"

    env = data["EnvironmentVariables"]
    assert env["LIFEOS_ROOT"] == "__REPO__"
    assert env["LIFEOS_DB_PATH"] == "__DB_PATH__"
    assert data["StandardOutPath"] == "__LOG_DIR__/bot.launchd.log"


def test_plist_template_has_no_real_token() -> None:
    """В шаблоне не должно быть настоящего токена — только заглушка."""
    text = PLIST.read_text(encoding="utf-8")
    assert not TOKEN_PATTERN.search(text)
    # Ключ токена появляется в установленном plist только по явному флагу.
    assert "<!-- __TOKEN_LINE__ -->" in text


# ---------------------------------------------------------------------------
# Скрипт службы
# ---------------------------------------------------------------------------


def test_service_script_is_valid_bash() -> None:
    assert subprocess.run(["bash", "-n", str(SERVICE)]).returncode == 0


def test_service_script_knows_all_commands() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    for command in ("install", "start", "stop", "restart", "status", "logs", "uninstall"):
        assert f"{command})" in text, f"нет команды {command}"
    assert "launchctl" in text
    assert "plutil -lint" in text


def test_install_without_token_prints_hint_instead_of_traceback(tmp_path: Path) -> None:
    """Без токена: понятная фраза, код 1, никакого трейсбека."""
    result = _run_script("install", root=tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert TOKEN_HINT in output
    assert "Traceback" not in output
    assert not (tmp_path / "Library" / "LaunchAgents" / "com.lifeos.bot.plist").exists()


def test_status_without_service_and_token_reports_problem(tmp_path: Path) -> None:
    """status без службы и токена: код 1 и подсказка, что делать."""
    bin_dir = tmp_path / "bin"
    _fake_launchctl(bin_dir)
    result = _run_script("status", root=tmp_path, path_prefix=bin_dir)

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "не поставлена" in output
    assert "бот не работает" in output
    assert "bot-service.sh install" in output


def test_install_dry_run_writes_plist_with_token_only_on_request(tmp_path: Path) -> None:
    """--dry-run: plist собран, служба не тронута; токен попадает в plist только по флагу."""
    _write_config(tmp_path, {"telegram": {"token": FAKE_TOKEN}, "paths": {"db": "data/base.db"}})
    (tmp_path / "ops").mkdir()
    shutil.copy(PLIST, tmp_path / "ops" / "com.lifeos.bot.plist")
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    plist_path = launch_agents / "com.lifeos.bot.plist"

    result = _run_script("install", "--dry-run", root=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert plist_path.is_file()
    assert "LIFEOS_TELEGRAM_TOKEN" not in plistlib.loads(plist_path.read_bytes())["EnvironmentVariables"]
    assert "bot_launcher.py" in " ".join(plistlib.loads(plist_path.read_bytes())["ProgramArguments"])

    result = _run_script("install", "--dry-run", "--token-in-plist", root=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    installed = plistlib.loads(plist_path.read_bytes())
    assert installed["EnvironmentVariables"]["LIFEOS_TELEGRAM_TOKEN"] == FAKE_TOKEN
    assert plist_path.stat().st_mode & 0o777 == 0o600

# ---------------------------------------------------------------------------
# Launcher: окружение для службы
# ---------------------------------------------------------------------------


def test_launcher_without_token_exits_zero_with_one_line(tmp_path: Path) -> None:
    """launchd не должен перезапускать бота по кругу из-за пустого токена."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LIFEOS_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, str(LAUNCHER)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0
    assert "@BotFather" in (result.stdout + result.stderr)
    assert "Traceback" not in (result.stdout + result.stderr)


def test_launcher_prepares_token_and_db_from_config(tmp_path: Path) -> None:
    launcher = _load_launcher()
    _write_config(tmp_path, {"telegram": {"token": FAKE_TOKEN}, "paths": {"db": "data/base.db"}})

    env, problem = launcher.prepare_environment(tmp_path, environ={})

    assert problem == ""
    assert env["LIFEOS_TELEGRAM_TOKEN"] == FAKE_TOKEN
    assert env["LIFEOS_DB_PATH"] == str((tmp_path / "data" / "base.db").resolve())
    assert env["LIFEOS_LOGS_DIR"] == str((tmp_path / "data" / "logs").resolve())


def test_launcher_does_not_override_explicit_environment(tmp_path: Path) -> None:
    launcher = _load_launcher()
    _write_config(tmp_path, {"telegram": {"token": FAKE_TOKEN}, "paths": {"db": "data/base.db"}})
    given = {
        "LIFEOS_TELEGRAM_TOKEN": "999:env-token",
        "LIFEOS_DB_PATH": "/tmp/other.db",
        "LIFEOS_LOGS_DIR": "/tmp/logs",
    }

    env, problem = launcher.prepare_environment(tmp_path, environ=dict(given))

    assert problem == ""
    for key, value in given.items():
        assert env[key] == value


def test_launcher_explains_broken_config(tmp_path: Path) -> None:
    launcher = _load_launcher()
    directory = tmp_path / "config"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text("{ не json", encoding="utf-8")

    env, problem = launcher.prepare_environment(tmp_path, environ={})

    assert "config/config.json" in problem
    assert "LIFEOS_TELEGRAM_TOKEN" in problem
    assert "LIFEOS_TELEGRAM_TOKEN" not in env


# ---------------------------------------------------------------------------
# Лог: bot.log с ротацией
# ---------------------------------------------------------------------------


def _restore_logging(saved_handlers, saved_flag) -> None:
    root = logging.getLogger()
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


def test_bot_log_rotates(tmp_path: Path) -> None:
    """data/logs/bot.log ограничен по размеру: старые части уходят в bot.log.1."""
    settings = load_settings(env={"LIFEOS_LOGS_DIR": str(tmp_path / "logs")}, root=tmp_path)
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_flag = getattr(root, "_lifeos_configured", None)
    try:
        root.handlers = []
        if hasattr(root, "_lifeos_configured"):
            delattr(root, "_lifeos_configured")
        bot_main.configure_logging(settings)
        line = "x" * 900
        for _ in range(1400):
            logging.getLogger("lifeos.bot").info(line)
    finally:
        _restore_logging(saved_handlers, saved_flag)

    logs = tmp_path / "logs"
    assert (logs / "bot.log").stat().st_size <= 1024 * 1024
    assert (logs / "bot.log.1").is_file()
    assert "lifeos.bot" in (logs / "bot.log.1").read_text(encoding="utf-8", errors="replace") + ""


# ---------------------------------------------------------------------------
# Секреты: git и конфиг
# ---------------------------------------------------------------------------


def test_config_json_is_not_tracked_by_git() -> None:
    if not (PROJECT / ".git").exists():
        pytest.skip("не git-репозиторий")
    ignored = subprocess.run(
        ["git", "check-ignore", "config/config.json"], cwd=str(PROJECT), capture_output=True, text=True
    )
    tracked = subprocess.run(
        ["git", "ls-files", "config/config.json"], cwd=str(PROJECT), capture_output=True, text=True
    )
    assert ignored.returncode == 0, "config/config.json должен быть в .gitignore"
    assert tracked.stdout.strip() == "", "config/config.json не должен быть в индексе git"


def test_example_config_has_empty_token_field() -> None:
    example = json.loads((PROJECT / "config" / "config.example.json").read_text(encoding="utf-8"))
    assert example["telegram"]["token"] == ""
    assert example["telegram"]["allowed_chat_id"] == 0
