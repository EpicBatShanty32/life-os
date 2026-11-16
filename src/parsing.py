"""Разбор ответов человека в запись дневника (entry).

Публичный контракт — INTERFACES.md §6:
    CATEGORIES
    parse_answer(llm, slot, question, answer, now_iso) -> dict
    heuristic_metrics(text) -> dict

Логика:
1. Если LLM доступен — просим строгий JSON через prompts.parse_messages()
   и аккуратно нормализуем его (категория из списка, title ≤ 80, metrics
   только известные ключи с числами).
2. Если LLM выключен/упал/вернул мусор — эвристика без сети: категория по
   ключевым словам (иначе по слоту), title из первой строки, text как есть.
   По контракту metrics в этом случае пустые.

Главное правило: parse_answer никогда не бросает исключений.
Личные тексты в логи не пишем — только тип ошибки.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

try:  # обычный запуск: python3 -m src.main / from src import parsing
    from . import prompts as _prompts
except ImportError:  # запасной вариант: файл запущен как одиночный модуль
    import prompts as _prompts  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

#: Допустимые категории записи (INTERFACES.md §6, ТЗ §3.4).
CATEGORIES = set(_prompts.CATEGORY_LIST)

#: Ключи metrics, которые мы принимаем (единый источник — prompts.METRIC_KEYS).
KNOWN_METRIC_KEYS = frozenset(_prompts.METRIC_KEYS)

#: Максимальная длина заголовка.
TITLE_LIMIT = 80

#: Диапазоны числовых метрик: (минимум, максимум).
#: Значения вне диапазона отбрасываются, кроме шкал 1–5 — они прижимаются.
_NUMERIC_RANGES = {
    "sleep_h": (0.0, 24.0),
    "water_ml": (0.0, 20000.0),
    "calories": (0.0, 20000.0),
    "protein_g": (0.0, 1000.0),
    "weight_kg": (0.0, 500.0),
    "duration_min": (0.0, 1440.0),
    "distance_km": (0.0, 1000.0),
    "mood": (1.0, 5.0),
    "energy": (1.0, 5.0),
    "focus": (1.0, 5.0),
}
_SCALE_KEYS = ("mood", "energy", "focus")
_INT_KEYS = ("water_ml", "calories", "duration_min")
_LIST_KEYS = ("tasks_done", "tasks_planned")

#: Русские/разговорные синонимы категорий — на случай, если LLM ответит по-русски.
_CATEGORY_ALIASES = {
    "еда": "food",
    "питание": "food",
    "поел": "food",
    "тренировка": "training",
    "тренировки": "training",
    "спорт": "training",
    "сон": "sleep",
    "вода": "water",
    "питье": "water",
    "питьё": "water",
    "настроение": "mood",
    "самочувствие": "mood",
    "состояние": "mood",
    "задачи": "tasks",
    "дела": "tasks",
    "планы": "tasks",
    "цели": "goals",
    "дневник": "journal",
    "мысли": "journal",
    "журнал": "journal",
    "рефлексия": "journal",
    "прочее": "other",
    "другое": "other",
    "разное": "other",
}

#: Категория по умолчанию, если в тексте нет ключевых слов (по слоту).
_SLOT_CATEGORIES = {
    "morning": "tasks",
    "midday": "food",
    "evening": "journal",
    "weekly": "goals",
}

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Извлечение категории по словам (fallback без LLM)
# ---------------------------------------------------------------------------

_CATEGORY_PATTERNS = (
    ("food", re.compile(
        r"\bел(?:а|и)?\b|\bсъел\w*|\bпоел\w*|\bобед\w*|\bзавтрак\w*|\bужин\w*"
        r"|\bперекус\w*|\bкалори\w*|\bпорци\w*|\bеда\b|\bблюд\w*|\bкофе\b|\bчай\b"
        r"|\bсалат\w*|\bсуп\w*|\bрис\b|\bкуриц\w*|\bтворог\w*|\bбанан\w*|\bяблок\w*"
        r"|\bхлеб\w*|\bмакарон\w*|\bпицц\w*|\bбургер\w*|\bомлет\w*|\bкаш[аиуе]\b",
        re.IGNORECASE | re.UNICODE)),
    ("training", re.compile(
        r"трениров\w*|\bзал\w*|\bжим\w*|присед\w*|\bбег\w*|пробеж\w*|кардио"
        r"|\bйог\w*|растяж\w*|плава\w*|турник\w*|подтягива\w*|штанг\w*|гантел\w*"
        r"|кроссфит|\bспорт\w*|велосипед\w*|лыж\w*|отжима\w*",
        re.IGNORECASE | re.UNICODE)),
    ("sleep", re.compile(
        r"\bсон\b|\bсна\b|\bсне\b|\bспал\w*|\bсплю\b|\bспит\b|\bпоспал\w*"
        r"|выспал\w*|бессонниц\w*|недосып\w*",
        re.IGNORECASE | re.UNICODE)),
    ("water", re.compile(
        r"\bвод(?:а|ы|у|ой|е)\b|\bмл\b|миллилитр\w*|\bлитр\w*|\bвыпил\w*"
        r"|\bбутылк\w*|\bстакан\w*",
        re.IGNORECASE | re.UNICODE)),
    ("mood", re.compile(
        r"настроени\w*|\bустал\w*|энерги\w*|фокус\w*|тревог\w*|стресс\w*"
        r"|самочувств\w*|\bнастрой\b|апати\w*|радост\w*|груст\w*|разбит\w*"
        r"|\bзлюсь\b|\bспокойн\w*",
        re.IGNORECASE | re.UNICODE)),
    ("tasks", re.compile(
        r"\bплан\w*|\bзадач\w*|\bсделал\w*|сделано|перенёс\w*|перенес\w*"
        r"|дедлайн\w*|\bуспел\w*|закрыл\w*|\bсделаю\b",
        re.IGNORECASE | re.UNICODE)),
    ("goals", re.compile(
        r"\bцел(?:ь|и|ью|ей|ям|ях|ями|евая|евой)\b|прогресс\w*|\bрубеж\w*"
        r"|продвинул\w*",
        re.IGNORECASE | re.UNICODE)),
)


#: Порядок приоритета категорий для ничьих (как в задании: еда → тренировка →
#: сон → вода → настроение → задачи → цели).
_CATEGORY_ORDER = tuple(category for category, _ in _CATEGORY_PATTERNS)


def _guess_category(slot: str, text: str) -> str:
    """Категория по ключевым словам, иначе — по слоту. Никогда не бросает.

    Считаем число попаданий по категориям: фраза «три задачи: код, отчёт, зал»
    остаётся задачами, а не тренировкой, хотя «зал» и встретилось. При ничьей
    предпочитаем категорию слота, иначе — общий порядок приоритета.
    """
    slot_key = str(slot or "").strip().lower()
    slot_category = _SLOT_CATEGORIES.get(slot_key, "other")
    low = (text or "").lower().replace("ё", "е")
    if low.strip():
        scores = {}
        for category, pattern in _CATEGORY_PATTERNS:
            try:
                count = len(pattern.findall(low))
            except Exception:  # pragma: no cover — regex-паника
                count = 0
            if count:
                scores[category] = count
        if scores:
            best = max(scores.values())
            tied = [category for category in _CATEGORY_ORDER
                    if scores.get(category) == best]
            if slot_category in tied:
                return slot_category
            return tied[0]
    return slot_category


# ---------------------------------------------------------------------------
# Дата/время записи
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_ts(value) -> str:
    """ISO-строка времени; если пришёл мусор — текущее локальное время."""
    try:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str) and value.strip():
            text = value.strip()
            try:
                datetime.fromisoformat(text.replace("Z", "+00:00"))
                return text
            except (TypeError, ValueError):
                match = _DATE_RE.search(text)
                if match:
                    return match.group(1)
        return _now_iso()
    except Exception:  # pragma: no cover
        return _now_iso()


def _date_from_ts(ts: str) -> str:
    """Дата YYYY-MM-DD из ISO-времени."""
    try:
        match = _DATE_RE.match(str(ts))
        if match:
            return match.group(1)
        return datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return datetime.now().astimezone().date().isoformat()


# ---------------------------------------------------------------------------
# Нормализация title/text/category
# ---------------------------------------------------------------------------

def _collapse(text) -> str:
    """Схлопывает пробелы и переводы строк в один пробел."""
    try:
        return _WHITESPACE_RE.sub(" ", str(text or "")).strip()
    except Exception:
        return ""


def _first_line(text) -> str:
    """Первая непустая строка (без markdown-маркеров списка)."""
    try:
        for line in str(text or "").splitlines():
            clean = line.strip().lstrip("-—–*•#> \t")
            if clean:
                return clean
    except Exception:
        pass
    return ""


def _truncate(text: str, limit: int = TITLE_LIMIT) -> str:
    """Обрезка до limit символов по границе слова — красиво и предсказуемо."""
    clean = str(text or "").strip()
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    cut = cut.rstrip(" ,;:.!?—–-«»\"'")
    return cut or clean[:limit]


def _make_title(title, answer: str, category: str) -> str:
    """Заголовок: сначала из LLM, иначе первая строка ответа, иначе подпись категории."""
    clean = ""
    if isinstance(title, str):
        clean = _collapse(title)
    clean = clean.strip("«»\"'` ")
    if not clean:
        clean = _first_line(answer)
    if not clean:
        clean = _prompts.CATEGORY_LABELS.get(category, "запись")
    clean = _truncate(clean, TITLE_LIMIT)
    return clean or "запись"


def _make_text(text, answer: str) -> str:
    """Текст записи: из LLM (одна строка), иначе ответ как есть."""
    if isinstance(text, str) and text.strip():
        return _collapse(text)
    return answer if isinstance(answer, str) and answer.strip() else ""


def _normalize_category(value) -> "str | None":
    """Категория из ответа LLM; None, если не распознали."""
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace("ё", "е")
    if key in CATEGORIES:
        return key
    return _CATEGORY_ALIASES.get(key)


# ---------------------------------------------------------------------------
# Метрики: числа и списки
# ---------------------------------------------------------------------------

def _to_number(value) -> "float | None":
    """Число из int/float/строки («7,5», «7.5 ч», «80»). None, если числа нет."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            cleaned = value.strip().replace(",", ".").replace(" ", "")
            match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
            if not match:
                return None
            number = float(match.group())
        else:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return number


