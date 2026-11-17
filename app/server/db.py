"""SQLite-слой Life OS v3: подключение, схема, миграции, FTS5-поиск.

Схема версионируется через ``PRAGMA user_version``; текущая версия —
:data:`SCHEMA_VERSION` (= 3). :func:`migrate` идемпотентна: повторный вызов на
актуальной базе ничего не делает и не дублирует данные.

Миграция 3 — схема v4 из ``studio/HANDOFF-OPUS-5.5.md`` §11: сферы, проекты,
задачи, инбокс, библиотека, финансы, чек-ины и итоги плюс новые колонки в
``goals``/``courses``/``habits``. Она только добавляет объекты (``CREATE ...
IF NOT EXISTS`` и проверяемый ``ALTER TABLE ADD COLUMN``), поэтому база v2
переезжает без потери данных.

Что важно знать о схеме:

* ``entries.date`` — генерируемая колонка ``substr(ts, 1, 10)``: её нельзя
  вставлять/обновлять, но можно выбирать и индексировать;
* подключение включает WAL, ``foreign_keys=ON`` и ``busy_timeout=5000``;
* полнотекстовый поиск — внешние FTS5-таблицы ``entries_fts`` / ``segments_fts``
  с триггерами синхронизации; пользовательский запрос собирает :func:`fts_query`
  (кавычки и ``*`` защищают от синтаксиса FTS и спецсимволов);
* база Life OS v0.x (таблицы ``messages``/``money``/``learning`` или старый
  формат ``entries``/``goals``/``habits``) не перезаписывается: :func:`migrate`
  бросает :class:`LegacySchemaError` и просит указать новый файл.

Хелперы ``add_entry``/``add_metric``/``set_setting`` не коммитят транзакцию:
вызывающий код оборачивает их в ``with conn:``.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date as date_cls
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = [
    "BUSY_TIMEOUT_MS",
    "FTS_TABLES",
    "LegacySchemaError",
    "SCHEMA_VERSION",
    "SchemaVersionError",
    "TABLES",
    "add_entry",
    "add_metric",
    "connect",
    "ensure_fts",
    "fts5_available",
    "fts_query",
    "get_setting",
    "init_db",
    "is_legacy_schema",
    "migrate",
    "search_entries",
    "search_segments",
    "set_setting",
    "user_version",
]

SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 5000

#: Таблицы схемы (без FTS-индексов): база v3 и новые сущности v4 из миграции 3.
TABLES: tuple[str, ...] = (
    # миграция 1
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
    # миграция 3 — схема v4 (§11)
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

#: Виртуальные FTS5-таблицы.
FTS_TABLES: tuple[str, ...] = ("entries_fts", "segments_fts")

# Колонки, которые обязаны существовать, чтобы базу можно было считать
# v3-совместимой (используется только при user_version = 0).
_REQUIRED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "entries": ("id", "ts", "kind", "text", "source", "meta_json"),
    "goals": ("id", "title", "target", "current", "unit", "deadline", "status", "created"),
    "habits": ("id", "name", "emoji", "target_per_week", "archived", "created"),
}

# Таблицы, которые есть только в v0.x и однозначно выдают чужую схему.
_LEGACY_ONLY_TABLES = frozenset({"messages", "money", "learning", "kv"})


class SchemaVersionError(RuntimeError):
    """Файл БД новее, чем понимает этот код."""


class LegacySchemaError(RuntimeError):
    """В файле БД схема Life OS v0.x — v3 не имеет права её перезаписывать."""


# --------------------------------------------------------------------------
# DDL: миграция 1
# --------------------------------------------------------------------------

_MIGRATION_1: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'telegram',
        meta_json TEXT NOT NULL DEFAULT '{}',
        date TEXT GENERATED ALWAYS AS (substr(ts, 1, 10)) VIRTUAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        kind TEXT NOT NULL,
        value REAL NOT NULL,
        unit TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'telegram'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS habits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        emoji TEXT NOT NULL DEFAULT '',
        target_per_week INTEGER NOT NULL DEFAULT 7,
        archived INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS habit_marks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        value REAL NOT NULL DEFAULT 1,
        UNIQUE (habit_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        target REAL,
        current REAL NOT NULL DEFAULT 0,
        unit TEXT NOT NULL DEFAULT '',
        deadline TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        cover TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lessons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        idx INTEGER NOT NULL DEFAULT 0,
        title TEXT NOT NULL,
        media_path TEXT NOT NULL DEFAULT '',
        duration_s REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'new',
        UNIQUE (course_id, idx)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lesson_progress (
        lesson_id INTEGER PRIMARY KEY REFERENCES lessons(id) ON DELETE CASCADE,
        position_s REAL NOT NULL DEFAULT 0,
        watched_s REAL NOT NULL DEFAULT 0,
        updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transcripts (
        lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
        lang TEXT NOT NULL DEFAULT 'ru',
        text TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (lesson_id, lang)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS segments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
        start_s REAL NOT NULL,
        end_s REAL NOT NULL,
        text TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
        ts TEXT NOT NULL,
        text TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        finished TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings_kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date)",
    "CREATE INDEX IF NOT EXISTS idx_entries_kind_date ON entries(kind, date)",
    "CREATE INDEX IF NOT EXISTS idx_entries_ts ON entries(ts)",
    "CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_kind_date ON metrics(kind, date)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_date ON metrics(date)",
    "CREATE INDEX IF NOT EXISTS idx_habit_marks_date ON habit_marks(date)",
    "CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status)",
    "CREATE INDEX IF NOT EXISTS idx_lessons_course ON lessons(course_id, idx)",
    "CREATE INDEX IF NOT EXISTS idx_segments_lesson ON segments(lesson_id, start_s)",
    "CREATE INDEX IF NOT EXISTS idx_notes_lesson ON notes(lesson_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
        text,
        content='entries',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2',
        prefix='2 3'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
        text,
        content='segments',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2',
        prefix='2 3'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entries_fts_ai AFTER INSERT ON entries BEGIN
        INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entries_fts_ad AFTER DELETE ON entries BEGIN
        INSERT INTO entries_fts(entries_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entries_fts_au AFTER UPDATE ON entries BEGIN
        INSERT INTO entries_fts(entries_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS segments_fts_ai AFTER INSERT ON segments BEGIN
        INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS segments_fts_ad AFTER DELETE ON segments BEGIN
        INSERT INTO segments_fts(segments_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS segments_fts_au AFTER UPDATE ON segments BEGIN
        INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    # Индекс мог отсутствовать (частично созданная база) — восстанавливаем.
    "INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')",
    "INSERT INTO segments_fts(segments_fts) VALUES ('rebuild')",
)

def _migration_2_jobs_meta(conn: sqlite3.Connection) -> None:
    """v2: колонка ``jobs.meta_json`` — прогресс и пояснения воркера.

    Добавляется отдельной миграцией, а не в v1, чтобы уже созданные базы v3.0
    обновлялись без пересоздания: ``ALTER TABLE ... ADD COLUMN`` безопасен для
    существующих задач (у старых ``meta_json`` = ``'{}'``).
    """
    columns = {str(info[1]) for info in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "meta_json" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")


# --------------------------------------------------------------------------
# DDL: миграция 3 — схема v4 (сферы, проекты, задачи, библиотека, финансы,
# рефлексия). Источник — studio/HANDOFF-OPUS-5.5.md §11.
# --------------------------------------------------------------------------

_MIGRATION_3_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS spheres (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        color TEXT NOT NULL DEFAULT '#A18BB7',
        icon TEXT NOT NULL DEFAULT '',
        sort INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS goal_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        due TEXT,
        sort INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS anti_goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period TEXT NOT NULL CHECK (period IN ('month', 'quarter')),
        text TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        sphere_id INTEGER REFERENCES spheres(id) ON DELETE SET NULL,
        goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
        deadline TEXT,
        status TEXT NOT NULL DEFAULT 'idea'
            CHECK (status IN ('idea', 'active', 'paused', 'done')),
        outcome TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        updated TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        sort INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'note' CHECK (kind IN ('note', 'link')),
        title TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        sphere_id INTEGER REFERENCES spheres(id) ON DELETE SET NULL,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
        list_kind TEXT NOT NULL DEFAULT 'work' CHECK (list_kind IN ('work', 'personal')),
        tags TEXT NOT NULL DEFAULT '',
        due_date TEXT,
        start_at TEXT,
        end_at TEXT,
        resource TEXT NOT NULL DEFAULT 'important'
            CHECK (resource IN ('must', 'important', 'low')),
        status TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'done', 'cancelled')),
        recurrence TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        done_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inbox_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'processed')),
        converted_type TEXT NOT NULL DEFAULT '',
        converted_id INTEGER,
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS books (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        author TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT 'nonfiction'
            CHECK (category IN ('pro', 'fiction', 'nonfiction')),
        pages_total INTEGER NOT NULL DEFAULT 0,
        pages_read INTEGER NOT NULL DEFAULT 0,
        shelf TEXT NOT NULL DEFAULT 'queue'
            CHECK (shelf IN ('reading', 'queue', 'done', 'dropped')),
        rating INTEGER,
        why TEXT NOT NULL DEFAULT '',
        priority INTEGER NOT NULL DEFAULT 0,
        finished_at TEXT,
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        updated TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS book_insights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        text TEXT NOT NULL,
        sort INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS book_quotes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        text TEXT NOT NULL,
        page INTEGER,
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS book_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        text TEXT NOT NULL,
        task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS finance_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'card'
            CHECK (kind IN ('cash', 'card', 'savings', 'broker')),
        currency TEXT NOT NULL DEFAULT 'RUB',
        balance_start_kopecks INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        sort INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS finance_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
        color TEXT NOT NULL DEFAULT '#A18BB7',
        sort INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        UNIQUE (name, kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        amount_kopecks INTEGER NOT NULL CHECK (amount_kopecks > 0),
        kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
        category_id INTEGER REFERENCES finance_categories(id) ON DELETE SET NULL,
        account_id INTEGER REFERENCES finance_accounts(id) ON DELETE SET NULL,
        note TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual',
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER NOT NULL REFERENCES finance_categories(id) ON DELETE CASCADE,
        month TEXT NOT NULL,
        limit_kopecks INTEGER NOT NULL,
        UNIQUE (category_id, month)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL UNIQUE,
        energy INTEGER NOT NULL CHECK (energy BETWEEN 1 AND 5),
        mood TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period TEXT NOT NULL CHECK (period IN ('week', 'month')),
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        wins TEXT NOT NULL DEFAULT '',
        misses TEXT NOT NULL DEFAULT '',
        focus TEXT NOT NULL DEFAULT '',
        care TEXT NOT NULL DEFAULT '',
        facts_json TEXT NOT NULL DEFAULT '{}',
        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        UNIQUE (period, start_date)
    )
    """,
    # Индексы из §11: обязательны, поиск идёт через LIKE по индексированным
    # полям (FTS для новых сущностей намеренно не расширяем).
    "CREATE INDEX IF NOT EXISTS idx_tasks_due_status ON tasks(due_date, status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_sphere ON tasks(sphere_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_list_kind ON tasks(list_kind)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_ts ON transactions(ts)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id)",
    "CREATE INDEX IF NOT EXISTS idx_goal_steps_goal ON goal_steps(goal_id)",
    "CREATE INDEX IF NOT EXISTS idx_project_steps_project ON project_steps(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_books_shelf ON books(shelf)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox_items(status)",
    "CREATE INDEX IF NOT EXISTS idx_checkins_date ON checkins(date)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_period_start ON reviews(period, start_date)",
)

