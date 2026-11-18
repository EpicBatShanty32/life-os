"""Тесты миграций № 3–4 — схема v4 (``studio/HANDOFF-OPUS-5.5.md`` §11).

Проверяется контракт схемы:

* свежая база сразу получает ``user_version = 4``, все таблицы/индексы v4
  (включая дневник питания: ``products``, ``food_log``, ``nutrition_targets``);
* записи ``entries.kind = 'food'`` при миграции 4 переезжают в ``food_log``;
* база v2 (``_MIGRATION_1`` + миграция 2, ``user_version = 2``) переезжает на
  v4 без потери данных, старые строки получают дефолты новых колонок;
* ``migrate`` идемпотентна, ``PRAGMA user_version`` — единственный источник
  версии; защиты ``LegacySchemaError``/``SchemaVersionError`` не сломаны;
* FK-политики (CASCADE / SET NULL), CHECK- и UNIQUE-констрейнты новых таблиц;
* дефолты и ISO-метки времени новых строк.

Все базы — во временных каталогах (``tmp_path``); ``data/`` не затрагивается.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Sequence

import pytest

from app.server.db import (
    FTS_TABLES,
    MIGRATIONS,
    SCHEMA_VERSION,
    TABLES,
    LegacySchemaError,
    SchemaVersionError,
    _MIGRATION_1,
    add_entry,
    connect,
    init_db,
    migrate,
    search_entries,
    user_version,
)

#: Таблицы, которых не было в v2 (миграция 3).
NEW_TABLES: tuple[str, ...] = (
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

#: Новые колонки v4 в таблицах, созданных миграциями 1–2.
NEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "goals": ("sphere_id", "horizon", "parent_id", "why", "progress"),
    "courses": ("organizer", "deadline", "notes", "project_id", "goal_id"),
    "habits": ("sphere_id", "color"),
}

#: Индексы, обязательные по §11 handoff.
V3_INDEXES: tuple[str, ...] = (
    "idx_tasks_due_status",
    "idx_tasks_project",
    "idx_tasks_sphere",
    "idx_tasks_list_kind",
    "idx_transactions_ts",
    "idx_transactions_category",
    "idx_goal_steps_goal",
    "idx_project_steps_project",
    "idx_books_shelf",
    "idx_inbox_status",
    "idx_checkins_date",
    "idx_reviews_period_start",
)

#: Таблицы миграции 4 — дневник питания.
NUTRITION_TABLES: tuple[str, ...] = ("products", "food_log", "nutrition_targets")

#: Индексы дневника питания.
NUTRITION_INDEXES: tuple[str, ...] = (
    "idx_products_name",
    "idx_food_log_date",
    "idx_food_log_meal",
)

#: Таблицы схемы v2 — их содержимое обязано пережить миграцию.
V2_TABLES: tuple[str, ...] = (
    "entries",
    "metrics",
    "habits",
    "habit_marks",
    "goals",
    "courses",
    "lessons",
    "lesson_progress",
    "transcripts",
    "segments",
    "notes",
    "jobs",
    "settings_kv",
)

_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture()
def conn(tmp_path: Path):
    connection = init_db(tmp_path / "lifeos.db")
    try:
        yield connection
    finally:
        connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def _index_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    return {str(row[0]) for row in rows}


def _column_info(connection: sqlite3.Connection, table: str) -> dict[str, tuple[int, Any, int]]:
    """``{колонка: (notnull, default, pk)}`` из ``PRAGMA table_info``."""
    return {
        str(info[1]): (int(info[3]), info[4], int(info[5]))
        for info in connection.execute("PRAGMA table_info(%s)" % table).fetchall()
    }


def _counts(connection: sqlite3.Connection, tables: Sequence[str]) -> dict[str, int]:
    return {
        table: int(connection.execute("SELECT count(*) FROM %s" % table).fetchone()[0])
        for table in tables
    }


def _insert(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    return int(connection.execute(sql, params).lastrowid or 0)


def _assert_check_violation(
    connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with connection:
            connection.execute(sql, params)


# --------------------------------------------------------------------------
# Имитация базы v2 и её наполнение
# --------------------------------------------------------------------------


def _apply_v2_schema(connection: sqlite3.Connection) -> None:
    """Приводит БД в состояние v2: миграция 1 + миграция 2, ``user_version = 2``."""
    for statement in _MIGRATION_1:
        connection.execute(statement)
    migration_2 = MIGRATIONS[2]
    assert callable(migration_2)
    migration_2(connection)
    connection.execute("PRAGMA user_version = 2")
    connection.commit()


def _fill_v2(connection: sqlite3.Connection) -> dict[str, int]:
    """Пишет правдоподобные данные v2 и возвращает количество строк по таблицам."""
    with connection:
        connection.executemany(
            "INSERT INTO entries(ts, kind, text, source, meta_json) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "2026-11-01T08:00:00+03:00",
                    "food",
                    "Овсянка на завтрак",
                    "telegram",
                    "{}",
                ),
                (
                    "2026-11-02T20:00:00+03:00",
                    "thought",
                    "Хочу больше спать",
                    "telegram",
                    '{"mood":"спокойно"}',
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO metrics(date, kind, value, unit) VALUES (?, ?, ?, ?)",
            [
                ("2026-11-01", "weight", 79.4, "кг"),
                ("2026-11-02", "sleep", 7.5, "ч"),
            ],
        )
        habit_id = _insert(
            connection,
            "INSERT INTO habits(name, emoji, target_per_week) VALUES ('Вода', '💧', 7)",
        )
        connection.executemany(
            "INSERT INTO habit_marks(habit_id, date) VALUES (?, ?)",
            [(habit_id, "2026-11-01"), (habit_id, "2026-11-02")],
        )
        goal_id = _insert(
            connection,
            "INSERT INTO goals(title, target, current, unit, deadline) "
            "VALUES ('Дойти до 78 кг', 78.0, 79.4, 'кг', '2027-01-01')",
        )
        course_id = _insert(
            connection,
            "INSERT INTO courses(title, category, cover) VALUES ('Здоровье', 'Тело', '#AEDEC2')",
        )
        lesson_ids = [
            _insert(
                connection,
                "INSERT INTO lessons(course_id, idx, title, media_path, duration_s) "
                "VALUES (?, ?, ?, ?, ?)",
                (course_id, number, "Урок %d" % number, "media/%d.mp4" % number, 600.0),
            )
            for number in (1, 2)
        ]
        connection.execute(
            "INSERT INTO lesson_progress(lesson_id, position_s, watched_s) VALUES (?, ?, ?)",
            (lesson_ids[0], 120.0, 90.0),
        )
        connection.execute(
            "INSERT INTO transcripts(lesson_id, lang, text) VALUES (?, 'ru', 'конспект')",
            (lesson_ids[0],),
        )
        connection.executemany(
            "INSERT INTO segments(lesson_id, start_s, end_s, text) VALUES (?, ?, ?, ?)",
            [
                (lesson_ids[0], 0.0, 30.0, "Клетчатка и вода"),
                (lesson_ids[1], 30.0, 60.0, "Сон не меньше семи часов"),
            ],
        )
        connection.execute(
            "INSERT INTO notes(lesson_id, ts, text) VALUES (?, ?, ?)",
            (lesson_ids[0], "2026-11-01T10:00:00+03:00", "вернуться к теме"),
        )
        connection.executemany(
            "INSERT INTO jobs(kind, payload_json, status) VALUES (?, ?, ?)",
            [
                ("transcribe", '{"lesson_id":1}', "done"),
                ("reindex", '{"scope":"entries"}', "queued"),
            ],
        )
        connection.execute(
            "INSERT INTO settings_kv(key, value) VALUES ('theme', 'dark')"
        )
        assert goal_id and habit_id and course_id
    return _counts(connection, V2_TABLES)


def _make_v2_db(path: Path) -> dict[str, int]:
    connection = connect(path)
    try:
        _apply_v2_schema(connection)
        assert user_version(connection) == 2
        return _fill_v2(connection)
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Свежая база: версия, таблицы, колонки, индексы
# --------------------------------------------------------------------------


def test_fresh_database_is_schema_v3(conn: sqlite3.Connection) -> None:
    assert SCHEMA_VERSION == 5
    assert user_version(conn) == 5
    assert callable(MIGRATIONS[3])
    assert callable(MIGRATIONS[4])

    tables = _table_names(conn)
    for table in TABLES:
        assert table in tables, "нет таблицы %s" % table
    for fts_table in FTS_TABLES:
        assert fts_table in tables, "нет FTS-таблицы %s" % fts_table


def test_all_new_tables_exist_and_empty_on_fresh_db(conn: sqlite3.Connection) -> None:
    tables = _table_names(conn)
    missing = [table for table in NEW_TABLES if table not in tables]
    assert missing == []
    assert _counts(conn, NEW_TABLES) == {table: 0 for table in NEW_TABLES}


def test_new_columns_and_defaults(conn: sqlite3.Connection) -> None:
    for table, columns in NEW_COLUMNS.items():
        info = _column_info(conn, table)
        for column in columns:
            assert column in info, "нет %s.%s" % (table, column)

    assert _column_info(conn, "goals")["horizon"][1] == "'year'"
    assert _column_info(conn, "goals")["why"][1] == "''"
    assert _column_info(conn, "goals")["progress"][1] == "0"
    assert _column_info(conn, "courses")["organizer"][1] == "''"
    assert _column_info(conn, "habits")["color"][1] == "'#A18BB7'"


def test_v3_indexes_created(conn: sqlite3.Connection) -> None:
    indexes = _index_names(conn)
    missing = [name for name in V3_INDEXES if name not in indexes]
    assert missing == []


# --------------------------------------------------------------------------
# Миграция базы v2
# --------------------------------------------------------------------------


def test_v2_database_migrates_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "lifeos-v2.db"
    counts_before = _make_v2_db(path)
    assert counts_before["entries"] == 2
    assert counts_before["goals"] == 1
    assert counts_before["courses"] == 1

    connection = init_db(path)
    try:
        assert user_version(connection) == 5
        # Ни одна строка v2 не потеряна: пищевая запись переехала в дневник.
        after = _counts(connection, V2_TABLES)
        assert after["entries"] == counts_before["entries"] - 1
        for table in V2_TABLES:
            if table != "entries":
                assert after[table] == counts_before[table], table
        assert _counts(connection, ("food_log",)) == {"food_log": 1}
        diary = connection.execute(
            "SELECT date, meal, text, kcal, source FROM food_log"
        ).fetchone()
        assert diary["date"] == "2026-11-01"
        assert diary["meal"] == "breakfast"
        assert diary["text"] == "Овсянка на завтрак"
        assert diary["kcal"] == 0.0
        assert diary["source"] == "telegram"

        # Новые таблицы появились и пусты.
        tables = _table_names(connection)
        for table in NEW_TABLES:
            assert table in tables, "нет таблицы %s" % table
        assert _counts(connection, NEW_TABLES) == {table: 0 for table in NEW_TABLES}

        # Журнал v2 читается как раньше; «еда» из него уехала в дневник.
        assert search_entries(connection, "овсянка") == []
        assert search_entries(connection, "спать")[0]["kind"] == "thought"
        goal = connection.execute("SELECT * FROM goals").fetchone()
        assert goal["title"] == "Дойти до 78 кг"
        assert goal["deadline"] == "2027-01-01"
        job = connection.execute("SELECT * FROM jobs ORDER BY id").fetchone()
        assert job["payload_json"] == '{"lesson_id":1}'
        assert job["meta_json"] == "{}"
        lesson = connection.execute("SELECT * FROM lessons ORDER BY id").fetchone()
        assert lesson["title"] == "Урок 1"

        # Старые строки получили дефолты новых колонок.
        assert goal["horizon"] == "year"
        assert goal["why"] == ""
        assert goal["progress"] == 0
        assert goal["sphere_id"] is None
        assert goal["parent_id"] is None
        course = connection.execute("SELECT * FROM courses").fetchone()
        assert course["organizer"] == ""
        assert course["notes"] == ""
        assert course["deadline"] is None
        assert course["project_id"] is None
        assert course["goal_id"] is None
        habit = connection.execute("SELECT * FROM habits").fetchone()
        assert habit["sphere_id"] is None
        assert habit["color"] == "#A18BB7"
    finally:
        connection.close()


def test_repeated_migrate_is_idempotent_on_fresh_db(conn: sqlite3.Connection) -> None:
    with conn:
        sphere_id = _insert(conn, "INSERT INTO spheres(name) VALUES ('Здоровье')")
        task_id = _insert(
            conn,
            "INSERT INTO tasks(title, sphere_id) VALUES ('Позвонить в клинику', ?)",
            (sphere_id,),
        )
    tables_before = _table_names(conn)
    indexes_before = _index_names(conn)
    counts_before = _counts(conn, TABLES)

    assert migrate(conn) == 5
    assert migrate(conn) == 5

    assert user_version(conn) == 5
    assert _table_names(conn) == tables_before
    assert _index_names(conn) == indexes_before
    assert _counts(conn, TABLES) == counts_before
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["title"] == "Позвонить в клинику"
    assert row["status"] == "todo"


def test_repeated_migrate_is_idempotent_after_v2_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "lifeos-v2.db"
    counts_before = _make_v2_db(path)

    connection = init_db(path)
    try:
        assert migrate(connection) == 5
        assert user_version(connection) == 5
        # Пищевая запись v2 уехала в дневник — считаем её отдельно.
        counts_after = _counts(connection, V2_TABLES)
        assert counts_after["entries"] == counts_before["entries"] - 1
        assert _counts(connection, ("food_log",)) == {"food_log": 1}

        with connection:
            add_entry(connection, "thought", "Запись после миграции")
        assert migrate(connection) == 5
        assert user_version(connection) == 5
        assert _counts(connection, V2_TABLES)["entries"] == counts_after["entries"] + 1
        # Повторная миграция не дублирует перенесённые записи дневника.
        assert _counts(connection, ("food_log",)) == {"food_log": 1}
    finally:
        connection.close()


def test_user_version_is_single_source_of_truth(tmp_path: Path) -> None:
    # База без таблиц, но с заявленной версией 5: migrate верит PRAGMA и не
    # переигрывает миграции (источник версии — только user_version).
    connection = connect(tmp_path / "version-only.db")
    try:
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
        assert migrate(connection) == 5
        assert user_version(connection) == 5
        assert _table_names(connection) == set()
    finally:
        connection.close()


def test_legacy_and_future_schema_guards(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(legacy_path))
    raw.executescript(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT);
        CREATE TABLE money (id INTEGER PRIMARY KEY, amount REAL);
        """
    )
    raw.commit()
    raw.close()

    connection = connect(legacy_path)
    try:
        with pytest.raises(LegacySchemaError, match="v0.x"):
            migrate(connection)
        assert user_version(connection) == 0
    finally:
        connection.close()

    future = init_db(tmp_path / "future.db")
    try:
        future.execute("PRAGMA user_version = %d" % (SCHEMA_VERSION + 1))
        with pytest.raises(SchemaVersionError):
            migrate(future)
    finally:
        future.close()


