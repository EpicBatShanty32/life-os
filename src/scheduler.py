"""Планировщик Life OS — сердце бота.

Отвечает за:
* слоты опроса (morning / midday / evening / weekly) по Europe/Moscow;
* диалоговые сессии: вопрос → ответ → уточнение (follow-up через LLM) →
  следующий вопрос; состояние сессии лежит в kv-ключе ``session`` (INTERFACES §8);
* команды Telegram и свободный чат;
* сохранение каждого ответа в SQLite (``storage.add_entry``), запись в Obsidian
  (``vault.write_entry``) и пересборку дашборда (``dash_build``);
* информативные русские тексты: status / today / report / goals.

Модуль не бросает исключения в цикл main: все ошибки LLM/vault/dashboard
ловятся и логируются, чат продолжает работать.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import goals_math, parsing, prompts, telegram

__all__ = ["Scheduler"]

_SLOT_ORDER = ("morning", "midday", "evening", "weekly")

#: Максимальная длина ответа LLM перед отправкой, в символах (F2 адверсариального
#: теста). Меньше лимита одного сообщения Bot API (4000 UTF-16 units), поэтому
#: даже «простыня» от модели уходит одним сообщением, а не 25 чанками.
MAX_REPLY_CHARS = 3500
#: Бюджет истории диалога для LLM, в символах (F2). Старые сообщения
#: отбрасываются целиком; текущее сообщение посередине не режется.
MAX_HISTORY_CHARS = 12000
#: Лимит размера значения сессии в kv, в байтах (F9): слишком большая сессия
#: сбрасывается с предупреждением, а не читается на каждом сообщении/тике.
MAX_SESSION_BYTES = 32 * 1024

# Подписи на случай, если prompts.SLOT_LABELS недоступны/изменены.
_FALLBACK_SLOT_LABELS = {
    "morning": "утро",
    "midday": "день",
    "evening": "вечер",
    "weekly": "неделя",
}
_SLOT_TITLES = {
    "morning": "Утро",
    "midday": "День",
    "evening": "Вечер",
    "weekly": "Неделя",
    "free": "Свободный чат",
}
_CATEGORY_LABELS = {
    "tasks": "задачи",
    "food": "еда",
    "training": "тренировка",
    "goals": "цели",
    "mood": "настроение",
    "journal": "мысли",
    "sleep": "сон",
    "water": "вода",
    "other": "прочее",
}
_STATUS_LABELS = {
    "active": "активна",
    "done": "достигнута",
    "paused": "на паузе",
    "dropped": "снята",
}
_WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# Пропуск текущего вопроса / прерывание сессии.
_SKIP_TOKENS = {"-", "—", "–", "пропустить", "пропусти", "скип", "skip"}
_STOP_TOKENS = {"стоп", "stop", "хватит", "прекрати", "отмена"}

# Если prompts.scripted_questions сломается, бот всё равно должен спрашивать.
# Порядок тем совпадает с _SLOT_TOPICS: иначе уточнения и пропуски «поедут».
_FALLBACK_QUESTIONS = {
    "morning": [
        "Что главное должно случиться сегодня?",
        "Какие 2–3 задачи к этому ведут?",
        "Что с целями — какая сейчас важнее всего?",
    ],
    "midday": [
        "Что уже ел?",
        "Сколько воды выпил?",
        "Как энергия и фокус — от 1 до 5?",
    ],
    "evening": [
        "Как прошла тренировка?",
        "Что сделал по задачам, что перенёс?",
        "Что было по еде за день?",
        "Как настроение и энергия — от 1 до 5?",
        "Что важного произошло, о чём стоит помнить?",
    ],
    "weekly": [
        "Что получилось на неделе, а что не вышло?",
        "Как неделя по телу: сон, еда, тренировки?",
        "Что с целями — что сдвинулось, где застрял?",
        "Что главное на следующую неделю?",
    ],
}
_FALLBACK_GREETINGS = {
    "morning": "Доброе утро! Давай сверимся с планами на день.",
    "midday": "Привет! Как дела? Давай коротко про еду, воду и состояние.",
    "evening": "Добрый вечер! Как прошёл день? Давай сохраним итоги.",
    "weekly": "Привет! Давай подведём итоги недели и заглянем в следующую.",
}
_CLOSING_TEXTS = {
    "morning": "Спасибо! План сохранил, дашборд обновил. Хорошего дня — писать можно в любой момент.",
    "midday": "Спасибо, всё записал. Если что-то изменится — просто напиши.",
    "evening": "Спасибо за день и за искренность. Всё сохранил, дашборд обновил. Отдыхай.",
    "weekly": "Спасибо! Итоги недели сохранил. Дальше — по плану новой недели.",
}

# Темы вопросов по слотам: нужны, чтобы (а) не задавать вопрос, на который
# человек уже ответил в том же сообщении (UX_REVIEW P1-4), и (б) подобрать
# осмысленное уточнение без LLM (UX_REVIEW P1-5).
_SLOT_TOPICS = {
    "morning": ("plan", "tasks", "goals"),
    "midday": ("food", "water", "wellbeing"),
    "evening": ("training", "tasks", "food", "wellbeing", "journal"),
    "weekly": ("week_review", "body", "goals", "next_week"),
}

#: Ключевые слова, по которым видно, что тема уже раскрыта. Специально узкие:
#: общие слова вроде «план» не считаем ответом на вопрос про задачи.
_TOPIC_PATTERNS = {
    "tasks": re.compile(
        r"\bзадач\w*|\bвыполн\w*|\bперенёс\w*|\bперенес\w*|\bуспел\w*"
        r"|дедлайн\w*|\bзакрыл[аи]?\s+(?:задач|план|этап)",
        re.IGNORECASE | re.UNICODE),
    "goals": re.compile(
        r"\bцел(?:ь|и|ью|ей|ям|ях|ями|евая|евой)\b|прогресс\w*|\bрубеж\w*"
        r"|продвинул\w*",
        re.IGNORECASE | re.UNICODE),
    "food": re.compile(
        r"\bел\w*|\bсъел\w*|\bпоел\w*|\bеда\b|\bзавтрак\w*|\bобед\w*|\bужин\w*"
        r"|\bперекус\w*|\bкалори\w*|\bпорци\w*|\bблюд\w*|\bкофе\b|\bчай\b"
        r"|\bсалат\w*|\bсуп\w*|\bрис\b|\bкуриц\w*|\bтворог\w*|\bбанан\w*"
        r"|\bяблок\w*|\bхлеб\w*|\bмакарон\w*|\bпицц\w*|\bбургер\w*|\bомлет\w*",
        re.IGNORECASE | re.UNICODE),
    "water": re.compile(
        r"\bвод(?:а|ы|у|ой|е)\b|\bмл\b|миллилитр\w*|\bлитр\w*|\bвыпил\w*"
        r"|\bбутылк\w*|\bстакан\w*",
        re.IGNORECASE | re.UNICODE),
    "training": re.compile(
        r"трениров\w*|\bзал\b|\bжим\w*|присед\w*|\bбег\w*|пробеж\w*|кардио\w*"
        r"|\bйог\w*|растяж\w*|плава\w*|турник\w*|подтягива\w*|штанг\w*|гантел\w*"
        r"|кроссфит\w*|\bспорт\w*|велосипед\w*|\bлыж\w*|отжима\w*",
        re.IGNORECASE | re.UNICODE),
    "wellbeing": re.compile(
        r"настроени\w*|энерги\w*|фокус\w*|самочувств\w*|тревог\w*|стресс\w*"
        r"|\bустал\w*|апати\w*|радост\w*|груст\w*|разбит\w*",
        re.IGNORECASE | re.UNICODE),
    "journal": re.compile(
        r"\bдумал\w*|мысл\w*|тревож\w*|пережива\w*|вспомн\w*|\bчувств\w*"
        r"|хорошего\b|важного\b",
        re.IGNORECASE | re.UNICODE),
}

#: Винительный падеж для человеческой фразы «про … ты уже рассказал».
_TOPIC_LABELS = {
    "tasks": "задачи",
    "goals": "цели",
    "food": "еду",
    "water": "воду",
    "training": "тренировку",
    "wellbeing": "самочувствие",
    "journal": "мысли",
}

#: Уточнения без LLM — по теме текущего вопроса, но сформулированы иначе,
#: чтобы не повторять исходный вопрос слово в слово (UX_REVIEW P1-5).
_TOPIC_FOLLOWUPS = {
    "tasks": "Назови хотя бы одну конкретную задачу — с неё и начнём.",
    "goals": "Про какую цель речь — и что по ней сейчас?",
    "food": "Хотя бы одно блюдо — что было на тарелке?",
    "water": "Хотя бы примерно: пол-литра, литр?",
    "training": "Сколько примерно заняло и что делал?",
    "wellbeing": "Хотя бы примерно — ближе к 2 или к 4?",
    "journal": "Одно слово или одна мысль — что больше всего задело?",
    "plan": "Одно конкретное дело, которое сегодня важнее остальных?",
    "week_review": "Хотя бы одно, что получилось, — и одно, что нет?",
    "body": "Хотя бы одно: как спал или что ел на неделе?",
    "next_week": "Одна главная вещь на следующую неделю — какая?",
}
_FOLLOWUP_FALLBACKS = {
    "morning": "Хотя бы одно конкретное дело на сегодня?",
    "midday": "Хотя бы один пример — что именно?",
    "evening": "Одна мысль или одно событие, которое запомнилось?",
    "weekly": "Хотя бы одно, что получилось за неделю?",
}

#: Ответы-заглушки: смысла в них мало, поэтому уточняем (UX_REVIEW P1-5).
#: Короткие подтверждения в свободном чате («ок», «понял»): это не запись
#: дневника. Без этого «ок» оседало в /today как заметка (UX_REVIEW, P2).
_ACK_TOKENS = {
    "ок", "окей", "ok", "okay", "хорошо", "норм", "нормально",
    "ага", "угу", "да", "нет", "супер", "отлично", "понял", "поняла",
    "понятно", "ясно", "ладно", "принято", "👍",
}
_ACK_SPLIT_RE = re.compile(r"[^\wА-Яа-яЁё]+", re.UNICODE)


def _is_ack_only(text: str) -> bool:
    """True, если текст состоит только из подтверждений: «ок», «ладно, понял».

    «ладно, понял» — два подтверждения подряд, тоже не запись дневника.
    Эмодзи («👍») отбрасываются разделителем, поэтому одиночный токен
    проверяем ещё и по исходной строке.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.strip("!.,… ") in _ACK_TOKENS:
        return True
    words = [word for word in _ACK_SPLIT_RE.split(stripped) if word]
    return bool(words) and all(word in _ACK_TOKENS for word in words)
