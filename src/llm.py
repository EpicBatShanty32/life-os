"""LLM-клиент Life OS (OpenAI-совместимый шлюз SAMP), только stdlib.

Публичный контракт — INTERFACES.md §4:
    llm = LLM(cfg)
    llm.available() -> bool
    llm.chat(messages, temperature=0.7, max_tokens=900) -> str | None
    llm.json_chat(messages, max_tokens=900) -> dict | None

Главное правило: клиент никогда не выбрасывает исключений. Любая проблема
(нет ключа, таймаут, сеть, 5xx, битый JSON) превращается в None.
При 429 делается один короткий повтор (пауза не больше 5 секунд).

Безопасность: ключ API и тела запросов/ответов в логи не попадают —
логируется только код ошибки (и то без деталей).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.samp.team/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash-1017"
DEFAULT_TIMEOUT = 60.0

#: Явный User-Agent обязателен: Cloudflare у шлюза SAMP отвечает 403
#: (error 1010) на дефолтный «Python-urllib/3.x». Проверено 2026-11-16.
USER_AGENT = "LifeOS/1.0"

#: Максимальная пауза перед повтором при HTTP 429, секунды.
MAX_RETRY_SLEEP = 5.0
#: Сколько всего попыток делать при 429 (1 обычная + 1 повтор).
MAX_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Вспомогательные преобразования конфига (терпимые к мусору)
# ---------------------------------------------------------------------------

def _as_float(value, default):
    """float или default — никогда не бросает."""
    try:
        if isinstance(value, bool):
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):  # NaN/inf
        return default
    return result


def _as_int(value, default):
    """int или default — никогда не бросает."""
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, low, high, default):
    number = _as_float(value, default)
    if number < low:
        return low
    if number > high:
        return high
    return number


def _as_bool(value, default: bool = True) -> bool:
    """Терпимый к строкам bool: «false»/«0»/«no»/«off» — это False."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "выкл")
    return bool(value)


def _retry_after(headers) -> float:
    """Пауза после 429: Retry-After (секунды) в разумных пределах."""
    wait = 1.0
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except Exception:
        raw = None
    if raw:
        try:
            parsed = float(str(raw).strip())
            if parsed == parsed:  # не NaN
                wait = parsed
        except (TypeError, ValueError):
            wait = 1.0
    if wait < 0.2:
        wait = 0.2  # не крутим холостой цикл
    if wait > MAX_RETRY_SLEEP:
        wait = MAX_RETRY_SLEEP
    return wait


# ---------------------------------------------------------------------------
# Извлечение JSON-объекта из «болтливого» ответа модели
# ---------------------------------------------------------------------------