# --------------------------------------------------------------------------
# CHECK-констрейнты: допустимые и недопустимые значения
# --------------------------------------------------------------------------


def test_check_constraints_accept_valid_values(conn: sqlite3.Connection) -> None:
    with conn:
        project_id = _insert(conn, "INSERT INTO projects(name, status) VALUES ('Ремонт', 'paused')")
        valid_rows: tuple[str, ...] = (
            "INSERT INTO tasks(title, resource, status, list_kind) "
            "VALUES ('Важное', 'must', 'done', 'work')",
            "INSERT INTO tasks(title, resource, status, list_kind) "
            "VALUES ('Рутина', 'low', 'cancelled', 'personal')",
            "INSERT INTO books(title, shelf, category) VALUES ('Книга', 'reading', 'pro')",
            "INSERT INTO books(title, shelf, category) VALUES ('Роман', 'done', 'fiction')",
            "INSERT INTO finance_categories(name, kind) VALUES ('Кафе', 'expense')",
            "INSERT INTO finance_categories(name, kind) VALUES ('Зарплата', 'income')",
            "INSERT INTO finance_accounts(name, kind) VALUES ('Наличные', 'cash')",
            "INSERT INTO transactions(ts, amount_kopecks, kind) "
            "VALUES ('2026-11-17T12:00:00Z', 125050, 'expense')",
            "INSERT INTO checkins(date, energy) VALUES ('2026-11-17', 4)",
            "INSERT INTO reviews(period, start_date, end_date) "
            "VALUES ('week', '2026-11-16', '2026-11-22')",
            "INSERT INTO anti_goals(period, text) VALUES ('month', 'Не есть после 21:00')",
            "INSERT INTO inbox_items(text) VALUES ('Купить лампу')",
            "INSERT INTO project_steps(project_id, title) VALUES (%d, 'Смета')" % project_id,
            "INSERT INTO project_notes(project_id, kind, title) "
            "VALUES (%d, 'link', 'Статья')" % project_id,
        )
        for sql in valid_rows:
            conn.execute(sql)

    assert int(conn.execute("SELECT count(*) FROM tasks").fetchone()[0]) == 2
    assert int(conn.execute("SELECT count(*) FROM books").fetchone()[0]) == 2
    assert int(conn.execute("SELECT count(*) FROM project_steps").fetchone()[0]) == 1
    assert int(conn.execute("SELECT count(*) FROM project_notes").fetchone()[0]) == 1


