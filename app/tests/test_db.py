"""Тесты SQLite-слоя Life OS v3 (app/server/db.py) и моделей API.

Проверяется: создание схемы, идемпотентность миграций, WAL/прагмы, FTS5 по
журналу и субтитрам, каскадное удаление курса, индексы, защита от базы v0.x,
а также сборка pydantic-моделей из строк ``sqlite3.Row``.

Все базы — во временных каталогах, рабочая ``data/lifeos.db`` не затрагивается.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.server.db import (
    BUSY_TIMEOUT_MS,
    FTS_TABLES,
    SCHEMA_VERSION,
    TABLES,
    LegacySchemaError,
    SchemaVersionError,
    add_entry,
    add_metric,
    connect,
    ensure_fts,
    fts5_available,
    fts_query,
    get_setting,
    init_db,
    is_legacy_schema,
    migrate,
    search_entries,
    search_segments,
    set_setting,
    user_version,
)
from app.server.models import (
    AgendaItem,
    Course,
    CourseDetail,
    Entry,
    Goal,
    Habit,
    JobStatus,
    Lesson,
    LessonDetail,
    Metric,
    Note,
    Overview,
    OverviewKpis,
    Progress,
    Segment,
    SyncStatus,
    Transcript,
)


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


def _create_course(connection: sqlite3.Connection, title: str = "Здоровье", lessons: int = 2):
    with connection:
        cursor = connection.execute(
            "INSERT INTO courses(title, category, cover) VALUES (?, ?, ?)",
            (title, "Тело", "#AEDEC2"),
        )
        course_id = int(cursor.lastrowid or 0)
        lesson_ids: list[int] = []
        for number in range(1, lessons + 1):
            cursor = connection.execute(
                "INSERT INTO lessons(course_id, idx, title, media_path, duration_s) "
                "VALUES (?, ?, ?, ?, ?)",
                (course_id, number, "Урок %d" % number, "media/%d.mp4" % number, 600.0),
            )
            lesson_ids.append(int(cursor.lastrowid or 0))
    return course_id, lesson_ids


# --------------------------------------------------------------------------
# Схема, миграции, подключение
# --------------------------------------------------------------------------


def test_schema_version_and_tables(conn: sqlite3.Connection) -> None:
    assert SCHEMA_VERSION == 5
    assert user_version(conn) == SCHEMA_VERSION

    tables = _table_names(conn)
    for table in TABLES:
        assert table in tables, "нет таблицы %s" % table
    for fts_table in FTS_TABLES:
        assert fts_table in tables, "нет FTS-таблицы %s" % fts_table


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "lifeos.db"
    connection = init_db(path)
    with connection:
        entry_id = add_entry(connection, "food", "Овсянка на завтрак")
    tables_before = _table_names(connection)

    assert migrate(connection) == SCHEMA_VERSION
    assert migrate(connection) == SCHEMA_VERSION
    assert user_version(connection) == SCHEMA_VERSION
    assert _table_names(connection) == tables_before
    assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 1
    connection.close()

    # Повторное открытие базы: версия та же, данные и поиск на месте.
    reopened = init_db(path)
    try:
        assert user_version(reopened) == SCHEMA_VERSION
        found = search_entries(reopened, "овсянка")
        assert [row["id"] for row in found] == [entry_id]
        assert reopened.execute("SELECT count(*) FROM entries_fts").fetchone()[0] == 1
    finally:
        reopened.close()


def test_wal_and_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.row_factory is sqlite3.Row
    assert fts5_available(conn) is True


def test_init_db_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "lifeos.db"
    connection = init_db(path)
    try:
        assert path.is_file()
        assert user_version(connection) == SCHEMA_VERSION
    finally:
        connection.close()


def test_in_memory_database_supported() -> None:
    connection = connect(":memory:")
    try:
        assert migrate(connection) == SCHEMA_VERSION
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "memory"
        with connection:
            add_entry(connection, "thought", "Проверка памяти")
        assert len(search_entries(connection, "памя")) == 1
    finally:
        connection.close()


def test_legacy_v0_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id INTEGER, text TEXT);
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, ts TEXT, slot TEXT, category TEXT, title TEXT, text TEXT
        );
        """
    )
    raw.commit()
    raw.close()

    connection = connect(path)
    try:
        assert is_legacy_schema(connection) is True
        with pytest.raises(LegacySchemaError, match="v0.x"):
            migrate(connection)
    finally:
        connection.close()


