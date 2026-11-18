"""Интент-роутер Life OS v4: сообщение с телефона -> одна запись или ответ.

Бот — главный вход (§1 ``studio/BOT-MAIN.md``): человек пишет как в переписке,
бот раскладывает фразу в сущность v4 — дело, еду, деньги, книгу, мысль,
привычку, чек-ин, правку последней записи или ответ из БД. Одно сообщение —
ровно одно намерение.

Порядок важен (см. :meth:`RulesV4.parse`):

* **еда раньше денег**: «на обед гречка 200 г и курица 150» — приём пищи,
  а не расход; «гречка 200 г» не сумма;
* **вопросы не создают записей**: «какие задачи на завтра», «сколько энергии
  вчера» — ответ из БД; непонятный вопрос — в инбокс;
* **ничего не теряется**: свободная мысль — в журнал, абсурд/обрывок — в
  инбокс с исходным текстом; одиночное слово — короткий уточняющий вопрос;
* числа понимаются цифрами и словами («тысячу двести», «семь с половиной»),
  «3 20» — это 320; даты — «завтра», «15 числа», «в следующий вторник»,
  опечатки «звтра пазванить» чинятся словарём.

Два движка, как и в v3 (:mod:`app.bot.intent`): :class:`V4Router` сначала
спрашивает LLM (OpenAI-совместимый шлюз), но только при наличии ключа; любой
сбой молча уступает правилам :class:`RulesV4`. Модуль ничего не пишет в БД и
не ходит в сеть, пока не вызван реальный шлюз с непустым ключом.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "CATEGORY_KEYWORDS",
    "DEFAULT_SPHERES",
    "INCOME_KEYWORDS",
    "INTENTS",
    "KEYBOARD_BUTTONS",
    "RulesV4",
    "SPHERE_KEYWORDS",
    "V4Router",
    "WRITE_INTENTS",
    "build_router",
    "format_date_short",
    "format_human_day",
    "format_money",
    "infer_account",
    "infer_category",
    "infer_mood",
    "infer_sphere",
    "infer_topics",
    "normalize_text",
    "parse",
    "parse_amount",
    "parse_date_phrase",
    "parse_food",
    "parse_russian_number",
    "validate",
]

#: Все намерения, которые понимает бот. ``legacy`` — разбор v3 (замеры,
#: тренировки): старые фразы сохраняются без изменений.
INTENTS = frozenset(
    {
        "task",
        "task_done",
        "goal_progress",
        "goal_step_done",
        "create_goal",
        "expense",
        "income",
        "finance_summary",
        "query",
        "book_progress",
        "book_quote",
        "book_insight",
        "create_book",
        "journal",
        "habit",
        "practice",
        "food",
        "checkin",
        "reschedule",
        "move_work",
        "move_personal",
        "delete_last",
        "today",
        "week",
        "clarify",
        "inbox",
        "legacy",
        "unknown",
    }
)

#: Намерения, после которых в БД появляется запись (и кнопка «Удалить»).
WRITE_INTENTS = frozenset(
    {
        "task",
        "task_done",
        "goal_progress",
        "goal_step_done",
        "create_goal",
        "expense",
        "income",
        "book_progress",
        "book_quote",
        "book_insight",
        "create_book",
        "journal",
        "habit",
        "practice",
        "food",
        "checkin",
        "reschedule",
        "move_work",
        "move_personal",
    }
)

#: Постоянная reply-клавиатура (§3 BOT-MAIN). Порядок важен.
KEYBOARD_BUTTONS: Tuple[str, ...] = (
    "Дело",
    "Расход",
    "Еда",
    "Привычка",
    "Мысль",
    "Сводка",
)

#: Кнопка клавиатуры -> (вид записи, подсказка человеку).
MODE_HINTS: Mapping[str, Tuple[str, str]] = {
    "дело": ("task", "Напиши дело: «завтра позвонить в банк», «в пятницу стоматолог в 15»."),
    "расход": ("expense", "Напиши расход: «такси 450», «кофе 320 наличными», «зарплата 120000»."),
    "еда": ("food", "Напиши, что съел: «на обед гречка 200 г и курица 150» или пришли фото."),
    "привычка": ("habit", "Напиши привычку: «выпил 2 л», «пробежал 5 км», «не курил»."),
    "мысль": ("journal", "Пришли мысль — запишу в журнал."),
    "сводка": ("today", ""),
}

#: Сферы жизни по умолчанию (§10.1 HANDOFF); бот создаёт недостающую.
DEFAULT_SPHERES: Tuple[Tuple[str, str, str], ...] = (
    ("Работа и проекты", "#8e76a8", "briefcase"),
    ("Обучение и развитие", "#7e98bc", "book"),
    ("Личное и семья", "#c98f74", "user"),
    ("Здоровье и ресурс", "#7fa890", "heart"),
    ("Финансы и быт", "#b08e5e", "wallet"),
)

#: Канонические сферы -> слова-маркеры в тексте (первое совпадение выигрывает).
SPHERE_KEYWORDS: Mapping[str, Tuple[str, ...]] = {
    "Здоровье и ресурс": (
        "здоров",
        "трениров",
        "спорт",
        "врач",
        "стоматолог",
        "аптек",
        "сон",
        "спал",
        "вес",
        "бег",
        "пробеж",
        "прогул",
        "тело",
        "энерг",
        "растяж",
        "бассейн",
        "фитнес",
        "йог",
        "массаж",
    ),
    "Финансы и быт": (
        "деньг",
        "банк",
        "оплат",
        "налог",
        "финанс",
        "счёт",
        "счет",
        "кредит",
        "ипотек",
        "бюджет",
        "квитанц",
        "квартплат",
        "квартир",
        "жкх",
        "такси",
        "зарплат",
        "расход",
        "доход",
        "перевод",
    ),
    "Обучение и развитие": (
        "книг",
        "учеб",
        "курс",
        "читать",
        "читаю",
        "читал",
        "лекци",
        "семинар",
        "англ",
        "язык",
        "науч",
        "вебинар",
        "экзамен",
    ),
    "Работа и проекты": (
        "работ",
        "созвон",
        "клиент",
        "офис",
        "проект",
        "дедлайн",
        "отчет",
        "отчёт",
        "встреч",
        "коллег",
        "босс",
        "задач",
        "презентац",
        "планерк",
        "совещан",
    ),
    "Личное и семья": (
        "дом",
        "семь",
        "жена",
        "муж",
        "реб",
        "друг",
        "родител",
        "покуп",
        "уборк",
        "ремонт",
        "быт",
        "мам",
        "пап",
        "отпуск",
        "подарок",
    ),
}

#: Статьи расходов -> слова-маркеры (канонические имена как в сидере §12.4).
CATEGORY_KEYWORDS: Mapping[str, Tuple[str, ...]] = {
    "Транспорт": (
        "такси",
        "метро",
        "автобус",
        "бензин",
        "заправк",
        "парков",
        "электричк",
        "билет",
        "проезд",
        "каршеринг",
        "самокат",
    ),
    "Кафе и рестораны": (
        "кофе",
        "кафе",
        "ресторан",
        "обед",
        "ужин",
        "завтрак",
        "пицц",
        "шаурм",
        "доставк",
        "столов",
        "бар",
        "десерт",
        "морожен",
        "бургер",
        "суши",
        "роллы",
    ),
    "Еда и продукты": (
        "продукт",
        "магазин",
        "пятёрочк",
        "пятерочк",
        "вкусвилл",
        "рынок",
        "супермаркет",
        "азбук",
        "лента",
        "перекресток",
        "перекрёсток",
        "гипермаркет",
    ),
    "Здоровье": (
        "аптек",
        "врач",
        "лекарств",
        "стоматолог",
        "клиник",
        "анализ",
        "массаж",
        "бад",
        "витамин",
    ),
    "Дом и быт": (
        "ремонт",
        "мебел",
        "быт",
        "химия",
        "квартплат",
        "квартир",
        "жкх",
        "коммунал",
        "уборк",
        "посуд",
        "лампа",
        "кровать",
        "стройматериал",
    ),
    "Развлечения": (
        "кино",
        "театр",
        "концерт",
        "подписк",
        "игр",
        "netflix",
        "спотифай",
        "развлеч",
        "музей",
        "выставк",
    ),
    "Одежда": (
        "одежд",
        "кроссовк",
        "обув",
        "куртк",
        "футболк",
        "джинс",
        "шапк",
    ),
}

#: Статьи доходов -> слова-маркеры.
INCOME_KEYWORDS: Mapping[str, Tuple[str, ...]] = {
    "Зарплата": ("зарплат", "аванс", "оклад", "получк"),
    "Фриланс": ("фриланс", "гонорар", "заказ", "подработ", "премия"),
    "Проценты по вкладу": ("процент", "вклад", "дивиденд", "кэшбэк", "кешбэк"),
}

#: Слова-маркеры общего дохода без конкретной статьи.
INCOME_GENERIC_WORDS: Tuple[str, ...] = (
    "получил",
    "получила",
    "заработал",
    "заработала",
    "пришло",
    "пришли",
    "перевели",
    "вернули",
    "начислили",
    "доход",
)

#: Слова-маркеры траты (в прошедшем времени — это уже транзакция).
SPEND_WORDS: Tuple[str, ...] = (
    "потратил",
    "потратила",
    "потрачено",
    "заплатил",
    "заплатила",
    "оплатил",
    "оплатила",
    "купил",
    "купила",
    "списали",
    "обошлось",
    "отдал",
    "отдала",
)

#: Инфинитивы, которые чаще означают план (дело), а не случившуюся трату.
TASK_VERBS: Tuple[str, ...] = (
    "позвонить",
    "позвони",
    "купить",
    "отправить",
    "написать",
    "сделать",
    "оплатить",
    "записаться",
    "забрать",
    "встретиться",
    "проверить",
    "починить",
    "приготовить",
    "разобрать",
    "подписать",
    "оформить",
    "заказать",
    "забронировать",
    "сходить",
    "сдать",
    "найти",
    "выбрать",
    "собрать",
    "прочитать",
    "выучить",
    "съездить",
    "заехать",
    "зайти",
    "доделать",
    "отвезти",
    "напомнить",
    "разобраться",
    "настроить",
    "оплачивать",
)

#: Приоритет (ресурс задач v4) по словам в тексте.
MUST_WORDS: Tuple[str, ...] = (
    "срочно",
    "обязательно",
    "критично",
    "must",
    "сегодня же",
    "не откладывая",
)
LOW_WORDS: Tuple[str, ...] = (
    "если будет время",
    "когда будет время",
    "потом",
    "не срочно",
    "низкий приоритет",
    "рутина",
)

#: Настроение: тег и слова.
MOOD_WORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("усталость", ("устал", "устала", "вымот", "истощ", "нет сил", "выгор", "сонлив")),
    ("радость", ("рад", "радост", "счастлив", "доволен", "довольна", "класс", "отлично")),
    ("тревога", ("тревог", "волну", "страшн", "переживаю", "нервнич", "паник")),
    ("злость", ("злюсь", "бесит", "раздраж", "гнев", "ярость")),
    ("спокойно", ("спокой", "тихо", "умиротвор", "норм", "ровно")),
    ("грусть", ("груст", "печаль", "тоск", "обидно", "жаль")),
)

#: Темы журнала.
TOPIC_KEYWORDS: Mapping[str, Tuple[str, ...]] = {
    "работа": ("работ", "созвон", "клиент", "офис", "проект", "задач", "коллег", "босс", "встреч"),
    "здоровье": ("здоров", "тело", "спорт", "сон", "устал", "энерг", "болит", "врач"),
    "семья": ("семь", "жена", "муж", "реб", "мам", "пап", "родител", "друг", "дом"),
    "деньги": ("деньг", "банк", "финанс", "трат", "зарплат", "бюджет", "кредит"),
    "учёба": ("книг", "учеб", "курс", "читать", "лекци", "англ", "язык"),
    "отдых": ("отдых", "отпуск", "прогул", "кино", "сериал", "игр", "поездк"),
    "творчество": ("идея", "творч", "музык", "рису", "писал", "стих"),
}

#: Опечатки и разговорные формы, которые бот понимает без LLM.
_TYPO_WORDS: Mapping[str, str] = {
    "звтра": "завтра",
    "зватра": "завтра",
    "зафтра": "завтра",
    "сигодня": "сегодня",
    "севодня": "сегодня",
    "вчира": "вчера",
    "вчерась": "вчера",
    "пазванить": "позвонить",
    "пазвани": "позвони",
    "званить": "звонить",
    "нада": "надо",
    "нужна": "нужно",
    "щас": "сейчас",
    "купиь": "купить",
    "атправить": "отправить",
    "праверить": "проверить",
    "пратикуку": "практику",
    "пратику": "практику",
    "пратике": "практике",
    "хоффа": "хофа",
    "хофф": "хоф",
    "дыхательнй": "дыхательной",
}

_SMALLTALK = frozenset(
    {
        "привет",
        "приветик",
        "здравствуй",
        "здравствуйте",
        "доброе утро",
        "добрый день",
        "добрый вечер",
        "спасибо",
        "спс",
        "благодарю",
        "ок",
        "окей",
        "хорошо",
        "понял",
        "поняла",
        "ясно",
        "ага",
        "да",
        "нет",
    }
)


# --------------------------------------------------------------------------
# Даты и время
# --------------------------------------------------------------------------

_MONTH_FORMS: Mapping[int, Tuple[str, ...]] = {
    1: ("января", "январь"),
    2: ("февраля", "февраль"),
    3: ("марта", "март"),
    4: ("апреля", "апрель"),
    5: ("мая", "май"),
    6: ("июня", "июнь"),
    7: ("июля", "июль"),
    8: ("августа", "август"),
    9: ("сентября", "сентябрь"),
    10: ("октября", "октябрь"),
    11: ("ноября", "ноябрь"),
    12: ("декабря", "декабрь"),
}

_MONTH_RE_PARTS: List[Tuple[int, str]] = [
    (month, form) for month, forms in _MONTH_FORMS.items() for form in forms
]
_MONTH_RE_PARTS.sort(key=lambda item: len(item[1]), reverse=True)
_MONTH_ALT = "|".join(form for _, form in _MONTH_RE_PARTS)
_MONTH_BY_FORM = {form: month for month, form in _MONTH_RE_PARTS}

_WEEKDAYS: Mapping[str, int] = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "среду": 2,
    "четверг": 3,
    "пятница": 4,
    "пятницу": 4,
    "суббота": 5,
    "субботу": 5,
    "воскресенье": 6,
}

_MONTH_TITLES = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

_DATE_DMY_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")
_DATE_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})\s+(?:%s)\b" % _MONTH_ALT, re.IGNORECASE)
_DATE_DAY_CHISLA_RE = re.compile(r"\b(\d{1,2})\s+числа\b", re.IGNORECASE)
_DATE_DAY_ONLY_RE = re.compile(r"\b(\d{1,2})[ -]?го\b", re.IGNORECASE)
_DATE_IN_DAYS_RE = re.compile(
    r"\bчерез\s+(\d{1,3})\s*(?:день|дня|дней|ден[ьй])\b", re.IGNORECASE
)
_DATE_WEEKDAY_RE = re.compile(
    r"\b(?:в|во|на)\s+(?:(?:следующий|следующую|следующее|этот|эту|"
    r"ближайший|ближайшую)\s+)?"
    r"(понедельник|вторник|среду|среда|четверг|пятницу|пятница|"
    r"субботу|суббота|воскресенье)\b",
    re.IGNORECASE,
)
_WEEK_END_RE = re.compile(r"\bдо\s+конца\s+недели\b", re.IGNORECASE)
_TIME_RE = re.compile(r"\b(?:в|к|во)\s+(\d{1,2})(?::(\d{2}))?\b", re.IGNORECASE)
_TIME_COLON_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _shift(day: date, days: int) -> str:
    return (day + timedelta(days=days)).isoformat()


def _next_weekday(today: date, weekday: int) -> date:
    ahead = (weekday - today.weekday()) % 7
    return today + timedelta(days=ahead if ahead else 7)


def parse_date_phrase(text: str, today: Optional[date] = None) -> Tuple[Optional[str], str]:
    """Дата из фразы: ``("2026-11-19", "завтра")``; нет даты — ``(None, "")``.

    Понимает «сегодня/завтра/послезавтра/вчера», «через N дней»,
    «в пятницу», «в следующий вторник», «до конца недели», «19.11»,
    «19 ноября», «15 числа», «19-го». Прошедшие день/месяц переносятся на
    следующий год; для дня недели берётся ближайший будущий.
    """
    base = today or date.today()
    source = str(text or "")
    lowered = source.casefold()

    for word, offset in (("послезавтра", 2), ("завтра", 1), ("сегодня", 0), ("вчера", -1)):
        if re.search(r"\b%s\b" % word, lowered):
            return _shift(base, offset), word

    match = _WEEK_END_RE.search(source)
    if match:
        sunday = base + timedelta(days=6 - base.weekday())
        return sunday.isoformat(), match.group(0)

    match = _DATE_IN_DAYS_RE.search(source)
    if match:
        return _shift(base, int(match.group(1))), match.group(0)

    match = _DATE_WEEKDAY_RE.search(source)
    if match:
        weekday = _WEEKDAYS.get(match.group(1).casefold())
        if weekday is not None:
            return _next_weekday(base, weekday).isoformat(), match.group(0)

    match = _DATE_DMY_RE.search(source)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = _year_for(base, month, day, match.group(3))
        try:
            return date(year, month, day).isoformat(), match.group(0)
        except ValueError:
            return None, ""

    match = _DATE_DAY_MONTH_RE.search(source)
    if match:
        day = int(match.group(1))
        month = _MONTH_BY_FORM.get(match.group(2).casefold(), 0)
        if month:
            try:
                return date(_year_for(base, month, day), month, day).isoformat(), match.group(0)
            except ValueError:
                return None, ""

    match = _DATE_DAY_CHISLA_RE.search(source)
    if match:
        return _day_this_or_next_month(base, int(match.group(1))), match.group(0)

    match = _DATE_DAY_ONLY_RE.search(source)
    if match:
        return _day_this_or_next_month(base, int(match.group(1))), match.group(0)

    return None, ""


def _day_this_or_next_month(base: date, day: int) -> str:
    year, month = base.year, base.month
    if day < base.day:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        # 31-е в коротком месяце — берём последний день месяца.
        next_month = (month % 12) + 1
        next_year = year + (1 if month == 12 else 0)
        return date(next_year, next_month, 1).isoformat()


def _year_for(base: date, month: int, day: int, raw_year: Optional[str]) -> int:
    if raw_year:
        year = int(raw_year)
        if year < 100:
            year += 2000
        return year
    try:
        candidate = date(base.year, month, day)
    except ValueError:  # 31 февраля и т.п.
        return base.year
    return candidate.year if candidate >= base else base.year + 1


def parse_time_phrase(text: str) -> Optional[str]:
    """Время события «в 15» / «15:30» / «в 9 утра» -> ``"15:00"``."""
    source = str(text or "")
    match = _TIME_COLON_RE.search(source)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour <= 23 and minute <= 59:
            return "%02d:%02d" % (hour, minute)
    match = _TIME_RE.search(source)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if hour <= 23 and minute <= 59:
            return "%02d:%02d" % (hour, minute)
    return None


def format_date_short(iso: Optional[str], today: Optional[date] = None) -> str:
    """Дата для одной строки подтверждения: «сегодня», «завтра», «19.11»."""
    if not iso:
        return ""
    base = today or date.today()
    try:
        day = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    delta = (day - base).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == 2:
        return "послезавтра"
    if delta == -1:
        return "вчера"
    return "%02d.%02d" % (day.day, day.month)


def format_human_day(iso: str) -> str:
    """«2026-11-19» -> «19 ноября» (для сводок)."""
    try:
        day = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    return "%d %s" % (day.day, _MONTH_TITLES[day.month - 1])


# --------------------------------------------------------------------------
# Числительные и суммы
# --------------------------------------------------------------------------

_UNITS_WORDS: Mapping[str, float] = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "одно": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
    "двести": 200,
    "триста": 300,
    "четыреста": 400,
    "пятьсот": 500,
    "шестьсот": 600,
    "семьсот": 700,
    "восемьсот": 800,
    "девятьсот": 900,
    "полтора": 1.5,
    "полторы": 1.5,
    "сотня": 100,
    "полсотни": 50,
}
_MULT_WORDS: Mapping[str, float] = {
    "тысяча": 1000,
    "тысячи": 1000,
    "тысячу": 1000,
    "тысяч": 1000,
    "тыща": 1000,
    "тыщу": 1000,
    "тыщи": 1000,
    "миллион": 1_000_000,
    "миллиона": 1_000_000,
    "миллионов": 1_000_000,
    "млн": 1_000_000,
}
_NUMBER_WORDS = frozenset(_UNITS_WORDS) | frozenset(_MULT_WORDS)
_WORD_TOKEN_RE = re.compile(r"[А-Яа-яЁё]+|\d+(?:[.,]\d+)?", re.UNICODE)
_HALF_RE = re.compile(
    r"\b([А-Яа-яЁё]+|\d+(?:[.,]\d+)?)\s+с\s+половиной\b", re.IGNORECASE
)
_TYPO_RE = re.compile(
    r"(?<![\wА-Яа-яЁё])(?:%s)(?![\wА-Яа-яЁё])"
    % "|".join(re.escape(word) for word in sorted(_TYPO_WORDS, key=len, reverse=True)),
    re.IGNORECASE,
)


def _word_value(words: Sequence[str]) -> float:
    total = 0.0
    chunk = 0.0
    for word in words:
        token = word.casefold()
        if token in _MULT_WORDS:
            total += (chunk or 1.0) * _MULT_WORDS[token]
            chunk = 0.0
        else:
            chunk += _UNITS_WORDS.get(token, 0.0)
    return total + chunk


def _format_plain_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return ("%.1f" % value).replace(".", ",")


def normalize_text(text: str) -> str:
    """Чинит опечатки и переводит числительные словами в цифры.

    «звтра пазванить» → «завтра позвонить», «спал семь с половиной часов» →
    «спал 7,5 часов», «выпил два литра» → «выпил 2 литра»,
    «получил 50 тысяч» остаётся числами (множитель разберёт денежный слой).
    """
    source = str(text or "")
    if not source.strip():
        return source

    def _typo(match: re.Match) -> str:
        return _TYPO_WORDS.get(match.group(0).casefold(), match.group(0))

    result = _TYPO_RE.sub(_typo, source)

    def _half(match: re.Match) -> str:
        raw = match.group(1)
        try:
            number = float(raw.replace(",", "."))
        except ValueError:
            number = _UNITS_WORDS.get(raw.casefold(), 0.0)
        return _format_plain_number(number + 0.5)

    result = _HALF_RE.sub(_half, result)

    matches = list(_WORD_TOKEN_RE.finditer(result))
    if not matches:
        return result
    out: List[str] = []
    position = 0
    index = 0
    while index < len(matches):
        match = matches[index]
        token = match.group(0).casefold()
        if token in _NUMBER_WORDS:
            # «50 тысяч» — число цифрами плюс множитель: это денежный слой,
            # словами его не переписываем.
            if token in _MULT_WORDS and index > 0:
                previous = matches[index - 1]
                gap = result[previous.end() : match.start()].strip(" ")
                if re.fullmatch(r"\d+(?:[.,]\d+)?", previous.group(0)) and gap == "":
                    index += 1
                    continue
            run = [match]
            cursor = index + 1
            while cursor < len(matches):
                candidate = matches[cursor]
                gap = result[run[-1].end() : candidate.start()].strip(" -")
                if candidate.group(0).casefold() in _NUMBER_WORDS and gap in {"", "и"}:
                    run.append(candidate)
                    cursor += 1
                else:
                    break
            value = _word_value([item.group(0) for item in run])
            if value > 0 or (len(run) == 1 and run[0].group(0).casefold() == "ноль"):
                out.append(result[position : match.start()])
                out.append(_format_plain_number(value))
                position = run[-1].end()
            index = cursor
            continue
        index += 1
    out.append(result[position:])
    return "".join(out)


def parse_russian_number(text: str) -> Optional[float]:
    """«тысячу двести» -> 1200.0; числительные идут подряд, иначе ``None``."""
    tokens = re.findall(r"[а-яё]+", str(text or "").casefold())
    value = 0.0
    started = False
    run: List[str] = []
    for token in tokens:
        if token in _NUMBER_WORDS:
            run.append(token)
            started = True
        elif run:
            break
    if not started or not run:
        return None
    value = _word_value(run)
    return value if value > 0 else None


_NUM = r"(\d{1,3}(?:[ \u00a0\u202f]\d{1,3})+|\d+(?:[.,]\d{1,2})?)"
_CURRENCY_RE = re.compile(
    _NUM + r"\s*(?:₽|руб(?:\.|лей|ля|ль|ля)?\b|р\.(?=\s|$)|р\b(?=\s|$))",
    re.IGNORECASE,
)
_THOUSANDS_RE = re.compile(
    r"(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+(?:[.,]\d{1,2})?)\s*"
    r"(тысяч\w*|тыс\.?|миллион\w*|млн)\b",
    re.IGNORECASE,
)
_UNIT_BLACKLIST = (
    "кг",
    "км",
    "м",
    "мл",
    "мин",
    "минут",
    "ч",
    "час",
    "%",
    "стр",
    "страниц",
    "ккал",
    "г",
    "гр",
    "л",
    "литр",
    "раз",
    "дн",
    "дней",
    "шаг",
    "тыс",
    "м2",
    "подход",
)
_UNIT_GUARD = r"(?!\s*(?:%s))" % "|".join(re.escape(unit) for unit in _UNIT_BLACKLIST)
#: Число не должно быть временем («в 15»), датой или единицей измерения.
_PLAIN_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(?<!\bв\s)(?<!\bк\s)" + _NUM + _UNIT_GUARD,
    re.IGNORECASE,
)


def _to_kopecks(value: float) -> int:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:  # pragma: no cover - value уже число
        return 0
    kopecks = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(kopecks)


def parse_amount(text: str) -> Tuple[Optional[int], str]:
    """Сумма в рублях -> копейки и найденный фрагмент.

    Понимает «450 ₽», «1 200», «3 20» (320), «50 тысяч», «тысячу двести»;
    не принимает числа с единицами измерения и время «в 15».
    """
    source = str(text or "")
    match = _CURRENCY_RE.search(source)
    if match:
        return _to_kopecks(_number(match.group(1))), match.group(0)
    match = _THOUSANDS_RE.search(source)
    if match:
        value = _number(match.group(1))
        if match.group(2).casefold().startswith(("м", "m")):
            value *= 1_000_000
        else:
            value *= 1000
        return _to_kopecks(value), match.group(0)
    words = parse_russian_number(source)
    if words is not None:
        return _to_kopecks(words), ""
    match = _PLAIN_AMOUNT_RE.search(source)
    if match:
        return _to_kopecks(_number(match.group(1))), match.group(0)
    return None, ""


def format_money(kopecks: Optional[int]) -> str:
    """Копейки -> «1 200 ₽» без лишних нулей."""
    value = Decimal(int(kopecks or 0)) / 100
    if value == value.to_integral_value():
        text = "{:,}".format(int(value)).replace(",", " ")
    else:
        text = ("%.2f" % float(value)).replace(".", ",")
    return text + " ₽"


def _number(raw: str) -> float:
    cleaned = str(raw or "").replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# Словари: сфера, категория, счёт, темы, настроение
# --------------------------------------------------------------------------


def _contains_any(text: str, words: Iterable[str]) -> bool:
    lowered = str(text or "").casefold()
    return any(word in lowered for word in words)


def infer_sphere(text: str) -> str:
    """Каноническое имя сферы по словам в тексте; пусто — не угадали."""
    lowered = str(text or "").casefold()
    for name, _color, _icon in DEFAULT_SPHERES:
        root = name.split()[0].casefold()
        if root and root in lowered:
            return name
    for canonical, words in SPHERE_KEYWORDS.items():
        if _contains_any(lowered, words):
            return canonical
    return ""


def infer_category(text: str, kind: str = "expense") -> str:
    """Статья расхода/дохода по словам; пусто — «прочее»."""
    lowered = str(text or "").casefold()
    if kind == "income":
        for name, words in INCOME_KEYWORDS.items():
            if _contains_any(lowered, words):
                return name
        return ""
    for name, words in CATEGORY_KEYWORDS.items():
        if _contains_any(lowered, words):
            return name
    return ""


def infer_account(text: str) -> str:
    """Вид счёта: ``cash`` / ``card`` / ``savings`` / ``broker`` / пусто."""
    lowered = str(text or "").casefold()
    if _contains_any(lowered, ("наличн", "нал ", "налом", "кэш", "кеш", "из кошелька")):
        return "cash"
    if _contains_any(lowered, ("картой", "карта", "с карты", "безнал", "пластик")):
        return "card"
    if _contains_any(lowered, ("накопит", "сберегат", "вклад", "кубышк")):
        return "savings"
    if _contains_any(lowered, ("брокер", "инвест", "акци", "фонд")):
        return "broker"
    return ""


def account_title(hint: str) -> str:
    return {"cash": "Наличные", "card": "Карта", "savings": "Накопительный счёт", "broker": "Брокерский счёт"}.get(
        str(hint or ""), ""
    )


def infer_topics(text: str) -> List[str]:
    """Темы мысли: работа, здоровье, семья, деньги, учёба, отдых, творчество."""
    lowered = str(text or "").casefold()
    return [name for name, words in TOPIC_KEYWORDS.items() if _contains_any(lowered, words)]


def infer_mood(text: str) -> str:
    """Тег настроения из слов: усталость, радость, тревога, спокойно..."""
    lowered = str(text or "").casefold()
    for tag, words in MOOD_WORDS:
        if _contains_any(lowered, words):
            return tag
    return ""


# --------------------------------------------------------------------------
# Регулярки намерений
# --------------------------------------------------------------------------

_DELETE_LAST_RE = re.compile(
    r"\b(?:удали|убери|сотри|отмени|удалить|убрать)\b.{0,24}?"
    r"\b(?:последн\w*|запис\w*|это|всё|все|строку)\b",
    re.IGNORECASE,
)
_RESCHEDULE_RE = re.compile(
    r"\b(?:перенеси|перенести|передвинь|сдвинь|отложи)\b", re.IGNORECASE
)
_MOVE_WORK_RE = re.compile(
    r"^(?:(?:это|задачу|дело)\s+)?в\s+(?:работ[уе]|рабоч\w+\s+список)[!.?]*$"
    r"|^сделай\s+(?:это\s+)?рабоч\w*$",
    re.IGNORECASE,
)
_MOVE_PERSONAL_RE = re.compile(
    r"^(?:(?:это|задачу|дело)\s+)?в\s+(?:личн\w+|личное)[!.?]*$"
    r"|^сделай\s+(?:это\s+)?личн\w*$",
    re.IGNORECASE,
)

_SUMMARY_RE = re.compile(
    r"\bсколько\b[^?]*\b(?:потрат\w*|трат\w*|расход\w*|заработ\w*|доход\w*|"
    r"денег|деньги|ушло|пришло)\b"
    r"|\b(?:траты|расходы|доходы|финансы)\b[^?]*\b(?:за|сегодня|недел\w*|месяц\w*)\b"
    r"|\bсколько\s+(?:я\s+)?(?:за\s+)?(?:недел\w*|месяц\w*)\b",
    re.IGNORECASE,
)
_TODAY_RE = re.compile(
    r"^(?:что\s+)?(?:на\s+)?сегодня[?.!]*$|\bчто\s+на\s+сегодня\b|\bсводка\b"
    r"|\bпланы\s+на\s+день\b|\bчто\s+по\s+дню\b",
    re.IGNORECASE,
)
_WEEK_RE = re.compile(
    r"\bитоги\s+недели\b|\bчто\s+за\s+неделю\b|\bнеделя\b|\bсводка\s+за\s+неделю\b"
    r"|\bкак\s+неделя\b",
    re.IGNORECASE,
)
_QUERY_TASKS_RE = re.compile(
    r"\b(?:какие|что|покажи|список|напомни)\b[^?]{0,30}\b(?:задачи|дела|план)\b"
    r"|\bзадачи\s+на\b",
    re.IGNORECASE,
)
_QUERY_ENERGY_RE = re.compile(
    r"\b(?:сколько|какая|какой|уровень)\b[^?]{0,20}\bэнерги\w*\b"
    r"|\bэнерги\w*\s+(?:вчера|сегодня|за)\b",
    re.IGNORECASE,
)
_QUERY_SLEEP_RE = re.compile(
    r"\b(?:как\s+(?:я\s+)?спал|сколько\s+(?:я\s+)?спал|сон\s+за|"
    r"как\s+сон|высыпа\w*)\b",
    re.IGNORECASE,
)
_QUERY_READ_RE = re.compile(
    r"\bчто\s+(?:я\s+)?читал\w*|\bпро\s+что\s+читал\w*|\bчто\s+почитать\b",
    re.IGNORECASE,
)
_QUERY_BOOKS_RE = re.compile(
    r"\b(?:покажи|какие|список|мои)\b[^?]{0,20}\bкниг\w*", re.IGNORECASE
)
_MONEY_TOP_RE = re.compile(
    r"\bна\s+что\b[^?]*\b(?:потрат\w*|трат\w*|расход\w*|ушло|больше\s+всего)\b"
    r"|\bбольше\s+всего\b[^?]*\b(?:потрат\w*|трат\w*|расход\w*)\b"
    r"|\bкуда\s+уход\w*\s+деньги\b",
    re.IGNORECASE,
)
_QUERY_DAY_RE = re.compile(
    r"\bчто\s+(?:у\s+меня\s+)?(?:на|по)\s+(сегодня|завтра|послезавтра)\b"
    r"|\bкакие\s+(?:дела|задачи|планы)\s+(?:на|по)\s+(сегодня|завтра|послезавтра)\b"
    r"|\bчто\s+у\s+меня\s+(?:сегодня|завтра)\b",
    re.IGNORECASE,
)
_QUERY_OVERDUE_RE = re.compile(
    r"\bчто\s+просрочен\w*|\bпросрочен\w*\s+(?:дела|задачи)?\b|\bчто\s+просрочил\w*",
    re.IGNORECASE,
)
_QUERY_GOAL_RE = re.compile(
    r"\b(?:как\s+дела\s+с\s+целью|как\s+цель|что\s+с\s+целью|"
    r"какой\s+прогресс\s+по\s+цели|прогресс\s+по\s+цели|прогресс\s+цели|"
    r"как\s+продвигается\s+цель)\s+([^?.,;]+)",
    re.IGNORECASE,
)
_QUERY_WEIGHT_RE = re.compile(
    r"\b(?:сколько\s+(?:я\s+)?весил\w*|средний\s+вес|какой\s+(?:у\s+меня\s+)?вес|"
    r"вес\s+за|динамика\s+веса)\b",
    re.IGNORECASE,
)
_QUERY_NUTRITION_RE = re.compile(
    r"\bсколько\s+(?:я\s+)?(?:съел\w*|калори\w*|ккал|белк\w*|жир\w*|углевод\w*|"
    r"съела|поел\w*)\b|\bкалории\s+(?:за|вчера|сегодня)\b",
    re.IGNORECASE,
)
_QUERY_JOURNAL_RE = re.compile(
    r"\bо\s+ч[её]м\s+(?:я\s+)?писал\w*|\bчто\s+(?:я\s+)?писал\w*|"
    r"\bчто\s+в\s+журнале\b|\bо\s+ч[её]м\s+записи\b|\bчто\s+я\s+записал\w*",
    re.IGNORECASE,
)
_QUERY_LAST_WORKOUT_RE = re.compile(
    r"\bкогда\s+(?:я\s+)?(?:последний\s+раз\s+)?(?:был[аи]?\s+)?"
    r"(?:в\s+зале|на\s+тренировке|тренировал\w*|занимал\w*)\b"
    r"|\bпоследн\w+\s+тренировк\w*",
    re.IGNORECASE,
)
_QUERY_HABITS_RE = re.compile(
    r"\bсколько\s+привычек\b|\bкак\s+с\s+привычками\b|\bпривычки\s+держу\b"
    r"|\bкакие\s+привычки\b|\bкак\s+привычки\b",
    re.IGNORECASE,
)
_QUERY_POINTS_RULES_RE = re.compile(
    r"\bчто\b[^?]{0,40}\b(?:да[её]т|дают|приносит|приносят)\b[^?]{0,30}\bочк\w*"
    r"|\bза\s+что\b[^?]{0,30}\bочк\w*"
    r"|\bкак\s+(?:получить|заработать|набрать)\s+очк\w*",
    re.IGNORECASE,
)
_QUERY_LEVEL_RE = re.compile(
    r"\bсколько\b[^?]{0,40}\bдо\s+(?:следующего\s+|нового\s+)?уровн\w*"
    r"|\b(?:какой|какая|когда)\b[^?]{0,25}\bуровень\b"
    r"|\bдо\s+(?:следующего|нового)\s+уровн\w*",
    re.IGNORECASE,
)
_QUERY_POINTS_RE = re.compile(
    r"\bочк\w*|\bбалл\w*|\bpoints\b", re.IGNORECASE
)
_QUESTION_WORDS = frozenset(
    {"что", "какие", "какой", "какая", "сколько", "где", "когда", "почему", "зачем", "кто", "как", "можно", "есть"}
)
_UNCERTAIN_RE = re.compile(
    r"\bне\s+знаю\s+что\b|\bчто-то\s+но\b|\bне\s+помню\s+что\b|\bхочу\s+что-то\b",
    re.IGNORECASE,
)

_CHECKIN_RE = re.compile(
    r"\b(?:энерги[яю]|ресурс|заряд)\s*[:=]?\s*([1-5])(?:\s*/\s*5)?\b",
    re.IGNORECASE,
)
_CHECKIN_HEAD_RE = re.compile(r"\b(?:чек[\s-]?ин|checkin)\b", re.IGNORECASE)

_GOAL_STEP_DONE_RE = re.compile(
    r"\bвех[аиу]\b[^.]{0,60}\b(?:выполн\w*|готова|готов\w*|сделана|закрыта|"
    r"закрыл\w*|закрой|закройте|отметь|отметить|зачёт|зачет|done)\b"
    r"|\b(?:выполн\w*|закрыл\w*|закрыта|закрой\w*|сделана|заверш\w*|отметь\w*)\b"
    r"[^.]{0,40}\bвех[аиу]\b",
    re.IGNORECASE,
)
_GOAL_DELTA_RE = re.compile(
    r"([+-]?\d{1,3})\s*(?:%|процент\w*)\s*(?:к|по|в)\s+цели\s+(.+)$",
    re.IGNORECASE,
)
_GOAL_AFTER_RE = re.compile(
    r"\bцел[иья]\s+([^,.;]+?)\s+([+-]?\d{1,3})\s*(?:%|процент\w*)?\s*$",
    re.IGNORECASE,
)
_GOAL_PROGRESS_RE = re.compile(
    r"\bпрогресс\s+(?:по\s+)?цели\s+([^,.;?]+?)\s+([+-]?\d{1,3})\s*%?",
    re.IGNORECASE,
)
_GOAL_BARE_RE = re.compile(r"\b([+-]\d{1,3})\s*(?:%|процент\w*)\s+(.+)$", re.IGNORECASE)
_GOAL_CREATE_RE = re.compile(
    r"^(?:новая\s+)?цель\s*[:\-—]\s*(.+)$|^(?:поставь|поставить)\s+цель\s*[:\-—]?\s*(.+)$",
    re.IGNORECASE,
)

_QUOTE_RE = re.compile(
    r"^(?:цитата|выписал[аи]?|выписка|цитирую)\s*(?:из\s+книги)?\s*[:\-—]?\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_INSIGHT_RE = re.compile(
    r"^(?:мысль\s+из\s+книги|идея\s+из\s+книги|инсайт(?:\s+из\s+книги)?|"
    r"мысль|идея)\s*[:\-—]?\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_BOOK_PAGE_RE = re.compile(
    r"\b(?:стр\.?|страниц[аыу]?|странице|page)\s*(\d{1,4})(?:\s*(?:из|до)\s*(\d{1,4}))?",
    re.IGNORECASE,
)
_BOOK_PREFIX_RE = re.compile(
    r"^(?:читаю|читал[аи]?|дочитываю)\s+(?:книгу\s+)?(.+)$",
    re.IGNORECASE,
)
_BOOK_READ_PAGES_RE = re.compile(
    r"^(?:прочитал[аи]?|прочёл|прочел[аи]?)\s+(\d{1,4})\s*страниц\w*\s*(.*)$",
    re.IGNORECASE,
)
_BOOK_PAGES_ONLY_RE = re.compile(
    r"^(\d{1,4})\s*страниц\w*$", re.IGNORECASE
)
_BOOK_DONE_RE = re.compile(
    r"^(?:дочитал[аи]?|закончил[аи]?\s+книгу|прочитал[аи]?\s+книгу)\s+(.+)$",
    re.IGNORECASE,
)
_BOOK_BUY_RE = re.compile(
    r"^(?:купил[аи]?|приобрёл|приобрел[аи]?)\s+книгу\s+(.+)$",
    re.IGNORECASE,
)
_BOOK_START_RE = re.compile(
    r"^(?:взял[аи]?\s+в\s+работу|начал[аи]?\s+читать|начал[аи]?\s+книгу)\s+(.+)$",
    re.IGNORECASE,
)
_BOOK_DROP_RE = re.compile(
    r"^(?:отложил[аи]?|бросил[аи]?)\s+(?:книгу\s+)?(.+)$",
    re.IGNORECASE,
)

_HABIT_WATER_RE = re.compile(
    r"\bвыпил[аи]?\b.{0,24}?(\d{1,4}(?:[.,]\d{1,2})?)\s*(л|литр\w*|мл|ml|l)\b",
    re.IGNORECASE,
)
_HABIT_RUN_RE = re.compile(
    r"\b(?:пробежал[аи]?|пробежк\w*|бегал[аи]?|кросс)\b.{0,24}?(\d{1,3}(?:[.,]\d{1,2})?)\s*км\b",
    re.IGNORECASE,
)
_HABIT_WALK_RE = re.compile(
    r"\b(?:прош[её]л[аи]?|прогул\w*|прошагал[аи]?|шаг\w*)\b.{0,24}?"
    r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(тыс\w*\s*шаг\w*|шаг\w*|км)\b",
    re.IGNORECASE,
)
_HABIT_WALK_MIN_RE = re.compile(r"\bпрогул\w*\b.{0,16}?(\d{1,3})\s*мин", re.IGNORECASE)
_HABIT_READ_RE = re.compile(r"\bчитал[аи]?\b.{0,16}?(\d{1,3})\s*мин", re.IGNORECASE)
_HABIT_STRETCH_RE = re.compile(r"\bрастяжк\w*|\bпотянул[аи]?ся\b", re.IGNORECASE)
_HABIT_EXERCISE_RE = re.compile(r"\bзарядк\w*|\bзарядку\b", re.IGNORECASE)
_HABIT_NO_SMOKE_RE = re.compile(
    r"\bне\s+курил[аи]?\b|\bбез\s+сигарет\b|\bбросил\w*\s+курить\b", re.IGNORECASE
)
_HABIT_NO_DRINK_RE = re.compile(r"\bне\s+пил[аи]?\b|\bбез\s+алкогол\w*\b", re.IGNORECASE)
_HABIT_NO_SUGAR_RE = re.compile(r"\bбез\s+сахар\w*\b", re.IGNORECASE)
_HABIT_MARK_RE = re.compile(
    r"\b(?:отметь|отметить|запиши|записать)\s+(?:привычку\s+)?(.+)$", re.IGNORECASE
)

_TASK_DONE_RE = re.compile(
    r"\b(?:сделал[аи]?|выполнил[аи]?|готово|закрыл[аи]?|закрыто|закрыть|закрой|"
    r"завершил[аи]?|завершено|done|вычеркни)\b\s*(.*)$",
    re.IGNORECASE,
)
_TASK_ESTIMATE_RE = re.compile(r"\b(\d{1,3})\s*(?:мин|минут\w*)\b", re.IGNORECASE)
_TASK_HALF_HOUR_RE = re.compile(r"\bполчаса\b", re.IGNORECASE)
_TASK_HOUR_RE = re.compile(r"\b(?:час|1 час)\b", re.IGNORECASE)
_TASK_PREFIX_RE = re.compile(r"^\s*(?:задача|дело)\s*[:\-—]\s*", re.IGNORECASE)

_WORKOUT_SETS_RE = re.compile(
    r"(\d{1,2})\s*подход\w*\s*(?:по|х|×)\s*(\d{1,3})", re.IGNORECASE
)
_WORKOUT_EXERCISE_RE = re.compile(
    r"\b(отжал\w*|присел\w*|пресс|планк\w*|подтян\w*|выпад\w*|жим\w*|присед\w*)\b",
    re.IGNORECASE,
)
_MEAL_RE = re.compile(r"\b(завтрак|обед|ужин|перекус|ланч)\b", re.IGNORECASE)
_EATEN_RE = re.compile(
    r"\b(?:съел[аи]?|поел[аи]?|ел[аи]?|ем|перекусил[аи]?|выпил[аи]?\s+кофе)\b",
    re.IGNORECASE,
)
_KCAL_RE = re.compile(r"(\d{1,4}(?:[.,]\d+)?)\s*(?:ккал|калори\w*)", re.IGNORECASE)
_PROTEIN_RE = re.compile(
    r"(\d{1,3}(?:[.,]\d+)?)\s*(?:г\s*)?(?:белк\w*|протеин\w*)", re.IGNORECASE
)
_FAT_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*(?:г\s*)?(?:жир\w*)", re.IGNORECASE)
_CARBS_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*(?:г\s*)?(?:углевод\w*)", re.IGNORECASE)
#: Название продукта: слово + (процент жирности | второе слово). Процент
#: внутри названия — часть продукта («творог 5%», «молоко 2,5%»), а не граммы.
_FOOD_NAME = (
    r"([А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-]{2,}"
    r"(?:\s+(?:\d{1,3}(?:[.,]\d+)?\s*%|[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-]{2,}))?)"
)
_FOOD_ITEM_RE = re.compile(
    _FOOD_NAME + r"\s+"
    r"(\d{1,4}(?:[.,]\d+)?)(?!\s*%)\s*"
    r"(г|гр|грамм\w*|кг|ккал|калори\w*)?(?![А-Яа-яЁёA-Za-z])",
    re.IGNORECASE,
)
#: Обратный порядок: «150 г творога», «0,5 кг творога» — граммы впереди продукта.
_FOOD_GRAMS_FIRST_RE = re.compile(
    r"(\d{1,4}(?:[.,]\d+)?)\s*(г|гр|грамм\w*|кг)\s+"
    r"([А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-]{2,}"
    r"(?:\s+[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-]{2,})?)",
    re.IGNORECASE,
)
#: Яйца без весов: «2 яйца», «омлет из 3 яиц» — считаем по 55 г на яйцо.
_FOOD_EGGS_RE = re.compile(
    r"(?:(?:омлет\w*|яичниц\w*)\s+из\s+)?(\d{1,2})\s*я[ий]ц\w*",
    re.IGNORECASE,
)
#: Вес одного яйца без скорлупы, граммы (среднее куриное).
EGG_GRAMS = 55.0
#: Рядом с продуктом эти слова означают расход, а не еду: «кофе 320 картой».
_MONEY_HINT_RE = re.compile(
    r"(наличн\w*|картой|картою|рубл\w*|₽|\bруб\b|потратил\w*|потрачено|"
    r"заплатил\w*|заплачено|купил\w*|покупк\w*|стоил\w*|обошл\w*|заработал\w*)",
    re.IGNORECASE,
)
#: Слова-единицы и служебные слова: в позиции еды они не название продукта.
_FOOD_UNITS = frozenset({"г", "гр", "грамм", "граммов", "грамма", "кг", "ккал", "калорий", "калории"})
#: Слова, которые не являются продуктами в «слово + граммы».
_FOOD_STOP = frozenset(
    {
        "завтрак",
        "обед",
        "ужин",
        "перекус",
        "ланч",
        "съел",
        "съела",
        "поел",
        "поела",
        "ем",
        "белка",
        "белок",
        "белков",
        "жира",
        "жир",
        "жиров",
        "углеводов",
        "углеводы",
        "ккал",
        "калорий",
        "калории",
        "грамм",
        "граммов",
        "после",
        "через",
        "сегодня",
        "вчера",
        "завтра",
        "около",
        "примерно",
        "всего",
        "итого",
        "потратил",
        "купил",
        "кафе",
        "такси",
    }
)
#: Коды приёмов пищи (как в ``food_log.meal``).
_MEAL_CODES = frozenset({"breakfast", "lunch", "dinner", "snack"})

_MEAL_LABELS = {
    "завтрак": "breakfast",
    "обед": "lunch",
    "ужин": "dinner",
    "перекус": "snack",
    "ланч": "lunch",
}

_WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]+")

#: Сколько позиций еды разбираем в одной фразе (защита от полотна текста).
_MAX_FOOD_ITEMS = 12


def _clean_food_name(raw: str) -> str:
    """Название продукта из «на обед гречка»: убирает служебные слова."""
    words = [word for word in str(raw or "").split() if word]
    while words and words[0].casefold() in _FOOD_STOP:
        words.pop(0)
    while len(words) > 1 and words[-1].casefold() in _FOOD_STOP:
        words.pop()
    name = " ".join(words).strip(" .,-—:")
    return "" if name.casefold() in _FOOD_STOP else name


def _food_leftovers(text: str, spans: Sequence[Tuple[int, int]]) -> List[str]:
    """Продукты без граммов: «завтрак овсянка 250 и кофе» -> кофе.

    Слово считается позицией, если оно похоже на продукт (по справочнику
    ``nutrition_v4``) и не входит в уже разобранную пару «продукт + число».
    """
    from app.bot.nutrition_v4 import is_food_word

    masked = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(masked), end)):
            masked[index] = " "
    cleaned = "".join(masked)
    found: List[Dict[str, Any]] = []
    for match in _WORD_RE.finditer(cleaned):
        token = match.group(0)
        lowered = token.casefold()
        if lowered in _FOOD_STOP or lowered in _FOOD_UNITS:
            continue
        if not is_food_word(token):
            continue
        if found and match.start() - int(found[-1]["end"]) <= 2:
            found[-1]["name"] = "%s %s" % (found[-1]["name"], token)
            found[-1]["end"] = match.end()
        else:
            found.append({"name": token, "end": match.end()})
    return [str(item["name"]) for item in found]


def _overlaps(span: Tuple[int, int], spans: Sequence[Tuple[int, int]]) -> bool:
    """Пересекается ли найденный фрагмент с уже разобранной позицией еды."""
    start, end = span
    return any(start < other_end and other_start < end for other_start, other_end in spans)


def parse_food(text: str) -> Optional[Dict[str, Any]]:
    """Разбирает фразу про еду: позиции, граммы, приём пищи, явные КБЖУ.

    Возвращает ``None``, если это не еда. Формат ответа::

        {"food_items": [{"name": "гречка", "grams": 200.0},
                        {"name": "курица", "grams": 150.0}],
         "meal": "обед", "grams_total": 350.0,
         "kcal": None, "protein": None, "fat": None, "carbs": None}

    Правила (важнее, чем кажется):

    * «продукт + число» — еда, даже без слова «съел» и без единицы
      («гречка 200 и курица 150»); «кг» переводится в граммы;
    * рядом с деньгами («кофе 320 картой», «купил гречку 200») еды нет —
      иначе расход превращался бы в завтрак;
    * продукт без граммов попадает в разбор с ``grams = 0``: обработчик
      честно спросит граммовку, а не придумает порцию;
    * ничего не пишет в БД и не ходит в сеть.
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    meal_match = _MEAL_RE.search(raw)
    eaten = bool(_EATEN_RE.search(raw))
    kcal = _number_match(_KCAL_RE, raw)
    protein = _number_match(_PROTEIN_RE, raw)
    fat = _number_match(_FAT_RE, raw)
    carbs = _number_match(_CARBS_RE, raw)

    if _MONEY_HINT_RE.search(raw) and not eaten and kcal is None:
        # «гречка 200 г за 100 рублей» — всё-таки еда: граммы названы явно.
        if not re.search(r"\d+\s*(?:г|гр|грамм\w*|кг)\b", raw, re.IGNORECASE):
            return None

    items: List[Dict[str, Any]] = []
    spans: List[Tuple[int, int]] = []
    grams_total = 0.0
    grams_explicit = False
    for match in _FOOD_ITEM_RE.finditer(raw):
        name = _clean_food_name(match.group(1))
        if not name:
            continue
        unit = (match.group(3) or "").casefold()
        if unit.startswith(("ккал", "калори")):
            continue
        value = _number(match.group(2))
        if unit == "кг":
            value *= 1000
            grams_explicit = True
        elif unit:
            grams_explicit = True
        if value < 0:
            continue
        items.append({"name": name, "grams": round(value, 1)})
        grams_total += value
        spans.append(match.span())

    # «150 г творога»: граммы названы раньше продукта. Берём, только если
    # слово действительно похоже на продукт, — «150 г после тренировки» не еда.
    for match in _FOOD_GRAMS_FIRST_RE.finditer(raw):
        if _overlaps(match.span(), spans):
            continue
        name = _clean_food_name(match.group(3))
        if not name or not _looks_food_item(name):
            continue
        value = _number(match.group(1))
        if (match.group(2) or "").casefold() == "кг":
            value *= 1000
        if value <= 0:
            continue
        items.append({"name": name, "grams": round(value, 1)})
        grams_total += value
        grams_explicit = True
        spans.append(match.span())

    # Яйца по счёту: «2 яйца», «омлет из 3 яиц» — 55 г на яйцо, продукт из
    # справочника («Яйцо куриное»), чтобы КБЖУ посчиталось, а не спрашивалось.
    for match in _FOOD_EGGS_RE.finditer(raw):
        if _overlaps(match.span(), spans):
            continue
        count = int(_number(match.group(1)))
        if count < 1 or count > 20:
            continue
        items.append({"name": "Яйцо куриное", "grams": round(count * EGG_GRAMS, 1)})
        grams_total += count * EGG_GRAMS
        grams_explicit = True
        spans.append(match.span())

    has_meal = meal_match is not None
    has_signal = bool(has_meal or eaten or kcal is not None or grams_explicit)
    if not has_signal:
        # Без приёма пищи и единиц едой считаем только узнаваемые продукты:
        # «гречка 200», «гречка 200 и курица 150», «кофе».
        items = [item for item in items if _looks_food_item(item["name"])]
        has_signal = bool(items)
    if not has_signal:
        return None

    leftovers = _food_leftovers(raw, spans) if (has_meal or eaten or items) else []
    for name in leftovers:
        items.append({"name": name, "grams": 0.0})

    deduped: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items[:_MAX_FOOD_ITEMS]:
        key = item["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    items = deduped

    if not items and kcal is None:
        return None

    meal = ""
    if meal_match:
        meal = meal_match.group(1).casefold()
    return {
        "food_items": items,
        "meal": meal,
        "grams_total": round(grams_total, 1),
        "kcal": kcal,
        "protein": protein,
        "fat": fat if kcal is not None else None,
        "carbs": carbs if kcal is not None else None,
    }


def _looks_food_item(name: str) -> bool:
    """Похоже ли название на продукт: «гречка», «куриная грудка», «кофе»."""
    from app.bot.nutrition_v4 import is_food_word

    return any(is_food_word(word) for word in str(name or "").split() if word)

_PRACTICE_RE = re.compile(
    r"\b(?:практик\w*|подыш\w*|задыш\w*|дыш\w*|дыхан\w*|медитаци\w*)\b",
    re.IGNORECASE,
)
_VIM_HOF_RE = re.compile(r"\bвим\w*\s*[-\s]?\s*хоф\w*", re.IGNORECASE)
_PRACTICE_ROUNDS_RE = re.compile(
    r"(\d{1,2})\s*(?:круг\w*|раунд\w*|цикл\w*|подход\w*)", re.IGNORECASE
)
_PRACTICE_DONE_RE = re.compile(
    r"\b(?:сделал[аи]?|выполнил[аи]?|закончил[аи]?|практиковал[аи]?|подышал[аи]?)\b",
    re.IGNORECASE,
)
#: Круги без слова «практика»: «3 раунда», «2 круга», «цикл». «Подход» сюда
#: не входит — это слово чаще про силовые упражнения.
_PRACTICE_BARE_ROUNDS_RE = re.compile(
    r"(\d{1,2})\s*(?:круг\w*|раунд\w*|цикл\w*)", re.IGNORECASE
)
#: Названия практик, которые сами по себе означают практику: «4-7-8».
_PRACTICE_NAMED_RE = re.compile(
    r"4\s*-?\s*7\s*-?\s*8|\b478\b|боксов\w*|\b5\s*-\s*5\b", re.IGNORECASE
)
#: Слова, при которых «3 круга» — это спорт, а не дыхательная практика.
_NOT_PRACTICE_RE = re.compile(
    r"\b(?:пробеж\w*|пробег\w*|бег\w*|бегу|бегом|стадион\w*|велосипед\w*|"
    r"плава\w*|бассейн\w*|кросс\w*|тренировк\w*|зал[ае]?|отжал\w*|приседа\w*|"
    r"подтяг\w*|жим\w*|штанг\w*|гантел\w*|упражнен\w*|планк\w*|кардио\w*)\b",
    re.IGNORECASE,
)


def _practice_hold_seconds(text: str) -> int:
    """«задержка 2 минуты», «задержка 1:30», «задержал 90 секунд» → секунды.

    Ноль — фраза не про задержку: тогда работает только счёт кругов.
    """
    lowered = str(text or "").casefold()
    if "задерж" not in lowered:
        return 0
    clock = re.search(r"задерж\w*[^.]{0,25}(\d{1,2}):(\d{2})", lowered)
    if clock:
        return int(clock.group(1)) * 60 + int(clock.group(2))
    minutes = re.search(r"задерж\w*[^.]{0,25}(\d{1,3})\s*(?:мин\w*|м\b)", lowered)
    if minutes:
        return int(minutes.group(1)) * 60
    seconds = re.search(r"задерж\w*[^.]{0,25}(\d{1,4})\s*(?:с|сек\w*)\b", lowered)
    if seconds:
        return int(seconds.group(1))
    return 0
_STATE_RE = re.compile(
    r"^(?:(?:делаю|делал[аи]?|занимаюсь|занимался|занималась|"
    r"работаю\s+над|сейчас\s+(?:делаю|занимаюсь|работаю))\b"
    # «дела X» без «-ю» — тот же факт, чем занят: «дела life os». Предлоги
    # («дела на сегодня», «дела по дому») сюда не попадают — это вопросы/списки.
    r"|дела\s+(?!(?:на|по|в|с|это|все|всё)\b))",
    re.IGNORECASE,
)

#: Фразы-состояния дня: факт о себе, а не мусор для Инбокса.
#: «поздно не сплю», «устал но держусь», «весь день в созвонах».
_LIFE_STATE_RE = re.compile(
    r"\bне\s+сплю\b|"
    r"\bпоздно\s+(?:лёг|лег|ложусь|встаю|проснул(?:ся|ась))\b|"
    r"\bдержусь\b|"
    r"\bвесь\s+день\b",
    re.IGNORECASE,
)
_SHORT_STARTERS: Mapping[str, Tuple[str, str]] = {
    "вода": ("water", "Сколько воды выпил?"),
    "воды": ("water", "Сколько воды выпил?"),
    "такси": ("money", "Сколько стоило такси? Напиши, например: «такси 450»."),
    "устал": ("energy", "Насколько устал? Оцени энергию от 1 до 5."),
    "устала": ("energy", "Насколько устала? Оцени энергию от 1 до 5."),
    "вымотан": ("energy", "Насколько вымотан? Оцени энергию от 1 до 5."),
    "вымотана": ("energy", "Насколько вымотана? Оцени энергию от 1 до 5."),
    "еда": ("food", "Что именно съел? Напиши: «на обед гречка 200 г и курица 150»."),
    "книга": ("book", "Какую книгу читаешь? Напиши: «читаю Sapiens стр 120»."),
}
_CLARIFY_OPTIONS: Mapping[str, List[Tuple[str, str]]] = {
    "water": [("250 мл", "v4:quick:water:250"), ("500 мл", "v4:quick:water:500"), ("1 л", "v4:quick:water:1000")],
    "energy": [("1", "v4:quick:energy:1"), ("2", "v4:quick:energy:2"), ("3", "v4:quick:energy:3"), ("4", "v4:quick:energy:4"), ("5", "v4:quick:energy:5")],
}


def _shorten(text: str, limit: int) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: max(1, limit - 1)].rstrip() + "…"


