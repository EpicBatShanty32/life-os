"""Тесты src/dashboard_data.py: collect_data на демо-БД, null'ы, сборка HTML."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import _support
from src import config as config_mod
from src import dashboard_data, storage as storage_mod

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=_support.MSK)
TODAY = "2026-11-16"


class DashboardDataTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-dash-")
        self.tmp = Path(self._tmp.name)
        self.storage = storage_mod.Storage(str(self.tmp / "demo.db"))
        self.cfg = _support.make_cfg()

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    # ----------------------------------------------------------- помощники
    def add(self, **entry) -> int:
        entry.setdefault("ts", "2026-11-16T09:00:00+03:00")
        entry.setdefault("date", entry["ts"][:10])
        entry.setdefault("slot", "morning")
        entry.setdefault("category", "other")
        entry.setdefault("title", "запись")
        entry.setdefault("text", "текст")
        return self.storage.add_entry(entry)

    def fill_demo(self) -> None:
        # сегодня: сон, вода, тренировка, задачи
        self.add(date=TODAY, ts=TODAY + "T07:30:00+03:00", slot="morning",
                 category="sleep", title="сон", text="спал 7,5 часов",
                 metrics={"sleep_h": 7.5})
        self.add(date="2026-11-15", ts="2026-11-15T07:30:00+03:00",
                 slot="morning", category="sleep", text="спал 8 часов",
                 metrics={"sleep_h": 8})
        self.add(date=TODAY, ts=TODAY + "T13:00:00+03:00", slot="midday",
                 category="food", title="обед", text="рис с курицей",
                 metrics={"water_ml": 500, "calories": 700})
        self.add(date=TODAY, ts=TODAY + "T15:00:00+03:00", slot="midday",
                 category="water", text="ещё вода", metrics={"water_ml": 300})
        self.add(date=TODAY, ts=TODAY + "T19:00:00+03:00", slot="evening",
                 category="training", title="тренировка", text="силовая 60 минут",
                 metrics={"duration_min": 60, "mood": 4, "energy": 3})
        self.add(date=TODAY, ts=TODAY + "T21:00:00+03:00", slot="evening",
                 category="tasks", title="задачи", text="сделал отчёт",
                 metrics={"tasks_done": ["отчёт", "письма"],
                          "tasks_planned": ["отчёт", "письма", "созвон"]})
        self.storage.upsert_goal({"title": "Пробежать 10 км", "current": "6",
                                  "target": "10", "deadline": "2027-06-01"})
        self.storage.upsert_goal({"title": "Прочитать 12 книг", "current": "4",
                                  "target": "12", "status": "paused"})
        self.storage.touch_habit("зарядка", "2026-11-15")
        self.storage.touch_habit("зарядка", TODAY)

    # --------------------------------------------------------------- тесты
    def test_empty_database_schema_and_nulls(self) -> None:
        data = dashboard_data.collect_data(self.cfg, self.storage, now=NOW)
        for key in ("generated_at", "today", "kpis", "series", "goals",
                    "habits", "feed", "heatmap"):
            self.assertIn(key, data)
        self.assertEqual(data["today"]["date"], TODAY)
        self.assertEqual(data["today"]["entries"], 0)
        self.assertEqual(data["today"]["checkins"],
                         {"morning": False, "midday": False, "evening": False})
        self.assertIsNone(data["kpis"]["avg_sleep_h"])
        self.assertIsNone(data["kpis"]["mood_avg"])
        self.assertIsNone(data["kpis"]["water_avg_ml"])
        self.assertEqual(data["kpis"]["streak_days"], 0)
        self.assertIsNone(data["kpis"]["training_days_week"],
                          "нет данных, а не ложный ноль — осознанный контракт dashboard_data")
        self.assertEqual(data["series"]["dates"][-1], TODAY)
        self.assertEqual(len(data["series"]["dates"]), 14)
        self.assertEqual(data["series"]["sleep_h"], [None] * 14)
        self.assertEqual(data["series"]["training"], [0] * 14)
        self.assertEqual(data["goals"], [])
        self.assertEqual(data["habits"], [])
        self.assertEqual(data["feed"], [])
        self.assertEqual(data["heatmap"]["weeks"], 8)
        self.assertEqual(len(data["heatmap"]["cells"]), 56)
        # в данных нет NaN — page не сломается
        json.dumps(data, allow_nan=False)

    def test_demo_database_full_schema(self) -> None:
        self.fill_demo()
        # после фикса PII (security t9) дефолтный vault_path пуст — для проверки
        # ссылки на страницу целей задаём базу Obsidian явно
        cfg = _support.make_cfg(obsidian__vault_path="/tmp/vault")
        data = dashboard_data.collect_data(cfg, self.storage, now=NOW)
        self.assertTrue(data["generated_at"].startswith(TODAY + "T12:00"))
        self.assertEqual(data["today"]["date"], TODAY)
        self.assertEqual(data["today"]["entries"], 5)
        self.assertEqual(data["today"]["checkins"],
                         {"morning": True, "midday": True, "evening": True})

        kpis = data["kpis"]
        self.assertEqual(kpis["streak_days"], 2)  # сегодня и вчера
        self.assertGreaterEqual(kpis["checkin_rate_week"], 0.0)
        self.assertLessEqual(kpis["checkin_rate_week"], 1.0)
        self.assertEqual(kpis["training_days_week"], 1)
        self.assertAlmostEqual(kpis["avg_sleep_h"], 7.8, places=1)
        self.assertAlmostEqual(kpis["mood_avg"], 4.0, places=1)
        self.assertEqual(kpis["water_avg_ml"], 800)
        self.assertEqual(kpis["tasks_done_week"], 2)
        self.assertEqual(kpis["tasks_planned_week"], 3)

        series = data["series"]
        self.assertEqual(len(series["dates"]), 14)
        self.assertEqual(series["dates"][-1], TODAY)
        self.assertEqual(series["sleep_h"][-1], 7.5)
        self.assertEqual(series["water_ml"][-1], 800)
        self.assertEqual(series["calories"][-1], 700)
        self.assertEqual(series["mood"][-1], 4)
        self.assertEqual(series["energy"][-1], 3)
        self.assertEqual(series["training"][-1], 1)
        self.assertIsNone(series["sleep_h"][0])

        goals = data["goals"]
        self.assertEqual(len(goals), 2)
        active = [goal for goal in goals if goal["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["title"], "Пробежать 10 км")
        self.assertAlmostEqual(active[0]["progress"], 0.6, places=2)
        self.assertEqual(active[0]["deadline"], "2027-06-01")
        self.assertTrue(active[0]["link"].startswith("obsidian://open"))
        paused = [goal for goal in goals if goal["status"] == "paused"]
        self.assertAlmostEqual(paused[0]["progress"], 4 / 12, places=2)

        self.assertEqual(len(data["habits"]), 1)
        habit = data["habits"][0]
        self.assertEqual(habit["name"], "зарядка")
        self.assertEqual(habit["streak"], 2)
        self.assertEqual(habit["last_done"], TODAY)

        feed = data["feed"]
        self.assertEqual(len(feed), 5)
        for item in feed:
            self.assertIn("date", item)
            self.assertIn("time", item)
            self.assertIn("category", item)
            self.assertTrue(item["text"])
        self.assertEqual(feed[0]["date"], TODAY, "новые записи сверху")
        json.dumps(data, allow_nan=False)

    def test_heatmap_marks_today(self) -> None:
        self.fill_demo()
        data = dashboard_data.collect_data(self.cfg, self.storage, now=NOW)
        heatmap = data["heatmap"]
        start = datetime.fromisoformat(heatmap["start"]).date()
        self.assertEqual(start.weekday(), 0, "хитмап начинается с понедельника")
        dates = [cell["date"] for cell in heatmap["cells"]]
        self.assertEqual(dates, sorted(dates))
        self.assertIn(TODAY, dates)
        today_cell = next(cell for cell in heatmap["cells"] if cell["date"] == TODAY)
        self.assertEqual(today_cell["count"], 5)
        self.assertEqual(len(dates), 56)

    def test_weird_data_does_not_crash(self) -> None:
        self.add(date=TODAY, ts=TODAY + "T10:00:00+03:00",
                 metrics="не json", title=None, text=None, category=None)
        self.storage.add_entry({
            "date": TODAY, "ts": TODAY + "T11:00:00+03:00",
            "category": "food", "metrics": '{"water_ml": "много", "calories": null}',
            "title": "x" * 500,
        })
        data = dashboard_data.collect_data(self.cfg, self.storage, now=NOW)
        json.dumps(data, allow_nan=False, ensure_ascii=False)
        self.assertEqual(data["today"]["date"], TODAY)

    def test_storage_none_returns_skeleton(self) -> None:
        data = dashboard_data.collect_data(self.cfg, None, now=NOW)
        self.assertEqual(data["today"]["entries"], 0)
        self.assertEqual(data["feed"], [])


class BuildDashboardTestCase(unittest.TestCase):
    TEMPLATE = (
        "<!doctype html><html><head><title>{{APP_NAME}}</title></head><body>"
        "<div>{{GENERATED_HUMAN}} ({{GENERATED_AT}}, {{GENERATED_YEAR}})</div>"
        "<script>window.LIFEOS_DATA = {{LIFEOS_DATA}};</script>"
        "</body></html>"
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-build-")
        self.tmp = Path(self._tmp.name)
        self.storage = storage_mod.Storage(str(self.tmp / "demo.db"))
        self.storage.add_entry({
            "date": TODAY, "ts": TODAY + "T09:00:00+03:00", "slot": "morning",
            "category": "tasks", "title": "план", "text": "написать отчёт",
            "metrics": {},
        })
        self.storage.upsert_goal({"title": "Цель", "current": "1", "target": "2"})

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_build_with_custom_template(self) -> None:
        template = self.tmp / "template.html"
        template.write_text(self.TEMPLATE, encoding="utf-8")
        out = self.tmp / "out" / "index.html"
        cfg = _support.make_cfg(paths={
            "db": "unused.db",
            "logs": "logs",
            "dashboard_out": str(out),
            "dashboard_template": str(template),
        })
        result = dashboard_data.build_dashboard(cfg, self.storage)
        self.assertIsNotNone(result)
        self.assertTrue(out.is_file())
        page = out.read_text(encoding="utf-8")
        self.assertIn("window.LIFEOS_DATA", page)
        self.assertNotIn("{{", page, "все плейсхолдеры заменены")
        self.assertIn("Life OS", page)
        self.assertIn("написать отчёт", page)
        # рядом лежит data.json
        data = json.loads((out.parent / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(data["feed"][0]["text"], "написать отчёт")
        self.assertEqual(len(data["heatmap"]["cells"]), 56)
        # повторная сборка перезаписывает без ошибок
        self.assertEqual(dashboard_data.build_dashboard(cfg, self.storage), str(out))

    def test_build_without_template_returns_none(self) -> None:
        cfg = _support.make_cfg(
            _root=str(self.tmp),
            paths={"db": "x.db", "logs": "logs",
                   "dashboard_out": str(self.tmp / "index.html")},
        )
        self.assertIsNone(dashboard_data.build_dashboard(cfg, self.storage))

    def test_build_with_project_template_if_present(self) -> None:
        template = config_mod.project_root() / "dashboard" / "template.html"
        if not template.is_file():
            self.skipTest("dashboard/template.html ещё не создан dash-ui — проверка "
                          "HTML отложена")
        out = self.tmp / "project" / "index.html"
        cfg = _support.make_cfg(paths={
            "db": "unused.db", "logs": "logs", "dashboard_out": str(out),
        })
        result = dashboard_data.build_dashboard(cfg, self.storage)
        self.assertIsNotNone(result)
        page = out.read_text(encoding="utf-8")
        self.assertIn("LIFEOS_DATA", page)
        self.assertNotIn("{{", page)
        self.assertNotRegex(page, r"\bNaN\b", "в данных/странице не должно быть NaN")
        # self-contained: никаких внешних скриптов/стилей/картинок
        lowered = page.lower()
        self.assertNotIn("<script src=", lowered)
        self.assertNotIn("<link rel=\"stylesheet\"", lowered)
        data = json.loads((out.parent / "data.json").read_text(encoding="utf-8"))
        self.assertTrue(data["feed"])



class GoalProgressContractTestCase(unittest.TestCase):
    """P0-1: дашборд считает прогресс тем же goals_math, что бот и Obsidian."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-goal-")
        self.storage = storage_mod.Storage(
            str(Path(self._tmp.name) / "goals.db"))
        self.cfg = _support.make_cfg()

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def _progress(self, title: str) -> float:
        self.storage.upsert_goal({"title": title, "start": "92",
                                  "current": "85", "target": "78"})
        data = dashboard_data.collect_data(self.cfg, self.storage, now=NOW)
        return data["goals"][0]["progress"]

    def test_weight_goal_is_50_percent_at_85(self) -> None:
        self.assertAlmostEqual(self._progress("Сбросить вес"), 0.5, places=3)

    def test_weight_goal_at_start_is_zero(self) -> None:
        self.storage.upsert_goal({"title": "Сбросить вес", "start": "92",
                                  "current": "92", "target": "78",
                                  "direction": "down"})
        data = dashboard_data.collect_data(self.cfg, self.storage, now=NOW)
        self.assertEqual(data["goals"][0]["progress"], 0.0)

    def test_weight_goal_at_target_is_100_percent(self) -> None:
        self.storage.upsert_goal({"title": "Сбросить вес", "start": "92",
                                  "current": "78", "target": "78"})
        data = dashboard_data.collect_data(self.cfg, self.storage, now=NOW)
        self.assertEqual(data["goals"][0]["progress"], 1.0)

    def test_dashboard_matches_goals_math(self) -> None:
        from src import goals_math

        self.storage.upsert_goal({"title": "Сбросить вес", "start": "92",
                                  "current": "85", "target": "78"})
        goal = self.storage.goals()[0]
        data = dashboard_data.collect_data(self.cfg, self.storage, now=NOW)
        self.assertEqual(data["goals"][0]["progress"],
                         goals_math.goal_fraction(goal))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SidebarTemplateTestCase(unittest.TestCase):
    """Сайдбар дашборда v0.1.2: разделы, поиск, тема, мобильное меню.

    Проверки статические — по тексту шаблона: они ловят регрессии
    разметки (пункт меню без секции, внешний ресурс, съехавшие размеры)
    до браузера. Поведение (контраст, фокус, прокрутка) проверяется
    отдельно в Playwright-прогоне релиза.
    """

    SECTIONS = [
        ("top", "Обзор"),
        ("sec-today", "Сегодня"),
        ("sec-goals", "Цели"),
        ("sec-sleep", "Сон"),
        ("sec-train", "Тренировки"),
        ("sec-food", "Питание"),
        ("sec-mood", "Настроение"),
        ("sec-habits", "Привычки"),
        ("sec-journal", "Журнал"),
        ("sec-heat", "Активность"),
    ]

    @classmethod
    def setUpClass(cls) -> None:
        cls.template_path = config_mod.project_root() / "dashboard" / "template.html"
        if not cls.template_path.is_file():
            raise unittest.SkipTest("dashboard/template.html отсутствует")
        cls.html = cls.template_path.read_text(encoding="utf-8")

    def test_nav_links_match_sections(self) -> None:
        pairs = re.findall(
            r'class="nav-a[^"]*"\s+href="#([^"]+)"\s+data-section="([^"]+)"',
            self.html,
        )
        self.assertEqual(
            pairs,
            [(section, section) for section, _ in self.SECTIONS],
            "пункты меню и их якоря должны идти в одном порядке",
        )
        for section, label in self.SECTIONS:
            self.assertIn('id="%s"' % section, self.html, "нет секции %s" % section)
            self.assertIn(
                '<span class="nav-label">%s</span>' % label, self.html,
                "нет подписи %s" % label,
            )
        # по умолчанию активен ровно один пункт — «Обзор»
        self.assertEqual(self.html.count('aria-current="location"'), 1)
        self.assertIn(
            'href="#top" data-section="top" title="Обзор" aria-current="location"',
            self.html,
        )

    def test_dashboard_is_self_contained(self) -> None:
        refs = re.findall(r'(?:src|href)="([^"]+)"', self.html)
        remote = [
            ref for ref in refs
            if ref.startswith(("http://", "https://", "//"))
        ]
        self.assertEqual(remote, [], "дашборд не должен грузить внешние ресурсы")
        urls = re.findall(r"url\(([^)]+)\)", self.html)
        self.assertEqual(
            [u for u in urls if "http" in u.lower()], [],
            "в CSS нет внешних url()",
        )
        self.assertIn('src="data:image/png;base64,', self.html)
        self.assertNotIn('rel="stylesheet"', self.html)
        self.assertNotIn("<link ", self.html)

    def test_sidebar_contract(self) -> None:
        # 228px в диапазоне 224–232 и компактные строки
        width = re.search(r"--side-w:(\d+)px", self.html)
        self.assertIsNotNone(width)
        self.assertTrue(224 <= int(width.group(1)) <= 232)
        nav_rule = re.search(r"\.nav-a\{[^}]*\}", self.html)
        self.assertIsNotNone(nav_rule)
        self.assertIn("font-size:13px", nav_rule.group(0))
        self.assertIn("min-height:36px", nav_rule.group(0))
        # свёрнутый режим, мобильный drawer, логотип
        self.assertIn("grid-template-columns:76px minmax(0,1fr)", self.html)
        self.assertIn("width:272px", self.html)
        self.assertIn(".logo{width:42px", self.html)
        self.assertIn("--side-active:#EAE8F7", self.html)
        # тема: тёмные значения на тех же токенах + запоминание
        self.assertIn('html[data-theme="dark"]{', self.html)
        self.assertIn("localStorage", self.html)
        self.assertIn('id="theme"', self.html)
        self.assertIn('aria-pressed="false"', self.html)
        self.assertIn('name="theme-color"', self.html)
        # поиск: горячие клавиши и счётчик
        self.assertIn('id="q"', self.html)
        self.assertIn('"найдено "', self.html)
        self.assertIn('e.metaKey || e.ctrlKey', self.html)
        self.assertIn('e.key === "/"', self.html)

    def test_active_section_is_computed_after_render(self) -> None:
        # наверху страницы — «Обзор», последний пункт не форсируется
        self.assertIn("y < 12 && !jumping", self.html)
        self.assertIn("setActiveLink(navLinks[0])", self.html)
        # состояние из адреса восстанавливаем после отрисовки карточек:
        # до неё высоты секций нулевые и активным был бы «низ страницы»
        boot = self.html[self.html.index("function boot()"):]
        for render in ("renderToday()", "renderFeed()", "renderHeatmap()", "renderCharts()"):
            self.assertLess(boot.index(render), boot.index("restoreHash();"),
                            "%s должен идти до restoreHash()" % render)
        # прокрутка и клик ведут к секции, а не к подмене экранов
        self.assertIn('window.addEventListener("scroll", onScroll', self.html)
        self.assertIn('navigate(this.getAttribute("data-section")', self.html)
        self.assertNotIn("is-off", self.html)
