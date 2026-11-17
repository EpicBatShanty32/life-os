"""Тесты демо-данных v4 в app/server/seeder.py (§14 handoff).

Проверяется контракт сида новых модулей:

* объём и состав: 5 сфер, 9 целей с вехами, анти-цели, проекты, ~120 задач,
  инбокс, 12 книг по полкам, счета/категории/транзакции/бюджеты, чек-ины, итоги;
* детерминированность и идемпотентность: второй прогон не плодит дубли и даёт
  те же счётчики, что и первый;
* ``--reset`` удаляет только демо-строки и не трогает ``source='telegram'``
  и собственные записи пользователя в новых таблицах;
* целостность данных: копейки целые, kind категорий совпадает с транзакцией,
  даты вокруг «сегодня», уникальные даты чек-инов и периоды итогов;
* приёмка CLI: ``--db <tmp> --days 120`` дважды идемпотентен.

Все базы — во временных каталогах; ``data/lifeos-v3.db`` не затрагивается.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.server.db import connect, migrate
from app.server.seeder import (
    DEMO_ACCOUNT_NAMES,
    DEMO_ANTI_GOALS,
    DEMO_BOOK_TITLES,
    DEMO_BOOKS,
    DEMO_CATEGORIES,
    DEMO_CHECKIN_MOODS,
    DEMO_GOALS,
    DEMO_INBOX_TEXTS,
    DEMO_PROJECTS,
    DEMO_PROJECT_NAMES,
    DEMO_PROJECT_NOTES,
    DEMO_SOURCE,
    DEMO_TASK_POOL,
    DEMO_SPHERES,
    DEMO_TASK_TITLES,
    SeedStats,
    clear_demo,
    seed,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 11, 17)
DAYS = 120

#: Таблицы новых сущностей v4.
V4_TABLES: tuple[str, ...] = (
    "spheres",
    "goal_steps",
    "anti_goals",
    "projects",
    "project_steps",
    "project_notes",
    "tasks",
    "inbox_items",
    "books",
    "book_insights",
    "book_quotes",
    "book_actions",
    "finance_accounts",
    "finance_categories",
    "transactions",
    "budgets",
    "checkins",
    "reviews",
)


# ---------------------------------------------------------------------------
# Фикстуры и помощники
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """Пустая база схемы v3 (user_version = 3) во временном каталоге."""
    connection = connect(tmp_path / "seeder-v4.db")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture()
def seeded(db: sqlite3.Connection) -> sqlite3.Connection:
    """База с полным демо-набором v4 (фиксированная дата — детерминизм)."""
    seed(db, days=DAYS, today=TODAY)
    return db


def count(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> int:
    sql = "SELECT COUNT(*) FROM %s" % table
    if where:
        sql += " WHERE " + where
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> object:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def _snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Содержимое ключевых таблиц для сравнения двух прогонов."""
    return {
        "tasks": rows(
            connection,
            "SELECT title, list_kind, due_date, status, resource, recurrence, start_at "
            "FROM tasks ORDER BY id",
        ),
        "transactions": rows(
            connection,
            "SELECT t.ts, t.amount_kopecks, t.kind, c.name, a.name, t.note "
            "FROM transactions AS t "
            "LEFT JOIN finance_categories AS c ON c.id = t.category_id "
            "LEFT JOIN finance_accounts AS a ON a.id = t.account_id "
            "ORDER BY t.ts, t.amount_kopecks, t.note",
        ),
        "books": rows(connection, "SELECT title, shelf, pages_read FROM books ORDER BY id"),
        "goals": rows(
            connection,
            "SELECT g.title, g.horizon, s.name, g.deadline FROM goals AS g "
            "LEFT JOIN spheres AS s ON s.id = g.sphere_id ORDER BY g.title",
        ),
        "checkins": rows(connection, "SELECT date, energy, mood FROM checkins ORDER BY date"),
        "reviews": rows(
            connection, "SELECT period, start_date, end_date, facts_json FROM reviews ORDER BY id"
        ),
    }