def test_check_constraints_reject_invalid_values(conn: sqlite3.Connection) -> None:
    with conn:
        project_id = _insert(conn, "INSERT INTO projects(name) VALUES ('Ремонт')")
    _assert_check_violation(
        conn, "INSERT INTO project_notes(project_id, kind) VALUES (?, 'file')", (project_id,)
    )
    assert int(conn.execute("SELECT count(*) FROM project_notes").fetchone()[0]) == 0

    invalid_rows: tuple[tuple[str, Sequence[Any]], ...] = (
        ("INSERT INTO tasks(title, resource) VALUES ('x', 'urgent')", ()),
        ("INSERT INTO tasks(title, resource) VALUES ('x', '')", ()),
        ("INSERT INTO tasks(title, status) VALUES ('x', 'doing')", ()),
        ("INSERT INTO tasks(title, list_kind) VALUES ('x', 'office')", ()),
        ("INSERT INTO books(title, shelf) VALUES ('x', 'someday')", ()),
        ("INSERT INTO books(title, category) VALUES ('x', 'art')", ()),
        ("INSERT INTO finance_categories(name, kind) VALUES ('x', 'transfer')", ()),
        (
            "INSERT INTO transactions(ts, amount_kopecks, kind) "
            "VALUES ('2026-11-17T12:00:00Z', 0, 'expense')",
            (),
        ),
        (
            "INSERT INTO transactions(ts, amount_kopecks, kind) "
            "VALUES ('2026-11-17T12:00:00Z', -100, 'expense')",
            (),
        ),
        (
            "INSERT INTO transactions(ts, amount_kopecks, kind) "
            "VALUES ('2026-11-17T12:00:00Z', 100, 'transfer')",
            (),
        ),
        ("INSERT INTO checkins(date, energy) VALUES ('2026-11-17', 0)", ()),
        ("INSERT INTO checkins(date, energy) VALUES ('2026-11-17', 6)", ()),
        ("INSERT INTO anti_goals(period, text) VALUES ('year', 'x')", ()),
        ("INSERT INTO reviews(period, start_date, end_date) VALUES ('day', 'a', 'b')", ()),
        ("INSERT INTO inbox_items(text, status) VALUES ('x', 'later')", ()),
        ("INSERT INTO projects(name, status) VALUES ('x', 'planning')", ()),
        ("INSERT INTO finance_accounts(name, kind) VALUES ('x', 'crypto')", ()),
    )
    for sql, params in invalid_rows:
        _assert_check_violation(conn, sql, params)

    # Ни одна невалидная строка не должна остаться в базе.
    assert int(conn.execute("SELECT count(*) FROM tasks").fetchone()[0]) == 0
    assert int(conn.execute("SELECT count(*) FROM checkins").fetchone()[0]) == 0