#: Новые колонки v4 в таблицах, созданных миграциями 1–2:
#: ``(таблица, колонка, DDL-хвост)``. Добавляются только если колонки ещё нет.
_MIGRATION_3_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("goals", "sphere_id", "INTEGER REFERENCES spheres(id)"),
    ("goals", "horizon", "TEXT NOT NULL DEFAULT 'year'"),
    ("goals", "parent_id", "INTEGER REFERENCES goals(id) ON DELETE SET NULL"),
    ("goals", "why", "TEXT NOT NULL DEFAULT ''"),
    ("goals", "progress", "INTEGER NOT NULL DEFAULT 0"),
    ("courses", "organizer", "TEXT NOT NULL DEFAULT ''"),
    ("courses", "deadline", "TEXT"),
    ("courses", "notes", "TEXT NOT NULL DEFAULT ''"),
    ("courses", "project_id", "INTEGER REFERENCES projects(id) ON DELETE SET NULL"),
    ("courses", "goal_id", "INTEGER REFERENCES goals(id) ON DELETE SET NULL"),
    ("habits", "sphere_id", "INTEGER REFERENCES spheres(id) ON DELETE SET NULL"),
    ("habits", "color", "TEXT NOT NULL DEFAULT '#A18BB7'"),
)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    """``ALTER TABLE ... ADD COLUMN``, только если колонки ещё нет.

    SQLite не умеет ``ADD COLUMN IF NOT EXISTS``; проверка через
    ``PRAGMA table_info`` делает миграцию идемпотентной даже на базе,
    где часть колонок уже появилась (например, после ручного ремонта).
    """
    columns = {
        str(info[1]) for info in conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    }
    if column not in columns:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))


