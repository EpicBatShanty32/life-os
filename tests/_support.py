"""Общие помощники для unit-тестов Life OS (не тестовый модуль, не собирается).

Здесь живут:
* путь к project_root и правка ``sys.path`` для импорта ``src.*``;
* фабрика конфига на базе ``config.DEFAULTS``;
* простые фейки Telegram / LLM / Vault / dashboard_build, которые пишут
  вызовы в память (в сеть не ходят);
* константы времени Europe/Moscow.

Тесты запускаются из project_root:
    python3 -m unittest discover tests -v
"""

from __future__ import annotations

import copy
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import config as config_mod  # noqa: E402  (после правки sys.path)

# Модули проекта логируют через "lifeos*"; в тестах это лишний шум в stderr
# (ожидаемые warning/exception при проверке ошибок). Гасим централизованно.
_lifeos_logger = logging.getLogger("lifeos")
_lifeos_logger.handlers = []
_lifeos_logger.propagate = False
_lifeos_logger.addHandler(logging.NullHandler())
_lifeos_logger.setLevel(logging.CRITICAL)

# dashboard_data/parsing логируют как "src.*" (модуль импортируется пакетом).
_src_logger = logging.getLogger("src")
_src_logger.handlers = []
_src_logger.propagate = False
_src_logger.addHandler(logging.NullHandler())
_src_logger.setLevel(logging.CRITICAL)

MSK = ZoneInfo("Europe/Moscow")

#: chat_id, который тесты считают «разрешённым».
TEST_CHAT_ID = 111222333


def quiet_logger(name: str = "lifeos-tests") -> logging.Logger:
    """Логгер, который ничего не печатает (модули любят log.exception)."""
    logger = logging.getLogger(name)
    logger.handlers = []
    logger.propagate = False
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.CRITICAL)
    return logger


def at(year: int, month: int, day: int, hour: int = 12,
       minute: int = 0) -> datetime:
    """Дата-время Europe/Moscow (как в бою)."""
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def make_cfg(**overrides) -> dict:
    """Конфиг по умолчанию из config.DEFAULTS + точечные переопределения.

    Поддерживает вложенные ключи через ``a__b=value``:
        make_cfg(telegram__allowed_chat_id=1, llm__enabled=False)
    или плоские: make_cfg(schedule={...}) — заменяет секцию целиком.
    """
    cfg = copy.deepcopy(config_mod.DEFAULTS)
    cfg["_root"] = str(PROJECT_ROOT)
    cfg["_config_path"] = str(PROJECT_ROOT / "config" / "config.example.json")
    for key, value in overrides.items():
        if "__" in key:
            head, tail = key.split("__", 1)
            if not isinstance(cfg.get(head), dict):
                cfg[head] = {}
            cfg[head][tail] = value
        else:
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key] = value
            else:
                cfg[key] = value
    return cfg


# ===========================================================================
# Фейки для планировщика (в сеть и на диск не ходят)
# ===========================================================================
class FakeTelegram:
    """Собирает отправленные сообщения; умеет «падать» один раз."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.calls = 0
        self.fail_with: Exception | None = None

    def send(self, chat_id: int, text: str) -> None:
        self.calls += 1
        if self.fail_with is not None:
            error = self.fail_with
            self.fail_with = None
            raise error
        self.messages.append((int(chat_id), str(text)))

    # удобные проверки
    def texts(self) -> list[str]:
        return [text for _chat, text in self.messages]

    def last(self) -> str:
        return self.messages[-1][1] if self.messages else ""

    def sent_text(self) -> str:
        return "\n".join(self.texts())

    def clear(self) -> None:
        self.messages.clear()


class FakeLLM:
    """Поведение LLM задаётся полями; ничего не делает по умолчанию."""

    def __init__(self, available: bool = False,
                 chat_reply: str | None = None,
                 json_reply: dict | None = None,
                 raise_on_chat: bool = False) -> None:
        self._available = available
        self.chat_reply = chat_reply
        self.json_reply = json_reply
        self.raise_on_chat = raise_on_chat
        self.chat_calls = 0
        self.json_calls = 0

    def available(self) -> bool:
        return self._available

    def chat(self, messages, temperature: float = 0.7, max_tokens: int = 900):
        self.chat_calls += 1
        if self.raise_on_chat:
            raise RuntimeError("fake LLM error")
        return self.chat_reply

    def json_chat(self, messages, max_tokens: int = 900):
        self.json_calls += 1
        if self.raise_on_chat:
            raise RuntimeError("fake LLM error")
        return self.json_reply


class FakeVault:
    """Считает вызовы write_entry/rewrite_goals/write_mirror/sync_pending."""

    def __init__(self, write_ok: bool = True) -> None:
        self.write_ok = write_ok
        self.entries: list[dict] = []
        self.rewrite_goals_calls = 0
        self.mirror_calls = 0
        self.sync_calls = 0
        self.sync_result = 0

    def write_entry(self, entry: dict) -> bool:
        self.entries.append(dict(entry))
        return self.write_ok

    def rewrite_goals(self) -> bool:
        self.rewrite_goals_calls += 1
        return True

    def write_mirror(self, entries) -> None:
        self.mirror_calls += 1

    def sync_pending(self) -> int:
        self.sync_calls += 1
        return self.sync_result


class FakeDashboard:
    """callable(cfg, storage) -> str|None, запоминает вызовы."""

    def __init__(self, path: str = "dashboard/index.html") -> None:
        self.path = path
        self.calls = 0

    def __call__(self, cfg, storage):
        self.calls += 1
        return self.path
