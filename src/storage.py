"""Хранилище Life OS на SQLite (стандартная библиотека).

Схема — ТЗ §5: ``messages``, ``entries``, ``goals``, ``habits``, ``kv``.
Режим WAL и `busy_timeout`, чтобы дашборд/тесты могли читать базу, пока
агент в неё пишет.

``entries.metrics`` хранится JSON-строкой, наружу всегда отдаётся dict.
Время — ISO с оффсетом таймзоны приложения (по умолчанию Europe/Moscow,
``2026-11-16T14:02:11+03:00``); таймзону можно передать в конструктор —
строкой IANA или готовым ``tzinfo``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

__all__ = ["Storage"]

_DEFAULT_TZ_NAME = "Europe/Moscow"
_TZ = ZoneInfo(_DEFAULT_TZ_NAME)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    direction TEXT,
    text TEXT,
    ts TEXT,
    slot TEXT
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    ts TEXT,
    slot TEXT,
    category TEXT,
    title TEXT,
    text TEXT,
    raw TEXT,
    metrics TEXT,
    written INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    target TEXT,
    current TEXT,
    deadline TEXT,
    status TEXT DEFAULT 'active',
    note TEXT,
    created TEXT,
    updated TEXT
);

CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    streak INTEGER DEFAULT 0,
    last_done TEXT,
    total INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date);
CREATE INDEX IF NOT EXISTS idx_entries_written ON entries(written);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
"""

_GOAL_FIELDS = ("title", "target", "current", "deadline", "status", "note")


def _resolve_tz(value) -> tzinfo:
    """tzinfo из аргумента конструктора: ``None``/кривое → Europe/Moscow.

    Принимает готовый ``tzinfo`` или строку IANA. Битое значение не роняет
    Storage (обратная совместимость: старый вызов ``Storage(path)`` работает).
    """
    if value is None:
        return _TZ
    if isinstance(value, str):
        name = value.strip()
        if not name:
            return _TZ
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 — неизвестная зона/нет tzdata
            return _TZ
    if isinstance(value, tzinfo):
        return value
    return _TZ


def _today_iso(tz: tzinfo | None = None) -> str:
    return datetime.now(_resolve_tz(tz)).date().isoformat()


def _now_iso(tz: tzinfo | None = None) -> str:
    return datetime.now(_resolve_tz(tz)).isoformat(timespec="seconds")


def _text(value) -> str:
    """Значение в TEXT-колонку: None → пустая строка, остальное → str."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _parse_metrics(raw) -> dict:
    """JSON-строка metrics → dict (битое/пустое → {})."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _entry_from_row(row: sqlite3.Row) -> dict:
    entry = dict(row)
    entry["metrics"] = _parse_metrics(entry.get("metrics"))
    return entry


def _goal_from_row(row: sqlite3.Row) -> dict:
    return {key: ("" if row[key] is None else row[key]) for key in row.keys()}


