"""Интент-роутер Life OS v3: текст сообщения -> запись журнала и метрики.

Два движка:

* :class:`LLMRouter` — OpenAI-совместимый шлюз (как в ``src/llm.py`` v0.x,
  логика переиспользована без импорта старого пакета). Работает только при
  наличии ключа и включённом LLM; любой сбой (таймаут, битый JSON, неверная
  схема) молча уступает правилам.
* :class:`ScriptedFallback` — регулярки без сети: «вес 81.2, жир 18.5»,
  «вода 300», «спал 7.5», «жим 80х5», «съел 600 ккал и 40 г белка»,
  «тренировка 45 минут».

Результат всегда имеет вид::

    {
        "kind": "note|food|workout|weight|voice|video|doc",
        "title": "Вес 81,2 кг, жир 18,5 %",
        "text": "исходный текст",
        "category": "Тело",
        "metrics": {"weight": 81.2, "fat": 18.5},
        "engine": "llm" | "rules",
    }

Модуль не бросает исключений наружу и не ходит в сеть, пока не вызван
реальный шлюз с непустым ключом. В тестах используется фейковый шлюз.
"""

import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_KINDS",
    "LLMRouter",
    "METRIC_SPECS",
    "OpenAICompatibleGateway",
    "ScriptedFallback",
    "build_router",
    "extract_json_object",
    "parse_text",
    "validate_intent",
]

ALLOWED_KINDS = frozenset({"note", "food", "workout", "weight", "voice", "video", "doc"})

#: Допустимые числовые метрики: единица, минимум, максимум.
METRIC_SPECS: Mapping[str, tuple] = {
    "weight": ("кг", 20.0, 350.0),
    "fat": ("%", 1.0, 70.0),
    "muscle": ("кг", 1.0, 150.0),
    "water": ("мл", 1.0, 10000.0),
    "sleep": ("ч", 0.1, 24.0),
    "calories": ("ккал", 1.0, 20000.0),
    "protein": ("г", 0.5, 500.0),
    "duration_min": ("мин", 1.0, 1440.0),
    "mood": ("из 10", 1.0, 10.0),
    # Силовые упражнения — не колонки metrics, но нужны в meta записи.
    "load_kg": ("кг", 1.0, 500.0),
    "reps": ("раз", 1.0, 100.0),
}

_MAX_TEXT = 4000
_MAX_TITLE = 120
_MAX_CATEGORY = 40
_MAX_METRICS = 12

DEFAULT_BASE_URL = "https://api.samp.team/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash-1017"
DEFAULT_TIMEOUT = 20.0
USER_AGENT = "LifeOS/3.0"

SYSTEM_PROMPT = """Ты — разборщик сообщений личного дневника Life OS (русский язык).
Верни ТОЛЬКО один JSON-объект без пояснений и markdown:
{
  "kind": "note|food|workout|weight",
  "title": "краткая подпись до 80 символов",
  "text": "исходный текст пользователя",
  "category": "Тело|Питание|Тренировка|Сон|Вода|Настроение|Заметка",
  "metrics": {"weight": 81.2, "fat": 18.5}
}

Правила:
- metrics — только числа из сообщения, ключи из списка:
  weight (кг), fat (%), muscle (кг), water (мл), sleep (ч),
  calories (ккал), protein (г), duration_min (мин), mood (1-10).
- «жим 80х5» / «присед 100 на 3»: kind=workout, metrics={"load_kg": 80, "reps": 5},
  в title укажи упражнение.
- Если чисел нет — kind=note, metrics={}.
- Не выдумывай значения, которых не было в сообщении.
- Никаких советов и оценок: только разбор."""


def extract_json_object(text: Any) -> Optional[Dict[str, Any]]:
    """Первый корректный JSON-объект из ответа модели (терпимо к обёрткам)."""
    if not isinstance(text, str) or not text:
        return None
    decoder = json.JSONDecoder()
    start = text.find("{")
    attempts = 0
    while start != -1 and attempts < 20:
        attempts += 1
        try:
            parsed, _ = decoder.raw_decode(text[start:])
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        start = text.find("{", start + 1)
    return None