def _normalize_number(value, key: str):
    """Приводит число к допустимому виду для конкретного ключа metrics."""
    number = _to_number(value)
    if number is None:
        return None
    low, high = _NUMERIC_RANGES[key]
    if key in _SCALE_KEYS:
        # шкалы 1–5: прижимаем, чтобы не терять сигнал «высокое/низкое»
        if number < low:
            number = low
        if number > high:
            number = high
        return int(round(number))
    if number < low or number > high:
        return None
    if key in _INT_KEYS:
        return int(round(number))
    if float(number).is_integer():
        return int(number)
    return round(number, 2)


def _clean_scalar(value) -> str:
    """Строковое значение (training_type) без мусора."""
    try:
        if isinstance(value, bool) or value is None:
            return ""
        if isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, str):
            text = value
        else:
            return ""
        return _collapse(text)[:60]
    except Exception:
        return ""


def _clean_list(value) -> list:
    """Список задач: аккуратные непустые строки, не больше 20 штук."""
    try:
        if isinstance(value, str):
            parts = re.split(r"[;\n•]", value) if ";" in value or "\n" in value else [value]
        elif isinstance(value, (list, tuple, set)):
            parts = list(value)
        else:
            return []
        items = []
        for part in parts:
            if isinstance(part, dict):
                part = part.get("title") or part.get("text") or ""
            text = _collapse(part)
            text = re.sub(r"^\s*(?:\d+[.)]|[-—–*•])\s*", "", text).strip()
            if text:
                items.append(text[:160])
            if len(items) >= 20:
                break
        return items
    except Exception:
        return []


