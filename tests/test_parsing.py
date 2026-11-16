"""Тесты src/parsing.py: fallback без LLM, heuristic_metrics, странный ввод."""

from __future__ import annotations

import unittest

import _support
from src import parsing

NOW_ISO = "2026-11-16T14:02:11+03:00"


class HeuristicMetricsTestCase(unittest.TestCase):
    def test_water(self) -> None:
        self.assertEqual(parsing.heuristic_metrics("Выпил 2000 мл воды")["water_ml"], 2000)
        self.assertEqual(parsing.heuristic_metrics("2 литра воды")["water_ml"], 2000)
        self.assertEqual(parsing.heuristic_metrics("500 мл + 300 мл")["water_ml"], 800)

    def test_sleep_and_training_hours_disambiguated(self) -> None:
        self.assertEqual(parsing.heuristic_metrics("Спал 8 часов")["sleep_h"], 8)
        exercise = parsing.heuristic_metrics("Тренировка 1.5 часа, силовая")
        self.assertEqual(exercise["duration_min"], 90)
        self.assertNotIn("sleep_h", exercise)
        self.assertEqual(parsing.heuristic_metrics("45 минут бега")["duration_min"], 45)

    def test_food_metrics(self) -> None:
        metrics = parsing.heuristic_metrics("Съел 600 ккал, 40 г белка")
        self.assertEqual(metrics["calories"], 600)
        self.assertEqual(metrics["protein_g"], 40)

    def test_weight_and_distance(self) -> None:
        self.assertEqual(parsing.heuristic_metrics("Вешу 80 кг")["weight_kg"], 80)
        # вес штанги — это не вес тела
        self.assertNotIn("weight_kg", parsing.heuristic_metrics("Жим лёжа 80 кг"))
        metrics = parsing.heuristic_metrics("Пробежал 3 км за 45 мин")
        self.assertEqual(metrics["distance_km"], 3)
        self.assertEqual(metrics["duration_min"], 45)

    def test_scales(self) -> None:
        metrics = parsing.heuristic_metrics("настроение 4 из 5, энергия 3, фокус 5")
        self.assertEqual(metrics["mood"], 4)
        self.assertEqual(metrics["energy"], 3)
        self.assertEqual(metrics["focus"], 5)

    def test_weird_inputs_never_raise(self) -> None:
        for value in (None, "", "   ", "abc", 123, 4.5, [], {}, "😀🎉", "0 мл",
                      "NaN мл", "-5 кг", "x" * 25000):
            with self.subTest(value=repr(value)[:40]):
                result = parsing.heuristic_metrics(value)
                self.assertIsInstance(result, dict)
                for key, item in result.items():
                    self.assertIsInstance(key, str)
                    self.assertNotIsInstance(item, bool)

    def test_no_numbers_no_metrics(self) -> None:
        self.assertEqual(parsing.heuristic_metrics("сегодня был хороший день"), {})