def test_future_schema_is_rejected(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA user_version = %d" % (SCHEMA_VERSION + 10))
    with pytest.raises(SchemaVersionError):
        migrate(conn)


def test_indexes_created(conn: sqlite3.Connection) -> None:
    expected = {
        "idx_entries_date",
        "idx_entries_kind_date",
        "idx_entries_ts",
        "idx_entries_source",
        "idx_metrics_kind_date",
        "idx_metrics_date",
        "idx_habit_marks_date",
        "idx_goals_status",
        "idx_lessons_course",
        "idx_segments_lesson",
        "idx_notes_lesson",
        "idx_jobs_status",
    }
    assert expected.issubset(_index_names(conn))


def test_generated_date_column(conn: sqlite3.Connection) -> None:
    with conn:
        entry_id = add_entry(
            conn,
            "food",
            "Съел овсянку с ягодами",
            ts="2026-11-17T08:30:00+03:00",
            meta={"mood": "хорошо"},
        )

    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    assert row["date"] == "2026-11-17"
    assert row["ts"] == "2026-11-17T08:30:00+03:00"
    assert row["source"] == "telegram"
    assert row["meta_json"] == '{"mood":"хорошо"}'

    # date — генерируемая колонка: попытка записи должна падать.
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "INSERT INTO entries(ts, kind, text, date) VALUES ('2026-11-18T00:00:00+03:00', 'x', 'y', '2026-11-18')"
        )


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO lessons(course_id, idx, title) VALUES (999, 1, 'Сирота')"
            )


def test_course_cascade_delete(conn: sqlite3.Connection) -> None:
    course_id, lesson_ids = _create_course(conn, lessons=2)
    first_lesson, second_lesson = lesson_ids

    with conn:
        conn.execute(
            "INSERT INTO lesson_progress(lesson_id, position_s, watched_s) VALUES (?, ?, ?)",
            (first_lesson, 120.0, 90.0),
        )
        conn.execute(
            "INSERT INTO transcripts(lesson_id, lang, text) VALUES (?, 'ru', 'конспект')",
            (first_lesson,),
        )
        conn.execute(
            "INSERT INTO segments(lesson_id, start_s, end_s, text) VALUES (?, ?, ?, ?)",
            (first_lesson, 0.0, 30.0, "Клетчатка и вода помогают пищеварению"),
        )
        conn.execute(
            "INSERT INTO segments(lesson_id, start_s, end_s, text) VALUES (?, ?, ?, ?)",
            (second_lesson, 31.0, 60.0, "Сон не меньше семи часов"),
        )
        conn.execute(
            "INSERT INTO notes(lesson_id, ts, text) VALUES (?, ?, ?)",
            (first_lesson, "2026-11-17T10:00:00+03:00", "вернуться к теме"),
        )

    assert len(search_segments(conn, "клетчатка")) == 1

    with conn:
        conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))

    for table in ("courses", "lessons", "lesson_progress", "transcripts", "segments", "notes"):
        count = conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
        assert count == 0, "в %s остались строки" % table
    assert search_segments(conn, "клетчатка") == []
    assert search_segments(conn, "сон") == []


# --------------------------------------------------------------------------
# FTS5-поиск
# --------------------------------------------------------------------------


def test_entries_fts_insert_update_delete(conn: sqlite3.Connection) -> None:
    with conn:
        oats = add_entry(conn, "food", "Овсянка и вода на завтрак", ts="2026-11-17T08:00:00+03:00")
        squats = add_entry(conn, "training", "Приседания 5 на 5", ts="2026-11-17T19:00:00+03:00")
        sleep = add_entry(conn, "thought", "Хочу больше спать", ts="2026-11-17T22:00:00+03:00")

    assert [row["id"] for row in search_entries(conn, "овсянка")] == [oats]
    assert [row["id"] for row in search_entries(conn, "овсян")] == [oats]  # префикс
    assert [row["id"] for row in search_entries(conn, "приседания")] == [squats]
    assert [row["id"] for row in search_entries(conn, "спать")] == [sleep]
    assert search_entries(conn, "борщ") == []
    assert conn.execute("SELECT count(*) FROM entries_fts").fetchone()[0] == 3

    with conn:
        conn.execute("UPDATE entries SET text = ? WHERE id = ?", ("Овсянка с ягодами", oats))

    assert search_entries(conn, "вода") == []
    assert [row["id"] for row in search_entries(conn, "ягодами")] == [oats]
    assert conn.execute("SELECT count(*) FROM entries_fts").fetchone()[0] == 3

    with conn:
        conn.execute("DELETE FROM entries WHERE id = ?", (oats,))

    assert search_entries(conn, "овсянка") == []
    assert conn.execute("SELECT count(*) FROM entries_fts").fetchone()[0] == 2


