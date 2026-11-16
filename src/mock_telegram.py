"""Мок-сервер Telegram Bot API для тестов и e2e (INTERFACES.md §11).

Поднимает локальный HTTP-сервер на ``127.0.0.1:<порт>`` и притворяется
Telegram Bot API настолько, насколько это нужно агенту:

* ``POST /bot<TOKEN>/getUpdates`` — long polling: отдаёт накопленную очередь
  апдейтов; ``offset`` подтверждает и удаляет более старые апдейты (как в
  настоящем Bot API). Таймаут запроса ограничен 1–2 секундами (в тестах не
  хотим ждать 20 секунд, как просит боевой клиент);
* ``POST /bot<TOKEN>/sendMessage`` — складывает текст сообщения в ``sent``;
* ``POST /bot<TOKEN>/getMe``, ``/deleteWebhook`` — заглушки «ок»;
* режимы ошибок ``mode="429" / "409" / "500"`` (и ``"401"``) — сервер отвечает
  ошибкой Telegram с ``description``/``retry_after``, чтобы тесты проверяли
  обработку сбоев в ``telegram.py`` и ``main.py``.

Публичный API (используется тестами qa и e2e):

    server = MockTelegramServer()
    port = server.start()            # -> int, сервер уже слушает
    server.push_update("привет", chat_id=123)
    server.wait_sent(timeout=5, min_count=1)   # ждёт ответов бота
    server.sent()                    # -> list[str]
    server.stop()

Всё потокобезопасно: сервер многопоточный, очередь и список отправленного
защищены одним ``RLock``/``Condition``. Внешних зависимостей нет — только
stdlib. Секретов мок не знает и не логирует.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ["MockTelegramServer"]

#: Максимальное время удержания long-poll запроса (секунды).
MAX_LONG_POLL_SECONDS = 1.5

#: Как отвечать на эмулируемые ошибки: код → (описание, retry_after|None).
_ERROR_PRESETS = {
    400: ("Bad Request", None),
    401: ("Unauthorized", None),
    403: ("Forbidden: bot was blocked by the user", None),
    409: ("Conflict: terminated by other getUpdates request; "
          "make sure that only one bot instance is running", None),
    429: ("Too Many Requests: retry after 2", 2),
    500: ("Internal Server Error", None),
    502: ("Bad Gateway", None),
}

_METHOD_RE = re.compile(r"/bot[^/]+/([A-Za-z0-9_]+)\s*$")


def _to_int(value, default=None):
    """int из str/int/None — без исключений."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class MockTelegramServer:
    """Локальный «Telegram»: очередь апдейтов, отправленные сообщения, ошибки.

    Args:
        token: токен, который ожидается в URL (по умолчанию любой — проверка
            не строгая: путь ``/bot<что угодно>/method`` принимается).
        mode: режим эмуляции ошибки: ``None`` (норма), ``"429"``, ``"409"``,
            ``"500"``, ``"401"``... Или можно менять на лету свойством
            ``server.mode = "429"``.
        host: адрес прослушивания (по умолчанию ``127.0.0.1``).
        max_long_poll: максимум удержания getUpdates, секунды (≤ 2).
    """

    def __init__(self, token: str = "123456:TEST-TOKEN", mode=None,
                 host: str = "127.0.0.1",
                 max_long_poll: float = MAX_LONG_POLL_SECONDS) -> None:
        self.token = str(token or "123456:TEST-TOKEN")
        self.host = host
        self._mode = mode
        self.max_long_poll = max(0.0, min(float(max_long_poll), 2.0))

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)

        self._updates: list[dict] = []
        self._next_update_id = 1
        self._next_message_id = 1

        self._sent: list[str] = []          # только тексты (контракт sent())
        self._sent_messages: list[dict] = []  # полные сообщения (chat_id и пр.)
        self._calls: list[tuple[str, dict]] = []
        self._errors: list[str] = []        # ошибки обработчика (диагностика)

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    # ------------------------------------------------------------- свойства
    @property
    def mode(self):
        """Текущий режим эмуляции ошибки (None — нормальная работа)."""
        with self._lock:
            return self._mode

    @mode.setter
    def mode(self, value) -> None:
        with self._lock:
            self._mode = None if value is None else str(value).strip()
            self._cond.notify_all()

    @property
    def url(self) -> str:
        """Базовый URL сервера (то, что передаётся как ``--mock-url``)."""
        port = self.port
        if not port:
            raise RuntimeError("MockTelegramServer ещё не запущен: вызовите start()")
        return "http://%s:%d" % (self.host, port)

    @property
    def api_base(self) -> str:
        """Алиас ``url`` — подходит для ``Telegram(api_base=...)``."""
        return self.url

    # --------------------------------------------------------------- запуск
    def start(self) -> int:
        """Запускает сервер и возвращает порт. Повторный вызов — тот же порт."""
        with self._lock:
            if self._httpd is not None:
                return int(self.port or 0)

        mock = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # не засоряем stderr тестов
            def log_message(self, *_args) -> None:  # noqa: D401
                return

            def do_GET(self) -> None:  # noqa: N802 - так требует http.server
                self._handle()

            def do_POST(self) -> None:  # noqa: N802
                self._handle()

            def _handle(self) -> None:
                try:
                    mock._handle_request(self)
                except Exception as exc:  # noqa: BLE001 - мок не должен падать
                    with mock._lock:
                        mock._errors.append("%s: %s" % (type(exc).__name__, exc))
                    try:
                        mock._respond(self, 500, {
                            "ok": False,
                            "error_code": 500,
                            "description": "mock server error",
                        })
                    except Exception:  # noqa: BLE001 - соединение уже закрыто
                        pass

        httpd = ThreadingHTTPServer((self.host, 0), _Handler)
        httpd.daemon_threads = True
        httpd.allow_reuse_address = True
        thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="mock-telegram",
            daemon=True,
        )
        thread.start()
        with self._lock:
            self._httpd = httpd
            self._thread = thread
            self.port = int(httpd.server_address[1])
            return self.port

    def stop(self) -> None:
        """Останавливает сервер (идемпотентно, без исключений)."""
        with self._lock:
            httpd = self._httpd
            thread = self._thread
            self._httpd = None
            self._thread = None
            self._cond.notify_all()
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=3.0)
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "MockTelegramServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ------------------------------------------------------------ апдейты
    def push_update(self, text: str, chat_id: int = 1,
                    update_id: int | None = None) -> dict:
        """Кладёт входящее сообщение в очередь и будит long-poll.

        Args:
            text: текст сообщения.
            chat_id: id чата/пользователя (в тестах — ``allowed_chat_id``).
            update_id: свой update_id (по умолчанию — следующий по порядку).

        Returns:
            Сам апдейт (dict) — удобно проверить update_id.
        """
        with self._cond:
            if update_id is None:
                update_id = self._next_update_id
            update_id = int(update_id)
            self._next_update_id = max(self._next_update_id, update_id + 1)

            message_id = self._next_message_id
            self._next_message_id += 1
            update = {
                "update_id": update_id,
                "message": {
                    "message_id": message_id,
                    "from": {
                        "id": int(chat_id),
                        "is_bot": False,
                        "first_name": "QA",
                    },
                    "chat": {"id": int(chat_id), "type": "private"},
                    "date": int(time.time()),
                    "text": "" if text is None else str(text),
                },
            }
            self._updates.append(update)
            self._updates.sort(key=lambda item: item["update_id"])
            self._cond.notify_all()
            return update

    def push_updates(self, texts, chat_id: int = 1) -> list[dict]:
        """Несколько апдейтов подряд (удобно для пакетных сценариев)."""
        return [self.push_update(text, chat_id=chat_id) for text in texts]

    def pending_updates(self) -> list[dict]:
        """Копия очереди ещё не подтверждённых апдейтов (диагностика)."""
        with self._lock:
            return [dict(item) for item in self._updates]

    # ---------------------------------------------------------- отправленное
    def sent(self) -> list[str]:
        """Все тексты, отправленные ботом через sendMessage (по порядку)."""
        with self._lock:
            return list(self._sent)

    def sent_messages(self) -> list[dict]:
        """Полные сообщения: ``{"chat_id", "text", "message_id"}``."""
        with self._lock:
            return [dict(item) for item in self._sent_messages]

    def wait_sent(self, timeout: float = 5.0, min_count: int = 1) -> list[str]:
        """Ждёт, пока бот отправит минимум ``min_count`` сообщений.

        Возвращает текущий список отправленных текстов (даже если таймаут
        истёк, а сообщений меньше — вызывающий решает, что это значит).
        """
        min_count = max(1, int(min_count))
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while len(self._sent) < min_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return list(self._sent)

    def wait_sent_count(self, count: int, timeout: float = 5.0) -> list[str]:
        """Ждёт ровно ``count`` отправленных сообщений (alias wait_sent)."""
        return self.wait_sent(timeout=timeout, min_count=count)

    # ------------------------------------------------------------ диагностика
    def calls(self) -> list[tuple[str, dict]]:
        """Список вызовов API: ``[(метод, payload), ...]``."""
        with self._lock:
            return [(method, dict(payload)) for method, payload in self._calls]

    def errors(self) -> list[str]:
        """Внутренние ошибки обработчика (пусто в нормальной работе)."""
        with self._lock:
            return list(self._errors)

    def reset(self) -> None:
        """Сбрасывает очередь, отправленное и счётчики (сервер не трогает)."""
        with self._cond:
            self._updates.clear()
            self._sent.clear()
            self._sent_messages.clear()
            self._calls.clear()
            self._errors.clear()
            self._next_update_id = 1
            self._next_message_id = 1
            self._mode = None
            self._cond.notify_all()

    # ================================================================ HTTP
    def _read_payload(self, handler: BaseHTTPRequestHandler) -> dict:
        """Тело запроса → dict: JSON или form-urlencoded (+ query string)."""
        payload: dict = {}
        try:
            query = urllib.parse.urlsplit(handler.path).query
            if query:
                for key, values in urllib.parse.parse_qs(query).items():
                    payload[key] = values[-1]
        except Exception:  # noqa: BLE001
            pass

        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = b""
        if length > 0:
            try:
                raw = handler.rfile.read(length)
            except Exception:  # noqa: BLE001
                raw = b""

        content_type = (handler.headers.get("Content-Type") or "").lower()
        if raw:
            if "application/json" in content_type or raw.lstrip().startswith(b"{"):
                try:
                    parsed = json.loads(raw.decode("utf-8", "replace"))
                    if isinstance(parsed, dict):
                        payload.update(parsed)
                except (ValueError, TypeError):
                    pass
            elif "application/x-www-form-urlencoded" in content_type or b"=" in raw:
                try:
                    form = urllib.parse.parse_qs(
                        raw.decode("utf-8", "replace"), keep_blank_values=True
                    )
                    for key, values in form.items():
                        payload[key] = values[-1]
                except (ValueError, TypeError):
                    pass
        return payload

    @staticmethod
    def _method_from_path(path: str) -> str:
        """Имя метода из ``/bot<token>/sendMessage`` (или последний сегмент)."""
        clean = path.split("?", 1)[0].rstrip("/")
        match = _METHOD_RE.search(clean)
        if match:
            return match.group(1)
        return clean.rsplit("/", 1)[-1] or ""

    def _handle_request(self, handler: BaseHTTPRequestHandler) -> None:
        method = self._method_from_path(handler.path)
        payload = self._read_payload(handler)

        with self._lock:
            self._calls.append((method, payload))
            mode = self._mode

        if mode is not None:
            self._respond_error(handler, mode)
            return

        if method == "getUpdates":
            self._handle_get_updates(handler, payload)
        elif method == "sendMessage":
            self._handle_send_message(handler, payload)
        elif method == "getMe":
            self._respond(handler, 200, {
                "ok": True,
                "result": {
                    "id": 987654321,
                    "is_bot": True,
                    "first_name": "Life OS (mock)",
                    "username": "lifeos_mock_bot",
                    "can_join_groups": False,
                    "can_read_all_group_messages": False,
                    "supports_inline_queries": False,
                },
            })
        elif method in ("deleteWebhook", "setWebhook", "getWebhookInfo", "close"):
            self._respond(handler, 200, {"ok": True, "result": True})
        elif method in ("sendChatAction", "setMyCommands", "deleteMyCommands"):
            self._respond(handler, 200, {"ok": True, "result": True})
        else:
            # Неизвестный метод: отвечаем «ок», как заглушка (тесты не падают).
            self._respond(handler, 200, {"ok": True, "result": {}})

    def _handle_get_updates(self, handler: BaseHTTPRequestHandler,
                            payload: dict) -> None:
        offset = _to_int(payload.get("offset"))
        requested_timeout = _to_int(payload.get("timeout"), 0) or 0
        wait_seconds = min(max(requested_timeout, 0), self.max_long_poll)
        deadline = time.monotonic() + wait_seconds

        with self._cond:
            if offset is not None:
                # offset — подтверждение: всё с update_id < offset удаляется.
                self._updates = [
                    item for item in self._updates
                    if int(item.get("update_id", 0)) >= offset
                ]
            while True:
                result = [
                    dict(item) for item in self._updates
                    if offset is None or int(item.get("update_id", 0)) >= offset
                ]
                if result:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)

        self._respond(handler, 200, {"ok": True, "result": result})

    def _handle_send_message(self, handler: BaseHTTPRequestHandler,
                             payload: dict) -> None:
        chat_id = _to_int(payload.get("chat_id"), 0) or 0
        text = payload.get("text")
        text = "" if text is None else str(text)

        with self._cond:
            message_id = self._next_message_id
            self._next_message_id += 1
            self._sent.append(text)
            self._sent_messages.append({
                "chat_id": chat_id,
                "text": text,
                "message_id": message_id,
            })
            self._cond.notify_all()

        self._respond(handler, 200, {
            "ok": True,
            "result": {
                "message_id": message_id,
                "from": {"id": 987654321, "is_bot": True,
                         "first_name": "Life OS (mock)",
                         "username": "lifeos_mock_bot"},
                "chat": {"id": chat_id, "type": "private"},
                "date": int(time.time()),
                "text": text,
            },
        })

    def _respond_error(self, handler: BaseHTTPRequestHandler, mode) -> None:
        """Ответ с эмулируемой ошибкой Telegram (429/409/500/401...)."""
        try:
            code = int(str(mode).strip())
        except (TypeError, ValueError):
            code = 500
        description, retry_after = _ERROR_PRESETS.get(
            code, ("Mock error %s" % code, None)
        )
        body = {
            "ok": False,
            "error_code": code,
            "description": description,
        }
        if retry_after is not None:
            body["parameters"] = {"retry_after": retry_after}
        self._respond(handler, code, body)

    @staticmethod
    def _respond(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