def _sanitize_metrics(raw) -> dict:
    """Только известные ключи с валидными значениями; неизвестное отбрасываем."""
    metrics = {}
    if not isinstance(raw, dict):
        return metrics
    for key in _prompts.METRIC_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key in _NUMERIC_RANGES:
            number = _normalize_number(value, key)
            if number is not None:
                metrics[key] = number
        elif key == "training_type":
            text = _clean_scalar(value)
            if text:
                metrics[key] = text
        elif key in _LIST_KEYS:
            items = _clean_list(value)
            if items:
                metrics[key] = items
    return metrics


# ---------------------------------------------------------------------------
# Эвристические метрики по тексту (без LLM)
# ---------------------------------------------------------------------------

_NUM = r"(\d+(?:[.,]\d+)?)"

_RE_ML = re.compile(_NUM + r"\s*(?:мл|ml|миллилитр\w*)(?!\w)", re.IGNORECASE)
_RE_LITER = re.compile(_NUM + r"\s*(?:литр\w*|liter\w*|л)(?!\w)", re.IGNORECASE)
_RE_HOURS = re.compile(_NUM + r"\s*(?:час(?:а|ов)?|ч)(?!\w)", re.IGNORECASE)
_RE_MINUTES = re.compile(_NUM + r"\s*(?:мин(?:ут\w*)?|мин\.)(?!\w)", re.IGNORECASE)
_RE_KCAL = re.compile(_NUM + r"\s*(?:ккал|kcal|калори\w*)(?!\w)", re.IGNORECASE)
_RE_PROTEIN = re.compile(
    _NUM + r"\s*(?:г|гр|грамм\w*)\.?\s*(?:белк\w*|протеин\w*)", re.IGNORECASE)
