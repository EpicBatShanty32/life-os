"""Тесты src/obsidian.py: дневник, разделы, идемпотентность, цели, зеркало."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support
from src import obsidian, storage as storage_mod

DATE = "2026-11-16"


class VaultTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-vault-")
        self.vault_root = Path(self._tmp.name) / "vault"
        self.vault_root.mkdir(parents=True)
        self.cfg = _support.make_cfg(obsidian__vault_path=str(self.vault_root))
        self.storage = storage_mod.Storage(str(Path(self._tmp.name) / "test.db"))
        self.vault = obsidian.Vault(self.cfg, storage=self.storage)

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------ помощники
    def make_template(self, text: str = "## Главное\n{{date:DD.MM.YYYY}} | {{date}}\n") -> Path:
        path = self.vault_root / "Шаблоны" / "Шаблон - День.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def daily_text(self, date: str = DATE) -> str:
        path = self.vault_root / "01 Daily" / ("%s.md" % date)
        return path.read_text(encoding="utf-8")

    def entry(self, entry_id: int, category: str, text: str,
              ts: str = "2026-11-16T14:02:11+03:00") -> dict:
        return {
            "id": entry_id,
            "date": DATE,
            "ts": ts,
            "slot": "midday",
            "category": category,
            "title": "запись",
            "text": text,
            "raw": text,
            "metrics": {},
        }

    # ------------------------------------------------------- дневная заметка
    def test_ensure_daily_from_template(self) -> None:
        self.make_template("## Главное\n{{date:DD.MM.YYYY}} | {{date}} | {{title}}\n")
        path = self.vault.ensure_daily(DATE)
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "2026-11-16.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("16.11.2026 | 2026-11-16 | 2026-11-16", text)
        self.assertNotIn("{{", text)
        # повторный вызов не перезаписывает и не ломается
        self.assertEqual(self.vault.ensure_daily(DATE), path)
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_ensure_daily_does_not_touch_existing_user_file(self) -> None:
        path = self.vault.daily_path(DATE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# мой день\nне трогай\n", encoding="utf-8")
        self.vault.ensure_daily(DATE)
        self.assertEqual(path.read_text(encoding="utf-8"), "# мой день\nне трогай\n")

    def test_ensure_daily_fallback_skeleton_without_template(self) -> None:
        path = self.vault.ensure_daily(DATE)
        text = path.read_text(encoding="utf-8")
        self.assertIn("## Главное", text)
        self.assertIn("16.11.2026", text)

    def test_ensure_daily_invalid_date(self) -> None:
        with self.assertRaises(obsidian.ProgrammerError):
            self.vault.ensure_daily("16.11.2026")
        with self.assertRaises(obsidian.ProgrammerError):
            self.vault.ensure_daily("../evil")
        with self.assertRaises(TypeError):
            self.vault.ensure_daily(20261116)

    # -------------------------------------------------------------- записи
    def test_write_entry_all_categories(self) -> None:
        self.make_template()
        categories = ["food", "training", "tasks", "goals",
                      "mood", "journal", "sleep", "water", "other"]
        for index, category in enumerate(categories, start=1):
            added = self.vault.write_entry(
                self.entry(index, category, "текст %s" % category)
            )
            self.assertTrue(added, "категория %s должна записаться" % category)
        daily = self.daily_text()
        for category in categories:
            heading = "## " + obsidian.CATEGORY_SECTIONS[category]
            self.assertIn(heading, daily)
        for index in range(1, len(categories) + 1):
            self.assertIn("<!-- lifeos:entry:%d -->" % index, daily)
        self.assertEqual(daily.count("<!-- lifeos:entry:"), len(categories))
        # формат пункта: время — текст — маркер
        self.assertIn("14:02 — текст food <!-- lifeos:entry:1 -->", daily)

    def test_write_entry_is_idempotent_by_marker(self) -> None:
        self.make_template()
        entry = self.entry(42, "food", "обед")
        self.assertTrue(self.vault.write_entry(entry))
        self.assertFalse(self.vault.write_entry(entry), "дубль должен вернуть False")
        daily = self.daily_text()
        self.assertEqual(daily.count("<!-- lifeos:entry:42 -->"), 1)
        self.assertEqual(daily.count("обед"), 1)
        # запись без id: маркер детерминированный, дубля тоже нет
        no_id = dict(entry)
        no_id.pop("id")
        self.assertTrue(self.vault.write_entry(no_id))
        self.assertFalse(self.vault.write_entry(no_id))
        self.assertIn("<!-- lifeos:entry:noid-", self.daily_text())

    def test_write_entry_preserves_user_text(self) -> None:
        path = self.vault.daily_path(DATE)
        path.parent.mkdir(parents=True, exist_ok=True)
        user_content = (
            "# Мой день\n"
            "\n"
            "Утренняя мысль — не удалять.\n"
            "\n"
            "## Мысли (агент)\n"
            "- моя личная запись\n"
            "\n"
            "## Хвост\n"
            "чужой текст после раздела\n"
            "\n"
            "## Мысли\n"
            "пользовательский раздел\n"
        )
        path.write_text(user_content, encoding="utf-8")
        self.assertTrue(self.vault.write_entry(self.entry(7, "mood", "настроение 4")))
        daily = self.daily_text()
        for line in ("# Мой день", "Утренняя мысль — не удалять.",
                     "- моя личная запись", "## Хвост",
                     "чужой текст после раздела", "## Мысли",
                     "пользовательский раздел"):
            self.assertIn(line, daily, "пользовательский текст потерялся: %s" % line)
        # новый пункт — в разделе «Мысли (агент)», перед «## Хвост»
        section_pos = daily.index("## Мысли (агент)")
        bullet_pos = daily.index("настроение 4")
        tail_pos = daily.index("## Хвост")
        self.assertLess(section_pos, bullet_pos)
        self.assertLess(bullet_pos, tail_pos)
        # split('\n') затем join — исходные строки не меняются
        self.assertIn("- моя личная запись", daily)
        self.assertLess(daily.index("- моя личная запись"), bullet_pos,
                        "новый пункт должен идти после пользовательских в своём разделе")

    def test_write_entry_adds_new_section_at_end(self) -> None:
        self.make_template("## Главное\n")
        self.vault.ensure_daily(DATE)
        self.vault.write_entry(self.entry(1, "food", "завтрак"))
        daily = self.daily_text()
        self.assertTrue(daily.rstrip().endswith("<!-- lifeos:entry:1 -->"))
        self.assertIn("## Еда (агент)", daily)

    def test_write_entry_errors(self) -> None:
        with self.assertRaises(TypeError):
            self.vault.write_entry("не dict")  # type: ignore[arg-type]

    def test_write_entry_disabled_vault(self) -> None:
        cfg = _support.make_cfg(
            obsidian__vault_path=str(self.vault_root),
            obsidian__enabled=False,
        )
        vault = obsidian.Vault(cfg, storage=self.storage)
        self.assertFalse(vault.write_entry(self.entry(1, "food", "x")))
        self.assertFalse((self.vault_root / "01 Daily" / (DATE + ".md")).exists())

    # ---------------------------------------------------------------- цели
    def test_rewrite_goals_creates_table_and_keeps_text(self) -> None:
        goals_path = self.vault_root / "Проекты" / "Life OS" / "Цели.md"
        goals_path.parent.mkdir(parents=True, exist_ok=True)
        goals_path.write_text("# Мои цели\n\nВажная заметка пользователя.\n",
                              encoding="utf-8")
        self.storage.upsert_goal({"title": "Пробежать 10 км", "target": "10",
                                  "current": "3", "deadline": "2027-06-01"})
        self.storage.upsert_goal({"title": "Прочитать 12 книг", "target": "12",
                                  "current": "4", "status": "paused"})
        self.assertTrue(self.vault.rewrite_goals())
        text = goals_path.read_text(encoding="utf-8")
        self.assertIn(obsidian.GOALS_START, text)
        self.assertIn(obsidian.GOALS_END, text)
        self.assertIn("| Цель | Прогресс | Срок | Статус |", text)
        self.assertIn("[[Пробежать 10 км]]", text)
        self.assertIn("30%", text)
        self.assertIn("2027-06-01", text)
        self.assertIn("[[Прочитать 12 книг]]", text)
        self.assertIn("пауза", text)
        self.assertIn("Важная заметка пользователя.", text, "текст вне маркеров сохранён")
        # повторный запуск идемпотентен
        self.assertTrue(self.vault.rewrite_goals())
        self.assertEqual(goals_path.read_text(encoding="utf-8"), text)
        # порча таблицы между маркерами — перегенерируется
        broken = text.replace("30%", "мусор")
        goals_path.write_text(broken, encoding="utf-8")
        self.assertTrue(self.vault.rewrite_goals())
        fixed = goals_path.read_text(encoding="utf-8")
        self.assertIn("30%", fixed)
        self.assertNotIn("мусор", fixed)
        self.assertIn("Важная заметка пользователя.", fixed)

    def test_weight_goal_progress_is_0_50_100(self) -> None:
        """P0-1: «92 → 78» — 0%/50%/100%, а не 117,9% от current/target."""
        goals_path = self.vault_root / "Проекты" / "Life OS" / "Цели.md"
        goal_id = self.storage.upsert_goal({
            "title": "Сбросить вес", "target": "78", "current": "92",
            "start": "92", "direction": "down",
        })
        for current, expected in (("92", "0%"), ("85", "50%"), ("78", "100%")):
            self.storage.upsert_goal({"id": goal_id, "current": current})
            self.assertTrue(self.vault.rewrite_goals())
            text = goals_path.read_text(encoding="utf-8")
            line = next(row for row in text.splitlines()
                        if "[[Сбросить вес]]" in row)
            self.assertIn("| %s |" % expected, line,
                          "current=%s: %s" % (current, line))
            self.assertNotIn("117", line)

    def test_rewrite_goals_creates_file(self) -> None:
        self.storage.upsert_goal({"title": "Цель 1"})
        self.assertTrue(self.vault.rewrite_goals())
        path = self.vault_root / "Проекты" / "Life OS" / "Цели.md"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("[[Цель 1]]", text)
        self.assertIn("—", text)  # прогресс/срок по умолчанию

    def test_rewrite_goals_without_storage(self) -> None:
        vault = obsidian.Vault(self.cfg, storage=None)
        self.assertFalse(vault.rewrite_goals())

    # -------------------------------------------------------------- зеркало
    def test_write_mirror(self) -> None:
        ids = [self.storage.add_entry({
            "date": DATE, "ts": "2026-11-16T09:00:00+03:00",
            "category": "food", "title": "завтрак", "text": "овсянка",
            "metrics": {"calories": 350},
        }) for _ in range(2)]
        self.storage.upsert_goal({"title": "Цель", "target": "10"})
        entries = self.storage.entries("0001-01-01", "9999-12-31")
        self.vault.write_mirror(entries)
        folder = self.vault_root / "Life OS" / "data"
        lines = (folder / "entries.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            parsed = json.loads(line)
            self.assertIn("category", parsed)
            self.assertIsInstance(parsed["metrics"], dict)
        goals = json.loads((folder / "goals.json").read_text(encoding="utf-8"))
        self.assertIsInstance(goals, list)
        self.assertEqual(goals[0]["title"], "Цель")
        # полная перезапись: меньше записей → меньше строк
        self.vault.write_mirror(entries[:1])
        lines = (folder / "entries.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(ids[0] > 0, True)

    def test_write_mirror_disabled_by_config(self) -> None:
        cfg = _support.make_cfg(
            obsidian__vault_path=str(self.vault_root),
            obsidian__write_mirror=False,
        )
        vault = obsidian.Vault(cfg, storage=self.storage)
        entries = self.storage.entries("0001-01-01", "9999-12-31")
        vault.write_mirror(entries)
        self.assertFalse((self.vault_root / "Life OS" / "data" / "entries.jsonl").exists())

    # --------------------------------------------------------- синхронизация
    def test_sync_pending_writes_and_marks(self) -> None:
        self.make_template()
        for index, category in enumerate(("food", "training"), start=1):
            self.storage.add_entry({
                "date": DATE, "ts": "2026-11-16T1%d:00:00+03:00" % index,
                "slot": "midday", "category": category,
                "title": "t%d" % index, "text": "текст %d" % index,
                "raw": "raw", "metrics": {},
            })
        self.assertEqual(len(self.storage.entries_unwritten()), 2)
        written = self.vault.sync_pending()
        self.assertEqual(written, 2)
        self.assertEqual(self.storage.entries_unwritten(), [])
        daily = self.daily_text()
        self.assertIn("<!-- lifeos:entry:", daily)
        self.assertEqual(daily.count("<!-- lifeos:entry:"), 2)
        # повторный вызов ничего не делает
        self.assertEqual(self.vault.sync_pending(), 0)
        self.assertEqual(self.daily_text(), daily)

    def test_sync_pending_unwritable_vault_keeps_flags(self) -> None:
        blocked = Path(self._tmp.name) / "not-a-dir"
        blocked.write_text("я файл, а не папка", encoding="utf-8")
        cfg = _support.make_cfg(obsidian__vault_path=str(blocked))
        vault = obsidian.Vault(cfg, storage=self.storage)
        self.storage.add_entry({"date": DATE, "ts": "2026-11-16T10:00:00+03:00",
                                "category": "food", "text": "x"})
        self.assertEqual(vault.sync_pending(), 0)
        self.assertEqual(len(self.storage.entries_unwritten()), 1,
                         "незаписанная запись остаётся для повтора")

    def test_disabled_vault_sync_returns_zero(self) -> None:
        cfg = _support.make_cfg(obsidian__enabled=False,
                                obsidian__vault_path=str(self.vault_root))
        vault = obsidian.Vault(cfg, storage=self.storage)
        self.assertEqual(vault.sync_pending(), 0)
        self.assertFalse(vault.rewrite_goals())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class VaultCyrillicPathTestCase(unittest.TestCase):
    """Путь vault как у пользователя: кириллица и пробелы."""

    def test_cyrillic_and_spaces_in_vault_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lifeos-vault-cyr-") as tmp:
            vault_root = Path(tmp) / "чисто лайфик тест" / "чисто лайфик"
            template = vault_root / "Шаблоны" / "Шаблон - День.md"
            template.parent.mkdir(parents=True)
            template.write_text("## Главное\n{{date}}\n", encoding="utf-8")
            cfg = _support.make_cfg(obsidian__vault_path=str(vault_root))
            storage = storage_mod.Storage(str(Path(tmp) / "база данных.sqlite"))
            try:
                vault = obsidian.Vault(cfg, storage=storage)
                path = vault.ensure_daily("2026-11-16")
                self.assertTrue(path.is_file())
                self.assertIn("чисто лайфик тест", str(path))
                entry_id = storage.add_entry({
                    "date": "2026-11-16", "ts": "2026-11-16T12:00:00+03:00",
                    "slot": "midday", "category": "food",
                    "title": "обед", "text": "рис", "metrics": {},
                })
                entry = storage.entries("2026-11-16", "2026-11-16")[0]
                entry["id"] = entry_id
                self.assertTrue(vault.write_entry(entry))
                self.assertIn("<!-- lifeos:entry:%d -->" % entry_id,
                              path.read_text(encoding="utf-8"))
            finally:
                storage.close()