def _capitalize(text: str) -> str:
    value = str(text or "").strip()
    return value[:1].upper() + value[1:] if value else ""


def _strip_phrases(text: str, phrases: Sequence[str]) -> str:
    result = str(text or "")
    for phrase in phrases:
        if phrase:
            result = re.sub(re.escape(phrase), " ", result, flags=re.IGNORECASE)
    return " ".join(result.split()).strip(" .,-—:")


def _clean_title(text: str, *, limit: int = 120) -> str:
    title = " ".join(str(text or "").split()).strip(" .,-—:")
    title = re.sub(
        r"^\s*(?:надо|нужно|надо бы|должен|должна|должны|пожалуйста|плиз|"
        r"срочно|важно|обязательно|не забыть|напомни|напомнить|"
        r"задача|дело)\s*[:\-—]?\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"^(?:до|к)\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^(?:про|о|об)\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" .,-—:")
    return _capitalize(_shorten(title, limit))


def _task_like(text: str) -> bool:
    lowered = str(text or "").casefold()
    if re.search(r"\bне забыть\b|\bнапомни\b|\bнапомнить\b|\bзадача\b", lowered):
        return True
    for verb in TASK_VERBS:
        if re.search(r"(?<![\wА-Яа-яЁё])%s(?![\wА-Яа-яЁё])" % re.escape(verb), lowered):
            return True
    return False