# ---------------------------------------------------------------------------
# Объём и состав демо-набора
# ---------------------------------------------------------------------------


def test_spheres(seeded: sqlite3.Connection) -> None:
    """Пять сфер из §10.1: уникальные имена, цвета и иконки, сортировка."""
    spheres = rows(seeded, "SELECT name, color, icon, sort, archived FROM spheres ORDER BY sort")
    assert [row[0] for row in spheres] == [name for name, _, _ in DEMO_SPHERES]
    assert len(spheres) == 5
    for name, color, icon, sort, archived in spheres:
        assert color.startswith("#") and len(color) == 7
        assert icon
        assert archived == 0
    assert [row[3] for row in spheres] == [0, 1, 2, 3, 4]


def test_goals_by_horizons_with_steps(seeded: sqlite3.Connection) -> None:
    """9 целей: год/квартал/месяц, сфера, «зачем», прогресс 0, вехи."""
    goals = rows(
        seeded,
        "SELECT id, title, horizon, sphere_id, why, progress, deadline FROM goals ORDER BY id",
    )
    assert [row[1] for row in goals] == [title for title, _, _, _ in DEMO_GOALS]
    assert {row[2] for row in goals} == {"year", "quarter", "month"}
    assert count(seeded, "goals", "horizon = 'year'") >= 3
    assert count(seeded, "goals", "horizon = 'quarter'") >= 3
    assert count(seeded, "goals", "horizon = 'month'") >= 2

    sphere_ids = {int(row[0]) for row in rows(seeded, "SELECT id FROM spheres")}
    for goal_id, title, horizon, sphere_id, why, progress, deadline in goals:
        assert sphere_id in sphere_ids, "у цели %s нет сферы" % title
        assert why
        assert progress == 0  # ручного оверрайда нет: считаем по вехам
        days_left = (date.fromisoformat(str(deadline)) - TODAY).days
        assert 0 < days_left <= {"year": 400, "quarter": 120, "month": 40}[horizon]
        steps = rows(
            seeded,
            "SELECT title, done, due, sort FROM goal_steps WHERE goal_id = ? ORDER BY sort",
            (goal_id,),
        )
        assert 3 <= len(steps) <= 4, "у цели %s %d вех" % (title, len(steps))
        assert [row[3] for row in steps] == list(range(len(steps)))
        done_flags = [int(row[1]) for row in steps]
        assert set(done_flags) <= {0, 1}
        assert 1 <= sum(done_flags) < len(steps), "вехи цели %s должны быть частично закрыты" % title
        assert all(row[2] for row in steps)

    steps_total = count(seeded, "goal_steps")
    assert steps_total == len(DEMO_GOALS) * 3 + 3  # 9 целей: три по 3 вехи, шесть по 4


def test_anti_goals(seeded: sqlite3.Connection) -> None:
    """2–3 анти-цели: месяц и квартал, активные, без дублей текста."""
    anti_goals = rows(seeded, "SELECT period, text, active FROM anti_goals ORDER BY id")
    assert len(anti_goals) == len(DEMO_ANTI_GOALS) == 3
    assert [row[0] for row in anti_goals] == [period for period, _ in DEMO_ANTI_GOALS]
    assert [row[1] for row in anti_goals] == [text for _, text in DEMO_ANTI_GOALS]
    assert all(int(row[2]) == 1 for row in anti_goals)
    assert count(seeded, "anti_goals", "period = 'month'") == 2
    assert count(seeded, "anti_goals", "period = 'quarter'") == 1


