"""Тесты src/goals_math.py — единый расчёт прогресса целей (UX_REVIEW P0-1)."""

from __future__ import annotations

import unittest

from src import goals_math


class GoalFractionTestCase(unittest.TestCase):
    def test_up_without_start_is_current_over_target(self) -> None:
        goal = {"title": "Читать", "current": "3", "target": "10"}
        self.assertAlmostEqual(goals_math.goal_fraction(goal), 0.3, places=3)

    def test_up_with_start_counts_from_start(self) -> None:
        goal = {"title": "Читать", "start": "3", "current": "6", "target": "10"}
        self.assertAlmostEqual(goals_math.goal_fraction(goal), 3 / 7, places=3)

    def test_down_without_start_is_zero_until_target(self) -> None:
        goal = {"title": "Сбросить вес", "current": "92", "target": "78"}
        self.assertEqual(goals_math.goal_fraction(goal), 0.0,
                         "без старта «почти 100%» было бы выдумкой")
        goal["current"] = "78"
        self.assertEqual(goals_math.goal_fraction(goal), 1.0)

    def test_weight_92_78_with_start_gives_0_50_100(self) -> None:
        base = {"title": "Сбросить вес", "start": "92", "target": "78"}
        cases = (("92", 0.0), ("85", 0.5), ("78", 1.0))
        for current, expected in cases:
            goal = dict(base, current=current)
            self.assertAlmostEqual(goals_math.goal_fraction(goal), expected,
                                   places=3, msg="current=%s" % current)

    def test_start_equal_target_does_not_divide_by_zero(self) -> None:
        goal = {"title": "Цель", "start": "10", "current": "5", "target": "10"}
        value = goals_math.goal_fraction(goal)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 1.0)

    def test_zero_target(self) -> None:
        goal = {"title": "Цель", "current": "0", "target": "0"}
        self.assertEqual(goals_math.goal_fraction(goal), 1.0)
        goal["current"] = "5"
        self.assertEqual(goals_math.goal_fraction(goal), 0.0)

    def test_done_status_is_100_percent(self) -> None:
        goal = {"title": "Цель", "status": "done", "current": "1", "target": "10"}
        self.assertEqual(goals_math.goal_fraction(goal), 1.0)

    def test_percent_in_text_is_parsed(self) -> None:
        self.assertAlmostEqual(
            goals_math.goal_fraction({"title": "Цель", "note": "60%"}),
            0.6, places=3,
        )

    def test_progress_field(self) -> None:
        self.assertAlmostEqual(
            goals_math.goal_fraction({"title": "Цель", "progress": 0.25}),
            0.25, places=3,
        )
        self.assertAlmostEqual(
            goals_math.goal_fraction({"title": "Цель", "progress": "60%"}),
            0.6, places=3,
        )
        self.assertAlmostEqual(
            goals_math.goal_fraction({"title": "Цель", "progress": 60}),
            0.6, places=3,
        )

    def test_garbage_gives_zero(self) -> None:
        self.assertEqual(goals_math.goal_fraction(None), 0.0)
        self.assertEqual(goals_math.goal_fraction({}), 0.0)
        self.assertEqual(goals_math.goal_fraction({"current": "много"}),
                         0.0)


class GoalDirectionTestCase(unittest.TestCase):
    def test_direction_from_title(self) -> None:
        self.assertEqual(goals_math.goal_direction({"title": "Сбросить вес"}),
                         "down")
        self.assertEqual(goals_math.goal_direction({"title": "Пробежать 10 км"}),
                         "up")
        self.assertEqual(goals_math.goal_direction({"title": "Набрать вес"}),
                         "up", "набор веса — это рост")

    def test_stored_direction_wins(self) -> None:
        self.assertEqual(
            goals_math.goal_direction({"title": "Пробежать 10 км",
                                       "direction": "вниз"}),
            "down",
        )

    def test_goal_start_synonyms(self) -> None:
        self.assertEqual(goals_math.goal_start({"start": "92"}), 92.0)
        self.assertEqual(goals_math.goal_start({"start_value": "1 800"}), 1800.0)
        self.assertIsNone(goals_math.goal_start({"title": "Цель"}))


class GoalPercentTestCase(unittest.TestCase):
    def test_none_without_any_data(self) -> None:
        self.assertIsNone(goals_math.goal_percent({"title": "Цель"}))

    def test_target_only_is_zero_like_dashboard(self) -> None:
        self.assertEqual(
            goals_math.goal_percent({"title": "Цель", "target": "10"}), 0,
        )

    def test_rounding(self) -> None:
        self.assertEqual(
            goals_math.goal_percent({"title": "Цель", "current": "3",
                                     "target": "10"}),
            30,
        )
        self.assertEqual(
            goals_math.goal_percent({"title": "Цель", "start": "92",
                                     "current": "85", "target": "78",
                                     "direction": "down"}),
            50,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
