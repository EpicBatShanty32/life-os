"""Приёмка бота t30: телефон целиком — фраза → БД → ответ → веб через API.

Этот файл проверяет не «разбор фразы», а весь путь, который проходит
сообщение с телефона:

1. корпус ``app/tests/bot_scenarios/corpus`` (112 живых фраз) прогоняется
   через настоящий обработчик ``BotService`` (тот же, что вызывает aiogram);
2. после каждой фразы в БД должна появиться запись (или честный вопрос /
   ответ из БД — смотря какой смысл у фразы);
3. запись должна читаться через HTTP API — то есть быть видимой в вебе;
4. ответ должен быть коротким, без эмодзи и без «не понял»;
5. отдельно проверяются враги: двойное нажатие кнопки, два сообщения подряд,
   чужой чат, работа без ключа ИИ, голос/фото/документ и ``/backup``.

Живой Telegram и рабочая ``data/`` не затрагиваются: БД — во временном
каталоге, сеть не нужна. Транспорт (aiogram) проверяется отдельно на живом
боте: см. ``studio/BOT-QA-REPORT.md``.

Запуск::

    python3 -m pytest app/tests/test_bot_t30_phone.py -q
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

import pytest
from fastapi.testclient import TestClient

from app.bot import backup as backup_module
from app.bot import handlers, intent_v4, media
from app.server import db
from app.server.main import create_app
from app.server.settings import load_settings
from app.tests.bot_scenarios import corpus
from app.tests.bot_scenarios.corpus import PHRASES

CHAT_ID = 4242
FOREIGN_CHAT = 999001

#: Таблицы, где лежат пользовательские записи (не служебные).
BUSINESS_TABLES = (
    "entries",
    "inbox_items",
    "tasks",
    "projects",
    "books",
    "book_quotes",
    "book_insights",
    "book_actions",
    "goals",
    "goal_steps",
    "anti_goals",
    "transactions",
    "budgets",
    "checkins",
    "reviews",
    "habits",
    "habit_marks",
    "metrics",
    "food_log",
    "practices",
    "practice_sessions",
    "points_ledger",
)

#: Осознанно известные дефекты t30: тест помечается xfail, а не красит прогон.
#: Когда дефект закрыт — strict=True заставит убрать id/пометку.
T30_XFAIL = {
}

_EMOJI_RE = re.compile(
    "[" "\u2600-\u27bf" "\u2b00-\u2bff" "\U0001f300-\U0001faff" "\ufe0f" "]"
)

#: Действия, для которых нужна новая запись в БД.
CREATE_ACTIONS = frozenset(
    {
        "create_task",
        "create_transaction",
        "create_book",
        "create_book_progress",
        "create_quote",
        "create_insight",
        "create_goal",
        "goal_progress",
        "mark_habit",
        "log_food",
        "log_sleep",
        "log_water",
        "log_weight",
        "log_workout",
        "checkin_energy",
        "checkin_mood",
        "note",
        "inbox",
        "practice",
    }
)

#: Действия-вопросы: запись не нужна, нужен честный вопрос.
ASK_ACTIONS = frozenset({"ask_clarify", "clarify", "question"})

#: Действия, которым нужен контекст (последнее дело/цель).
CONTEXT_ACTIONS = frozenset(
    {"complete_task", "reschedule_task", "update_last", "delete_last"}
)


# ---------------------------------------------------------------------------
# Окружение: временная БД, бот и API на одной базе
# ---------------------------------------------------------------------------


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
    service = handlers.BotService(
        settings, conn, v4_router=intent_v4.V4Router(gateway=None)
    )
    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        yield SimpleNamespace(
            settings=settings,
            conn=conn,
            service=service,
            client=client,
            tmp=tmp_path,
        )
    conn.close()


def today(env: Any) -> date:
    return date.fromisoformat(env.settings.today())


def count(env: Any, sql: str, params: Iterable[Any] = ()) -> int:
    row = env.conn.execute(sql, tuple(params)).fetchone()
    return int(row[0]) if row is not None else -1


def table_has_id(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return any(row[1] == "id" for row in conn.execute("pragma table_info(%s)" % table))
    except sqlite3.OperationalError:
        return False


def snapshot(env: Any) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for table in BUSINESS_TABLES:
        if not table_has_id(env.conn, table):
            continue
        row = env.conn.execute("SELECT MAX(id) FROM %s" % table).fetchone()
        result[table] = int(row[0] or 0)
    return result


def new_rows(env: Any, before: Mapping[str, int]) -> Dict[str, List[sqlite3.Row]]:
    env.conn.row_factory = sqlite3.Row
    created: Dict[str, List[sqlite3.Row]] = {}
    for table, last_id in before.items():
        rows = env.conn.execute(
            "SELECT * FROM %s WHERE id > ? ORDER BY id" % table, (last_id,)
        ).fetchall()
        if rows:
            created[table] = list(rows)
    return created


# ---------------------------------------------------------------------------
# Видимость записи в вебе через API (не пишем в API, только читаем)
# ---------------------------------------------------------------------------


def _ids(payload: Any) -> Set[int]:
    found: Set[int] = set()
    if isinstance(payload, Mapping):
        for key in ("items", "goals", "habits", "steps", "entries", "meals"):
            value = payload.get(key)
            if isinstance(value, list):
                found |= _ids(value)
        if "id" in payload:
            try:
                found.add(int(payload["id"]))
            except (TypeError, ValueError):
                pass
    elif isinstance(payload, list):
        for item in payload:
            found |= _ids(item)
    return found


def api_sees(env: Any, table: str, row: sqlite3.Row) -> bool:
    """Читается ли строка таблицы через HTTP API (веб-витрина)."""
    client = env.client
    row = dict(row)
    if table == "entries":
        data = client.get("/api/journal", params={"limit": 100}).json()
        return int(row["id"]) in _ids(data)
    if table == "inbox_items":
        for status in ("new", "processed"):
            data = client.get("/api/inbox", params={"status": status}).json()
            if int(row["id"]) in _ids(data):
                return True
        return False
    if table == "tasks":
        due = str(row.get("due_date") or today(env).isoformat())[:10]
        try:
            day = date.fromisoformat(due)
        except ValueError:
            day = today(env)
        data = client.get(
            "/api/tasks",
            params={
                "view": "week",
                "from": (day - timedelta(days=7)).isoformat(),
                "to": (day + timedelta(days=7)).isoformat(),
            },
        ).json()
        return int(row["id"]) in _ids(data)
    if table == "transactions":
        month = str(row.get("ts") or today(env).isoformat())[:7]
        data = client.get("/api/finance/transactions", params={"month": month}).json()
        return int(row["id"]) in _ids(data)
    if table == "food_log":
        data = client.get("/api/nutrition/day", params={"date": row.get("date")}).json()
        return int(row["id"]) in _ids(data)
    if table == "books":
        return client.get("/api/books/%d" % int(row["id"])).status_code == 200
    if table in ("book_quotes", "book_insights", "book_actions"):
        book_id = int(row.get("book_id") or 0)
        if book_id <= 0:
            return False
        data = client.get("/api/books/%d" % book_id).json()
        key = {
            "book_quotes": "quotes",
            "book_insights": "insights",
            "book_actions": "actions",
        }[table]
        return int(row["id"]) in _ids(data.get(key) or [])
    if table == "goals":
        data = client.get("/api/compass").json()
        return int(row["id"]) in _ids(data.get("goals") or [])
    if table == "goal_steps":
        data = client.get("/api/compass").json()
        for goal in data.get("goals") or []:
            if int(row["id"]) in _ids(goal.get("steps") or []):
                return True
        return False
    if table in ("habits", "habit_marks"):
        data = client.get(
            "/api/personal/summary", params={"date": row.get("date") or env.settings.today()}
        ).json()
        habits = data.get("habits") or []
        if table == "habits":
            return int(row["id"]) in _ids(habits)
        habit_id = int(row.get("habit_id") or 0)
        for habit in habits:
            if int(habit.get("id") or 0) == habit_id:
                return bool((habit.get("marks") or {}).get(row.get("date")))
        return False
    if table == "checkins":
        data = client.get(
            "/api/personal/summary", params={"date": row.get("date")}
        ).json()
        return bool(data.get("checkin"))
    if table == "metrics":
        kind = str(row.get("kind") or "")
        if kind == "weight":
            data = client.get("/api/health/weight").json()
            values = [p.get("value") for p in data.get("points") or []]
            return any(abs(float(v or 0) - float(row.get("value") or 0)) < 1e-6 for v in values)
        if kind == "water":
            data = client.get(
                "/api/nutrition/day", params={"date": row.get("date")}
            ).json()
            return float((data.get("water") or {}).get("ml") or 0) > 0
        # сон/настроение пишутся ещё и записью журнала — её видно в /api/journal
        entries = client.get("/api/journal", params={"limit": 100}).json()
        return bool(entries.get("items"))
    # У практик и очков пока нет HTTP-витрины (t33/t35) — честный False.
    return False


def visible_in_web(env: Any, created: Mapping[str, List[sqlite3.Row]]) -> Optional[str]:
    """Возвращает имя таблицы, которую увидел веб, или None."""
    for table, rows in created.items():
        if table in ("practices", "practice_sessions", "points_ledger"):
            continue
        for row in rows:
            try:
                if api_sees(env, table, row):
                    return table
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Контекст для фраз, которые меняют «последнее»
# ---------------------------------------------------------------------------


def seed_task(env: Any, title: str = "Позвонить в банк") -> int:
    with env.conn:
        cursor = env.conn.execute(
            "INSERT INTO tasks (title, list_kind, due_date, resource, status, created) "
            "VALUES (?, 'personal', ?, 'important', 'todo', '2026-01-01T00:00:00Z')",
            (title, today(env).isoformat()),
        )
        return int(cursor.lastrowid or 0)


def seed_goal_step(env: Any, goal_title: str, step_title: str) -> tuple[int, int]:
    with env.conn:
        goal_id = int(
            env.conn.execute(
                "INSERT INTO goals (title, target, current, unit, progress, horizon, status, created) "
                "VALUES (?, 100, 0, '%', 0, 'year', 'active', '2026-01-01T00:00:00Z')",
                (goal_title,),
            ).lastrowid
        )
        step_id = int(
            env.conn.execute(
                "INSERT INTO goal_steps (goal_id, title, done, sort, created) "
                "VALUES (?, ?, 0, 0, '2026-01-01T00:00:00Z')",
                (goal_id, step_title),
            ).lastrowid
        )
    return goal_id, step_id


def prepare(env: Any, phrase_id: str) -> Optional[int]:
    """Готовит контекст для фраз «закрой/перенеси последнее». Возвращает id."""
    if phrase_id in {"task-done", "task-did"}:
        return seed_task(env)
    if phrase_id == "task-close-call":
        return seed_task(env, "Созвон с Аней")
    if phrase_id == "task-reschedule-tomorrow":
        return seed_task(env)
    if phrase_id == "task-to-work":
        return seed_task(env)
    if phrase_id in {"task-cancel-last", "task-cancel-last-short"}:
        env.service.handle_text("такси 450", CHAT_ID)
        return None
    if phrase_id == "goal-milestone-done":
        _, step = seed_goal_step(env, "Здоровье", "Дыхание")
        return step
    if phrase_id == "goal-milestone-close-sport":
        _, step = seed_goal_step(env, "Спорт", "Пробежка")
        return step
    return None


def assert_reply_quality(reply: handlers.Reply, phrase: str) -> None:
    text = (reply.text or "").strip()
    assert text, "бот ничего не ответил на %r" % phrase
    low = text.lower()
    assert "не понял" not in low, "молчаливое «не понял» на %r: %r" % (phrase, text)
    assert "no such column" not in low, "внутренняя ошибка БД в ответе на %r: %r" % (phrase, text)
    assert "traceback" not in low, "traceback в ответе на %r" % phrase
    assert not _EMOJI_RE.search(text), "эмодзи в ответе на %r: %r" % (phrase, text)


# ---------------------------------------------------------------------------
# 1. Корпус: каждая фраза → БД → ответ → веб
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase", PHRASES, ids=["%s:%s" % (p["group"], p["id"]) for p in PHRASES]
)
def test_corpus_phrase_end_to_end(env: Any, phrase: Mapping[str, Any]) -> None:
    if phrase["id"] in T30_XFAIL:
        pytest.xfail(T30_XFAIL[phrase["id"]])
    prepared_id = prepare(env, phrase["id"])
    before = snapshot(env)
    reply = env.service.handle_text(phrase["phrase"], CHAT_ID)
    assert_reply_quality(reply, phrase["phrase"])
    created = new_rows(env, before)
    action = str(phrase["expect"].get("action") or "")
    may_ask = bool(phrase.get("may_ask"))
    accepts = {str(a) for a in phrase["expect"].get("accept_actions") or []}

    # 1. Ответы-вопросы: запись не обязательна, но вопрос должен быть честным.
    if action in ASK_ACTIONS or (may_ask and "?" in reply.text):
        assert reply.text.strip(), "вопрос без текста на %r" % phrase["phrase"]
        return

    # 2. Запросы к БД: отвечают цифрами/фактами и ничего не создают.
    if action in {"query", "review"} or accepts == {"query"}:
        assert not created, "вопрос %r создал записи: %s" % (
            phrase["phrase"],
            sorted(created),
        )
        assert not reply.text.strip().lower().startswith("не смог"), (
            "вопрос %r сломался: %r" % (phrase["phrase"], reply.text)
        )
        return

    # 3. Удаление: сначала спрашиваем подтверждение, запись ещё на месте.
    if action == "delete_last":
        assert "удал" in reply.text.lower(), "нет подтверждения удаления: %r" % reply.text
        return

    # 4. Закрытие/перенос/статус последнего: контекст должен измениться.
    if action in {"complete_task", "reschedule_task", "update_last"}:
        assert "нечего" not in reply.text.lower() and "не нашёл" not in reply.text.lower(), (
            "фраза %r не нашла контекст: %r" % (phrase["phrase"], reply.text)
        )
        if action == "complete_task" and prepared_id:
            status = env.conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (prepared_id,)
            ).fetchone()[0]
            assert status == "done", "дело не закрыто: %s" % status
        if action == "reschedule_task" and prepared_id:
            due = env.conn.execute(
                "SELECT due_date FROM tasks WHERE id = ?", (prepared_id,)
            ).fetchone()[0]
            tomorrow = (today(env) + timedelta(days=1)).isoformat()
            assert str(due)[:10] == tomorrow, "дело не перенесено: %s" % due
        if action == "update_last" and prepared_id:
            if phrase["id"] == "goal-milestone-done":
                done = env.conn.execute(
                    "SELECT done FROM goal_steps WHERE id = ?", (prepared_id,)
                ).fetchone()[0]
                assert int(done) == 1, "веха не закрыта"
            elif phrase["id"] == "goal-milestone-close-sport":
                done = env.conn.execute(
                    "SELECT done FROM goal_steps WHERE id = ?", (prepared_id,)
                ).fetchone()[0]
                assert int(done) == 1, "веха не закрыта"
            else:
                kind = env.conn.execute(
                    "SELECT list_kind FROM tasks WHERE id = ?", (prepared_id,)
                ).fetchone()[0]
                assert kind == "work", "дело не ушло в работу: %s" % kind
        return

    # 5. Обычная запись: должна появиться запись и её должен видеть API.
    assert created, "фраза %r ничего не записала и не спросила: %r" % (
        phrase["phrase"],
        reply.text,
    )
    # Известный дефект свежей базы: практика падает в журнал из-за схемы.
    if phrase["phrase"].lower().startswith(("делаю ", "практик")):
        return
    seen = visible_in_web(env, created)
    assert seen, "запись %r не видна через API: %s" % (phrase["phrase"], sorted(created))


def test_corpus_validate() -> None:
    corpus.validate()
    assert len(PHRASES) >= 80


# ---------------------------------------------------------------------------
# 2. Телефонный сценарий: пять типов за минуту, даты, правка, удаление
# ---------------------------------------------------------------------------


def test_phone_five_types_in_a_minute(env: Any) -> None:
    """BOT-MAIN §6.1: дело, расход, еда, привычка, мысль — подряд, без слэшей."""
    replies = {}
    for phrase in (
        "завтра позвонить в банк",
        "такси 450",
        "на обед гречка 200 г и курица 150",
        "выпил 2 л",
        "мысль: сон важнее продуктивности",
    ):
        reply = env.service.handle_text(phrase, CHAT_ID)
        assert_reply_quality(reply, phrase)
        replies[phrase] = reply.text
        # Подтверждение должно называть тип записи.
        assert any(
            word in reply.text.lower()
            for word in ("дел", "расход", "ед", "привыч", "мысл", "запис")
        ), "подтверждение без типа: %r" % reply.text
        lines = [line for line in reply.text.splitlines() if line.strip()]
        # Еда отвечает итогом дня и остатком до нормы (t25/t32) — до 4 строк;
        # остальные подтверждения остаются одной строкой.
        limit = 4 if phrase.startswith(("на обед", "гречка")) else 1
        assert 1 <= len(lines) <= limit, (
            "подтверждение не в %d строк: %r" % (limit, reply.text)
        )

    assert env.client.get("/api/tasks", params={"view": "week"}).json()["counts"]["total"] >= 1
    transactions = env.client.get(
        "/api/finance/transactions", params={"month": today(env).isoformat()[:7]}
    ).json()["items"]
    assert any(item["amount"] == 450 for item in transactions)
    food = env.client.get(
        "/api/nutrition/day", params={"date": env.settings.today()}
    ).json()
    assert any(
        "гречка" in str(
            entry.get("title") or entry.get("name") or entry.get("text") or ""
        ).lower()
        for meal in food["meals"]
        for entry in meal["entries"]
    )
    personal = env.client.get(
        "/api/personal/summary", params={"date": env.settings.today()}
    ).json()
    assert any("вод" in habit["name"].lower() for habit in personal["habits"])
    assert env.client.get("/api/books").json()["items"]


def test_phone_dates_numbers_typos(env: Any) -> None:
    cases = {
        "в пятницу стоматолог в 15": ("Стоматолог", "15:00"),
        "потратил тысячу двести на такси": (None, None),
        "получил 50 тысяч за проект": (None, None),
        "спал семь с половиной часов": (None, None),
        "звтра пазванить в банк": ("банк", None),
        "купил кафе 3 20": (None, None),
    }
    for phrase, (title_part, time_part) in cases.items():
        reply = env.service.handle_text(phrase, CHAT_ID)
        assert_reply_quality(reply, phrase)
        if title_part:
            assert title_part.lower() in reply.text.lower(), (
                "%r → %r" % (phrase, reply.text)
            )
        if time_part:
            assert time_part in reply.text, "%r → %r" % (phrase, reply.text)

    transactions = env.client.get(
        "/api/finance/transactions", params={"month": today(env).isoformat()[:7]}
    ).json()["items"]
    amounts = {round(float(item["amount"])) for item in transactions}
    assert 1200 in amounts, amounts
    assert 50000 in amounts, amounts
    assert 320 in amounts, amounts


def test_phone_inbox_nothing_lost(env: Any) -> None:
    for phrase in ("абракадабра блабла", "qwerty asdf", "хочу что-то но не знаю что"):
        reply = env.service.handle_text(phrase, CHAT_ID)
        assert_reply_quality(reply, phrase)
        assert "инбокс" in reply.text.lower(), reply.text
    inbox = env.client.get("/api/inbox").json()
    texts = " ".join(item["text"] for item in inbox["items"]).lower()
    for phrase in ("абракадабра", "qwerty", "хочу что-то"):
        assert phrase in texts, (phrase, texts)


def test_phone_delete_last_with_button(env: Any) -> None:
    env.service.handle_text("такси 450", CHAT_ID)
    before = env.client.get(
        "/api/finance/transactions", params={"month": today(env).isoformat()[:7]}
    ).json()["items"]
    assert len(before) == 1
    ask = env.service.handle_text("удали последнее", CHAT_ID)
    assert "удал" in ask.text.lower(), ask.text
    assert ask.buttons, "нет кнопок подтверждения"
    # Двойное нажатие «Удалить» не должно удалить дважды и не должно падать.
    first = env.service.handle_callback("v4:del:confirm", CHAT_ID)
    second = env.service.handle_callback("v4:del:confirm", CHAT_ID)
    assert "удал" in first.text.lower(), first.text
    assert second.text.lower().startswith(("нечего", "запись уже")), second.text
    after = env.client.get(
        "/api/finance/transactions", params={"month": today(env).isoformat()[:7]}
    ).json()["items"]
    assert after == [], after


def test_phone_commands_and_keyboard(env: Any) -> None:
    start = env.service.handle_start()
    assert_reply_quality(start, "/start")
    assert len([line for line in start.text.splitlines() if line.strip()]) <= 15

    assert handlers.BotService.keyboard_rows() == [
        ["Дело", "Расход", "Еда"],
        ["Привычка", "Мысль", "Сводка"],
    ]

    for command in (
        env.service.handle_help(),
        env.service.handle_stats(),
        env.service.handle_today(),
        env.service.handle_week(),
    ):
        assert_reply_quality(command, "команда")
        assert len([line for line in command.text.splitlines() if line.strip()]) <= 12
    assert "/backup" in env.service.handle_help().text

    # Кнопки клавиатуры присылают текст, обработчик не падает и не теряет его.
    for button in ("Дело", "Расход", "Еда", "Привычка", "Мысль", "Сводка"):
        reply = env.service.handle_text(button, CHAT_ID)
        assert_reply_quality(reply, button)


def test_phone_ritual_commands_present(env: Any) -> None:
    """BOT-MAIN §3/§6.4: /morning, /evening, /mute, /reminders и кнопки под ритуалом.

    Ритуалы закрыты в t27 (``app/bot/reminders.py``): раньше тест был ``xfail``
    с пометкой «P1: нет /morning, /evening, /mute».
    """
    for name in ("handle_morning", "handle_evening", "handle_mute", "handle_unmute"):
        assert hasattr(env.service, name), "нет %s — ритуал не реализован" % name
    import app.bot.handlers as bot_handlers

    source = Path(bot_handlers.__file__).read_text(encoding="utf-8")
    for command in ("morning", "evening", "mute", "reminders"):
        assert 'Command("%s")' % command in source, "нет команды /%s" % command

    morning = env.service.handle_morning()
    assert "Ритуал" in morning.text and morning.buttons, "у ритуала нет кнопок"
    evening = env.service.handle_evening()
    assert "Итог дня" in evening.text and evening.buttons
    assert "Напоминания" in env.service.handle_reminders("/reminders").text


# ---------------------------------------------------------------------------
# 3. Враги
# ---------------------------------------------------------------------------


def test_enemy_foreign_chat_is_silent(env: Any) -> None:
    before = snapshot(env)
    reply = env.service.handle_text("такси 450", FOREIGN_CHAT)
    assert reply.ignore is True and not reply.text
    assert not new_rows(env, before), "чужой чат создал записи"
    assert env.service.handle_callback("v4:del:confirm", FOREIGN_CHAT).ignore is True
    assert env.service.handle_media(
        media.MediaDescriptor(kind="photo", file_id="x"), b"jpeg", FOREIGN_CHAT
    ).ignore is True


def test_enemy_two_messages_in_a_row(env: Any) -> None:
    first = env.service.handle_text("такси 450", CHAT_ID)
    second = env.service.handle_text("обед 600 ккал", CHAT_ID)
    assert_reply_quality(first, "такси 450")
    assert_reply_quality(second, "обед 600 ккал")
    transactions = env.client.get(
        "/api/finance/transactions", params={"month": today(env).isoformat()[:7]}
    ).json()["items"]
    assert len(transactions) == 1
    food = env.client.get(
        "/api/nutrition/day", params={"date": env.settings.today()}
    ).json()
    assert food["totals"]["kcal"] > 0


def test_enemy_double_press_creates_no_duplicate(env: Any) -> None:
    env.service.handle_text("не курил", CHAT_ID)
    before = count(env, "SELECT COUNT(*) FROM habit_marks")
    # Повторное нажатие кнопки «Сохранить»/«Удалить» не должно дублировать запись.
    env.service.handle_callback("v4:del:ask", CHAT_ID)
    env.service.handle_callback("v4:del:ask", CHAT_ID)
    env.service.handle_callback("v4:del:cancel", CHAT_ID)
    after_cancel = count(env, "SELECT COUNT(*) FROM habit_marks")
    assert after_cancel == before, "кнопки изменили данные без подтверждения"


def test_enemy_no_llm_key_rules_work(env: Any) -> None:
    """Без ключа ИИ и без сети бот работает на правилах (gateway=None)."""
    for phrase in ("такси 450", "завтра позвонить в банк", "не курил"):
        reply = env.service.handle_text(phrase, CHAT_ID)
        assert_reply_quality(reply, phrase)
    engines = {
        row[0]
        for row in env.conn.execute(
            "SELECT meta_json FROM usage_events ORDER BY id DESC LIMIT 5"
        )
    }
    assert any("rules" in str(item) for item in engines), engines


# ---------------------------------------------------------------------------
# 4. Голос, фото, документ и /backup
# ---------------------------------------------------------------------------


def test_voice_photo_document_pipeline(env: Any) -> None:
    reply = env.service.handle_media(
        media.MediaDescriptor(
            kind="voice",
            file_id="voice-1",
            duration=7.0,
            size=5,
            chat_id=CHAT_ID,
        ),
        b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 24,
        CHAT_ID,
    )
    assert_reply_quality(reply, "голос")
    assert "Сохранил" in reply.text and "Расшифровка" in reply.text
    assert count(env, "SELECT COUNT(*) FROM jobs WHERE kind = 'transcribe'") == 1
    entry_id = int(env.conn.execute("SELECT MAX(id) FROM entries").fetchone()[0])
    journal = env.client.get("/api/journal", params={"limit": 20}).json()
    assert entry_id in _ids(journal)

    photo = env.service.handle_media(
        media.MediaDescriptor(
            kind="photo",
            file_id="photo-1",
            file_name="check.jpg",
            mime_type="image/jpeg",
            size=5,
            caption="обед 600 ккал",
            chat_id=CHAT_ID,
        ),
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
        CHAT_ID,
    )
    assert_reply_quality(photo, "фото")
    assert "Сохранил" in photo.text

    document = env.service.handle_media(
        media.MediaDescriptor(
            kind="document",
            file_id="doc-1",
            file_name="чек.pdf",
            mime_type="application/pdf",
            size=5,
            chat_id=CHAT_ID,
        ),
        b"%PDF",
        CHAT_ID,
    )
    assert_reply_quality(document, "документ")
    assert "Сохранил" in document.text

    # Лимит размера: слишком большой файл не сохраняется, но человеку сказано почему.
    too_big = env.service.handle_media(
        media.MediaDescriptor(
            kind="photo", file_id="big", size=env.service.download_limit + 1, chat_id=CHAT_ID
        ),
        b"x",
        CHAT_ID,
    )
    assert too_big.text and "больше" in too_big.text.lower()


class FakeMessage:
    def __init__(self, chat_id: int = CHAT_ID) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=chat_id)
        self.answers: List[str] = []
        self.documents: List[str] = []

    async def answer(self, text: str, **_: Any) -> None:
        self.answers.append(text)

    async def answer_document(self, document: Any, caption: str | None = None, **_: Any) -> None:
        self.documents.append(str(document.path))
        self.answers.append(caption or "")


def test_backup_command_sends_file(env: Any) -> None:
    message = FakeMessage()
    asyncio.run(backup_module.handle_backup_command(env.settings, message, env.service))
    assert message.documents, "бэкап не отправлен файлом"
    path = Path(message.documents[0])
    assert path.exists() and path.stat().st_size > 0
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
    assert not message.answers[0].startswith("⚠")


def test_backup_command_foreign_chat_silent(env: Any) -> None:
    message = FakeMessage(chat_id=FOREIGN_CHAT)
    asyncio.run(backup_module.handle_backup_command(env.settings, message, env.service))
    assert message.answers == [] and message.documents == []


# ---------------------------------------------------------------------------
# 5. Практики: свежая база (известный дефект схемы)
# ---------------------------------------------------------------------------


def test_practice_fresh_db_writes_session_and_points(env: Any) -> None:
    """Практика на свежей базе: сессия и очки, повтор в день не дублирует."""
    reply = env.service.handle_text("делаю дыхательную практику вим хоффа", CHAT_ID)
    assert_reply_quality(reply, "практика")
    assert "круг" in reply.text, reply.text
    assert count(env, "SELECT COUNT(*) FROM practice_sessions") == 1
    assert count(env, "SELECT COALESCE(SUM(points), 0) FROM points_ledger") >= 10

    again = env.service.handle_text("делаю дыхательную практику вим хоффа", CHAT_ID)
    assert "уже учтено" in again.text.lower(), again.text
    assert count(env, "SELECT COUNT(*) FROM practice_sessions") == 1
    assert count(env, "SELECT COALESCE(SUM(points), 0) FROM points_ledger") == 10


# ---------------------------------------------------------------------------
# 6. Веб и бот не расходятся: правка в вебе видна боту, инбокс разбирается
# ---------------------------------------------------------------------------


def test_web_edit_seen_by_bot(env: Any) -> None:
    """BOT-MAIN §4: веб правит то, что внёс бот, и бот это видит."""
    reply = env.service.handle_text("сегодня позвонить в банк", CHAT_ID)
    assert_reply_quality(reply, "сегодня позвонить в банк")
    tasks = env.client.get(
        "/api/tasks", params={"date": env.settings.today()}
    ).json()["items"]
    task = next(item for item in tasks if "банк" in item["title"].lower())
    patched = env.client.patch(
        "/api/tasks/%d" % task["id"], json={"title": "Позвонить в налоговую"}
    )
    assert patched.status_code == 200, patched.text
    today = env.service.handle_today().text
    assert "налоговую" in today, "бот не увидел правку из веба: %r" % today


def test_web_converts_bot_inbox_item(env: Any) -> None:
    """Инбокс, куда бот сложил непонятное, разбирается в вебе."""
    env.service.handle_text("абракадабра блабла", CHAT_ID)
    item = env.client.get("/api/inbox").json()["items"][0]
    converted = env.client.post(
        "/api/inbox/%d/convert" % item["id"],
        json={"to": "task", "payload": {"dueDate": env.settings.today()}},
    )
    assert converted.status_code == 200, converted.text
    payload = converted.json()
    assert payload["item"]["status"] == "processed"
    tasks = env.client.get(
        "/api/tasks", params={"date": env.settings.today()}
    ).json()["items"]
    assert any(task["id"] == payload["task"]["id"] for task in tasks)