def _task_title(text: str, date_phrase: str, time_phrase: str) -> str:
    title = _strip_phrases(text, [date_phrase, "до конца недели"])
    if time_phrase:
        try:
            hour_text, minute_text = time_phrase.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (TypeError, ValueError):  # pragma: no cover - формат уже проверен
            hour, minute = 0, 0
        if hour:
            title = re.sub(
                r"\b(?:в|к|во)\s+%d(?::%02d)?\b" % (hour, minute),
                " ",
                title,
                flags=re.IGNORECASE,
            )
            title = re.sub(r"\b%d:%02d\b" % (hour, minute), " ", title)
    title = re.sub(r"\b\d{1,3}\s*(?:мин|минут\w*)\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\bполчаса\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(?:утра|вечера|дня|ночи)\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"^\s*(?:про|о|об|по)\s+", "", title, flags=re.IGNORECASE)
    return _clean_title(title)


def _estimate_minutes(text: str) -> int:
    match = _TASK_ESTIMATE_RE.search(text)
    if match:
        return max(1, min(int(match.group(1)), 600))
    if _TASK_HALF_HOUR_RE.search(text):
        return 30
    if _TASK_HOUR_RE.search(text):
        return 60
    return 0


def _is_question(original: str, normalized: str) -> bool:
    if "?" in original:
        return True
    words = re.findall(r"[А-Яа-яЁё]+", normalized.casefold())
    return bool(words) and words[0] in _QUESTION_WORDS


def _period(lowered: str) -> str:
    if "месяц" in lowered:
        return "month"
    if "недел" in lowered:
        return "week"
    if "сегодня" in lowered or re.search(r"\bза\s+день\b", lowered):
        return "day"
    return "week"


# --------------------------------------------------------------------------
# RulesV4
# --------------------------------------------------------------------------


class RulesV4:
    """Разбор без сети: правила, словари, числа, даты и вопросы к БД."""

    def __init__(self, today: Optional[date] = None) -> None:
        self.today = today or date.today()

    # -- главный вход -----------------------------------------------------

    def parse(self, text: str) -> Dict[str, Any]:
        original = str(text or "").strip()
        normalized = normalize_text(original)
        base: Dict[str, Any] = {
            "intent": "unknown",
            "text": original,
            "engine": "rules",
        }
        if not original:
            return {**base, "intent": "inbox"}

        lowered = normalized.casefold().strip(" .,!?")
        mode = _mode_intent(lowered)
        if mode:
            return {**base, **mode}
        if lowered in _SMALLTALK:
            return {**base, "intent": "greeting"}

        if _DELETE_LAST_RE.search(normalized):
            return {**base, "intent": "delete_last"}

        edits = self._edits(normalized)
        if edits:
            return {**base, **edits}

        # Длинная мысль с несколькими сущностями — одно сообщение, одна запись:
        # разложить нельзя, поэтому сохраняем целиком в инбокс.
        if self._multi_entity(original, normalized):
            return {**base, "intent": "inbox"}
        if "\n" in original:
            return {
                **base,
                "intent": "journal",
                "topics": infer_topics(normalized),
                "mood": infer_mood(normalized),
            }

        # Вопросы к данным — до любых записей.
        if _SUMMARY_RE.search(normalized):
            return {**base, "intent": "finance_summary", "period": _period(normalized.casefold())}
        if _WEEK_RE.search(normalized):
            return {**base, "intent": "week"}
        if _TODAY_RE.search(normalized):
            return {**base, "intent": "today"}
        query = self._query(normalized)
        if query:
            return {**base, **query}
        if _is_question(original, normalized):
            return {
                **base,
                "intent": "query",
                "query_kind": "unknown",
                "topic": "unknown",
                "question": original,
            }

        checkin = self._checkin(normalized)
        if checkin:
            return {**base, **checkin}

        goal = self._goal_step(normalized) or self._goal_progress(normalized) or self._goal_create(normalized)
        if goal:
            return {**base, **goal}

        practice = self._practice(normalized)
        if practice:
            return {**base, **practice}

        habit = self._habit(normalized)
        if habit:
            return {**base, **habit}

        activity = _STATE_RE.search(normalized) is not None
        state = _LIFE_STATE_RE.search(normalized) is not None
        if activity or state:
            return {
                **base,
                "intent": "journal",
                "journal_kind": "activity" if activity else "state",
                "topics": infer_topics(normalized),
                "mood": infer_mood(normalized),
            }

        book = (
            self._book_quote(normalized)
            or self._book_insight(normalized)
            or self._book_action(normalized)
            or self._book_progress(normalized)
        )
        if book:
            return {**base, **book}

        workout = self._workout(normalized, original)
        if workout:
            return {**base, **workout}

        food = self._food(normalized, original)
        if food:
            return {**base, **food}

        money = self._money(normalized, explicit_only=True)
        if money:
            return {**base, **money}

        legacy = _legacy_intent(normalized)
        if legacy is not None:
            return {**base, "intent": "legacy", "legacy": legacy}

        money = self._money(normalized, explicit_only=False)
        if money:
            return {**base, **money}

        done = self._task_done(normalized)
        if done:
            return {**base, **done}

        task = self._task(normalized)
        if task:
            return {**base, **task}

        clarify = self._clarify(normalized)
        if clarify:
            return {**base, **clarify}

        if _looks_inbox(normalized):
            return {**base, "intent": "inbox"}
        return {
            **base,
            "intent": "journal",
            "topics": infer_topics(normalized),
            "mood": infer_mood(normalized),
        }

    # -- правки -----------------------------------------------------------

    def _edits(self, text: str) -> Optional[Dict[str, Any]]:
        if _RESCHEDULE_RE.search(text):
            due, phrase = parse_date_phrase(text, self.today)
            if due is None:
                due = _shift(self.today, 1)
            return {
                "intent": "reschedule",
                "due_date": due,
                "title": _clean_title(
                    _strip_phrases(text, [phrase, "перенеси", "перенести", "на", "это"])
                ),
            }
        if _MOVE_WORK_RE.search(text):
            return {
                "intent": "move_work",
                "title": _clean_title(
                    _strip_phrases(text, ["это", "в работу", "задачу", "дело", "сделай"])
                ),
            }
        if _MOVE_PERSONAL_RE.search(text):
            return {
                "intent": "move_personal",
                "title": _clean_title(
                    _strip_phrases(text, ["это", "в личное", "задачу", "дело", "сделай"])
                ),
            }
        return None

    # -- вопросы к БД -----------------------------------------------------

    def _query(self, text: str) -> Optional[Dict[str, Any]]:
        due, _phrase = parse_date_phrase(text, self.today)
        lowered = text.casefold()
        # Очки: сначала «за что дают», потом «сколько до уровня», потом итог.
        if _QUERY_POINTS_RULES_RE.search(text):
            return {"intent": "query", "query_kind": "points_rules", "topic": "points"}
        if _QUERY_LEVEL_RE.search(text):
            return {"intent": "query", "query_kind": "points_level", "topic": "points"}
        if _QUERY_POINTS_RE.search(text):
            return {"intent": "query", "query_kind": "points", "topic": "points"}
        if _MONEY_TOP_RE.search(text):
            if "недел" in lowered:
                period = "week"
            elif "сегодня" in lowered or "вчера" in lowered:
                period = "day"
            else:
                period = "month"
            return {
                "intent": "query",
                "query_kind": "spent",
                "topic": "money",
                "focus": "top",
                "period": period,
            }
        day_match = _QUERY_DAY_RE.search(text)
        if day_match:
            word = (day_match.group(1) or day_match.group(2) or "").casefold()
            offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}.get(word, 0)
            day = (self.today + timedelta(days=offset)).isoformat()
            return {
                "intent": "query",
                "query_kind": "today",
                "topic": "tasks",
                "date": day,
            }
        if _QUERY_OVERDUE_RE.search(text):
            return {"intent": "query", "query_kind": "overdue", "topic": "overdue"}
        goal_match = _QUERY_GOAL_RE.search(text)
        if goal_match and not re.search(r"\d\s*%", text):
            return {
                "intent": "query",
                "query_kind": "goal",
                "topic": "goal",
                "goal": _clean_title(goal_match.group(1), limit=80),
            }
        if _QUERY_WEIGHT_RE.search(text):
            period = "month"
            if "недел" in lowered:
                period = "week"
            return {
                "intent": "query",
                "query_kind": "weight",
                "topic": "weight",
                "period": period,
            }
        if _QUERY_NUTRITION_RE.search(text):
            macro = "kcal"
            if "белк" in lowered:
                macro = "protein"
            elif "жир" in lowered:
                macro = "fat"
            elif "углевод" in lowered:
                macro = "carbs"
            day = due or (self.today - timedelta(days=1)).isoformat()
            return {
                "intent": "query",
                "query_kind": "nutrition",
                "topic": "nutrition",
                "macro": macro,
                "date": day,
            }
        if _QUERY_JOURNAL_RE.search(text):
            period = "week" if "недел" in lowered else ("month" if "месяц" in lowered else "week")
            return {
                "intent": "query",
                "query_kind": "journal",
                "topic": "journal",
                "period": period,
            }
        if _QUERY_LAST_WORKOUT_RE.search(text):
            return {
                "intent": "query",
                "query_kind": "workout",
                "topic": "workout",
            }
        if _QUERY_HABITS_RE.search(text):
            return {
                "intent": "query",
                "query_kind": "habits",
                "topic": "habits",
            }
        if _QUERY_TASKS_RE.search(text):
            return {
                "intent": "query",
                "query_kind": "today",
                "topic": "tasks",
                "date": due or self.today.isoformat(),
            }
        if _QUERY_ENERGY_RE.search(text):
            when = due or self.today.isoformat()
            return {
                "intent": "query",
                "query_kind": "energy",
                "topic": "energy",
                "date": when,
            }
        if _QUERY_SLEEP_RE.search(text):
            period = "week" if "недел" in lowered else ("month" if "месяц" in lowered else "week")
            return {
                "intent": "query",
                "query_kind": "sleep",
                "topic": "sleep",
                "period": period,
            }
        if _QUERY_READ_RE.search(text):
            topic = ""
            match = re.search(r"\bпро\s+(.+)$", text, re.IGNORECASE)
            if match:
                topic = _clean_title(match.group(1), limit=60)
            return {
                "intent": "query",
                "query_kind": "read",
                "topic": "read",
                "term": topic,
            }
        if _QUERY_BOOKS_RE.search(text):
            return {"intent": "query", "query_kind": "books", "topic": "books"}
        return None

    # -- несколько сущностей в одном сообщении ----------------------------

    def _multi_entity(self, original: str, normalized: str) -> bool:
        if "\n" not in original:
            return False
        signals = 0
        if re.search(
            r"\b(?:спал[аи]?|сон|вес[а]?|жир|жим|присед|вода|выпил[аи]?|температур\w*)\b",
            normalized,
            re.IGNORECASE,
        ):
            signals += 1
        if _contains_any(normalized.casefold(), SPEND_WORDS) or _contains_any(
            normalized.casefold(), INCOME_GENERIC_WORDS
        ):
            signals += 1
        if _CHECKIN_RE.search(normalized):
            signals += 1
        if _KCAL_RE.search(normalized) or re.search(
            r"\d+\s*(?:г|гр|грамм\w*)\b", normalized, re.IGNORECASE
        ):
            signals += 1
        if parse_date_phrase(normalized, self.today)[0] or _task_like(normalized):
            signals += 1
        return signals >= 2

    # -- чек-ин -----------------------------------------------------------

    def _checkin(self, text: str) -> Optional[Dict[str, Any]]:
        match = _CHECKIN_RE.search(text)
        has_head = _CHECKIN_HEAD_RE.search(text) is not None
        if not match and not has_head:
            return None
        energy = int(match.group(1)) if match else 0
        note = _strip_phrases(text, [match.group(0) if match else ""])
        note = re.sub(r"^[,.;:\-\s]+", "", note)
        return {
            "intent": "checkin",
            "energy": energy,
            "mood": infer_mood(text),
            "note": _shorten(note, 300),
        }

    # -- цели -------------------------------------------------------------

    def _goal_step(self, text: str) -> Optional[Dict[str, Any]]:
        if not _GOAL_STEP_DONE_RE.search(text):
            return None
        goal = ""
        match = re.search(
            r"(?:к\s+)?цели\s+([^,.;?]+?)\s+(?:выполн|готова|готов|сделана|закрыта|закрыл|заверш)",
            text,
            re.IGNORECASE,
        )
        if match:
            goal = _clean_title(match.group(1), limit=80)
        step = ""
        quoted = re.findall(r"[«\"']([^»\"']+)[»\"']", text)
        if quoted:
            step = _clean_title(quoted[-1], limit=120)
        if not step:
            match = re.search(
                r"\bвех[аиу]\s+(.+?)\s*(?:выполн\w*|готова|готов\w*|сделана|закрыта|"
                r"закрыл\w*|закрой\w*|отметь\w*|заверш\w*|done)?$",
                text,
                re.IGNORECASE,
            )
            if match:
                candidate = _clean_title(match.group(1), limit=120)
                lowered_candidate = candidate.casefold()
                if candidate and not lowered_candidate.startswith(("к цели", "цели ")):
                    if lowered_candidate not in {"выполнена", "выполнено", "сделана", "закрыта"}:
                        step = candidate
        if not goal:
            match = re.search(
                r"\bвех[аиу]\s+(?:к\s+)?цели\s+(.+?)$", text, re.IGNORECASE
            )
            if match:
                goal = _clean_title(match.group(1), limit=80)
        if goal.casefold().startswith("к цели"):
            goal = goal[6:].strip()
        return {"intent": "goal_step_done", "goal": goal, "step": step}

    def _goal_progress(self, text: str) -> Optional[Dict[str, Any]]:
        delta = 0
        goal = ""
        mode = "delta"
        match = _GOAL_DELTA_RE.search(text)
        if match:
            delta = int(match.group(1))
            goal = _clean_title(match.group(2), limit=80)
        if not goal:
            match = _GOAL_PROGRESS_RE.search(text)
            if match:
                goal = _clean_title(match.group(1), limit=80)
                delta = int(match.group(2))
                mode = "set"
        if not goal:
            match = _GOAL_AFTER_RE.search(text)
            if match:
                goal = _clean_title(match.group(1), limit=80)
                delta = int(match.group(2))
        if not goal:
            match = _GOAL_BARE_RE.search(text)
            if match:
                goal = _clean_title(match.group(2), limit=80)
                delta = int(match.group(1))
        if not goal or not delta:
            return None
        return {"intent": "goal_progress", "goal": goal, "delta": delta, "mode": mode}

    def _goal_create(self, text: str) -> Optional[Dict[str, Any]]:
        match = _GOAL_CREATE_RE.match(text)
        if not match:
            return None
        body = (match.group(1) or match.group(2) or "").strip()
        if not body:
            return None
        return {
            "intent": "create_goal",
            "title": _clean_title(body, limit=200),
            "text": text,
            "horizon": "year",
        }

    # -- практики ---------------------------------------------------------

    def _practice(self, text: str) -> Optional[Dict[str, Any]]:
        """Практика: «подышал», «3 раунда», «задержка 2 минуты», «вим хоф».

        Круги и задержка считаются и без слова «дыхание»: «3 раунда» и
        «задержка 2 минуты» — это та же практика. Ограда от спорта: «пробежал
        3 круга» и «3 подхода со штангой» практикой не считаются.
        """
        lowered = text.casefold()
        vim = _VIM_HOF_RE.search(text)
        looks_practice = _PRACTICE_RE.search(text) is not None
        if _PRACTICE_NAMED_RE.search(text) is not None:
            looks_practice = True
        rounds_match = _PRACTICE_ROUNDS_RE.search(text)
        bare_rounds_match = _PRACTICE_BARE_ROUNDS_RE.search(text)
        hold_s = _practice_hold_seconds(text)
        not_practice = _NOT_PRACTICE_RE.search(text) is not None
        bare_rounds = bare_rounds_match is not None and not looks_practice and not not_practice
        bare_hold = hold_s > 0 and not not_practice
        if not vim and not looks_practice and not bare_rounds and not bare_hold:
            return None
        if not vim and not bare_rounds and not bare_hold and not (
            _PRACTICE_DONE_RE.search(text)
            or "дыха" in lowered
            or "дыш" in lowered
            or "медитац" in lowered
            or _PRACTICE_NAMED_RE.search(text) is not None
        ):
            return None
        rounds = int(rounds_match.group(1)) if rounds_match else 0
        if bare_hold and not rounds:
            rounds = 1
        if vim:
            slug, title = "wim-hof", "Дыхание Вим Хофа"
        elif "4-7-8" in lowered or "4 7 8" in lowered or "478" in lowered:
            slug, title = "breath-478", "Дыхание 4-7-8"
        elif "бокс" in lowered:
            slug, title = "box-breathing", "Боксовое дыхание"
        elif "5-5" in lowered or "5 5" in lowered or "медитац" in lowered:
            slug, title = "breath-55", "Дыхание 5-5"
        elif "растяж" in lowered:
            slug, title = "stretch-7", "Растяжка 7 минут"
        elif "холодн" in lowered or "душ" in lowered:
            slug, title = "cold-shower-60", "Холодный душ 60 секунд"
        else:
            # «подышал», «3 раунда», «задержка 2 минуты», «дыхание» без уточнения —
            # это основная практика (3 раунда Вима Хофа), а не 4-7-8.
            slug, title = "wim-hof", "Дыхание Вим Хофа"
        return {
            "intent": "practice",
            "practice": title,
            "slug": slug,
            "rounds": rounds,
            "best_hold_s": hold_s,
            "date": self.today.isoformat(),
            "text": text,
        }

    # -- привычки ---------------------------------------------------------

    def _habit(self, text: str) -> Optional[Dict[str, Any]]:
        match = _HABIT_WATER_RE.search(text)
        if match:
            amount = _number(match.group(1))
            unit = match.group(2).casefold()
            ml = int(round(amount * 1000)) if unit.startswith("л") or unit == "l" else int(round(amount))
            return {
                "intent": "habit",
                "habit": "Вода 2 литра",
                "emoji": "💧",
                "amount": amount if ml % 1000 else ml // 1000,
                "unit": "л" if ml % 1000 == 0 else "мл",
                "water_ml": ml,
            }
        match = _HABIT_RUN_RE.search(text)
        if match:
            distance = _number(match.group(1))
            return {
                "intent": "habit",
                "habit": "Бег",
                "emoji": "🏃",
                "amount": distance,
                "unit": "км",
            }
        match = _HABIT_WALK_MIN_RE.search(text)
        if match:
            return {
                "intent": "habit",
                "habit": "Прогулка 8 тысяч шагов",
                "emoji": "🚶",
                "amount": int(match.group(1)),
                "unit": "мин",
            }
        match = _HABIT_WALK_RE.search(text)
        if match:
            amount = _number(match.group(1))
            unit = match.group(2).casefold()
            return {
                "intent": "habit",
                "habit": "Прогулка 8 тысяч шагов",
                "emoji": "🚶",
                "amount": amount * 1000 if "тыс" in unit else amount,
                "unit": "шагов" if "шаг" in unit else "км",
            }
        match = _HABIT_READ_RE.search(text)
        if match:
            return {
                "intent": "habit",
                "habit": "Чтение 20 минут",
                "emoji": "📖",
                "amount": int(match.group(1)),
                "unit": "мин",
            }
        if _HABIT_EXERCISE_RE.search(text):
            return {"intent": "habit", "habit": "Зарядка", "emoji": "🤸", "amount": 0, "unit": ""}
        if _HABIT_NO_SMOKE_RE.search(text):
            return {"intent": "habit", "habit": "Не курить", "emoji": "🚭", "amount": 0, "unit": ""}
        if _HABIT_NO_DRINK_RE.search(text):
            return {"intent": "habit", "habit": "Без алкоголя", "emoji": "🚫", "amount": 0, "unit": ""}
        if _HABIT_NO_SUGAR_RE.search(text):
            return {"intent": "habit", "habit": "Без сахара", "emoji": "🍬", "amount": 0, "unit": ""}
        if _HABIT_STRETCH_RE.search(text):
            return {
                "intent": "habit",
                "habit": "Растяжка перед сном",
                "emoji": "🧘",
                "amount": 0,
                "unit": "",
            }
        match = _HABIT_MARK_RE.search(text)
        if match:
            name = _clean_title(match.group(1), limit=80)
            if name:
                return {"intent": "habit", "habit": name, "amount": 0, "unit": ""}
        return None

    # -- книги ------------------------------------------------------------

    def _book_quote(self, text: str) -> Optional[Dict[str, Any]]:
        match = _QUOTE_RE.match(text)
        if not match:
            return None
        body = match.group(1).strip()
        page = None
        page_match = _BOOK_PAGE_RE.search(body)
        if page_match:
            page = int(page_match.group(1))
            body = _strip_phrases(body, [page_match.group(0)])
        book = ""
        book_match = re.search(
            r"\bиз\s+книги\s+[«\"']?([^»\"',.;]+)[»\"']?", body, re.IGNORECASE
        )
        if book_match:
            book = _clean_title(book_match.group(1), limit=120)
            body = _strip_phrases(body, [book_match.group(0)])
        body = body.strip(" .,-—:")
        if not body:
            return None
        return {"intent": "book_quote", "text": body, "page": page, "book": book}

    def _book_insight(self, text: str) -> Optional[Dict[str, Any]]:
        match = _INSIGHT_RE.match(text)
        if not match:
            return None
        body = match.group(1).strip()
        page = None
        page_match = _BOOK_PAGE_RE.search(body)
        if page_match:
            page = int(page_match.group(1))
            body = _strip_phrases(body, [page_match.group(0)])
        book = ""
        book_match = re.search(
            r"\bиз\s+книги\s+[«\"']?([^»\"',.;]+)[»\"']?", body, re.IGNORECASE
        )
        if book_match:
            book = _clean_title(book_match.group(1), limit=120)
            body = _strip_phrases(body, [book_match.group(0)])
        body = body.strip(" .,-—:")
        if not body:
            return None
        return {"intent": "book_insight", "text": body, "page": page, "book": book}

    def _book_action(self, text: str) -> Optional[Dict[str, Any]]:
        match = _BOOK_DONE_RE.match(text)
        if match:
            body = match.group(1).strip()
            rating = None
            rating_match = re.search(r"(\d)\s*(?:из|\/)\s*5\b", body)
            if rating_match:
                rating = int(rating_match.group(1))
                body = _strip_phrases(body, [rating_match.group(0)])
            book = _clean_title(body.strip(" ,.;-—"), limit=120)
            if book:
                result = {
                    "intent": "book_progress",
                    "book": book,
                    "page": 0,
                    "pages_total": 0,
                    "book_status": "done",
                    "status": "done",
                    "shelf": "done",
                }
                if rating is not None:
                    result["rating"] = rating
                    result["metrics"] = {"rating": rating}
                return result
        match = _BOOK_BUY_RE.match(text)
        if match:
            book = _clean_title(match.group(1), limit=120)
            if book:
                return {
                    "intent": "create_book",
                    "book": book,
                    "book_status": "queue",
                    "status": "queue",
                    "shelf": "queue",
                    "text": text,
                }
        match = _BOOK_START_RE.match(text)
        if match:
            book = _clean_title(match.group(1), limit=120)
            if book:
                return {
                    "intent": "create_book",
                    "book": book,
                    "book_status": "reading",
                    "status": "reading",
                    "shelf": "reading",
                    "text": text,
                }
        match = _BOOK_DROP_RE.match(text)
        if match:
            book = _clean_title(match.group(1), limit=120)
            if book:
                return {
                    "intent": "book_progress",
                    "book": book,
                    "page": 0,
                    "pages_total": 0,
                    "book_status": "dropped",
                    "status": "dropped",
                    "shelf": "dropped",
                }
        return None

    def _book_progress(self, text: str) -> Optional[Dict[str, Any]]:
        pages_only = _BOOK_PAGES_ONLY_RE.match(text)
        if pages_only:
            return {
                "intent": "book_progress",
                "book": "",
                "page": int(pages_only.group(1)),
                "pages_total": 0,
            }
        pages_match = _BOOK_READ_PAGES_RE.match(text)
        if pages_match:
            return {
                "intent": "book_progress",
                "book": _clean_title(pages_match.group(2), limit=120),
                "page": int(pages_match.group(1)),
                "pages_total": 0,
            }
        page = None
        total = None
        page_match = _BOOK_PAGE_RE.search(text)
        if page_match:
            page = int(page_match.group(1))
            total = int(page_match.group(2)) if page_match.group(2) else 0
        prefix = _BOOK_PREFIX_RE.match(text)
        if not prefix and page is None:
            return None
        book = ""
        if prefix:
            book = _clean_title(
                _strip_phrases(prefix.group(1), [page_match.group(0)] if page_match else []),
                limit=120,
            )
        if not book and page_match:
            book = _clean_title(
                _strip_phrases(text, [page_match.group(0)]), limit=120
            )
        if not book and page is None:
            return None
        if not prefix and not page_match:
            return None
        if not page and re.search(r"\bмин\b|\bккал\b|\bкг\b|\bкм\b", book):
            return None
        return {
            "intent": "book_progress",
            "book": book,
            "page": page or 0,
            "pages_total": total or 0,
        }

    # -- подходы и повторы (до еды) ---------------------------------------

    def _workout(self, text: str, original: str) -> Optional[Dict[str, Any]]:
        match = _WORKOUT_SETS_RE.search(text)
        if not match:
            return None
        sets = int(match.group(1))
        reps = int(match.group(2))
        exercise_match = _WORKOUT_EXERCISE_RE.search(text)
        exercise = _capitalize(exercise_match.group(1)) if exercise_match else "Упражнение"
        legacy = {
            "kind": "workout",
            "title": "%s %dx%d" % (exercise, sets, reps),
            "text": original,
            "category": "Тренировка",
            "metrics": {"reps": reps, "sets": sets, "exercise": exercise},
            "engine": "rules",
        }
        return {"intent": "legacy", "legacy": legacy}

    # -- еда (до денег) ---------------------------------------------------

    def _food(self, text: str, original: str) -> Optional[Dict[str, Any]]:
        """Еда: «на обед гречка 200 г и курица 150». Возвращает legacy-запись.

        Разбор общий с :func:`parse_food`, чтобы страховка в обработчике
        («модель назвала еду мыслью») видела ровно то же, что и правила.
        """
        parsed = parse_food(text)
        if parsed is None:
            return None
        items = parsed["food_items"]
        meal_label = parsed["meal"]
        meal_code = _MEAL_LABELS.get(meal_label, "")
        metrics: Dict[str, float] = {}
        for key in ("kcal", "protein", "fat", "carbs"):
            if parsed.get(key) is not None:
                metrics["calories" if key == "kcal" else key] = parsed[key]
        if parsed.get("grams_total"):
            metrics["grams_total"] = parsed["grams_total"]
        parts = []
        for item in items[:6]:
            grams = item["grams"]
            parts.append(
                "%s %s г" % (_capitalize(item["name"]), _format_plain_number(grams))
                if grams
                else _capitalize(item["name"])
            )
        if parsed.get("kcal"):
            parts.append("%s ккал" % _format_plain_number(parsed["kcal"]))
        if parsed.get("protein"):
            parts.append("белок %s г" % _format_plain_number(parsed["protein"]))
        if meal_label:
            parts.append(meal_label)
        title = ", ".join(parts) or "Еда"
        legacy = {
            "kind": "food",
            "title": _shorten(_capitalize(title), 120),
            "text": original,
            "category": "Питание",
            "metrics": metrics,
            "grams_total": parsed["grams_total"],
            "items": [item["name"] for item in items][:12],
            "food_items": [{"name": item["name"], "grams": item["grams"]} for item in items][:12],
            "meal": meal_code,
            "engine": "rules",
        }
        return {"intent": "legacy", "legacy": legacy}

    # -- деньги -----------------------------------------------------------

    def _money(self, text: str, *, explicit_only: bool) -> Optional[Dict[str, Any]]:
        lowered = text.casefold()
        income_category = infer_category(text, "income")
        income = _contains_any(lowered, INCOME_GENERIC_WORDS) or bool(income_category)
        spent = _contains_any(lowered, SPEND_WORDS)
        currency = _CURRENCY_RE.search(text) is not None
        category_hint = bool(infer_category(text, "income" if income else "expense"))
        if explicit_only:
            if not (currency or spent or income):
                return None
        else:
            if not category_hint:
                return None
            if _task_like(text) and not spent:
                return None
            if not spent and parse_date_phrase(text, self.today)[0] is not None:
                return None
            if not spent and parse_time_phrase(text) is not None:
                return None
        amount_kopecks, _fragment = parse_amount(text)
        if not amount_kopecks or amount_kopecks <= 0:
            return None
        kind = "income" if income else "expense"
        category = infer_category(text, kind)
        account = infer_account(text)
        due, _phrase = parse_date_phrase(text, self.today)
        result = {
            "intent": kind,
            "amount_kopecks": amount_kopecks,
            "category": category,
            "account": account,
            "accountName": account_title(account),
            "note": _clean_title(text, limit=200),
        }
        if due and due <= self.today.isoformat():
            result["date"] = due
        return result

    # -- дела -------------------------------------------------------------

    def _task(self, text: str) -> Optional[Dict[str, Any]]:
        stripped = _TASK_PREFIX_RE.sub("", text)
        due, date_phrase = parse_date_phrase(stripped, self.today)
        time_phrase = parse_time_phrase(stripped) or ""
        estimate = _estimate_minutes(stripped)
        task_like = _task_like(stripped) or bool(due) or bool(time_phrase)
        if not task_like:
            return None
        title = _task_title(stripped, date_phrase, time_phrase)
        if not title or len(title) < 2:
            return None
        resource = "important"
        if _contains_any(stripped, MUST_WORDS):
            resource = "must"
        elif _contains_any(stripped, LOW_WORDS):
            resource = "low"
        sphere = infer_sphere(title)
        return {
            "intent": "task",
            "title": title,
            "due_date": due or self.today.isoformat(),
            "start_time": time_phrase,
            "resource": resource,
            "minutes": estimate,
            "sphere": sphere,
            "list_kind": "personal" if sphere == "Личное и семья" else "work",
        }

    def _task_done(self, text: str) -> Optional[Dict[str, Any]]:
        if "\n" in text or len(text) > 80:
            return None
        match = _TASK_DONE_RE.search(text)
        if not match:
            return None
        if parse_date_phrase(text, self.today)[0] is not None:
            return None
        title = _clean_title(
            _strip_phrases(match.group(1), ["это", "задачу", "дело", "пожалуйста"]),
            limit=120,
        )
        return {"intent": "task_done", "title": title}

    # -- уточнение --------------------------------------------------------

    def _clarify(self, text: str) -> Optional[Dict[str, Any]]:
        key = text.casefold().strip(" .,!?:;")
        if key not in _SHORT_STARTERS:
            return None
        mode, question = _SHORT_STARTERS[key]
        result: Dict[str, Any] = {
            "intent": "clarify",
            "clarification": True,
            "mode": mode,
            "question": question,
            "text": text,
        }
        options = _CLARIFY_OPTIONS.get(mode)
        if options:
            result["options"] = [list(option) for option in options]
        return result


