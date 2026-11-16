"""Устойчивость v0.3: backoff в главном цикле, «пульс» планировщика,
tools/health.py и ротация логов.

Тесты не ходят в сеть, не трогают боевые БД/логи: всё во временных каталогах.
"""

from __future__ import annotations

import gc
import importlib.util
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import _support
from src import config as config_mod
from src import main as main_mod
from src import scheduler as scheduler_mod
from src import storage as storage_mod

CHAT = _support.TEST_CHAT_ID


def load_health_module():
    """Импортирует ``tools/health.py`` как модуль (tools/ — не пакет)."""
    path = Path(_support.PROJECT_ROOT) / "tools" / "health.py"
    spec = importlib.util.spec_from_file_location("lifeos_health", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


health = load_health_module()


def _quiet_scheduler(workdir, **cfg_overrides):
    """Планировщик на фейках: без сети, vault и дашборда."""
    storage = storage_mod.Storage(os.path.join(workdir, "db.sqlite"))
    cfg = _support.make_cfg(
        telegram__allowed_chat_id=CHAT,
        llm__enabled=False,
        schedule={
            "morning": {"time": "08:30", "enabled": False},
            "midday": {"time": "14:00", "enabled": False},
            "evening": {"time": "21:30", "enabled": False},
            "weekly": {"time": "21:00", "weekday": 6, "enabled": False},
        },
    )
    for key, value in cfg_overrides.items():
        cfg[key] = value
    scheduler = scheduler_mod.Scheduler(
        cfg, storage, _support.FakeTelegram(), _support.FakeLLM(),
        None, None, _support.quiet_logger("lifeos.resilience"),
    )
    return scheduler, storage


# ===========================================================================
# 1. Backoff главного цикла
# ===========================================================================
class BackoffTest(unittest.TestCase):
    def test_grows_exponentially_and_stops_at_cap(self):
        backoff = main_mod.Backoff()
        delays = [backoff.next() for _ in range(7)]
        self.assertEqual(delays, [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])

    def test_reset_returns_to_first_delay(self):
        backoff = main_mod.Backoff()
        for _ in range(4):
            backoff.next()
        self.assertEqual(backoff.next(), 32.0)
        backoff.reset()
        self.assertEqual(backoff.attempt, 0)
        self.assertEqual(backoff.next(), 2.0)

    def test_peek_does_not_advance(self):
        backoff = main_mod.Backoff()
        self.assertEqual(backoff.peek(), 2.0)
        self.assertEqual(backoff.peek(), 2.0)
        self.assertEqual(backoff.attempt, 0)

    def test_retry_after_is_respected(self):
        backoff = main_mod.Backoff()
        self.assertEqual(backoff.next(retry_after=7), 7.0)
        # Второй раз сервер просит меньше, чем выросшая пауза, — берём паузу.
        self.assertEqual(backoff.next(retry_after=3), 4.0)

    def test_retry_after_is_capped(self):
        backoff = main_mod.Backoff()
        self.assertEqual(backoff.next(retry_after=10 ** 9), main_mod.RETRY_AFTER_MAX_S)

    def test_garbage_retry_after_is_ignored(self):
        for bad in (None, "abc", "", -5, 0, float("nan"), object()):
            backoff = main_mod.Backoff()
            self.assertEqual(backoff.next(retry_after=bad), 2.0, "retry_after=%r" % (bad,))

    def test_custom_base_and_factor(self):
        backoff = main_mod.Backoff(base=1.0, factor=3.0, cap=10.0)
        self.assertEqual([backoff.next() for _ in range(4)], [1.0, 3.0, 9.0, 10.0])

    def test_zero_base_keeps_zero(self):
        backoff = main_mod.Backoff(base=0.0, cap=30.0)
        self.assertEqual([backoff.next() for _ in range(3)], [0.0, 0.0, 0.0])

    def test_cap_is_not_lower_than_base(self):
        backoff = main_mod.Backoff(base=30.0, cap=5.0)
        self.assertEqual(backoff.next(), 30.0)

    def test_backoff_constants_match_docs(self):
        self.assertEqual(main_mod.BACKOFF_BASE_S, 2.0)
        self.assertEqual(main_mod.BACKOFF_CAP_S, 60.0)
        self.assertEqual(main_mod.POLL_TIMEOUT_S, 10)


# ===========================================================================
# 2. Баннер запуска и ротация в _setup_logging
# ===========================================================================
class StartupBannerTest(unittest.TestCase):
    def test_banner_has_version_pid_and_timezone(self):
        banner = main_mod._startup_banner({"app": {"timezone": "Europe/Moscow"}})
        self.assertIn("Life OS v%s" % main_mod.APP_VERSION, banner)
        self.assertIn("pid %d" % os.getpid(), banner)
        self.assertIn("Europe/Moscow", banner)
        self.assertIn("UTC+0300", banner)

    def test_banner_falls_back_on_broken_config(self):
        banner = main_mod._startup_banner({})
        self.assertIn("Life OS v", banner)
        self.assertIn("Europe/Moscow", banner)

    def test_banner_has_no_secrets(self):
        banner = main_mod._startup_banner({
            "app": {"timezone": "Europe/Moscow"},
            "telegram": {"token": "1234567890:AAHdemoToken"},
            "llm": {"api_key": "sk-demo-key"},
        })
        self.assertNotIn("AAHdemoToken", banner)
        self.assertNotIn("sk-demo", banner)


class LogRotationSettingsTest(unittest.TestCase):
    def test_rotation_keeps_exactly_one_archive(self):
        self.assertEqual(main_mod.LOG_BACKUP_COUNT, 1)
        self.assertEqual(main_mod.LOG_MAX_BYTES, 5 * 1024 * 1024)

    def test_setup_logging_uses_one_archive_and_5mb(self):
        tmp = tempfile.TemporaryDirectory(prefix="lifeos-log-")
        self.addCleanup(tmp.cleanup)
        cfg = _support.make_cfg(paths={
            "db": os.path.join(tmp.name, "db.sqlite"),
            "logs": os.path.join(tmp.name, "logs"),
            "dashboard_out": os.path.join(tmp.name, "index.html"),
        })
        root = logging.getLogger()
        try:
            main_mod._setup_logging(cfg)
            handlers = [h for h in root.handlers
                        if isinstance(h, logging.handlers.RotatingFileHandler)]
            self.assertEqual(len(handlers), 1)
            self.assertEqual(handlers[0].backupCount, 1)
            self.assertEqual(handlers[0].maxBytes, 5 * 1024 * 1024)
        finally:
            for handler in list(root.handlers):
                if getattr(handler, "_lifeos_managed", False):
                    root.removeHandler(handler)
                    try:
                        handler.close()
                    except Exception:  # noqa: BLE001
                        pass


# ===========================================================================
# 3. «Пульс» планировщика
# ===========================================================================
class SchedulerHeartbeatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-pulse-")
        self.addCleanup(self._tmp.cleanup)
        self.scheduler, self.storage = _quiet_scheduler(self._tmp.name)
        self.addCleanup(self.storage.close)

    def test_successful_tick_writes_heartbeat(self):
        now = _support.at(2026, 11, 17, 9, 0)
        self.scheduler.tick(now)
        self.assertEqual(
            self.storage.kv_get(scheduler_mod.HEARTBEAT_KEY),
            now.isoformat(timespec="seconds"),
        )

    def test_heartbeat_is_throttled(self):
        first = _support.at(2026, 11, 17, 9, 0)
        self.scheduler.tick(first)
        # Следующий тик через минуту, но писать в БД чаще раза в минуту незачем:
        # отметка времени не должна меняться, пока не истёк интервал.
        self.scheduler._heartbeat_at = time.monotonic()
        self.scheduler.tick(_support.at(2026, 11, 17, 9, 1))
        self.assertEqual(
            self.storage.kv_get(scheduler_mod.HEARTBEAT_KEY),
            first.isoformat(timespec="seconds"),
        )

    def test_heartbeat_advances_after_interval(self):
        self.scheduler.tick(_support.at(2026, 11, 17, 9, 0))
        self.scheduler._heartbeat_at = time.monotonic() - (
            scheduler_mod.HEARTBEAT_INTERVAL_S + 5
        )
        later = _support.at(2026, 11, 17, 9, 5)
        self.scheduler.tick(later)
        self.assertEqual(
            self.storage.kv_get(scheduler_mod.HEARTBEAT_KEY),
            later.isoformat(timespec="seconds"),
        )

    def test_failed_phase_does_not_raise_and_marks_error(self):
        self.scheduler.tick(_support.at(2026, 11, 17, 9, 0))
        good_stamp = self.storage.kv_get(scheduler_mod.HEARTBEAT_KEY)
        # Разрешаем записать пульс заново — и роняем одну фазу.
        self.scheduler._heartbeat_at = 0.0
        with mock.patch.object(self.scheduler, "_tick_slots",
                               side_effect=RuntimeError("boom")):
            self.scheduler.tick(_support.at(2026, 11, 17, 9, 10))  # не бросает
        error = self.storage.kv_get(scheduler_mod.TICK_ERROR_KEY) or ""
        self.assertIn("RuntimeError", error)
        # Пульс не сдвинулся: «последний успешный тик» остался прежним.
        self.assertEqual(self.storage.kv_get(scheduler_mod.HEARTBEAT_KEY), good_stamp)

    def test_error_is_cleared_by_next_success(self):
        self.storage.kv_set(scheduler_mod.TICK_ERROR_KEY, "2026-11-17T09:00:00 OSError")
        self.scheduler._heartbeat_at = 0.0
        self.scheduler.tick(_support.at(2026, 11, 17, 9, 5))
        self.assertIsNone(self.storage.kv_get(scheduler_mod.TICK_ERROR_KEY))

    def test_error_note_has_no_exception_text(self):
        self.scheduler._heartbeat_at = 0.0
        with mock.patch.object(
            self.scheduler, "_tick_rollover",
            side_effect=RuntimeError("token=1234567890:AAHsecret"),
        ):
            self.scheduler.tick(_support.at(2026, 11, 17, 9, 10))
        notes = (self.storage.kv_get(scheduler_mod.TICK_ERROR_KEY) or "") + \
            (self.storage.kv_get(scheduler_mod.HEARTBEAT_KEY) or "")
        self.assertNotIn("AAHsecret", notes)

    def test_heartbeat_survives_closed_storage(self):
        self.storage.close()
        # Тик на закрытой БД: ошибки не выходят наружу (main продолжает цикл).
        self.scheduler.tick(_support.at(2026, 11, 17, 9, 0))


# ===========================================================================
# 4. tools/health.py: проверки по отдельности
# ===========================================================================
class HealthHelpersTest(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(health.human_size(0), "0 Б")
        self.assertEqual(health.human_size(512), "512 Б")
        self.assertEqual(health.human_size(2048), "2.0 КБ")
        self.assertEqual(health.human_size(6 * 1024 * 1024), "6.0 МБ")
        self.assertEqual(health.human_size(None), "?")

    def test_short_time(self):
        self.assertEqual(health._short_time(0), "0 с")
        self.assertEqual(health._short_time(120), "2 мин")
        self.assertEqual(health._short_time(3600), "60 мин")
        self.assertEqual(health._short_time(5400), "1 ч 30 мин")
        self.assertEqual(health._short_time(3 * 3600 + 20 * 60), "3 ч 20 мин")
        self.assertEqual(health._short_time(72 * 3600), "3 дн")

    def test_parse_pid_file_formats(self):
        self.assertEqual(health.parse_pid_file('{"pid": 42, "started_at": "x"}'), 42)
        self.assertEqual(health.parse_pid_file("42\n2026-11-17T09:00:00+03:00\n"), 42)
        self.assertEqual(health.parse_pid_file("42"), 42)
        self.assertEqual(health.parse_pid_file(""), 0)
        self.assertEqual(health.parse_pid_file("мусор"), 0)
        self.assertEqual(health.parse_pid_file('{"pid": 0}'), 0)
        self.assertEqual(health.parse_pid_file('{"pid": "abc"}'), 0)

    def test_pid_alive_own_process(self):
        self.assertTrue(health.pid_alive(os.getpid()))
        self.assertFalse(health.pid_alive(0))
        self.assertFalse(health.pid_alive(999999))
        self.assertFalse(health.pid_alive("abc"))

    def test_check_python_ok(self):
        check = health.check_python()
        self.assertEqual(check.status, health.OK)
        self.assertIn("%d.%d" % sys.version_info[:2], check.detail)

    def test_check_python_old_version_is_fail(self):
        with mock.patch.object(health.sys, "version_info", (3, 8, 10)):
            check = health.check_python()
        self.assertEqual(check.status, health.FAIL)
        self.assertIn("3.8.10", check.detail)


class HealthConfigTest(unittest.TestCase):
    def _write_config(self, tmpdir, **patch):
        cfg = json.loads(json.dumps(config_mod.DEFAULTS))
        cfg["_root"] = str(_support.PROJECT_ROOT)
        cfg["telegram"]["token"] = "1234567890:AAHdemo"
        cfg["telegram"]["allowed_chat_id"] = CHAT
        cfg["llm"]["api_key"] = "sk-demo"
        cfg["paths"]["db"] = os.path.join(tmpdir, "lifeos.db")
        cfg["paths"]["logs"] = os.path.join(tmpdir, "logs")
        for key, value in patch.items():
            head, _, tail = key.partition("__")
            if tail:
                cfg[head][tail] = value
            else:
                cfg[key] = value
        path = os.path.join(tmpdir, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)
        return path

    def test_config_ok(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-cfg-") as tmp:
            path = self._write_config(tmp)
            check, cfg = health.check_config(path)
        self.assertEqual(check.status, health.OK, check.detail)
        self.assertIsNotNone(cfg)
        self.assertIn("chat_id %d" % CHAT, check.detail)

    def test_config_without_token_fails(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-cfg-") as tmp:
            path = self._write_config(tmp, telegram__token="")
            check, cfg = health.check_config(path)
        self.assertEqual(check.status, health.FAIL)
        self.assertIn("telegram.token", check.detail)
        self.assertIsNotNone(cfg)  # конфиг прочитался — остальное проверяем дальше

    def test_config_without_chat_id_fails(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-cfg-") as tmp:
            path = self._write_config(tmp, telegram__allowed_chat_id=0)
            check, _cfg = health.check_config(path)
        self.assertEqual(check.status, health.FAIL)
        self.assertIn("allowed_chat_id", check.detail)

    def test_config_without_llm_key_warns(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-cfg-") as tmp:
            path = self._write_config(tmp, llm__api_key="", llm__enabled=False)
            check, _cfg = health.check_config(path)
        self.assertEqual(check.status, health.WARN)
        self.assertIn("скриптованном режиме", check.detail)

    def test_missing_config_fails_with_hint(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-cfg-") as tmp:
            check, cfg = health.check_config(os.path.join(tmp, "нет.json"))
        self.assertEqual(check.status, health.FAIL)
        self.assertIsNone(cfg)
        self.assertTrue(check.detail)


class HealthStorageTest(unittest.TestCase):
    def _cfg(self, tmpdir):
        cfg = json.loads(json.dumps(config_mod.DEFAULTS))
        cfg["_root"] = str(_support.PROJECT_ROOT)
        cfg["paths"]["db"] = os.path.join(tmpdir, "lifeos.db")
        cfg["paths"]["logs"] = os.path.join(tmpdir, "logs")
        return cfg

    def test_absent_db_warns_and_creates_nothing(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-db-") as tmp:
            cfg = self._cfg(tmp)
            check, storage = health.check_storage(cfg)
            self.assertEqual(check.status, health.WARN, check.detail)
            self.assertIsNone(storage)
            self.assertFalse(os.path.exists(os.path.join(tmp, "lifeos.db")))

    def test_db_write_and_read_ok(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-db-") as tmp:
            cfg = self._cfg(tmp)
            path = os.path.join(tmp, "lifeos.db")
            seed = storage_mod.Storage(path)
            seed.kv_set("session", "{}")
            seed.close()
            check, storage = health.check_storage(cfg)
            try:
                self.assertEqual(check.status, health.OK, check.detail)
                self.assertIsNotNone(storage)
                # Проба не осталась в базе.
                self.assertIsNone(storage.kv_get(health.KV_PROBE))
                self.assertEqual(storage.kv_get("session"), "{}")
            finally:
                storage.close()

    def test_broken_db_file_fails(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-db-") as tmp:
            cfg = self._cfg(tmp)
            path = os.path.join(tmp, "lifeos.db")
            with open(path, "wb") as fh:
                fh.write("это не sqlite, а мусор".encode("utf-8") * 8)
            # sqlite3.connect на битом файле оставляет незакрытое соединение —
            # это шум ResourceWarning из stdlib, к проверке отношения не имеет.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                check, storage = health.check_storage(cfg)
                gc.collect()
        self.assertEqual(check.status, health.FAIL)
        self.assertIsNone(storage)
        self.assertIn("не открывается", check.detail)

    def test_db_path_is_directory_fails(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-db-") as tmp:
            cfg = self._cfg(tmp)
            os.makedirs(os.path.join(tmp, "lifeos.db"))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                check, _storage = health.check_storage(cfg)
                gc.collect()
        self.assertEqual(check.status, health.FAIL)


class HealthSchedulerTest(unittest.TestCase):
    def _root_with_lock(self, payload):
        tmp = tempfile.TemporaryDirectory(prefix="lifeos-lock-")
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name) / "data"
        data.mkdir()
        (data / "lifeos.pid").write_text(payload, encoding="utf-8")
        return tmp.name

    def test_no_pid_file_warns(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-lock-") as tmp:
            with mock.patch.object(health, "launchd_state", return_value=(False, 0, None)):
                check, running = health.check_scheduler(tmp)
        self.assertEqual(check.status, health.WARN)
        self.assertFalse(running)
        self.assertIn("pid-файла нет", check.detail)
        self.assertIn("не загружен", check.detail)

    def test_dead_pid_warns(self):
        root = self._root_with_lock('{"pid": 999999, "started_at": "2026-11-17T09:00:00"}')
        with mock.patch.object(health, "launchd_state", return_value=(False, 0, None)):
            check, running = health.check_scheduler(root)
        self.assertEqual(check.status, health.WARN)
        self.assertFalse(running)
        self.assertIn("не работает", check.detail)

    def test_live_agent_ok(self):
        root = self._root_with_lock('{"pid": %d}' % os.getpid())
        with mock.patch.object(health, "launchd_state", return_value=(True, os.getpid(), 0)), \
                mock.patch.object(health, "_proc_command", return_value="python3 src/main.py"):
            check, running = health.check_scheduler(root)
        self.assertTrue(running)
        self.assertEqual(check.status, health.OK, check.detail)
        self.assertIn("агент работает", check.detail)
        self.assertIn("launchd: загружен", check.detail)

    def test_foreign_pid_is_not_agent(self):
        root = self._root_with_lock('{"pid": %d}' % os.getpid())
        with mock.patch.object(health, "launchd_state", return_value=(False, 0, None)), \
                mock.patch.object(health, "_proc_command", return_value="/usr/bin/vim notes.md"):
            check, running = health.check_scheduler(root)
        self.assertFalse(running)
        self.assertEqual(check.status, health.WARN)

    def test_crashing_under_launchd_fails(self):
        root = self._root_with_lock("42\n2026-11-17T09:00:00+03:00\n")
        with mock.patch.object(health, "launchd_state", return_value=(True, 0, 1)), \
                mock.patch.object(health, "agent_running", return_value=False):
            check, running = health.check_scheduler(root)
        self.assertEqual(check.status, health.FAIL)
        self.assertFalse(running)
        self.assertIn("кодом 1", check.detail)

    def test_launchd_unknown_is_not_an_error(self):
        root = self._root_with_lock('{"pid": %d}' % os.getpid())
        with mock.patch.object(health, "launchd_state", return_value=None), \
                mock.patch.object(health, "agent_running", return_value=True):
            check, running = health.check_scheduler(root)
        self.assertEqual(check.status, health.OK)
        self.assertTrue(running)
        self.assertIn("неизвестно", check.detail)


class HealthLastTickTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-tick-")
        self.addCleanup(self._tmp.cleanup)
        self.storage = storage_mod.Storage(os.path.join(self._tmp.name, "db.sqlite"))
        self.addCleanup(self.storage.close)

    def _set_heartbeat(self, delta_seconds):
        moment = datetime.now().astimezone() - timedelta(seconds=delta_seconds)
        self.storage.kv_set(health.KV_HEARTBEAT, moment.isoformat(timespec="seconds"))
        return moment

    def test_recent_tick_ok(self):
        self._set_heartbeat(30)
        check = health.check_last_tick(self.storage, running=True)
        self.assertEqual(check.status, health.OK, check.detail)
        self.assertIn("назад", check.detail)

    def test_no_heartbeat_warns(self):
        check = health.check_last_tick(self.storage, running=True)
        self.assertEqual(check.status, health.WARN)
        self.assertIn("отметок нет", check.detail)

    def test_stale_tick_while_running_fails(self):
        self._set_heartbeat(3600)
        check = health.check_last_tick(self.storage, running=True)
        self.assertEqual(check.status, health.FAIL)
        self.assertIn("завис", check.detail)

    def test_stale_tick_while_stopped_warns(self):
        self._set_heartbeat(3600)
        check = health.check_last_tick(self.storage, running=False)
        self.assertEqual(check.status, health.WARN)
        self.assertIn("не работает", check.detail)

    def test_error_note_is_shown(self):
        self._set_heartbeat(30)
        self.storage.kv_set(health.KV_TICK_ERROR, "2026-11-17T09:00:00 RuntimeError")
        check = health.check_last_tick(self.storage, running=True)
        self.assertIn("последняя ошибка тика", check.detail)
        self.assertIn("RuntimeError", check.detail)

    def test_without_storage_warns(self):
        check = health.check_last_tick(None, running=True)
        self.assertEqual(check.status, health.WARN)


class HealthLogsTest(unittest.TestCase):
    def _cfg(self, logs_dir):
        cfg = json.loads(json.dumps(config_mod.DEFAULTS))
        cfg["_root"] = str(_support.PROJECT_ROOT)
        cfg["paths"]["logs"] = logs_dir
        return cfg

    def test_no_logs_warns(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-logs-") as tmp:
            check, over = health.check_logs(self._cfg(tmp))
        self.assertEqual(check.status, health.WARN)
        self.assertEqual(over, [])

    def test_small_log_ok(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-logs-") as tmp:
            with open(os.path.join(tmp, "agent.log"), "w", encoding="utf-8") as fh:
                fh.write("строка лога\n")
            check, over = health.check_logs(self._cfg(tmp))
        self.assertEqual(check.status, health.OK, check.detail)
        self.assertIn("agent.log", check.detail)
        self.assertEqual(over, [])

    def test_oversized_log_warns_about_rotation(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-logs-") as tmp:
            path = os.path.join(tmp, "agent.log")
            with open(path, "wb") as fh:
                fh.truncate(health.LOG_ROTATE_BYTES + 1024)
            check, over = health.check_logs(self._cfg(tmp))
        self.assertEqual(check.status, health.WARN)
        self.assertIn("--rotate", check.detail)
        self.assertEqual(len(over), 1)

    def test_archive_is_not_counted_as_oversized(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-logs-") as tmp:
            with open(os.path.join(tmp, "agent.log"), "w", encoding="utf-8") as fh:
                fh.write("немного\n")
            with open(os.path.join(tmp, "agent.log.1"), "wb") as fh:
                fh.truncate(health.LOG_ROTATE_BYTES + 1024)
            check, over = health.check_logs(self._cfg(tmp))
        self.assertEqual(check.status, health.OK, check.detail)
        self.assertEqual(over, [])
        self.assertIn("архив", check.detail)


class RotateLogTest(unittest.TestCase):
    def test_small_file_is_untouched(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-rot-") as tmp:
            path = os.path.join(tmp, "agent.log")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("мало данных\n")
            self.assertFalse(health.rotate_log(path))
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".1"))

    def test_oversized_file_becomes_single_archive(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-rot-") as tmp:
            path = os.path.join(tmp, "agent.log")
            with open(path, "wb") as fh:
                fh.truncate(health.LOG_ROTATE_BYTES + 1)
            self.assertTrue(health.rotate_log(path))
            self.assertEqual(os.path.getsize(path), 0)
            self.assertEqual(os.path.getsize(path + ".1"), health.LOG_ROTATE_BYTES + 1)
            names = sorted(os.listdir(tmp))
            self.assertEqual(names, ["agent.log", "agent.log.1"])

    def test_second_rotation_replaces_archive(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-rot-") as tmp:
            path = os.path.join(tmp, "agent.log")
            for _ in range(2):
                with open(path, "wb") as fh:
                    fh.truncate(health.LOG_ROTATE_BYTES + 1)
                self.assertTrue(health.rotate_log(path))
            names = sorted(os.listdir(tmp))
            # Архив ровно один: agent.log + agent.log.1, без .2 и .1.1.
            self.assertEqual(names, ["agent.log", "agent.log.1"])

    def test_new_log_is_owner_only(self):
        if os.name != "posix":  # режимы файлов есть только в POSIX
            self.skipTest("режимы файлов проверяются в POSIX")
        with tempfile.TemporaryDirectory(prefix="lifeos-rot-") as tmp:
            path = os.path.join(tmp, "agent.log")
            with open(path, "wb") as fh:
                fh.truncate(health.LOG_ROTATE_BYTES + 1)
            self.assertTrue(health.rotate_log(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-rot-") as tmp:
            self.assertFalse(health.rotate_log(os.path.join(tmp, "нет.log")))

    def test_rotation_refused_while_agent_running(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-rot-") as tmp:
            cfg = json.loads(json.dumps(config_mod.DEFAULTS))
            cfg["_root"] = str(_support.PROJECT_ROOT)
            cfg["paths"]["logs"] = tmp
            with open(os.path.join(tmp, "agent.log"), "wb") as fh:
                fh.truncate(health.LOG_ROTATE_BYTES + 1)
            actions = health.perform_rotation(cfg, running=True)
            self.assertTrue(actions)
            self.assertIn("не трогаю", actions[0])
            self.assertFalse(os.path.exists(os.path.join(tmp, "agent.log.1")))
            # А если бот стоит — крутим.
            actions = health.perform_rotation(cfg, running=False)
            self.assertTrue(os.path.exists(os.path.join(tmp, "agent.log.1")))
            self.assertIn("→", actions[0])


class HealthCommandTest(unittest.TestCase):
    """Полный прогон tools/health.py отдельным процессом: вывод и код выхода."""

    def _run(self, args):
        env = dict(os.environ)
        env["PYTHONPYCACHEPREFIX"] = tempfile.mkdtemp(prefix="lifeos-pyc-")
        return subprocess.run(
            [sys.executable, str(Path(_support.PROJECT_ROOT) / "tools" / "health.py")] + args,
            capture_output=True, text=True, timeout=60, env=env,
            cwd=str(_support.PROJECT_ROOT),
        )

    def _write_config(self, tmpdir, token="1234567890:AAHdemo"):
        cfg = json.loads(json.dumps(config_mod.DEFAULTS))
        cfg["_root"] = str(_support.PROJECT_ROOT)
        cfg["telegram"]["token"] = token
        cfg["telegram"]["allowed_chat_id"] = CHAT
        cfg["llm"]["api_key"] = "sk-demo"
        cfg["paths"]["db"] = os.path.join(tmpdir, "lifeos.db")
        cfg["paths"]["logs"] = os.path.join(tmpdir, "logs")
        cfg["paths"]["dashboard_out"] = os.path.join(tmpdir, "index.html")
        # Пустая база уже существует — иначе проверка честно скажет «ещё нет».
        storage_mod.Storage(cfg["paths"]["db"]).close()
        path = os.path.join(tmpdir, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)
        return path

    def test_valid_config_exits_zero(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-health-") as tmp:
            result = self._run(["--config", self._write_config(tmp)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[ OK ] python", result.stdout)
        self.assertIn("[ OK ] конфиг", result.stdout)
        self.assertIn("[ OK ] база", result.stdout)
        self.assertNotIn("[FAIL]", result.stdout)

    def test_empty_token_exits_one(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-health-") as tmp:
            result = self._run(["--config", self._write_config(tmp, token="")])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[FAIL] конфиг", result.stdout)
        self.assertIn("ПРОБЛЕМЫ", result.stdout)

    def test_missing_config_exits_one(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-health-") as tmp:
            result = self._run(["--config", os.path.join(tmp, "нет-такого.json")])
        self.assertEqual(result.returncode, 1)
        self.assertIn("[FAIL] конфиг", result.stdout)

    def test_output_has_no_secrets(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-health-") as tmp:
            result = self._run(["--config", self._write_config(
                tmp, token="1234567890:AAHdemoTokenValue")])
        self.assertNotIn("AAHdemoTokenValue", result.stdout + result.stderr)
        self.assertNotIn("sk-demo", result.stdout + result.stderr)


class HealthCollectTest(unittest.TestCase):
    def test_collect_reports_python_and_config_without_cfg(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-health-") as tmp:
            checks, cfg, rotations = health.collect(os.path.join(tmp, "нет.json"))
        self.assertIsNone(cfg)
        self.assertEqual(rotations, [])
        self.assertEqual([check.name for check in checks], ["python", "конфиг"])
        self.assertEqual(checks[1].status, health.FAIL)

    def test_collect_runs_all_checks_with_valid_config(self):
        with tempfile.TemporaryDirectory(prefix="lifeos-health-") as tmp:
            cfg = json.loads(json.dumps(config_mod.DEFAULTS))
            cfg["_root"] = str(_support.PROJECT_ROOT)
            cfg["telegram"]["token"] = "1234567890:AAHdemo"
            cfg["telegram"]["allowed_chat_id"] = CHAT
            cfg["llm"]["api_key"] = "sk-demo"
            cfg["paths"]["db"] = os.path.join(tmp, "lifeos.db")
            cfg["paths"]["logs"] = os.path.join(tmp, "logs")
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False)
            checks, loaded, _rotations = health.collect(path)
        names = [check.name for check in checks]
        self.assertEqual(
            names, ["python", "конфиг", "планировщик", "база", "последний тик", "логи"])
        self.assertIsNotNone(loaded)


if __name__ == "__main__":
    unittest.main()