def test_entries_fts_filters_and_limits(conn: sqlite3.Connection) -> None:
    with conn:
        for index in range(3):
            add_entry(conn, "water" if index < 2 else "food", "Выпил воды 200 мл")
        add_entry(conn, "food", "Овсянка на завтрак")

    assert len(search_entries(conn, "вод")) == 3
    assert len(search_entries(conn, "вод", limit=2)) == 2
    assert len(search_entries(conn, "вод", limit=0)) == 1  # минимум 1
    assert len(search_entries(conn, "вод", limit=1000)) == 3  # максимум 200, но строк 3
    assert len(search_entries(conn, "вод", kind="water")) == 2
    assert search_entries(conn, "вод", kind="training") == []
    assert search_entries(conn, "   ") == []


def test_fts_query_escapes_operators(conn: sqlite3.Connection) -> None:
    assert fts_query("вес OR NEAR -вода") == '"вес"* "вода"*'
    assert fts_query("") == ""
    assert fts_query("!!! -- ()") == ""

    with conn:
        add_entry(conn, "thought", "Вес стоит на месте, зато сон хороший")

    # Сырые операторы FTS5 не должны ломать запрос и не трактуются как синтаксис.
    assert len(search_entries(conn, "вес OR сон")) == 1
    assert len(search_entries(conn, "вес сон")) == 1
    assert search_entries(conn, "OR NEAR - + ( )") == []
    assert search_entries(conn, '"вес"') != []


def test_segments_fts_and_lesson_filter(conn: sqlite3.Connection) -> None:
    _, (first_lesson, second_lesson) = _create_course(conn, lessons=2)

    with conn:
        conn.execute(
            "INSERT INTO segments(lesson_id, start_s, end_s, text) VALUES (?, ?, ?, ?)",
            (first_lesson, 0.0, 30.0, "Клетчатка и вода важны для пищеварения"),
        )
        conn.execute(
            "INSERT INTO segments(lesson_id, start_s, end_s, text) VALUES (?, ?, ?, ?)",
            (first_lesson, 30.0, 60.0, "Считаем калории без фанатизма"),
        )
        conn.execute(
            "INSERT INTO segments(lesson_id, start_s, end_s, text) VALUES (?, ?, ?, ?)",
            (second_lesson, 0.0, 20.0, "Вода перед тренировкой"),
        )

    found = search_segments(conn, "вода")
    assert len(found) == 2
    assert {row["lesson_id"] for row in found} == {first_lesson, second_lesson}
    assert all(row["lesson_title"] for row in found)
    assert len(search_segments(conn, "вода", lesson_id=first_lesson)) == 1
    assert search_segments(conn, "вода", lesson_id=99999) == []
    assert [row["start_s"] for row in search_segments(conn, "калории")] == [30.0]


def test_ensure_fts_repairs_index(conn: sqlite3.Connection) -> None:
    with conn:
        add_entry(conn, "food", "Овсянка на завтрак")
        conn.execute("INSERT INTO entries_fts(entries_fts) VALUES ('delete-all')")

    assert search_entries(conn, "овсянка") == []
    ensure_fts(conn)
    assert len(search_entries(conn, "овсянка")) == 1


# --------------------------------------------------------------------------
# Хелперы записи и прочие таблицы
# --------------------------------------------------------------------------