# --------------------------------------------------------------------------
# Вопросы без ответа, мусор, инбокс
# --------------------------------------------------------------------------


def _number_match(pattern: "re.Pattern[str]", text: str) -> Optional[float]:
    match = pattern.search(text)
    if match is None:
        return None
    return _number(match.group(1))


def _looks_inbox(text: str) -> bool:
    """Фраза без смысла: мусор, латиница-абракадабра, «не знаю что»."""
    lowered = text.casefold()
    if _UNCERTAIN_RE.search(lowered):
        return True
    words = re.findall(r"[А-Яа-яЁёA-Za-z]+", lowered)
    if not words:
        return True
    latin = [word for word in words if re.fullmatch(r"[a-z]+", word)]
    if len(latin) == len(words) and not _contains_any(lowered, ("ok", "google", "telegram")):
        return True
    if len(words) <= 3:
        known = _known_signal(lowered)
        return not known
    return False


def _known_signal(lowered: str) -> bool:
    if re.search(r"\d", lowered):
        return True
    if re.search(
        r"\b(?:спал|сон|вес|жир|вода|выпил|жим|присед|трениров|бег|пробеж|"
        r"устал|энерг|настроен|мысли|мысль|книг|читаю|читал|дел|задач|"
        r"позвон|купить|проверить|записать|привычк|цель|вех|ед|обед|завтрак|ужин)\b",
        lowered,
    ):
        return True
    if any(word in lowered for words in SPHERE_KEYWORDS.values() for word in words):
        return True
    if any(word in lowered for words in CATEGORY_KEYWORDS.values() for word in words):
        return True
    if any(word in lowered for words in TOPIC_KEYWORDS.values() for word in words):
        return True
    if any(word in lowered for word in ("отдых", "отпуск", "устал", "радост", "тревог")):
        return True
    return False


