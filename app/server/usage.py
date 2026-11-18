"""События использования Life OS: веб-экраны и Telegram-бот в одной таблице.

Зачем: видеть, какими функциями реально пользуются, где провалы, в какие часы
человек живёт в приложении, и какие темы он приносит в журнал. Считаются все
каналы:

* **веб** — успешный ``GET /api/...`` от SPA; путь проецируется на экран
  (``/api/tasks`` → «Сегодня»), повторные запросы одного экрана в течение
  минуты схлопываются, чтобы один экран не давал 5 событий;
* **бот** — каждое сообщение пишется из :mod:`app.bot.handlers` с
  ``source='bot'``, экран = имя интента/команды (``task``, ``journal``,
  ``/today``, ``voice``); это ровно те же строки, что у веба, отдельной схемы
  для бота нет.

Таблица ``usage_events`` создаётся идемпотентно в
:func:`app.server.db.ensure_usage_events`; ``meta_json`` хранит детали
(интент, тип медиа), но анализ опирается только на ``ts/source/screen/action``.

Блок «Мысли» — темы из журнала (``entries.kind = journal/note/thought``):
частые темы, рост относительно предыдущего окна и темы, впервые появившиеся
в текущем окне. Темы берутся из ``meta.topics`` бота и из значимых слов
текста (стоп-слова и грубая нормализация окончаний — без внешних NLP-библиотек).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

from app.server.db import connect

__all__ = [
    "BOT_COMMANDS",
    "BOT_SCREENS",
    "SOURCES",
    "THOUGHT_KINDS",
    "WEB_SCREENS",
    "collect_thoughts",
    "collect_usage",
    "feature_catalog",
    "label_for",
    "record_event",
    "record_web_request",
    "reset_web_dedupe",
    "screen_for_path",
]

logger = logging.getLogger("lifeos.usage")

SOURCES: tuple[str, ...] = ("web", "bot")

#: Экран веба → подпись. Ключи совпадают с тем, что отдаёт ``screen_for_path``.
WEB_SCREENS: dict[str, str] = {
    "overview": "Обзор",
    "today": "Сегодня",
    "week": "Неделя",
    "compass": "Компас",
    "projects": "Проекты",
    "inbox": "Инбокс",
    "library": "Библиотека",
    "finance": "Финансы",
    "nutrition": "Питание",
    "personal": "Личное",
    "analytics": "Аналитика",
    "review": "Итоги",
    "habits": "Привычки",
    "journal": "Журнал",
    "health": "Здоровье",
    "sections": "Разделы",
    "search": "Поиск",
    "settings": "Настройки",
    "courses": "Курсы",
}

#: Интенты и типы сообщений бота → подпись (ключ = ``screen`` в событии).
BOT_SCREENS: dict[str, str] = {
    "task": "Дела",
    "task_done": "Закрыть дело",
    "reschedule": "Перенос дела",
    "move_work": "В работу",
    "move_personal": "В личное",
    "expense": "Расход",
    "income": "Доход",
    "finance_summary": "Деньги",
    "goal_progress": "Прогресс цели",
    "goal_step_done": "Шаг цели",
    "book_progress": "Книга",
    "book_quote": "Цитата",
    "book_insight": "Выжимка",
    "journal": "Мысль",
    "habit": "Привычка",
    "checkin": "Чек-ин",
    "delete_last": "Удаление",
    "today": "Сводка дня",
    "week": "Итоги недели",
    "mode_hint": "Подсказка",
    "greeting": "Приветствие",
    "inbox": "Инбокс",
    "legacy": "Старый разбор",
    "unknown": "Не распознано",
    "food": "Еда",
    "weight": "Вес",
    "workout": "Тренировка",
    "note": "Заметка",
    "voice": "Голос",
    "media": "Медиа",
}

#: Команды бота → подпись.
BOT_COMMANDS: dict[str, str] = {
    "/start": "Старт",
    "/help": "Помощь",
    "/stats": "Статистика",
    "/today": "Сводка дня",
    "/week": "Итоги недели",
}

#: Префиксы API → экран. Порядок важен: длинные префиксы раньше коротких.
_API_SCREEN_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/api/analytics/usage", ""),  # сама статистика не должна себя считать
    ("/api/analytics", "analytics"),
    ("/api/health/weight", "health"),
    ("/api/health/composition", "health"),
    ("/api/nutrition", "nutrition"),
    ("/api/finance", "finance"),
    ("/api/books", "library"),
    ("/api/book-", "library"),
    ("/api/compass", "compass"),
    ("/api/goals", "compass"),
    ("/api/goal-steps", "compass"),
    ("/api/anti-goals", "compass"),
    ("/api/personal/review", "review"),
    ("/api/personal", "personal"),
    ("/api/journal", "journal"),
    ("/api/tasks", "today"),
    ("/api/inbox", "inbox"),
    ("/api/projects", "projects"),
    ("/api/project-", "projects"),
    ("/api/habits", "habits"),
    ("/api/health", "health"),
    ("/api/sections", "sections"),
    ("/api/courses", "courses"),
    ("/api/learning", "courses"),
    ("/api/lessons", "courses"),
    ("/api/search", "search"),
    ("/api/settings", "settings"),
    ("/api/export", "settings"),
    ("/api/overview", "overview"),
    ("/api/ws", ""),
)

#: Сколько секунд один и тот же веб-экран не считается повторно.
WEB_DEDUPE_SECONDS = 60.0

_web_last: dict[str, float] = {}
_web_lock = threading.Lock()

#: Виды записей журнала, которые считаем «мыслями».
THOUGHT_KINDS: tuple[str, ...] = ("journal", "note", "thought", "idea", "мысль")

#: Русские и английские стоп-слова: темы не должны состоять из служебных слов.
STOPWORDS: frozenset[str] = frozenset(
    """
    а без более больше будто бы был была были было быть в вам вами вас ведь весь
    во вон вот все всего всей всем всеми всех вся всю вы где да даже два для до
    другой его ее ей ему если есть еще ею же за зачем здесь и из или им ими их к
    как какая какие каким какими каких какой какою какую каков который которая
    которое которые которым которые которых который которые кто куда ли либо мне
    много может можно мой моя мою мои мы на над надо наконец нам нами нас наш
    наша наше наши не него нее ней нельзя нем нему нет ни нибудь никогда ним ними
    них ничего но ну об один одна одно они оно опять от перед по под после потом
    потому почти при про раз разве с сам сама сами само свое свой своя свою свои
    своим своих сделал сделала сделали сейчас со совсем так такая такие таким
    такими таких такой такое таком такую там те тебе тебя тем теми тех то тогда
    того тоже той только том тому тот тою ту тут ты у уж уже чего чем через что
    чтоб чтобы чуть эта эти этим этими этих это этого этой этом этому этот эту я
    the and you that was for are with his they this have from one had word but not
    what all were when your can said there use each which she how their will would
    about if then them write like so these her long make thing see him two has
    look more day could go come did number sound no most people my over know water
    than call first who may down side been now find any new work part take get
    place made live where after back little only round man year came show every
    good me give our under name very just form sentence great think say help low
    line differ turn cause much mean before move right boy old too same tell does
    set three want air well also play small end put home read hand port large spell
    add even land here must big high such follow act why ask men change went light
    kind off need house picture try us again animal point mother world near build
    self earth father
    утро утром вечер вечером день днем ночь ночью сегодня вчера завтра
    неделя неделю месяцем месяцы год году годы следующий следующая следующее
    следующие прошлый прошлая прошлое прошлые новый новая новое новые
    каждый каждая каждое каждые каждую новый новый
    очень просто быстро медленно хорошо плохо важно нужно можно нельзя
    понятно спокойно отлично нормально обычно сразу часто редко легко сложно
    интересно давно недавно теперь тогда снова опять почти совсем далее потом
    сначала наконец проще лучше хуже дальше ближе больше меньше
    тысяч тысяча тысячи шагов шаг минут минута минуты часов час часа
    километр километров грамм граммов литр литров процент процентов
    стало стал стала стали стать станет становится становилось
    дошел дошла дошли идти шел шла шли
    месяц месяца месяце месяцев неделе недели недель
    темно светло вести жить жил жила жили отвез отвезла отвезли подряд долго
    вперед пришлось приходилось получилось оказалось удалось захотелось
    хотел хотела хотели хотеть вместо одну одну правильно верно точно
    рядом около кроме среди внутри прямо наверное кажется
    читал читала читали читать читаю читает обсудил обсудила обсудили обсуждали
    сделал сделала сделали делать пришел пришла пришли пошел пошла пошли
    решил решила решили решать купил купила купили покупать
    приготовил приготовила приготовили готовить проверил проверила проверили проверять
    договорился договорилась договорились записал записала записали записывать
    разобрал разобрала разобрали разбирать прибрал прибрала прибрали прибирать
    болел болела болели болеть дошел дошла дошли доходить
    начал начала начали начинать продолжил продолжила продолжили продолжать
    закрыл закрыла закрыли закрывать перенес перенесла перенесли переносить
    позвонил позвонила позвонили звонить поговорил поговорила поговорили говорить
    помог помогла помогли помогать сказал сказала сказали сказать
    увидел увидела увидели видеть заметил заметила заметили замечать
    успел успела успели успевать устал устала устали уставать
    отдохнул отдохнула отдохнули отдыхать потратил потратила потратили тратить
    получил получила получили получать отправил отправила отправили отправлять
    """.split()
)

# 2. Отсев «служебных» форм: глаголы, прилагательные, причастия.
_SERVICE_SUFFIXES: tuple[str, ...] = (
    "ться", "тся", "лся", "лась", "лись", "сь",
    "ировал", "ировала", "ировали", "овать", "евать",
    "ешь", "ишь", "ите", "ете", "ует", "юет",
    "ал", "ала", "али", "ял", "яла", "яли",
    "ил", "ила", "или", "ел", "ела", "ели",
    "ла", "ло", "ли", "л",
    "ет", "ут", "ют", "ат", "ят", "им", "ем",
    "ый", "ий", "ая", "ое", "ые", "ого", "ому", "ым", "ыми",
    "ей", "ее", "ше", "ую", "юю", "его", "ему", "ых", "их", "ой", "ою",
    "ть", "ти", "чь",
)

#: Для односимвольных окончаний основа должна быть длиннее, иначе пострадают
#: короткие слова вроде «дача»/«баня»: их оставляем как есть.
_SHORT_SUFFIX_MIN_BASE = 4


def _looks_service(word: str) -> bool:
    """Похоже на глагол/прилагательное: такие слова не темы, а каркас фразы."""
    for suffix in _SERVICE_SUFFIXES:
        minimum = _SHORT_SUFFIX_MIN_BASE if len(suffix) == 1 else 4
        if word.endswith(suffix) and len(word) - len(suffix) >= minimum:
            return True
    return False


_WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-]{2,}")
_MAX_TOPICS_PER_ENTRY = 8
_MAX_TOPICS = 12
_MAX_RISING = 6
_MAX_NEW = 6
_GROWTH_MIN = 1.3

#: Подписи частых тем: каноническая основа → человеческое название. Темы вне
#: словаря показываются как есть (первая буква заглавная), поэтому новые темы
#: не теряются.
TOPIC_LABELS: dict[str, str] = {
    "работ": "Работа",
    "проект": "Проекты",
    "созвон": "Созвоны",
    "звонк": "Звонки",
    "встреч": "Встречи",
    "спорт": "Спорт",
    "тренировк": "Тренировки",
    "зарядк": "Зарядка",
    "прогулк": "Прогулки",
    "вес": "Вес",
    "питани": "Питание",
    "еда": "Еда",
    "курс": "Курсы",
    "нутрициологи": "Нутрициология",
    "книг": "Книги",
    "чтен": "Чтение",
    "почт": "Почта",
    "ремонт": "Ремонт",
    "дач": "Дача",
    "родител": "Родители",
    "семь": "Семья",
    "коллег": "Коллеги",
    "друз": "Друзья",
    "деньг": "Деньги",
    "расход": "Расходы",
    "бюджет": "Бюджет",
    "покупк": "Покупки",
    "магазин": "Покупки",
    "здоров": "Здоровье",
    "врач": "Врач",
    "спин": "Спина",
    "колен": "Колени",
    "сон": "Сон",
    "энерг": "Энергия",
    "настроени": "Настроение",
    "устал": "Усталость",
    "отдых": "Отдых",
    "план": "Планы",
    "заметк": "Заметки",
    "переезд": "Переезд",
    "шкаф": "Шкаф",
    "стол": "Стол",
    "бан": "Баня",
    "ламп": "Лампа",
    "вещ": "Вещи",
    "разбор": "Разбор вещей",
    "дефицит": "Дефицит",
    "диет": "Диеты",
    "телефон": "Телефон",
    "сном": "Сон",
    "сна": "Сон",
    "сне": "Сон",
    "жен": "Жена",
    "мест": "Место",
}

#: Окончания для грубой нормализации: длинные раньше коротких.
_SUFFIXES: tuple[str, ...] = (
    "иями", "ями", "ами", "ыми", "ими", "ого", "его", "ому", "ему", "ией",
    "ая", "ое", "ые", "ей", "ов", "ев", "ий", "ый", "ам", "ям", "ах", "ях",
    "ом", "ем", "ой", "ую", "юю", "ия", "ие", "ию", "ии", "ть", "ся", "сь",
    "у", "ю", "ы", "и", "а", "я", "о", "е", "ь",
)


# --------------------------------------------------------------------------
# Запись событий
# --------------------------------------------------------------------------


def screen_for_path(path: str) -> str | None:
    """Экран веба по пути API; ``None`` — путь не считаем (health, usage)."""
    clean, _, query = str(path or "").partition("?")
    if clean.rstrip("/") == "/api/health":
        return None  # health-check не является экраном
    if clean.rstrip("/") == "/api/tasks" and (
        "view=week" in query or "from=" in query or "to=" in query
    ):
        return "week"
    for prefix, screen in _API_SCREEN_PREFIXES:
        if clean.startswith(prefix):
            return screen or None
    return None


def label_for(source: str, screen: str) -> str:
    """Подпись экрана: веб/бот/команда; неизвестное — как есть."""
    key = str(screen or "")
    if source == "web":
        return WEB_SCREENS.get(key, key)
    if key.startswith("/"):
        return BOT_COMMANDS.get(key, key)
    return BOT_SCREENS.get(key, key)


def feature_catalog() -> list[dict[str, str]]:
    """Все функции продукта, по которым считается покрытие."""
    items: list[dict[str, str]] = []
    items.extend(
        {"key": key, "label": label, "source": "web"} for key, label in WEB_SCREENS.items()
    )
    items.extend(
        {"key": key, "label": label, "source": "bot"} for key, label in BOT_SCREENS.items()
    )
    items.extend(
        {"key": key, "label": label, "source": "bot"} for key, label in BOT_COMMANDS.items()
    )
    return items


def record_event(
    conn: Any,
    *,
    source: str,
    screen: str,
    action: str = "view",
    ts: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> int | None:
    """Пишет событие в ``usage_events``; никогда не роняет продукт.

    Ошибка статистики не должна ломать запись мысли или экран: любые проблемы
    логируются и возвращают ``None``.
    """
    source_text = str(source or "").strip().lower()
    screen_text = str(screen or "").strip()[:120]
    if source_text not in SOURCES or not screen_text:
        return None
    action_text = str(action or "view").strip().lower()[:40] or "view"
    stamp = str(ts or "").strip()
    if not stamp:
        return None
    payload = "{}"
    if meta:
        try:
            payload = json.dumps(dict(meta), ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            payload = "{}"
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO usage_events (ts, source, screen, action, meta_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (stamp, source_text, screen_text, action_text, payload),
            )
        return int(cursor.lastrowid or 0)
    except Exception:  # pragma: no cover - зависит от состояния БД
        logger.exception("не удалось записать событие использования")
        return None


def reset_web_dedupe() -> None:
    """Сбрасывает окно дедупликации веба (нужно тестам)."""
    with _web_lock:
        _web_last.clear()


def record_web_request(settings: Any, path: str) -> int | None:
    """Событие веб-экрана по пути API с дедупликацией ``WEB_DEDUPE_SECONDS``."""
    screen = screen_for_path(path)
    if not screen:
        return None
    now = time.monotonic()
    with _web_lock:
        previous = _web_last.get(screen)
        if previous is not None and now - previous < WEB_DEDUPE_SECONDS:
            return None
        _web_last[screen] = now
    try:
        conn = connect(settings.db_path)
    except Exception:  # pragma: no cover
        logger.exception("не удалось открыть БД для события использования")
        return None
    try:
        return record_event(
            conn,
            source="web",
            screen=screen,
            action="view",
            ts=settings.now().isoformat(timespec="seconds"),
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Мысли: темы журнала
# --------------------------------------------------------------------------


def _norm(word: str) -> str:
    return str(word or "").strip().lower().replace("ё", "е")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        minimum = _SHORT_SUFFIX_MIN_BASE if len(suffix) == 1 else 4
        if len(word) - len(suffix) >= minimum and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _display(word: str) -> str:
    text = str(word or "").strip()
    return text[:1].upper() + text[1:] if text else text


def _canonical_topic(word: str) -> str:
    normal = _norm(word)
    return normal if " " in normal else _stem(normal)


def _topics_from_entry(text: str, meta_json: str | None) -> dict[str, str]:
    """Канонические темы записи → поверхностная форма для показа."""
    topics: dict[str, str] = {}
    meta: Mapping[str, Any] = {}
    if meta_json:
        try:
            loaded = json.loads(meta_json)
            if isinstance(loaded, Mapping):
                meta = loaded
        except (TypeError, ValueError):
            meta = {}
    raw_topics = meta.get("topics")
    if isinstance(raw_topics, list):
        for raw in raw_topics[:_MAX_TOPICS_PER_ENTRY]:
            surface = str(raw or "").strip()
            if len(surface) < 3:
                continue
            canonical = _canonical_topic(surface)
            if len(canonical) >= 3 and canonical not in STOPWORDS:
                topics.setdefault(canonical, surface)
    extracted = 0
    for word in _WORD_RE.findall(str(text or "")):
        if extracted >= _MAX_TOPICS_PER_ENTRY:
            break
        normal = _norm(word)
        if len(normal) < 4 or normal in STOPWORDS or _looks_service(normal):
            continue
        canonical = _stem(normal)
        if len(canonical) < 3 or canonical in STOPWORDS:
            continue
        if canonical not in topics:
            topics[canonical] = normal
            extracted += 1
    return topics


def _clock(ts: Any) -> datetime | None:
    text = str(ts or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


def collect_thoughts(conn: Any, today: date, days: int = 90) -> dict[str, Any]:
    """Темы журнала: частые, растущие и впервые появившиеся за окно."""
    days = max(1, int(days))
    start = today - timedelta(days=days - 1)
    prev_start = start - timedelta(days=days)
    prev_end = start - timedelta(days=1)
    start_text, today_text = start.isoformat(), today.isoformat()
    prev_start_text, prev_end_text = prev_start.isoformat(), prev_end.isoformat()

    placeholders = ", ".join("?" for _ in THOUGHT_KINDS)
    rows = conn.execute(
        "SELECT ts, text, meta_json FROM entries "
        "WHERE kind IN (%s) AND substr(ts, 1, 10) BETWEEN ? AND ?"
        % placeholders,
        (*THOUGHT_KINDS, prev_start_text, today_text),
    ).fetchall()

    current: Counter[str] = Counter()
    previous: Counter[str] = Counter()
    surface: dict[str, Counter[str]] = defaultdict(Counter)
    first_seen: dict[str, str] = {}
    last_seen: dict[str, str] = {}
    entries_current = 0
    entries_previous = 0
    for row in rows:
        moment = str(row["ts"] or "")
        day = moment[:10]
        if day >= start_text:
            entries_current += 1
        elif prev_start_text <= day <= prev_end_text:
            entries_previous += 1
        else:
            continue
        topics = _topics_from_entry(str(row["text"] or ""), row["meta_json"])
        if not topics:
            continue
        if day >= start_text:
            current.update(topics.keys())
        else:
            previous.update(topics.keys())
        for canonical, shown in topics.items():
            surface[canonical][shown] += 1
            if canonical not in first_seen or day < first_seen[canonical]:
                first_seen[canonical] = day
            if canonical not in last_seen or day > last_seen[canonical]:
                last_seen[canonical] = day

    topics: list[dict[str, Any]] = []
    for canonical, count in current.items():
        prev_count = int(previous.get(canonical, 0))
        growth = (
            round((count - prev_count) / prev_count * 100.0, 1) if prev_count else None
        )
        label = TOPIC_LABELS.get(
            canonical, _display(surface[canonical].most_common(1)[0][0]) if surface[canonical] else canonical
        )
        topics.append(
            {
                "topic": canonical,
                "label": label,
                "count": int(count),
                "prevCount": prev_count,
                "growthPct": growth,
                "firstSeen": first_seen.get(canonical),
                "lastSeen": last_seen.get(canonical),
                "isNew": prev_count == 0,
            }
        )
    topics.sort(key=lambda item: (-item["count"], -item["prevCount"], item["label"].lower()))

    rising = [
        item
        for item in topics
        if item["prevCount"] >= 1 and item["count"] >= 2 and item["count"] >= item["prevCount"] * _GROWTH_MIN
    ]
    rising.sort(key=lambda item: (-(item["growthPct"] or 0), -item["count"]))
    fresh = [item for item in topics if item["prevCount"] == 0 and item["count"] >= 2]
    fresh.sort(key=lambda item: (-item["count"], item["firstSeen"] or ""))

    note = None
    if entries_current == 0:
        note = (
            "Пока нет мыслей за окно: их пишут ботом («мысль …») "
            "или в журнале — темы появятся сами."
        )
    elif not topics:
        note = "Мысли есть, но темы не выделились: добавьте пару слов по существу."

    return {
        "window": {
            "from": start_text,
            "to": today_text,
            "days": days,
            "prevFrom": prev_start_text,
            "prevTo": prev_end_text,
        },
        "entries": entries_current,
        "prevEntries": entries_previous,
        "topics": topics[:_MAX_TOPICS],
        "rising": rising[:_MAX_RISING],
        "new": fresh[:_MAX_NEW],
        "note": note,
    }


# --------------------------------------------------------------------------
# Сводка использования
# --------------------------------------------------------------------------


def _share(count: int, total: int) -> float:
    return round(count / total, 3) if total else 0.0


def _dense_days(start: date, days: int) -> list[str]:
    return [(start + timedelta(days=offset)).isoformat() for offset in range(days)]


def collect_usage(
    conn: Any,
    today: date,
    days: int = 30,
    *,
    thoughts_days: int = 90,
) -> dict[str, Any]:
    """Полная статистика использования: каналы, экраны, покрытие, карта, мысли."""
    days = max(1, int(days))
    start = today - timedelta(days=days - 1)
    prev_start = start - timedelta(days=days)
    prev_end = start - timedelta(days=1)
    start_text, today_text = start.isoformat(), today.isoformat()
    prev_start_text, prev_end_text = prev_start.isoformat(), prev_end.isoformat()

    rows = conn.execute(
        "SELECT ts, source, screen, action FROM usage_events "
        "WHERE substr(ts, 1, 10) BETWEEN ? AND ? ORDER BY ts, id",
        (start_text, today_text),
    ).fetchall()
    previous_row = conn.execute(
        "SELECT COUNT(*) AS total, COUNT(DISTINCT substr(ts, 1, 10)) AS active "
        "FROM usage_events WHERE substr(ts, 1, 10) BETWEEN ? AND ?",
        (prev_start_text, prev_end_text),
    ).fetchone()
    previous_events = int(previous_row["total"] or 0) if previous_row else 0
    previous_active = int(previous_row["active"] or 0) if previous_row else 0

    total = len(rows)
    web_count = sum(1 for row in rows if str(row["source"]) == "web")
    bot_count = total - web_count
    actions = Counter(str(row["action"] or "view") for row in rows)

    active_days: set[str] = set()
    web_active: set[str] = set()
    bot_active: set[str] = set()
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    heatmap = [[0 for _ in range(24)] for _ in range(7)]
    by_day: Counter[str] = Counter()
    weekdays = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

    for row in rows:
        source = str(row["source"])
        screen = str(row["screen"])
        action = str(row["action"] or "view")
        day = str(row["ts"] or "")[:10]
        active_days.add(day)
        (web_active if source == "web" else bot_active).add(day)
        by_day[day] += 1
        moment = _clock(row["ts"])
        if moment is not None:
            heatmap[moment.weekday()][moment.hour] += 1
        bucket = grouped.setdefault(
            (source, screen),
            {"source": source, "screen": screen, "count": 0, "actions": Counter(), "last": ""},
        )
        bucket["count"] += 1
        bucket["actions"][action] += 1
        if day > bucket["last"]:
            bucket["last"] = day

    sources = [
        {
            "key": "web",
            "label": "Веб",
            "count": web_count,
            "share": _share(web_count, total),
            "activeDays": len(web_active),
        },
        {
            "key": "bot",
            "label": "Бот",
            "count": bot_count,
            "share": _share(bot_count, total),
            "activeDays": len(bot_active),
        },
    ]

    screens = [
        {
            "key": bucket["screen"],
            "label": label_for(bucket["source"], bucket["screen"]),
            "source": bucket["source"],
            "count": bucket["count"],
            "share": _share(bucket["count"], total),
            "lastUsed": bucket["last"] or None,
            "actions": dict(sorted(bucket["actions"].items())),
        }
        for bucket in grouped.values()
    ]
    screens.sort(key=lambda item: (-item["count"], item["source"], item["key"]))

    catalog = feature_catalog()
    used_keys = {(item["source"], item["key"]) for item in screens}
    used_catalog = [item for item in catalog if (item["source"], item["key"]) in used_keys]
    unused_catalog = [item for item in catalog if (item["source"], item["key"]) not in used_keys]
    web_total = len(WEB_SCREENS)
    bot_total = len(BOT_SCREENS) + len(BOT_COMMANDS)
    web_used = sum(1 for item in used_catalog if item["source"] == "web")
    bot_used = sum(1 for item in used_catalog if item["source"] == "bot")
    coverage = {
        "used": len(used_catalog),
        "total": len(catalog),
        "pct": round(len(used_catalog) / len(catalog) * 100.0, 1) if catalog else 0.0,
        "web": {
            "used": web_used,
            "total": web_total,
            "pct": round(web_used / web_total * 100.0, 1) if web_total else 0.0,
        },
        "bot": {
            "used": bot_used,
            "total": bot_total,
            "pct": round(bot_used / bot_total * 100.0, 1) if bot_total else 0.0,
        },
        "unused": unused_catalog,
    }

    peak = None
    for weekday in range(7):
        for hour in range(24):
            value = heatmap[weekday][hour]
            if value and (peak is None or value > peak["count"]):
                peak = {"weekday": weekday, "hour": hour, "count": value}
    if peak is not None:
        peak["weekdayLabel"] = weekdays[peak["weekday"]]

    by_day_items = [
        {"date": day, "count": by_day.get(day, 0)} for day in _dense_days(start, days)
    ]

    change_pct = (
        round((total - previous_events) / previous_events * 100.0, 1)
        if previous_events
        else None
    )

    return {
        "window": {"from": start_text, "to": today_text, "days": days},
        "totals": {
            "events": total,
            "activeDays": len(active_days),
            "days": days,
            "web": web_count,
            "bot": bot_count,
            "messages": actions.get("message", 0),
            "commands": actions.get("command", 0),
            "voice": actions.get("voice", 0),
            "media": actions.get("media", 0),
        },
        "sources": sources,
        "screens": screens,
        "coverage": coverage,
        "heatmap": {
            "weekdays": list(weekdays),
            "hours": list(range(24)),
            "matrix": heatmap,
            "byDay": by_day_items,
            "peak": peak,
        },
        "trend": {
            "previousEvents": previous_events,
            "changePct": change_pct,
            "previousActiveDays": previous_active,
            "activeDaysChange": len(active_days) - previous_active,
        },
        "thoughts": collect_thoughts(conn, today, thoughts_days),
    }