def test_add_entry_validation(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        add_entry(conn, "", "текст")
    with pytest.raises(ValueError):
        add_entry(conn, "food", "   ")

    with conn:
        entry_id = add_entry(conn, "thought", "Без явного времени")
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    assert row["kind"] == "thought"
    assert row["date"] == row["ts"][:10]


def test_add_metric(conn: sqlite3.Connection) -> None:
    today = datetime.now().astimezone().date().isoformat()
    with conn:
        weight_id = add_metric(conn, "weight", 79.4, date="2026-11-17", unit="кг")
        sleep_id = add_metric(conn, "sleep", 7.5)

    weight = conn.execute("SELECT * FROM metrics WHERE id = ?", (weight_id,)).fetchone()
    assert weight["date"] == "2026-11-17"
    assert weight["value"] == pytest.approx(79.4)
    assert weight["unit"] == "кг"
    assert weight["source"] == "telegram"

    sleep = conn.execute("SELECT * FROM metrics WHERE id = ?", (sleep_id,)).fetchone()
    assert sleep["date"] == today

    with pytest.raises(ValueError):
        add_metric(conn, "weight", float("nan"))
    with pytest.raises(ValueError):
        add_metric(conn, "weight", 79.4, date="17.11.2026")
    with pytest.raises(ValueError):
        add_metric(conn, "", 79.4)


def test_settings_kv(conn: sqlite3.Connection) -> None:
    with conn:
        set_setting(conn, "theme", "dark")
    assert get_setting(conn, "theme") == "dark"

    with conn:
        set_setting(conn, "theme", "light")
    assert get_setting(conn, "theme") == "light"
    assert get_setting(conn, "missing") is None
    assert get_setting(conn, "missing", "fallback") == "fallback"

    with pytest.raises(ValueError):
        set_setting(conn, "", "x")


def test_habits_marks_unique_and_cascade(conn: sqlite3.Connection) -> None:
    with conn:
        habit_id = int(
            conn.execute(
                "INSERT INTO habits(name, emoji, target_per_week) VALUES (?, ?, ?)",
                ("Вода", "💧", 7),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO habit_marks(habit_id, date) VALUES (?, ?)", (habit_id, "2026-11-17")
        )

    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO habit_marks(habit_id, date) VALUES (?, ?)",
                (habit_id, "2026-11-17"),
            )

    with conn:
        conn.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
    assert conn.execute("SELECT count(*) FROM habit_marks").fetchone()[0] == 0


def test_jobs_defaults(conn: sqlite3.Connection) -> None:
    with conn:
        job_id = int(
            conn.execute(
                "INSERT INTO jobs(kind, payload_json) VALUES (?, ?)",
                ("transcribe", '{"lesson_id":1}'),
            ).lastrowid
        )

    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "queued"
    assert job["attempts"] == 0
    assert job["error"] == ""
    assert job["created"]
    assert job["finished"] is None

    with conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', attempts = 2, error = 'нет ffmpeg', "
            "finished = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (job_id,),
        )
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "failed"
    assert job["attempts"] == 2
    assert job["finished"]


# --------------------------------------------------------------------------
# Pydantic-модели API
# --------------------------------------------------------------------------