# --------------------------------------------------------------------------
# v3 fallback
# --------------------------------------------------------------------------


def _legacy_intent(text: str) -> Optional[Dict[str, Any]]:
    """Разбор старых фраз v3 (вес, вода, сон, еда, тренировка, настроение)."""
    try:
        from app.bot.intent import ScriptedFallback

        parsed = ScriptedFallback().parse(text)
    except Exception:  # pragma: no cover - rules не должны падать
        return None
    if not isinstance(parsed, Mapping):
        return None
    metrics = parsed.get("metrics") or {}
    if parsed.get("kind") in {"note"} and not metrics:
        return None
    return dict(parsed)


def _mode_intent(lowered: str) -> Optional[Dict[str, Any]]:
    key = lowered.strip(" .!?")
    if key in MODE_HINTS:
        intent, hint = MODE_HINTS[key]
        if intent == "today":
            return {"intent": "today"}
        return {"intent": "mode_hint", "mode": intent, "hint": hint, "clarification": False}
    return None


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------

_LLM_INTENTS = INTENTS - {"legacy", "unknown", "clarify", "inbox"}
_LLM_ALLOWED_STR = {
    "title": 200,
    "note": 300,
    "category": 80,
    "account": 20,
    "accountName": 80,
    "sphere": 80,
    "project": 120,
    "goal": 120,
    "step": 200,
    "book": 200,
    "habit": 120,
    "text": 4000,
    "mood": 60,
    "period": 10,
    "due_date": 10,
    "date": 10,
    "start_time": 5,
    "unit": 20,
    "resource": 20,
    "list_kind": 20,
    "mode": 10,
    "query_kind": 20,
    "topic": 20,
    "term": 60,
    "focus": 12,
    "macro": 12,
    "question": 300,
    "book_status": 20,
    "meal": 20,
    "note": 300,
}
_LLM_ALLOWED_INT = {
    "kcal": 10_000,
    "protein": 1_000,
    "fat": 1_000,
    "carbs": 1_000,
    "grams": 3_000,
    "amount": 100_000_000,
    "amount_kopecks": 10_000_000_000,
    "delta": 1000,
    "minutes": 600,
    "energy": 5,
    "page": 1_000_000,
    "pages_total": 1_000_000,
    "pages": 1_000_000,
    "rating": 5,
}