def test_unique_constraints(conn: sqlite3.Connection) -> None:
    with conn:
        _insert(conn, "INSERT INTO spheres(name) VALUES ('Здоровье')")
        _insert(conn, "INSERT INTO finance_categories(name, kind) VALUES ('Кафе', 'expense')")
        _insert(
            conn,
            "INSERT INTO budgets(category_id, month, limit_kopecks) VALUES (1, '2026-11', 3000000)",
        )
        _insert(conn, "INSERT INTO checkins(date, energy) VALUES ('2026-11-17', 3)")
        _insert(
            conn,
            "INSERT INTO reviews(period, start_date, end_date) VALUES ('week', '2026-11-16', '2026-11-22')",
        )

    duplicate_rows: tuple[str, ...] = (
        "INSERT INTO spheres(name) VALUES ('Здоровье')",
        "INSERT INTO finance_categories(name, kind) VALUES ('Кафе', 'expense')",
        "INSERT INTO budgets(category_id, month, limit_kopecks) VALUES (1, '2026-11', 100)",
        "INSERT INTO checkins(date, energy) VALUES ('2026-11-17', 5)",
        "INSERT INTO reviews(period, start_date, end_date) VALUES ('week', '2026-11-16', '2026-11-23')",
    )
    for sql in duplicate_rows:
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(sql)

    # Имя категории уникально только в паре с видом операции.
    with conn:
        _insert(conn, "INSERT INTO finance_categories(name, kind) VALUES ('Кафе', 'income')")
    assert int(conn.execute("SELECT count(*) FROM finance_categories").fetchone()[0]) == 2