def test_projects_with_steps_and_notes(seeded: sqlite3.Connection) -> None:
    """4–6 проектов со статусами, этапами, заметками и ссылками."""
    projects = rows(
        seeded,
        "SELECT id, name, status, deadline, outcome, sphere_id, goal_id FROM projects ORDER BY id",
    )
    assert 4 <= len(projects) <= 6
    assert [row[1] for row in projects] == list(DEMO_PROJECT_NAMES)
    statuses = {row[2] for row in projects}
    assert {"idea", "active", "paused", "done"}.issubset(statuses)
    assert statuses <= {"idea", "active", "paused", "done"}
    sphere_ids = {int(row[0]) for row in rows(seeded, "SELECT id FROM spheres")}
    goal_ids = {int(row[0]) for row in rows(seeded, "SELECT id FROM goals")}
    for project_id, name, status, deadline, outcome, sphere_id, goal_id in projects:
        assert sphere_id in sphere_ids
        assert goal_id is None or goal_id in goal_ids
        assert outcome
        assert deadline
        steps = rows(
            seeded,
            "SELECT title, done, sort FROM project_steps WHERE project_id = ? ORDER BY sort",
            (project_id,),
        )
        expected = next(item[5] for item in DEMO_PROJECTS if item[0] == name)
        assert len(steps) == len(expected)
        assert [row[0] for row in steps] == [step for step, _ in expected]
        assert [int(row[1]) for row in steps] == [1 if done else 0 for _, done in expected]
        assert [row[2] for row in steps] == list(range(len(expected)))

    notes = rows(seeded, "SELECT project_id, kind, title, body, url FROM project_notes ORDER BY id")
    assert len(notes) == len(DEMO_PROJECT_NOTES)
    assert {row[1] for row in notes} == {"note", "link"}
    for _, kind, title, body, url in notes:
        assert title
        if kind == "note":
            assert body and not url
        else:
            assert url.startswith("https://") and not body


def test_tasks_volume_and_variety(seeded: sqlite3.Connection) -> None:
    """~120 задач: закрытые/открытые/отменённые, жёсткие события и повторения."""
    total = count(seeded, "tasks")
    assert 110 <= total <= 130
    assert total == len(DEMO_TASK_POOL) * 4  # 30 названий в пуле → 120 задач

    statuses = {row[0] for row in rows(seeded, "SELECT DISTINCT status FROM tasks")}
    assert statuses == {"todo", "done", "cancelled"}
    resources = {row[0] for row in rows(seeded, "SELECT DISTINCT resource FROM tasks")}
    assert resources == {"must", "important", "low"}
    list_kinds = {row[0] for row in rows(seeded, "SELECT DISTINCT list_kind FROM tasks")}
    assert list_kinds == {"work", "personal"}

    assert count(seeded, "tasks", "start_at IS NOT NULL AND end_at IS NOT NULL") == 8
    for start_at, end_at in rows(
        seeded, "SELECT start_at, end_at FROM tasks WHERE start_at IS NOT NULL"
    ):
        assert start_at < end_at
    assert count(seeded, "tasks", "status = 'done' AND done_at IS NOT NULL") == count(
        seeded, "tasks", "status = 'done'"
    )
    assert count(seeded, "tasks", "status = 'todo' AND due_date IS NOT NULL") == count(
        seeded, "tasks", "status = 'todo'"
    )
    recurrences = {row[0] for row in rows(seeded, "SELECT DISTINCT recurrence FROM tasks")}
    assert recurrences <= {"", "daily", "weekly:1,3,5", "weekly:2,4", "monthly:15"}
    assert count(seeded, "tasks", "recurrence <> ''") >= 10
    assert count(seeded, "tasks", "list_kind = 'personal'") >= 30
    assert count(seeded, "tasks", "tags <> ''") >= 30
    assert count(seeded, "tasks", "project_id IS NOT NULL") >= 10
    assert count(seeded, "tasks", "goal_id IS NOT NULL") >= 5
    assert count(seeded, "tasks", "sphere_id IS NULL") == 0

    titles = {row[0] for row in rows(seeded, "SELECT title FROM tasks")}
    assert titles <= set(DEMO_TASK_TITLES)