SYSTEM_PROMPT = """Ты — разборщик сообщений личного ассистента Life OS (русский язык).
Верни ТОЛЬКО один JSON-объект без пояснений, markdown и комментариев.

Допустимые intent и поля:
{"intent":"task","title":"Позвонить в банк","due_date":"YYYY-MM-DD","start_time":"HH:MM","sphere":"финансы","project":"","resource":"must|important|low","minutes":15}
{"intent":"task_done","title":"позвонить в банк"}
{"intent":"goal_progress","goal":"Здоровье","delta":10,"mode":"delta"}
{"intent":"goal_step_done","goal":"Здоровье","step":"Взвешиваться"}
{"intent":"create_goal","title":"Прочитать 12 книг","text":"цель: прочитать 12 книг за год"}
{"intent":"expense|income","amount":450,"category":"Транспорт","account":"cash|card|savings|broker","note":"такси"}
{"intent":"finance_summary","period":"day|week|month"}
{"intent":"query","query_kind":"today|spent|sleep|energy|read|books|goal|weight|nutrition|journal|workout|habits|overdue|unknown","topic":"money|tasks|overdue|goal|weight|nutrition|sleep|energy|read|books|journal|workout|habits|unknown","date":"YYYY-MM-DD","period":"day|week|month","term":"","goal":"Здоровье","focus":"top","macro":"kcal|protein|fat|carbs","question":""}
{"intent":"book_progress","book":"Sapiens","page":120,"pages_total":0,"book_status":"reading|done|dropped","rating":5}
{"intent":"create_book","book":"Атомные привычки","book_status":"queue|reading"}
{"intent":"book_quote","text":"цитата","book":"Sapiens","page":42}
{"intent":"book_insight","text":"мысль","book":"Sapiens"}
{"intent":"journal","text":"мысль","topics":["работа"],"mood":"усталость"}
{"intent":"habit","habit":"Вода 2 литра","amount":2,"unit":"литр"}
{"intent":"practice","practice":"Дыхание Вим Хофа","rounds":2}
{"intent":"legacy","kind":"weight|note|workout","title":"Вес 81,2","metrics":{"weight":81.2,"fat":18.5,"muscle":35,"sleep":7.5,"water":300,"calories":600,"protein":40,"duration_min":45,"mood":8}}
{"intent":"food","meal":"breakfast|lunch|dinner|snack","items":[{"name":"гречка","grams":200},{"name":"курица","grams":150}]}
{"intent":"food","kcal":600,"protein":40,"meal":"lunch"}
{"intent":"checkin","energy":4,"mood":"усталость","note":"устал после созвонов"}
{"intent":"reschedule","due_date":"YYYY-MM-DD","title":""}
{"intent":"move_work|move_personal","title":""}
{"intent":"delete_last"}
{"intent":"today|week"}

Правила:
- Одно сообщение — одно намерение. Не выдумывай полей, которых нет в сообщении.
- Еда — это food, а не journal и не inbox: «на обед гречка 200 г и курица 150»,
  «банан 100», «творог 200 и 18 белка», «съел 600 ккал». Продукт + количество —
  всегда food, даже без слова «съел». Никогда не превращай еду в мысль.
- В items каждый продукт — отдельной позицией со своими граммами («гречка 200 и
  курица 150» — две позиции); «кг» переводи в граммы (0,5 кг = 500). Если названы
  только калории/белки без продуктов — food с kcal/protein и пустым items.
- Вопросы про еду («сколько калорий вчера») — это query, запись не создавай.
- Сегодня {today}. «завтра» = {tomorrow}, «послезавтра» = {day_after}.
- amount и page — целые числа; деньги в рублях, без копеек, если их не назвали.
- Вопросы к данным (сколько, какие) — intent=query, записей не создавай.
- Всегда добавляй "confidence": число от 0 до 1 — насколько уверен в разборе.
- Не уверен — не гадай и не отправляй непустую фразу в inbox: верни
  {"intent":"clarify","confidence":0.3,"question":"один короткий вопрос",
   "options":[{"label":"вариант","intent":"...","title":"..."}]}.
  В options — 2–4 правдоподобных варианта; в каждом свои поля.
- Если для записи не хватает суммы, даты или названия — не выдумывай их:
  верни clarify с вопросом («Сколько стоило такси?»).
- Исходный текст человека сохраняй в "text": запись остаётся его формулировкой,
  даже если ты понял опечатку.
- Никаких советов, оценок и лишнего текста."""


