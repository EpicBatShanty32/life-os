"""Тесты src/storage.py: схема, CRUD, WAL, метрики, goals, habits, kv."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
from src import storage as storage_mod


class StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-db-")
        self.db_path = str(Path(self._tmp.name) / "lifeos.db")
        self.storage = storage_mod.Storage(self.db_path)

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    # --------------------------------------------------------------- схема
    def test_schema_and_wal(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in ("messages", "entries", "goals", "habits", "kv"):
                self.assertIn(table, tables)
            indexes = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertIn("idx_entries_date", indexes)
            self.assertIn("idx_entries_written", indexes)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
        finally:
            conn.close()

    def test_reopen_existing_db_keeps_data(self) -> None:
        self.storage.kv_set("hello", "world")
        self.storage.close()
        reopened = storage_mod.Storage(self.db_path)
        try:
            self.assertEqual(reopened.kv_get("hello"), "world")
        finally:
            reopened.close()

    # ----------------------------------------------------------- сообщения
    def test_messages_roundtrip_and_history_order(self) -> None:
        self.storage.record_message(5, "in", "привет")
        self.storage.record_message(5, "out", "здравствуй", slot="morning")
        self.storage.record_message(5, "in", "как дела")
        history = self.storage.chat_history(limit=10)
        self.assertEqual([row["text"] for row in history],
                         ["привет", "здравствуй", "как дела"])
        self.assertEqual(history[0]["direction"], "in")
        self.assertEqual(history[1]["slot"], "morning")
        self.assertIn("ts", history[0])
        # limit работает и берёт последние
        tail = self.storage.chat_history(limit=2)
        self.assertEqual([row["text"] for row in tail], ["здравствуй", "как дела"])
        self.assertEqual(self.storage.chat_history(limit=0), [])

    # -------------------------------------------------------------- записи
    def test_add_entry_and_entries_range(self) -> None:
        entry_id = self.storage.add_entry({
            "date": "2026-11-16",
            "ts": "2026-11-16T10:00:00+03:00",
            "slot": "morning",
            "category": "tasks",
            "title": "план дня",
            "text": "написать отчёт",
            "raw": "план: отчёт",
            "metrics": {"tasks_planned": ["отчёт"]},
        })
        self.assertIsInstance(entry_id, int)
        self.assertGreater(entry_id, 0)
        rows = self.storage.entries("2026-11-16", "2026-11-16")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], entry_id)
        self.assertEqual(row["category"], "tasks")
        self.assertEqual(row["title"], "план дня")
        self.assertEqual(row["metrics"], {"tasks_planned": ["отчёт"]})
        # за пределами диапазона — пусто
        self.assertEqual(self.storage.entries("2026-11-17", "2026-11-18"), [])
        self.assertEqual(self.storage.entries("2026-11-01", "2026-11-30"), rows)

    def test_add_entry_defaults_and_bad_metrics(self) -> None:
        entry_id = self.storage.add_entry({"title": "без даты", "metrics": "не json"})
        rows = self.storage.entries("0001-01-01", "9999-12-31")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertGreaterEqual(len(row["date"]), 10, "date подставлена")
        self.assertTrue(row["ts"], "ts подставлен")
        self.assertEqual(row["metrics"], {}, "битый metrics → {}")
        self.assertEqual(rows[0]["id"], entry_id)
        # metrics-строка с валидным JSON
        self.storage.add_entry({
            "date": "2026-12-01", "ts": "2026-12-01T11:00:00+03:00",
            "metrics": '{"water_ml": 500}',
        })
        self.assertEqual(self.storage.entries("2026-12-01", "2026-12-01")[0]["metrics"],
                         {"water_ml": 500})

    def test_written_flags(self) -> None:
        first = self.storage.add_entry({"date": "2026-11-16", "ts": "2026-11-16T10:00:00+03:00"})
        second = self.storage.add_entry({"date": "2026-11-16", "ts": "2026-11-16T11:00:00+03:00"})
        pending = self.storage.entries_unwritten()
        self.assertEqual({row["id"] for row in pending}, {first, second})
        self.storage.mark_written([first])
        pending = self.storage.entries_unwritten()
        self.assertEqual([row["id"] for row in pending], [second])
        self.storage.set_entry_written(second, True)
        self.assertEqual(self.storage.entries_unwritten(), [])
        self.storage.set_entry_written(second, False)
        self.assertEqual([row["id"] for row in self.storage.entries_unwritten()], [second])
        # мусорные id не должны ломать
        self.storage.mark_written([None, "abc", None])
        self.storage.mark_written([])

    # ---------------------------------------------------------------- kv
    def test_kv(self) -> None:
        self.assertIsNone(self.storage.kv_get("нет"))
        self.assertEqual(self.storage.kv_get("нет", "def"), "def")
        self.storage.kv_set("k", "v1")
        self.assertEqual(self.storage.kv_get("k"), "v1")
        self.storage.kv_set("k", "v2")
        self.assertEqual(self.storage.kv_get("k"), "v2")
        self.storage.kv_del("k")
        self.assertIsNone(self.storage.kv_get("k"))
        self.storage.kv_del("k")  # повторное удаление не падает
        self.storage.kv_set(123, 456)  # не строки допустимы
        self.assertEqual(self.storage.kv_get("123"), "456")

    # --------------------------------------------------------------- цели
    def test_goals_crud(self) -> None:
        goal_id = self.storage.upsert_goal({
            "title": "Пробежать 10 км",
            "target": "10",
            "current": "3",
            "deadline": "2027-06-01",
        })
        goal = self.storage.get_goal(goal_id)
        self.assertIsNotNone(goal)
        self.assertEqual(goal["title"], "Пробежать 10 км")
        self.assertEqual(goal["status"], "active")
        self.assertTrue(goal["created"])
        self.assertTrue(goal["updated"])

        # частичное обновление
        returned = self.storage.upsert_goal({"id": goal_id, "current": "7"})
        self.assertEqual(returned, goal_id)
        goal = self.storage.get_goal(goal_id)
        self.assertEqual(goal["current"], "7")
        self.assertEqual(goal["title"], "Пробежать 10 км")
        # пустое название не затирает существующее
        self.storage.upsert_goal({"id": goal_id, "title": "   "})
        self.assertEqual(self.storage.get_goal(goal_id)["title"], "Пробежать 10 км")

        # вторая цель — новые сначала
        second = self.storage.upsert_goal({"title": "Прочитать 12 книг"})
        self.assertEqual([g["title"] for g in self.storage.goals()],
                         ["Прочитать 12 книг", "Пробежать 10 км"])
        self.storage.delete_goal(second)
        self.assertIsNone(self.storage.get_goal(second))
        self.assertEqual(len(self.storage.goals()), 1)

        with self.assertRaises(ValueError):
            self.storage.upsert_goal({"title": ""})
        self.assertIsNone(self.storage.get_goal(99999))

    def test_old_goals_table_is_migrated(self) -> None:
        """P0-1: в старой БД без start/direction колонки добавляются сами."""
        legacy_path = str(Path(self._tmp.name) / "legacy.db")
        conn = sqlite3.connect(legacy_path)
        try:
            conn.execute(
                "CREATE TABLE goals ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, target TEXT, "
                "current TEXT, deadline TEXT, status TEXT DEFAULT 'active', "
                "note TEXT, created TEXT, updated TEXT)"
            )
            conn.execute(
                "INSERT INTO goals(title, target, current, status) "
                "VALUES ('Пробежать 10 км', '10', '3', 'active')"
            )
            conn.commit()
        finally:
            conn.close()

        legacy = storage_mod.Storage(legacy_path)
        try:
            goal = legacy.goals()[0]
            self.assertEqual(goal["title"], "Пробежать 10 км")
            self.assertEqual(goal["start"], "")
            self.assertEqual(goal["direction"], "")
            # новые колонки пишутся и читаются
            legacy.upsert_goal({"id": goal["id"], "start": "3",
                                "direction": "up"})
            goal = legacy.get_goal(goal["id"])
            self.assertEqual(goal["start"], "3")
            self.assertEqual(goal["direction"], "up")
        finally:
            legacy.close()

    def test_goal_start_and_direction_roundtrip(self) -> None:
        goal_id = self.storage.upsert_goal({
            "title": "Сбросить вес", "target": "78", "current": "92",
            "start": "92", "direction": "down",
        })
        goal = self.storage.get_goal(goal_id)
        self.assertEqual(goal["start"], "92")
        self.assertEqual(goal["direction"], "down")
        # частичное обновление current не затирает start/direction
        self.storage.upsert_goal({"id": goal_id, "current": "85"})
        goal = self.storage.get_goal(goal_id)
        self.assertEqual(goal["start"], "92")
        self.assertEqual(goal["direction"], "down")
        self.assertEqual(goal["current"], "85")

    def test_goal_sql_injection_safe(self) -> None:
        evil = "'; DROP TABLE goals; --"
        self.storage.upsert_goal({"title": evil, "note": "\"quoted\""})
        self.assertEqual(self.storage.goals()[0]["title"], evil)

    # ----------------------------------------------------------- привычки
    def test_habits_and_streak(self) -> None:
        self.storage.touch_habit("вода", "2026-11-15")
        habits = self.storage.habits()
        self.assertEqual(habits[0]["name"], "вода")
        self.assertEqual(habits[0]["streak"], 1)
        self.assertEqual(habits[0]["total"], 1)
        # повтор в тот же день — идемпотентно
        self.storage.touch_habit("вода", "2026-11-15")
        habits = self.storage.habits()
        self.assertEqual((habits[0]["streak"], habits[0]["total"]), (1, 1))
        # следующий день подряд — стрик растёт
        self.storage.touch_habit("вода", "2026-11-16")
        habits = self.storage.habits()
        self.assertEqual((habits[0]["streak"], habits[0]["total"]), (2, 2))
        self.assertEqual(habits[0]["last_done"], "2026-11-16")
        # пропуск дня — стрик сбрасывается
        self.storage.touch_habit("вода", "2026-11-20")
        self.assertEqual(self.storage.habits()[0]["streak"], 1)
        # пустое имя игнорируется
        self.storage.touch_habit("  ", "2026-11-20")
        self.assertEqual(len(self.storage.habits()), 1)

    # ------------------------------------------------------------- прочее
    def test_close_idempotent(self) -> None:
        self.storage.close()
        self.storage.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.storage.kv_get("x")

    def test_context_manager(self) -> None:
        path = str(Path(self._tmp.name) / "ctx.db")
        with storage_mod.Storage(path) as storage:
            storage.kv_set("a", "b")
        self.assertTrue(Path(path).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StorageCyrillicPathTestCase(unittest.TestCase):
    def test_db_path_with_cyrillic_and_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lifeos-db-cyr-") as tmp:
            db_path = Path(tmp) / "жизнь тест" / "база данных.sqlite"
            storage = storage_mod.Storage(str(db_path))
            try:
                storage.kv_set("ключ", "значение")
                self.assertEqual(storage.kv_get("ключ"), "значение")
                storage.add_entry({"date": "2026-11-16",
                                   "ts": "2026-11-16T12:00:00+03:00",
                                   "text": "кириллица"})
                self.assertEqual(len(storage.entries("2026-11-16", "2026-11-16")), 1)
                self.assertTrue(db_path.is_file())
            finally:
                storage.close()