def _migration_3_v4_schema(conn: sqlite3.Connection) -> None:
    """v3: схема v4 — сферы, проекты, задачи, библиотека, финансы, рефлексия.

    Создаёт новые таблицы и индексы (``CREATE ... IF NOT EXISTS``), затем
    аккуратно добавляет колонки в ``goals``/``courses``/``habits``. Порядок
    важен для FK: ``spheres`` раньше ``goals``/``projects``/``tasks``,
    ``tasks`` раньше ``book_actions``, ``projects`` раньше ``courses``.
    """
    for statement in _MIGRATION_3_DDL:
        conn.execute(statement)
    for table, column, ddl in _MIGRATION_3_COLUMNS:
        _add_column_if_missing(conn, table, column, ddl)


#: Применённые миграции: версия -> SQL-инструкции или функция-миграция.
MIGRATIONS: Mapping[int, tuple[str, ...] | Callable[[sqlite3.Connection], None]] = {
    1: _MIGRATION_1,
    2: _migration_2_jobs_meta,
    3: _migration_3_v4_schema,
}


# --------------------------------------------------------------------------
# Подключение и миграции
# --------------------------------------------------------------------------


def connect(
    path: str | Path,
    *,
    timeout: float = 5.0,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """Открывает БД с настройками v3: WAL, внешние ключи, busy_timeout.

    Для ``:memory:`` WAL не включается (SQLite не поддерживает его для
    in-memory баз), остальные прагмы применяются.
    """
    raw_path = str(path)
    is_memory = raw_path == ":memory:"
    if not is_memory:
        Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        raw_path,
        timeout=timeout,
        check_same_thread=check_same_thread,
        isolation_level="",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = %d" % BUSY_TIMEOUT_MS)
    if not is_memory:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    """Открывает (или создаёт) базу и применяет миграции."""
    conn = connect(path)
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Доводит базу до :data:`SCHEMA_VERSION`; возвращает версию схемы.

    Идемпотентна. На базе v0.x бросает :class:`LegacySchemaError`, на базе
    из будущего — :class:`SchemaVersionError`.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    if not fts5_available(conn):
        raise RuntimeError("SQLite собран без FTS5: Life OS v3 требует полнотекстовый поиск")
    version = user_version(conn)
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            "Файл БД новее кода: user_version=%d, поддерживается не больше %d"
            % (version, SCHEMA_VERSION)
        )
    if version == 0 and is_legacy_schema(conn):
        raise LegacySchemaError(
            "В файле БД найдена схема Life OS v0.x (таблицы messages/money/learning "
            "или старый формат entries/goals/habits). Life OS v3 не перезаписывает "
            "рабочую базу: укажите новый файл через paths.db в config/config.json "
            "или переменную окружения LIFEOS_DB_PATH (например, data/lifeos-v3.db)."
        )
    while version < SCHEMA_VERSION:
        version = _apply_migration(conn, version + 1)
    return version