_RE_PROTEIN_REV = re.compile(
    r"(?:белк\w*|протеин\w*)\D{0,12}?" + _NUM + r"\s*(?:г|гр|грамм\w*)(?!\w)",
    re.IGNORECASE)
_RE_KG = re.compile(_NUM + r"\s*(?:кг|kg|килограмм\w*)(?!\w)", re.IGNORECASE)
_RE_KM = re.compile(_NUM + r"\s*(?:км|km|километр\w*)(?!\w)", re.IGNORECASE)

_RE_SLEEP_WORDS = re.compile(
    r"спал|сплю|сон|сна|сне|поспал|выспал|ночью|ночь|недосып|бессонниц",
    re.IGNORECASE)
_RE_TRAINING_WORDS = re.compile(
    r"трениров|зал|жим|присед|бег|бегал|пробеж|кардио|йог|растяж|плава|"
    r"турник|подтягива|штанг|гантел|кроссфит|спорт|велосипед|лыж|отжима",
    re.IGNORECASE)
_RE_BAR_WEIGHT = re.compile(
    r"присед|жим|штан|гантел|тяг[аи]|становая|снаряд|блин",
    re.IGNORECASE)

_RE_MOOD = re.compile(r"настроени\w*\D{0,15}?" + _NUM, re.IGNORECASE)
_RE_ENERGY = re.compile(r"энерги\w*\D{0,15}?" + _NUM, re.IGNORECASE)
_RE_FOCUS = re.compile(r"фокус\w*\D{0,15}?" + _NUM, re.IGNORECASE)


def _round1(value) -> "int | float":
    """Округление до одного знака; целые — как int."""
    try:
        rounded = round(float(value), 1)
        return int(rounded) if float(rounded).is_integer() else rounded
    except (TypeError, ValueError):
        return 0


def _nearest_keyword_distance(text: str, position: int, pattern):
    """Расстояние от позиции числа до ближайшего слова по pattern (None — нет слов)."""
    best = None
    try:
        for match in pattern.finditer(text):
            if match.start() <= position <= match.end():
                distance = 0
            elif position > match.end():
                distance = position - match.end()
            else:
                distance = match.start() - position
            if distance >= 0 and (best is None or distance < best):
                best = distance
    except Exception:
        return None
    return best