def test_tasks_dates_around_today(seeded: sqlite3.Connection) -> None:
    """Сроки задач — вокруг сегодня: от небольшой просрочки до двух недель вперёд."""
    todo_due = [
        date.fromisoformat(str(row[0]))
        for row in rows(
            seeded, "SELECT due_date FROM tasks WHERE status = 'todo' AND due_date IS NOT NULL"
        )
    ]
    assert todo_due
    assert min(todo_due) >= TODAY - timedelta(days=10)
    assert max(todo_due) <= TODAY + timedelta(days=21)
    assert any(due == TODAY for due in todo_due)

    done_due = [
        date.fromisoformat(str(row[0]))
        for row in rows(
            seeded, "SELECT due_date FROM tasks WHERE status = 'done' AND due_date IS NOT NULL"
        )
    ]
    assert done_due
    assert max(done_due) <= TODAY

    created = [str(row[0])[:10] for row in rows(seeded, "SELECT created FROM tasks")]
    for day in created:
        assert date.fromisoformat(day) <= TODAY
        assert date.fromisoformat(day) >= TODAY - timedelta(days=DAYS + 5)


def test_inbox_items(seeded: sqlite3.Connection) -> None:
    """5 элементов инбокса в статусе new."""
    items = rows(seeded, "SELECT text, status, converted_type, converted_id FROM inbox_items ORDER BY id")
    assert [row[0] for row in items] == list(DEMO_INBOX_TEXTS)
    assert len(items) == 5
    for _, status, converted_type, converted_id in items:
        assert status == "new"
        assert converted_type == ""
        assert converted_id is None


def test_books_shelves(seeded: sqlite3.Connection) -> None:
    """12 книг: 2 читаю, 4 в очереди, 4 прочитано, 2 отложено."""
    shelves = dict(
        rows(seeded, "SELECT shelf, COUNT(*) FROM books GROUP BY shelf ORDER BY shelf")
    )
    assert shelves == {"done": 4, "dropped": 2, "queue": 4, "reading": 2}
    books = rows(
        seeded,
        "SELECT title, author, category, shelf, pages_total, pages_read, rating, why, finished_at "
        "FROM books ORDER BY id",
    )
    assert [row[0] for row in books] == list(DEMO_BOOK_TITLES)
    for title, author, category, shelf, pages_total, pages_read, rating, why, finished_at in books:
        assert author and category in {"pro", "fiction", "nonfiction"} and why
        assert int(pages_total) > 0
        if shelf == "queue":
            assert pages_read == 0
            assert rating is None and finished_at is None
        elif shelf == "reading":
            assert 0 < pages_read < pages_total
            assert rating is None
        elif shelf == "done":
            assert pages_read == pages_total
            assert 4 <= rating <= 5
            assert finished_at and date.fromisoformat(str(finished_at)) < TODAY
        else:  # dropped
            assert 0 < pages_read < pages_total
            assert 2 <= rating <= 3

    queue_priorities = [row[0] for row in rows(
        seeded, "SELECT priority FROM books WHERE shelf = 'queue' ORDER BY priority"
    )]
    assert queue_priorities == [1, 2, 3, 4]


def test_books_insights_quotes_actions(seeded: sqlite3.Connection) -> None:
    """У книг есть главные мысли, цитаты со страницами и действия на внедрение."""
    assert count(seeded, "book_insights") == len(DEMO_BOOKS) * 3
    assert count(seeded, "book_quotes") >= 6
    assert count(seeded, "book_actions") >= 6

    for book_id, _ in rows(seeded, "SELECT id, title FROM books"):
        insights = rows(
            seeded,
            "SELECT text, sort FROM book_insights WHERE book_id = ? ORDER BY sort",
            (book_id,),
        )
        assert len(insights) == 3
        assert [row[1] for row in insights] == [0, 1, 2]
        assert all(row[0] for row in insights)

    for text, page in rows(seeded, "SELECT text, page FROM book_quotes"):
        assert text
        assert page is None or page > 0
    for text, task_id in rows(seeded, "SELECT text, task_id FROM book_actions"):
        assert text
        assert task_id is None  # действие ждёт кнопки «Превратить в задачу»


