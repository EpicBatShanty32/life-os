"""Тесты бота v4: ввод всех сущностей с телефона (задача t23).

Проверяется весь путь «сообщение → интент → запись в общей БД → видно через
API», без сети и Telegram:

* дела (дата, время, сфера, проект, приоритет, оценка), закрытие последнего;
* цели и вехи: ручной прогресс и закрытие шага;
* деньги: расход/доход, статья, счёт, сумма словами; сводка за период;
* книги: прогресс страниц, цитаты, мысли; журнал с темами и настроением;
* привычки (вода/бег/отказ), чек-ин энергии;
* правки: перенос даты, смена списка, удаление последней записи кнопкой;
* инбокс как страховка «ничего не теряется»;
* постоянная клавиатура, /today, /week;
* LLM-ветка и откат на правила без ключа;
* импорт ``app.bot.main`` не поднимает поллинг, токен не попадает в логи.

База — во временном каталоге (``tmp_path``), рабочая ``data/`` не трогается.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List

import pytest
from fastapi.testclient import TestClient

from app.bot import handlers, intent_v4
from app.server import db
from app.server.main import create_app
from app.server.settings import load_settings

CHAT_ID = 4242
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Окружение: временная БД, бот и API на одной базе
# --------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path):
    """Бот и FastAPI на одной временной базе — как в жизни, разные процессы."""
    settings = load_settings(
        env={
            "LIFEOS_ADMIN_TELEGRAM_ID": str(CHAT_ID),
            "LIFEOS_DB_PATH": str(tmp_path / "lifeos.db"),
        },
        root=tmp_path,
    )
    settings.ensure_dirs()
    conn = db.init_db(settings.db_path)
    service = handlers.BotService(
        settings, conn, v4_router=intent_v4.V4Router(gateway=None)
    )
    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        yield SimpleNamespace(
            settings=settings,
            conn=conn,
            service=service,
            client=client,
            tmp=tmp_path,
        )
    conn.close()


def send(env: Any, text: str) -> handlers.Reply:
    return env.service.handle_text(text, CHAT_ID)


def today(env: Any) -> date:
    return date.fromisoformat(env.settings.today())


def day(env: Any, offset: int) -> str:
    return (today(env) + timedelta(days=offset)).isoformat()


def count(env: Any, sql: str, params: Iterable[Any] = ()) -> int:
    row = env.conn.execute(sql, tuple(params)).fetchone()
    return int(row[0]) if row is not None else -1


def insert(env: Any, sql: str, params: Iterable[Any] = ()) -> int:
    with env.conn:
        cursor = env.conn.execute(sql, tuple(params))
        return int(cursor.lastrowid or 0)


def seed_sphere(env: Any, name: str = "Здоровье и ресурс") -> int:
    return insert(
        env,
        "INSERT INTO spheres (name, color, icon, sort, archived) VALUES (?, '#7fa890', '', 0, 0)",
        (name,),
    )


def seed_goal(env: Any, title: str = "Здоровье", *, sphere_id: int = 0, **fields: Any) -> int:
    payload = {
        "target": 100,
        "current": 20,
        "unit": "%",
        "progress": 20,
        "horizon": "year",
        "status": "active",
    }
    payload.update(fields)
    return insert(
        env,
        "INSERT INTO goals (title, target, current, unit, progress, horizon, status, sphere_id, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        (
            title,
            payload["target"],
            payload["current"],
            payload["unit"],
            payload["progress"],
            payload["horizon"],
            payload["status"],
            sphere_id or None,
        ),
    )


def seed_step(env: Any, goal_id: int, title: str, sort: int = 0) -> int:
    return insert(
        env,
        "INSERT INTO goal_steps (goal_id, title, done, sort, created) "
        "VALUES (?, ?, 0, ?, '2026-01-01T00:00:00Z')",
        (goal_id, title, sort),
    )


def seed_project(env: Any, name: str = "Ремонт") -> int:
    return insert(
        env,
        "INSERT INTO projects (name, status, created) VALUES (?, 'active', '2026-01-01T00:00:00Z')",
        (name,),
    )


def seed_task(env: Any, title: str, list_kind: str = "work", due: str | None = None) -> int:
    return insert(
        env,
        "INSERT INTO tasks (title, list_kind, due_date, status, resource, created) "
        "VALUES (?, ?, ?, 'todo', 'important', '2026-01-01T00:00:00Z')",
        (title, list_kind, due),
    )


# --------------------------------------------------------------------------
# Дела
# --------------------------------------------------------------------------


def test_task_date_sphere_and_api(env) -> None:
    reply = send(env, "завтра позвонить в банк")
    assert "Позвонить в банк" in reply.text
    assert "завтра" in reply.text

    row = env.conn.execute("SELECT * FROM tasks").fetchone()
    assert row["title"] == "Позвонить в банк"
    assert row["due_date"] == day(env, 1)
    assert row["status"] == "todo"
    assert row["resource"] == "important"
    sphere = env.conn.execute(
        "SELECT name FROM spheres WHERE id = ?", (row["sphere_id"],)
    ).fetchone()
    assert sphere["name"] == "Финансы и быт"

    payload = env.client.get(
        "/api/tasks", params={"view": "day", "date": day(env, 1)}
    ).json()
    titles = [item["title"] for item in payload["items"]]
    assert "Позвонить в банк" in titles


def test_task_time_priority_estimate(env) -> None:
    send(env, "в пятницу срочно стоматолог в 15, 45 минут")
    row = env.conn.execute("SELECT * FROM tasks").fetchone()
    assert row["title"] == "Стоматолог"
    assert row["start_at"] == "%sT15:00:00" % row["due_date"]
    assert row["resource"] == "must"
    assert "Оценка: 45 мин" in row["notes"]
    assert row["due_date"] >= day(env, 0)


def test_task_project_link(env) -> None:
    project_id = seed_project(env, "Ремонт")
    send(env, "завтра позвонить по ремонту")
    row = env.conn.execute("SELECT * FROM tasks").fetchone()
    assert row["project_id"] == project_id
    payload = env.client.get(
        "/api/tasks", params={"view": "day", "date": day(env, 1)}
    ).json()
    assert payload["items"][0]["projectName"] == "Ремонт"


def test_task_done_by_title(env) -> None:
    send(env, "сегодня позвонить в банк")
    send(env, "сегодня купить хлеб")
    reply = send(env, "сделал хлеб")
    assert "Купить хлеб" in reply.text
    done = env.conn.execute(
        "SELECT title FROM tasks WHERE status = 'done'"
    ).fetchall()
    assert [row["title"] for row in done] == ["Купить хлеб"]
    payload = env.client.get(
        "/api/tasks", params={"view": "day", "date": day(env, 0), "status": "all"}
    ).json()
    statuses = {item["title"]: item["status"] for item in payload["items"]}
    assert statuses["Купить хлеб"] == "done"
    assert statuses["Позвонить в банк"] == "todo"


def test_task_done_plain_gotovo_closes_latest(env) -> None:
    send(env, "сегодня позвонить в банк")
    send(env, "сегодня купить хлеб")
    send(env, "готово")
    done = env.conn.execute("SELECT title FROM tasks WHERE status = 'done'").fetchall()
    assert [row["title"] for row in done] == ["Купить хлеб"]


# --------------------------------------------------------------------------
# Цели и вехи
# --------------------------------------------------------------------------


def test_goal_progress_existing_goal(env) -> None:
    sphere_id = seed_sphere(env)
    goal_id = seed_goal(env, "Здоровье", sphere_id=sphere_id)
    reply = send(env, "+10% к цели Здоровье")
    assert "30%" in reply.text

    row = env.conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    assert int(row["progress"]) == 30
    assert float(row["current"]) == pytest.approx(30.0)

    compass = env.client.get("/api/compass").json()
    goal = next(item for item in compass["goals"] if item["id"] == goal_id)
    assert goal["progressPct"] == 30


def test_goal_progress_creates_missing_goal(env) -> None:
    send(env, "+10% к цели Здоровье")
    row = env.conn.execute("SELECT * FROM goals").fetchone()
    assert row["title"] == "Здоровье"
    assert int(row["progress"]) == 10
    compass = env.client.get("/api/compass").json()
    assert compass["goals"][0]["title"] == "Здоровье"


def test_goal_step_first_open_step(env) -> None:
    sphere_id = seed_sphere(env)
    goal_id = seed_goal(env, "Здоровье", sphere_id=sphere_id)
    first = seed_step(env, goal_id, "Взвешиваться пять раз в неделю", 0)
    second = seed_step(env, goal_id, "Держать дефицит", 1)
    reply = send(env, "веха к цели Здоровье выполнена")
    assert "Взвешиваться" in reply.text

    open_steps = env.conn.execute(
        "SELECT id FROM goal_steps WHERE done = 0 ORDER BY sort"
    ).fetchall()
    assert [int(row["id"]) for row in open_steps] == [second]
    assert int(
        env.conn.execute("SELECT done FROM goal_steps WHERE id = ?", (first,)).fetchone()[0]
    ) == 1

    compass = env.client.get("/api/compass").json()
    goal = next(item for item in compass["goals"] if item["id"] == goal_id)
    assert goal["steps"][0]["done"] is True


def test_goal_step_named_in_quotes(env) -> None:
    goal_id = seed_goal(env, "Здоровье")
    step_id = seed_step(env, goal_id, "Держать дефицит", 0)
    send(env, "веха «Держать дефицит» выполнена")
    row = env.conn.execute("SELECT done FROM goal_steps WHERE id = ?", (step_id,)).fetchone()
    assert int(row["done"]) == 1


# --------------------------------------------------------------------------
# Деньги
# --------------------------------------------------------------------------


def test_expense_transport_with_default_account(env) -> None:
    reply = send(env, "такси 450")
    assert "450 ₽" in reply.text

    row = env.conn.execute(
        "SELECT t.*, c.name AS category_name, a.kind AS account_kind "
        "FROM transactions AS t "
        "LEFT JOIN finance_categories AS c ON c.id = t.category_id "
        "LEFT JOIN finance_accounts AS a ON a.id = t.account_id"
    ).fetchone()
    assert int(row["amount_kopecks"]) == 45000
    assert row["kind"] == "expense"
    assert row["category_name"] == "Транспорт"
    assert row["account_kind"] == "card"

    month = env.settings.today()[:7]
    payload = env.client.get("/api/finance/transactions", params={"month": month}).json()
    assert payload["items"][0]["amount"] == pytest.approx(450.0)
    assert payload["items"][0]["categoryName"] == "Транспорт"


def test_expense_cash_and_category(env) -> None:
    send(env, "кофе 320 наличными")
    row = env.conn.execute(
        "SELECT t.*, c.name AS category_name, a.kind AS account_kind "
        "FROM transactions AS t "
        "LEFT JOIN finance_categories AS c ON c.id = t.category_id "
        "LEFT JOIN finance_accounts AS a ON a.id = t.account_id"
    ).fetchone()
    assert int(row["amount_kopecks"]) == 32000
    assert row["category_name"] == "Кафе и рестораны"
    assert row["account_kind"] == "cash"


def test_income_salary(env) -> None:
    reply = send(env, "зарплата 120000")
    assert "доход" in reply.text.lower()
    row = env.conn.execute(
        "SELECT t.*, c.name AS category_name, c.kind AS category_kind "
        "FROM transactions AS t LEFT JOIN finance_categories AS c ON c.id = t.category_id"
    ).fetchone()
    assert int(row["amount_kopecks"]) == 12000000
    assert row["kind"] == "income"
    assert row["category_kind"] == "income"
    assert row["category_name"] == "Зарплата"


def test_expense_words_thousand_two_hundred(env) -> None:
    send(env, "потратил тысячу двести на такси")
    row = env.conn.execute("SELECT amount_kopecks, kind FROM transactions").fetchone()
    assert int(row["amount_kopecks"]) == 120000
    assert row["kind"] == "expense"


def test_finance_summary_week(env) -> None:
    send(env, "такси 450")
    send(env, "кофе 320 наличными")
    before = count(env, "SELECT COUNT(*) FROM transactions")
    reply = send(env, "сколько потратил за неделю")
    assert "Расходы за неделю" in reply.text
    assert "770 ₽" in reply.text
    assert "Транспорт" in reply.text
    assert count(env, "SELECT COUNT(*) FROM transactions") == before


# --------------------------------------------------------------------------
# Книги и журнал
# --------------------------------------------------------------------------


def test_book_progress_quote_insight_and_api(env) -> None:
    reply = send(env, "читаю Sapiens стр 120")
    assert "Sapiens" in reply.text

    book = env.conn.execute("SELECT * FROM books").fetchone()
    assert book["title"] == "Sapiens"
    assert int(book["pages_read"]) == 120
    assert book["shelf"] == "reading"

    send(env, "цитата: Мы то, что мы делаем каждый день")
    send(env, "мысль из книги: важно отдыхать")

    assert count(env, "SELECT COUNT(*) FROM book_quotes WHERE book_id = ?", (book["id"],)) == 1
    assert count(env, "SELECT COUNT(*) FROM book_insights WHERE book_id = ?", (book["id"],)) == 1

    payload = env.client.get("/api/books").json()
    assert payload["items"][0]["title"] == "Sapiens"
    assert payload["items"][0]["pagesRead"] == 120

    card = env.client.get("/api/books/%d" % int(book["id"])).json()
    assert len(card["quotes"]) == 1
    assert len(card["insights"]) == 1


def test_book_quote_page_number(env) -> None:
    send(env, "читаю Sapiens стр 120")
    send(env, "цитата: Мы то, что мы делаем каждый день, стр 42")
    row = env.conn.execute("SELECT text, page FROM book_quotes").fetchone()
    assert "стр 42" not in row["text"]
    assert int(row["page"]) == 42


def test_journal_free_text_topics_mood(env) -> None:
    reply = send(env, "Думал про работу, устал и хочу в отпуск")
    assert "Мысль записана" in reply.text
    assert "работа" in reply.text
    assert "усталость" in reply.text

    row = env.conn.execute("SELECT * FROM entries").fetchone()
    assert row["kind"] == "journal"
    meta = json.loads(row["meta_json"])
    assert "работа" in meta["topics"]
    assert meta["mood"] == "усталость"

    payload = env.client.get("/api/journal").json()
    assert payload["items"][0]["kind"] == "journal"
    assert payload["items"][0]["text"].startswith("Думал про работу")


# --------------------------------------------------------------------------
# Привычки и чек-ин
# --------------------------------------------------------------------------


def test_habit_water_marks_habit_and_water_metric(env) -> None:
    reply = send(env, "выпил 2 л")
    assert "Вода 2 литра" in reply.text

    habit = env.conn.execute("SELECT * FROM habits").fetchone()
    assert habit["name"] == "Вода 2 литра"
    mark = env.conn.execute("SELECT * FROM habit_marks").fetchone()
    assert int(mark["habit_id"]) == int(habit["id"])
    assert mark["date"] == env.settings.today()
    assert float(mark["value"]) == pytest.approx(2.0)
    water = env.conn.execute(
        "SELECT COALESCE(SUM(value), 0) FROM metrics WHERE kind = 'water'"
    ).fetchone()
    assert float(water[0]) == pytest.approx(2000.0)

    summary = env.client.get("/api/personal/summary").json()
    habit_payload = next(item for item in summary["habits"] if item["name"] == "Вода 2 литра")
    assert habit_payload["marks"][env.settings.today()] == 1


def test_habit_run(env) -> None:
    send(env, "пробежал 5 км")
    habit = env.conn.execute("SELECT name FROM habits").fetchone()
    assert habit["name"] == "Бег"
    mark = env.conn.execute("SELECT value FROM habit_marks").fetchone()
    assert float(mark["value"]) == pytest.approx(5.0)


def test_habit_not_smoking(env) -> None:
    send(env, "не курил")
    habit = env.conn.execute("SELECT name FROM habits").fetchone()
    assert habit["name"] == "Не курить"
    assert count(env, "SELECT COUNT(*) FROM habit_marks WHERE value > 0") == 1


def test_checkin_energy_and_note(env) -> None:
    reply = send(env, "энергия 4, устал после созвонов")
    assert "энергия 4/5" in reply.text

    row = env.conn.execute("SELECT * FROM checkins").fetchone()
    assert int(row["energy"]) == 4
    assert row["mood"] == "усталость"
    assert "устал после созвонов" in row["note"]

    summary = env.client.get("/api/personal/summary").json()
    assert summary["checkin"]["energy"] == 4
    assert summary["checkin"]["mood"] == "усталость"


def test_checkin_repeat_overwrites_and_undo_restores(env) -> None:
    send(env, "энергия 4, устал после созвонов")
    send(env, "энергия 3, спокойно")
    row = env.conn.execute("SELECT energy FROM checkins").fetchone()
    assert int(row["energy"]) == 3
    assert count(env, "SELECT COUNT(*) FROM checkins") == 1

    send(env, "удали последнее")
    env.service.handle_callback("v4:del:confirm", CHAT_ID)
    row = env.conn.execute("SELECT energy, mood FROM checkins").fetchone()
    assert int(row["energy"]) == 4
    assert row["mood"] == "усталость"


# --------------------------------------------------------------------------
# Правки и удаление
# --------------------------------------------------------------------------


def test_reschedule_task_to_tomorrow(env) -> None:
    send(env, "сегодня позвонить в банк")
    reply = send(env, "перенеси на завтра")
    assert "завтра" in reply.text

    row = env.conn.execute("SELECT due_date FROM tasks").fetchone()
    assert row["due_date"] == day(env, 1)
    payload = env.client.get(
        "/api/tasks", params={"view": "day", "date": day(env, 1)}
    ).json()
    assert payload["items"][0]["title"] == "Позвонить в банк"


def test_move_task_to_work_list(env) -> None:
    task_id = seed_task(env, "Забрать посылку", list_kind="personal")
    send(env, "это в работу")
    row = env.conn.execute("SELECT list_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["list_kind"] == "work"
    payload = env.client.get("/api/tasks", params={"view": "kanban"}).json()
    assert payload["items"][0]["listKind"] == "work"


def test_move_task_to_personal_list(env) -> None:
    task_id = seed_task(env, "Позвонить клиенту", list_kind="work")
    send(env, "это в личное")
    row = env.conn.execute("SELECT list_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["list_kind"] == "personal"


def test_delete_last_transaction_requires_confirmation(env) -> None:
    send(env, "такси 450")
    assert count(env, "SELECT COUNT(*) FROM transactions") == 1

    prompt = send(env, "удали последнее")
    assert "Удалить" in prompt.text
    callbacks = [button[1] for row in prompt.buttons for button in row]
    assert "v4:del:confirm" in callbacks
    assert count(env, "SELECT COUNT(*) FROM transactions") == 1  # без подтверждения не удаляем

    reply = env.service.handle_callback("v4:del:confirm", CHAT_ID)
    assert "Удалено" in reply.text
    assert count(env, "SELECT COUNT(*) FROM transactions") == 0
    assert env.client.get("/api/finance/transactions").json()["items"] == []


def test_delete_last_task(env) -> None:
    send(env, "сегодня позвонить в банк")
    send(env, "удали последнее")
    env.service.handle_callback("v4:del:confirm", CHAT_ID)
    assert count(env, "SELECT COUNT(*) FROM tasks") == 0


def test_delete_last_journal_entry(env) -> None:
    send(env, "просто мысль про отдых")
    send(env, "удали последнее")
    env.service.handle_callback("v4:del:confirm", CHAT_ID)
    assert count(env, "SELECT COUNT(*) FROM entries") == 0


def test_delete_cancel_keeps_record(env) -> None:
    send(env, "такси 450")
    send(env, "удали последнее")
    reply = env.service.handle_callback("v4:del:cancel", CHAT_ID)
    assert "Отмена" in reply.text
    assert count(env, "SELECT COUNT(*) FROM transactions") == 1


def test_delete_habit_mark_restores_nothing(env) -> None:
    send(env, "не курил")
    assert count(env, "SELECT COUNT(*) FROM habit_marks") == 1
    send(env, "удали последнее")
    env.service.handle_callback("v4:del:confirm", CHAT_ID)
    assert count(env, "SELECT COUNT(*) FROM habit_marks") == 0
    assert count(env, "SELECT COUNT(*) FROM habits") == 0


# --------------------------------------------------------------------------
# Инбокс: ничего не теряется
# --------------------------------------------------------------------------


def test_unrecognised_text_goes_to_inbox(env) -> None:
    reply = send(env, "12345")
    assert "инбокс" in reply.text.lower()
    row = env.conn.execute("SELECT * FROM inbox_items").fetchone()
    assert row["text"] == "12345"
    assert row["status"] == "new"

    payload = env.client.get("/api/inbox").json()
    assert payload["items"][0]["text"] == "12345"
    assert payload["counts"]["new"] == 1


def test_free_text_is_not_lost(env) -> None:
    send(env, "надо подумать, как перестроить неделю")
    assert count(env, "SELECT COUNT(*) FROM entries WHERE kind = 'journal'") == 1


# --------------------------------------------------------------------------
# Клавиатура, /today, /week
# --------------------------------------------------------------------------


def test_reply_keyboard_is_permanent(env) -> None:
    reply = env.service.handle_start()
    assert reply.keyboard == [
        ["Дело", "Расход", "Еда"],
        ["Привычка", "Мысль", "Сводка"],
    ]
    markup = handlers._reply_keyboard(reply)
    assert markup is not None
    assert markup.is_persistent is True
    labels = [button.text for row in markup.keyboard for button in row]
    assert labels == ["Дело", "Расход", "Еда", "Привычка", "Мысль", "Сводка"]

    assert env.service.handle_today().keyboard == reply.keyboard
    assert env.service.handle_week().keyboard == reply.keyboard


def test_keyboard_buttons_send_plain_text(env) -> None:
    for label in ("Дело", "Расход", "Еда", "Привычка", "Мысль"):
        reply = send(env, label)
        assert reply.text
        assert "Напиши" in reply.text or "Пришли" in reply.text
    summary = send(env, "Сводка")
    assert summary.text.startswith("Сегодня,")
    # Кнопки-подсказки сами по себе ничего не записывают.
    assert count(env, "SELECT COUNT(*) FROM entries") == 0


def test_today_summary(env) -> None:
    send(env, "сегодня позвонить в банк")
    send(env, "такси 450")
    send(env, "съел 600 ккал и 40 г белка")
    send(env, "выпил 2 л")
    send(env, "энергия 4, устал после созвонов")

    text = env.service.handle_today().text
    assert text.startswith("Сегодня,")
    assert "Позвонить в банк" in text
    assert "450 ₽" in text
    assert "600 ккал" in text
    assert "вода 2 л" in text
    assert "энергия 4/5" in text
    assert "Привычки" in text


def test_today_empty_is_honest(env) -> None:
    text = env.service.handle_today().text
    assert "Дел на сегодня нет" in text
    assert "Чек-ина нет" in text


def test_week_summary(env) -> None:
    send(env, "сегодня позвонить в банк")
    send(env, "такси 450")
    send(env, "энергия 4")
    text = env.service.handle_week().text
    assert text.startswith("Итоги недели")
    assert "Дела:" in text
    assert "Деньги:" in text
    assert "Энергия:" in text


# --------------------------------------------------------------------------
# LLM-ветка и откат на правила
# --------------------------------------------------------------------------


class FakeGateway:
    """Фейковый LLM-шлюз: отдаёт заранее заданный ответ или падает."""

    def __init__(self, data: Any = None, error: Any = None, available: bool = True) -> None:
        self.data = data
        self.error = error
        self._available = available
        self.calls: List[List[Dict[str, str]]] = []

    def available(self) -> bool:
        return self._available

    def json_chat(self, messages: List[Dict[str, str]], max_tokens: int = 700) -> Any:
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return self.data


def test_llm_answer_is_used_for_task(env) -> None:
    gateway = FakeGateway(
        {
            "intent": "task",
            "title": "Оплатить интернет",
            "due_date": day(env, 2),
            "resource": "must",
        }
    )
    service = handlers.BotService(
        env.settings, env.conn, v4_router=intent_v4.V4Router(gateway=gateway)
    )
    reply = service.handle_text("оплатить интернет", CHAT_ID)
    assert "Оплатить интернет" in reply.text
    row = env.conn.execute("SELECT * FROM tasks").fetchone()
    assert row["title"] == "Оплатить интернет"
    assert row["due_date"] == day(env, 2)
    assert row["resource"] == "must"
    assert gateway.calls


def test_llm_invalid_answer_falls_back_to_rules(env) -> None:
    gateway = FakeGateway({"kind": "note", "title": "не тот формат"})
    service = handlers.BotService(
        env.settings, env.conn, v4_router=intent_v4.V4Router(gateway=gateway)
    )
    reply = service.handle_text("такси 450", CHAT_ID)
    assert "450 ₽" in reply.text
    assert count(env, "SELECT COUNT(*) FROM transactions") == 1


def test_llm_failure_does_not_break_bot(env) -> None:
    gateway = FakeGateway(error=RuntimeError("network down"))
    service = handlers.BotService(
        env.settings, env.conn, v4_router=intent_v4.V4Router(gateway=gateway)
    )
    reply = service.handle_text("завтра позвонить в банк", CHAT_ID)
    assert "Позвонить в банк" in reply.text
    assert count(env, "SELECT COUNT(*) FROM tasks") == 1


def test_router_without_key_works(env) -> None:
    router = intent_v4.build_router(env.settings)
    parsed = router.parse("выпил 2 л")
    assert parsed["intent"] == "habit"
    assert parsed["engine"] == "rules"


# --------------------------------------------------------------------------
# Запуск и секреты
# --------------------------------------------------------------------------


def test_import_main_does_not_start_polling() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import app.bot.main; print('imported')"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "imported" in result.stdout
    combined = (result.stdout + result.stderr).lower()
    assert "polling" not in combined


def test_token_is_not_logged(env, caplog) -> None:
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    settings = load_settings(
        env={
            "LIFEOS_ADMIN_TELEGRAM_ID": str(CHAT_ID),
            "LIFEOS_DB_PATH": str(env.tmp / "token.db"),
            "LIFEOS_TELEGRAM_TOKEN": token,
        },
        root=env.tmp,
    )
    assert token not in repr(settings)
    assert token not in json.dumps(settings.safe_dict(), ensure_ascii=False)

    conn = db.init_db(settings.db_path)
    try:
        service = handlers.BotService(settings, conn, v4_router=intent_v4.V4Router(gateway=None))
        with caplog.at_level(logging.DEBUG):
            service.handle_text("завтра позвонить в банк", CHAT_ID)
            service.handle_today()
            service.handle_week()
        assert token not in caplog.text
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Приёмка QA t26: еда раньше денег, суммы, вопросы, практики, «делаю X»
# --------------------------------------------------------------------------


def test_food_with_grams_is_food_not_money(env) -> None:
    """t25/t32: «продукт + граммы» — еда; каждый продукт отдельной строкой."""
    reply = send(env, "на обед гречка 200 г и курица 150")
    assert "ед" in reply.text.lower()
    rows = env.conn.execute(
        "SELECT grams, meal, text FROM food_log ORDER BY id"
    ).fetchall()
    assert len(rows) == 2, "два продукта — две строки дневника"
    assert [int(float(row["grams"])) for row in rows] == [200, 150]
    assert {str(row["meal"]) for row in rows} == {"lunch"}
    assert "гречка" in str(rows[0]["text"]).lower()
    assert "куриц" in str(rows[1]["text"]).lower()
    assert count(env, "SELECT COUNT(*) FROM transactions") == 0


def test_amount_with_space_groups_is_320(env) -> None:
    reply = send(env, "купил кафе 3 20")
    assert "расход" in reply.text.lower() or "320" in reply.text
    row = env.conn.execute(
        "SELECT amount_kopecks, kind FROM transactions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert int(row["amount_kopecks"]) == 32000
    assert str(row["kind"]) == "expense"


def test_question_answers_data_and_creates_no_record(env) -> None:
    send(env, "завтра позвонить в банк")
    tasks_before = count(env, "SELECT COUNT(*) FROM tasks")
    reply = send(env, "какие задачи на завтра")
    assert "банк" in reply.text.lower()
    assert count(env, "SELECT COUNT(*) FROM tasks") == tasks_before

    send(env, "энергия 3")
    reply = send(env, "сколько энергии вчера")
    assert "чек-ин" in reply.text.lower() or "энерг" in reply.text.lower()
    assert count(env, "SELECT COUNT(*) FROM tasks") == tasks_before


def test_practice_with_typos_awards_points_and_memo(env) -> None:
    reply = send(env, "делаю дыхательную пратикуку вима хоффа")
    assert "\n" not in reply.text, "подтверждение практики должно быть одной строкой"
    lowered = reply.text.lower()
    assert "вим хофа" in lowered and "очк" in lowered
    session = env.conn.execute(
        "SELECT rounds_done FROM practice_sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert session is not None
    assert int(session["rounds_done"]) == 1
    points = env.conn.execute(
        "SELECT COALESCE(SUM(points), 0) FROM points_ledger WHERE kind = 'practice'"
    ).fetchone()
    assert int(points[0]) == 10

    second = send(env, "вимхоф 2 круга")
    assert "уже учтено" in second.text.lower()
    assert int(second.text.split(" — ")[1].split(" ")[0]) == 2
    usage = env.conn.execute(
        "SELECT meta_json FROM usage_events WHERE screen = 'practice' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    meta = json.loads(usage["meta_json"])
    assert meta["intent"] == "practice"
    assert "вимхоф" in meta["memo"].lower()


def test_current_state_phrase_goes_to_journal_not_inbox(env) -> None:
    reply = send(env, "делаю life os")
    assert "inbox" not in reply.text.lower()
    assert count(env, "SELECT COUNT(*) FROM inbox_items") == 0
    entries = count(
        env,
        "SELECT COUNT(*) FROM entries WHERE text LIKE '%life os%'",
    )
    assert entries == 1


def test_inbox_reply_tells_what_to_do(env) -> None:
    reply = send(env, "абракадабра блабла")
    lowered = reply.text.lower()
    assert "инбокс" in lowered
    assert "разбер" in lowered
    assert count(env, "SELECT COUNT(*) FROM inbox_items") == 1