# --------------------------------------------------------------------------
# FK: CASCADE и SET NULL
# --------------------------------------------------------------------------


def test_goal_cascade_and_references_set_null(conn: sqlite3.Connection) -> None:
    with conn:
        parent_id = _insert(conn, "INSERT INTO goals(title) VALUES ('Год: здоровье')")
        child_id = _insert(
            conn,
            "INSERT INTO goals(title, parent_id) VALUES ('Квартал: спорт', ?)",
            (parent_id,),
        )
        step_id = _insert(
            conn,
            "INSERT INTO goal_steps(goal_id, title) VALUES (?, 'Выбрать зал')",
            (parent_id,),
        )
        task_id = _insert(
            conn, "INSERT INTO tasks(title, goal_id) VALUES ('Купить абонемент', ?)", (parent_id,)
        )
        project_id = _insert(
            conn, "INSERT INTO projects(name, goal_id) VALUES ('Форма к лету', ?)", (parent_id,)
        )
        course_id = _insert(
            conn, "INSERT INTO courses(title, goal_id) VALUES ('Здоровье', ?)", (parent_id,)
        )

    with conn:
        conn.execute("DELETE FROM goals WHERE id = ?", (parent_id,))

    # Вехи цели удалены каскадом, ссылки остальных таблиц обнулены.
    assert int(conn.execute("SELECT count(*) FROM goal_steps WHERE id = ?", (step_id,)).fetchone()[0]) == 0
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert task["goal_id"] is None
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    assert project["goal_id"] is None
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    assert course["goal_id"] is None
    child = conn.execute("SELECT * FROM goals WHERE id = ?", (child_id,)).fetchone()
    assert child["parent_id"] is None


def test_project_cascade_and_task_reference_set_null(conn: sqlite3.Connection) -> None:
    with conn:
        project_id = _insert(conn, "INSERT INTO projects(name) VALUES ('Ремонт')")
        _insert(
            conn,
            "INSERT INTO project_steps(project_id, title) VALUES (?, 'Смета')",
            (project_id,),
        )
        _insert(
            conn,
            "INSERT INTO project_steps(project_id, title) VALUES (?, 'Бригада')",
            (project_id,),
        )
        _insert(
            conn,
            "INSERT INTO project_notes(project_id, kind, title, body) "
            "VALUES (?, 'note', 'Заметка', 'текст')",
            (project_id,),
        )
        _insert(
            conn,
            "INSERT INTO project_notes(project_id, kind, title, url) "
            "VALUES (?, 'link', 'Статья', 'https://example.com')",
            (project_id,),
        )
        task_id = _insert(
            conn, "INSERT INTO tasks(title, project_id) VALUES ('Позвонить', ?)", (project_id,)
        )

    with conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    assert int(conn.execute("SELECT count(*) FROM project_steps").fetchone()[0]) == 0
    assert int(conn.execute("SELECT count(*) FROM project_notes").fetchone()[0]) == 0
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert task["project_id"] is None
    assert task["title"] == "Позвонить"