def user_version(conn: sqlite3.Connection) -> int:
    """Текущая версия схемы из ``PRAGMA user_version``."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def _apply_migration(conn: sqlite3.Connection, target: int) -> int:
    migration = MIGRATIONS.get(target)
    if migration is None:
        raise SchemaVersionError("Нет миграции для версии %d" % target)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if callable(migration):
            migration(conn)
        else:
            for statement in migration:
                conn.execute(statement)
        conn.execute("PRAGMA user_version = %d" % target)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise RuntimeError("Миграция %d не применена: %s" % (target, exc)) from exc
    return target


def ensure_fts(conn: sqlite3.Connection) -> None:
    """Пересобирает FTS-индексы из таблиц-источников (ремонт после сбоя)."""
    with conn:
        conn.execute("INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')")
        conn.execute("INSERT INTO segments_fts(segments_fts) VALUES ('rebuild')")


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Собран ли SQLite с FTS5."""
    try:
        row = conn.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def is_legacy_schema(conn: sqlite3.Connection) -> bool:
    """Похожа ли база на схему Life OS v0.x.

    Проверка имеет смысл только при ``user_version = 0``: у v3-базы версия
    всегда выставлена миграцией.
    """
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    tables = {str(row[0]) for row in rows}
    if not tables:
        return False
    if tables & _LEGACY_ONLY_TABLES:
        return True
    for table, required in _REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {
            str(info[1]) for info in conn.execute("PRAGMA table_info(%s)" % table).fetchall()
        }
        if not set(required).issubset(existing):
            return True
    return False