def _llm_food_items(data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Позиции еды из ответа модели: название + граммы, только правдоподобное.

    Модель может назвать поле ``items``, ``food``, ``products`` — принимаем
    все варианты, но берём только то, что похоже на продукт с граммами:
    название 2–80 символов, граммы 0–3000 (0 — «граммовка не названа»).
    """
    raw: Any = None
    for key in ("items", "food", "products", "food_items"):
        value = data.get(key)
        if isinstance(value, list):
            raw = value
            break
    if raw is None:
        return []
    items: List[Dict[str, Any]] = []
    for entry in raw[:12]:
        if isinstance(entry, str):
            name: Any = entry
            grams: Any = 0
        elif isinstance(entry, Mapping):
            name = entry.get("name") or entry.get("product") or entry.get("title") or ""
            grams = (
                entry.get("grams")
                or entry.get("gram")
                or entry.get("g")
                or entry.get("weight")
                or entry.get("grams_total")
                or 0
            )
        else:
            continue
        clean_name = " ".join(str(name or "").split())[:80]
        if len(clean_name) < 2:
            continue
        try:
            value = float(str(grams).replace(",", ".").replace(" ", ""))
        except (TypeError, ValueError):
            value = 0.0
        if not (0 <= value <= 3000):
            continue
        items.append({"name": clean_name, "grams": round(value, 1)})
    return items


def _validate_core(data: Any, original: str, today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """Проверяет один вариант разбора (без confidence/ask)."""
    if not isinstance(data, Mapping):
        return None
    intent = str(data.get("intent") or data.get("action") or "").strip().casefold()
    if intent == "legacy":
        # Замеры/сон/вода/тренировка: модель отдаёт metrics, проверяем их
        # тем же валидатором, что и старый v3-разбор (единицы и диапазоны).
        from app.bot.intent import validate_intent

        metrics: Dict[str, Any] = {}
        raw_metrics = data.get("metrics")
        if isinstance(raw_metrics, Mapping):
            metrics.update(dict(raw_metrics))
        for key in ("weight", "fat", "muscle", "water", "sleep", "calories", "protein", "duration_min", "mood"):
            if key in data and key not in metrics:
                metrics[key] = data[key]
        inner = validate_intent(
            {
                "kind": str(data.get("kind") or data.get("legacy_kind") or "").strip().lower(),
                "title": str(data.get("title") or ""),
                "text": str(original or "")[:4000],
                "category": str(data.get("category") or ""),
                "metrics": metrics,
            },
            str(original or ""),
        )
        if inner is None:
            return None
        return {
            "intent": "legacy",
            "text": str(original or "")[:4000],
            "legacy": inner,
            "engine": "llm",
        }
    if intent not in _LLM_INTENTS:
        return None
    # Вопрос никогда не создаёт запись, даже если модель ошиблась.
    if _is_question(str(original or ""), str(original or "")) and intent not in {
        "query",
        "finance_summary",
        "today",
        "week",
        "clarify",
        "inbox",
    }:
        return None
    clean: Dict[str, Any] = {"intent": intent, "text": str(original or "")[:4000], "engine": "llm"}
    for key, limit in _LLM_ALLOWED_STR.items():
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            clean[key] = value.strip()[:limit]
    for key, limit in _LLM_ALLOWED_INT.items():
        value = data.get(key)
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= number <= limit:
            clean[key] = number
    topics = data.get("topics")
    if isinstance(topics, list):
        clean["topics"] = [str(item)[:40] for item in topics[:8] if str(item).strip()]

    if intent == "food":
        items = _llm_food_items(data)
        if items:
            clean["items"] = items
        meal = str(clean.get("meal") or "").strip().casefold()
        if meal in _MEAL_CODES:
            clean["meal"] = meal
        elif meal in _MEAL_LABELS:
            clean["meal"] = _MEAL_LABELS[meal]
        else:
            clean.pop("meal", None)
        if not items and not clean.get("kcal"):
            # Еда без позиций и без калорий — модели не поверили, работают правила.
            return None

    for key in ("due_date", "date"):
        due = clean.get(key)
        if not due:
            continue
        try:
            parsed = date.fromisoformat(due[:10])
        except ValueError:
            clean.pop(key, None)
        else:
            base = today or date.today()
            if not (base - timedelta(days=365) <= parsed <= base + timedelta(days=365 * 3)):
                clean.pop(key, None)
            else:
                clean[key] = parsed.isoformat()
    if "amount" in clean and "amount_kopecks" not in clean:
        clean["amount_kopecks"] = _to_kopecks(float(clean["amount"]))
    resource = clean.get("resource")
    if resource not in {"must", "important", "low"}:
        clean.pop("resource", None)
    list_kind = clean.get("list_kind")
    if list_kind not in {"work", "personal"}:
        clean.pop("list_kind", None)
    if intent == "task" and not clean.get("title"):
        return None
    if intent in {"expense", "income"} and not clean.get("amount_kopecks"):
        return None
    if intent == "checkin" and not (1 <= int(clean.get("energy") or 0) <= 5):
        return None
    if intent == "create_book" and not clean.get("book"):
        return None
    if intent == "create_goal" and not clean.get("title"):
        return None
    return clean


#: Ниже этой уверенности модель обязана спросить, а не записывать наугад.
MODEL_CONFIDENCE_MIN = 0.55

#: Порог уверенности правил: ниже — фразу отдаём модели вторым слоем.
RULES_CONFIDENCE_MIN = 0.7

#: Короткие подписи действий для вопроса «как сохранить?».
_INTENT_LABELS: Mapping[str, str] = {
    "task": "Это дело",
    "expense": "Это расход",
    "income": "Это доход",
    "food": "Это еда",
    "journal": "Это мысль",
    "checkin": "Это чек-ин",
    "habit": "Это привычка",
    "practice": "Это практика",
    "book_quote": "Это цитата",
    "book_insight": "Это выжимка",
    "book_progress": "Это прогресс книги",
    "create_book": "Добавить книгу",
    "create_goal": "Это цель",
    "goal_progress": "Это прогресс цели",
    "query": "Это вопрос",
    "finance_summary": "Это вопрос о деньгах",
}


def _model_confidence(data: Mapping[str, Any], default: float = 0.8) -> float:
    """Уверенность модели из ответа; мусор/диапазон — значение по умолчанию."""
    for key in ("confidence", "certainty"):
        raw = data.get(key)
        if isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 1.0:
            return value
    return default


def _clarify_from_ask(
    data: Mapping[str, Any],
    original: str,
    today: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Вопрос с кнопками из поля ``ask`` ответа модели."""
    raw = data.get("ask")
    if not isinstance(raw, Mapping):
        # Модель может положить вопрос и варианты на верхний уровень —
        # именно так просит подсказка ({"intent":"clarify","question":…}).
        raw = data if isinstance(data.get("options"), (list, tuple)) else None
    if not isinstance(raw, Mapping):
        return None
    question = str(raw.get("question") or data.get("question") or "").strip()
    if not question:
        return None
    options: List[Dict[str, Any]] = []
    raw_options = raw.get("options")
    if not isinstance(raw_options, (list, tuple)):
        raw_options = []
    for entry in list(raw_options)[:4]:
        if isinstance(entry, str):
            label = entry
            candidate: Dict[str, Any] = {"intent": "journal", "text": original}
        elif isinstance(entry, Mapping):
            label = str(entry.get("label") or entry.get("title") or "").strip()
            candidate = {
                key: value
                for key, value in entry.items()
                if key not in {"label", "confidence", "ask"}
            }
        else:
            continue
        if not label:
            continue
        parsed = _validate_core(candidate, original, today)
        if parsed is None:
            continue
        options.append({"label": label[:40], "parsed": parsed})
    if not options:
        return None
    return {
        "intent": "clarify",
        "text": str(original or "")[:4000],
        "engine": "llm",
        "question": question[:300],
        "options": options,
        "clarification": True,
    }


def _clarify_from_model(
    data: Any,
    original: str,
    today: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Низкая уверенность модели -> один вопрос с кнопками, а не Инбокс."""
    if not isinstance(data, Mapping):
        return None
    confidence = _model_confidence(data)
    asked = _clarify_from_ask(data, original, today)
    if asked is not None:
        return asked
    intent = str(data.get("intent") or data.get("action") or "").strip().casefold()
    has_confidence = "confidence" in data or "certainty" in data
    if intent in {"clarify", "inbox", "unknown"}:
        pass
    elif has_confidence and confidence < MODEL_CONFIDENCE_MIN:
        pass
    else:
        # Мусор вместо разбора (ни intent, ни уверенности) — не вопрос, а откат.
        return None

    candidate = _validate_core(data, original, today)
    question = str(data.get("question") or "").strip()
    options: List[Dict[str, Any]] = []
    if candidate is not None:
        label = _INTENT_LABELS.get(candidate.get("intent", ""), "Это запись")
        options.append({"label": label, "parsed": candidate})
        if not question:
            question = "Так понял: %s. Сохранить?" % label.lower()
    if not question:
        question = "Не уверен, что это. Сохранить как мысль?"
    options.append(
        {"label": "Это мысль", "parsed": {"intent": "journal", "text": original}}
    )
    options.append(
        {"label": "В инбокс", "parsed": {"intent": "inbox", "text": original}}
    )
    return {
        "intent": "clarify",
        "text": str(original or "")[:4000],
        "engine": "llm",
        "question": question[:300],
        "options": options,
        "clarification": True,
    }


def validate(data: Any, original: str, today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """Проверяет ответ LLM; ``None`` — ответ непригоден, работают правила.

    Понимает и ``intent``, и ``action``; при низкой уверенности возвращает
    ``clarify`` с вопросом и кнопками (``ask.options``), а не запись наугад.
    """
    if not isinstance(data, Mapping):
        return None
    confidence = _model_confidence(data)
    asked = _clarify_from_ask(data, original, today)
    intent = str(data.get("intent") or data.get("action") or "").strip().casefold()
    if asked is not None and (confidence < MODEL_CONFIDENCE_MIN or intent not in _LLM_INTENTS):
        return asked
    if confidence < MODEL_CONFIDENCE_MIN:
        # Без вариантов ответа записывать нельзя: правила или общий вопрос.
        return None
    clean = _validate_core(data, original, today)
    if clean is None:
        return asked
    return clean


def _system_prompt(today: date) -> str:
    """Подставляет даты в подсказку, не трогая JSON-примеры в скобках."""
    return (
        SYSTEM_PROMPT.replace("{today}", today.isoformat())
        .replace("{tomorrow}", (today + timedelta(days=1)).isoformat())
        .replace("{day_after}", (today + timedelta(days=2)).isoformat())
    )


#: Таймауты одного вызова модели: разбор быстрый, ответ — аккуратный.
PARSE_TIMEOUT_S = 8.0
ANSWER_TIMEOUT_S = 25.0
#: Кэш одинаковых фраз: модель не вызывается повторно 10 минут.
PARSE_CACHE_TTL_S = 600.0
_LLM_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})
_PERIOD_WORDS = frozenset(
    {"неделю", "недели", "месяц", "месяца", "день", "дня", "сегодня", "вчера", "год", "года"}
)


def _effort(value: Any, default: str = "") -> str:
    """Нормализует уровень reasoning effort; неизвестное — default."""
    text = str(value or "").strip().casefold()
    return text if text in _LLM_EFFORTS else default


def rules_confidence(
    parsed: Mapping[str, Any],
    text: str,
    today: Optional[date] = None,
) -> float:
    """Насколько правила уверены в разборе (0..1).

    Ниже :data:`RULES_CONFIDENCE_MIN` фразу смотрит модель: свободная мысль,
    непонятный вопрос, дело без явного глагола/даты, «мысль: …» без книги.
    Явные записи (еда с граммами, сумма, привычка, дата и время) остаются на
    правилах — модель для них не нужна.
    """
    if not isinstance(parsed, Mapping):
        return 0.0
    original = str(text or "").strip()
    if not original:
        return 1.0
    intent = str(parsed.get("intent") or "").strip().casefold()
    if intent in {"", "inbox", "unknown", "legacy_none"}:
        return 0.0
    if intent == "clarify":
        return 0.9
    if intent == "journal":
        return 0.35
    if intent == "query":
        kind = str(parsed.get("query_kind") or "").strip()
        return 0.4 if kind in {"", "unknown"} else 0.85
    if intent == "finance_summary":
        lowered = normalize_text(original).casefold()
        for match in re.finditer(r"\b(?:на|за|по|про)\s+([а-яё][а-яё-]{2,})", lowered):
            if match.group(1) not in _PERIOD_WORDS:
                # «сколько ушло на еду»: правила не фильтруют по предмету трат.
                return 0.5
        return 0.85
    if intent == "task":
        stripped = _TASK_PREFIX_RE.sub("", str(original))
        base_day = today or date.today()
        explicit_date = parse_date_phrase(stripped, base_day)[0] is not None
        explicit_time = parse_time_phrase(stripped) is not None
        task_like = _task_like(stripped) or bool(_TASK_PREFIX_RE.match(str(original)))
        if task_like and (explicit_date or explicit_time):
            return 0.85
        if task_like:
            return 0.55
        # Дело «получилось» только из-за даты («я сегодня как выжатый лимон»).
        return 0.3
    if intent in {"expense", "income"}:
        return 0.9 if parsed.get("amount_kopecks") else 0.3
    if intent == "food":
        return 0.9 if (parsed.get("items") or parsed.get("kcal")) else 0.4
    if intent in {"habit", "practice", "book_progress", "book_quote", "create_book"}:
        return 0.85
    if intent == "book_insight" and not str(parsed.get("book") or "").strip():
        # «мысль: …» без книги — это скорее журнал, пусть решает модель.
        return 0.3
    if intent == "checkin":
        return 0.85 if 1 <= int(parsed.get("energy") or 0) <= 5 else 0.5
    return 0.85


def _numbers_in(text: str) -> set:
    """Числа текста в каноническом виде: «8 900» -> 8900, «2,5» -> 2.5."""
    clean = re.sub(r"(?<=\d)[ \u00a0](?=\d)", "", str(text or ""))
    result = set()
    for match in re.finditer(r"\d+(?:[.,]\d+)?", clean):
        raw = match.group(0).replace(",", ".")
        try:
            value = float(raw)
        except ValueError:  # pragma: no cover - регулярка уже гарантирует число
            continue
        if value.is_integer():
            result.add(str(int(value)))
        else:
            result.add(("%.4f" % value).rstrip("0").rstrip("."))
    return result


def numbers_from_source(answer: str, source: str) -> bool:
    """Все числа ответа есть в переданных агрегатах (ни одного придуманного)."""
    return _numbers_in(answer) <= _numbers_in(source)


ANSWER_SYSTEM_PROMPT = """Ты — голос личного ассистента Life OS (русский язык).
Тебе дают ВОПРОС человека и ДАННЫЕ — готовый ответ, уже посчитанный кодом по
базе. Перескажи его человеческим языком: 1–4 короткие строки, без канцелярита
и без советов.

Жёсткие правила:
- НЕ считай сам и НЕ добавляй ни одного числа, которого нет в ДАННЫХ.
  Числа из данных можно переставлять словами («четыреста» вместо «400» нельзя —
  пиши цифрами, как в данных).
- Не выдумывай даты, проценты, суммы и названия.
- Если в ДАННЫХ сказано, что записей нет, честно скажи «нет записей за период».
- Никаких технических слов, JSON, markdown и оценок.
Верни ТОЛЬКО JSON: {"answer": "текст ответа"}."""


class AnswerWriter:
    """Модель формулирует ответ, но не считает: числа только из агрегатов."""

    MAX_TOKENS = 700

    def __init__(
        self,
        gateway: Any,
        effort: str = "xhigh",
        timeout: float = ANSWER_TIMEOUT_S,
    ) -> None:
        self.gateway = gateway
        self.effort = _effort(effort, "xhigh")
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            self.timeout = ANSWER_TIMEOUT_S
        self.calls = 0
        self.last_error = ""

    def _chat(self, messages: List[Dict[str, str]], max_tokens: int) -> Any:
        attempts = (
            {"max_tokens": max_tokens, "reasoning_effort": self.effort, "timeout": self.timeout},
            {"max_tokens": max_tokens, "reasoning_effort": self.effort},
            {"max_tokens": max_tokens},
        )
        last: Optional[BaseException] = None
        for kwargs in attempts:
            try:
                return self.gateway.json_chat(messages, **kwargs)
            except TypeError as exc:  # фейковый/старый шлюз без части аргументов
                last = exc
                continue
        if last is not None:
            raise last
        return None

    def rewrite(
        self,
        answer_text: str,
        detail: str = "",
        topic: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Переписать готовый ответ; ``None`` — оставить ответ кода."""
        self.last_error = ""
        if self.gateway is None:
            self.last_error = "no_gateway"
            return None
        try:
            if not self.gateway.available():
                self.last_error = "unavailable"
                return None
        except Exception:
            self.last_error = "unavailable"
            return None
        source = "\n".join(part for part in (answer_text, detail) if str(part or "").strip())
        if not source:
            return None
        messages = [
            {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "ВОПРОС (%s): %s\n\nДАННЫЕ:\n%s"
                % (topic or "данные", "перескажи ответ по данным", source),
            },
        ]
        self.calls += 1
        try:
            data = self._chat(messages, self.MAX_TOKENS)
        except Exception as exc:
            self.last_error = type(exc).__name__
            logger.warning("LLM-ответ: сбой (%s) — отвечаю кодом", type(exc).__name__)
            return None
        if not isinstance(data, Mapping):
            self.last_error = "bad_response"
            return None
        text = data.get("answer")
        if not isinstance(text, str) or not text.strip():
            self.last_error = "empty"
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 6:
            lines = lines[:6]
        clean = "\n".join(lines).strip()
        if len(clean) > 800:
            self.last_error = "too_long"
            return None
        if not numbers_from_source(clean, source):
            self.last_error = "invented_number"
            logger.warning("LLM-ответ: придуманное число — оставляю ответ кода")
            return None
        return {"text": clean, "engine": "llm"}

    def __repr__(self) -> str:
        return "AnswerWriter(effort=%r, timeout=%r)" % (self.effort, self.timeout)


def _fallback_clarify(original: str) -> Dict[str, Any]:
    """Модель не ответила, а правила сомневаются — спросить, а не писать наугад."""
    return {
        "intent": "clarify",
        "text": str(original or "")[:4000],
        "engine": "rules",
        "question": "Не уверен, что это. Как сохранить?",
        "options": [
            {"label": "Это мысль", "parsed": {"intent": "journal", "text": original}},
            {"label": "В инбокс", "parsed": {"intent": "inbox", "text": original}},
        ],
        "clarification": True,
    }


class V4Router:
    """Правила первыми, модель — вторым слоем на низкой уверенности.

    * явные записи (еда с граммами, сумма, привычка, дата/время) разбирают
      правила — модель не вызывается, деньги за токены не тратятся;
    * свободная фраза, непонятный вопрос или дело без явного глагола/даты —
      уходит модели с подсказкой, где перечислены все действия бота;
    * низкая уверенность модели — один вопрос с кнопками, не Инбокс;
    * любая ошибка/таймаут — тихий откат на правила;
    * одинаковые фразы кэшируются на 10 минут (модель не видит их дважды).
    """

    #: Лимит ответа разбора: хватает и на рассуждение, и на JSON.
    MAX_TOKENS = 1100

    def __init__(
        self,
        gateway: Optional[Any] = None,
        rules: Optional[RulesV4] = None,
        reasoning_effort: str = "",
        parse_effort: str = "medium",
        answer_effort: str = "xhigh",
        cache_ttl: float = PARSE_CACHE_TTL_S,
        min_confidence: float = RULES_CONFIDENCE_MIN,
        parse_timeout: float = PARSE_TIMEOUT_S,
        answer_timeout: float = ANSWER_TIMEOUT_S,
    ) -> None:
        self.gateway = gateway
        self.rules = rules or RulesV4()
        #: Совместимость с прежним аргументом: им задавался уровень разбора.
        self.reasoning_effort = _effort(reasoning_effort, "")
        self.parse_effort = _effort(parse_effort, self.reasoning_effort or "medium")
        self.answer_effort = _effort(answer_effort, self.reasoning_effort or "xhigh")
        try:
            self.cache_ttl = max(0.0, float(cache_ttl))
        except (TypeError, ValueError):
            self.cache_ttl = PARSE_CACHE_TTL_S
        try:
            self.min_confidence = float(min_confidence)
        except (TypeError, ValueError):
            self.min_confidence = RULES_CONFIDENCE_MIN
        try:
            self.parse_timeout = max(1.0, float(parse_timeout))
        except (TypeError, ValueError):
            self.parse_timeout = PARSE_TIMEOUT_S
        try:
            self.answer_timeout = max(1.0, float(answer_timeout))
        except (TypeError, ValueError):
            self.answer_timeout = ANSWER_TIMEOUT_S
        self.model_calls = 0
        self._cache: Dict[Any, tuple] = {}
        self._cache_lock = threading.Lock()
        self.answer_writer = (
            AnswerWriter(gateway, effort=self.answer_effort, timeout=self.answer_timeout)
            if gateway is not None
            else None
        )

    # -- кэш --------------------------------------------------------------

    def _cache_key(self, text: str, today: date) -> tuple:
        return (today.isoformat(), str(text or "").casefold().strip())

    def _cache_get(self, key: tuple) -> Optional[Dict[str, Any]]:
        if self.cache_ttl <= 0:
            return None
        with self._cache_lock:
            item = self._cache.get(key)
            if item is None:
                return None
            expires, payload = item
            if time.monotonic() >= expires:
                self._cache.pop(key, None)
                return None
            return copy.deepcopy(payload)

    def _cache_put(self, key: tuple, payload: Mapping[str, Any]) -> None:
        if self.cache_ttl <= 0:
            return
        with self._cache_lock:
            if len(self._cache) > 512:
                now = time.monotonic()
                for stale in [k for k, (end, _) in self._cache.items() if end <= now]:
                    self._cache.pop(stale, None)
                while len(self._cache) > 512:
                    self._cache.pop(next(iter(self._cache)), None)
            self._cache[key] = (time.monotonic() + self.cache_ttl, copy.deepcopy(dict(payload)))

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    # -- вызов модели -----------------------------------------------------

    def _gateway_ready(self) -> bool:
        if self.gateway is None:
            return False
        try:
            return bool(self.gateway.available())
        except Exception:
            return False

    def _chat_json(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int,
        effort: str = "",
        timeout: float = PARSE_TIMEOUT_S,
    ) -> Any:
        """Вызов с подсказкой об усилии; фейковые шлюзы без неё — тоже ок."""
        attempts = (
            {"max_tokens": max_tokens, "reasoning_effort": effort or None, "timeout": timeout},
            {"max_tokens": max_tokens, "reasoning_effort": effort or None},
            {"max_tokens": max_tokens},
        )
        last: Optional[BaseException] = None
        for kwargs in attempts:
            try:
                return self.gateway.json_chat(messages, **kwargs)
            except TypeError as exc:
                last = exc
                continue
        if last is not None:
            raise last
        return None

    # -- разбор -----------------------------------------------------------

    def parse(self, text: str, today: Optional[date] = None) -> Dict[str, Any]:
        original = str(text or "").strip()
        current = today or self.rules.today
        if not original:
            return {"intent": "inbox", "text": "", "engine": "rules"}
        key = self._cache_key(original, current)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        rules_parsed = self.rules.parse(original)
        confidence = rules_confidence(rules_parsed, original, current)
        if confidence >= self.min_confidence or not self._gateway_ready():
            result = dict(rules_parsed)
            result["engine"] = "rules"
            self._cache_put(key, result)
            return copy.deepcopy(result)

        # Второй слой: свободная фраза — модели, но один раз и с таймаутом 8 с.
        self.model_calls += 1
        data: Any = None
        model_failed = False
        try:
            data = self._chat_json(
                [
                    {"role": "system", "content": _system_prompt(current)},
                    {"role": "user", "content": original[:4000]},
                ],
                self.MAX_TOKENS,
                self.parse_effort,
                self.parse_timeout,
            )
        except Exception as exc:  # LLM не должен ронять бота
            model_failed = True
            logger.warning("LLM v4: сбой (%s), работаю по правилам", type(exc).__name__)
        if data is None and getattr(self.gateway, "last_error", ""):
            # Шлюз гасит сетевые ошибки внутри и возвращает None: это тоже
            # сбой, а не «модель промолчала»; такой ответ не кэшируем.
            model_failed = True

        parsed = validate(data, original, current)
        if parsed is None:
            parsed = _clarify_from_model(data, original, current)
        if parsed is not None:
            parsed.setdefault("engine", "llm")
            parsed.setdefault("text", original[:4000])
            self._cache_put(key, parsed)
            return copy.deepcopy(parsed)

        model_answered = isinstance(data, Mapping) and bool(
            str(data.get("intent") or data.get("action") or "").strip()
        )
        if (
            confidence < 0.45
            and str(rules_parsed.get("intent") or "") not in {"journal"}
            and (model_failed or model_answered)
        ):
            # Правила «поняли» фразу только из-за даты/слова и уверены слабо
            # («я сегодня как выжатый лимон» -> дело), а модель не дала
            # пригодного разбора. Записывать наугад хуже, чем спросить.
            asked = _fallback_clarify(original)
            return copy.deepcopy(asked)
        result = dict(rules_parsed)
        result["engine"] = "rules"
        # Сбой/таймаут не кэшируем: следующая попытка снова спросит модель,
        # иначе одна медленная сеть «замораживала» фразу на 10 минут.
        if not model_failed:
            self._cache_put(key, result)
        return copy.deepcopy(result)


def build_router(settings: Any = None) -> V4Router:
    """Собирает роутер из настроек (ключ LLM может отсутствовать)."""
    gateway = None
    if settings is not None:
        try:
            from app.bot.intent import OpenAICompatibleGateway

            gateway = OpenAICompatibleGateway(
                base_url=getattr(settings, "llm_base_url", None) or "https://api.samp.team/v1",
                api_key=getattr(settings, "llm_api_key", ""),
                model=getattr(settings, "llm_model", "") or "deepseek-v4.1-flash-1017",
                timeout=getattr(settings, "llm_timeout", 20.0),
                enabled=getattr(settings, "llm_enabled", True),
                reasoning_effort="",
            )
        except Exception:  # pragma: no cover - настройки могут быть неполными
            gateway = None
    fallback = str(getattr(settings, "llm_reasoning_effort", "") or "") if settings is not None else ""
    parse_effort = str(getattr(settings, "llm_parse_effort", "") or fallback or "medium")
    answer_effort = str(getattr(settings, "llm_answer_effort", "") or fallback or "xhigh")
    return V4Router(
        gateway=gateway,
        parse_effort=parse_effort,
        answer_effort=answer_effort,
        parse_timeout=getattr(settings, "llm_parse_timeout", PARSE_TIMEOUT_S) if settings is not None else PARSE_TIMEOUT_S,
        answer_timeout=getattr(settings, "llm_answer_timeout", ANSWER_TIMEOUT_S) if settings is not None else ANSWER_TIMEOUT_S,
    )


def parse(text: str, settings: Any = None, today: Optional[date] = None) -> Dict[str, Any]:
    """Удобная обёртка: разобрать фразу с настройками по умолчанию."""
    return build_router(settings).parse(text, today=today)
