"""Тесты src/scheduler.py на фейковых tg/llm/vault/dash (без сети и диска vault)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import _support
from src import prompts
from src import scheduler as scheduler_mod
from src import storage as storage_mod

CHAT = _support.TEST_CHAT_ID


class _Retry429Error(Exception):
    def __init__(self, message: str = "rate limit") -> None:
        super().__init__(message)
        self.status = 429
        self.retry_after = 0.01
        self.message = message


class SchedulerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-sched-")
        self.storage = storage_mod.Storage(str(Path(self._tmp.name) / "db.sqlite"))
        self.tg = _support.FakeTelegram()
        self.llm = _support.FakeLLM(available=False)
        self.vault = _support.FakeVault()
        self.dash = _support.FakeDashboard()
        self.cfg = _support.make_cfg(
            telegram__allowed_chat_id=CHAT,
            llm__enabled=False,
        )
        self.scheduler = self.make_scheduler()

    def make_scheduler(self, **overrides):
        cfg = self.cfg if not overrides else _support.make_cfg(
            telegram__allowed_chat_id=overrides.pop("chat_id", CHAT),
            llm__enabled=False,
            **overrides,
        )
        return scheduler_mod.Scheduler(
            cfg, self.storage, self.tg, self.llm, self.vault, self.dash,
            _support.quiet_logger(),
        )

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    # ----------------------------------------------------------- помощники
    def start_morning(self) -> list[str]:
        self.scheduler.tick(_support.at(2026, 11, 16, 8, 30))
        return self.tg.texts()

    def all_entries(self) -> list[dict]:
        return self.storage.entries("0001-01-01", "9999-12-31")


class SlotTickTestCase(SchedulerTestBase):
    def test_tick_starts_morning_slot(self) -> None:
        texts = self.start_morning()
        self.assertEqual(len(texts), 1)
        first_question = prompts.scripted_questions("morning")[0]
        self.assertIn(first_question, texts[0])
        self.assertIn(prompts.greeting("morning")[:20], texts[0])
        session = json.loads(self.storage.kv_get("session"))
        self.assertEqual(session["slot"], "morning")
        self.assertEqual(session["step"], "question")
        self.assertEqual(session["date"], "2026-11-16")
        self.assertEqual(self.storage.kv_get("last_slot:morning"), "2026-11-16")
        # повторный тик в тот же день не начинает заново
        self.scheduler.tick(_support.at(2026, 11, 16, 8, 31))
        self.assertEqual(len(self.tg.texts()), 1)

    def test_tick_before_slot_time_does_nothing(self) -> None:
        self.scheduler.tick(_support.at(2026, 11, 16, 7, 0))
        self.assertEqual(self.tg.texts(), [])
        self.assertIsNone(self.storage.kv_get("session"))

    def test_tick_paused_does_not_start(self) -> None:
        self.storage.kv_set("paused", "1")
        self.scheduler.tick(_support.at(2026, 11, 16, 8, 30))
        self.assertEqual(self.tg.texts(), [])
        self.assertIsNone(self.storage.kv_get("session"))

    def test_second_slot_not_started_while_session_active(self) -> None:
        self.start_morning()
        self.scheduler.tick(_support.at(2026, 11, 16, 14, 0))
        session = json.loads(self.storage.kv_get("session"))
        self.assertEqual(session["slot"], "morning", "второй слот не должен перебивать сессию")
        self.assertIsNone(self.storage.kv_get("last_slot:midday"))
        # при простое 5.5 ч бот мог отправить напоминание — это не старт слота
        self.assertLessEqual(len(self.tg.texts()), 2)
        self.assertIn("Если ещё актуально", self.tg.last())

    def test_session_starts_late_within_catchup(self) -> None:
        self.scheduler.tick(_support.at(2026, 11, 16, 9, 30))  # опоздание 60 мин
        self.assertEqual(len(self.tg.texts()), 1)
        self.assertIsNotNone(self.storage.kv_get("session"))

    def test_heavily_late_slot_soft_ping_evening(self) -> None:
        scheduler = self.make_scheduler(schedule={
            "morning": {"time": "08:30", "enabled": True},
            "midday": {"time": "14:00", "enabled": True},
            "evening": {"time": "21:30", "enabled": True},
            "weekly": {"time": "21:00", "weekday": 6, "enabled": False},
            "catchup_minutes": 0,
        })
        scheduler.tick(_support.at(2026, 11, 16, 23, 0))
        texts = self.tg.texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("День почти прошёл", texts[0])
        self.assertIsNone(self.storage.kv_get("session"))
        # второй тик не повторяет пинг
        scheduler.tick(_support.at(2026, 11, 16, 23, 10))
        self.assertEqual(len(self.tg.texts()), 1)

    def test_disabled_slot_not_started(self) -> None:
        scheduler = self.make_scheduler(schedule={
            "morning": {"time": "08:30", "enabled": False},
            "midday": {"time": "14:00", "enabled": False},
            "evening": {"time": "21:30", "enabled": False},
            "weekly": {"time": "21:00", "weekday": 6, "enabled": False},
        })
        scheduler.tick(_support.at(2026, 11, 16, 9, 0))
        self.assertEqual(self.tg.texts(), [])

    def test_weekly_slot_starts_on_configured_weekday(self) -> None:
        scheduler = self.make_scheduler(schedule={
            "morning": {"time": "08:30", "enabled": False},
            "midday": {"time": "14:00", "enabled": False},
            "evening": {"time": "21:30", "enabled": False},
            "weekly": {"time": "21:00", "weekday": 6, "enabled": True},
        })
        scheduler.tick(_support.at(2026, 11, 15, 21, 0))  # воскресенье
        self.assertEqual(len(self.tg.texts()), 1)
        self.assertEqual(json.loads(self.storage.kv_get("session"))["slot"], "weekly")
        # не воскресенье — не стартует
        self.storage.kv_del("session")
        self.storage.kv_del("last_slot:weekly")
        scheduler.tick(_support.at(2026, 11, 16, 21, 0))  # понедельник
        self.assertEqual(len(self.tg.texts()), 1)

    def test_naive_datetime_treated_as_local(self) -> None:
        from datetime import datetime

        self.scheduler.tick(datetime(2026, 11, 16, 8, 30))
        self.assertEqual(len(self.tg.texts()), 1)


class SessionFlowTestCase(SchedulerTestBase):
    def test_full_morning_dialog(self) -> None:
        questions = prompts.scripted_questions("morning")
        self.start_morning()
        answers = [
            "План: написать отчёт и провести встречу с командой",
            "Задачи: 1) отчёт 2) встреча 3) спортзал вечером",
            "Цели: продвинуться по проекту Life OS и закрыть этап тестов",
        ]
        for index, answer in enumerate(answers):
            self.scheduler.on_message(
                CHAT, answer, _support.at(2026, 11, 16, 8, 31 + index)
            )
            if index < len(questions) - 1:
                self.assertIn(questions[index + 1], self.tg.last())
        self.assertIn("Спасибо! План сохранил", self.tg.last())
        self.assertIsNone(self.storage.kv_get("session"))
        entries = self.all_entries()
        self.assertEqual(len(entries), len(questions))
        self.assertTrue(all(entry["slot"] == "morning" for entry in entries))
        self.assertTrue(all(entry["written"] == 1 for entry in entries))
        self.assertEqual(len(self.vault.entries), len(questions))
        self.assertGreaterEqual(self.dash.calls, len(questions))
        # история диалога записана: вопросы бота + ответы пользователя
        history = self.storage.chat_history(limit=50)
        self.assertTrue(any(row["direction"] == "in" for row in history))
        self.assertTrue(any(row["direction"] == "out" for row in history))

    def test_resume_session_after_scheduler_restart(self) -> None:
        questions = prompts.scripted_questions("morning")
        self.start_morning()
        restarted = self.make_scheduler()
        restarted.on_message(CHAT, "План: отчёт и встреча", _support.at(2026, 11, 16, 8, 35))
        self.assertEqual(len(self.all_entries()), 1)
        self.assertIn(questions[1], self.tg.last())

    def test_skip_advances_without_entry(self) -> None:
        self.start_morning()
        self.scheduler.on_message(CHAT, "-", _support.at(2026, 11, 16, 8, 31))
        self.assertIn("пропустил", self.tg.last().lower())
        # вопрос №2 идёт следующим сообщением (с префиксом)
        self.assertIn(prompts.scripted_questions("morning")[1], self.tg.last())
        self.assertEqual(self.all_entries(), [])
        self.assertIsNotNone(self.storage.kv_get("session"))

    def test_stop_command_clears_session(self) -> None:
        self.start_morning()
        self.scheduler.on_message(CHAT, "стоп", _support.at(2026, 11, 16, 8, 31))
        self.assertIn("сессию остановил", self.tg.last())
        self.assertIsNone(self.storage.kv_get("session"))

    def test_skip_command_without_session(self) -> None:
        self.scheduler.on_message(CHAT, "/skip", _support.at(2026, 11, 16, 9, 0))
        self.assertIn("нет активной сессии", self.tg.last())

    def test_followup_limit(self) -> None:
        scheduler = self.make_scheduler(llm={"enabled": True, "max_followups": 2})
        self.llm = _support.FakeLLM(available=True,
                                    chat_reply="Уточни: сколько воды выпил?")
        scheduler.llm = self.llm
        scheduler.tick(_support.at(2026, 11, 16, 8, 30))
        questions = prompts.scripted_questions("morning")
        # короткий ответ → уточнение №1
        scheduler.on_message(CHAT, "норм", _support.at(2026, 11, 16, 8, 31))
        self.assertEqual(self.tg.last(), "Уточни: сколько воды выпил?")
        # ещё короткий → уточнение №2
        scheduler.on_message(CHAT, "норм", _support.at(2026, 11, 16, 8, 32))
        self.assertEqual(self.tg.last(), "Уточни: сколько воды выпил?")
        # лимит исчерпан → следующий вопрос
        scheduler.on_message(CHAT, "норм", _support.at(2026, 11, 16, 8, 33))
        self.assertIn(questions[1], self.tg.last())
        self.assertEqual(len(self.all_entries()), 3)
        # каждый ответ сохранён, в базе три записи

    def test_no_followup_for_long_answer(self) -> None:
        self.llm = _support.FakeLLM(available=True, chat_reply="уточнение?")
        self.scheduler.llm = self.llm
        self.start_morning()
        self.llm.chat_calls = 0
        long_answer = "Написал отчёт, провёл две встречи, разобрал почту и сделал зарядку"
        self.scheduler.on_message(CHAT, long_answer, _support.at(2026, 11, 16, 8, 31))
        self.assertEqual(self.llm.chat_calls, 0)
        self.assertIn(prompts.scripted_questions("morning")[1], self.tg.last())

    def test_answer_saved_even_if_telegram_fails(self) -> None:
        self.start_morning()
        self.tg.fail_with = RuntimeError("сеть пропала")
        self.scheduler.on_message(CHAT, "план: отчёт", _support.at(2026, 11, 16, 8, 31))
        self.assertEqual(len(self.all_entries()), 1)

    def test_send_retry_on_429(self) -> None:
        self.tg.fail_with = _Retry429Error()
        self.scheduler.tick(_support.at(2026, 11, 16, 8, 30))
        self.assertEqual(len(self.tg.texts()), 1, "повтор после 429 должен пройти")
        self.assertIsNotNone(self.storage.kv_get("session"))

    def test_overdue_session_cleared_next_day(self) -> None:
        self.start_morning()
        self.scheduler.tick(_support.at(2026, 11, 17, 9, 0))
        self.assertIsNone(self.storage.kv_get("session"))
        # на следующем тике может начаться новый слот этого дня
        self.scheduler.tick(_support.at(2026, 11, 17, 9, 1))
        session = json.loads(self.storage.kv_get("session"))
        self.assertEqual(session["date"], "2026-11-17")

    def test_session_reminder_after_idle(self) -> None:
        self.start_morning()
        self.scheduler.tick(_support.at(2026, 11, 16, 12, 30))  # простой 4 часа
        self.assertIn("Если ещё актуально", self.tg.last())
        self.assertIsNotNone(self.storage.kv_get("session"))
        count = len(self.tg.texts())
        self.scheduler.tick(_support.at(2026, 11, 16, 13, 0))
        self.assertEqual(len(self.tg.texts()), count, "напоминание только раз")

    def test_session_ttl_clears_stale(self) -> None:
        self.start_morning()
        self.scheduler.tick(_support.at(2026, 11, 16, 21, 30))  # 13 часов простоя
        self.assertIsNone(self.storage.kv_get("session"))


class CommandsTestCase(SchedulerTestBase):
    def test_help(self) -> None:
        self.scheduler.on_message(CHAT, "/help", _support.at(2026, 11, 16, 9, 0))
        text = self.tg.last()
        self.assertIn("Команды Life OS", text)
        for command in ("/status", "/today", "/report", "/goals", "/pause",
                        "/resume", "/help", "/skip", "/add_goal"):
            self.assertIn(command, text)

    def test_status(self) -> None:
        self.scheduler.on_message(CHAT, "/status", _support.at(2026, 11, 16, 9, 0))
        text = self.tg.last()
        self.assertIn("статус", text.lower())
        self.assertIn("Сессия", text)

    def test_today_and_report(self) -> None:
        from datetime import datetime

        today = datetime.now(_support.MSK).date().isoformat()
        self.storage.add_entry({"date": today,
                                "ts": today + "T10:00:00+03:00",
                                "slot": "morning", "category": "tasks",
                                "title": "план", "text": "написать отчёт"})
        self.scheduler.on_message(CHAT, "/today", _support.at(2026, 11, 16, 12, 0))
        self.assertIn("Сегодня", self.tg.last())
        self.assertIn("план", self.tg.last())
        self.scheduler.on_message(CHAT, "/report", _support.at(2026, 11, 16, 12, 1))
        self.assertIn("Отчёт за 7 дней", self.tg.last())

    def test_goals_and_add_goal(self) -> None:
        self.scheduler.on_message(CHAT, "/goals", _support.at(2026, 11, 16, 9, 0))
        self.assertIn("Целей пока нет", self.tg.last())
        self.scheduler.on_message(
            CHAT, "/add_goal Пробежать 10 км | 3/10 | 2027-06-01",
            _support.at(2026, 11, 16, 9, 1),
        )
        self.assertIn("Цель добавлена", self.tg.last())
        goals = self.storage.goals()
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["title"], "Пробежать 10 км")
        self.assertEqual(goals[0]["current"], "3")
        self.assertEqual(goals[0]["target"], "10")
        self.assertEqual(self.vault.rewrite_goals_calls, 1)
        self.scheduler.on_message(CHAT, "/goals", _support.at(2026, 11, 16, 9, 2))
        self.assertIn("30%", self.tg.last())
        self.assertIn("Пробежать 10 км", self.tg.last())

    def test_pause_and_resume(self) -> None:
        self.scheduler.on_message(CHAT, "/pause", _support.at(2026, 11, 16, 9, 0))
        self.assertEqual(self.storage.kv_get("paused"), "1")
        self.assertIn("Пауза включена", self.tg.last())
        self.scheduler.on_message(CHAT, "/resume", _support.at(2026, 11, 16, 9, 1))
        self.assertEqual(self.storage.kv_get("paused"), "0")
        self.assertIn("Пауза снята", self.tg.last())

    def test_command_aliases_russian(self) -> None:
        self.scheduler.on_message(CHAT, "статус", _support.at(2026, 11, 16, 9, 0))
        self.assertIn("статус", self.tg.last().lower())
        self.scheduler.on_message(CHAT, "помощь", _support.at(2026, 11, 16, 9, 1))
        self.assertIn("Команды Life OS", self.tg.last())
        self.scheduler.on_message(CHAT, "сегодня", _support.at(2026, 11, 16, 9, 2))
        self.assertIn("Сегодня", self.tg.last())
        self.scheduler.on_message(CHAT, "пауза", _support.at(2026, 11, 16, 9, 3))
        self.assertEqual(self.storage.kv_get("paused"), "1")

    def test_unknown_command(self) -> None:
        self.scheduler.on_message(CHAT, "/foobar", _support.at(2026, 11, 16, 9, 0))
        self.assertIn("Не знаю такую команду", self.tg.last())

    def test_empty_message(self) -> None:
        self.scheduler.on_message(CHAT, "   ", _support.at(2026, 11, 16, 9, 0))
        self.assertIn("Пустое сообщение", self.tg.last())

    def test_design_command_rebuilds_dashboard(self) -> None:
        before = self.dash.calls
        self.scheduler.on_message(CHAT, "/дизайн", _support.at(2026, 11, 16, 9, 0))
        self.assertEqual(self.dash.calls, before + 1)
        self.assertIn("Дашборд обновлён", self.tg.last())

    def test_start_command(self) -> None:
        self.scheduler.on_message(CHAT, "/start", _support.at(2026, 11, 16, 9, 0))
        self.assertIn("Life OS", self.tg.last())
        self.assertIn("/today", self.tg.last())


class FreeChatAndAccessTestCase(SchedulerTestBase):
    def test_free_text_without_llm(self) -> None:
        self.scheduler.on_message(CHAT, "Съел бутерброд и выпил кофе",
                                  _support.at(2026, 11, 16, 12, 0))
        self.assertIn("Записал", self.tg.last())
        entries = self.all_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["slot"], "free")
        self.assertEqual(entries[0]["category"], "food")
        self.assertEqual(entries[0]["written"], 1)

    def test_free_text_with_llm_reply(self) -> None:
        self.llm = _support.FakeLLM(available=True, chat_reply="Понял, записал.")
        self.scheduler.llm = self.llm
        self.scheduler.on_message(CHAT, "Привет! Как дела?",
                                  _support.at(2026, 11, 16, 12, 0))
        self.assertEqual(self.tg.last(), "Понял, записал.")
        self.assertEqual(len(self.all_entries()), 1)

    def test_foreign_chat_ignored(self) -> None:
        self.scheduler.on_message(999, "чужой", _support.at(2026, 11, 16, 12, 0))
        self.assertEqual(self.tg.texts(), [])
        self.assertEqual(self.all_entries(), [])

    def test_allowed_chat_zero_accepts_any(self) -> None:
        scheduler = self.make_scheduler(chat_id=0)
        scheduler.on_message(555, "привет", _support.at(2026, 11, 16, 12, 0))
        self.assertEqual(len(self.tg.texts()), 1)

    def test_vault_failure_leaves_entry_unwritten(self) -> None:
        self.vault = _support.FakeVault(write_ok=False)
        self.scheduler.vault = self.vault
        self.scheduler.on_message(CHAT, "Съел суп", _support.at(2026, 11, 16, 12, 0))
        entries = self.all_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["written"], 0)
        self.assertEqual(len(self.storage.entries_unwritten()), 1)

    def test_status_text_after_data(self) -> None:
        from datetime import datetime

        today = datetime.now(_support.MSK).date().isoformat()
        self.storage.add_entry({"date": today,
                                "ts": today + "T10:00:00+03:00",
                                "slot": "morning", "category": "tasks",
                                "text": "план", "title": "план"})
        self.storage.upsert_goal({"title": "Цель", "current": "1", "target": "2"})
        status = self.scheduler.status_text()
        self.assertIn("Записей сегодня", status)
        self.assertIn("Цели: 1", status)
        today = self.scheduler.today_text()
        self.assertIn("Сегодня", today)
        report = self.scheduler.report_text()
        self.assertIn("Отчёт за 7 дней", report)
        goals = self.scheduler.goals_text()
        self.assertIn("Цели (1)", goals)

    def test_empty_texts_do_not_crash(self) -> None:
        self.assertIn("Записей пока нет", self.scheduler.today_text())
        self.assertIn("Записей за неделю нет", self.scheduler.report_text())
        self.assertIn("Целей пока нет", self.scheduler.goals_text())
        self.assertIn("Life OS", self.scheduler.status_text())

    def test_tick_rollover_runs_once_per_day(self) -> None:
        self.scheduler.tick(_support.at(2026, 11, 16, 7, 0))
        calls_after_first = (self.vault.sync_calls, self.vault.rewrite_goals_calls,
                             self.vault.mirror_calls, self.dash.calls)
        self.scheduler.tick(_support.at(2026, 11, 16, 7, 1))
        self.assertEqual(
            (self.vault.sync_calls, self.vault.rewrite_goals_calls,
             self.vault.mirror_calls, self.dash.calls),
            calls_after_first,
        )
        self.scheduler.tick(_support.at(2026, 11, 17, 7, 0))
        self.assertGreater(self.vault.sync_calls, calls_after_first[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
