#!/usr/bin/env python3
"""E2E Life OS на мок-сервере Telegram (без реального Telegram и LLM).

Сценарий (ТЗ §8, INTERFACES §11):

1. поднимаем ``MockTelegramServer`` и временную Obsidian-базу в ``tests/tmp``;
2. пишем временный конфиг: ``llm.enabled=false``, утренний слот — «сейчас»,
   остальные слоты выключены, vault/db/logs/dashboard — во временном каталоге;
3. запускаем ``python3 src/main.py --config <temp> --mock-url <mock>`` отдельным
   процессом и ждём, пока бот начнёт утреннюю сессию;
4. отвечаем на все вопросы (список вопросов берём из ``src/prompts.py``);
5. команды: /status, /today, /help, /pause, /resume, /add_goal, /дизайн;
6. проверяем: daily-заметка с маркерами записей, ``entries.written=1``,
   ``goals.md`` с таблицей целей, дашборд с ``window.LIFEOS_DATA`` и данными;
7. SIGTERM, повторный запуск: нет дублей записей и маркеров, сессия не стартует;
8. ещё одна проверка устойчивости: временная 500-ошибка мок-сервера — процесс жив.

Запуск (из корня проекта):
    python3 tests/e2e_mock.py            # краткий отчёт и код 0/1
    python3 tests/e2e_mock.py -v         # подробный вывод шагов
    python3 tests/e2e_mock.py --keep     # не удалять tests/tmp/e2e-* (разбор)

Всё офлайн; живая Obsidian-база не затрагивается. Временный каталог удаляется.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import mock_telegram  # noqa: E402
from src import prompts  # noqa: E402
from src import obsidian as obsidian_mod  # noqa: E402

try:
    from zoneinfo import ZoneInfo

    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover - на modern macOS zoneinfo есть
    MSK = None  # type: ignore[assignment]

TOKEN = "123456:E2E-TOKEN"
CHAT_ID = 777000111

#: Общий таймаут на один «дождись сообщения бота», секунды.
WAIT_BOT = 30.0


class E2EFailure(AssertionError):
    """Провал проверки e2e (с человекочитаемым сообщением)."""


class E2ERunner:
    def __init__(self, verbose: bool = False, keep: bool = False) -> None:
        self.verbose = verbose
        self.keep = keep
        self.checks: list[tuple[str, bool, str]] = []
        self.server: mock_telegram.MockTelegramServer | None = None
        self.process: subprocess.Popen | None = None
        self.tmp: Path | None = None
        self.run_root: Path | None = None
        self.db_path: Path | None = None
        self.dashboard_path: Path | None = None
        self.vault_path: Path | None = None
        self.daily_path: Path | None = None
        self.goals_path: Path | None = None
        self.config_path: Path | None = None
        self.sent_marker = 0

    # ------------------------------------------------------------- утилиты
    def log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        """Регистрирует проверку и падает, если она не прошла."""
        self.checks.append((name, bool(condition), detail))
        mark = "✓" if condition else "✗"
        line = "  %s %s" % (mark, name)
        if detail:
            line += " — %s" % detail
        print(line, flush=True)
        if not condition:
            raise E2EFailure("Проверка не пройдена: %s (%s)" % (name, detail))

    # ---------------------------------------------------------- подготовка
    def prepare_environment(self) -> None:
        """Временные каталоги, vault-шаблон, конфиг."""
        tests_tmp = PROJECT_ROOT / "tests" / "tmp"
        tests_tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tests_tmp / ("e2e-%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
        # Изолированная копия проекта: pid-лок и пути main.py считает от _ROOT
        # (родитель src/), поэтому во временной копии мы не мешаем другим e2e
        # и не оставляем следов в корне проекта.
        self.run_root = self.tmp / "run"
        self.run_root.mkdir(parents=True)
        shutil.copytree(
            PROJECT_ROOT / "src",
            self.run_root / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        template_src = PROJECT_ROOT / "dashboard" / "template.html"
        if template_src.is_file():
            (self.run_root / "dashboard").mkdir(parents=True)
            shutil.copy2(template_src, self.run_root / "dashboard" / "template.html")
        self.vault_path = self.tmp / "vault"
        (self.vault_path / "Шаблоны").mkdir(parents=True)
        (self.vault_path / "Шаблоны" / "Шаблон - День.md").write_text(
            "---\n"
            "tags:\n"
            "  - дневник\n"
            "---\n"
            "# {{date:DD.MM.YYYY}}\n"
            "\n"
            "## Главное\n"
            "\n"
            "## Мысли\n"
            "пользовательский текст, который нельзя терять\n",
            encoding="utf-8",
        )
        self.dashboard_path = self.tmp / "dashboard" / "index.html"
        self.db_path = self.tmp / "data" / "lifeos.db"
        self.goals_path = self.vault_path / "Проекты" / "Life OS" / "Цели.md"
        self.daily_path = (
            self.vault_path / "01 Daily" / (self.today() + ".md")
        )

        now = datetime.now(MSK) if MSK else datetime.now()
        schedule_time = now.strftime("%H:%M")
        config = {
            "telegram": {
                "token": TOKEN,
                "allowed_chat_id": CHAT_ID,
                "api_base": "https://api.telegram.org",  # будет подменён --mock-url
            },
            "obsidian": {
                "enabled": True,
                "vault_path": str(self.vault_path),
                "daily_folder": "01 Daily",
                "template_path": "Шаблоны/Шаблон - День.md",
                "goals_path": "Проекты/Life OS/Цели.md",
                "data_folder": "Life OS/data",
                "write_mirror": True,
            },
            "llm": {
                "enabled": False,           # обязательный режим e2e: без LLM
                "base_url": "http://127.0.0.1:9/v1",  # заведомо недоступен
                "model": "deepseek-v4.1-flash-1017",
                "api_key": "",
                "timeout": 5,
                "max_followups": 0,
            },
            "schedule": {
                "morning": {"time": schedule_time, "enabled": True},
                "midday": {"time": "14:00", "enabled": False},
                "evening": {"time": "21:30", "enabled": False},
                "weekly": {"time": "21:00", "weekday": 6, "enabled": False},
                "catchup_minutes": 180,
            },
            "paths": {
                "db": str(self.db_path),
                "logs": str(self.tmp / "data" / "logs"),
                "dashboard_out": str(self.dashboard_path),
            },
            "app": {
                "name": "Life OS",
                "timezone": "Europe/Moscow",
                "lang": "ru",
            },
        }
        self.config_path = self.tmp / "config.json"
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.log("  временный каталог: %s" % self.tmp)
        self.log("  изолированная копия проекта: %s" % self.run_root)
        self.log("  утренний слот: %s, vault: %s" % (schedule_time, self.vault_path))

    @staticmethod
    def today() -> str:
        now = datetime.now(MSK) if MSK else datetime.now()
        return now.date().isoformat()

    # --------------------------------------------------------------- запуск
    def start_bot(self) -> None:
        assert self.config_path is not None and self.server is not None
        env = dict(os.environ)
        env.pop("LIFEOS_CONFIG", None)
        self.process = subprocess.Popen(
            [sys.executable, "src/main.py",
             "--config", str(self.config_path),
             "--mock-url", self.server.url],
            cwd=str(self.run_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        # Ждём первое сообщение бота: это старт утреннего слота.
        texts = self.wait_for_new_messages(timeout=WAIT_BOT)
        self.check("бот начал утреннюю сессию", bool(texts),
                   "получено сообщений: %d" % len(texts))
        questions = prompts.scripted_questions("morning")
        self.check("первый вопрос сессии задан", questions and questions[0] in texts[0],
                   (texts[0][:80] + "…") if texts else "нет сообщений")

    def wait_for_new_messages(self, timeout: float = WAIT_BOT,
                              count: int = 1) -> list[str]:
        """Ждёт ``count`` новых (относительно sent_marker) сообщений бота."""
        assert self.server is not None
        target = self.sent_marker + count
        texts = self.server.wait_sent(timeout=timeout, min_count=target)
        if len(texts) < target:
            raise E2EFailure(
                "бот не ответил за %.1f c (ожидались сообщения #%d..#%d, всего %d)"
                % (timeout, self.sent_marker + 1, target, len(texts))
            )
        new = texts[self.sent_marker:]
        self.sent_marker = len(texts)
        return new

    def send(self, text: str) -> None:
        """Пользователь пишет боту (апдейт в мок-сервер)."""
        assert self.server is not None
        self.server.push_update(text, chat_id=CHAT_ID)
        self.log("  → %s" % text.replace("\n", " ")[:100])

    def exchange(self, text: str, timeout: float = WAIT_BOT) -> str:
        """Отправить сообщение и дождаться ответа бота."""
        self.send(text)
        messages = self.wait_for_new_messages(timeout=timeout)
        reply = messages[-1]
        self.log("  ← %s" % reply.replace("\n", " ")[:140])
        return reply

    # -------------------------------------------------------------- сессия
    def run_dialog(self) -> list[str]:
        """Отвечает на все скриптованные вопросы утреннего слота."""
        questions = prompts.scripted_questions("morning")
        answers = [
            "План: написать отчёт, провести встречу и вечером потренироваться",
            "Три задачи: 1) отчёт 2) встреча 3) тренировка вечером",
            "Цели: продвинуться по проекту Life OS и закрыть этап тестов",
            "Свободный ответ про самочувствие — настроение 4 из 5, энергия 3",
            "Что ещё: воды выпил 500 мл, спал 7 часов",
            "Главное на завтра: закончить отчёт и разобрать почту",
        ]
        for index, question in enumerate(questions):
            if index > 0:
                # Проверяем, что бот действительно задал следующий вопрос.
                last = self.server.sent()[-1] if self.server else ""
                self.check(
                    "вопрос %d из %d задан" % (index + 1, len(questions)),
                    question in last,
                    last.replace("\n", " ")[:80],
                )
            reply = self.exchange(answers[index % len(answers)])
            if index == len(questions) - 1:
                self.check("сессия завершена (закрывающий текст)",
                           "Спасибо" in reply, reply.replace("\n", " ")[:80])
        return answers

    def run_commands(self) -> None:
        reply = self.exchange("/status")
        self.check("/status отвечает", "статус" in reply.lower(), reply[:60])
        reply = self.exchange("/today")
        self.check("/today отвечает", "Сегодня" in reply, reply[:60])
        reply = self.exchange("/help")
        self.check("/help отвечает", "Команды Life OS" in reply, reply[:60])
        reply = self.exchange("/pause")
        self.check("/pause включает паузу", "Пауза включена" in reply, reply[:60])
        reply = self.exchange("/resume")
        self.check("/resume снимает паузу", "работаю по расписанию" in reply, reply[:60])
        reply = self.exchange("/add_goal Пробежать 10 км | 3/10 | 2027-06-01")
        self.check("цель добавлена", "Цель добавлена" in reply, reply[:60])
        reply = self.exchange("/дизайн")
        self.check("дашборд пересобран", "Дашборд обновлён" in reply, reply[:60])

    # ------------------------------------------------------------ проверки
    def db_entries(self) -> list[dict]:
        """Читает entries из SQLite, пока процесс жив (read-only URI)."""
        assert self.db_path is not None
        conn = sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, date, ts, slot, category, title, text, written "
                "FROM entries ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def wait_entries(self, expected: int, timeout: float = 15.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        rows: list[dict] = []
        while time.monotonic() < deadline:
            rows = self.db_entries()
            if len(rows) >= expected:
                return rows
            time.sleep(0.2)
        return rows

    def check_database(self, expected_entries: int) -> list[dict]:
        rows = self.wait_entries(expected_entries)
        self.check("в БД %d записей (ответы на вопросы)" % expected_entries,
                   len(rows) == expected_entries,
                   "фактически: %d" % len(rows))
        self.check("все записи written=1 (попали в Obsidian)",
                   all(int(row["written"] or 0) == 1 for row in rows),
                   "written: %s" % [row["written"] for row in rows])
        self.check("записи сохранены со слотом morning",
                   all(row["slot"] == "morning" for row in rows),
                   "слоты: %s" % sorted({row["slot"] for row in rows}))
        self.check("у записей есть заголовки и тексты",
                   all((row["title"] or "").strip() and (row["text"] or "").strip()
                       for row in rows))
        return rows

    def check_daily_note(self, expected_entries: int) -> str:
        assert self.daily_path is not None
        self.check("daily-заметка создана", self.daily_path.is_file(),
                   str(self.daily_path))
        text = self.daily_path.read_text(encoding="utf-8")
        markers = text.count("<!-- lifeos:entry:")
        self.check("в дневнике %d маркеров записей" % expected_entries,
                   markers == expected_entries, "маркеров: %d" % markers)
        self.check("daily создан из шаблона (дата подставлена)",
                   datetime.strptime(self.today(), "%Y-%m-%d").strftime("%d.%m.%Y") in text,
                   "дата %s" % self.today())
        self.check("пользовательский текст шаблона сохранён",
                   "пользовательский текст, который нельзя терять" in text)
        self.check("записи попали в разделы «(агент)»",
                   "## Задачи (агент)" in text or "## Цели (агент)" in text,
                   "разделы: " + ", ".join(
                       line.strip() for line in text.splitlines()
                       if line.startswith("## ")))
        return text

    def check_goals_file(self) -> str:
        assert self.goals_path is not None
        self.check("goals.md создан после /add_goal", self.goals_path.is_file(),
                   str(self.goals_path))
        text = self.goals_path.read_text(encoding="utf-8")
        self.check("в goals.md маркеры блока целей",
                   obsidian_mod.GOALS_START in text and obsidian_mod.GOALS_END in text)
        self.check("цель с вики-ссылкой и прогрессом",
                   "[[Пробежать 10 км]]" in text and "0%" in text,
                   text[:120].replace("\n", " | "))
        return text

    def check_mirror(self, expected_entries: int) -> None:
        assert self.vault_path is not None
        folder = self.vault_path / "Life OS" / "data"
        entries_file = folder / "entries.jsonl"
        goals_file = folder / "goals.json"
        self.check("зеркало entries.jsonl создано", entries_file.is_file(),
                   str(entries_file))
        lines = [line for line in entries_file.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        self.check("в зеркале %d записей" % expected_entries,
                   len(lines) == expected_entries, "строк: %d" % len(lines))
        parsed = [json.loads(line) for line in lines]
        self.check("metadata metrics в зеркале — dict",
                   all(isinstance(item.get("metrics"), dict) for item in parsed))
        self.check("зеркало goals.json создано", goals_file.is_file())
        goals = json.loads(goals_file.read_text(encoding="utf-8"))
        self.check("в goals.json есть цель", isinstance(goals, list) and bool(goals))

    def check_dashboard(self) -> None:
        assert self.dashboard_path is not None
        self.check("dashboard/index.html собран", self.dashboard_path.is_file(),
                   str(self.dashboard_path))
        page = self.dashboard_path.read_text(encoding="utf-8")
        self.check("в дашборде есть window.LIFEOS_DATA", "LIFEOS_DATA" in page)
        self.check("плейсхолдеры шаблона заменены", "{{" not in page)
        import re as _re
        self.check("в HTML нет NaN в данных",
                   _re.search(r"\bNaN\b", page) is None)
        data_path = self.dashboard_path.parent / "data.json"
        self.check("рядом лежит data.json", data_path.is_file())
        data = json.loads(data_path.read_text(encoding="utf-8"))
        self.check("в дашборде непустой фид", bool(data.get("feed")), str(data.get("feed"))[:120])
        self.check("в дашборде есть цели", bool(data.get("goals")),
                   str([goal.get("title") for goal in data.get("goals") or []]))
        self.check("серии из 14 дней", len(data.get("series", {}).get("dates", [])) == 14)
        self.check("хитмап 8 недель", len(data.get("heatmap", {}).get("cells", [])) == 56)

    # ------------------------------------------------------- остановка/старт
    def stop_bot(self, timeout: float = 20.0) -> int:
        assert self.process is not None
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = self.process.wait(timeout=5)
            raise E2EFailure("процесс не завершился по SIGTERM за %.0f c" % timeout)
        stderr = ""
        try:
            _stdout, stderr = self.process.communicate(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        self.log("  процесс завершился с кодом %s" % code)
        if stderr and self.verbose:
            tail = stderr.strip().splitlines()[-5:]
            self.log("  stderr: " + " | ".join(tail))
        return code

    def wait_queue_drained(self, timeout: float = 8.0) -> bool:
        """Ждёт, пока main подтвердит все апдейты (offset) — очередь мока пуста.

        В реальном Telegram неподтверждённые апдейты переотдаются после
        рестарта, поэтому перед SIGTERM даём циклу сделать ещё один
        getUpdates с offset.
        """
        assert self.server is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.server.pending_updates():
                return True
            time.sleep(0.2)
        return False

    def check_process_alive(self) -> None:
        assert self.process is not None
        code = self.process.poll()
        if code is not None:
            stderr = ""
            try:
                _stdout, stderr = self.process.communicate(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            raise E2EFailure(
                "процесс бота завершился раньше времени (код %s): %s"
                % (code, (stderr or "").strip()[-500:])
            )

    # ------------------------------------------------------------- сценарии
    def run(self) -> int:
        print("E2E Life OS на мок-сервере Telegram")
        print("====================================")
        try:
            self.prepare_environment()
            self.server = mock_telegram.MockTelegramServer(token=TOKEN)
            self.server.start()
            self.log("  мок Telegram: %s" % self.server.url)

            print("1. Первый запуск: утренняя сессия без LLM")
            self.start_bot()
            self.check_process_alive()
            answers = self.run_dialog()
            expected_entries = len(prompts.scripted_questions("morning"))
            self.check_database(expected_entries)
            daily_text = self.check_daily_note(expected_entries)

            print("2. Команды и данные")
            self.run_commands()
            self.check_process_alive()
            self.check_goals_file()
            self.check_mirror(expected_entries)
            self.check_dashboard()
            # /pause был выставлен и снят; проверим, что бот не на паузе
            reply = self.exchange("/status")
            self.check("после /resume бот не на паузе", "на паузе" not in reply.lower(),
                       reply[:80])

            print("3. Устойчивость к ошибке Telegram (500)")
            self.server.mode = "500"
            time.sleep(2.5)
            self.server.mode = None
            self.check_process_alive()
            reply = self.exchange("/help")
            self.check("после временной 500-ошибки бот работает",
                       "Команды Life OS" in reply, reply[:60])

            print("4. Остановка по SIGTERM и повторный запуск")
            self.check("все апдейты подтверждены (offset), очередь мока пуста",
                       self.wait_queue_drained())
            code = self.stop_bot()
            self.check("SIGTERM завершает процесс с кодом 0", code == 0, "код: %s" % code)
            entries_before = len(self.db_entries())
            markers_before = daily_text.count("<!-- lifeos:entry:")

            baseline = len(self.server.sent())
            self.start_process_only()
            # повторный запуск не должен начинать сессию заново
            time.sleep(3.0)
            self.check_process_alive()
            repeated = self.server.sent()[baseline:]
            self.check("повторный запуск не дублирует слот",
                       not repeated,
                       "лишние сообщения: %r" % (repeated[:3],))
            self.sent_marker = baseline
            reply = self.exchange("/status")
            self.check("после рестарта /status работает",
                       "статус" in reply.lower(), reply[:60])
            entries_after = len(self.db_entries())
            self.check("повторный запуск не дублирует записи",
                       entries_after == entries_before,
                       "было %d, стало %d" % (entries_before, entries_after))
            daily_after = self.daily_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.check("повторный запуск не дублирует маркеры в дневнике",
                       daily_after.count("<!-- lifeos:entry:") == markers_before,
                       "было %d, стало %d" % (
                           markers_before, daily_after.count("<!-- lifeos:entry:")))
            code = self.stop_bot()
            self.check("второй SIGTERM — код 0", code == 0, "код: %s" % code)
            return 0
        except E2EFailure as exc:
            print("\n❌ E2E ПРОВАЛ: %s" % exc)
            self._dump_logs()
            return 1
        except Exception:  # noqa: BLE001
            print("\n❌ E2E УПАЛ С ИСКЛЮЧЕНИЕМ:")
            traceback.print_exc()
            self._dump_logs()
            return 1
        finally:
            self.cleanup()

    # -------------------------------------------------------------- сервис
    def start_process_only(self) -> None:
        """Запуск процесса без ожидания первого сообщения (рестарт)."""
        assert self.config_path is not None and self.server is not None
        env = dict(os.environ)
        env.pop("LIFEOS_CONFIG", None)
        self.process = subprocess.Popen(
            [sys.executable, "src/main.py",
             "--config", str(self.config_path),
             "--mock-url", self.server.url],
            cwd=str(self.run_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    def _dump_logs(self) -> None:
        log_path = (self.tmp / "data" / "logs" / "agent.log") if self.tmp else None
        if log_path and log_path.is_file():
            print("\n--- agent.log (последние 60 строк) ---")
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            print("\n".join(lines[-60:]))
        if self.process is not None and self.process.poll() is not None:
            try:
                _stdout, stderr = self.process.communicate(timeout=2)
                if stderr:
                    print("\n--- stderr процесса ---")
                    print(stderr[-2000:])
            except Exception:  # noqa: BLE001
                pass

    def cleanup(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.tmp is not None and not self.keep:
            shutil.rmtree(self.tmp, ignore_errors=True)
        elif self.tmp is not None and self.keep:
            print("\n(временный каталог сохранён: %s)" % self.tmp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="E2E Life OS на мок-сервере Telegram (офлайн)."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный вывод")
    parser.add_argument("--keep", action="store_true",
                        help="не удалять временный каталог tests/tmp/e2e-*")
    args = parser.parse_args(argv)

    runner = E2ERunner(verbose=args.verbose, keep=args.keep)
    code = runner.run()

    print("\n===== ИТОГ =====")
    passed = sum(1 for _name, ok, _detail in runner.checks if ok)
    total = len(runner.checks)
    print("Проверок пройдено: %d из %d" % (passed, total))
    if code == 0:
        print("E2E: OK — все проверки пройдены")
    else:
        failed = [(name, detail) for name, ok, detail in runner.checks if not ok]
        if failed:
            print("Проваленные проверки:")
            for name, detail in failed:
                print("  - %s (%s)" % (name, detail))
        print("E2E: FAIL")
    return code


if __name__ == "__main__":
    sys.exit(main())
