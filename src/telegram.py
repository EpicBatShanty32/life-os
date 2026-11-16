"""Клиент Telegram Bot API на стандартной библиотеке (urllib).

Реализует ровно то, что нужно агенту: long polling ``getUpdates`` и отправка
текстовых сообщений с разбивкой длинных текстов. Все ошибки упаковываются в
``TelegramError`` — вызывающий код (планировщик, main) решает, что делать:
429/409 — backoff, 401/403 — сообщить о проблеме с токеном, сеть — повторить.

Секреты: токен никогда не попадает в тексты исключений и в repr объекта.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "Telegram", "TelegramError", "split_message", "MAX_MESSAGE_LEN",
    "sanitize_text",
]

# Telegram режет сообщения длиннее 4096 символов; берём с запасом.
MAX_MESSAGE_LEN = 4000

#: Адрес Bot API по умолчанию.
DEFAULT_API_BASE = "https://api.telegram.org"

#: Хосты, для которых допустим незашифрованный ``http://`` (только локальные
#: тесты и e2e-мок; токен в открытом виде по сети так не уходит).
LOCAL_HTTP_HOSTS = ("127.0.0.1", "localhost", "::1")


#: Все code point'ы UTF-16-суррогатов. В корректном Python-str их быть не может
#: (эмодзи — один code point), поэтому любой символ отсюда — «lone surrogate».
_SURROGATE_TABLE = {code: None for code in range(0xD800, 0xE000)}


def sanitize_text(text) -> str:
    """Убирает lone surrogate (U+D800–U+DFFF), которые ломают UTF-8.

    ``str`` в Python 3 может содержать одиночные суррогаты: они приходят из
    «битых» апдейтов Telegram и из чужих API. Такой символ не кодируется в
    UTF-8, поэтому ``json.dumps(...).encode("utf-8")`` бросает
    ``UnicodeEncodeError``, а SQLite теряет запись. Все тексты, уходящие в
    Telegram (и сохраняемые планировщиком), проходят через эту функцию.

    Args:
        text: строка или что угодно, приводимое к строке (``None`` → ``""``).

    Returns:
        Строка без суррогатных code point'ов.
    """
    if text is None:
        return ""
    value = text if isinstance(text, str) else str(text)
    if not value:
        return ""
    return value.translate(_SURROGATE_TABLE)


def _sanitize_payload(value):
    """Рекурсивно чистит строки в теле запроса Bot API от lone surrogate."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        cleaned: dict = {}
        for key, item in value.items():
            new_key = sanitize_text(key) if isinstance(key, str) else key
            cleaned[new_key] = _sanitize_payload(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize_payload(item) for item in value]
    return value