_VAGUE_ANSWERS = {
    "норм", "нормально", "норма", "ок", "окей", "ok", "okay", "хорошо", "плохо",
    "хз", "не знаю", "не помню", "неважно", "пока не знаю", "всё нормально",
    "все нормально", "всё хорошо", "все хорошо", "как обычно", "обычно",
    "ничего", "нечего", "нет", "да", "ага", "угу", "да нормально",
    "более-менее", "более менее", "так себе", "средне", "пойдёт", "пойдет",
    "неплохо", "супер", "отлично",
}
_VAGUE_MARKERS = (
    "без подробност", "не хочу рассказ", "не буду", "неважно", "не помню",
    "не знаю", "как обычно", "ничего особенного", "ничего нового",
    "нечего сказать", "пропущу",
)
#: Слова, которые в коротком ответе ещё не раскрывают тему.
_VAGUE_WORDS = {"норм", "нормально", "хорошо", "плохо", "ок", "окей", "обычно",
                "устал", "устала", "средне", "неплохо", "супер", "отлично"}
_WORD_RE = re.compile(r"[\wА-Яа-яЁё]+", re.UNICODE)
_DIGIT_RE = re.compile(r"\d", re.UNICODE)

#: Признаки содержательного рассказа о жизни — по ним свободный текст
#: сохраняется в дневник, а не превращается в болтовню с записью (UX_REVIEW P1-6).
_LIFE_SIGNAL_RE = re.compile(
    r"\bел\w*|\bсъел\w*|\bпоел\w*|\bеда\b|\bзавтрак\w*|\bобед\w*|\bужин\w*"
    r"|\bперекус\w*|\bкалори\w*|\bпорци\w*|\bблюд\w*|\bкофе\b|\bчай\b"
    r"|\bсалат\w*|\bсуп\w*|\bрис\b|\bкуриц\w*|\bтворог\w*|\bбанан\w*"
    r"|\bяблок\w*|\bхлеб\w*|\bмакарон\w*|\bпицц\w*|\bбургер\w*|\bомлет\w*"
    r"|трениров\w*|\bзал\b|\bжим\w*|присед\w*|\bбег\w*|пробеж\w*|кардио\w*"
    r"|\bйог\w*|растяж\w*|плава\w*|турник\w*|подтягива\w*|штанг\w*|гантел\w*"
    r"|кроссфит\w*|\bспорт\w*|велосипед\w*|\bлыж\w*|отжима\w*"
    r"|\bсон\b|\bсна\b|\bспал\w*|\bсплю\b|поспал\w*|выспал\w*|бессонниц\w*"
    r"|недосып\w*|\bвод[аыуей]\b|\bмл\b|миллилитр\w*|\bлитр\w*|\bвыпил\w*"
    r"|\bбутылк\w*|\bстакан\w*|настроени\w*|энерги\w*|фокус\w*|тревог\w*"
    r"|стресс\w*|самочувств\w*|\bустал\w*|апати\w*|радост\w*|груст\w*"
    r"|\bплан\w*|\bзадач\w*|сделал\w*|сделано\b|перенёс\w*|перенес\w*"
    r"|дедлайн\w*|\bуспел\w*|\bзакрыл\w*|\bцел(?:ь|и|ью|ей|ям|ях|ями)\b"
    r"|прогресс\w*|рубеж\w*|продвинул\w*|\bкг\b|\bкм\b|шаг(?:и|ов)?\b"
    r"|дневник\w*|мысл\w*",
    re.IGNORECASE | re.UNICODE,
)

#: Приветствия и болтовня: на них отвечаем тепло и НЕ пишем запись.
_GREETING_RE = re.compile(
    r"^(?:привет\w*|приветик|здравствуй\w*|здарова\w*|хай|хи+|hello|hi|hey|ку"
    r"|добр(?:ое|ый|ого)\s+(?:утро|день|вечер|ночи|время)|доброе\s+утро)\b",
    re.IGNORECASE | re.UNICODE,
)
_THANKS_RE = re.compile(
    r"^(?:спасибо\w*|благодарю\w*|спс|thanks|thank\s+you|мерси)\b",
    re.IGNORECASE | re.UNICODE,
)
_CHITCHAT_RE = re.compile(
    r"как\s+(?:ты|дела|сам|сама|жизнь|оно|настроение)|что\s+нового"
    r"|чем\s+занят",
    re.IGNORECASE | re.UNICODE,
)
_CAPABILITY_RE = re.compile(
    r"ты\s+кто|кто\s+ты|что\s+ты\s+(?:умеешь|можешь|делаешь)"
    r"|как\s+тебя\s+зовут|что\s+это\s+за\s+бот",
    re.IGNORECASE | re.UNICODE,
)
#: Просьба поговорить/объяснить — без LLM честно говорим, что не можем.
_REQUEST_RE = re.compile(
    r"\bрасскажи\w*|\bобъясни\w*|\bпосоветуй\w*|\bподскажи\w*|\bпоговори\w*"
    r"|\bпоболтай\w*|\bпошути\w*",
    re.IGNORECASE | re.UNICODE,
)
#: Явный вопрос: начинается с вопросительного слова и кончается «?».
#: Такой текст не сохраняем, даже если в нём мелькнуло «ел» или «вода».
_EXPLICIT_QUESTION_RE = re.compile(
    r"^(?:а\s+)?(?:что|чего|как|какой|какая|какие|когда|почему|зачем|где|куда"
    r"|кто|сколько|можно|стоит|есть\s+ли)\b",
    re.IGNORECASE | re.UNICODE,
)

# Алиасы команд: латиница со слэшем и русские слова без слэша.
_COMMAND_ALIASES = {
    "start": "start",
    "старт": "start",
    "pause": "pause",
    "пауза": "pause",
    "resume": "resume",
    "продолжить": "resume",
    "возобновить": "resume",
    "status": "status",
    "статус": "status",
    "today": "today",
    "сегодня": "today",
    "report": "report",
    "отчёт": "report",
    "отчет": "report",
    "goals": "goals",
    "цели": "goals",
    "add_goal": "add_goal",
    "добавить_цель": "add_goal",
    "новая_цель": "add_goal",
    "skip": "skip",
    "пропустить": "skip",
    "пропусти": "skip",
    "help": "help",
    "помощь": "help",
    "дизайн": "design",
    "design": "design",
    "дашборд": "design",
    "dashboard": "design",
    "stop": "stop",
    "стоп": "stop",
    "habit": "habit",
    "привычка": "habit",
    "habits": "habits",
    "привычки": "habits",
}

