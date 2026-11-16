"""Тесты финансового домена v0.2: storage.money_* и команды /spend, /income, /fin.

Ни сети, ни записи в Obsidian: Storage живёт во временном файле, Telegram,
LLM, Vault и dashboard — фейки из ``tests/_support.py`` (как в
``test_scheduler.py``). Приватный текст (заметки) в тестах только проверяется,
в лог ничего не уходит.
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


class MoneyStorageTestCase(unittest.TestCase):
    """add_money / money_between / money_totals / spend_by_category."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-money-db-")
        self.db_path = str(Path(self._tmp.name) / "db.sqlite")
        self.storage = storage_mod.Storage(self.db_path)

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def add(self, kind: str = "spend", amount=500, category: str = "кафе",
            note: str = "", day: str = "2026-11-10", hour: int = 12) -> int:
        return self.storage.add_money(
            kind, amount, category=category, note=note,
            ts="%sT%02d:00:00+03:00" % (day, hour),
        )

    # --------------------------------------------------------------- схема
    def test_schema_tables_and_indexes(self) -> None:
        tables = _names(self.db_path, "table")
        self.assertIn("money", tables)
        self.assertIn("learning", tables)
        indexes = _names(self.db_path, "index")
        self.assertIn("idx_money_date", indexes)
        self.assertIn("idx_learning_date", indexes)

    # ------------------------------------------------------------- запись
    def test_add_money_roundtrip(self) -> None:
        record_id = self.add(note="обед")
        self.assertIsInstance(record_id, int)
        self.assertGreater(record_id, 0)
        rows = self.storage.money_between("2026-11-10", "2026-11-10")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], record_id)
        self.assertEqual(row["date"], "2026-11-10")
        self.assertEqual(row["ts"], "2026-11-10T12:00:00+03:00")
        self.assertEqual(row["kind"], "spend")
        self.assertEqual(row["amount"], 500.0)
        self.assertEqual(row["currency"], "RUB")
        self.assertEqual(row["category"], "кафе")
        self.assertEqual(row["note"], "обед")

    def test_amount_accepts_locale_strings(self) -> None:
        self.add(amount="500,50", category="кафе")
        self.add(amount="1 000", category="продукты")
        self.add(amount="1.000", category="акции")
        amounts = {row["category"]: row["amount"]
                   for row in self.storage.money_between("2026-11-01", "2026-11-30")}
        self.assertEqual(amounts["кафе"], 500.5, "запятая — десятичный разделитель")
        self.assertEqual(amounts["продукты"], 1000.0, "пробел — разделитель тысяч")
        self.assertEqual(amounts["акции"], 1.0, "точка — десятичный разделитель")

    def test_amount_rounds_to_kopecks(self) -> None:
        self.add(amount=500.555)
        self.add(amount=10.1)
        self.add(amount=20.2)
        rows = self.storage.money_between("2026-11-01", "2026-11-30")
        for row in rows:
            self.assertEqual(row["amount"], round(row["amount"], 2),
                             "сумма хранится с точностью до копеек")
        totals = self.storage.money_totals("2026-11-01", "2026-11-30")
        self.assertAlmostEqual(totals["spend"], round(500.555, 2) + 10.1 + 20.2,
                               places=2)

    def test_amount_bounds(self) -> None:
        self.add(amount=1e9, category="максимум")
        self.assertEqual(
            self.storage.money_between("2026-11-01", "2026-11-30")[0]["amount"], 1e9
        )
        for bad in (0, -5, -0.01, 1e9 + 1, 1e12, "abc", "", None, True,
                    float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.storage.add_money("spend", bad)
        self.assertEqual(len(self.storage.money_between("0001-01-01", "9999-12-31")), 1)

    def test_kind_validation(self) -> None:
        self.add(kind="SPEND")
        self.add(kind="Income", amount=1000)
        kinds = [row["kind"] for row in
                 self.storage.money_between("2026-11-01", "2026-11-30")]
        self.assertEqual(kinds, ["income", "spend"], "регистр нормализуется")
        for bad in ("transfer", "", "  ", None, "spend income"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.storage.add_money(bad, 100)

    def test_empty_fields_do_not_break(self) -> None:
        record_id = self.storage.add_money("spend", 100)
        row = self.storage.money_between("0001-01-01", "9999-12-31")[0]
        self.assertEqual(row["id"], record_id)
        self.assertEqual(row["category"], "прочее")
        self.assertEqual(row["note"], "")
        self.assertEqual(row["currency"], "RUB")
        # Пустые/пробельные категория и заметка тоже допустимы.
        self.add(category="", note="   ")
        self.storage.add_money("spend", 5, category=None, note=None)
        rows = self.storage.money_between("0001-01-01", "9999-12-31")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["category"] == "прочее" for row in rows))

    def test_note_and_category_truncated(self) -> None:
        self.add(category="к" * 200, note="з" * 500)
        row = self.storage.money_between("2026-11-10", "2026-11-10")[0]
        self.assertEqual(len(row["category"]), 80)
        self.assertEqual(len(row["note"]), 200)

    def test_date_from_ts_and_default(self) -> None:
        self.add(day="2026-10-31")
        today = datetime.now(self.storage.tz).date().isoformat()
        self.storage.add_money("spend", 10)
        self.storage.add_money("spend", 20, ts="кривой-ts")
        rows = self.storage.money_between("0001-01-01", "9999-12-31")
        by_amount = {row["amount"]: row["date"] for row in rows}
        self.assertEqual(by_amount[500.0], "2026-10-31")
        self.assertEqual(by_amount[10.0], today, "ts=None → сегодняшняя дата")
        self.assertEqual(by_amount[20.0], today, "битый ts → сегодняшняя дата")

    # ----------------------------------------------------------- чтение
    def test_money_between_order_and_range(self) -> None:
        self.add(amount=100, day="2026-11-10")
        self.add(amount=200, day="2026-11-12")
        self.add(amount=300, day="2026-11-12")
        rows = self.storage.money_between("2026-11-01", "2026-11-30")
        self.assertEqual([row["amount"] for row in rows], [300.0, 200.0, 100.0],
                         "свежие сверху: дата, затем id по убыванию")
        self.assertEqual([len(self.storage.money_between("2026-11-01", "2026-11-09")),
                          len(self.storage.money_between("2026-11-13", "2026-11-30"))],
                         [0, 0], "пустые диапазоны")
        self.assertEqual(len(self.storage.money_between("2026-11-10", "2026-11-12")), 3,
                         "границы диапазона включительно")
        self.assertEqual(self.storage.money_between("2026-11-12", "2026-11-10"), [],
                         "перевёрнутый диапазон — пусто")

    def test_money_totals(self) -> None:
        self.add(amount=500)
        self.add(amount=250.5, day="2026-11-11")
        self.add(kind="income", amount=100000, day="2026-11-12")
        self.add(amount=99, day="2026-10-31")  # другой месяц
        totals = self.storage.money_totals("2026-11-01", "2026-11-30")
        self.assertEqual(totals, {"spend": 750.5, "income": 100000.0})
        empty = self.storage.money_totals("2026-12-01", "2026-12-31")
        self.assertEqual(empty, {"spend": 0.0, "income": 0.0})
        self.assertIsInstance(empty["spend"], float)

    def test_spend_by_category(self) -> None:
        self.add(amount=300, category="кафе")
        self.add(amount=300, category="кафе", day="2026-11-11")
        self.add(amount=1000, category="продукты", day="2026-11-12")
        self.add(amount=100, category="книги", day="2026-11-13")
        self.add(kind="income", amount=5000, category="зарплата", day="2026-11-14")
        by_category = self.storage.spend_by_category("2026-11-01", "2026-11-30")
        self.assertEqual(by_category, [
            {"category": "продукты", "amount": 1000.0},
            {"category": "кафе", "amount": 600.0},
            {"category": "книги", "amount": 100.0},
        ], "по убыванию суммы, доходы не считаются")
        self.assertEqual(self.storage.spend_by_category("2026-12-01", "2026-12-31"), [])

    # -------------------------------------------------------- миграции
    def test_old_db_opens_and_new_tables_appear(self) -> None:
        legacy_path = str(Path(self._tmp.name) / "legacy.db")
        conn = sqlite3.connect(legacy_path)
        try:
            conn.executescript(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "chat_id INTEGER, direction TEXT, text TEXT, ts TEXT, slot TEXT);"
                "CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "date TEXT, ts TEXT, slot TEXT, category TEXT, title TEXT, text TEXT, "
                "raw TEXT, metrics TEXT, written INTEGER DEFAULT 0);"
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
            tables = _names(legacy_path, "table")
            self.assertIn("money", tables)
            self.assertIn("learning", tables)
            self.assertIn("idx_money_date", _names(legacy_path, "index"))
            self.assertEqual(legacy.chat_history(10)[0]["text"], "старое сообщение")
            self.assertEqual(legacy.kv_get("onboarded"), "1")
            legacy.add_money("spend", 100, "кафе", ts="2026-11-02T10:00:00+03:00")
            self.assertEqual(
                legacy.money_totals("2026-11-01", "2026-11-30")["spend"], 100.0
            )
        finally:
            legacy.close()

        reopened = storage_mod.Storage(legacy_path)
        try:
            self.assertEqual(len(reopened.money_between("2026-11-01", "2026-11-30")), 1)
            self.assertEqual(reopened.chat_history(10)[0]["text"], "старое сообщение")
        finally:
            reopened.close()


class MoneyCommandsTestCase(unittest.TestCase):
    """/spend, /income, /fin через фейковый Telegram (без сети)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-money-bot-")
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
        return self.storage.money_between("0001-01-01", "9999-12-31")

    # ------------------------------------------------------------- /spend
    def test_spend_writes_record(self) -> None:
        reply = self.send("/spend 500 кафе обед")
        self.assertEqual(reply, "Записал расход: 500 ₽ — кафе (обед).")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "spend")
        self.assertEqual(rows[0]["amount"], 500.0)
        self.assertEqual(rows[0]["category"], "кафе")
        self.assertEqual(rows[0]["note"], "обед")
        self.assertEqual(rows[0]["date"], "2026-11-16")

    def test_spend_without_args_hints(self) -> None:
        reply = self.send("/spend")
        self.assertIn("/spend 500 кафе", reply)
        self.assertEqual(self.rows(), [])

    def test_spend_bad_amount_hints(self) -> None:
        for text in ("/spend abc кафе", "/spend ,,, кафе", "/spend 1e5 кафе",
                     "/spend тыс кафе"):
            with self.subTest(text=text):
                reply = self.send(text)
                self.assertIn("Не понял сумму", reply)
                self.assertIn("/spend 500", reply)
        self.assertEqual(self.rows(), [], "мусор не пишется в БД")

    def test_spend_zero_negative_rejected(self) -> None:
        for text in ("/spend 0 кафе", "/spend -5 кафе", "/spend -0,5 кафе"):
            with self.subTest(text=text):
                reply = self.send(text)
                self.assertIn("больше нуля", reply)
        self.assertEqual(self.rows(), [])

    def test_spend_too_large_rejected(self) -> None:
        reply = self.send("/spend 1000000001 кафе")
        self.assertIn("Слишком большая сумма", reply)
        self.assertIn("1 000 000 000", reply)
        self.assertEqual(self.rows(), [])
        # Ровно граница — допустима.
        self.assertIn("Записал расход", self.send("/spend 1000000000 кафе"))

    def test_amount_with_comma_and_spaces(self) -> None:
        self.assertIn("500,5 ₽", self.send("/spend 500,50 кафе"))
        self.assertIn("1 000 ₽", self.send("/spend 1 000 продукты"))
        self.assertIn("1 ₽", self.send("/spend 1.000 акции"),
                      "точка — десятичный разделитель")
        amounts = {row["category"]: row["amount"] for row in self.rows()}
        self.assertEqual(amounts, {"кафе": 500.5, "продукты": 1000.0, "акции": 1.0})

    def test_category_first_word_note_is_rest(self) -> None:
        reply = self.send("/spend 100 кафе обед с другом")
        self.assertEqual(reply, "Записал расход: 100 ₽ — кафе (обед с другом).")
        row = self.rows()[0]
        self.assertEqual(row["category"], "кафе")
        self.assertEqual(row["note"], "обед с другом")

    def test_note_truncated_to_200(self) -> None:
        self.send("/spend 100 кафе " + "о" * 400)
        self.assertEqual(len(self.rows()[0]["note"]), 200)

    # ------------------------------------------------------------ /income
    def test_income_writes_record(self) -> None:
        reply = self.send("/income 100000 зарплата")
        self.assertEqual(reply, "Записал доход: 100 000 ₽ — зарплата.")
        rows = self.rows()
        self.assertEqual(rows[0]["kind"], "income")
        self.assertEqual(rows[0]["amount"], 100000.0)
        self.assertEqual(rows[0]["category"], "зарплата")

    def test_income_without_args_hints(self) -> None:
        reply = self.send("/income")
        self.assertIn("/income 500", reply)
        self.assertEqual(self.rows(), [])

    def test_russian_aliases(self) -> None:
        self.assertIn("Записал расход", self.send("/расход 100 кафе"))
        self.assertIn("Записал доход", self.send("/доход 5000 зарплата"))
        self.assertIn("Формат: /spend", self.send("трата", hour=13),
                      "«трата» без аргументов — подсказка формата")
        self.assertIn("Финансы", self.send("финансы"))
        self.assertEqual(len(self.rows()), 2)

    # --------------------------------------------------------------- /fin
    def test_fin_empty_month(self) -> None:
        self.assertEqual(
            self.send("/fin"),
            "Пока нет записей за этот месяц. Начни с /spend 500 кафе.",
        )

    def test_fin_month_summary(self) -> None:
        self.send("/spend 300 кафе")
        self.send("/spend 1000 продукты")
        self.send("/spend 500 транспорт")
        self.send("/spend 100 книги")
        self.send("/income 100000 зарплата")
        text = self.send("/fin")
        self.assertIn("Финансы за ноябрь 2026", text)
        self.assertIn("Расходы: 1 900 ₽", text)
        self.assertIn("Доходы: 100 000 ₽", text)
        self.assertIn("Баланс: 98 100 ₽", text)
        self.assertIn("продукты", text)
        self.assertIn("транспорт", text)
        self.assertIn("кафе", text)
        self.assertNotIn("книги", text, "в топ попадают только 3 категории")

    def test_fin_ignores_other_months(self) -> None:
        self.storage.add_money("spend", 999, "прошлый месяц",
                               ts="2026-10-31T23:00:00+03:00")
        self.assertEqual(
            self.send("/fin"),
            "Пока нет записей за этот месяц. Начни с /spend 500 кафе.",
        )

    def test_fin_negative_balance(self) -> None:
        self.send("/spend 1000 кафе")
        self.send("/income 200 подработка")
        text = self.send("/fin")
        self.assertIn("Баланс: -800 ₽", text)

    def test_fin_with_only_income(self) -> None:
        self.send("/income 1000 подарок")
        text = self.send("/fin")
        self.assertIn("Доходы: 1 000 ₽", text)
        self.assertIn("Расходов пока нет", text)

    # -------------------------------------------------------------- /help
    def test_help_lists_money_commands(self) -> None:
        text = self.scheduler.help_text()
        for command in ("/spend", "/income", "/fin"):
            self.assertIn(command, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