def _hours_to_metric(low: str, start: int, end: int, value: float):
    """Часы → sleep_h или duration_min: смотрим, какое слово ближе к числу.

    «Спал 8 часов, тренировка 1 час» — 8 часов про сон, 1 час про тренировку.
    """
    window_start = max(0, start - 40)
    window_end = min(len(low), end + 40)
    window = low[window_start:window_end]
    relative = start - window_start
    sleep_distance = _nearest_keyword_distance(window, relative, _RE_SLEEP_WORDS)
    training_distance = _nearest_keyword_distance(window, relative, _RE_TRAINING_WORDS)
    if training_distance is not None and (sleep_distance is None
                                          or training_distance < sleep_distance):
        return "duration_min", int(round(value * 60))
    return "sleep_h", _round1(value)


def _first_number(pattern, low: str, skip_context=None):
    """Первое подходящее число по regex; при skip_context — пропускаем фразы про штангу."""
    try:
        for match in pattern.finditer(low):
            number = _to_number(match.group(1))
            if number is None or number <= 0:
                continue
            if skip_context is not None:
                window = low[max(0, match.start() - 25): match.end() + 25]
                if skip_context.search(window):
                    continue
            return number
    except Exception:
        return None
    return None


def heuristic_metrics(text: str) -> dict:
    """Достаёт из текста числа с единицами: мл/л, ч, мин, ккал, кг, км, г белка.

    Возвращает только известные ключи metrics. Никогда не бросает.
    """
    try:
        if text is None:
            return {}
        if not isinstance(text, str):
            text = str(text)
        if not text.strip():
            return {}
        low = text[:20000].lower().replace("ё", "е")
        metrics = {}

        # --- вода: суммы мл и литров -------------------------------------
        water_ml = 0.0
        has_water = False
        for raw in _RE_ML.findall(low):
            number = _to_number(raw)
            if number is not None and 0 < number:
                water_ml += number
                has_water = True
        for raw in _RE_LITER.findall(low):
            number = _to_number(raw)
            if number is not None and 0 < number:
                water_ml += number * 1000
                has_water = True
        if has_water and water_ml > 0:
            metrics["water_ml"] = int(round(water_ml))

        # --- часы: сон или длительность тренировки ----------------------
        sleep_h = None
        duration_from_hours = None
        for match in _RE_HOURS.finditer(low):
            number = _to_number(match.group(1))
            if number is None or number <= 0:
                continue
            key, value = _hours_to_metric(low, match.start(), match.end(), number)
            if key == "sleep_h" and sleep_h is None:
                sleep_h = value
            elif key == "duration_min" and duration_from_hours is None:
                duration_from_hours = int(value)
        minutes = []
        for raw in _RE_MINUTES.findall(low):
            number = _to_number(raw)
            if number is not None and number > 0:
                minutes.append(number)
        duration_min = int(round(minutes[0])) if minutes else duration_from_hours
        if sleep_h is not None:
            metrics["sleep_h"] = sleep_h
        if duration_min is not None and duration_min > 0:
            metrics["duration_min"] = duration_min

        # --- калории / белок / вес / км ---------------------------------
        calories = _first_number(_RE_KCAL, low)
        if calories is not None:
            metrics["calories"] = int(round(calories))

        protein = _first_number(_RE_PROTEIN, low)
        if protein is None:
            protein = _first_number(_RE_PROTEIN_REV, low)
        if protein is not None:
            metrics["protein_g"] = _round1(protein)

        weight = _first_number(_RE_KG, low, skip_context=_RE_BAR_WEIGHT)
        if weight is not None:
            metrics["weight_kg"] = _round1(weight)

        distance = _first_number(_RE_KM, low)
        if distance is not None:
            metrics["distance_km"] = _round1(distance)

        # --- самооценки 1–5 ---------------------------------------------
        for key, pattern in (("mood", _RE_MOOD), ("energy", _RE_ENERGY),
                             ("focus", _RE_FOCUS)):
            try:
                match = pattern.search(low)
                if not match:
                    continue
                value = _to_number(match.group(1))
                if value is not None and 1 <= value <= 5:
                    metrics[key] = int(round(value))
            except Exception:
                continue

        return metrics
    except Exception as exc:  # контракт: не бросать
        logger.debug("heuristic_metrics: ошибка (%s)", type(exc).__name__)
        return {}


# ---------------------------------------------------------------------------
# Сборка записи
# ---------------------------------------------------------------------------