class TelegramError(Exception):
    """Ошибка обращения к Telegram Bot API.

    Attributes:
        message: человекочитаемое описание (без токена!).
        status: HTTP-статус или ``error_code`` от Telegram, если известен.
        retry_after: сколько секунд подождать (для 429), если Telegram сообщил.
    """

    def __init__(self, message: str, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:  # чтобы логи читались без скобок кортежа
        return self.message


def _utf16_len(text: str) -> int:
    """Длина строки в UTF-16 code units — именно её считает Telegram.

    Python ``len`` считает code points, поэтому эмодзи и другие символы вне
    BMP «весят» в Bot API в два раза больше. Без этого суррогатная пара на
    границе лимита даёт 400 ``message is too long``.
    """
    try:
        return len(text.encode("utf-16-le")) // 2
    except Exception:  # pragma: no cover — не бывает для валидного str
        return len(text)


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Режет текст на части не больше ``limit`` UTF-16 units по границам строк.

    Длинная строка без переводов строк режется жёстко. Пустые куски не
    возвращаются; текст короче лимита возвращается одним элементом.

    Контракт реконструкции (F10 адверсариального теста). Перевод строки на
    стыке чанков не теряется: разделителем служит сама граница — каждый чанк
    уходит отдельным сообщением Telegram, и визуальный перевод строки между
    сообщениями сохраняется. Поэтому:

    * ``"\\n".join(parts)`` == исходник, если текст резался только по границам
      строк (обычный случай: дневник, отчёты, ответы LLM);
    * ``"".join(parts)`` == исходник, если ни одна строка не разрезана жёстко.

    Жёсткая резка (строка длиннее ``limit``) разделителя между чанками не даёт:
    такие чанки склеиваются через ``""``. Держать ``"\\n"`` внутри чанка нельзя —
    тогда при склейке ``"\\n".join(parts)`` на каждом стыке появлялся бы лишний
    перевод строки.
    """
    if not text:
        return []
    if _utf16_len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        # Строка сама длиннее лимита — режем её жёстко, не разрывая суррогаты.
        while _utf16_len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            cut = 0
            used = 0
            for ch in line:
                width = _utf16_len(ch)
                if used + width > limit:
                    break
                used += width
                cut += 1
            if cut <= 0:  # pragma: no cover — защита от limit <= 0
                cut = 1
            parts.append(line[:cut])
            line = line[cut:]
        candidate = line if not current else current + "\n" + line
        if _utf16_len(candidate) > limit and current:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return [part for part in parts if part]


def _parse_api_base(value) -> str:
    """Строгая проверка ``telegram.api_base`` и нормализация адреса.

    Разрешено только ``https://`` (и ``http://`` для 127.0.0.1/localhost —
    локальные тесты и e2e-мок). Путь, query, fragment и логин/пароль в адресе
    запрещены: из-за них токен уезжает не туда. Именно поэтому
    ``https://api.telegram.org@evil.tld`` отклоняется — по ``urlsplit``
    реальный хост здесь ``evil.tld``, а «api.telegram.org» лишь userinfo.

    Args:
        value: адрес из конфига (может быть пустым — тогда значение по умолчанию).

    Returns:
        Нормализованный адрес вида ``https://api.telegram.org`` (без хвоста ``/``).

    Raises:
        TelegramError: адрес небезопасен или не разбирается. Текст ошибки не
            содержит ни URL целиком, ни токена.
    """
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return DEFAULT_API_BASE
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        raise TelegramError(
            "Некорректный telegram.api_base: адрес не разбирается. Нужен вид "
            "https://api.telegram.org (со схемой http/https, без пробелов)."
        ) from None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise TelegramError(
            "Некорректный telegram.api_base: нужен адрес вида "
            "https://api.telegram.org (со схемой http/https, без пробелов)."
        )
    if parts.username or parts.password:
        raise TelegramError(
            "Некорректный telegram.api_base: в адресе не должно быть логина, "
            "пароля или лишнего «@» перед хостом."
        )
    try:
        host = (parts.hostname or "").lower()
    except ValueError:  # битый IPv6 в квадратных скобках
        host = ""
    if not host:
        raise TelegramError(
            "Некорректный telegram.api_base: не удалось выделить хост адреса."
        )
    if parts.path:
        raise TelegramError(
            "Некорректный telegram.api_base: в адресе не должно быть пути — "
            "только схема, хост и (необязательно) порт."
        )
    if parts.query or parts.fragment:
        raise TelegramError(
            "Некорректный telegram.api_base: уберите «?» и «#» из адреса."
        )
    if scheme == "http" and host not in LOCAL_HTTP_HOSTS:
        raise TelegramError(
            "Некорректный telegram.api_base: http:// разрешён только для "
            "127.0.0.1 и localhost (тесты). Для Telegram нужен https://."
        )
    return "%s://%s" % (scheme, parts.netloc)


class Telegram:
    """Минимальный клиент Bot API: ``call``, ``send``, ``get_updates``.

    POST идёт с JSON-телом (Bot API это принимает) — так проще и надёжнее,
    чем form-encoded с вложенным JSON.
    """

    def __init__(self, token: str, api_base: str = DEFAULT_API_BASE) -> None:
        if not token or not str(token).strip():
            raise TelegramError(
                "Пустой токен Telegram-бота. Укажите telegram.token в config/config.json "
                "(токен выдаёт @BotFather)."
            )
        self._token = str(token).strip()
        self.api_base = _parse_api_base(api_base)
        self.timeout = 60

    def __repr__(self) -> str:  # токен не показываем даже в отладке
        return "Telegram(api_base=%r, token=***)" % (self.api_base,)

    # ------------------------------------------------------------------ API
    def _url(self, method: str) -> str:
        """Собирает URL метода по уже проверенному ``api_base``.

        ``api_base`` провалидирован в :meth:`__init__` (``_parse_api_base``),
        а токен подставляется только здесь. Текст любой ошибки намеренно не
        содержит ни URL, ни токена.
        """
        base = str(self.api_base or "")
        if not base.lower().startswith(("http://", "https://")):
            raise TelegramError(
                "Некорректный telegram.api_base: нужен адрес вида "
                "https://api.telegram.org (со схемой http/https, без пробелов)."
            )
        return "%s/bot%s/%s" % (base, self._token, method)

    def call(self, method: str, payload: dict | None = None,
             timeout: int = 60) -> dict:
        """Вызывает метод Bot API и возвращает поле ``result``.

        Raises:
            TelegramError: при ошибке сети, любом HTTP-статусе != 200 или
                ``ok: false`` в ответе. Для 429 заполняется ``retry_after``.
        """
        body = payload if payload is not None else {}
        try:
            # Lone surrogate в тексте не кодируется в UTF-8 — чистим тело до
            # сериализации, чтобы сообщение ушло, а не упало UnicodeEncodeError.
            data = json.dumps(
                _sanitize_payload(body), ensure_ascii=False
            ).encode("utf-8")
        except UnicodeEncodeError:
            raise TelegramError(
                "Не удалось отправить запрос Telegram (метод %s): в тексте "
                "недопустимые символы (lone surrogate). Сообщение не отправлено."
                % method
            ) from None
        try:
            # Request строится внутри try: ValueError/InvalidURL от кривого
            # api_base не должны улететь наружу с URL, в котором есть токен.
            request = urllib.request.Request(
                self._url(method),
                data=data,
                method="POST",
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                    "User-Agent": "LifeOS/1.0 (+python-urllib)",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except (ValueError, http.client.InvalidURL):
            raise TelegramError(
                "Некорректный telegram.api_base: проверьте схему и хост "
                "(ожидается https://api.telegram.org). HTTP-запрос не построен."
            ) from None
        except urllib.error.HTTPError as exc:
            # Тело ошибки Telegram — JSON {"ok": false, "description": ...}.
            raw = b""
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001 - тело ошибки может не прочитаться
                pass
            status = int(exc.code)
            description = ""
            retry_after: float | None = None
            try:
                parsed = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(parsed, dict):
                    description = str(parsed.get("description") or "")
                    parameters = parsed.get("parameters") or {}
                    if parameters.get("retry_after") is not None:
                        retry_after = float(parameters["retry_after"])
            except (ValueError, TypeError):
                pass
            if not description:
                description = "HTTP %s" % status
            if status == 429:
                raise TelegramError(
                    "Telegram ограничил частоту запросов (429): %s" % description,
                    status=status, retry_after=retry_after,
                ) from None
            if status == 409:
                raise TelegramError(
                    "Конфликт getUpdates (409): вероятно, запущен второй экземпляр бота. %s"
                    % description,
                    status=status, retry_after=retry_after,
                ) from None
            if status in (401, 403):
                raise TelegramError(
                    "Telegram отклонил запрос (%s): проверьте telegram.token и доступ к чату. %s"
                    % (status, description),
                    status=status,
                ) from None
            raise TelegramError(
                "Ошибка Telegram API (%s): %s" % (status, description),
                status=status, retry_after=retry_after,
            ) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise TelegramError(
                "Нет связи с Telegram (метод %s): %s" % (method, reason)
            ) from None

        if not raw:
            raise TelegramError("Пустой ответ Telegram API (метод %s)" % method)
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise TelegramError(
                "Не удалось разобрать ответ Telegram (метод %s)" % method
            ) from None
        if not isinstance(parsed, dict):
            raise TelegramError("Неожиданный формат ответа Telegram (метод %s)" % method)
        if not parsed.get("ok"):
            status = parsed.get("error_code")
            retry_after = None
            parameters = parsed.get("parameters") or {}
            if parameters.get("retry_after") is not None:
                retry_after = float(parameters["retry_after"])
            raise TelegramError(
                "Telegram вернул ошибку (метод %s): %s"
                % (method, parsed.get("description") or "без описания"),
                status=int(status) if isinstance(status, int) else None,
                retry_after=retry_after,
            )
        return parsed.get("result")

    def send(self, chat_id: int, text: str) -> None:
        """Отправляет текст; длиннее 4000 символов — несколькими сообщениями."""
        if text is None:
            return
        text = sanitize_text(text)
        if not text.strip():
            return
        for chunk in split_message(text):
            self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )

    def get_updates(self, offset: int | None, timeout: int = 20) -> list[dict]:
        """long polling getUpdates.

        Args:
            offset: id последнего обработанного апдейта + 1 (или None).
            timeout: сколько секунд Telegram может держать запрос (0 — сразу).

        Returns:
            Список апдейтов (пустой при таймауте).
        """
        payload: dict = {
            "timeout": int(timeout),
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = int(offset)
        # HTTP-таймаут должен быть заметно больше long-poll таймаута.
        http_timeout = max(int(timeout) + 15, 20)
        result = self.call("getUpdates", payload, timeout=http_timeout)
        if isinstance(result, list):
            return result
        return []