def test_models_from_rows(conn: sqlite3.Connection) -> None:
    course_id, (lesson_id, second_lesson) = _create_course(conn, lessons=2)
    with conn:
        conn.execute(
            "INSERT INTO lesson_progress(lesson_id, position_s, watched_s) VALUES (?, ?, ?)",
            (lesson_id, 120.0, 90.0),
        )
        conn.execute(
            "INSERT INTO transcripts(lesson_id, lang, text) VALUES (?, 'ru', 'полный конспект')",
            (lesson_id,),
        )
        conn.execute(
            "INSERT INTO segments(lesson_id, start_s, end_s, text) VALUES (?, ?, ?, ?)",
            (lesson_id, 0.0, 30.0, "Клетчатка и вода"),
        )
        conn.execute(
            "INSERT INTO notes(lesson_id, ts, text) VALUES (?, ?, ?)",
            (lesson_id, "2026-11-17T10:00:00+03:00", "вернуться к теме"),
        )
        conn.execute(
            "INSERT INTO metrics(date, kind, value, unit) VALUES ('2026-11-17', 'weight', 79.4, 'кг')"
        )
        conn.execute(
            "INSERT INTO goals(title, target, current, unit, deadline) VALUES (?, ?, ?, ?, ?)",
            ("Дойти до 78 кг", 78.0, 79.4, "кг", "2027-01-01"),
        )
        conn.execute(
            "INSERT INTO habits(name, emoji) VALUES (?, ?)", ("Вода", "💧")
        )
        conn.execute(
            "INSERT INTO jobs(kind, payload_json) VALUES ('transcribe', '{\"lesson_id\":1}')"
        )
        entry_id = add_entry(
            conn,
            "food",
            "Овсянка",
            ts="2026-11-17T08:30:00+03:00",
            meta={"mood": "хорошо"},
        )

    course = Course.from_row(
        conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    )
    lesson = Lesson.from_row(
        conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    )
    progress = Progress.from_row(
        conn.execute("SELECT * FROM lesson_progress WHERE lesson_id = ?", (lesson_id,)).fetchone()
    )
    segment = Segment.from_row(
        conn.execute("SELECT * FROM segments WHERE lesson_id = ?", (lesson_id,)).fetchone()
    )
    note = Note.from_row(
        conn.execute("SELECT * FROM notes WHERE lesson_id = ?", (lesson_id,)).fetchone()
    )
    transcript = Transcript.from_row(
        conn.execute("SELECT * FROM transcripts WHERE lesson_id = ?", (lesson_id,)).fetchone()
    )
    metric = Metric.from_row(conn.execute("SELECT * FROM metrics").fetchone())
    goal = Goal.from_row(conn.execute("SELECT * FROM goals").fetchone())
    habit = Habit.from_row(conn.execute("SELECT * FROM habits").fetchone())
    job = JobStatus.from_row(conn.execute("SELECT * FROM jobs").fetchone())
    entry = Entry.from_row(conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone())

    assert course.title == "Здоровье"
    assert lesson.course_id == course_id
    assert lesson.idx == 1
    assert progress.position_s == pytest.approx(120.0)
    assert segment.start_s == pytest.approx(0.0)
    assert note.text == "вернуться к теме"
    assert transcript.lang == "ru"
    assert metric.value == pytest.approx(79.4)
    assert goal.deadline == "2027-01-01"
    assert habit.emoji == "💧"
    assert job.payload == {"lesson_id": 1}
    assert job.status == "queued"
    assert entry.meta == {"mood": "хорошо"}
    assert entry.date == "2026-11-17"

    detail = LessonDetail(
        lesson=lesson,
        course=course,
        progress=progress,
        transcript=transcript.text,
        segments=[segment],
        notes=[note],
        next_lesson_id=second_lesson,
    )
    detail_dump = detail.model_dump()
    assert detail_dump["lesson"]["title"] == "Урок 1"
    assert detail_dump["segments"][0]["text"] == "Клетчатка и вода"
    assert detail_dump["next_lesson_id"] == second_lesson

    course_detail = CourseDetail(**course.model_dump(), lessons=[lesson])
    assert len(course_detail.lessons) == 1

    kpis = OverviewKpis(
        streak={"days": 12, "bestDays": 21},
        checkins={"completed": 4, "total": 6},
        workouts={"week": 3, "target": 4},
        sleep={"lastHours": 7.4, "avg7Hours": 7.1},
        water={"ml": 1500, "targetMl": 2000},
        mood={"score": 7, "max": 10, "label": "спокойно", "avg7": 6.8},
    )
    overview = Overview(
        date="2026-11-17",
        kpis=kpis,
        weight={
            "currentKg": 79.4,
            "targetKg": 78,
            "delta30Kg": -1.2,
            "series": [{"date": "2026-11-17", "value": 79.4}],
        },
        course={
            "id": str(course_id),
            "title": "Здоровье",
            "lessonTitle": "Урок 1",
            "doneLessons": 0,
            "totalLessons": 2,
            "nextLessonId": str(lesson_id),
        },
        agenda=[
            {"id": "a1", "title": "Вода", "meta": "привычка", "done": True, "kind": "habit"}
        ],
        activity=[{"date": "2026-11-17", "label": "Пн", "minutes": 45}],
        insight={"title": "Сон → энергия", "text": "Сон длиннее — энергия выше", "footnote": "r = 0,42"},
        sync=SyncStatus(state="ok", at="2026-11-17T08:00:00+03:00"),
        nav={"today": 2, "habits": 1, "goals": 0, "courses": 1},
        generated_at="2026-11-17T08:00:00+03:00",
        widget_extra="разрешено",
    )
    overview_dump = overview.model_dump()
    assert overview_dump["kpis"]["water"]["ml"] == 1500
    assert overview_dump["kpis"]["streak"]["bestDays"] == 21
    assert overview_dump["weight"]["series"][0]["value"] == 79.4
    assert overview_dump["course"]["nextLessonId"] == str(lesson_id)
    assert overview_dump["nav"]["today"] == 2
    assert overview_dump["widget_extra"] == "разрешено"

    with pytest.raises(ValidationError):
        AgendaItem(id="x", title="y", kind="unknown")
    with pytest.raises(ValidationError):
        SyncStatus(state="later", at="2026-11-17T08:00:00+03:00")


def test_models_reject_broken_rows() -> None:
    with pytest.raises(ValidationError):
        Entry(id="не число", ts="2026-11-17T08:00:00+03:00", kind="food", text="x")

    with pytest.raises(TypeError):
        Entry.from_row(object())

    # Пустой/битый meta_json не должен ронять ответ API.
    entry = Entry.from_row(
        {
            "id": 1,
            "ts": "2026-11-17T08:00:00+03:00",
            "kind": "food",
            "text": "x",
            "source": "telegram",
            "meta_json": "не json",
            "date": "2026-11-17",
        }
    )
    assert entry.meta == {}