# --------------------------------------------------------------------------
# Полнотекстовый поиск
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_MAX_QUERY_TOKENS = 16
_MAX_LIMIT = 200
# Слова-операторы FTS5 не поддерживаем: пользователь ищет словами, а не
# синтаксисом, поэтому «вес OR сон» трактуется как «вес сон».
_FTS_OPERATORS = frozenset({"OR", "AND", "NOT", "NEAR"})


def fts_query(query: str) -> str:
    """Безопасный FTS5-запрос: слова в кавычках, каждое — префиксным.

    ``вес 79`` -> ``"вес"* AND "79"*``. Спецсимволы FTS5 (``NEAR``, дефис,
    звёздочка, скобки) не попадают в запрос и не могут его сломать; слова
    ``OR``/``AND``/``NOT``/``NEAR`` отбрасываются как операторы.
    """
    tokens = [
        token
        for token in _TOKEN_RE.findall(query or "")
        if token.upper() not in _FTS_OPERATORS
    ][:_MAX_QUERY_TOKENS]
    if not tokens:
        return ""
    return " ".join('"%s"*' % token for token in tokens)


def _clamp_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 50
    return max(1, min(value, _MAX_LIMIT))


def search_entries(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 50,
    kind: str | None = None,
) -> list[sqlite3.Row]:
    """Поиск по журналу: релевантность (``bm25``), затем свежесть."""
    match = fts_query(query)
    if not match:
        return []
    sql = (
        "SELECT e.* FROM entries AS e "
        "JOIN (SELECT rowid AS rid, bm25(entries_fts) AS rank FROM entries_fts "
        "      WHERE entries_fts MATCH ?) AS f ON f.rid = e.id "
    )
    params: list[Any] = [match]
    if kind:
        sql += "WHERE e.kind = ? "
        params.append(kind)
    sql += "ORDER BY f.rank, e.ts DESC LIMIT ?"
    params.append(_clamp_limit(limit))
    return list(conn.execute(sql, params))