class ParseAnswerFallbackTestCase(unittest.TestCase):
    def test_midday_food_without_llm(self) -> None:
        entry = parsing.parse_answer(
            None, "midday", "Что ел?", "Съел рис с курицей и выпил 500 мл воды",
            NOW_ISO,
        )
        self.assertEqual(entry["category"], "food")
        self.assertIn("рис", entry["text"])
        self.assertEqual(entry["date"], "2026-11-16")
        self.assertEqual(entry["ts"], NOW_ISO)
        self.assertEqual(entry["slot"], "midday")
        self.assertEqual(entry["metrics"], {"water_ml": 500},
                         "fallback выжимает числа эвристикой, чтобы дашборд жил без LLM")
        self.assertLessEqual(len(entry["title"]), 80)
        self.assertEqual(entry["raw"], "Съел рис с курицей и выпил 500 мл воды")

    def test_morning_plan_is_tasks(self) -> None:
        entry = parsing.parse_answer(
            None, "morning", "Какой план?",
            "План: написать отчёт, провести встречу, сходить в зал", NOW_ISO,
        )
        self.assertEqual(entry["category"], "tasks")
        self.assertLessEqual(len(entry["title"]), 80)

    def test_slot_fallback_for_vague_answer(self) -> None:
        entry = parsing.parse_answer(None, "evening", "Итоги?", "нормально", NOW_ISO)
        self.assertIn(entry["category"], parsing.CATEGORIES)
        self.assertTrue(entry["title"])
        self.assertTrue(entry["text"])

    def test_llm_unavailable_object_falls_back(self) -> None:
        llm = _support.FakeLLM(available=False,
                               json_reply={"category": "food", "title": "не должен использоваться"})
        entry = parsing.parse_answer(llm, "midday", "Что ел?", "омлет", NOW_ISO)
        self.assertNotEqual(entry["title"], "не должен использоваться")
        self.assertEqual(llm.json_calls, 0)

    def test_llm_raising_falls_back(self) -> None:
        llm = _support.FakeLLM(available=True, raise_on_chat=True)
        entry = parsing.parse_answer(llm, "midday", "Что ел?", "омлет и тост", NOW_ISO)
        self.assertEqual(entry["category"], "food")
        self.assertTrue(entry["text"])

    def test_llm_garbage_falls_back(self) -> None:
        llm = _support.FakeLLM(available=True, json_reply=None)
        entry = parsing.parse_answer(llm, "morning", "План?", "написать отчёт", NOW_ISO)
        self.assertIn(entry["category"], parsing.CATEGORIES)
        self.assertLessEqual(len(entry["title"]), 80)

    def test_weird_answers_never_raise(self) -> None:
        for answer in (None, "", 123, {"a": 1}, "x" * 5000):
            for slot in ("morning", "midday", "evening", "free", "", "unknown"):
                with self.subTest(answer=repr(answer)[:20], slot=slot):
                    entry = parsing.parse_answer(None, slot, "вопрос", answer, NOW_ISO)
                    self.assertIsInstance(entry, dict)
                    for key in ("date", "ts", "slot", "category", "title",
                                "text", "raw", "metrics"):
                        self.assertIn(key, entry)
                    self.assertIn(entry["category"], parsing.CATEGORIES)
                    self.assertLessEqual(len(entry["title"]), 80)
                    self.assertIsInstance(entry["metrics"], dict)
                    self.assertRegex(entry["date"], r"^\d{4}-\d{2}-\d{2}$")


class ParseAnswerLLMTestCase(unittest.TestCase):
    def test_llm_json_normalized(self) -> None:
        llm = _support.FakeLLM(available=True, json_reply={
            "category": "еда",
            "title": "обед: рис с курицей",
            "text": "На обед ел рис с курицей.",
            "metrics": {"calories": 600, "water_ml": 500, "unknown_key": 1,
                        "weight_kg": 1000},
        })
        entry = parsing.parse_answer(llm, "midday", "Что ел?",
                                     "рис с курицей, 600 ккал", NOW_ISO)
        self.assertEqual(entry["category"], "food")
        self.assertEqual(entry["title"], "обед: рис с курицей")
        self.assertEqual(entry["text"], "На обед ел рис с курицей.")
        self.assertEqual(entry["metrics"]["calories"], 600)
        self.assertEqual(entry["metrics"]["water_ml"], 500)
        self.assertNotIn("unknown_key", entry["metrics"])
        self.assertNotIn("weight_kg", entry["metrics"], "1000 кг — вне допустимого")

    def test_llm_bad_category_guessed(self) -> None:
        llm = _support.FakeLLM(available=True, json_reply={
            "category": "несуществующая",
            "title": "",
            "text": "",
            "metrics": "не объект",
        })
        entry = parsing.parse_answer(llm, "midday", "Что ел?", "съел суп", NOW_ISO)
        self.assertEqual(entry["category"], "food")
        self.assertEqual(entry["metrics"], {})
        self.assertTrue(entry["title"])

    def test_llm_title_truncated(self) -> None:
        llm = _support.FakeLLM(available=True, json_reply={
            "category": "journal", "title": "я" * 300, "text": "т", "metrics": {},
        })
        entry = parsing.parse_answer(llm, "evening", "Мысли?", "текст", NOW_ISO)
        self.assertLessEqual(len(entry["title"]), 80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