def test_book_cascade_and_action_task_reference_set_null(conn: sqlite3.Connection) -> None:
    with conn:
        book_id = _insert(conn, "INSERT INTO books(title) VALUES ('Атомные привычки')")
        _insert(conn, "INSERT INTO book_insights(book_id, text) VALUES (?, 'Начать с малого')", (book_id,))
        _insert(
            conn,
            "INSERT INTO book_quotes(book_id, text, page) VALUES (?, '1% лучше каждый день', 12)",
            (book_id,),
        )
        task_id = _insert(conn, "INSERT INTO tasks(title) VALUES ('Купить трекер')")
        action_id = _insert(
            conn,
            "INSERT INTO book_actions(book_id, text, task_id) VALUES (?, 'Завести трекер', ?)",
            (book_id, task_id),
        )

    with conn:
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))

    assert int(conn.execute("SELECT count(*) FROM book_insights").fetchone()[0]) == 0
    assert int(conn.execute("SELECT count(*) FROM book_quotes").fetchone()[0]) == 0
    assert int(conn.execute("SELECT count(*) FROM book_actions").fetchone()[0]) == 0
    # Книга удалена — действие исчезло, а задача осталась.
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert task["title"] == "Купить трекер"

    with conn:
        second_book_id = _insert(conn, "INSERT INTO books(title) VALUES ('Вторая книга')")
        conn.execute(
            "INSERT INTO book_actions(book_id, text, task_id) VALUES (?, 'Перечитать', ?)",
            (second_book_id, task_id),
        )
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    # Обратная сторона: удаление задачи обнуляет book_actions.task_id.
    actions = conn.execute("SELECT * FROM book_actions").fetchall()
    assert len(actions) == 1
    assert actions[0]["task_id"] is None
    assert action_id  # строка существовала до удаления книги


def test_sphere_references_are_set_null(conn: sqlite3.Connection) -> None:
    with conn:
        sphere_id = _insert(conn, "INSERT INTO spheres(name) VALUES ('Работа')")
        task_id = _insert(
            conn, "INSERT INTO tasks(title, sphere_id) VALUES ('Отчёт', ?)", (sphere_id,)
        )
        project_id = _insert(
            conn, "INSERT INTO projects(name, sphere_id) VALUES ('Найм', ?)", (sphere_id,)
        )
        habit_id = _insert(
            conn, "INSERT INTO habits(name, sphere_id) VALUES ('Планирование', ?)", (sphere_id,)
        )

    with conn:
        conn.execute("DELETE FROM spheres WHERE id = ?", (sphere_id,))

    assert conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()["sphere_id"] is None
    assert (
        conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()["sphere_id"]
        is None
    )
    assert (
        conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()["sphere_id"]
        is None
    )


def test_sphere_referenced_by_goal_cannot_be_deleted(conn: sqlite3.Connection) -> None:
    # §11: goals.sphere_id — REFERENCES spheres(id) без ON DELETE, поэтому
    # сферу, на которую ссылается цель, сначала нужно освободить (API архивирует).
    with conn:
        sphere_id = _insert(conn, "INSERT INTO spheres(name) VALUES ('Здоровье')")
        _insert(conn, "INSERT INTO goals(title, sphere_id) VALUES ('Дойти до 78 кг', ?)", (sphere_id,))

    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("DELETE FROM spheres WHERE id = ?", (sphere_id,))
    assert int(conn.execute("SELECT count(*) FROM spheres").fetchone()[0]) == 1


def test_finance_cascade_and_set_null(conn: sqlite3.Connection) -> None:
    with conn:
        account_id = _insert(
            conn, "INSERT INTO finance_accounts(name, kind) VALUES ('Карта', 'card')"
        )
        category_id = _insert(
            conn, "INSERT INTO finance_categories(name, kind) VALUES ('Кафе', 'expense')"
        )
        transaction_id = _insert(
            conn,
            "INSERT INTO transactions(ts, amount_kopecks, kind, category_id, account_id) "
            "VALUES ('2026-11-17T12:00:00Z', 45000, 'expense', ?, ?)",
            (category_id, account_id),
        )
        _insert(
            conn,
            "INSERT INTO budgets(category_id, month, limit_kopecks) VALUES (?, '2026-11', 3000000)",
            (category_id,),
        )

    with conn:
        conn.execute("DELETE FROM finance_categories WHERE id = ?", (category_id,))
    assert int(conn.execute("SELECT count(*) FROM budgets").fetchone()[0]) == 0
    transaction = conn.execute(
        "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
    ).fetchone()
    assert transaction["category_id"] is None
    assert transaction["account_id"] == account_id

    with conn:
        conn.execute("DELETE FROM finance_accounts WHERE id = ?", (account_id,))
    transaction = conn.execute(
        "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
    ).fetchone()
    assert transaction["account_id"] is None
    assert transaction["amount_kopecks"] == 45000


def test_habit_cascade_still_works(conn: sqlite3.Connection) -> None:
    with conn:
        habit_id = _insert(conn, "INSERT INTO habits(name) VALUES ('Вода')")
        _insert(conn, "INSERT INTO habit_marks(habit_id, date) VALUES (?, '2026-11-17')", (habit_id,))
    with conn:
        conn.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
    assert int(conn.execute("SELECT count(*) FROM habit_marks").fetchone()[0]) == 0


