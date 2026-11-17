"""Демо-данные Life OS v3: 120 дней правдоподобной жизни и два курса.

Задача сидера — чтобы свежая установка сразу выглядела живой: обзор с весом,
сном и водой, журнал с бытовыми записями, тренировки с прогрессией штанги,
привычки, цели и два курса с таймкодами и конспектами.

Принципы:

* **детерминированность** — один :class:`random.Random` с зерном 42; при
  одинаковых ``days``/``seed``/``today`` получается один и тот же набор;
* **идемпотентность** — :func:`seed` сначала удаляет прошлый демо-набор
  (:func:`clear_demo`), затем пишет новый, поэтому повторный запуск не
  дублирует строки;
* **демо-строки помечены** — у ``entries``/``metrics`` это ``source='demo'``,
  у привычек/целей/курсов — известные имена из констант модуля. ``--reset``
  удаляет только их и не трогает записи пользователя (``source='telegram'``);
* **без побочных эффектов** — сидер пишет только в SQLite: ни Obsidian-vault,
  ни ``data/media`` он не касается, поэтому его можно гонять на временной БД.

Запуск::

    python3 -m app.server.seeder --days 120
    python3 -m app.server.seeder --days 30 --reset
    LIFEOS_DB_PATH=/tmp/lifeos.db python3 -m app.server.seeder --days 30 --reset
    (синоним для совместимости с задачей: ``LIFEOS_DB``)

Файл БД берётся из ``--db``, затем из ``LIFEOS_DB_PATH`` (или ``LIFEOS_DB``),
затем из настроек приложения (``Settings.db_path``, обычно ``data/lifeos.db``).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.server.db import LegacySchemaError, connect, migrate

__all__ = [
    "DEMO_COURSE_TITLES",
    "DEMO_GOAL_TITLES",
    "DEMO_HABIT_NAMES",
    "DEMO_SOURCE",
    "DEFAULT_DAYS",
    "DEFAULT_SEED",
    "SeedStats",
    "clear_demo",
    "main",
    "resolve_db_path",
    "seed",
]

#: Метка демо-строк в ``entries.source`` / ``metrics.source``.
DEMO_SOURCE = "demo"

DEFAULT_SEED = 42
DEFAULT_DAYS = 120

#: Часовой пояс демо-времён (Europe/Moscow, +03:00 без перехода на летнее).
TZ_SUFFIX = "+03:00"

#: Демо-привычки: имя, эмодзи, цель в неделю.
DEMO_HABITS: tuple[tuple[str, str, int], ...] = (
    ("Вода 2 литра", "💧", 7),
    ("Прогулка 8 тысяч шагов", "🚶", 5),
    ("Чтение 20 минут", "📖", 6),
    ("Растяжка перед сном", "🧘", 5),
)

DEMO_HABIT_NAMES: tuple[str, ...] = tuple(name for name, _, _ in DEMO_HABITS)

#: Демо-цели: заголовок, цель, старт, единица.
DEMO_GOALS: tuple[tuple[str, float, float, str], ...] = (
    ("Вес 78 кг", 78.0, 81.4, "кг"),
    ("Жим лёжа 90 кг", 90.0, 70.0, "кг"),
    ("Пройти курс по EMDR", 6.0, 0.0, "уроков"),
)

DEMO_GOAL_TITLES: tuple[str, ...] = tuple(title for title, _, _, _ in DEMO_GOALS)


# ---------------------------------------------------------------------------
# Контент: еда, журнал, тренировки
# ---------------------------------------------------------------------------

#: Приёмы пищи: название, ккал, белки, жиры, углеводы (на порцию).
BREAKFASTS: tuple[tuple[str, int, int, int, int], ...] = (
    ("Овсянка на молоке с бананом", 420, 14, 9, 68),
    ("Омлет из трёх яиц с тостом", 380, 22, 20, 28),
    ("Творог 5% с ягодами и мёдом", 280, 25, 11, 20),
    ("Сырники со сметаной и кофе", 440, 19, 19, 49),
    ("Гречка с маслом и кусок сыра", 350, 13, 13, 44),
    ("Бутерброд с индейкой и какао", 390, 21, 14, 45),
)

LUNCHES: tuple[tuple[str, int, int, int, int], ...] = (
    ("Борщ с говядиной и гречка", 620, 32, 22, 72),
    ("Куриная грудка с рисом и салат", 560, 45, 14, 58),
    ("Суп-пюре из тыквы и паста", 480, 16, 15, 68),
    ("Плов с индейкой, порция небольшая", 640, 33, 21, 78),
    ("Треска запечённая с картофелем", 520, 41, 17, 49),
    ("Чечевичный суп и цельнозерновой хлеб", 450, 24, 12, 62),
)

DINNERS: tuple[tuple[str, int, int, int, int], ...] = (
    ("Омлет с овощами и зелёный чай", 340, 20, 20, 16),
    ("Творог с огурцом и льняным маслом", 260, 25, 12, 13),
    ("Куриное филе на гриле и овощи", 420, 42, 16, 22),
    ("Рыбные котлеты и брокколи", 380, 30, 17, 25),
    ("Греческий салат с фетой и тост", 360, 16, 23, 24),
    ("Индейка тушёная с кабачками", 390, 38, 15, 20),
)

SNACKS: tuple[tuple[str, int, int, int, int], ...] = (
    ("Яблоко и горсть миндаля", 210, 5, 14, 18),
    ("Йогурт без сахара", 130, 10, 4, 12),
    ("Протеиновый батончик", 190, 18, 7, 14),
    ("Кофе с молоком без сахара", 60, 3, 3, 5),
)

#: Записи журнала: правдоподобные бытовые события.
JOURNAL_TEXTS: tuple[str, ...] = (
    "Утром было тяжело вставать, но короткая прогулка помогла собраться",
    "Договорились с коллегами о созвоне в среду, перенёс два звонка",
    "Позвонил родителям, поговорили про дачу и ремонт",
    "Разобрал почту, закрыл два старых хвоста по работе",
    "Вечером читал про EMDR, отметил для себя восемь фаз протокола",
    "Сделал отчёт за неделю, наконец без спешки",
    "Съездил за продуктами, купил рыбу и овощи на три дня",
    "Час гулял по парку без телефона, стало заметно спокойнее",
    "Отменил одну встречу, чтобы выспаться — кажется, это было правильно",
    "Прибрал на столе, стало проще работать",
    "Начал вести заметки по курсу нутрициологии, пока на первом уроке",
    "Записал в планы на выходные баню и долгий сон",
    "Обсудили с женой отпуск: смотрим варианты на май",
    "Проверил замеры: вес пошёл вниз медленно, но уверенно",
    "Дошёл до 10 тысяч шагов, колени не болели",
    "Долго не мог уснуть, листал новости — надо возвращаться к режиму",
    "Созвонились с братом, обсудили его переезд",
    "Закончил разбор шкафа, отвёз два пакета вещей в donation",
    "Утром сделал зарядку, спина сказала спасибо",
    "Проверил бюджет за месяц: на еду ушло меньше, чем планировал",
    "Сходил к стоматологу, всё спокойно, следующий визит через полгода",
    "Записался на диагностику машины на следующей неделе",
    "Погуляли вечером с собакой под дождём, промокли, но было смешно",
    "Разобрал входящие до нуля впервые за месяц",
    "Приготовил суп на два дня вперёд, вечером не пришлось думать",
    "Читал перед сном вместо телефона, уснул быстрее",
    "Обсудили с командой план на неделю, задач стало меньше",
    "Сделал два подхода приседаний на технику без веса, колени в порядке",
    "Отменил такси и прошёл пешком, заодно набрал шаги",
)

MOOD_TEXTS: tuple[str, ...] = (
    "Спокойный день, без спешки и суеты",
    "Много задач, к вечеру накопилась усталость",
    "Хорошее настроение, продуктивно поработал",
    "Небольшая тревога из-за дедлайна, но к вечеру отпустило",
    "День ровный, ничего особенного, но и не плохой",
    "Встал не в духе, к обеду разошёлся",
    "После тренировки настроение заметно лучше",
)

NOTE_TEXTS: tuple[str, ...] = (
    "Идея: разнести отчёты по неделям, а не по месяцам",
    "Заметка по курсу: дефицит калорий важнее конкретной диеты",
    "Записать на неделе: проверить, хватает ли воды в жару",
    "Подумать про утреннюю зарядку на 10 минут вместо кофе",
    "Купить лампу для рабочего стола, стало темно по вечерам",
    "Перечитать конспект по безопасному месту перед практикой",
    "Проверить сон: ложиться до 23:30 три дня подряд",
)

#: Тренировки: название, ключ, подходы, повторы, стартовый вес, шаг веса.
WORKOUT_PLAN: tuple[tuple[str, str, int, int, float, float], ...] = (
    ("Жим лёжа", "bench", 4, 8, 70.0, 10.0),
    ("Приседания со штангой", "squat", 4, 10, 60.0, 15.0),
    ("Становая тяга", "deadlift", 3, 6, 90.0, 20.0),
)

#: Дни недели с тренировками (0 — понедельник): три раза в неделю.
WORKOUT_WEEKDAYS = (0, 2, 4)


# ---------------------------------------------------------------------------
# Контент: курсы
# ---------------------------------------------------------------------------

#: Курс: заголовок, категория, уроки (заголовок, длительность в секундах).
DEMO_COURSES: tuple[tuple[str, str, tuple[tuple[str, int], ...]], ...] = (
    (
        "Психология и EMDR",
        "психология",
        (
            ("Что такое EMDR", 780),
            ("Восемь фаз протокола", 1020),
            ("Безопасное место и ресурсы", 900),
            ("Десенсибилизация: как это выглядит", 1200),
            ("Работа с телесными реакциями", 960),
            ("Домашняя практика и дневник", 840),
        ),
    ),
    (
        "Нутрициология: основы",
        "питание",
        (
            ("Белки, жиры и углеводы", 780),
            ("Калорийность и дефицит", 900),
            ("Вода и электролиты", 720),
            ("Сон и восстановление", 840),
            ("Питание до и после тренировки", 1020),
            ("Срывы и как их пережить", 780),
        ),
    ),
)

DEMO_COURSE_TITLES: tuple[str, ...] = tuple(title for title, _, _ in DEMO_COURSES)

#: Фразы для сегментов курса «Психология и EMDR».
EMDR_PHRASES: tuple[str, ...] = (
    "EMDR — метод десенсибилизации и переработки движениями глаз",
    "Терапевт просит удерживать воспоминание и одновременно следить за движениями",
    "Восемь фаз протокола идут по порядку, но можно вернуться на предыдущую",
    "Сначала собирается история клиента и определяются мишени переработки",
    "На фазе подготовки объясняют, как устроен метод и чего ждать",
    "Безопасное место — это образ, к которому можно вернуться, если станет тяжело",
    "Стоп-сигнал даёт клиенту право в любой момент прервать упражнение",
    "Десенсибилизация снижает яркость и телесный отклик воспоминания",
    "После каждого подхода терапевт спрашивает, что изменилось",
    "Установка связывает переработанный опыт с новым, более здоровым убеждением",
    "Сканирование тела нужно, чтобы найти оставшееся напряжение",
    "Завершение возвращает клиента в спокойное состояние к концу сессии",
    "Домашняя практика — это дневник наблюдений и упражнения на ресурсы",
    "EMDR не требует подробного пересказа травмы — это его важное отличие",
    "Терапевт работает по протоколу и следит за признаками перегрузки",
    "Переработка может продолжаться между сессиями, во снах и в разговорах",
    "Оценка по шкале SUD показывает, насколько ярким остаётся воспоминание",
    "Курс не заменяет терапию, но даёт понятную карту метода",
)

#: Фразы для сегментов курса «Нутрициология: основы».
NUTRITION_PHRASES: tuple[str, ...] = (
    "Нутрициология начинается с энергии и баланса, а не с запретов",
    "Белок нужен для мышц и сытости, ориентир — 1,6 грамма на килограмм",
    "Жиры поддерживают гормоны, их не стоит опускать ниже половины грамма на кило",
    "Углеводы — топливо для тренировок, особенно сложные и с клетчаткой",
    "Калорийность рациона важнее названия диеты",
    "Дефицит на пятьсот килокалорий даёт около половины килограмма в неделю",
    "Слишком агрессивный дефицит забирает мышцы и портит сон",
    "Вода участвует в терморегуляции, ориентир — 30 миллилитров на килограмм",
    "Электролиты важны при жаре и длинных тренировках",
    "Сон меньше семи часов повышает аппетит и тягу к сладкому",
    "Стресс и недосып меняют гормоны голода сильнее, чем сила воли",
    "Перед тренировкой нужны углеводы, после — белок и углеводы вместе",
    "Один приём пищи не решает ни срыв, ни прогресс",
    "Срыв — это данные, а не провал: важно понять, что было до него",
    "Планирование покупок снижает импульсивные перекусы",
    "Взвешиваться лучше в одно время суток и смотреть на среднюю за неделю",
)


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------


@dataclass
class SeedStats:
    """Сколько строк создано (или удалено) за один прогон."""

    entries: int = 0
    metrics: int = 0
    habits: int = 0
    marks: int = 0
    goals: int = 0
    courses: int = 0
    lessons: int = 0
    segments: int = 0
    notes: int = 0
    progress: int = 0
    transcripts: int = 0

    def as_dict(self) -> dict[str, int]:
        """Плоский словарь для логов и тестов."""
        return {
            "entries": self.entries,
            "metrics": self.metrics,
            "habits": self.habits,
            "marks": self.marks,
            "goals": self.goals,
            "courses": self.courses,
            "lessons": self.lessons,
            "segments": self.segments,
            "notes": self.notes,
            "progress": self.progress,
            "transcripts": self.transcripts,
        }

    def summary(self) -> str:
        """Строка для CLI: «960 записей, 704 метрики, ...»."""
        parts = [
            "%d записей" % self.entries,
            "%d метрик" % self.metrics,
            "%d привычек" % self.habits,
            "%d отметок" % self.marks,
            "%d целей" % self.goals,
            "%d курсов" % self.courses,
            "%d уроков" % self.lessons,
            "%d сегментов" % self.segments,
            "%d заметок" % self.notes,
            "%d прогрессов" % self.progress,
        ]
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Очистка демо-данных
# ---------------------------------------------------------------------------


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def clear_demo(conn: sqlite3.Connection) -> SeedStats:
    """Удаляет только демо-данные и возвращает число удалённых строк.

    Демо-строки журнала и метрик узнаются по ``source='demo'``; привычки,
    цели и курсы — по именам из констант этого модуля. Пользовательские
    записи (``source='telegram'``, свои привычки и цели) остаются на месте.
    """
    stats = SeedStats()
    with conn:
        stats.entries = _count(
            conn, "SELECT COUNT(*) FROM entries WHERE source = ?", (DEMO_SOURCE,)
        )
        stats.metrics = _count(
            conn, "SELECT COUNT(*) FROM metrics WHERE source = ?", (DEMO_SOURCE,)
        )
        stats.marks = _count(
            conn,
            "SELECT COUNT(*) FROM habit_marks WHERE habit_id IN "
            "(SELECT id FROM habits WHERE name IN (%s))" % _placeholders(DEMO_HABIT_NAMES),
            DEMO_HABIT_NAMES,
        )
        stats.habits = _count(
            conn,
            "SELECT COUNT(*) FROM habits WHERE name IN (%s)" % _placeholders(DEMO_HABIT_NAMES),
            DEMO_HABIT_NAMES,
        )
        stats.goals = _count(
            conn,
            "SELECT COUNT(*) FROM goals WHERE title IN (%s)" % _placeholders(DEMO_GOAL_TITLES),
            DEMO_GOAL_TITLES,
        )
        stats.courses = _count(
            conn,
            "SELECT COUNT(*) FROM courses WHERE title IN (%s)"
            % _placeholders(DEMO_COURSE_TITLES),
            DEMO_COURSE_TITLES,
        )
        stats.lessons = _count(
            conn,
            "SELECT COUNT(*) FROM lessons WHERE course_id IN "
            "(SELECT id FROM courses WHERE title IN (%s))" % _placeholders(DEMO_COURSE_TITLES),
            DEMO_COURSE_TITLES,
        )
        stats.segments = _count(
            conn,
            "SELECT COUNT(*) FROM segments WHERE lesson_id IN (SELECT id FROM lessons "
            "WHERE course_id IN (SELECT id FROM courses WHERE title IN (%s)))"
            % _placeholders(DEMO_COURSE_TITLES),
            DEMO_COURSE_TITLES,
        )

        conn.execute("DELETE FROM entries WHERE source = ?", (DEMO_SOURCE,))
        conn.execute("DELETE FROM metrics WHERE source = ?", (DEMO_SOURCE,))
        conn.execute(
            "DELETE FROM habits WHERE name IN (%s)" % _placeholders(DEMO_HABIT_NAMES),
            DEMO_HABIT_NAMES,
        )
        conn.execute(
            "DELETE FROM goals WHERE title IN (%s)" % _placeholders(DEMO_GOAL_TITLES),
            DEMO_GOAL_TITLES,
        )
        conn.execute(
            "DELETE FROM courses WHERE title IN (%s)" % _placeholders(DEMO_COURSE_TITLES),
            DEMO_COURSE_TITLES,
        )
    return stats


def _count(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    row = conn.execute(sql, tuple(params)).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):  # pragma: no cover - защита от чужой строки
        return 0


# ---------------------------------------------------------------------------
# Генерация
# ---------------------------------------------------------------------------


def _days(today: _date, count: int) -> list[_date]:
    """Список дат: последние ``count`` дней, включая ``today``, по возрастанию."""
    span = max(1, int(count))
    return [today - timedelta(days=offset) for offset in range(span - 1, -1, -1)]


def _ts(day: _date, hour: int, minute: int) -> str:
    return "%sT%02d:%02d:00%s" % (day.isoformat(), hour, minute, TZ_SUFFIX)


def _meta(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def seed(
    conn: sqlite3.Connection,
    *,
    days: int = DEFAULT_DAYS,
    seed: int = DEFAULT_SEED,
    reset: bool = False,
    today: Any = None,
) -> SeedStats:
    """Пересобирает демо-набор в базе и возвращает статистику.

    ``reset=True`` — только удалить демо-данные, ничего не писать.
    Повторный вызов с теми же аргументами даёт тот же набор: старые демо-строки
    удаляются до вставки, поэтому дублей не появляется.
    """
    stats = clear_demo(conn)
    if reset:
        return stats

    rnd = random.Random(int(seed))
    last_day = _as_date(today)
    dates = _days(last_day, days)
    stamp = "%sT12:00:00%s" % (last_day.isoformat(), TZ_SUFFIX)

    with conn:
        entries, metrics = _build_daily(rnd, dates)
        _insert_entries(conn, entries)
        _insert_metrics(conn, metrics)
        marks = _build_habits(conn, rnd, dates, stamp)
        _build_goals(conn, rnd, last_day, stamp)
        courses = _build_courses(conn, rnd, last_day, stamp)

    stats.entries = len(entries)
    stats.metrics = len(metrics)
    stats.habits = len(DEMO_HABITS)
    stats.marks = marks
    stats.goals = len(DEMO_GOALS)
    stats.courses = courses["courses"]
    stats.lessons = courses["lessons"]
    stats.segments = courses["segments"]
    stats.notes = courses["notes"]
    stats.progress = courses["progress"]
    stats.transcripts = courses["transcripts"]
    return stats


def _as_date(value: Any) -> _date:
    if value is None:
        return _date.today()
    if isinstance(value, _date):
        return value
    return _date.fromisoformat(str(value))


# -- журнал и метрики ------------------------------------------------------


def _build_daily(
    rnd: random.Random, dates: Sequence[_date]
) -> tuple[list[tuple[str, str, str, str, str]], list[tuple[str, str, float, str, str]]]:
    """Собирает записи журнала и метрики за все дни."""
    entries: list[tuple[str, str, str, str, str]] = []
    metrics: list[tuple[str, str, float, str, str]] = []

    measure_flags = [index % 7 in (0, 1, 3, 4, 6) for index in range(len(dates))]
    measured_total = max(1, sum(measure_flags))
    measured_seen = 0

    for index, day in enumerate(dates):
        date_str = day.isoformat()

        # Сон, настроение, энергия и вода — каждый день.
        sleep_hours = 6.2 if index and index % 12 == 0 else rnd.uniform(6.4, 8.0)
        metrics.append((date_str, "sleep", round(sleep_hours, 1), "ч", DEMO_SOURCE))
        metrics.append((date_str, "mood", rnd.randint(2, 5), "балл", DEMO_SOURCE))
        metrics.append((date_str, "energy", rnd.randint(2, 5), "балл", DEMO_SOURCE))
        metrics.append(
            (date_str, "water", float(rnd.randrange(1200, 2601, 50)), "мл", DEMO_SOURCE)
        )

        # Вес и состав тела — 5 дней из 7.
        if measure_flags[index]:
            progress = measured_seen / (measured_total - 1) if measured_total > 1 else 0.0
            measured_seen += 1
            weight = 81.4 - 1.6 * progress + rnd.uniform(-0.4, 0.4)
            fat = 18.5 - 1.3 * progress + rnd.uniform(-0.15, 0.15)
            muscle = 34.8 + 0.5 * progress + rnd.uniform(-0.1, 0.1)
            body_water = 55.0 + 1.0 * progress + rnd.uniform(-0.3, 0.3)
            metrics.append((date_str, "weight", round(weight, 1), "кг", DEMO_SOURCE))
            metrics.append((date_str, "fat", round(fat, 1), "%", DEMO_SOURCE))
            metrics.append((date_str, "muscle", round(muscle, 1), "кг", DEMO_SOURCE))
            metrics.append((date_str, "body_water", round(body_water, 1), "%", DEMO_SOURCE))

        # Питание: 2–4 записи в день с КБЖУ.
        meals: list[tuple[int, str, tuple[str, int, int, int, int]]] = [
            (8, "Завтрак", rnd.choice(BREAKFASTS)),
            (13, "Обед", rnd.choice(LUNCHES)),
            (19, "Ужин", rnd.choice(DINNERS)),
        ]
        if rnd.random() < 0.6:
            meals.append((16, "Перекус", rnd.choice(SNACKS)))
        for hour, label, (title, kcal, protein, fat_g, carbs) in meals:
            entries.append(
                (
                    _ts(day, hour, rnd.choice((5, 15, 25, 35, 45))),
                    "food",
                    "%s: %s" % (label, title),
                    DEMO_SOURCE,
                    _meta(
                        {
                            "meal": label,
                            "kcal": kcal,
                            "protein_g": protein,
                            "fat_g": fat_g,
                            "carbs_g": carbs,
                        }
                    ),
                )
            )

        # Журнал: пять разных записей в день + настроение и заметка.
        for moment, text in zip((8, 11, 14, 17, 21), rnd.sample(JOURNAL_TEXTS, 5)):
            entries.append(
                (_ts(day, moment, rnd.choice((0, 10, 20, 30, 40, 50))),
                 "journal", text, DEMO_SOURCE, "{}")
            )
        entries.append(
            (_ts(day, 20, rnd.choice((5, 15, 30, 45))), "mood",
             rnd.choice(MOOD_TEXTS), DEMO_SOURCE, _meta({"score": rnd.randint(2, 5)}))
        )
        if rnd.random() < 0.7:
            entries.append(
                (_ts(day, 16, rnd.choice((0, 20, 40))), "note",
                 rnd.choice(NOTE_TEXTS), DEMO_SOURCE, "{}")
            )
        if rnd.random() < 0.5:
            entries.append(
                (_ts(day, 12, rnd.choice((0, 30))), "water",
                 "Вода: %d мл" % rnd.randrange(300, 600, 50), DEMO_SOURCE,
                 _meta({"ml": rnd.randrange(300, 600, 50)}))
            )

        # Тренировки: три раза в неделю с прогрессией веса.
        if day.weekday() in WORKOUT_WEEKDAYS:
            name, key, sets, reps, start_weight, step = WORKOUT_PLAN[
                WORKOUT_WEEKDAYS.index(day.weekday())
            ]
            week = index / 7.0
            weight = start_weight + step * (week / (len(dates) / 7.0 or 1.0))
            weight = round(weight / 2.5) * 2.5
            entries.append(
                (
                    _ts(day, 18, rnd.choice((0, 10, 20, 30))),
                    "workout",
                    "%s %dx%d @ %s кг, разминка 10 минут" % (name, sets, reps, _kg(weight)),
                    DEMO_SOURCE,
                    _meta(
                        {
                            "exercise": key,
                            "sets": sets,
                            "reps": reps,
                            "weight_kg": round(weight, 1),
                            "duration_min": rnd.choice((45, 50, 55, 60)),
                        }
                    ),
                )
            )
    return entries, metrics


def _kg(value: float) -> str:
    return ("%.1f" % value).rstrip("0").rstrip(".")


def _insert_entries(conn: sqlite3.Connection, entries: Sequence[tuple]) -> None:
    conn.executemany(
        "INSERT INTO entries (ts, kind, text, source, meta_json) VALUES (?, ?, ?, ?, ?)",
        entries,
    )


def _insert_metrics(conn: sqlite3.Connection, metrics: Sequence[tuple]) -> None:
    conn.executemany(
        "INSERT INTO metrics (date, kind, value, unit, source) VALUES (?, ?, ?, ?, ?)",
        metrics,
    )


# -- привычки, цели --------------------------------------------------------


def _build_habits(
    conn: sqlite3.Connection, rnd: random.Random, dates: Sequence[_date], stamp: str
) -> int:
    """Создаёт 4 привычки и отметки примерно на 80% дней."""
    marks: list[tuple[int, str, float]] = []
    for name, emoji, target in DEMO_HABITS:
        cursor = conn.execute(
            "INSERT INTO habits (name, emoji, target_per_week, created) VALUES (?, ?, ?, ?)",
            (name, emoji, target, stamp),
        )
        habit_id = int(cursor.lastrowid or 0)
        for day in dates:
            if rnd.random() < 0.8:
                marks.append((habit_id, day.isoformat(), 1.0))
    conn.executemany(
        "INSERT OR IGNORE INTO habit_marks (habit_id, date, value) VALUES (?, ?, ?)", marks
    )
    return len(marks)


def _build_goals(conn: sqlite3.Connection, rnd: random.Random, today: _date, stamp: str) -> None:
    """Три цели с правдоподобным текущим значением."""
    for title, target, start, unit in DEMO_GOALS:
        done = rnd.uniform(0.15, 0.65)
        current = start + (target - start) * done
        deadline = today + timedelta(days=rnd.choice((30, 60, 90, 120)))
        conn.execute(
            "INSERT INTO goals (title, target, current, unit, deadline, status, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, target, round(current, 1), unit, deadline.isoformat(), "active", stamp),
        )


# -- курсы -----------------------------------------------------------------


def _build_courses(
    conn: sqlite3.Connection, rnd: random.Random, today: _date, stamp: str
) -> dict[str, int]:
    """Два курса по 6 уроков: сегменты, заметки, прогресс, расшифровки."""
    counts = {
        "courses": 0,
        "lessons": 0,
        "segments": 0,
        "notes": 0,
        "progress": 0,
        "transcripts": 0,
    }
    for course_index, (title, category, lessons) in enumerate(DEMO_COURSES):
        cursor = conn.execute(
            "INSERT INTO courses (title, category, cover, created) VALUES (?, ?, ?, ?)",
            (title, category, "", stamp),
        )
        course_id = int(cursor.lastrowid or 0)
        counts["courses"] += 1
        phrases = EMDR_PHRASES if course_index == 0 else NUTRITION_PHRASES

        for lesson_index, (lesson_title, duration_s) in enumerate(lessons):
            cursor = conn.execute(
                "INSERT INTO lessons (course_id, idx, title, media_path, duration_s, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (course_id, lesson_index + 1, lesson_title, "", duration_s, "new"),
            )
            lesson_id = int(cursor.lastrowid or 0)
            counts["lessons"] += 1

            segments = _lesson_segments(rnd, phrases, lesson_index, duration_s)
            conn.executemany(
                "INSERT INTO segments (lesson_id, start_s, end_s, text) VALUES (?, ?, ?, ?)",
                [(lesson_id, start, start + 14, text) for start, text in segments],
            )
            counts["segments"] += len(segments)

            transcript = " ".join(text for _, text in segments) + "."
            conn.execute(
                "INSERT INTO transcripts (lesson_id, lang, text) VALUES (?, ?, ?)",
                (lesson_id, "ru", transcript),
            )
            counts["transcripts"] += 1

            for note_index, note_text in enumerate(_lesson_notes(rnd, lesson_index)):
                note_ts = "%sT%02d:%02d:00%s" % (
                    (today - timedelta(days=rnd.randint(1, 25))).isoformat(),
                    rnd.choice((9, 12, 15, 20, 22)),
                    rnd.choice((0, 15, 30, 45)),
                    TZ_SUFFIX,
                )
                conn.execute(
                    "INSERT INTO notes (lesson_id, ts, text) VALUES (?, ?, ?)",
                    (lesson_id, note_ts, note_text(lesson_title)),
                )
                counts["notes"] += 1

            # Прогресс: первый курс пройден наполовину, второй только начат.
            if course_index == 0 and lesson_index < 3:
                watched = duration_s * rnd.uniform(0.85, 0.98)
                conn.execute(
                    "INSERT INTO lesson_progress (lesson_id, position_s, watched_s, updated) "
                    "VALUES (?, ?, ?, ?)",
                    (lesson_id, round(watched, 1), round(watched, 1), stamp),
                )
                if duration_s and watched / duration_s >= 0.85:
                    conn.execute("UPDATE lessons SET status = 'done' WHERE id = ?", (lesson_id,))
                counts["progress"] += 1
            elif course_index == 1 and lesson_index < 2:
                position = duration_s * rnd.uniform(0.2, 0.5)
                conn.execute(
                    "INSERT INTO lesson_progress (lesson_id, position_s, watched_s, updated) "
                    "VALUES (?, ?, ?, ?)",
                    (lesson_id, round(position, 1), round(position, 1), stamp),
                )
                counts["progress"] += 1
    return counts


def _lesson_segments(
    rnd: random.Random, phrases: Sequence[str], lesson_index: int, duration_s: int
) -> list[tuple[int, str]]:
    """Таймкоды урока: 12–15 фрагментов, разнесённых по длительности."""
    count = 12 + (lesson_index + len(phrases)) % 4
    offset = lesson_index * 3
    start = 20
    step = max(30, (duration_s - 40) // count)
    segments: list[tuple[int, str]] = []
    for position in range(count):
        text = phrases[(offset + position) % len(phrases)]
        segments.append((start, text))
        start += step + rnd.choice((-5, 0, 0, 5))
    return segments


def _lesson_notes(rnd: random.Random, lesson_index: int) -> list[Any]:
    """1–2 заметки к уроку (фабрики текста, зависящие от названия урока)."""
    templates = (
        lambda title: "Пересмотреть блок про «%s» перед практикой" % title,
        lambda title: "Выписать тезисы урока «%s» в дневник" % title,
        lambda title: "Вопрос: как это применить в ближайшую неделю — «%s»" % title,
    )
    count = 2 if rnd.random() < 0.35 else 1
    return rnd.sample(templates, count)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def resolve_db_path(db: str | Path | None = None) -> Path:
    """Путь к БД: ``--db`` → ``LIFEOS_DB_PATH``/``LIFEOS_DB`` → настройки → ``data/``."""
    if db:
        return Path(str(db)).expanduser()
    env_path = os.environ.get("LIFEOS_DB_PATH") or os.environ.get("LIFEOS_DB")
    if env_path:
        return Path(env_path).expanduser()
    try:  # настройки читают config/config.json; без него возьмём путь по умолчанию
        from app.server.settings import load_settings

        return Path(load_settings().db_path)
    except Exception:  # pragma: no cover - конфига может не быть вовсе
        return Path(__file__).resolve().parents[2] / "data" / "lifeos.db"


def main(argv: Iterable[str] | None = None) -> int:
    """CLI сидера: печатает, что засеяно, и возвращает код выхода."""
    parser = argparse.ArgumentParser(
        prog="python3 -m app.server.seeder",
        description="Демо-данные Life OS v3: журнал, метрики, привычки, цели и курсы.",
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="сколько дней сеять")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="зерно генератора")
    parser.add_argument(
        "--reset", action="store_true", help="только удалить демо-данные, ничего не писать"
    )
    parser.add_argument(
        "--db", default=None, help="файл БД (иначе LIFEOS_DB_PATH / LIFEOS_DB / настройки)"
    )
    parser.add_argument("--quiet", action="store_true", help="не печатать статистику")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.days < 1 or args.days > 3650:
        print("--days должен быть от 1 до 3650", file=sys.stderr)
        return 2

    path = resolve_db_path(args.db)
    print("БД: %s" % path)
    try:
        conn = connect(path)
        try:
            migrate(conn)
            stats = seed(conn, days=args.days, seed=args.seed, reset=args.reset)
        finally:
            conn.close()
    except LegacySchemaError as exc:
        print("Ошибка: %s" % exc, file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print("Ошибка SQLite: %s" % exc, file=sys.stderr)
        return 2

    if args.reset:
        print("Удалены демо-данные: %s" % stats.summary())
    elif not args.quiet:
        print("Засеяно за %d дней: %s" % (args.days, stats.summary()))
    return 0


if __name__ == "__main__":  # pragma: no cover - точка входа CLI
    raise SystemExit(main())