_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _to_float(value) -> float | None:
    """Достаёт число из метрики: 7, "7.5", "7,5 ч", "2000 мл", "2 000" → float."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value)
    # Любые Unicode-пробелы → обычный: иначе «2 000» (U+2009) даёт 2.0.
    text = re.sub(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]", " ", text)
    text = re.sub(r"(?<=\d) (?=\d{3}(?:\D|$))", "", text)
    match = _NUM_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _fmt_num(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return ("%.1f" % value).rstrip("0").rstrip(".")


def _collapse(text) -> str:
    """Все пробелы и переводы строк — в один пробел."""
    return re.sub(r"\s+", " ", _text(text)).strip()


def _clip_text(text, limit: int = 120) -> str:
    """Обрезка текста записи по границе слова с многоточием."""
    clean = _collapse(text)
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    space = cut.rfind(" ")
    if space >= limit // 3:
        cut = cut[:space]
    cut = cut.rstrip(" ,;:.!?—–-«»\"'")
    return (cut or clean[:limit].rstrip()) + "…"


def _clip_reply(text, limit: int = MAX_REPLY_CHARS) -> str:
    """Обрезает ответ LLM до ``limit`` символов по границе слова (F2).

    Режет хвост, а не середину: сначала ищет последний пробел (или перевод
    строки) в пределах лимита, а если его нет — режет жёстко. Многоточие
    добавляется только когда текст реально обрезан.
    """
    clean = _text(text).strip()
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    space = max(cut.rfind("\n"), cut.rfind(" "))
    if space >= limit // 2:
        cut = cut[:space]
    cut = cut.rstrip(" \t\r\n,;:.!?-–—«»\"'")
    return (cut or clean[:limit].rstrip()) + "…"


def _same_start(full: str, title: str) -> bool:
    """Заголовок уже виден в начале текста — не дублируем его в /today."""
    def norm(value: str) -> str:
        return value.lower().replace("ё", "е").strip(" «»\"'`")

    body = norm(_collapse(full))
    head = norm(_collapse(title))
    if not body or not head:
        return False
    return body.startswith(head) or head.startswith(body)


def _minutes_to_hm(minutes: int) -> str:
    """Минуты от полуночи → ``ЧЧ:ММ`` (для тихих часов в /status)."""
    try:
        value = int(minutes) % (24 * 60)
    except (TypeError, ValueError):
        return "--:--"
    return "%02d:%02d" % (value // 60, value % 60)


def _plural(number: int, forms: tuple) -> str:
    """Русское склонение: 1 запись / 2 записи / 5 записей."""
    try:
        count = abs(int(number)) % 100
    except (TypeError, ValueError):
        return forms[2]
    if 11 <= count <= 14:
        return forms[2]
    count = count % 10
    if count == 1:
        return forms[0]
    if 2 <= count <= 4:
        return forms[1]
    return forms[2]


class Scheduler:
    """Планировщик слотов, диалогов и команд.

    Args:
        cfg: весь конфиг (см. config.load_config).
        storage: Storage — база.
        tg: Telegram-клиент (нужен метод ``send(chat_id, text)``). В dry-run
            подменяется заглушкой, которая пишет в лог.
        llm: LLM-клиент (``available()``, ``chat(messages)``).
        vault: Vault или None (``write_entry``, ``sync_pending``, ``rewrite_goals``).
        dash_build: callable(cfg, storage) -> str | None или None.
        logger: logging.Logger или None.
    """

    SESSION_KEY = "session"

    def __init__(self, cfg: dict, storage, tg, llm, vault, dash_build, logger) -> None:
        self.cfg = cfg or {}
        self.storage = storage
        self.tg = tg
        self.llm = llm
        self.vault = vault
        self.dash_build = dash_build
        self.log = logger or logging.getLogger("lifeos.scheduler")

        tz_name = _text((self.cfg.get("app") or {}).get("timezone")) or "Europe/Moscow"
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 — кривую таймзону уже отловил config
            self.tz = ZoneInfo("Europe/Moscow")

        telegram_cfg = self.cfg.get("telegram") or {}
        try:
            self.chat_id = int(telegram_cfg.get("allowed_chat_id") or 0)
        except (TypeError, ValueError):
            self.chat_id = 0
        llm_cfg = self.cfg.get("llm") or {}
        try:
            self.max_followups = int(llm_cfg.get("max_followups", 2) or 0)
        except (TypeError, ValueError):
            self.max_followups = 2
        self.schedule = self.cfg.get("schedule") or {}
        self.app_cfg = self.cfg.get("app") or {}
        # Итог последней сборки дашборда: нужен ролловеру, чтобы не отмечать
        # день успешным при упавшем dash_build (REVIEW P2.5).
        self._dashboard_ok = True

    # ================================================================ ВРЕМЯ
    def _local(self, now: datetime | None = None) -> datetime:
        """Приводит время к таймзоне приложения (naive считаем локальным)."""
        if now is None:
            return datetime.now(self.tz)
        if now.tzinfo is None:
            return now.replace(tzinfo=self.tz)
        return now.astimezone(self.tz)

    def _slot_label(self, slot: str) -> str:
        labels = getattr(prompts, "SLOT_LABELS", None)
        if isinstance(labels, dict) and labels.get(slot):
            return _text(labels[slot])
        return _FALLBACK_SLOT_LABELS.get(slot, slot)

    def _category_label(self, category: str) -> str:
        return _CATEGORY_LABELS.get(category or "other", category or "прочее")

    def _today_human(self, day) -> str:
        return "%s (%s)" % (day.strftime("%d.%m.%Y"), _WEEKDAY_SHORT[day.weekday()])

    def _short_date(self, day) -> str:
        return day.strftime("%d.%m")

    def _tz_label(self) -> str:
        key = _text(getattr(self.tz, "key", "")) or _text(self.app_cfg.get("timezone"))
        return "МСК" if key == "Europe/Moscow" else key

    # ============================================================== ОТПРАВКА
    def _send(self, chat_id: int, text: str, slot: str | None = None) -> None:
        """Отправляет сообщение и пишет историю. Ошибки Telegram не роняют бота."""
        if text is None:
            return
        text = _text(text)
        if not text.strip():
            return
        try:
            self.tg.send(chat_id, text)
        except Exception as exc:  # noqa: BLE001 — сеть/Telegram/мок
            status = getattr(exc, "status", None)
            retry_after = getattr(exc, "retry_after", None)
            # 429: один короткий повтор — дальше пусть main делает backoff.
            if status == 429 and retry_after and float(retry_after) <= 10:
                try:
                    time.sleep(float(retry_after))
                    self.tg.send(chat_id, text)
                except Exception:  # noqa: BLE001
                    self.log.warning("Telegram: повторная отправка не удалась (%s)", exc)
                    return
            else:
                self.log.warning("Telegram: отправка не удалась (%s)", exc)
                return
        if self.storage is not None:
            try:
                self.storage.record_message(chat_id, "out", text, slot=slot)
            except Exception:  # noqa: BLE001
                self.log.exception("Не удалось записать исходящее сообщение в БД")

    # ================================================================ СЕССИЯ
    def _load_session(self) -> dict | None:
        raw = self.storage.kv_get(self.SESSION_KEY)
        if not raw:
            return None
        # F9: огромное значение в kv (мусор, старая версия, чужая запись) не
        # разбираем и не таскаем через каждое сообщение — просто сбрасываем.
        raw_text = raw if isinstance(raw, str) else str(raw)
        if len(raw_text) > MAX_SESSION_BYTES:
            self.log.warning(
                "Сессия в kv больше лимита (%d симв. > %d) — сбрасываю",
                len(raw_text), MAX_SESSION_BYTES,
            )
            self.storage.kv_del(self.SESSION_KEY)
            return None
        try:
            session = json.loads(raw_text)
        except (ValueError, TypeError):
            self.log.warning("Битый JSON сессии в kv — сбрасываю")
            self.storage.kv_del(self.SESSION_KEY)
            return None
        if not isinstance(session, dict) or not session.get("slot"):
            self.storage.kv_del(self.SESSION_KEY)
            return None
        return session

    def _save_session(self, session: dict) -> None:
        """Сохраняет сессию в kv, не раздувая значение (F9: лимит ~32 КБ).

        Слишком большая (или несериализуемая) сессия сбрасывается с
        предупреждением в лог: иначе 2 МБ JSON читались бы на каждом сообщении
        и каждом тике планировщика.
        """
        if self.storage is None:
            return
        try:
            raw = json.dumps(session, ensure_ascii=False)
        except (TypeError, ValueError):
            self.log.warning("Сессия не сериализуется в JSON — сбрасываю её")
            self._clear_session()
            return
        size = len(raw.encode("utf-8", "replace"))
        if size > MAX_SESSION_BYTES:
            self.log.warning(
                "Сессия занимает %d КБ (лимит %d КБ) — сбрасываю, чтобы не "
                "раздувать kv", size // 1024, MAX_SESSION_BYTES // 1024,
            )
            self._clear_session()
            return
        self.storage.kv_set(self.SESSION_KEY, raw)

    def _clear_session(self) -> None:
        if self.storage is not None:
            self.storage.kv_del(self.SESSION_KEY)

    def _is_paused(self) -> bool:
        if self.storage is None:
            return False
        return _text(self.storage.kv_get("paused")).strip() in {"1", "true", "yes", "on"}

    # ------------------------------------------------------------ онбординг
    def _onboarding_required(self) -> bool:
        """Знакомство перед первым вопросом включается флагом app.onboarding."""
        return bool(self.app_cfg.get("onboarding"))

    def _is_onboarded(self) -> bool:
        if self.storage is None:
            return True
        return _text(self.storage.kv_get("onboarded")).strip() == "1"

    def _mark_onboarded(self) -> None:
        if self.storage is None:
            return
        try:
            self.storage.kv_set("onboarded", "1")
            self.storage.kv_set("onboarded_at", datetime.now().astimezone().isoformat(timespec="seconds"))
            self.storage.kv_del("onboarding_choice")
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось сохранить флаг онбординга")

    def _quiet_window(self) -> tuple[int, int] | None:
        spec = self.app_cfg.get("quiet_hours")
        if not isinstance(spec, (list, tuple)) or len(spec) != 2:
            return None
        start = self._parse_time(spec[0])
        end = self._parse_time(spec[1])
        if start is None or end is None:
            return None
        return (start[0] * 60 + start[1], end[0] * 60 + end[1])

    def _in_quiet_hours(self, now: datetime) -> bool:
        window = self._quiet_window()
        if window is None:
            return False
        start, end = window
        if start == end:
            return False
        current = now.hour * 60 + now.minute
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _onboarding_text(self) -> str:
        name = _text(self.app_cfg.get("name")) or "Life OS"
        return (
            "Привет! Я %s — дневник-собеседник.\n\n"
            "Несколько раз в день спрашиваю про дела, тело и голову и складываю "
            "ответы в твой Obsidian на этом Mac.\n\n"
            "Дальше я напишу сам: утром — про план, днём — про еду и силы, "
            "вечером — про итоги. Отвечать можно свободным текстом.\n\n"
            "Начать сейчас? Напиши «да» или «вечером».\n\n"
            "Приватность: записи хранятся локально. Если включены «умные» "
            "уточнения (llm.enabled), текст ответов уходит на api.samp.team — "
            "их можно выключить в config.json, и тогда всё останется только у тебя."
            % name
        )

    def _quiet_text(self) -> str:
        return (
            "Поздно уже, не буду грузить вопросами. Напиши, если хочешь что-то "
            "записать, — сохраню. Или продолжим утром."
        )

    def _handle_onboarding_choice(self, chat_id: int, text: str, now: datetime) -> bool:
        """Первое сообщение после знакомства: «да» — начать, «вечером» — ждать.

        Возвращает True, если сообщение обработано как выбор онбординга.
        """
        low = text.strip().lower().strip("!.,… ")
        start_words = {"да", "начать", "сейчас", "давай", "давай сейчас", "поехали", "ок", "окей"}
        wait_words = {"вечером", "потом", "нет", "позже", "не сейчас", "в следующий раз"}
        if low in start_words:
            self._mark_onboarded()
            if self._in_quiet_hours(now):
                self._send(chat_id, self._quiet_text())
                return True
            hour = now.hour
            slot = "morning" if hour < 12 else ("midday" if hour < 18 else "evening")
            self._start_session(slot, now)
            return True
        if low in wait_words:
            self._mark_onboarded()
            self._send(
                chat_id,
                "Договорились. Напишу в ближайший слот — а если что-то захочется "
                "записать раньше, просто пиши сюда.",
            )
            return True
        # P1-2: вместо кнопок-подсказок человек мог сразу написать по делу —
        # сохраняем такой текст и знакомство не теряем.
        if self._free_text_kind(text) == "record":
            self._mark_onboarded()
            entry = self._save_answer(chat_id, "free", "", text, now)
            self._send(
                chat_id,
                "Знакомство пропущу — вижу, есть что записать.\n"
                + self._saved_entry_reply(entry),
            )
            self.log.info("Знакомство: первое содержательное сообщение сохранено")
            return True
        self._send(chat_id, "Напиши «да», чтобы начать сейчас, или «вечером» — подожду до вечера.")
        return True

    def _questions(self, slot: str) -> list[str]:
        """Скриптованные вопросы слота из prompts с fallback-набором."""
        questions: list[str] = []
        try:
            raw = prompts.scripted_questions(slot)
            if isinstance(raw, (list, tuple)):
                questions = [_text(item).strip() for item in raw if _text(item).strip()]
        except Exception:  # noqa: BLE001
            self.log.exception("prompts.scripted_questions упал для слота %s", slot)
        if not questions:
            questions = list(_FALLBACK_QUESTIONS.get(slot, _FALLBACK_QUESTIONS["morning"]))
        return questions

    def _greeting(self, slot: str) -> str:
        name = _text(self.app_cfg.get("user_name")).strip()
        try:
            text = prompts.greeting(slot, name)
            if _text(text).strip():
                return _text(text).strip()
        except Exception:  # noqa: BLE001
            self.log.exception("prompts.greeting упал для слота %s", slot)
        return _FALLBACK_GREETINGS.get(slot, "Привет! Давай сверимся с днём.")

    def _closing_text(self, slot: str) -> str:
        return _CLOSING_TEXTS.get(slot, "Спасибо, всё сохранил. Писать можно в любой момент.")

    # ============================================================ TICK / СЛОТЫ
    def tick(self, now: datetime) -> None:
        """Периодический вызов из main: просроченные сессии, слоты, ролловер.

        Сначала закрываем/напоминаем про зависшую сессию, и только потом
        проверяем слоты: иначе сессия вчерашнего дня блокирует старт
        сегодняшнего слота до следующего тика (REVIEW P2.4).
        """
        now = self._local(now)
        try:
            self._tick_overdue(now)
        except Exception:  # noqa: BLE001
            self.log.exception("tick: ошибка при проверке просроченных сессий")
        try:
            self._tick_slots(now)
        except Exception:  # noqa: BLE001
            self.log.exception("tick: ошибка при запуске слота")
        try:
            self._tick_rollover(now)
        except Exception:  # noqa: BLE001
            self.log.exception("tick: ошибка ежедневного ролловера")

    def _parse_time(self, value) -> tuple[int, int] | None:
        match = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", _text(value))
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    def _catchup_minutes(self) -> float:
        """Сколько минут опоздания ещё можно начать слот (иначе — мягкий пинг)."""
        raw = self.schedule.get("catchup_minutes", 180)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 180.0

    def _tick_slots(self, now: datetime) -> None:
        """1) Если не пауза и время слота пришло — начать сессию."""
        if self._is_paused():
            return
        if self._onboarding_required() and not self._is_onboarded():
            return  # сначала знакомство (/start), потом вопросы
        if self._in_quiet_hours(now):
            return  # тихие часы: не будим вопросами
        if self._load_session() is not None:
            return  # одна сессия за раз

        today = now.date().isoformat()
        due: list[tuple[float, str]] = []
        for slot in _SLOT_ORDER:
            spec = self.schedule.get(slot)
            if not isinstance(spec, dict) or not spec.get("enabled"):
                continue
            hm = self._parse_time(spec.get("time"))
            if hm is None:
                continue
            if slot == "weekly":
                weekday = spec.get("weekday", 6)
                try:
                    weekday = int(weekday)
                except (TypeError, ValueError):
                    weekday = 6
                if now.weekday() != weekday:
                    continue
            slot_dt = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if now < slot_dt:
                continue
            if _text(self.storage.kv_get("last_slot:%s" % slot)) == today:
                continue
            late_minutes = (now - slot_dt).total_seconds() / 60.0
            due.append((late_minutes, slot))

        if not due:
            return

        catchup = self._catchup_minutes()
        ready = sorted(item for item in due if item[0] <= catchup)
        if ready:
            # Самый «свежий» из наступивших слотов (на случай долгого простоя).
            self._start_session(ready[0][1], now)
            return

        # Все слоты сильно опоздали: мягко напомним про вечер, без сессии.
        if {slot for _, slot in due} & {"evening"} and self._schedule_flag("soft_ping", True):
            self._soft_ping_evening(now)

    def _schedule_flag(self, key: str, default: bool) -> bool:
        value = self.schedule.get(key, default)
        if isinstance(value, bool):
            return value
        return bool(value)

    def _start_session(self, slot: str, now: datetime) -> None:
        chat_id = self.chat_id
        if not chat_id:
            self._warn_no_chat(now)
            return
        questions = self._questions(slot)
        first = questions[0] if questions else ""
        greeting = self._greeting(slot)
        session = {
            "slot": slot,
            "idx": 0,
            "step": "question",
            "followups": 0,
            "question": first,
            "asked": first,
            "started_at": now.isoformat(timespec="seconds"),
            "last_activity_at": now.isoformat(timespec="seconds"),
            "date": now.date().isoformat(),
            "answered": [],
            "covered": [],
        }
        # Сначала фиксируем состояние, потом отправляем: крах после отправки
        # не должен приводить к повторному старту в тот же день.
        self._save_session(session)
        try:
            self.storage.kv_set("last_slot:%s" % slot, now.date().isoformat())
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось отметить слот %s", slot)
        parts = [greeting]
        if len(questions) > 1:
            # P1-4: разрешаем ответить сразу про несколько пунктов.
            parts.append("Можно ответить сразу на несколько пунктов — я разберусь.")
        if first:
            parts.append(first)
        text = "\n\n".join(parts)
        self.log.info("Старт сессии «%s» (%d вопросов)", slot, len(questions))
        self._send(chat_id, text, slot=slot)

    def _warn_no_chat(self, now: datetime) -> None:
        key = "warn_no_chat:%s" % now.date().isoformat()
        if self.storage.kv_get(key):
            return
        try:
            self.storage.kv_set(key, "1")
        except Exception:  # noqa: BLE001
            pass
        self.log.warning(
            "Не задан telegram.allowed_chat_id — плановые сессии не отправляются. "
            "Укажите id своего чата в config/config.json (подсказка: @userinfobot)."
        )

    def _soft_ping_evening(self, now: datetime) -> None:
        if not self.chat_id:
            self._warn_no_chat(now)
            return
        key = "evening_ping:%s" % now.date().isoformat()
        if self.storage.kv_get(key):
            return
        try:
            self.storage.kv_set(key, "1")
            self._send(
                self.chat_id,
                "Привет! День почти прошёл. Если хочешь, расскажи в двух словах, "
                "как он прошёл, — я аккуратно запишу в дневник. "
                "Можно и просто написать что угодно, свободным текстом.",
                slot="evening",
            )
            self.log.info("Отправлен мягкий вечерний пинг (вечерний слот пропущен)")
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось отправить мягкий пинг")

    def _tick_overdue(self, now: datetime) -> None:
        """2) Закрыть/напомнить про зависшую сессию."""
        session = self._load_session()
        if session is None:
            return
        today = now.date().isoformat()
        if session.get("date") != today:
            self.log.info("Закрываю сессию прошлого дня (%s)", session.get("date"))
            self._clear_session()
            return
        started_at = self._parse_iso(session.get("last_activity_at")
                                     or session.get("started_at"))
        if started_at is None:
            return
        idle_hours = (now - started_at).total_seconds() / 3600.0
        if idle_hours >= self._session_ttl_hours():
            self.log.info("Сессия «%s» висит %.1f ч — закрываю", session.get("slot"), idle_hours)
            self._clear_session()
            return
        if idle_hours >= self._session_reminder_hours() and not session.get("reminded"):
            if self.chat_id and self._in_quiet_hours(now):
                # Тихие часы: не будим. Флаг не ставим — напоминание уйдёт
                # утром, когда окно закроется (README: «бот сам не пишет»).
                self.log.info("Напоминание отложено: тихие часы")
                return
            session["reminded"] = True
            self._save_session(session)
            if self.chat_id:
                self._send(
                    self.chat_id,
                    "Если ещё актуально — продолжим с того же места: просто ответь "
                    "на вопрос выше. Если нет — напиши /skip или /stop.",
                    slot=session.get("slot"),
                )
                self.log.info("Отправлено напоминание о незавершённой сессии")

    def _session_reminder_hours(self) -> float:
        return self._cfg_hours("session_reminder_hours", 3.0)

    def _session_ttl_hours(self) -> float:
        return self._cfg_hours("session_ttl_hours", 12.0)

    def _cfg_hours(self, key: str, default: float) -> float:
        try:
            return max(0.0, float(self.app_cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _parse_iso(self, value) -> datetime | None:
        text = _text(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return self._local(parsed) if parsed.tzinfo else parsed.replace(tzinfo=self.tz)

    def _tick_rollover(self, now: datetime) -> None:
        """3) Раз в день: досинхронизировать Obsidian и пересобрать дашборд.

        Маркер ``last_rollover`` пишется только после успешной работы: если
        vault/дашборд упали, следующий тик повторит ролловер в этот же день
        (REVIEW P2.5), а не отложит его до завтра.
        """
        today = now.date().isoformat()
        try:
            if _text(self.storage.kv_get("last_rollover")) == today:
                return
        except Exception:  # noqa: BLE001
            self.log.exception("Ролловер: не удалось прочитать kv")
            return
        self.log.info("Ролловер дня %s", today)

        sync_ok = self._sync_vault() >= 0
        goals_ok = self._rewrite_goals()
        mirror_ok = self._write_mirror_safe()
        self._rebuild_dashboard()
        dash_ok = self._dashboard_ok
        if not (sync_ok and goals_ok and mirror_ok and dash_ok):
            self.log.warning(
                "Ролловер дня %s завершился с ошибками — повторю на следующем тике",
                today,
            )
            return
        try:
            self.storage.kv_set("last_rollover", today)
        except Exception:  # noqa: BLE001
            self.log.exception(
                "Ролловер: не удалось сохранить last_rollover — повторю на следующем тике"
            )

    def _sync_vault(self) -> int:
        """Досинхронизация Obsidian; -1 — ошибка (для повтора ролловера)."""
        if self.vault is None or not hasattr(self.vault, "sync_pending"):
            return 0
        try:
            written = self.vault.sync_pending()
            written = int(written or 0)
        except Exception:  # noqa: BLE001
            self.log.exception("Vault.sync_pending упал")
            return -1
        if written:
            self.log.info("Досинхронизировано записей в Obsidian: %d", written)
        return written

    def _rewrite_goals(self) -> bool:
        """True — готово (в т.ч. если vault нет), False — ошибка."""
        if self.vault is None or not hasattr(self.vault, "rewrite_goals"):
            return True
        try:
            self.vault.rewrite_goals()
        except Exception:  # noqa: BLE001
            self.log.exception("Vault.rewrite_goals упал")
            return False
        return True

    def _rebuild_dashboard(self) -> str | None:
        if self.dash_build is None:
            self._dashboard_ok = True
            return None
        try:
            path = self.dash_build(self.cfg, self.storage)
        except Exception:  # noqa: BLE001
            self.log.exception("Сборка дашборда упала")
            self._dashboard_ok = False
            return None
        self._dashboard_ok = bool(path)
        return path if isinstance(path, str) else None

    # ============================================================== ВХОДЯЩИЕ
    def on_message(self, chat_id: int, text: str, now: datetime) -> None:
        """Обрабатывает любое входящее сообщение: команда, ответ, свободный чат."""
        now = self._local(now)
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            self.log.warning("Некорректный chat_id: %r — игнорирую", chat_id)
            return
        if not self._is_allowed(chat_id):
            return

        # F8: lone surrogate из «битого» апдейта не должен доходить до SQLite,
        # vault и тела запроса к LLM — они не кодируются в UTF-8.
        text = telegram.sanitize_text(_text(text))
        session = self._load_session()
        try:
            self.storage.record_message(
                chat_id, "in", text, slot=(session or {}).get("slot")
            )
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось записать входящее сообщение в БД")

        stripped = text.strip()
        if not stripped:
            self._send(chat_id, "Пустое сообщение. Напиши /help, если нужны подсказки.")
            return

        try:
            command = self._parse_command(stripped)
            if command is not None:
                self._handle_command(command[0], command[1], chat_id, now)
                return

            # Первый выбор после знакомства («да» / «вечером») — до сессий.
            if (
                self._onboarding_required()
                and not self._is_onboarded()
                and _text(self.storage.kv_get("onboarding_choice")).strip() == "1"
            ):
                if self._handle_onboarding_choice(chat_id, stripped, now):
                    return

            # P1-3: первое сообщение без /start — тоже знакомство, а не тишина
            # и не свободный чат. Иначе человек, написавший «привет», никогда
            # не онбордился и не получал плановых вопросов.
            if self._onboarding_required() and not self._is_onboarded():
                self.storage.kv_set("onboarding_choice", "1")
                self._send(chat_id, self._onboarding_text())
                if self._free_text_kind(stripped) == "record":
                    entry = self._save_answer(chat_id, "free", "", stripped, now)
                    if entry is not None:
                        self._send(chat_id, self._saved_entry_reply(entry))
                self.log.info("Знакомство начато с первого сообщения без /start")
                return

            session = self._load_session()
            if session is not None:
                self._handle_session_reply(chat_id, stripped, now, session)
            else:
                self._handle_free_text(chat_id, stripped, now)
        except Exception:  # noqa: BLE001 — чат не должен падать из-за одной ошибки
            self.log.exception("Ошибка обработки сообщения")
            try:
                self._send(
                    chat_id,
                    "Что-то пошло не так на моей стороне. Попробуй ещё раз "
                    "или напиши /status.",
                )
            except Exception:  # noqa: BLE001
                self.log.exception("Не удалось отправить сообщение об ошибке")

    def _is_allowed(self, chat_id: int) -> bool:
        if not self.chat_id:
            return True  # chat_id не настроен (dev/dry-run) — не блокируем
        if chat_id != self.chat_id:
            self.log.warning("Сообщение от чужого chat_id=%s проигнорировано", chat_id)
            return False
        return True

    # ============================================================== КОМАНДЫ
    def _parse_command(self, text: str) -> tuple[str, str] | None:
        """Возвращает (команда, аргументы) или None, если это не команда."""
        stripped = text.strip()
        if stripped.startswith("/"):
            head, _, rest = stripped.partition(" ")
            name = head[1:].split("@", 1)[0].strip().lower()
            if not name:
                return None
            return (_COMMAND_ALIASES.get(name, "unknown"), rest.strip())
        bare = stripped.rstrip("!.").strip().lower()
        command = _COMMAND_ALIASES.get(bare)
        if command:
            return command, ""
        return None

    def _handle_command(self, command: str, args: str, chat_id: int, now: datetime) -> None:
        if command == "start":
            if self._onboarding_required() and not self._is_onboarded():
                self.storage.kv_set("onboarding_choice", "1")
                self._send(chat_id, self._onboarding_text())
            elif self._load_session() is not None:
                self._send(
                    chat_id,
                    "Мы на середине вопросов. Продолжить — просто ответь на последний "
                    "вопрос, или /stop, чтобы остановить сессию.",
                )
            else:
                self._send(chat_id, self._start_text())
        elif command == "pause":
            self.storage.kv_set("paused", "1")
            self._send(
                chat_id,
                "⏸ Пауза включена: плановые вопросы приходить не будут. "
                "Ты можешь писать мне сам, а вернуть опросы — /resume (или «продолжить»).",
            )
            self.log.info("Пользователь поставил бота на паузу")
        elif command == "resume":
            was_paused = self._is_paused()
            self.storage.kv_set("paused", "0")
            self._send(
                chat_id,
                "▶️ Пауза снята, работаю по расписанию." if was_paused
                else "▶️ Я и так работаю по расписанию — паузы не было.",
            )
        elif command == "status":
            self._send(chat_id, self.status_text())
        elif command == "today":
            self._send(chat_id, self.today_text())
        elif command == "report":
            self._send(chat_id, self.report_text())
        elif command == "goals":
            self._send(chat_id, self.goals_text())
        elif command == "add_goal":
            self._handle_add_goal(chat_id, args)
        elif command == "habit":
            self._handle_habit(chat_id, args, now)
        elif command == "habits":
            self._send(chat_id, self.habits_text())
        elif command == "skip":
            session = self._load_session()
            if session is None:
                self._send(chat_id, "Сейчас нет активной сессии — пропускать нечего. /status покажет состояние.")
            else:
                self._advance(chat_id, now, session, skipped=True)
        elif command == "stop":
            session = self._load_session()
            if session is None:
                self._send(chat_id, "Сейчас нет активной сессии. Писать свободным текстом всё равно можно.")
            else:
                self._clear_session()
                self._send(chat_id, "Ок, сессию остановил. Ответы, которые ты уже дал, сохранены. Вернёмся в следующий слот или пиши что хочешь.")
                self.log.info("Сессия остановлена пользователем")
        elif command == "help":
            self._send(chat_id, self.help_text())
        elif command == "design":
            path = self._rebuild_dashboard()
            if path:
                self._send(chat_id, "🎨 Дашборд обновлён.\nФайл: %s" % path)
            elif self.dash_build is None:
                self._send(chat_id, "Дашборд пока не подключён (нет модуля dashboard_data).")
            else:
                self._send(chat_id, "Не удалось собрать дашборд — смотри лог агента, данные не потеряны.")
        else:
            self._send(
                chat_id,
                "Не знаю такую команду. Список всех команд — /help. "
                "А свободный текст я всегда понимаю.",
            )

    def _handle_add_goal(self, chat_id: int, args: str) -> None:
        raw = _text(args).strip()
        if not raw:
            self._send(
                chat_id,
                "Чтобы добавить цель, напиши в формате:\n"
                "/add_goal Название | прогресс | срок\n"
                "Например: /add_goal Пробежать 10 км | 3/10 | 2027-06-01\n"
                "Срок и текущий прогресс можно не указывать.",
            )
            return
        parts = [part.strip() for part in raw.split("|")]
        title = parts[0] if parts else ""
        if not title:
            self._send(chat_id, "Не понял название цели. Напиши: /add_goal Название | прогресс | срок")
            return
        # Прогресс можно задать как «3/10» или «3 из 10»; иначе это просто цель.
        progress_raw = parts[1] if len(parts) > 1 else ""
        current = ""
        target = progress_raw
        match = re.match(r"^\s*([\d.,]+)\s*(?:/|из)\s*([\d.,]+)\s*$", progress_raw)
        if match:
            current, target = match.group(1), match.group(2)
        goal = {
            "title": title,
            "target": target,
            "current": current,
            # Стартовая точка = текущее значение на момент создания: прогресс
            # считается от неё (P0-1), поэтому новая цель «3 из 10» — это 0%,
            # а не 30% от нуля, которого никогда не было.
            "start": current,
            "direction": goals_math.goal_direction({"title": title}),
            "deadline": parts[2] if len(parts) > 2 else "",
            "status": "active",
        }
        try:
            goal_id = self.storage.upsert_goal(goal)
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось сохранить цель")
            self._send(chat_id, "Не получилось сохранить цель — попробуй ещё раз.")
            return
        self._rewrite_goals()
        self._write_mirror_safe()
        details = []
        if current and target:
            details.append("прогресс %s из %s" % (current, target))
        elif goal["target"]:
            details.append("цель: %s" % goal["target"])
        if goal["deadline"]:
            details.append("срок: %s" % goal["deadline"])
        suffix = (" (%s)" % ", ".join(details)) if details else ""
        self._send(chat_id, "🎯 Цель добавлена: %s%s.\nВсе цели — /goals." % (title, suffix))
        self.log.info("Добавлена цель id=%s: %s", goal_id, title)

    def _handle_habit(self, chat_id: int, args: str, now: datetime) -> None:
        """«/habit зарядка» — отметить привычку за сегодня (P1-6)."""
        name = _text(args).strip()[:80].strip()
        if not name:
            self._send(
                chat_id,
                "Чтобы отметить привычку, напиши её название:\n"
                "/habit зарядка\n"
                "Список и серии — /habits.",
            )
            return
        day = now.date().isoformat()
        try:
            self.storage.touch_habit(name, day)
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось отметить привычку")
            self._send(chat_id, "Не получилось сохранить привычку — попробуй ещё раз.")
            return
        streak = 0
        try:
            for habit in self.storage.habits():
                if _text(habit.get("name")).strip().lower() == name.lower():
                    streak = int(habit.get("streak") or 0)
                    break
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось прочитать привычки для подтверждения")
        # Дашборд читает Storage, поэтому привычка появляется на нём сразу.
        self._rebuild_dashboard()
        self._send(
            chat_id,
            "✅ Привычка «%s» отмечена. Серия: %d %s.\nСписок — /habits."
            % (name, streak, _plural(streak, ("день", "дня", "дней"))),
        )
        self.log.info("Привычка отмечена: %s (серия %d)", name, streak)

    def _start_text(self) -> str:
        name = _text(self.app_cfg.get("name")) or "Life OS"
        return (
            "Привет! Я %s — твой персональный дневник-собеседник.\n\n"
            "Я сам пишу несколько раз в день: утром — про план, днём — про еду и "
            "состояние, вечером — про итоги дня. Отвечать можно в любой момент, "
            "в том числе свободным текстом — всё аккуратно запишу в дневник и "
            "отражу на дашборде.\n\n"
            "Полезное:\n"
            "• /today — записи за сегодня\n"
            "• /report — отчёт за 7 дней\n"
            "• /goals — цели и прогресс\n"
            "• /status — что сейчас происходит\n"
            "• /help — все команды\n\n"
            "Приватность: записи хранятся в твоём Obsidian на этом Mac. Если включены "
            "«умные» уточнения (llm.enabled), текст ответов уходит на api.samp.team — "
            "их можно отключить в config.json, тогда всё останется только у тебя.\n\n"
            "Если сейчас неудобно — /pause, вернуться — /resume."
            % name
        )

    def help_text(self) -> str:
        return (
            "Команды Life OS:\n"
            "/start — приветствие\n"
            "/status — состояние: пауза, слоты, активная сессия\n"
            "/today — записи за сегодня\n"
            "/report — итоги за 7 дней\n"
            "/goals — цели и прогресс\n"
            "/add_goal Название | 3/10 | срок — добавить цель\n"
            "/habit зарядка — отметить привычку сегодня\n"
            "/habits — привычки и серии\n"
            "/skip — пропустить текущий вопрос\n"
            "/pause — пауза (плановые вопросы не приходят)\n"
            "/resume — продолжить\n"
            "/дизайн — пересобрать дашборд (то же: «дашборд»)\n"
            "/stop — прервать сессию\n"
            "/help — эта справка\n\n"
            "Свободный текст можно писать в любой момент: отвечу и сохраню в дневник.\n\n"
            "Приватность: записи лежат локально в Obsidian. «Умные» уточнения "
            "(llm.enabled) отправляют текст ответов на api.samp.team; выключи "
            "llm.enabled в config.json — и данные останутся только у тебя."
        )

    # ======================================================= ДИАЛОГ: СЕССИЯ
    def _handle_session_reply(self, chat_id: int, text: str, now: datetime,
                              session: dict) -> None:
        low = text.strip().lower()
        if low in _STOP_TOKENS:
            self._clear_session()
            self._send(chat_id, "Ок, сессию прервал. Уже сказанное сохранено — продолжим в следующий слот.")
            return
        if low in _SKIP_TOKENS:
            self._advance(chat_id, now, session, skipped=True)
            return

        slot = _text(session.get("slot")) or "morning"
        question = _text(session.get("asked")) or _text(session.get("question"))
        self._save_answer(chat_id, slot, question, text, now)

        session["last_activity_at"] = now.isoformat(timespec="seconds")
        answered = session.get("answered")
        if not isinstance(answered, list):
            answered = []
        if len(answered) < 40:
            answered.append({"question": question[:160], "answer": text[:200]})
        session["answered"] = answered
        self._remember_topics(session, text)

        followup = self._maybe_followup(slot, _text(session.get("question")), text, session)
        if followup:
            try:
                followups = int(session.get("followups") or 0)
            except (TypeError, ValueError):
                followups = 0
            session["followups"] = followups + 1
            session["step"] = "followup"
            session["asked"] = followup
            self._save_session(session)
            self._send(chat_id, followup, slot=slot)
            return

        self._advance(chat_id, now, session)

    # ---------------------------------------------------- темы и уточнения
    def _remember_topics(self, session: dict, text: str) -> None:
        """Запоминает темы, которые человек уже раскрыл (P1-4)."""
        covered = session.get("covered")
        if not isinstance(covered, list):
            covered = []
        for topic in self._topics_in_text(text):
            if topic not in covered:
                covered.append(topic)
        session["covered"] = covered[:20]

    def _topics_in_text(self, text: str) -> set:
        """Темы, которые видны в тексте ответа (простая эвристика, без LLM)."""
        low = _text(text).lower().replace("ё", "е")
        if not low.strip():
            return set()
        topics = set()
        for topic, pattern in _TOPIC_PATTERNS.items():
            try:
                if pattern.search(low):
                    topics.add(topic)
            except Exception:  # noqa: BLE001 — regex не должен ронять бота
                continue
        return topics

    def _current_topic(self, session: dict) -> str | None:
        slot = _text(session.get("slot")).strip().lower()
        topics = _SLOT_TOPICS.get(slot, ())
        try:
            idx = int(session.get("idx") or 0)
        except (TypeError, ValueError):
            idx = 0
        if 0 <= idx < len(topics):
            return topics[idx]
        return None

    def _topic_is_covered(self, topic: str, covered: set) -> bool:
        # Пропускаем только конкретные темы, у которых есть человеческая
        # подпись. «План на день», «итоги недели» и «тело» — составные,
        # их не пропускаем: легко ошибиться и потерять вопрос.
        if topic not in _TOPIC_LABELS:
            return False
        return topic in covered

    @staticmethod
    def _join_ru(items: list) -> str:
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return "%s и %s" % (items[0], items[1])
        return "%s, %s и %s" % (", ".join(items[:-1]), items[-1])

    def _already_told_text(self, topics: list) -> str:
        labels = []
        for topic in topics:
            label = _TOPIC_LABELS.get(topic)
            if label and label not in labels:
                labels.append(label)
        if not labels:
            return ""
        return "Хорошо, про %s ты уже рассказал." % self._join_ru(labels)

    def _answer_needs_followup(self, answer: str) -> bool:
        """Нужен ли уточняющий вопрос: смотрим на смысл, а не на длину (P1-5)."""
        low = _text(answer).strip().lower().replace("ё", "е")
        if not low:
            return True
        if low.strip(".!…,;:—-«»\"' ") in _VAGUE_ANSWERS:
            return True
        for marker in _VAGUE_MARKERS:
            if marker in low:
                return True
        # Цифры и несколько содержательных слов — значит человек уже рассказал.
        words = [word for word in _WORD_RE.findall(low) if len(word) > 1]
        if _DIGIT_RE.search(low):
            return False
        if len(words) >= 4:
            return False
        if len(words) <= 2:
            return True
        # Три слова: уточняем, только если это всё ещё общая оценка.
        return any(word in _VAGUE_WORDS for word in words)

    def _scripted_followup(self, slot: str, topic: str | None) -> str:
        if topic and topic in _TOPIC_FOLLOWUPS:
            return _TOPIC_FOLLOWUPS[topic]
        return _FOLLOWUP_FALLBACKS.get(slot, "Что именно — хотя бы примерно?")

    def _maybe_followup(self, slot: str, question: str, answer: str,
                        session: dict) -> str | None:
        """Решает, нужен ли уточняющий вопрос. None — не нужен/не можем."""
        try:
            followups = int(session.get("followups") or 0)
        except (TypeError, ValueError):
            followups = 0
        if followups >= self.max_followups:
            return None
        if not self._answer_needs_followup(answer):
            # Развёрнутый/конкретный ответ не переспрашиваем и LLM не зовём.
            return None
        topic = self._current_topic(session)
        scripted = self._scripted_followup(slot, topic)
        if not self._llm_available():
            # Без LLM уточняем скриптованным вопросом, но только один раз
            # на вопрос: повторять то же самое человеку неприятно.
            if _text(session.get("step")) == "followup":
                return None
            return scripted
        try:
            messages = prompts.followup_messages(slot, question, answer, self._llm_history())
            reply = self.llm.chat(messages)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Follow-up через LLM не получился: %s", exc)
            return scripted
        cleaned = _clip_reply(reply)
        if not cleaned or self._followup_is_skip(cleaned):
            return None
        return cleaned

    def _followup_is_skip(self, reply: str) -> bool:
        """LLM ответил «OK/ОК» — уточнение не нужно (используем prompts, если есть).

        Если ``prompts.followup_is_skip`` вернул False (или упал), пробуем
        запасной ``_is_ok``: модели иногда отвечают «Хорошо»/«Готово» вместо
        строгого «OK» (REVIEW P2.7).
        """
        helper = getattr(prompts, "followup_is_skip", None)
        if callable(helper):
            try:
                if bool(helper(reply)):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return _is_ok(reply)

    def _advance(self, chat_id: int, now: datetime, session: dict,
                 skipped: bool = False) -> None:
        """Перейти к следующему вопросу или завершить сессию.

        Вопросы, темы которых человек уже раскрыл в своих ответах, пропускаем
        без вопроса — с короткой пометкой «уже рассказал» (UX_REVIEW P1-4).
        """
        slot = _text(session.get("slot")) or "morning"
        questions = self._questions(slot)
        topics = _SLOT_TOPICS.get(slot, ())
        try:
            idx = int(session.get("idx") or 0) + 1
        except (TypeError, ValueError):
            idx = 1
        session["followups"] = 0
        session["step"] = "question"
        session["last_activity_at"] = now.isoformat(timespec="seconds")
        covered = session.get("covered")
        if not isinstance(covered, list):
            covered = []
        covered_set = set(covered)
        skipped_topics = []
        while idx < len(questions):
            topic = topics[idx] if idx < len(topics) else None
            if topic and self._topic_is_covered(topic, covered_set):
                if topic not in skipped_topics:
                    skipped_topics.append(topic)
                idx += 1
                continue
            break
        if idx < len(questions):
            session["idx"] = idx
            session["question"] = questions[idx]
            session["asked"] = questions[idx]
            self._save_session(session)
            prefix = "Хорошо, пропустил.\n\n" if skipped else ""
            if skipped_topics:
                prefix += self._already_told_text(skipped_topics) + "\n\n"
            self._send(chat_id, prefix + questions[idx], slot=slot)
            return

        self._clear_session()
        self.log.info("Сессия «%s» завершена", slot)
        self._write_mirror_safe()
        self._send(chat_id, self._closing_text(slot), slot=slot)

    # ==================================================== ДИАЛОГ: СВОБОДНЫЙ
    def _handle_free_text(self, chat_id: int, text: str, now: datetime) -> None:
        """Свободный чат: болтовня — ответ без записи, рассказ — запись.

        Сначала определяем ``_free_text_kind`` (UX_REVIEW P1-1): LLM и дневник
        получают только содержательный текст. Иначе «привет» уходил в модель,
        сохранялся записью и навсегда оставался в /today.
        """
        kind = self._free_text_kind(text)
        if kind != "record":
            self._send(chat_id, self._free_text_reply(kind), slot="free")
            return
        reply: str | None = None
        if self._llm_available():
            try:
                messages = [{"role": "system", "content": prompts.system_prompt()}]
                messages.extend(self._llm_history(limit=12))
                reply = self.llm.chat(messages)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("LLM не ответил в свободном чате: %s", exc)
        clipped = _clip_reply(reply)
        if clipped:
            self._send(chat_id, clipped, slot="free")
            self._save_answer(chat_id, "free", "", text, now)
            return
        # Без LLM содержательный рассказ сохраняем и подтверждаем записью.
        entry = self._save_answer(chat_id, "free", "", text, now)
        self._send(chat_id, self._saved_entry_reply(entry), slot="free")

    def _free_text_reply(self, kind: str) -> str:
        """Ответ на текст, который не сохраняем в дневник."""
        if kind == "greeting":
            return self._greeting_reply()
        if kind == "thanks":
            return self._thanks_reply()
        if kind == "capabilities":
            return self._capabilities_reply()
        if kind == "question":
            return self._question_reply(self._llm_available())
        return self._chatter_reply()

    def _free_text_kind(self, text: str) -> str:
        """Что это за свободный текст: record / greeting / thanks / … / question.

        Простая эвристика без LLM: сначала отсекаем явные вопросы и болтовню,
        а содержательный рассказ о жизни сохраняем (UX_REVIEW P1-6).
        """
        clean = _text(text).strip()
        low = clean.lower().replace("ё", "е")
        if not low:
            return "chatter"
        # «ок», «понял», «норм» — подтверждение, а не заметка (P2).
        if _is_ack_only(low):
            return "chatter"
        is_question = clean.rstrip("… .!").endswith("?")
        explicit = bool(is_question and _EXPLICIT_QUESTION_RE.search(low))
        if explicit:
            if _CAPABILITY_RE.search(low):
                return "capabilities"
            if _CHITCHAT_RE.search(low):
                return "chatter"
            return "question"
        is_record = bool(_LIFE_SIGNAL_RE.search(low))
        if not is_record:
            try:
                is_record = bool(parsing.heuristic_metrics(clean))
            except Exception:  # noqa: BLE001 — метрики не должны ронять чат
                is_record = False
        if is_record:
            return "record"
        if _THANKS_RE.search(low):
            return "thanks"
        if _CAPABILITY_RE.search(low):
            return "capabilities"
        if _GREETING_RE.search(low):
            return "greeting"
        if _CHITCHAT_RE.search(low):
            return "chatter"
        if _REQUEST_RE.search(low) or is_question:
            return "question"
        return "record"

    def _greeting_reply(self) -> str:
        return (
            "Привет! У меня всё ровно, я на связи. Расскажи, что хочется "
            "записать, — сохраню в дневник. Или /help: покажу, что умею."
        )

    def _thanks_reply(self) -> str:
        return "Пожалуйста! Если захочешь что-то записать — просто напиши."

    def _capabilities_reply(self) -> str:
        return (
            "Я дневник-собеседник: спрашиваю про день и складываю ответы "
            "в Obsidian. Записать можно просто текстом. Посмотреть — /today, "
            "/report, /goals; всё остальное — /help."
        )

    def _chatter_reply(self) -> str:
        return (
            "Я рядом. Если есть что записать — еда, сон, тренировка, задачи, "
            "мысли — просто напиши."
        )

    def _question_reply(self, llm_available: bool = False) -> str:
        if llm_available:
            # LLM включён, но вопросы в свободном чате не сохраняем и не
            # отправляем в модель — честно говорим, что умеем.
            return (
                "На вопросы развёрнуто не отвечаю — я дневник-собеседник: "
                "записываю и показываю твой день. Еда, сон, тренировки, "
                "задачи, мысли — просто напиши. Команды — /help."
            )
        return (
            "Сейчас я без языковой модели, поэтому развёрнуто ответить не смогу. "
            "А записать в дневник могу: еда, сон, тренировка, задачи, мысли — "
            "просто напиши. Команды — /help."
        )

    def _metrics_snippet(self, metrics: dict, category: str = "") -> str:
        """Короткая человеческая выжимка по метрикам записи."""
        if not isinstance(metrics, dict):
            return ""
        category = _text(category).strip().lower()
        parts = []
        training_type = _text(metrics.get("training_type")).strip()
        if training_type:
            parts.append(training_type)
        duration = _to_float(metrics.get("duration_min"))
        if duration:
            parts.append("%s мин" % _fmt_num(duration))
        sleep = _to_float(metrics.get("sleep_h"))
        if sleep:
            parts.append(("%s ч" if category == "sleep" else "сон %s ч")
                         % _fmt_num(sleep))
        water = _to_float(metrics.get("water_ml"))
        if water:
            parts.append(("%s мл" if category == "water" else "вода %s мл")
                         % _fmt_num(water))
        calories = _to_float(metrics.get("calories"))
        if calories:
            parts.append("~%s ккал" % _fmt_num(calories))
        protein = _to_float(metrics.get("protein_g"))
        if protein:
            parts.append("%s г белка" % _fmt_num(protein))
        for key, label in (("mood", "настроение"), ("energy", "энергия"),
                           ("focus", "фокус")):
            value = _to_float(metrics.get(key))
            if value:
                if category == key:
                    parts.append("%s/5" % _fmt_num(value))
                else:
                    parts.append("%s %s/5" % (label, _fmt_num(value)))
                break
        done = metrics.get("tasks_done")
        if isinstance(done, list) and done:
            parts.append("задач: %d" % len(done))
        return ", ".join(parts[:3])

    def _saved_entry_reply(self, entry: dict | None) -> str:
        """Подтверждение записи: что именно записали, без канцелярита."""
        if not isinstance(entry, dict):
            return (
                "Хотел сохранить, но запись не удалась. Попробуй ещё раз — "
                "или /status покажет состояние."
            )
        category = _text(entry.get("category")) or "other"
        snippet = self._metrics_snippet(entry.get("metrics") or {}, category)
        if category == "other":
            return "Записал как заметку. /today покажет день целиком."
        label = self._category_label(category)
        if snippet:
            return "Записал: %s (%s). /today покажет день целиком." % (label, snippet)
        return "Записал: %s. /today покажет день целиком." % label

    # ========================================================= СОХРАНЕНИЕ
    def _save_answer(self, chat_id: int, slot: str, question: str,
                     answer: str, now: datetime) -> dict | None:
        """Ответ → entry (parsing) → SQLite → Obsidian → дашборд.

        Возвращает сохранённую запись (dict с id) или None, если сохранить
        не удалось: по ней же строим подтверждение в свободном чате.
        """
        now_iso = now.isoformat(timespec="seconds")
        answer = telegram.sanitize_text(answer)  # F8: без lone surrogate
        entry: dict | None = None
        try:
            entry = parsing.parse_answer(self.llm, slot, question, answer, now_iso)
        except Exception:  # noqa: BLE001
            self.log.exception("parsing.parse_answer упал — сохраняю ответ как есть")
        if not isinstance(entry, dict):
            entry = {}

        entry["date"] = _text(entry.get("date")) or now.date().isoformat()
        entry["ts"] = _text(entry.get("ts")) or now_iso
        entry["slot"] = _text(entry.get("slot")) or slot
        entry["category"] = _text(entry.get("category")) or "other"
        entry["title"] = _text(entry.get("title")) or self._short_title(answer)
        entry["text"] = _text(entry.get("text")) or answer
        entry["raw"] = _text(entry.get("raw")) or answer
        if not isinstance(entry.get("metrics"), dict):
            entry["metrics"] = {}

        try:
            entry_id = self.storage.add_entry(entry)
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось сохранить запись в SQLite")
            return None
        entry["id"] = int(entry_id)

        if self._write_to_vault(entry):
            try:
                self.storage.set_entry_written(int(entry_id), True)
            except Exception:  # noqa: BLE001
                self.log.exception("Не удалось отметить запись written=1")
        self._write_mirror_safe()
        self._rebuild_dashboard()
        return entry

    def _write_to_vault(self, entry: dict) -> bool:
        if self.vault is None:
            return False
        obsidian_cfg = self.cfg.get("obsidian") or {}
        if obsidian_cfg.get("enabled") is False:
            return False
        try:
            return bool(self.vault.write_entry(entry))
        except Exception:  # noqa: BLE001
            self.log.exception("Vault.write_entry упал — запись останется в БД")
            return False

    def _write_mirror_safe(self) -> bool:
        """Обновляет machine-readable зеркало Obsidian (entries.jsonl/goals.json).

        Vault.write_mirror() ждёт полный список записей; контракт Storage не
        меняем — берём все записи через entries() с широким диапазоном дат.
        Любые ошибки только логируются: зеркало вторично по отношению к БД/дневнику.
        Возвращает False при ошибке — ролловер тогда повторится (REVIEW P2.5).
        """
        if self.vault is None or not hasattr(self.vault, "write_mirror"):
            return True
        obsidian_cfg = self.cfg.get("obsidian") or {}
        if obsidian_cfg.get("enabled") is False:
            return True
        try:
            all_entries = self.storage.entries("0001-01-01", "9999-12-31")
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось прочитать записи для зеркала Obsidian")
            return False
        try:
            self.vault.write_mirror(all_entries)
        except Exception:  # noqa: BLE001
            self.log.exception(
                "Vault.write_mirror упал — данные в БД и дневнике не потеряны"
            )
            return False
        return True

    def _short_title(self, text: str, limit: int = 80) -> str:
        for line in _text(text).splitlines():
            line = line.strip()
            if line:
                return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
        return "запись"

    # ========================================================== LLM-ПОМОЩЬ
    def _llm_available(self) -> bool:
        try:
            return bool(self.llm is not None and self.llm.available())
        except Exception:  # noqa: BLE001
            return False

    def _llm_history(self, limit: int = 10,
                     max_chars: int = MAX_HISTORY_CHARS) -> list[dict]:
        """История диалога в формате OpenAI chat messages.

        Бюджет ``max_chars`` символов (F2 адверсариального теста): сообщения
        набираются от новых к старым, старые отбрасываются целиком. Самое
        свежее сообщение (текущая реплика человека) посередине не режется:
        если оно одно длиннее бюджета, остаётся его начало.

        Args:
            limit: сколько последних сообщений вообще запрашивать из БД.
            max_chars: максимальная суммарная длина истории в символах.

        Returns:
            Список сообщений в хронологическом порядке (старые → новые).
        """
        try:
            rows = self.storage.chat_history(limit)
        except Exception:  # noqa: BLE001
            return []
        prepared: list[tuple[str, str]] = []
        for row in rows:
            content = _text(row.get("text")).strip()
            if not content:
                continue
            role = "user" if _text(row.get("direction")) == "in" else "assistant"
            prepared.append((role, content))

        selected: list[tuple[str, str]] = []
        used = 0
        for role, content in reversed(prepared):
            size = len(content)
            if selected:
                if used + size > max_chars:
                    break
            elif size > max_chars:
                # Текущее (самое свежее) сообщение длиннее бюджета: режем хвост,
                # а не середину, и не выбрасываем его целиком. Многоточие входит
                # в бюджет, поэтому итог никогда не длиннее max_chars.
                content = content[: max(1, max_chars - 1)].rstrip() + "…"
                size = len(content)
            selected.append((role, content))
            used += size
        return [{"role": role, "content": content}
                for role, content in reversed(selected)]

    # ================================================================ ТЕКСТЫ
    def status_text(self) -> str:
        now = datetime.now(self.tz)
        today = now.date().isoformat()
        paused = self._is_paused()
        session = self._load_session()

        lines = ["📊 Life OS — статус", ""]
        lines.append("Состояние: %s" % ("⏸ на паузе (вернуться: /resume)" if paused else "✅ работает"))
        lines.append("Время: %s, %s" % (self._today_human(now.date()), now.strftime("%H:%M")))

        # Тихие часы: ночью бот молчит, и /status должен это показывать, а не
        # делать вид, что слоты просто «ждут» (UX_REVIEW, P2).
        window = self._quiet_window()
        if window is not None:
            start_hm = _minutes_to_hm(window[0])
            end_hm = _minutes_to_hm(window[1])
            if self._in_quiet_hours(now):
                lines.append("Тихие часы до %s — вопросов не будет, писать можно." % end_hm)
            else:
                lines.append("Тихие часы %s–%s" % (start_hm, end_hm))

        slot_parts = []
        for slot in ("morning", "midday", "evening"):
            spec = self.schedule.get(slot) or {}
            label = self._slot_label(slot)
            if not isinstance(spec, dict) or not spec.get("enabled"):
                slot_parts.append("%s — выкл" % label)
                continue
            hm = self._parse_time(spec.get("time"))
            sent_today = _text(self.storage.kv_get("last_slot:%s" % slot)) == today
            if sent_today:
                slot_parts.append("%s ✅" % label)
            elif hm is not None and (now.hour, now.minute) >= hm:
                slot_parts.append("%s ⏳ ждёт" % label)
            else:
                slot_parts.append("%s 🕓 %s" % (label, _text(spec.get("time"))))
        weekly = self.schedule.get("weekly") or {}
        if isinstance(weekly, dict) and weekly.get("enabled"):
            try:
                weekday = int(weekly.get("weekday", 6))
            except (TypeError, ValueError):
                weekday = 6
            if weekday == now.weekday():
                done = _text(self.storage.kv_get("last_slot:weekly")) == today
                slot_parts.append("неделя %s" % ("✅" if done else "🕓 %s" % _text(weekly.get("time"))))
            else:
                slot_parts.append("неделя — %s" % _WEEKDAY_SHORT[weekday % 7])
        else:
            slot_parts.append("неделя — выкл")
        lines.append("Слоты: " + " · ".join(slot_parts))

        if session is not None:
            slot = _text(session.get("slot")) or "morning"
            try:
                idx = int(session.get("idx") or 0) + 1
            except (TypeError, ValueError):
                idx = 1
            total = len(self._questions(slot))
            step = "уточняющий вопрос" if session.get("step") == "followup" else "вопрос"
            lines.append(
                "Сессия: %s — %s %d из %d" % (self._slot_label(slot), step, idx, total)
            )
        else:
            lines.append("Сессия: сейчас нет")

        entries: list[dict] = []
        try:
            entries = self.storage.entries(today, today)
        except Exception:  # noqa: BLE001
            self.log.exception("status: не удалось прочитать записи")
        lines.append("Записей сегодня: %d" % len(entries))
        if entries:
            counts: dict[str, int] = {}
            for entry in entries:
                category = _text(entry.get("category")) or "other"
                counts[category] = counts.get(category, 0) + 1
            summary = ", ".join(
                "%s %d" % (self._category_label(cat), counts[cat])
                for cat in sorted(counts, key=lambda c: (-counts[c], c))
            )
            lines.append("По категориям: %s" % summary)
            last = entries[-1]
            lines.append(
                "Последняя запись: %s — %s"
                % (self._entry_time(last), _text(last.get("title")) or "без названия")
            )
        try:
            goals = self.storage.goals()
            active = [g for g in goals if _text(g.get("status")) in ("", "active")]
            if goals:
                lines.append("Цели: %d, активных %d (/goals)" % (len(goals), len(active)))
        except Exception:  # noqa: BLE001
            self.log.exception("status: не удалось прочитать цели")
        return "\n".join(lines)

    def today_text(self) -> str:
        now = datetime.now(self.tz)
        today = now.date().isoformat()
        header = "📅 Сегодня, %s" % self._today_human(now.date())
        try:
            entries = self.storage.entries(today, today)
        except Exception:  # noqa: BLE001
            self.log.exception("today: не удалось прочитать записи")
            entries = []
        if not entries:
            return (
                header
                + "\n\nЗаписей пока нет. Бот спросит сам в ближайший слот — "
                "или просто напиши что-нибудь свободным текстом."
            )

        grouped: dict[str, list[dict]] = {}
        for entry in entries:
            slot = _text(entry.get("slot")) or "free"
            grouped.setdefault(slot, []).append(entry)
        order = [slot for slot in _SLOT_ORDER + ("free",) if slot in grouped]
        unknown = sorted(set(grouped) - set(order))
        order.extend(unknown)

        lines = [header, ""]
        for slot in order:
            lines.append(self._slot_title(slot))
            for entry in grouped[slot]:
                lines.append(self._today_entry_line(entry))
            lines.append("")

        totals = self._today_totals(entries)
        if totals:
            lines.append(totals)
        lines.append(
            "Итого: %d %s." % (len(entries), _plural(len(entries),
                                                     ("запись", "записи", "записей")))
        )
        return "\n".join(lines).strip()

    def _today_entry_line(self, entry: dict) -> str:
        """/today: показываем текст записи (~120 символов), а не заголовок.

        Заголовок добавляем только если он не совпадает с началом текста —
        иначе он просто дублировал бы первую строку (UX_REVIEW P1-8).
        """
        title = _text(entry.get("title")).strip()
        full = _text(entry.get("text"))
        category = _text(entry.get("category")).strip()
        suffix = " · %s" % self._category_label(category) if category else ""
        if full.strip() and title and not _same_start(full, title):
            head = title.rstrip(" :—-")
            room = max(40, 120 - len(head) - 2)
            content = "%s: %s" % (head, _clip_text(full, room))
        else:
            content = _clip_text(full, 120) or title or "без текста"
        return "• %s — %s%s" % (self._entry_time(entry), content, suffix)

    def _slot_title(self, slot: str) -> str:
        if slot in _SLOT_TITLES:
            return _SLOT_TITLES[slot]
        return _text(slot)

    def _today_totals(self, entries: list[dict]) -> str:
        parts = []
        water = sum(v for v in (self._metric(entry, "water_ml") for entry in entries) if v)
        if water:
            parts.append("вода %s мл" % _fmt_num(water))
        sleep = self._metric_values(entries, "sleep_h")
        if sleep:
            parts.append("сон %s ч" % _fmt_num(sum(sleep) / len(sleep)))
        moods = self._metric_values(entries, "mood")
        if moods:
            parts.append("настроение %s/5" % _fmt_num(sum(moods) / len(moods)))
        return ("За день: " + ", ".join(parts) + ".") if parts else ""

    def report_text(self) -> str:
        now = datetime.now(self.tz)
        date_to = now.date()
        date_from = date_to - timedelta(days=6)
        header = "📈 Отчёт за 7 дней: %s — %s" % (
            self._short_date(date_from), self._short_date(date_to)
        )
        try:
            entries = self.storage.entries(date_from.isoformat(), date_to.isoformat())
        except Exception:  # noqa: BLE001
            self.log.exception("report: не удалось прочитать записи")
            entries = []
        if not entries:
            return header + "\n\nЗаписей за неделю нет. Начни с ответа на ближайший вопрос или напиши что-нибудь свободным текстом."

        days = {_text(entry.get("date")) for entry in entries}
        lines = [header, ""]
        lines.append("Записей: %d · дней с записями: %d из 7" % (len(entries), len(days)))
        checkin_days = {
            _text(entry.get("date"))
            for entry in entries
            if _text(entry.get("slot")) in _SLOT_ORDER
        }
        lines.append("Чек-инов слотов: %d из 7 дней" % len(checkin_days))

        sleep = self._metric_values(entries, "sleep_h")
        if sleep:
            lines.append("Сон: в среднем %s ч (%d %s)"
                         % (_fmt_num(sum(sleep) / len(sleep)), len(sleep),
                            _plural(len(sleep), ("запись", "записи", "записей"))))
        waters = self._metric_values(entries, "water_ml")
        if waters:
            lines.append("Вода: в среднем %s мл на запись (%d %s)"
                         % (_fmt_num(sum(waters) / len(waters)), len(waters),
                            _plural(len(waters), ("запись", "записи", "записей"))))
        moods = self._metric_values(entries, "mood")
        if moods:
            lines.append("Настроение: %s из 5" % _fmt_num(sum(moods) / len(moods)))
        energies = self._metric_values(entries, "energy")
        if energies:
            lines.append("Энергия: %s из 5" % _fmt_num(sum(energies) / len(energies)))

        training_days = {
            _text(entry.get("date"))
            for entry in entries
            if _text(entry.get("category")) == "training"
        }
        if training_days:
            lines.append("Тренировки: %d дн." % len(training_days))

        done_tasks = 0
        planned_tasks = 0
        for entry in entries:
            done = entry.get("metrics", {}).get("tasks_done")
            planned = entry.get("metrics", {}).get("tasks_planned")
            if isinstance(done, list):
                done_tasks += len(done)
            if isinstance(planned, list):
                planned_tasks += len(planned)
        if done_tasks or planned_tasks:
            lines.append("Задачи: выполнено %d, запланировано %d" % (done_tasks, planned_tasks))

        counts: dict[str, int] = {}
        for entry in entries:
            category = _text(entry.get("category")) or "other"
            counts[category] = counts.get(category, 0) + 1
        top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        if top:
            lines.append("Категории: " + ", ".join(
                "%s %d" % (self._category_label(cat), num) for cat, num in top
            ))

        try:
            goals = self.storage.goals()
        except Exception:  # noqa: BLE001
            goals = []
        if goals:
            active = [g for g in goals if _text(g.get("status")) in ("", "active")]
            lines.append("Цели: %d, активных %d — /goals" % (len(goals), len(active)))
        return "\n".join(lines)

    def goals_text(self) -> str:
        try:
            goals = self.storage.goals()
        except Exception:  # noqa: BLE001
            self.log.exception("goals: не удалось прочитать цели")
            goals = []
        if not goals:
            return (
                "🎯 Цели\n\nЦелей пока нет. Добавить можно так:\n"
                "/add_goal Читать 12 книг | 4/12 | 2027-01-01\n"
                "Формат: Название | сколько уже / сколько всего | срок "
                "(всё, кроме названия, опционально)."
            )
        lines = ["🎯 Цели (%d)" % len(goals), ""]
        for number, goal in enumerate(goals, 1):
            title = _text(goal.get("title")) or "без названия"
            bits = []
            progress = self._goal_progress(goal)
            if progress is not None:
                # Тот же процент, что на дашборде и в «Цели.md» (P0-1).
                bits.append("%d%%" % progress)
            target = _to_float(goal.get("target"))
            current = _to_float(goal.get("current"))
            if target is not None and current is not None and target > 0:
                bits.append("%s из %s" % (_fmt_num(current), _fmt_num(target)))
            else:
                target_text = _text(goal.get("target")).strip()
                if target_text:
                    bits.append("цель %s" % target_text)
            deadline = _text(goal.get("deadline")).strip()
            if deadline:
                bits.append("срок %s" % deadline)
            status = _text(goal.get("status")).strip() or "active"
            bits.append(_STATUS_LABELS.get(status, status))
            lines.append("%d. %s — %s" % (number, title, " · ".join(bits)))
        lines.append("")
        lines.append("Добавить: /add_goal Название | прогресс | срок")
        return "\n".join(lines)

    def habits_text(self) -> str:
        """«/habits» — привычки с серией (P1-6)."""
        try:
            habits = self.storage.habits()
        except Exception:  # noqa: BLE001
            self.log.exception("habits: не удалось прочитать привычки")
            habits = []
        if not habits:
            return (
                "💪 Привычки\n\nПока ни одной. Отмечай так:\n"
                "/habit зарядка — отметить сегодня\n"
                "Потом /habits покажет серию."
            )
        today = datetime.now(self.tz).date().isoformat()
        lines = ["💪 Привычки (%d)" % len(habits), ""]
        for habit in habits:
            name = _text(habit.get("name")).strip() or "без названия"
            try:
                streak = int(habit.get("streak") or 0)
            except (TypeError, ValueError):
                streak = 0
            bits = ["серия %d %s" % (streak, _plural(streak, ("день", "дня", "дней")))]
            last_done = _text(habit.get("last_done")).strip()
            if last_done == today:
                bits.append("сегодня ✅")
            elif last_done:
                bits.append("последний раз %s" % last_done)
            lines.append("• %s — %s" % (name, ", ".join(bits)))
        lines.append("")
        lines.append("Отметить сегодня: /habit название")
        return "\n".join(lines)

    # -------------------------------------------------------- метрики/цели
    def _metric(self, entry: dict, key: str) -> float | None:
        metrics = entry.get("metrics")
        if not isinstance(metrics, dict):
            return None
        return _to_float(metrics.get(key))

    def _metric_values(self, entries: list[dict], key: str) -> list[float]:
        values = []
        for entry in entries:
            value = self._metric(entry, key)
            if value is not None:
                values.append(value)
        return values

    def _goal_progress(self, goal: dict) -> int | None:
        """Прогресс цели 0..100 из единого расчёта :mod:`src.goals_math`.

        Раньше здесь было ``current / target`` без направления и старта:
        «вес 92 → 78» показывал 94%, а дашборд — 0% (UX_REVIEW, P0-1).
        ``None`` — только если у цели ещё нет чисел/процентов.
        """
        percent = goals_math.goal_percent(goal)
        if percent is None:
            return None
        return max(0, min(100, int(percent)))

    def _entry_time(self, entry: dict) -> str:
        ts = _text(entry.get("ts"))
        if ts:
            try:
                parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return parsed.strftime("%H:%M")
            except ValueError:
                if len(ts) >= 16:
                    return ts[11:16]
        return "--:--"


def _is_ok(text: str) -> bool:
    """True, если ответ LLM — сигнал «уточнение не нужно» (OK/ОК)."""
    return text.strip().strip(".!").upper() in {"OK", "ОК", "ОКЕЙ", "ХОРОШО"}