def test_finance_accounts_categories_budgets(seeded: sqlite3.Connection) -> None:
    """3 счёта, 10 категорий, 3 бюджета на текущий месяц."""
    accounts = rows(seeded, "SELECT name, kind, currency, balance_start_kopecks FROM finance_accounts ORDER BY sort")
    assert [row[0] for row in accounts] == list(DEMO_ACCOUNT_NAMES)
    assert len(accounts) == 3
    assert {row[1] for row in accounts} == {"cash", "card", "savings"}
    assert all(row[2] == "RUB" for row in accounts)
    assert all(int(row[3]) > 0 for row in accounts)

    categories = rows(seeded, "SELECT name, kind, color FROM finance_categories ORDER BY sort")
    assert len(categories) == 10
    assert [row[0] for row in categories] == [name for name, _, _ in DEMO_CATEGORIES]
    assert [(row[0], row[1]) for row in categories] == [
        (name, kind) for name, kind, _ in DEMO_CATEGORIES
    ]
    assert count(seeded, "finance_categories", "kind = 'expense'") == 7
    assert count(seeded, "finance_categories", "kind = 'income'") == 3
    assert all(row[2].startswith("#") for row in categories)

    month = TODAY.strftime("%Y-%m")
    budgets = rows(
        seeded,
        "SELECT c.name, b.month, b.limit_kopecks FROM budgets AS b "
        "JOIN finance_categories AS c ON c.id = b.category_id ORDER BY b.id",
    )
    assert len(budgets) == 3
    assert all(row[1] == month for row in budgets)
    assert [row[0] for row in budgets] == ["Еда и продукты", "Кафе и рестораны", "Транспорт"]
    assert all(int(row[2]) > 0 for row in budgets)


def test_transactions_integer_kopecks(seeded: sqlite3.Connection) -> None:
    """~180 транзакций: целые копейки, kind = kind категории, 3 счёта и 10 категорий."""
    total = count(seeded, "transactions")
    assert 170 <= total <= 190
    assert total == 180
    assert count(seeded, "transactions", "source = ?", (DEMO_SOURCE,)) == total
    assert count(seeded, "transactions", "amount_kopecks <= 0") == 0
    assert count(
        seeded, "transactions", "amount_kopecks <> CAST(amount_kopecks AS INTEGER)"
    ) == 0

    bad_kind = rows(
        seeded,
        "SELECT t.id FROM transactions AS t JOIN finance_categories AS c ON c.id = t.category_id "
        "WHERE t.kind <> c.kind",
    )
    assert bad_kind == []
    assert count(seeded, "transactions", "category_id IS NULL") == 0
    assert count(seeded, "transactions", "account_id IS NULL") == 0
    assert count(seeded, "transactions", "kind = 'income'") >= 5
    assert count(seeded, "transactions", "kind = 'expense'") >= 150
    assert count(seeded, "transactions", "source = 'manual'") == 0
    assert scalar(seeded, "SELECT COUNT(DISTINCT category_id) FROM transactions") == 10
    assert scalar(seeded, "SELECT COUNT(DISTINCT account_id) FROM transactions") == 3

    stamps = [str(row[0]) for row in rows(seeded, "SELECT ts FROM transactions ORDER BY ts")]
    assert stamps == sorted(stamps)
    for stamp in stamps:
        assert len(stamp) >= 19 and stamp.endswith("+03:00")


