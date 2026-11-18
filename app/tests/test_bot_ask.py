"""Бот отвечает на вопросы о данных (t29).

Проверяется не «текст похож», а цифры: 20 живых вопросов из задания идут через
``BotService``, а ожидаемые значения берутся из API на той же базе (деньги,
дела, цели, вес, КБЖУ, сон, книги, журнал, тренировки, привычки, энергия).

Дополнительно: короткий ответ 3–6 строк, кнопки «Подробнее» (ссылка на экран
веба) и «Ещё/Кратко», честное «не знаю» с подсказкой, отсутствие записей от
вопросов и устойчивость к мусорному вопросу.

База — во временном каталоге; рабочая ``data/`` не трогается.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.bot import handlers, intent, intent_v4, store_v4
from app.server import db
from app.server.main import create_app
from app.server.settings import load_settings

CHAT_ID = 424242
WEB_BASE = "https://life.example"


# --------------------------------------------------------------------------
# Демо-данные и окружение
# --------------------------------------------------------------------------


def _ts(day: date, hour: int = 12) -> str:
    return day.isoformat() + "T%02d:00:00+03:00" % hour


def seed_ask_data(conn: Any, settings: Any) -> None:
    """Правдоподобные данные вокруг сегодняшнего дня (детерминированно)."""
    today = settings.now().date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    with conn:
        conn.execute("INSERT INTO finance_accounts (name, kind) VALUES ('Карта', 'card')")
        conn.executemany(
            "INSERT INTO finance_categories (name, kind) VALUES (?, ?)",
            [("Транспорт", "expense"), ("Кафе", "expense"), ("Зарплата", "income")],
        )
        transport, cafe, salary = 1, 2, 3
        transactions = [
            (week_start, 80000, "expense", transport, "метро"),
            (today, 50000, "expense", cafe, "кофе"),
            (month_start, 70000, "expense", transport, "бензин"),
            (month_start - timedelta(days=20), 30000, "expense", transport, "старое"),
            (today, 5_000_000, "income", salary, "зарплата"),
        ]
        for day, amount, kind, category, note in transactions:
            conn.execute(
                "INSERT INTO transactions (ts, amount_kopecks, kind, category_id, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (_ts(day), amount, kind, category, note),
            )

        tasks = [
            ("Позвонить в банк", today, "todo", "must", None),
            ("Разобрать почту", today, "todo", "important", None),
            ("Купить хлеб", today, "done", "low", _ts(today, 10)),
            ("Сдать отчёт", today + timedelta(days=1), "todo", "important", None),
            ("Забрать документы", today - timedelta(days=2), "todo", "important", None),
        ]
        for title, due, status, resource, done_at in tasks:
            conn.execute(
                "INSERT INTO tasks (title, due_date, status, resource, done_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (title, due.isoformat(), status, resource, done_at),
            )

        conn.execute(
            "INSERT INTO goals (title, target, current, unit, horizon, status, progress, created) "
            "VALUES ('Здоровье', 100, 45, '%', 'year', 'active', 40, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO goals (title, target, current, unit, horizon, status, progress, created) "
            "VALUES ('Финансы', 100, 30, '%', 'year', 'active', 30, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO goal_steps (goal_id, title, done) VALUES (1, 'Взвешиваться', 1)"
        )
        conn.execute(
            "INSERT INTO goal_steps (goal_id, title, done) VALUES (1, 'Спорт 3 раза в неделю', 0)"
        )

        for offset, value in ((1, 81.0), (3, 81.5), (5, 82.0)):
            conn.execute(
                "INSERT INTO metrics (date, kind, value, unit) VALUES (?, 'weight', ?, 'кг')",
                ((today - timedelta(days=offset)).isoformat(), value),
            )

        yesterday = today - timedelta(days=1)
        conn.execute(
            "INSERT INTO food_log (date, ts, meal, text, grams, kcal, protein, fat, carbs) "
            "VALUES (?, ?, 'breakfast', 'овсянка', 250, 500, 30, 15, 60)",
            (yesterday.isoformat(), _ts(yesterday, 9)),
        )
        conn.execute(
            "INSERT INTO food_log (date, ts, meal, text, grams, kcal, protein, fat, carbs) "
            "VALUES (?, ?, 'lunch', 'гречка и курица', 350, 700, 50, 25, 60)",
            (yesterday.isoformat(), _ts(yesterday, 13)),
        )

        sleep_days = {week_start, min(today, week_start + timedelta(days=1))}
        for index, day in enumerate(sorted(sleep_days)):
            conn.execute(
                "INSERT INTO metrics (date, kind, value, unit) VALUES (?, 'sleep', ?, 'ч')",
                (day.isoformat(), 7.0 + index),
            )

        conn.execute(
            "INSERT INTO books (title, author, category, pages_total, pages_read, shelf) "
            "VALUES ('Sapiens', 'Харари', 'nonfiction', 400, 120, 'reading')"
        )
        conn.execute(
            "INSERT INTO books (title, category, shelf) VALUES ('Атомные привычки', 'pro', 'queue')"
        )
        conn.execute(
            "INSERT INTO book_quotes (book_id, text) VALUES (1, 'Сон — это лучшее лекарство')"
        )
        conn.execute(
            "INSERT INTO book_insights (book_id, text) VALUES (1, 'Сон важнее продуктивности')"
        )

        journal_days = [today, today - timedelta(days=1), today]
        topics = ['["работа"]', '["здоровье", "семья"]', '["работа", "отдых"]']
        for index, (day, topic) in enumerate(zip(journal_days, topics)):
            conn.execute(
                "INSERT INTO entries (ts, kind, text, meta_json) VALUES (?, 'journal', ?, ?)",
                (_ts(day, 18 - index), "Запись %d" % (index + 1), '{"topics": %s}' % topic),
            )
        conn.execute(
            "INSERT INTO entries (ts, kind, text, meta_json) VALUES (?, 'journal', 'Старая', '{}')",
            (_ts(month_start - timedelta(days=20)),),
        )
        conn.execute(
            "INSERT INTO entries (ts, kind, text, meta_json) VALUES (?, 'workout', 'Жим 80x5', '{}')",
            (_ts(today - timedelta(days=3), 19),),
        )
        conn.execute(
            "INSERT INTO entries (ts, kind, text, meta_json) VALUES (?, 'workout', 'Присед 100x3', '{}')",
            (_ts(today - timedelta(days=10), 19),),
        )

        for name in ("Вода 2 литра", "Чтение 20 минут", "Зарядка"):
            conn.execute(
                "INSERT INTO habits (name, emoji, target_per_week) VALUES (?, '', 7)", (name,)
            )
        conn.execute(
            "INSERT INTO habit_marks (habit_id, date, value) VALUES (1, ?, 1)",
            (today.isoformat(),),
        )
        conn.execute(
            "INSERT INTO habit_marks (habit_id, date, value) VALUES (2, ?, 1)",
            (today.isoformat(),),
        )
        conn.execute(
            "INSERT INTO checkins (date, energy, mood) VALUES (?, 4, 'усталость')",
            (yesterday.isoformat(),),
        )


@pytest.fixture()
def ask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIFEOS_WEB_BASE_URL", WEB_BASE)
    settings = load_settings(
        env={
            "LIFEOS_ADMIN_TELEGRAM_ID": str(CHAT_ID),
            "LIFEOS_DB_PATH": str(tmp_path / "lifeos.db"),
            "LIFEOS_WEB_BASE_URL": WEB_BASE,
        },
        root=tmp_path,
    )
    settings.ensure_dirs()
    conn = db.init_db(settings.db_path)
    seed_ask_data(conn, settings)
    service = handlers.BotService(
        settings,
        conn,
        intent_router=intent.build_router(None),
        v4_router=intent_v4.V4Router(gateway=None),
        media_root=tmp_path / "media",
    )
    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        yield SimpleNamespace(
            settings=settings, conn=conn, service=service, client=client, tmp=tmp_path
        )
    conn.close()


def send(env: Any, text: str) -> handlers.Reply:
    return env.service.handle_text(text, CHAT_ID)


def api(env: Any, path: str, **params: Any) -> Dict[str, Any]:
    response = env.client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def money(kopecks: int) -> str:
    return intent_v4.format_money(int(kopecks))


def side_effects(env: Any) -> int:
    tables = ("entries", "inbox_items", "tasks", "transactions", "book_quotes", "habit_marks")
    total = 0
    for table in tables:
        total += int(env.conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
    return total


# --------------------------------------------------------------------------
# 20 вопросов: цифры сверяются с API
# --------------------------------------------------------------------------


def test_twenty_questions_answer_with_api_numbers(ask) -> None:
    today = ask.settings.now().date()
    week_start = today - timedelta(days=today.weekday())
    week_days = (today - week_start).days + 1
    month = today.strftime("%Y-%m")

    week_months = {week_start.strftime("%Y-%m"), today.strftime("%Y-%m")}
    week_items: List[Dict[str, Any]] = []
    for month_key in sorted(week_months):
        week_items.extend(api(ask, "/api/finance/transactions", month=month_key, limit=500)["items"])
    week_expense = sum(
        int(item["amountKopecks"])
        for item in week_items
        if item["kind"] == "expense" and item["date"] >= week_start.isoformat()
    )

    month_money = api(ask, "/api/finance/summary", month=month)
    month_expense = int(month_money["expenseKopecks"])
    top = max(
        (item for item in month_money["byCategory"] if item["kind"] == "expense"),
        key=lambda item: item["sumKopecks"],
    )

    books = api(ask, "/api/books")["items"]
    sapiens = next(book for book in books if book["title"] == "Sapiens")
    book_card = api(ask, "/api/books/%d" % sapiens["id"])
    quote = book_card["quotes"][0]["text"]

    compass = api(ask, "/api/compass")["goals"]
    health = next(goal for goal in compass if goal["title"] == "Здоровье")
    finance = next(goal for goal in compass if goal["title"] == "Финансы")

    weight = api(ask, "/api/health/weight", days=30)
    weight_avg = sum(point["value"] for point in weight["points"]) / len(weight["points"])

    yesterday = (today - timedelta(days=1)).isoformat()
    nutrition = api(ask, "/api/nutrition/day", date=yesterday)
    kcal = int(round(nutrition["totals"]["kcal"]))
    protein = int(round(nutrition["totals"]["protein"]))

    sleep = api(ask, "/api/sections/sleep", days=week_days)["series"][0]
    sleep_avg = store_v4._fmt_number(sleep["avg"], 1)

    tasks_today = api(ask, "/api/tasks", date=today.isoformat())["items"]
    open_today = [task for task in tasks_today if task["status"] == "todo"]
    tasks_tomorrow = api(
        ask, "/api/tasks", date=(today + timedelta(days=1)).isoformat()
    )["items"]
    open_tomorrow = [task for task in tasks_tomorrow if task["status"] == "todo"]
    overdue_api = api(
        ask,
        "/api/tasks",
        **{
            "from": (today - timedelta(days=30)).isoformat(),
            "to": yesterday,
            "status": "todo",
        },
    )["items"]
    overdue = [task for task in overdue_api if (task.get("dueDate") or "") < today.isoformat()]

    journal = api(ask, "/api/journal", limit=100)["items"]
    week_journal = [item for item in journal if (item.get("date") or "") >= week_start.isoformat()]

    workout_items = api(ask, "/api/journal", kind="workout", limit=10)["items"]
    last_workout = workout_items[0]
    workout_day = date.fromisoformat(last_workout["date"])
    workout_ago = (today - workout_day).days
    workout_ago_text = (
        "сегодня" if workout_ago == 0 else ("вчера" if workout_ago == 1 else "%d дня назад" % workout_ago)
    )

    habits = api(ask, "/api/personal/summary", date=today.isoformat())["habits"]
    marks_today = sum(1 for habit in habits if habit["marks"].get(today.isoformat()))

    checkin = api(ask, "/api/personal/summary", date=yesterday)["checkin"]
    review = api(ask, "/api/personal/review", period="week")["facts"]

    checks: List[tuple] = []

    def check(question: str, *fragments: str) -> None:
        reply = send(ask, question)
        assert reply.text.strip(), "пустой ответ на %r" % question
        lines = reply.text.splitlines()
        assert 1 <= len(lines) <= 6, "ответ на %r длиной %d строк" % (question, len(lines))
        for fragment in fragments:
            assert fragment in reply.text, "%r: нет %r в ответе:\n%s" % (question, fragment, reply.text)
        checks.append((question, reply.text))

    check("сколько потратил за неделю", money(week_expense))
    check("сколько потратил в этом месяце", money(month_expense))
    check("на что больше всего потратил", top["name"], money(int(top["sumKopecks"])))
    check("что у меня на сегодня", "%d открыто" % len(open_today))
    check("что на завтра", "Завтра", "%d открыто" % len(open_tomorrow))
    check("что просрочено", "Просрочено: %d" % len(overdue))
    check("как дела с целью Здоровье", "%d%%" % health["progressPct"], "1 из 2")
    check("сколько весил в среднем за месяц", store_v4._fmt_number(weight_avg, 1))
    check("сколько калорий вчера", store_v4._fmt_number(kcal, 0), "ккал")
    check("сколько спал на этой неделе", sleep_avg)
    check("что я читал про сон", "лекарство")
    check("о чём я писал на этой неделе", "%d %s" % (len(week_journal), store_v4._records_word(len(week_journal))))
    check("когда последний раз был в зале", "Жим 80x5", workout_ago_text)
    check("сколько привычек держу", "%d активных" % len(habits), "%d из %d" % (marks_today, len(habits)))
    check("сколько энергии вчера", "%d/5" % checkin["energy"])
    check("покажи книги", "Книг на полке: %d" % len(books), "Sapiens")
    check("сколько денег за месяц", money(month_expense))
    check("итоги недели", "Дела: закрыто %d из %d" % (review["tasksDone"], review["tasksTotal"]))
    check("сколько белков съел вчера", "Белок %d" % protein)
    check("какой прогресс по цели Финансы", "%d%%" % finance["progressPct"])

    assert len(checks) == 20, "проверено %d вопросов, нужно 20" % len(checks)


def test_questions_create_no_records(ask) -> None:
    before = side_effects(ask)
    for question in (
        "сколько потратил за неделю",
        "что просрочено",
        "как дела с целью Здоровье",
        "что я читал про сон",
        "какая погода в Москве?",
    ):
        send(ask, question)
    assert side_effects(ask) == before, "вопрос создал запись"


# --------------------------------------------------------------------------
# Кнопки «Подробнее» и «Ещё/Кратко»
# --------------------------------------------------------------------------


def test_answer_has_web_link_and_more(ask) -> None:
    reply = send(ask, "сколько потратил за неделю")
    assert reply.buttons, "нет кнопок у ответа"
    row = reply.buttons[0]
    labels = [label for label, _ in row]
    assert "Подробнее" in labels and "Ещё" in labels
    url = next(value for label, value in row if label == "Подробнее")
    assert url == WEB_BASE + "/#/finance"

    more = ask.service.handle_callback("v4:ask:more", CHAT_ID)
    assert "Кратко" in [label for label, _ in more.buttons[0]]
    assert "Статьи" in more.text or "Дней с тратами" in more.text

    brief = ask.service.handle_callback("v4:ask:brief", CHAT_ID)
    assert "Ещё" in [label for label, _ in brief.buttons[0]]


def test_more_without_question_is_gentle(ask) -> None:
    with ask.conn:
        store_v4.clear_last_ask(ask.conn)
    reply = ask.service.handle_callback("v4:ask:more", CHAT_ID)
    assert "устарел" in reply.notification.lower() or reply.text


# --------------------------------------------------------------------------
# «Не знаю» и мусорный вопрос
# --------------------------------------------------------------------------


def test_unknown_question_is_honest_with_suggestion(ask) -> None:
    before = side_effects(ask)
    reply = send(ask, "какая погода в Москве?")
    lowered = reply.text.lower()
    assert "не знаю" in lowered
    assert "деньги" in lowered or "спроси" in lowered
    assert side_effects(ask) == before

    missing = send(ask, "как дела с целью Отпуск")
    assert "не знаю" in missing.text.lower()
    assert "компас" in missing.text.lower() or "цел" in missing.text.lower()


def test_garbage_question_does_not_break_bot(ask) -> None:
    for question in ("???", "сколько ???", " ? "):
        reply = send(ask, question)
        assert not reply.ignore
        assert reply.text.strip()
    assert send(ask, "что на сегодня").text.strip()


# --------------------------------------------------------------------------
# LLM разбирает вопрос в запрос к БД; правила — фолбэк
# --------------------------------------------------------------------------


class _GatewayStub:
    """LLM-шлюз без сети: отвечает заданным JSON."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload

    def available(self) -> bool:
        return True

    def json_chat(self, messages: List[Dict[str, Any]], max_tokens: int = 400) -> Dict[str, Any]:
        return dict(self.payload)


def test_llm_query_is_used_for_answer(ask) -> None:
    router = intent_v4.V4Router(
        gateway=_GatewayStub(
            {
                "intent": "query",
                "query_kind": "spent",
                "topic": "money",
                "period": "month",
                "focus": "top",
            }
        )
    )
    parsed = router.parse("сколько ушло на еду")
    assert parsed["intent"] == "query"
    assert parsed["topic"] == "money"

    service = handlers.BotService(
        ask.settings,
        ask.conn,
        intent_router=intent.build_router(None),
        v4_router=router,
        media_root=ask.tmp / "media",
    )
    reply = service.handle_text("сколько ушло на еду", CHAT_ID)
    assert "Больше всего" in reply.text
    assert "месяц" in reply.text.lower()


def test_llm_invalid_answer_falls_back_to_rules(ask) -> None:
    router = intent_v4.V4Router(
        gateway=_GatewayStub({"intent": "task", "title": "из воздуха"})
    )
    parsed = router.parse("сколько потратил за неделю")
    # Модель не вернула сумму/период — правило разбирает как finance_summary.
    assert parsed.get("intent") in {"finance_summary", "query"}