def _balanced_object(text: str, start: int):
    """Возвращает первую сбалансированную {...}-подстроку с позиции start."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def extract_json_object(text) -> "dict | None":
    """Первый корректный JSON-объект из строки.

    Терпимо к ```json-обёрткам, преамбулам вроде «Вот результат:», мусору
    вокруг. Возвращает dict или None. Не бросает.
    """
    if not isinstance(text, str):
        return None
    start = text.find("{")
    attempts = 0
    while start != -1 and attempts < 20:
        attempts += 1
        candidate = _balanced_object(text, start)
        if candidate is not None:
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------

class LLM:
    """Клиент к OpenAI-совместимому `/chat/completions`.

    Все параметры берутся из cfg["llm"]; отсутствующие ключи не ломают клиент.
    """

    def __init__(self, cfg: dict):
        llm_cfg = {}
        try:
            if isinstance(cfg, dict):
                raw = cfg.get("llm")
                if isinstance(raw, dict):
                    llm_cfg = raw
        except Exception:  # совсем странный cfg — работаем на значениях по умолчанию
            llm_cfg = {}

        self.enabled = _as_bool(llm_cfg.get("enabled"), True)

        base_url = llm_cfg.get("base_url") or DEFAULT_BASE_URL
        self.base_url = str(base_url).strip().rstrip("/")

        api_key = llm_cfg.get("api_key") or ""
        self.api_key = str(api_key).strip()

        self.model = str(llm_cfg.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.timeout = _as_float(llm_cfg.get("timeout"), DEFAULT_TIMEOUT)
        if self.timeout <= 0:
            self.timeout = DEFAULT_TIMEOUT

        # диагностика (без секретов): заполняется последней ошибкой
        self.last_error = None  # type: "str | None"
        self.last_status = None  # type: "int | None"

        self._url = self.base_url + "/chat/completions"

    # -- состояние ---------------------------------------------------------

    def available(self) -> bool:
        """LLM включён и есть непустой api_key."""
        try:
            return bool(self.enabled and self.api_key and self.base_url)
        except Exception:
            return False

    def __repr__(self) -> str:
        return "LLM(model=%r, available=%r)" % (self.model, self.available())

    # -- основной вызов ----------------------------------------------------

    def chat(self, messages: list, temperature: float = 0.7,
             max_tokens: int = 900):
        """Запрос к модели. Возвращает текст ответа или None.

        Никогда не бросает. При HTTP 429 — одна короткая повторная попытка
        (суммарно не дольше MAX_RETRY_SLEEP секунд ожидания).
        """
        self.last_error = None
        self.last_status = None

        if not self.available():
            self.last_error = "unavailable"
            return None
        if not isinstance(messages, list) or not messages:
            self.last_error = "bad_messages"
            logger.error("LLM: пустой или некорректный список сообщений")
            return None

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": _clamp(temperature, 0.0, 2.0, 0.7),
            "max_tokens": max(1, _as_int(max_tokens, 900)),
        }
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            self.last_error = "bad_payload"
            logger.error("LLM: сообщения не сериализуются в JSON")
            return None

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": "Bearer " + self.api_key,
        }

        for attempt in range(1, MAX_ATTEMPTS + 1):
            request = urllib.request.Request(
                self._url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                self.last_status = exc.code
                self.last_error = "http_%s" % exc.code
                if exc.code == 429 and attempt < MAX_ATTEMPTS:
                    wait = _retry_after(getattr(exc, "headers", None))
                    logger.warning("LLM: HTTP 429, повтор через %.1f c", wait)
                    time.sleep(wait)
                    continue
                # тело ошибки может содержать чувствительные данные — не логируем
                logger.error("LLM: HTTP %s", exc.code)
                return None
            except Exception as exc:  # URLError, таймаут, обрыв связи и т.п.
                self.last_error = type(exc).__name__
                logger.error("LLM: ошибка запроса (%s)", type(exc).__name__)
                return None

            text = self._extract_text(raw)
            if text:
                self.last_status = 200
                return text
            self.last_error = "empty"
            logger.error("LLM: пустой ответ модели")
            return None

        return None

    def json_chat(self, messages: list, max_tokens: int = 900):
        """chat + первый {...} из ответа, разобранный json.loads. Или None."""
        text = self.chat(messages, temperature=0.2, max_tokens=max_tokens)
        if not text:
            return None
        try:
            return extract_json_object(text)
        except Exception as exc:  # на всякий случай: разбор не должен бросать
            self.last_error = type(exc).__name__
            return None

    # -- разбор HTTP-ответа ------------------------------------------------

    @staticmethod
    def _extract_text(raw) -> "str | None":
        """Достаёт текст ответа из JSON OpenAI-формата. None, если не вышло."""
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, (bytes, bytearray)):
            text = bytes(raw).decode("utf-8", "replace")
        else:
            return None
        if not text.strip():
            return None
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None
        content = LLM._content_from(data)
        if not content:
            return None
        content = content.strip()
        return content or None

    @staticmethod
    def _content_from(data) -> "str | None":
        """Поле choices[0].message.content (или choices[0].text) как строка."""
        if not isinstance(data, dict):
            return None
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = LLM._content_to_text(message.get("content"))
                    if content:
                        return content
                content = LLM._content_to_text(first.get("text"))
                if content:
                    return content
        # редкие совместимые варианты: {"content": "..."} / {"output_text": "..."}
        for key in ("content", "output_text", "text"):
            content = LLM._content_to_text(data.get(key))
            if content:
                return content
        return None

    @staticmethod
    def _content_to_text(value) -> "str | None":
        """content бывает строкой или списком частей [{"type":"text","text":...}]."""
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            parts = []
            for part in value:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    chunk = part.get("text")
                    if isinstance(chunk, str):
                        parts.append(chunk)
            joined = "".join(parts).strip()
            return joined or None
        return None