def test_checkins_90_days(seeded: sqlite3.Connection) -> None:
    """90 чек-инов: по одному на день, энергия 1–5, состояния из §10.4."""
    checkins = rows(seeded, "SELECT date, energy, mood, note, created FROM checkins ORDER BY date")
    assert len(checkins) == 90
    dates = [row[0] for row in checkins]
    assert len(set(dates)) == 90
    assert dates[-1] == TODAY.isoformat()
    assert dates[0] == (TODAY - timedelta(days=89)).isoformat()
    expected = [(TODAY - timedelta(days=offset)).isoformat() for offset in range(89, -1, -1)]
    assert dates == expected
    for checkin_date, energy, mood, _, created in checkins:
        assert 1 <= int(energy) <= 5
        assert mood in DEMO_CHECKIN_MOODS
        assert str(created).startswith(checkin_date)
        assert str(created).endswith("+03:00")
    assert count(seeded, "checkins", "mood = 'в потоке'") > 0
    assert count(seeded, "checkins", "mood = 'перегруз'") > 0


def test_reviews(seeded: sqlite3.Connection) -> None:
    """Два итога: неделя и месяц, с фактами и флагом demo."""
    reviews = rows(
        seeded,
        "SELECT period, start_date, end_date, wins, misses, focus, care, facts_json FROM reviews "
        "ORDER BY id",
    )
    assert [row[0] for row in reviews] == ["week", "month"]
    for period, start_date, end_date, wins, misses, focus, care, facts_json in reviews:
        start = date.fromisoformat(str(start_date))
        end = date.fromisoformat(str(end_date))
        assert start <= TODAY <= end
        assert end > start
        assert wins and misses and focus and care
        facts = json.loads(str(facts_json))
        assert facts["demo"] is True
        assert {"tasksDone", "tasksTotal", "pagesRead", "energyAvg", "habitsPct"} <= set(facts)
        assert facts["tasksTotal"] >= facts["tasksDone"] >= 0
        assert 1 <= facts["energyAvg"] <= 5
        assert 0 <= facts["habitsPct"] <= 100

    week_start = date.fromisoformat(str(reviews[0][1]))
    assert week_start.weekday() == 0
    assert week_start == TODAY - timedelta(days=TODAY.weekday())
    month_start = date.fromisoformat(str(reviews[1][1]))
    assert month_start == TODAY.replace(day=1)
    assert reviews[0][7] and "demo" in str(reviews[0][7])
    # Недельный итог видит свежую книгу и закрытые задачи — экран «Итоги» живой.
    week_facts = json.loads(str(reviews[0][7]))
    assert week_facts["pagesRead"] > 0
    assert week_facts["tasksDone"] >= 1


# ---------------------------------------------------------------------------
# Детерминированность, идемпотентность, статистика
# ---------------------------------------------------------------------------


def test_stats_match_database(seeded: sqlite3.Connection, db: sqlite3.Connection) -> None:
    """SeedStats совпадает с фактическим числом строк всех таблиц v4."""
    stats = seed(db, days=DAYS, today=TODAY)
    assert isinstance(stats, SeedStats)
    expected = {
        "spheres": len(DEMO_SPHERES),
        "goals": len(DEMO_GOALS),
        "anti_goals": len(DEMO_ANTI_GOALS),
        "projects": len(DEMO_PROJECTS),
        "project_notes": len(DEMO_PROJECT_NOTES),
        "tasks": len(DEMO_TASK_POOL) * 4,
        "inbox_items": len(DEMO_INBOX_TEXTS),
        "books": len(DEMO_BOOKS),
        "transactions": 180,
        "accounts": 3,
        "categories": 10,
        "budgets": 3,
        "checkins": 90,
        "reviews": 2,
    }
    table_by_field = {"accounts": "finance_accounts", "categories": "finance_categories"}
    for field, value in expected.items():
        assert getattr(stats, field) == value, field
        assert count(db, table_by_field.get(field, field)) == value, field
    assert stats.goal_steps == count(db, "goal_steps")
    assert stats.project_steps == count(db, "project_steps")
    assert stats.book_insights == count(db, "book_insights")
    assert stats.book_quotes == count(db, "book_quotes")
    assert stats.book_actions == count(db, "book_actions")
    payload = stats.as_dict()
    assert payload["tasks"] == 120 and payload["books"] == 12
    assert "задач" in stats.summary()
    assert "сфер" in stats.summary()