class OpenAICompatibleGateway:
    """Минимальный клиент OpenAI-совместимого ``/chat/completions``.

    Никогда не бросает наружу и не логирует ключ. При 429 — один короткий
    повтор. Таймаут ограничен сверху 30 секундами, чтобы не вешать бота.
    """

    #: Допустимые подсказки об усилии рассуждения (как в OpenAI/DeepSeek API).
    EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        enabled: bool = True,
        reasoning_effort: str = "",
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.enabled = bool(enabled)
        self.reasoning_effort = self._effort(reasoning_effort)
        try:
            self.timeout = min(max(float(timeout), 1.0), 30.0)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT
        #: Провайдер понимает поле ``reasoning``; после HTTP 400 — выключаем.
        self.reasoning_supported = True
        self.last_error = ""
        self.last_status = None

    def available(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url)

    def __repr__(self) -> str:  # ключ не попадает даже в отладочный вывод
        return "OpenAICompatibleGateway(model=%r, available=%r)" % (
            self.model,
            self.available(),
        )

    @classmethod
    def _effort(cls, value: Any) -> str:
        """Нормализует ``reasoning_effort``; неизвестное значение — пусто."""
        text = str(value or "").strip().casefold()
        return text if text in cls.EFFORTS else ""

    def json_chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 700,
        reasoning_effort: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        self.last_error = ""
        self.last_status = None
        if not self.available() or not messages:
            self.last_error = "unavailable"
            return None

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max(1, min(int(max_tokens), 2000)),
        }
        effort = self._effort(reasoning_effort) if reasoning_effort is not None else self.reasoning_effort
        if effort and self.reasoning_supported:
            # Стандартное поле OpenAI: «сколько думать» перед ответом.
            payload["reasoning"] = {"effort": effort}
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            self.last_error = "bad_payload"
            return None
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": "Bearer " + self.api_key,
        }
        url = self.base_url + "/chat/completions"
        try:
            call_timeout = min(max(float(timeout), 1.0), 30.0) if timeout is not None else self.timeout
        except (TypeError, ValueError):
            call_timeout = self.timeout
        reasoning_retry = False

        for attempt in range(1, 3):
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=call_timeout) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                self.last_status = exc.code
                self.last_error = "http_%s" % exc.code
                if exc.code == 400 and "reasoning" in payload and not reasoning_retry:
                    # Провайдер не знает поле reasoning: один повтор без него.
                    # Запоминаем на процесс, чтобы не платить за 400 каждый раз.
                    reasoning_retry = True
                    self.reasoning_supported = False
                    logger.warning(
                        "LLM: провайдер не принял reasoning=%s (HTTP 400) — "
                        "повторяю без подсказки", effort
                    )
                    payload.pop("reasoning", None)
                    try:
                        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    except (TypeError, ValueError):
                        self.last_error = "bad_payload"
                        return None
                    continue
                if exc.code == 429 and attempt == 1:
                    wait = _retry_after(getattr(exc, "headers", None))
                    logger.warning("LLM: HTTP 429, повтор через %.1f c", wait)
                    time.sleep(wait)
                    continue
                logger.error("LLM: HTTP %s", exc.code)
                return None
            except Exception as exc:  # сеть, таймаут, TLS и т.п.
                self.last_error = type(exc).__name__
                logger.error("LLM: ошибка запроса (%s)", type(exc).__name__)
                return None

            text = _content_from_response(raw)
            if not text:
                self.last_error = "empty"
                logger.error("LLM: пустой ответ модели")
                return None
            result = extract_json_object(text)
            if result is None:
                self.last_error = "bad_json"
                logger.error("LLM: ответ не содержит JSON-объекта")
            return result
        return None

    def chat_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 1200,
        reasoning_effort: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Один шаг диалога с инструментами (OpenAI ``tool_calls``).

        Возвращает нормализованное сообщение модели::

            {"content": "...", "tool_calls": [{"id", "name", "arguments"}, ...]}

        В отличие от :meth:`json_chat` ответ не обязан быть JSON: модель либо
        зовёт инструменты, либо говорит словами. ``None`` — вызов не удался
        (нет ключа, сеть, HTTP-ошибка): вызывающий уходит на запасной путь.
        """
        self.last_error = ""
        self.last_status = None
        if not self.available() or not messages:
            self.last_error = "unavailable"
            return None

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": max(1, min(int(max_tokens), 3000)),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        effort = (
            self._effort(reasoning_effort)
            if reasoning_effort is not None
            else self.reasoning_effort
        )
        if effort and self.reasoning_supported:
            payload["reasoning"] = {"effort": effort}
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            self.last_error = "bad_payload"
            return None
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": "Bearer " + self.api_key,
        }
        url = self.base_url + "/chat/completions"
        try:
            call_timeout = (
                min(max(float(timeout), 1.0), 120.0) if timeout is not None else self.timeout
            )
        except (TypeError, ValueError):
            call_timeout = self.timeout

        for attempt in range(1, 3):
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=call_timeout) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                self.last_status = exc.code
                self.last_error = "http_%s" % exc.code
                if exc.code == 400 and "reasoning" in payload:
                    # Провайдер не знает поле reasoning: повтор без него.
                    self.reasoning_supported = False
                    payload.pop("reasoning", None)
                    try:
                        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    except (TypeError, ValueError):
                        self.last_error = "bad_payload"
                        return None
                    continue
                if exc.code == 429 and attempt == 1:
                    time.sleep(_retry_after(getattr(exc, "headers", None)))
                    continue
                logger.error("LLM tools: HTTP %s", exc.code)
                return None
            except Exception as exc:  # сеть, таймаут, TLS и т.п.
                self.last_error = type(exc).__name__
                logger.error("LLM tools: ошибка запроса (%s)", type(exc).__name__)
                return None
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except (TypeError, ValueError):
                self.last_error = "bad_response"
                return None
            choices = data.get("choices") if isinstance(data, dict) else None
            message = (choices or [{}])[0].get("message") if choices else None
            if not isinstance(message, dict):
                self.last_error = "no_message"
                return None
            content = message.get("content")
            if not isinstance(content, str):
                content = "" if content is None else str(content)
            calls: List[Dict[str, Any]] = []
            for call in message.get("tool_calls") or []:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function") or {}
                name = str((function or {}).get("name") or "").strip()
                if not name:
                    continue
                arguments = (function or {}).get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments or {}, ensure_ascii=False)
                calls.append(
                    {
                        "id": str(call.get("id") or ""),
                        "name": name,
                        "arguments": arguments,
                    }
                )
            if not calls and not content.strip():
                self.last_error = "empty"
                return None
            return {"content": content.strip(), "tool_calls": calls}
        return None


class ScriptedFallback:
    """Разбор без сети: регулярки на типовые фразы дневника."""

    _NUMBER = r"(\d{1,5}(?:[.,]\d{1,2})?)"

    _WEIGHT_RE = re.compile(
        r"(?:\bвес\b|\bвешу\b|\bвесил[аи]?\b)\D{0,12}" + _NUMBER, re.IGNORECASE
    )
    _FAT_RE = re.compile(r"\bжир\w*\b\D{0,12}" + _NUMBER, re.IGNORECASE)
    _MUSCLE_RE = re.compile(r"\bмышц\w*\b\D{0,12}" + _NUMBER, re.IGNORECASE)
    _CALORIES_RE = re.compile(_NUMBER + r"\s*(?:ккал|калор\w*)\b", re.IGNORECASE)
    _PROTEIN_RE = re.compile(
        _NUMBER + r"\s*(?:г|гр|грамм\w*)?\s*(?:белка|протеина|protein)\b",
        re.IGNORECASE,
    )
    _WATER_KEYWORD_RE = re.compile(
        r"(?:\bвода\b|\bводы\b|\bвыпил[аи]?\b|\bпить\w*|\bводы\b)\D{0,12}"
        + _NUMBER
        + r"\s*(мл|ml|л|l)?\b",
        re.IGNORECASE,
    )
    _WATER_UNIT_RE = re.compile(_NUMBER + r"\s*(мл|ml|л|l)\b", re.IGNORECASE)
    _SLEEP_RE = re.compile(
        r"(?:\bспал[аи]?\b|\bпоспал[аи]?\b|\bсон\b)\D{0,12}"
        + _NUMBER
        + r"\s*(ч|час\w*)?\b",
        re.IGNORECASE,
    )
    _LIFT_RE = re.compile(
        r"(?P<exercise>жим\w*|присед\w*|тяга\w*|становая|подъ[её]м\w*)\D{0,16}?"
        r"(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:кг)?\s*(?:[xх×*]|на)\s*(\d{1,2})",
        re.IGNORECASE,
    )
    _WORKOUT_RE = re.compile(
        r"(?:\bтренировк\w*|\bбег\w*|\bзал\w*|\bходьб\w*|\bплава\w*|\bвелосипед\w*)"
        r"\D{0,16}(\d{1,3})\s*(?:мин|минут\w*)",
        re.IGNORECASE,
    )
    _MOOD_RE = re.compile(r"\bнастроени\w*\b\D{0,12}(\d{1,2})", re.IGNORECASE)

    def parse(self, text: str) -> Dict[str, Any]:
        original = (text or "").strip()
        normalized = original.replace(",", ".")
        metrics: Dict[str, Any] = {}
        notes: List[str] = []
        kind = "note"
        category = "Заметка"

        lift = self._first(self._LIFT_RE, normalized)
        if lift is not None:
            exercise = _clean_exercise(lift.group("exercise"))
            load = _number(lift.group(2))
            reps = _number(lift.group(3))
            if load is not None and reps is not None:
                metrics["load_kg"] = load
                metrics["reps"] = reps
                metrics["exercise"] = exercise
                kind = "workout"
                category = "Тренировка"
                notes.append("%s %s×%s" % (exercise, _fmt(load), _fmt(reps)))

        weight = _number_match(self._WEIGHT_RE, normalized)
        fat = _number_match(self._FAT_RE, normalized)
        muscle = _number_match(self._MUSCLE_RE, normalized)
        if weight is not None:
            metrics["weight"] = weight
            notes.append("вес %s кг" % _fmt(weight))
            kind, category = "weight", "Тело"
        if fat is not None:
            metrics["fat"] = fat
            notes.append("жир %s %%" % _fmt(fat))
            if kind == "note":
                kind, category = "weight", "Тело"
        if muscle is not None:
            metrics["muscle"] = muscle
            notes.append("мышцы %s кг" % _fmt(muscle))
            if kind == "note":
                kind, category = "weight", "Тело"

        water = _water_ml(self._WATER_KEYWORD_RE, self._WATER_UNIT_RE, normalized)
        if water is not None:
            metrics["water"] = water
            notes.append("вода %s мл" % _fmt(water))
            if kind == "note":
                category = "Вода"

        sleep = _number_match(self._SLEEP_RE, normalized)
        if sleep is not None and 0 < sleep <= 24:
            metrics["sleep"] = sleep
            notes.append("сон %s ч" % _fmt(sleep))
            if kind == "note":
                category = "Сон"

        calories = _number_match(self._CALORIES_RE, normalized)
        protein = _number_match(self._PROTEIN_RE, normalized)
        if calories is not None:
            metrics["calories"] = calories
            notes.append("%s ккал" % _fmt(calories))
            kind, category = "food", "Питание"
        if protein is not None:
            metrics["protein"] = protein
            notes.append("белок %s г" % _fmt(protein))
            if kind == "note":
                kind, category = "food", "Питание"

        duration = _number_match(self._WORKOUT_RE, normalized)
        if duration is not None:
            metrics["duration_min"] = duration
            notes.append("тренировка %s мин" % _fmt(duration))
            if kind in ("note", "weight") and lift is None:
                kind, category = "workout", "Тренировка"

        mood = _number_match(self._MOOD_RE, normalized)
        if mood is not None and 1 <= mood <= 10:
            metrics["mood"] = mood
            notes.append("настроение %s/10" % _fmt(mood))
            if kind == "note":
                category = "Настроение"

        title = ", ".join(notes) if notes else _shorten(original, _MAX_TITLE)
        if not title:
            title = "Заметка"
        metrics = _clean_metrics(metrics)
        return {
            "kind": kind,
            "title": _shorten(_capitalize(title), _MAX_TITLE),
            "text": original[:_MAX_TEXT],
            "category": category[:_MAX_CATEGORY],
            "metrics": metrics,
            "engine": "rules",
        }

    @staticmethod
    def _first(pattern: re.Pattern, text: str):
        try:
            return pattern.search(text)
        except (TypeError, RuntimeError):
            return None


class LLMRouter:
    """Пытается разобрать текст моделью, при любой проблеме — правилами."""

    def __init__(
        self,
        gateway: Optional[Any] = None,
        fallback: Optional[ScriptedFallback] = None,
    ) -> None:
        self.gateway = gateway
        self.fallback = fallback or ScriptedFallback()

    def parse(self, text: str) -> Dict[str, Any]:
        original = (text or "").strip()
        if not original:
            return self.fallback.parse("")
        if self.gateway is not None:
            try:
                if self.gateway.available():
                    data = self.gateway.json_chat(
                        [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": original[:_MAX_TEXT]},
                        ]
                    )
                    parsed = validate_intent(data, original)
                    if parsed is not None:
                        parsed["engine"] = "llm"
                        return parsed
            except Exception as exc:  # шлюз не должен ронять бота
                logger.warning("LLM-роутер: сбой (%s), работаю по правилам", type(exc).__name__)
        return self.fallback.parse(original)


def build_router(settings: Any = None) -> LLMRouter:
    """Собирает роутер из настроек приложения (ключ может отсутствовать)."""
    gateway = None
    if settings is not None:
        try:
            gateway = OpenAICompatibleGateway(
                base_url=getattr(settings, "llm_base_url", DEFAULT_BASE_URL),
                api_key=getattr(settings, "llm_api_key", ""),
                model=getattr(settings, "llm_model", DEFAULT_MODEL),
                timeout=getattr(settings, "llm_timeout", DEFAULT_TIMEOUT),
                enabled=getattr(settings, "llm_enabled", True),
            )
        except Exception:
            gateway = None
    return LLMRouter(gateway=gateway)


def parse_text(text: str, settings: Any = None) -> Dict[str, Any]:
    """Удобная обёртка: разобрать текст с настройками по умолчанию."""
    return build_router(settings).parse(text)


def validate_intent(data: Any, original: str) -> Optional[Dict[str, Any]]:
    """Проверяет ответ LLM; ``None`` — ответ непригоден, нужен fallback."""
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ALLOWED_KINDS:
        return None
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        text = original
    text = text.strip()[:_MAX_TEXT]

    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        title = text[:_MAX_TITLE]
    title = _shorten(title.strip(), _MAX_TITLE)

    category = data.get("category")
    if not isinstance(category, str) or not category.strip():
        category = "Заметка"
    category = category.strip()[:_MAX_CATEGORY]

    raw_metrics = data.get("metrics")
    metrics = _clean_metrics(raw_metrics if isinstance(raw_metrics, Mapping) else {})
    if kind in {"voice", "video", "doc"}:
        # Медиа-типы приходят только из медиа-пайплайна, текстом — нет.
        return None
    return {
        "kind": kind,
        "title": title or "Заметка",
        "text": text,
        "category": category,
        "metrics": metrics,
        "engine": "llm",
    }


def _clean_metrics(raw: Mapping[str, Any]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {}
    if not isinstance(raw, Mapping):
        return cleaned
    for key, value in raw.items():
        name = str(key).strip()
        if name == "exercise":
            exercise = str(value or "").strip()
            if exercise:
                cleaned[name] = exercise[:40]
            continue
        if name not in METRIC_SPECS:
            continue
        number = _number(value)
        if number is None:
            continue
        _, low, high = METRIC_SPECS[name]
        if not (low <= number <= high):
            continue
        cleaned[name] = number
        if len(cleaned) >= _MAX_METRICS:
            break
    return cleaned


def _content_from_response(raw: Any) -> Optional[str]:
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8", "replace")
        except Exception:
            return None
    else:
        return None
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return _content_from_payload(data)


def _content_from_payload(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        first = choices[0]
        message = first.get("message")
        if isinstance(message, dict):
            content = _content_to_text(message.get("content"))
            if content:
                return content
        content = _content_to_text(first.get("text"))
        if content:
            return content
    for key in ("content", "output_text", "text"):
        content = _content_to_text(data.get(key))
        if content:
            return content
    return None


def _content_to_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        parts = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        joined = "".join(parts).strip()
        return joined or None
    return None


def _retry_after(headers: Any) -> float:
    raw = None
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except Exception:
        raw = None
    try:
        wait = float(str(raw).strip()) if raw else 1.0
    except (TypeError, ValueError):
        wait = 1.0
    return min(max(wait, 0.2), 3.0)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return round(number, 2)


def _number_match(pattern: re.Pattern, text: str) -> Optional[float]:
    try:
        match = pattern.search(text)
    except (TypeError, RuntimeError):
        return None
    return _number(match.group(1)) if match is not None else None


def _water_ml(keyword_pattern: re.Pattern, unit_pattern: re.Pattern, text: str) -> Optional[float]:
    keyword = _first_match(keyword_pattern, text)
    if keyword is not None:
        value = _number(keyword.group(1))
        unit = (keyword.group(2) or "").lower() if keyword.lastindex and keyword.lastindex >= 2 else ""
        if value is not None:
            return value * 1000 if unit in ("л", "l") else value
    unit_match = _first_match(unit_pattern, text)
    if unit_match is not None:
        value = _number(unit_match.group(1))
        unit = (unit_match.group(2) or "").lower()
        if value is not None:
            return value * 1000 if unit in ("л", "l") else value
    return None


def _first_match(pattern: re.Pattern, text: str):
    try:
        return pattern.search(text)
    except (TypeError, RuntimeError):
        return None


def _clean_exercise(value: str) -> str:
    return _capitalize(str(value or "").strip().lower())[:40] or "Упражнение"


def _capitalize(value: str) -> str:
    text = str(value or "").strip()
    return text[:1].upper() + text[1:] if text else ""


def _shorten(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return ("%.2f" % float(value)).rstrip("0").rstrip(".").replace(".", ",")
