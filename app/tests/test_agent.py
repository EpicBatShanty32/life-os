"""Тесты автономного агента: инструменты, журнал, откат, память.

Сеть не нужна: модель подменяется ``FakeGateway``, который отдаёт заранее
записанные шаги (``tool_calls`` и финальный текст). Проверяется главное:

* фраза человека → инструменты → записи в базе (одна фраза = несколько записей);
* журнал действий и откат последнего действия;
* история диалога попадает в контекст следующего вызова;
* отложенные мысли и досье;
* если модель молчит — агент возвращает ``None`` и бот работает правилами.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.bot import agent as agent_module
from app.bot import handlers, intent_v4
from app.server import db
from app.server.settings import load_settings

CHAT_ID = 4242


# --------------------------------------------------------------------------
# Окружение: временная база и настройки без ключа LLM
# --------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path):
    settings = load_settings(
        env={
            "LIFEOS_ADMIN_TELEGRAM_ID": str(CHAT_ID),
            "LIFEOS_DB_PATH": str(tmp_path / "lifeos.db"),
        },
        root=tmp_path,
    )
    settings.ensure_dirs()
    conn = db.init_db(settings.db_path)
    yield SimpleNamespace(settings=settings, conn=conn, tmp=tmp_path)
    conn.close()


# --------------------------------------------------------------------------
# Фейковая модель
# --------------------------------------------------------------------------


def call(name: str, call_id: str = "", **args: Any) -> Dict[str, Any]:
    """Один шаг «модель зовёт инструмент»."""
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id or "call_%s" % name,
                "name": name,
                "arguments": json.dumps(args, ensure_ascii=False),
            }
        ],
    }


def answer(text: str) -> Dict[str, Any]:
    """Шаг «модель говорит словами»."""
    return {"content": text, "tool_calls": []}


class FakeGateway:
    """Отдаёт шаги по порядку и запоминает, что ей присылали."""

    def __init__(self, script: List[Optional[Dict[str, Any]]]) -> None:
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    def available(self) -> bool:
        return True

    def chat_tools(self, messages, tools=None, max_tokens=1200, reasoning_effort=None, timeout=None):
        self.calls.append(
            {
                "messages": [dict(item) for item in messages],
                "tools": [item["function"]["name"] for item in (tools or [])],
            }
        )
        if not self.script:
            return None
        return self.script.pop(0)


class DeadGateway:
    """Модель недоступна: бот должен тихо уйти на правила."""

    last_error = "unavailable"

    def available(self) -> bool:
        return False

    def chat_tools(self, *args, **kwargs):  # pragma: no cover - не вызывается
        raise AssertionError("недоступный шлюз не должен вызываться")


def build_agent(env: Any, script: List[Optional[Dict[str, Any]]]) -> Any:
    gateway = FakeGateway(script)
    return agent_module.Agent(env.settings, gateway=gateway), gateway


# --------------------------------------------------------------------------
# Запись через инструменты
# --------------------------------------------------------------------------


def test_agent_creates_task_from_free_text(env: Any) -> None:
    bot, _gateway = build_agent(
        env,
        [
            call("create_task", title="Позвонить бухгалтеру", due_date="завтра", start_time="12:00"),
            answer("Записал звонок бухгалтеру на завтра в 12."),
        ],
    )

    reply = bot.respond(env.conn, "завтра в 12 позвонить бухгалтеру")

    assert reply is not None
    assert "бухгалтер" in reply.text.casefold()
    assert reply.tools_used() == ["create_task"]

    row = env.conn.execute(
        "SELECT title, due_date, start_at, status FROM tasks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["title"] == "Позвонить бухгалтеру"
    assert row["due_date"] == (datetime.now() + timedelta(days=1)).date().isoformat()
    assert str(row["start_at"]).endswith("T12:00:00")
    assert row["status"] == "todo"

    journal = agent_module.journal_recent(env.conn)
    assert [item["tool"] for item in journal] == ["create_task"]


def test_one_phrase_becomes_several_records(env: Any) -> None:
    bot, _gateway = build_agent(
        env,
        [
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "add_expense",
                        "arguments": json.dumps(
                            {"amount": 2400, "category": "продукты", "note": "продукты"},
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "id": "c2",
                        "name": "create_task",
                        "arguments": json.dumps(
                            {"title": "Позвонить бухгалтеру", "due_date": "завтра"},
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "id": "c3",
                        "name": "add_note",
                        "arguments": json.dumps(
                            {"text": "Идея статьи про выгорание"}, ensure_ascii=False
                        ),
                    },
                ],
            },
            answer("Разложил: расход, звонок и идею сохранил."),
        ],
    )

    reply = bot.respond(
        env.conn,
        "Потратила 2400 на продукты, завтра в 12 позвонить бухгалтеру, "
        "и классная идея для статьи про выгорание",
    )

    assert reply is not None
    assert reply.tools_used() == ["add_expense", "create_task", "add_note"]

    money = env.conn.execute(
        "SELECT amount_kopecks, kind FROM transactions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert money["amount_kopecks"] == 240000
    assert money["kind"] == "expense"

    task = env.conn.execute("SELECT title FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
    assert task["title"] == "Позвонить бухгалтеру"

    note = env.conn.execute("SELECT text FROM inbox_items ORDER BY id DESC LIMIT 1").fetchone()
    assert "выгорание" in note["text"]

    assert len(agent_module.journal_recent(env.conn)) == 3


def test_agent_undo_rolls_back_last_action(env: Any) -> None:
    bot, _gateway = build_agent(
        env,
        [
            call("add_expense", amount=4200, category="шопинг", note="ТЦ"),
            answer("Записал 4200 на шопинг."),
            call("undo_last"),
            answer("Убрал последнюю трату."),
        ],
    )

    bot.respond(env.conn, "потратила 4200 в торговом центре")
    assert env.conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1

    bot.respond(env.conn, "отмени последнее")
    assert env.conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    journal = agent_module.journal_recent(env.conn)
    assert journal[0]["tool"] == "undo_last"
    assert journal[1]["undone"] == 1


def test_agent_keeps_history_for_next_turn(env: Any) -> None:
    bot, gateway = build_agent(
        env,
        [
            call("add_journal", text="Голова квадратная после созвонов"),
            answer("Записал. Такое бывает после плотного дня."),
            answer("Да, помню про квадратную голову."),
        ],
    )

    bot.respond(env.conn, "голова квадратная после созвонов")
    bot.respond(env.conn, "как вчера прошло?")

    second = gateway.calls[1]["messages"]
    roles = [item["role"] for item in second]
    assert roles[0] == "system"
    assert "assistant" in roles
    assert any("квадратн" in str(item.get("content", "")) for item in second)


def test_agent_survives_unknown_tool_and_bad_arguments(env: Any) -> None:
    bot, _gateway = build_agent(
        env,
        [
            {
                "content": "",
                "tool_calls": [
                    {"id": "x1", "name": "teleport", "arguments": "{}"},
                    {"id": "x2", "name": "create_task", "arguments": "{не json"},
                ],
            },
            answer("Не разобрал, повтори фразу."),
        ],
    )

    reply = bot.respond(env.conn, "сделай что-нибудь")

    assert reply is not None
    assert reply.text
    assert [step.ok for step in reply.steps] == [False, False]
    assert env.conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_agent_returns_none_when_model_is_silent(env: Any) -> None:
    bot, _gateway = build_agent(env, [None])
    assert bot.respond(env.conn, "любая фраза") is None
    assert env.conn.execute("SELECT count(*) FROM agent_journal").fetchone()[0] == 0


def test_agent_is_unavailable_without_key(env: Any) -> None:
    real = agent_module.Agent(env.settings)
    assert real.available() is False
    assert real.respond(env.conn, "привет") is None


def test_agent_sends_tool_schemas_to_model(env: Any) -> None:
    bot, gateway = build_agent(env, [answer("Привет!")])
    bot.respond(env.conn, "привет")
    names = set(gateway.calls[0]["tools"])
    assert {"create_task", "add_expense", "add_journal", "undo_last"} <= names


# --------------------------------------------------------------------------
# Память, мысли, отложенное
# --------------------------------------------------------------------------


def test_remember_fact_lands_in_prompt(env: Any) -> None:
    bot, gateway = build_agent(
        env,
        [call("remember", key="стиль", value="короткие ответы"), answer("Понял."), answer("Ок.")],
    )
    bot.respond(env.conn, "отвечай мне коротко")
    bot.respond(env.conn, "привет ещё раз")

    system = gateway.calls[-1]["messages"][0]["content"]
    assert "стиль" in system.casefold()
    assert "короткие ответы" in system


def test_schedule_thought_becomes_due(env: Any) -> None:
    bot, _gateway = build_agent(
        env,
        [call("schedule_thought", when="сегодня", note="разобрать бюджет"), answer("Хорошо.")],
    )
    bot.respond(env.conn, "напомни про бюджет")

    row = env.conn.execute(
        "SELECT due_ts, note, status FROM agent_thoughts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "pending"
    assert row["due_ts"].startswith(env.settings.today())
    assert "бюджет" in row["note"]

    # Просроченная мысль — срабатывает: её выбирает цикл напоминаний.
    past = (env.settings.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    agent_module.add_thought(env.conn, past, "проверить деньги", env.settings)
    now = env.settings.now().isoformat(timespec="seconds")
    due = agent_module.due_thoughts(env.conn, now)
    assert due and "деньги" in due[0]["note"]
    agent_module.mark_thought(env.conn, int(due[0]["id"]))
    assert all(item["note"] != "проверить деньги" for item in agent_module.due_thoughts(env.conn, now))


def test_history_is_trimmed(env: Any) -> None:
    for index in range(agent_module.HISTORY_KEEP):
        agent_module.save_turn(env.conn, "вопрос %d" % index, "ответ %d" % index, env.settings)
    rows = env.conn.execute("SELECT count(*) FROM agent_messages").fetchone()[0]
    assert rows <= agent_module.HISTORY_KEEP


# --------------------------------------------------------------------------
# Бот: агент первым, правила — запасным путём
# --------------------------------------------------------------------------


def test_bot_service_prefers_agent(env: Any) -> None:
    bot, _gateway = build_agent(
        env,
        [
            call("add_expense", amount=450, category="такси", note="такси домой"),
            answer("Такси 450 — записал."),
        ],
    )
    service = handlers.BotService(
        env.settings,
        env.conn,
        v4_router=intent_v4.V4Router(gateway=None),
        agent=bot,
    )

    reply = service.handle_text("такси 450", CHAT_ID)

    assert "450" in reply.text
    assert env.conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1


def test_bot_service_falls_back_to_rules(env: Any) -> None:
    service = handlers.BotService(
        env.settings,
        env.conn,
        v4_router=intent_v4.V4Router(gateway=None),
        agent=agent_module.Agent(env.settings, gateway=DeadGateway()),
    )

    reply = service.handle_text("завтра в 12 позвонить бухгалтеру", CHAT_ID)

    assert "бухгалтер" in reply.text.casefold()
    assert env.conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_undo_command_uses_journal(env: Any) -> None:
    bot, _gateway = build_agent(
        env,
        [call("add_note", text="Идея: курс про фокус"), answer("Сохранил.")],
    )
    service = handlers.BotService(
        env.settings,
        env.conn,
        v4_router=intent_v4.V4Router(gateway=None),
        agent=bot,
    )
    service.handle_text("идея курса про фокус", CHAT_ID)
    assert env.conn.execute("SELECT count(*) FROM inbox_items").fetchone()[0] == 1

    reply = service.handle_undo()

    assert "Отменил" in reply.text
    assert env.conn.execute("SELECT count(*) FROM inbox_items").fetchone()[0] == 0