def test_second_run_is_idempotent(seeded: sqlite3.Connection, db: sqlite3.Connection) -> None:
    """Повторный seed: те же счётчики, то же содержимое, никаких дублей."""
    before_stats = seed(db, days=DAYS, today=TODAY)
    before_counts = {table: count(db, table) for table in V4_TABLES}
    before_content = _snapshot(db)

    after_stats = seed(db, days=DAYS, today=TODAY)
    after_counts = {table: count(db, table) for table in V4_TABLES}
    after_content = _snapshot(db)

    assert before_stats.as_dict() == after_stats.as_dict()
    assert before_counts == after_counts
    assert before_content == after_content

    # Уникальные ограничения схемы не позволяют дублей по датам и периодам.
    assert count(db, "checkins") == scalar(db, "SELECT COUNT(DISTINCT date) FROM checkins")
    assert count(db, "budgets") == scalar(
        db, "SELECT COUNT(*) FROM (SELECT DISTINCT category_id, month FROM budgets)"
    )
    assert count(db, "reviews") == scalar(
        db, "SELECT COUNT(*) FROM (SELECT DISTINCT period, start_date FROM reviews)"
    )
    # Задачи повторяются по названиям намеренно (реальные списки так и выглядят),
    # но их количество после второго прогона не выросло.
    assert count(db, "tasks") == len(DEMO_TASK_POOL) * 4


def test_deterministic_across_databases(tmp_path: Path) -> None:
    """Два независимых прогона с одной датой дают одинаковые данные."""
    snapshots = []
    for name in ("a.db", "b.db"):
        connection = connect(tmp_path / name)
        try:
            migrate(connection)
            seed(connection, days=45, today=TODAY)
            snapshots.append(_snapshot(connection))
        finally:
            connection.close()
    assert snapshots[0] == snapshots[1]


def test_reset_keeps_user_rows(db: sqlite3.Connection) -> None:
    """--reset удаляет демо-строки, но не трогает telegram-записи и свои сущности."""
    user_checkin_date = (TODAY - timedelta(days=45)).isoformat()
    with db:
        db.execute(
            "INSERT INTO entries (ts, kind, text, source) VALUES (?, ?, ?, ?)",
            ("2026-11-17T09:00:00+03:00", "journal", "моя запись", "telegram"),
        )
        db.execute(
            "INSERT INTO metrics (date, kind, value, unit, source) VALUES (?, ?, ?, ?, ?)",
            ("2026-11-17", "weight", 80.1, "кг", "telegram"),
        )
        sphere_id = int(
            db.execute("INSERT INTO spheres (name) VALUES ('Моя сфера')").lastrowid or 0
        )
        db.execute("INSERT INTO goals (title, sphere_id) VALUES ('Моя цель', ?)", (sphere_id,))
        db.execute("INSERT INTO tasks (title, sphere_id) VALUES ('Моя задача', ?)", (sphere_id,))
        db.execute("INSERT INTO books (title) VALUES ('Моя книга')")
        account_id = int(
            db.execute(
                "INSERT INTO finance_accounts (name, kind) VALUES ('Мой кошелёк', 'cash')"
            ).lastrowid or 0
        )
        category_id = int(
            db.execute(
                "INSERT INTO finance_categories (name, kind) VALUES ('Моя категория', 'expense')"
            ).lastrowid or 0
        )
        db.execute(
            "INSERT INTO transactions (ts, amount_kopecks, kind, category_id, account_id, note) "
            "VALUES ('2026-11-17T10:00:00+03:00', 12345, 'expense', ?, ?, 'моё')",
            (category_id, account_id),
        )
        db.execute(
            "INSERT INTO checkins (date, energy, mood, note, created) VALUES (?, ?, ?, ?, ?)",
            (user_checkin_date, 5, "ровно", "", "2026-10-03T09:00:00+03:00"),
        )

    seed(db, days=DAYS, today=TODAY)
    assert count(db, "checkins") == 90  # 89 демо + свой чек-ин на занятую дату
    assert scalar(
        db, "SELECT energy FROM checkins WHERE date = ?", (user_checkin_date,)
    ) == 5

    removed = clear_demo(db)
    assert removed.spheres == 5
    assert removed.goals == 9
    assert removed.goal_steps == 30
    assert removed.projects == 6
    assert removed.tasks == 120
    assert removed.books == 12
    assert removed.accounts == 3
    assert removed.categories == 10
    assert removed.transactions == 180
    assert removed.budgets == 3
    assert removed.checkins == 89  # одну дату занял пользователь
    assert removed.reviews == 2

    assert count(db, "entries", "source = 'telegram'") == 1
    assert count(db, "metrics", "source = 'telegram'") == 1
    assert count(db, "spheres") == 1
    assert count(db, "goals") == 1
    assert count(db, "tasks") == 1
    assert count(db, "books") == 1
    assert count(db, "finance_accounts") == 1
    assert count(db, "finance_categories") == 1
    assert count(db, "transactions") == 1
    assert count(db, "checkins") == 1
    assert count(db, "reviews") == 0
    assert count(db, "goal_steps") == 0
    assert count(db, "project_steps") == 0
    assert count(db, "project_notes") == 0
    assert count(db, "book_insights") == 0
    assert count(db, "budgets") == 0
    assert count(db, "inbox_items") == 0