# --------------------------------------------------------------------------
# Дефолты и метки времени новых таблиц
# --------------------------------------------------------------------------


def test_new_table_defaults(conn: sqlite3.Connection) -> None:
    with conn:
        sphere_id = _insert(conn, "INSERT INTO spheres(name) VALUES ('Здоровье')")
        task_id = _insert(conn, "INSERT INTO tasks(title) VALUES ('Задача')")
        book_id = _insert(conn, "INSERT INTO books(title) VALUES ('Книга')")
        inbox_id = _insert(conn, "INSERT INTO inbox_items(text) VALUES ('Идея')")
        project_id = _insert(conn, "INSERT INTO projects(name) VALUES ('Проект')")
        account_id = _insert(conn, "INSERT INTO finance_accounts(name) VALUES ('Карта')")
        transaction_id = _insert(
            conn,
            "INSERT INTO transactions(ts, amount_kopecks, kind) "
            "VALUES ('2026-11-17T12:00:00Z', 100, 'expense')",
        )
        checkin_id = _insert(conn, "INSERT INTO checkins(date, energy) VALUES ('2026-11-17', 3)")
        review_id = _insert(
            conn,
            "INSERT INTO reviews(period, start_date, end_date) "
            "VALUES ('month', '2026-11-01', '2026-11-30')",
        )
        anti_goal_id = _insert(
            conn, "INSERT INTO anti_goals(period, text) VALUES ('quarter', 'Не залипать в ленте')"
        )

    sphere = conn.execute("SELECT * FROM spheres WHERE id = ?", (sphere_id,)).fetchone()
    assert sphere["color"] == "#A18BB7"
    assert sphere["icon"] == ""
    assert sphere["sort"] == 0
    assert sphere["archived"] == 0
    assert _ISO_TS_RE.match(sphere["created"])

    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert task["resource"] == "important"
    assert task["status"] == "todo"
    assert task["list_kind"] == "work"
    assert task["recurrence"] == ""
    assert task["notes"] == ""
    assert task["tags"] == ""
    assert task["due_date"] is None
    assert task["start_at"] is None
    assert task["end_at"] is None
    assert task["done_at"] is None
    assert _ISO_TS_RE.match(task["created"])

    book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    assert book["shelf"] == "queue"
    assert book["category"] == "nonfiction"
    assert book["author"] == ""
    assert book["pages_total"] == 0
    assert book["pages_read"] == 0
    assert book["priority"] == 0
    assert book["rating"] is None
    assert book["finished_at"] is None

    inbox_item = conn.execute("SELECT * FROM inbox_items WHERE id = ?", (inbox_id,)).fetchone()
    assert inbox_item["status"] == "new"
    assert inbox_item["converted_type"] == ""
    assert inbox_item["converted_id"] is None

    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    assert project["status"] == "idea"
    assert project["outcome"] == ""
    assert project["description"] == ""
    assert project["updated"] is None

    account = conn.execute("SELECT * FROM finance_accounts WHERE id = ?", (account_id,)).fetchone()
    assert account["kind"] == "card"
    assert account["currency"] == "RUB"
    assert account["balance_start_kopecks"] == 0
    assert account["archived"] == 0

    transaction = conn.execute(
        "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
    ).fetchone()
    assert transaction["source"] == "manual"
    assert transaction["note"] == ""
    assert transaction["category_id"] is None
    assert _ISO_TS_RE.match(transaction["created"])

    checkin = conn.execute("SELECT * FROM checkins WHERE id = ?", (checkin_id,)).fetchone()
    assert checkin["mood"] == ""
    assert checkin["note"] == ""
    assert _ISO_TS_RE.match(checkin["created"])

    review = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    assert review["wins"] == ""
    assert review["misses"] == ""
    assert review["focus"] == ""
    assert review["care"] == ""
    assert review["facts_json"] == "{}"

    anti_goal = conn.execute("SELECT * FROM anti_goals WHERE id = ?", (anti_goal_id,)).fetchone()
    assert anti_goal["active"] == 1
    assert _ISO_TS_RE.match(anti_goal["created"])


def test_active_tasks_and_books_reports(conn: sqlite3.Connection) -> None:
    """Новые таблицы участвуют в запросах с индексами (дымовая проверка §11)."""
    with conn:
        sphere_id = _insert(conn, "INSERT INTO spheres(name, sort) VALUES ('Работа', 1)")
        for title, resource, status, due in (
            ("Отчёт", "must", "todo", "2026-11-17"),
            ("Созвон", "important", "done", "2026-11-17"),
            ("Разобрать почту", "low", "todo", "2026-11-18"),
        ):
            _insert(
                conn,
                "INSERT INTO tasks(title, sphere_id, resource, status, due_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (title, sphere_id, resource, status, due),
            )
        _insert(conn, "INSERT INTO books(title, shelf) VALUES ('Читаю', 'reading')")
        _insert(conn, "INSERT INTO books(title, shelf) VALUES ('В очереди', 'queue')")

    counts = dict(
        conn.execute(
            "SELECT resource, count(*) FROM tasks WHERE status = 'todo' GROUP BY resource"
        ).fetchall()
    )
    assert counts["must"] == 1
    assert counts["low"] == 1
    reading = conn.execute("SELECT count(*) FROM books WHERE shelf = 'reading'").fetchone()[0]
    assert reading == 1
    due = conn.execute(
        "SELECT title FROM tasks WHERE due_date = '2026-11-17' AND status = 'todo' ORDER BY title"
    ).fetchall()
    assert [row["title"] for row in due] == ["Отчёт"]


