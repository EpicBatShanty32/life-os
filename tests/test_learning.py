"""Тесты домена обучения v0.2: storage.learning_* и команды /learn.

Как и в ``test_money.py``: временная БД, фейковые Telegram/LLM/Vault/dashboard
из ``tests/_support.py``, сеть не поднимается.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import _support
from src import scheduler as scheduler_mod
from src import storage as storage_mod

CHAT = _support.TEST_CHAT_ID


def _names(db_path: str, kind: str) -> set:
    """Имена объектов схемы (table/index) из файла БД."""
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
            )
        }
    finally:
        conn.close()


class LearningStorageTestCase(unittest.TestCase):
    """add_learning / learning_between / learning_minutes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-learn-db-")
        self.db_path = str(Path(self._tmp.name) / "db.sqlite")
        self.storage = storage_mod.Storage(self.db_path)

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def add(self, title: str = "python", minutes: int = 30, kind: str = "study",
            day: str = "2026-11-10", hour: int = 12) -> int:
        return self.storage.add_learning(
            title, minutes, kind=kind, ts="%sT%02d:00:00+03:00" % (day, hour),
        )

    def all_rows(self) -> list[dict]:
        return self.storage.learning_between("0001-01-01", "9999-12-31")

    # --------------------------------------------------------------- схема
    def test_schema_table_and_index(self) -> None:
        self.assertIn("learning", _names(self.db_path, "table"))
        self.assertIn("idx_learning_date", _names(self.db_path, "index"))

    # ------------------------------------------------------------- запись
    def test_add_learning_roundtrip(self) -> None:
        record_id = self.add(title="python", minutes=30)
        self.assertIsInstance(record_id, int)
        self.assertGreater(record_id, 0)
        rows = self.storage.learning_between("2026-11-10", "2026-11-10")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], record_id)
        self.assertEqual(row["date"], "2026-11-10")
        self.assertEqual(row["ts"], "2026-11-10T12:00:00+03:00")
        self.assertEqual(row["title"], "python")
        self.assertEqual(row["minutes"], 30)
        self.assertEqual(row["kind"], "study")

    def test_minutes_bounds(self) -> None:
        self.add(minutes=1)
        self.add(minutes=1440, day="2026-11-11")
        self.assertEqual(
            sorted(row["minutes"] for row in self.all_rows()), [1, 1440]
        )
        for bad in (0, -1, 1441, 10000, "abc", "", None, True, "3,5", 30.9,
                    float("nan")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.storage.add_learning("python", bad)
        self.assertEqual(len(self.all_rows()), 2)

    def test_kind_default_and_custom(self) -> None:
        self.add(title="python")
        self.add(title="книга", kind="reading", day="2026-11-11")
        self.add(title="лекция", kind="", day="2026-11-12")
        kinds = {row["title"]: row["kind"] for row in self.all_rows()}
        self.assertEqual(kinds["python"], "study")
        self.assertEqual(kinds["книга"], "reading")
        self.assertEqual(kinds["лекция"], "study", "пустой kind → study")

    def test_empty_title_is_stored(self) -> None:
        record_id = self.add(title="")
        row = self.all_rows()[0]
        self.assertEqual(row["id"], record_id)
        self.assertEqual(row["title"], "")
        self.storage.add_learning(None, 15)
        self.assertEqual(len(self.all_rows()), 2)
        self.assertTrue(all(row["title"] == "" for row in self.all_rows()))

    def test_title_truncated(self) -> None:
        self.add(title="п" * 500)
        self.assertEqual(len(self.all_rows()[0]["title"]), 200)

    def test_date_from_ts_and_default(self) -> None:
        self.add(day="2026-10-31")
        today = datetime.now(self.storage.tz).date().isoformat()
        self.storage.add_learning("python", 10)
        self.storage.add_learning("python", 20, ts="кривой-ts")
        by_minutes = {row["minutes"]: row["date"] for row in self.all_rows()}
        self.assertEqual(by_minutes[30], "2026-10-31")
        self.assertEqual(by_minutes[10], today, "ts=None → сегодняшняя дата")
        self.assertEqual(by_minutes[20], today, "битый ts → сегодняшняя дата")

    # ----------------------------------------------------------- чтение
    def test_learning_between_order_and_range(self) -> None:
        self.add(minutes=30, day="2026-11-10")
        self.add(minutes=45, day="2026-11-12")
        self.add(minutes=60, day="2026-11-12")
        rows = self.storage.learning_between("2026-11-01", "2026-11-30")
        self.assertEqual([row["minutes"] for row in rows], [60, 45, 30],
                         "свежие сверху: дата, затем id по убыванию")
        self.assertEqual(self.storage.learning_between("2026-11-01", "2026-11-09"), [])
        self.assertEqual(self.storage.learning_between("2026-11-13", "2026-11-30"), [])
        self.assertEqual(len(self.storage.learning_between("2026-11-10", "2026-11-12")), 3,
                         "границы диапазона включительно")
        self.assertEqual(self.storage.learning_between("2026-11-12", "2026-11-10"), [],
                         "перевёрнутый диапазон — пусто")

    def test_learning_minutes_sum(self) -> None:
        self.add(minutes=30, day="2026-11-10")
        self.add(minutes=45, day="2026-11-12")
        self.add(minutes=60, day="2026-11-12")
        self.add(minutes=999, day="2026-10-31")  # другой месяц
        self.assertEqual(self.storage.learning_minutes("2026-11-01", "2026-11-30"), 135)
        self.assertEqual(self.storage.learning_minutes("2026-11-12", "2026-11-12"), 105)
        self.assertEqual(self.storage.learning_minutes("2026-12-01", "2026-12-31"), 0)
        self.assertIsInstance(
            self.storage.learning_minutes("2026-12-01", "2026-12-31"), int
        )

    # -------------------------------------------------------- миграции
    def test_old_db_opens_and_new_table_appears(self) -> None:
        legacy_path = str(Path(self._tmp.name) / "legacy.db")
        conn = sqlite3.connect(legacy_path)
        try:
            conn.executescript(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "chat_id INTEGER, direction TEXT, text TEXT, ts TEXT, slot TEXT);"
                "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);"
                "INSERT INTO messages(chat_id, direction, text, ts) "
                "VALUES (1, 'in', 'старое сообщение', '2026-11-01T10:00:00+03:00');"
                "INSERT INTO kv(key, value) VALUES ('onboarded', '1');"
            )
            conn.commit()
        finally:
            conn.close()

        legacy = storage_mod.Storage(legacy_path)
        try:
            self.assertIn("learning", _names(legacy_path, "table"))
            self.assertIn("money", _names(legacy_path, "table"))
            self.assertIn("idx_learning_date", _names(legacy_path, "index"))
            self.assertEqual(legacy.chat_history(10)[0]["text"], "старое сообщение")
            self.assertEqual(legacy.kv_get("onboarded"), "1")
            legacy.add_learning("python", 25, ts="2026-11-02T10:00:00+03:00")
            self.assertEqual(legacy.learning_minutes("2026-11-01", "2026-11-30"), 25)
        finally:
            legacy.close()

        reopened = storage_mod.Storage(legacy_path)
        try:
            self.assertEqual(
                reopened.learning_minutes("2026-11-01", "2026-11-30"), 25
            )
            self.assertEqual(reopened.chat_history(10)[0]["text"], "старое сообщение")
        finally:
            reopened.close()


class LearningCommandsTestCase(unittest.TestCase):
    """/learn через фейковый Telegram (без сети)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-learn-bot-")
        self.storage = storage_mod.Storage(str(Path(self._tmp.name) / "db.sqlite"))
        self.tg = _support.FakeTelegram()
        self.llm = _support.FakeLLM(available=False)
        self.vault = _support.FakeVault()
        self.dash = _support.FakeDashboard()
        self.cfg = _support.make_cfg(
            telegram__allowed_chat_id=CHAT,
            llm__enabled=False,
        )
        self.scheduler = scheduler_mod.Scheduler(
            self.cfg, self.storage, self.tg, self.llm, self.vault, self.dash,
            _support.quiet_logger(),
        )

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def send(self, text: str, hour: int = 12) -> str:
        self.scheduler.on_message(CHAT, text, _support.at(2026, 11, 16, hour, 0))
        return self.tg.last()

    def rows(self) -> list[dict]:
        return self.storage.learning_between("0001-01-01", "9999-12-31")

    # ------------------------------------------------------------- /learn
    def test_learn_writes_record(self) -> None:
        reply = self.send("/learn 30 python")
        self.assertEqual(reply, "Записал обучение: 30 мин — python.")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["minutes"], 30)
        self.assertEqual(rows[0]["title"], "python")
        self.assertEqual(rows[0]["kind"], "study")
        self.assertEqual(rows[0]["date"], "2026-11-16")

    def test_learn_multiword_title(self) -> None:
        self.send("/learn 45 английский по сериалу")
        self.assertEqual(self.rows()[0]["title"], "английский по сериалу")
        self.assertEqual(self.rows()[0]["minutes"], 45)

    def test_learn_without_title(self) -> None:
        reply = self.send("/learn 45")
        self.assertEqual(reply, "Записал обучение: 45 мин.")
        self.assertEqual(self.rows()[0]["title"], "")

    def test_learn_title_truncated_to_200(self) -> None:
        self.send("/learn 30 " + "т" * 400)
        self.assertEqual(len(self.rows()[0]["title"]), 200)

    def test_learn_bad_minutes_hints(self) -> None:
        for text in ("/learn 0 python", "/learn 1441 python", "/learn -10 python",
                     "/learn abc python", "/learn 3,5 python"):
            with self.subTest(text=text):
                reply = self.send(text)
                self.assertIn("Формат: /learn 30 python", reply)
        self.assertEqual(self.rows(), [], "мусор не пишется в БД")

    def test_learn_bounds_accepted(self) -> None:
        self.assertIn("Записал обучение: 1 мин", self.send("/learn 1 python"))
        self.assertIn("Записал обучение: 1440 мин", self.send("/learn 1440 python"))

    # ------------------------------------------------------- сводка /learn
    def test_learn_summary_empty(self) -> None:
        self.assertEqual(
            self.send("/learn"),
            "Пока нет записей об обучении за 7 дней. Начни с /learn 30 python.",
        )

    def test_learn_summary_seven_days(self) -> None:
        self.send("/learn 30 python")
        self.send("/learn 45 английский")
        # 2026-11-09 уже вне окна 10.11–16.11 — в сводку не попадает.
        self.storage.add_learning("старое", 999, ts="2026-11-09T23:00:00+03:00")
        text = self.send("/learn")
        self.assertIn("Обучение за 7 дней (10.11 — 16.11)", text)
        self.assertIn("Минут: 75", text)
        self.assertIn("1 ч 15 мин", text)
        self.assertIn("сессий: 2", text)

    def test_learn_summary_days_plural(self) -> None:
        self.send("/learn 20 python")
        self.assertIn("сессий: 1", self.send("/learn"))
        self.send("/learn 20 python")
        self.assertIn("сессий: 2", self.send("/learn"))
        self.send("/learn 20 python")
        self.assertIn("сессий: 3", self.send("/learn"))

    # ---------------------------------------------------------- алиасы
    def test_learn_russian_alias(self) -> None:
        self.assertIn("Записал обучение: 30 мин — python",
                      self.send("/обучение 30 python"))
        self.assertIn("Обучение за 7 дней", self.send("обучение"))

    def test_help_lists_learn(self) -> None:
        text = self.scheduler.help_text()
        self.assertIn("/learn", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