class Storage:
    """Обёртка над SQLite: сообщения, записи, цели, привычки, key-value.

    Args:
        db_path: путь к файлу БД (или ``:memory:``).
        tz: таймзона для полей времени — строка IANA или ``tzinfo``.
            По умолчанию Europe/Moscow (обратная совместимость).
        timezone: псевдоним ``tz`` (на случай вызова по имени из конфига).
    """

    def __init__(self, db_path: str, tz=None, timezone=None) -> None:
        self.db_path = str(db_path)
        self.tz = _resolve_tz(tz if tz is not None else timezone)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.db_path, timeout=10, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL + busy_timeout: читатели не блокируют писателя.
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass  # :memory: и экзотические ФС — не фатально
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -------------------------------------------------------------- служебное
    def _execute(self, sql: str, params=(), commit: bool = True):
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("Storage уже закрыт")
            cursor = self._conn.execute(sql, params)
            if commit:
                self._conn.commit()
            return cursor

    def close(self) -> None:
        """Закрывает соединение (идемпотентно)."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                except sqlite3.Error:
                    pass
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -------------------------------------------------------------- время
    def _now_iso(self) -> str:
        """Текущее время в таймзоне хранилища, ISO с оффсетом."""
        return _now_iso(self.tz)

    def _today_iso(self) -> str:
        """Текущая дата в таймзоне хранилища (YYYY-MM-DD)."""
        return _today_iso(self.tz)

    # ------------------------------------------------------------- сообщения
    def record_message(self, chat_id: int, direction: str, text: str,
                       slot: str | None = None) -> int:
        """Сохраняет сообщение диалога. direction: ``in`` (пользователь) / ``out``."""
        cursor = self._execute(
            "INSERT INTO messages(chat_id, direction, text, ts, slot) VALUES (?,?,?,?,?)",
            (int(chat_id), _text(direction), _text(text), self._now_iso(), slot),
        )
        return int(cursor.lastrowid or 0)

    def chat_history(self, limit: int = 20) -> list[dict]:
        """Последние сообщения в хронологическом порядке (старые → новые)."""
        limit = int(limit)
        if limit <= 0:
            return []
        rows = self._execute(
            "SELECT direction, text, ts, slot FROM messages ORDER BY id DESC LIMIT ?",
            (limit,), commit=False,
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # --------------------------------------------------------------- записи
    def add_entry(self, entry: dict) -> int:
        """Добавляет запись дневника, возвращает id.

        ``entry`` — формат INTERFACES §12. metrics (dict) сериализуется в JSON.
        Недостающие date/ts подставляются текущим временем Москвы.
        """
        entry = dict(entry or {})
        ts = _text(entry.get("ts")) or self._now_iso()
        date_value = _text(entry.get("date")) or ts[:10] or self._today_iso()
        metrics = entry.get("metrics")
        if isinstance(metrics, str):
            metrics_json = metrics if metrics.strip() else "{}"
        else:
            try:
                metrics_json = json.dumps(metrics if isinstance(metrics, dict) else {},
                                          ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                metrics_json = "{}"
        cursor = self._execute(
            "INSERT INTO entries(date, ts, slot, category, title, text, raw, metrics, written) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                date_value,
                ts,
                entry.get("slot"),
                entry.get("category"),
                _text(entry.get("title")),
                _text(entry.get("text")),
                _text(entry.get("raw")),
                metrics_json,
                0,
            ),
        )
        return int(cursor.lastrowid or 0)

    def entries(self, date_from: str, date_to: str) -> list[dict]:
        """Записи за диапазон дат включительно (YYYY-MM-DD), старые → новые."""
        rows = self._execute(
            "SELECT * FROM entries WHERE date >= ? AND date <= ? ORDER BY date, id",
            (_text(date_from), _text(date_to)), commit=False,
        ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def entries_unwritten(self) -> list[dict]:
        """Записи, которые ещё не попали в Obsidian (written = 0)."""
        rows = self._execute(
            "SELECT * FROM entries WHERE written = 0 ORDER BY id", commit=False
        ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def mark_written(self, ids: list[int]) -> None:
        """Помечает записи как записанные в Obsidian."""
        clean = []
        for entry_id in ids or []:
            try:
                clean.append(int(entry_id))
            except (TypeError, ValueError):
                continue
        if not clean:
            return
        placeholders = ",".join("?" for _ in clean)
        self._execute(
            "UPDATE entries SET written = 1 WHERE id IN (%s)" % placeholders, tuple(clean)
        )

    def set_entry_written(self, entry_id: int, value: bool = True) -> None:
        self._execute(
            "UPDATE entries SET written = ? WHERE id = ?",
            (1 if value else 0, int(entry_id)),
        )

    # ------------------------------------------------------------ key-value
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self._execute(
            "SELECT value FROM kv WHERE key = ?", (_text(key),), commit=False
        ).fetchone()
        return row["value"] if row is not None else default

    def kv_set(self, key: str, value: str) -> None:
        self._execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
            (_text(key), _text(value)),
        )

    def kv_del(self, key: str) -> None:
        self._execute("DELETE FROM kv WHERE key = ?", (_text(key),))

    # ------------------------------------------------------------------ цели
    def goals(self) -> list[dict]:
        """Все цели, новые сначала."""
        rows = self._execute(
            "SELECT * FROM goals ORDER BY id DESC", commit=False
        ).fetchall()
        return [_goal_from_row(row) for row in rows]

    def get_goal(self, goal_id: int) -> dict | None:
        row = self._execute(
            "SELECT * FROM goals WHERE id = ?", (int(goal_id),), commit=False
        ).fetchone()
        return _goal_from_row(row) if row is not None else None

    def upsert_goal(self, goal: dict) -> int:
        """Вставляет цель (без id) или обновляет существующую (с id).

        При обновлении достаточно передать только меняющиеся поля
        (например ``{"id": 5, "current": "7"}``).
        """
        goal = dict(goal or {})
        now = self._now_iso()
        goal_id = goal.get("id")
        try:
            goal_id = int(goal_id) if goal_id is not None else None
        except (TypeError, ValueError):
            goal_id = None

        exists = False
        if goal_id is not None:
            row = self._execute(
                "SELECT id FROM goals WHERE id = ?", (goal_id,), commit=False
            ).fetchone()
            exists = row is not None

        if exists:
            sets = []
            params: list = []
            for field in _GOAL_FIELDS:
                if field in goal:
                    value = _text(goal.get(field))
                    if field == "title" and not value.strip():
                        continue  # пустым названием существующую цель не затираем
                    sets.append("%s = ?" % field)
                    params.append(value)
            sets.append("updated = ?")
            params.append(now)
            params.append(goal_id)
            self._execute(
                "UPDATE goals SET %s WHERE id = ?" % ", ".join(sets), tuple(params)
            )
            return int(goal_id)

        title = _text(goal.get("title")).strip()
        if not title:
            raise ValueError("upsert_goal: у новой цели обязательно должно быть название (title)")
        columns = list(_GOAL_FIELDS) + ["created", "updated"]
        values = [
            title,
            _text(goal.get("target")),
            _text(goal.get("current")),
            _text(goal.get("deadline")),
            _text(goal.get("status")) or "active",
            _text(goal.get("note")),
            now,
            now,
        ]
        if goal_id is not None:
            columns.insert(0, "id")
            values.insert(0, goal_id)
        placeholders = ",".join("?" for _ in columns)
        cursor = self._execute(
            "INSERT INTO goals(%s) VALUES (%s)" % (", ".join(columns), placeholders),
            tuple(values),
        )
        return int(goal_id if goal_id is not None else (cursor.lastrowid or 0))

    def delete_goal(self, goal_id: int) -> None:
        self._execute("DELETE FROM goals WHERE id = ?", (int(goal_id),))

    # -------------------------------------------------------------- привычки
    def habits(self) -> list[dict]:
        rows = self._execute(
            "SELECT name, streak, last_done, total FROM habits ORDER BY id", commit=False
        ).fetchall()
        return [dict(row) for row in rows]

    def touch_habit(self, name: str, day: str) -> None:
        """Отмечает привычку за дату (``YYYY-MM-DD``), считая стрик.

        Повторный вызов за ту же дату ничего не меняет (идемпотентно).
        """
        name = _text(name).strip()
        if not name:
            return
        day = _text(day).strip() or self._today_iso()
        row = self._execute(
            "SELECT id, streak, last_done FROM habits WHERE name = ?", (name,), commit=False
        ).fetchone()
        if row is None:
            self._execute(
                "INSERT INTO habits(name, streak, last_done, total) VALUES (?,?,?,?)",
                (name, 1, day, 1),
            )
            return
        if row["last_done"] == day:
            return

        streak = int(row["streak"] or 0)
        try:
            prev_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        except ValueError:
            prev_day = None
        streak = streak + 1 if prev_day is not None and row["last_done"] == prev_day else 1
        self._execute(
            "UPDATE habits SET streak = ?, last_done = ?, total = total + 1 WHERE id = ?",
            (streak, day, int(row["id"])),
        )