def _build_entry(slot: str, category: str, title: str, text: str,
                 raw: str, metrics: dict, date: str, ts: str) -> dict:
    """Entry-словарь по INTERFACES.md §12 — единая точка сборки."""
    return {
        "date": date,
        "ts": ts,
        "slot": slot,
        "category": category,
        "title": _truncate(str(title or ""), TITLE_LIMIT) or "запись",
        "text": text,
        "raw": raw,
        "metrics": metrics if isinstance(metrics, dict) else {},
    }


def _entry_from_llm(parsed: dict, slot: str, answer: str,
                    date: str, ts: str) -> dict:
    """Нормализация JSON от LLM в entry."""
    category = _normalize_category(parsed.get("category"))
    if category is None:
        category = _guess_category(slot, answer)
    title = _make_title(parsed.get("title"), answer, category)
    text = _make_text(parsed.get("text"), answer)
    metrics = _sanitize_metrics(parsed.get("metrics"))
    return _build_entry(slot, category, title, text, answer, metrics, date, ts)


def _entry_fallback(slot: str, answer: str, date: str, ts: str) -> dict:
    """Запись без LLM: категория по словам/слоту, title из первой строки, metrics {}."""
    category = _guess_category(slot, answer)
    title = _make_title(None, answer, category)
    # «text = ответ как есть»: не переформатируем, только убираем пустой ответ
    text = answer if isinstance(answer, str) and answer.strip() else ""
    return _build_entry(slot, category, title, text, answer, {}, date, ts)


def _minimal_entry(slot, answer, now_iso) -> dict:
    """Последняя линия обороны: запись, которая не может не собраться."""
    try:
        equivalent = answer if isinstance(answer, str) else (
            "" if answer is None else str(answer))
        ts = _normalize_ts(now_iso)
        date = _date_from_ts(ts)
        return _entry_fallback(str(slot or ""), equivalent, date, ts)
    except Exception:
        now = datetime.now().astimezone()
        return {
            "date": now.date().isoformat(),
            "ts": now.isoformat(timespec="seconds"),
            "slot": "" if slot is None else str(slot),
            "category": "other",
            "title": "запись",
            "text": "",
            "raw": "",
            "metrics": {},
        }


def _llm_ready(llm) -> bool:
    """Можно ли звать LLM: есть объект, available() и json_chat()."""
    if llm is None:
        return False
    try:
        checker = getattr(llm, "available", None)
        if callable(checker):
            if not checker():
                return False
    except Exception:
        return False
    return callable(getattr(llm, "json_chat", None))


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def parse_answer(llm, slot: str, question: str, answer: str,
                 now_iso: str) -> dict:
    """Ответ человека → entry. LLM опционален; никогда не бросает."""
    try:
        slot_key = str(slot or "").strip().lower()
        raw_answer = answer if isinstance(answer, str) else (
            "" if answer is None else str(answer))
        question_text = question if isinstance(question, str) else str(question or "")
        ts = _normalize_ts(now_iso)
        date = _date_from_ts(ts)

        # 1) пробуем LLM (если он есть, включён и есть что разбирать)
        if _llm_ready(llm) and raw_answer.strip():
            try:
                messages = _prompts.parse_messages(slot_key, question_text, raw_answer)
                parsed = llm.json_chat(messages)
            except Exception as exc:
                logger.debug("parse_answer: LLM недоступен (%s), берём эвристику",
                             type(exc).__name__)
                parsed = None
            if isinstance(parsed, dict):
                try:
                    return _entry_from_llm(parsed, slot_key, raw_answer, date, ts)
                except Exception as exc:
                    logger.warning("parse_answer: не удалось нормализовать JSON LLM (%s)",
                                   type(exc).__name__)

        # 2) fallback без LLM — всегда работает
        return _entry_fallback(slot_key, raw_answer, date, ts)
    except Exception as exc:  # последняя линия обороны контракта
        logger.warning("parse_answer: непредвиденная ошибка (%s)", type(exc).__name__)
        return _minimal_entry(slot, answer, now_iso)


__all__ = [
    "CATEGORIES",
    "KNOWN_METRIC_KEYS",
    "parse_answer",
    "heuristic_metrics",
]