def test_reset_flag_writes_nothing(db: sqlite3.Connection) -> None:
    """seed(..., reset=True) только удаляет: новых строк v4 не появляется."""
    seed(db, days=30, today=TODAY)
    stats = seed(db, days=30, today=TODAY, reset=True)
    assert stats.spheres == 5 and stats.tasks > 0
    for table in V4_TABLES:
        assert count(db, table) == 0, table
    assert count(db, "entries") == 0
    assert count(db, "metrics") == 0
    assert count(db, "habits") == 0
    assert count(db, "goals") == 0


def test_demo_markers(seeded: sqlite3.Connection) -> None:
    """Маркеры демо-строк: source, точная метка чек-инов, флаг в facts_json."""
    assert count(seeded, "entries", "source <> ?", (DEMO_SOURCE,)) == 0
    assert count(seeded, "metrics", "source <> ?", (DEMO_SOURCE,)) == 0
    assert count(seeded, "transactions", "source <> ?", (DEMO_SOURCE,)) == 0
    assert count(seeded, "checkins", "created NOT LIKE '%T21:05:00+03:00'") == 0
    assert count(seeded, "reviews", "facts_json NOT LIKE '%\"demo\": true%'") == 0


# ---------------------------------------------------------------------------
# CLI: приёмка §16.5 — два прогона не плодят дубли
# ---------------------------------------------------------------------------


def test_cli_seed_twice_is_idempotent(tmp_path: Path) -> None:
    """`--db <tmp> --days 120` дважды: exit 0, счётчики и содержимое те же."""
    db_path = tmp_path / "cli-v4.db"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    def run() -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "app.server.seeder",
                "--db",
                str(db_path),
                "--days",
                "120",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

    first = run()
    assert first.returncode == 0, first.stderr
    assert "Засеяно за 120 дней" in first.stdout
    assert "сфер" in first.stdout and "задач" in first.stdout

    connection = connect(db_path)
    try:
        stats_before = {table: count(connection, table) for table in V4_TABLES}
        content_before = _snapshot(connection)
    finally:
        connection.close()

    second = run()
    assert second.returncode == 0, second.stderr

    connection = connect(db_path)
    try:
        stats_after = {table: count(connection, table) for table in V4_TABLES}
        content_after = _snapshot(connection)
        assert scalar(connection, "PRAGMA integrity_check") == "ok"
    finally:
        connection.close()

    assert stats_before == stats_after
    assert content_before == content_after
    assert stats_after["tasks"] == len(DEMO_TASK_POOL) * 4
    assert stats_after["checkins"] == 90
    assert stats_after["transactions"] == 180