# --------------------------------------------------------------------------
# Миграция 4: дневник питания
# --------------------------------------------------------------------------


def test_fresh_database_has_nutrition_schema(conn: sqlite3.Connection) -> None:
    """Свежая база: таблицы и индексы дневника, нормы по умолчанию."""
    tables = _table_names(conn)
    for table in NUTRITION_TABLES:
        assert table in tables, "нет таблицы %s" % table

    indexes = _index_names(conn)
    missing = [name for name in NUTRITION_INDEXES if name not in indexes]
    assert missing == []

    assert _counts(conn, ("products", "food_log")) == {"products": 0, "food_log": 0}
    targets = conn.execute("SELECT * FROM nutrition_targets").fetchone()
    assert targets["id"] == 1
    assert targets["kcal"] == 2300
    assert targets["protein"] == 150
    assert targets["fat"] == 75
    assert targets["carbs"] == 260
    assert targets["fiber"] == 30
    assert targets["sugar"] == 50
    assert targets["water_ml"] == 2200
    assert targets["updated"] is not None


def test_food_log_check_and_product_reference(conn: sqlite3.Connection) -> None:
    """``meal`` ограничен CHECK, удаление продукта обнуляет ссылку (SET NULL)."""
    _assert_check_violation(
        conn,
        "INSERT INTO food_log(date, ts, meal, grams) VALUES ('2026-11-17', "
        "'2026-11-17T08:00:00+03:00', 'brunch', 100)",
    )
    product_id = _insert(
        conn, "INSERT INTO products(name, kcal) VALUES ('Гречка', 343)"
    )
    with conn:
        entry_id = _insert(
            conn,
            "INSERT INTO food_log(date, ts, meal, product_id, grams, kcal) "
            "VALUES ('2026-11-17', '2026-11-17T08:00:00+03:00', 'breakfast', ?, 100, 343)",
            (product_id,),
        )
    assert (
        conn.execute("SELECT product_id FROM food_log WHERE id = ?", (entry_id,)).fetchone()[0]
        == product_id
    )

    with conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    row = conn.execute("SELECT product_id, kcal FROM food_log WHERE id = ?", (entry_id,)).fetchone()
    assert row["product_id"] is None
    assert row["kcal"] == 343


def test_migration_4_moves_food_entries_with_macros(tmp_path: Path) -> None:
    """``entries.kind='food'`` уезжает в ``food_log`` с макросами из meta_json."""
    path = tmp_path / "v2-food.db"
    connection = connect(path)
    try:
        _apply_v2_schema(connection)
        with connection:
            connection.executemany(
                "INSERT INTO entries(ts, kind, text, source, meta_json) VALUES (?,?,?,?,?)",
                [
                    (
                        "2026-11-01T08:15:00+03:00",
                        "food",
                        "Завтрак: Овсянка",
                        "demo",
                        '{"meal":"Завтрак","kcal":320,"protein_g":12,"fat_g":6,"carbs_g":52}',
                    ),
                    (
                        "2026-11-01T20:45:00+03:00",
                        "food",
                        "Ужин: Творог",
                        "demo",
                        '{"kcal":180,"protein_g":20}',
                    ),
                    (
                        "2026-11-01T21:00:00+03:00",
                        "thought",
                        "Мысль дня",
                        "telegram",
                        "{}",
                    ),
                ],
            )
    finally:
        connection.close()

    conn = init_db(path)
    try:
        assert user_version(conn) == 5
        diary = conn.execute(
            "SELECT date, ts, meal, text, grams, kcal, protein, fat, source "
            "FROM food_log ORDER BY id"
        ).fetchall()
        assert len(diary) == 2
        assert diary[0]["date"] == "2026-11-01"
        assert diary[0]["meal"] == "breakfast"
        assert diary[0]["kcal"] == 320
        assert diary[0]["protein"] == 12
        assert diary[0]["fat"] == 6
        assert diary[0]["grams"] == 250
        assert diary[0]["source"] == "demo"
        # Без meta.meal приём пищи определяется по часу записи.
        assert diary[1]["meal"] == "dinner"
        assert diary[1]["kcal"] == 180
        assert diary[1]["protein"] == 20

        # В журнале остались только непищевые записи.
        kinds = [row["kind"] for row in conn.execute("SELECT kind FROM entries")]
        assert kinds == ["thought"]
        # Повторная миграция ничего не дублирует.
        assert migrate(conn) == 5
        assert conn.execute("SELECT count(*) FROM food_log").fetchone()[0] == 2
    finally:
        conn.close()