def search_segments(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 50,
    lesson_id: int | None = None,
) -> list[sqlite3.Row]:
    """Поиск по субтитрам уроков; строки содержат ``lesson_title``/``course_id``."""
    match = fts_query(query)
    if not match:
        return []
    sql = (
        "SELECT s.*, l.title AS lesson_title, l.course_id AS course_id "
        "FROM segments AS s "
        "JOIN (SELECT rowid AS rid, bm25(segments_fts) AS rank FROM segments_fts "
        "      WHERE segments_fts MATCH ?) AS f ON f.rid = s.id "
        "JOIN lessons AS l ON l.id = s.lesson_id "
    )
    params: list[Any] = [match]
    if lesson_id is not None:
        sql += "WHERE s.lesson_id = ? "
        params.append(int(lesson_id))
    sql += "ORDER BY f.rank, s.start_s LIMIT ?"
    params.append(_clamp_limit(limit))
    return list(conn.execute(sql, params))


# --------------------------------------------------------------------------
# Хелперы записи (не коммитят: оборачивайте в ``with conn:``)
# --------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def add_entry(
    conn: sqlite3.Connection,
    kind: str,
    text: str,
    *,
    ts: str | datetime | None = None,
    source: str = "telegram",
    meta: Mapping[str, Any] | None = None,
    tz: tzinfo | None = None,
) -> int:
    """Добавляет запись журнала и возвращает её ``id``."""
    kind_value = (kind or "").strip()
    text_value = (text or "").strip()
    if not kind_value:
        raise ValueError("entry.kind не может быть пустым")
    if not text_value:
        raise ValueError("entry.text не может быть пустым")
    ts_value = _ts_str(ts, tz)
    cursor = conn.execute(
        "INSERT INTO entries(ts, kind, text, source, meta_json) VALUES (?, ?, ?, ?, ?)",
        (ts_value, kind_value, text_value, (source or "telegram").strip(), _dump_json(meta)),
    )
    return int(cursor.lastrowid or 0)


def add_metric(
    conn: sqlite3.Connection,
    kind: str,
    value: float,
    *,
    date: str | date_cls | datetime | None = None,
    unit: str = "",
    source: str = "telegram",
) -> int:
    """Добавляет измерение (вес, сон, вода...) и возвращает ``id``."""
    kind_value = (kind or "").strip()
    if not kind_value:
        raise ValueError("metric.kind не может быть пустым")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("metric.value должно быть числом, получено %r" % (value,)) from exc
    if not math.isfinite(number):
        raise ValueError("metric.value должно быть конечным числом")
    cursor = conn.execute(
        "INSERT INTO metrics(date, kind, value, unit, source) VALUES (?, ?, ?, ?, ?)",
        (
            _date_str(date),
            kind_value,
            number,
            (unit or "").strip(),
            (source or "telegram").strip(),
        ),
    )
    return int(cursor.lastrowid or 0)


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Пишет пару в ``settings_kv`` (обновляет существующую)."""
    setting_key = (key or "").strip()
    if not setting_key:
        raise ValueError("settings_kv.key не может быть пустым")
    conn.execute(
        "INSERT INTO settings_kv(key, value, updated) "
        "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
        (setting_key, "" if value is None else str(value)),
    )


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Читает значение из ``settings_kv`` (``default``, если ключа нет)."""
    row = conn.execute("SELECT value FROM settings_kv WHERE key = ?", ((key or "").strip(),)).fetchone()
    return str(row[0]) if row is not None else default


def _dump_json(value: Mapping[str, Any] | None) -> str:
    payload = dict(value or {})
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ts_str(value: str | datetime | None, tz: tzinfo | None) -> str:
    if value is None:
        moment = datetime.now(tz) if tz is not None else datetime.now().astimezone()
        return moment.isoformat(timespec="seconds")
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz) if tz is not None else moment.astimezone()
        return moment.isoformat(timespec="seconds")
    text = str(value).strip()
    if not text:
        raise ValueError("entry.ts не может быть пустым")
    return text


def _date_str(value: str | date_cls | datetime | None) -> str:
    if value is None:
        return datetime.now().astimezone().date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date_cls):
        return value.isoformat()
    text = str(value).strip()
    if not _DATE_RE.match(text):
        raise ValueError("Ожидалась дата YYYY-MM-DD, получено %r" % (value,))
    return text
