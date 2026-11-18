"""Тесты напоминаний и ритуалов бота (задача t27).

Проверяется весь путь «расписание → сообщение в Telegram → кнопка → запись в
БД», без сети: движок отдаёт готовые сообщения, тест их «отправляет» и
отмечает, как это сделал бы цикл в ``app/bot/main.py``.

Что покрыто:

* утренний ритуал 09:00 (вес, сон, план) и вечерний итог 22:00 (итоги, вопросы,
  энергия кнопкой);
* напоминание о деле за 30 минут и его кнопки: «Сделал», «Отложить 1 ч»,
  «На завтра», «Пропустить»;
* привычки к 20:00 и отметка привычки кнопкой;
* тихие часы 23:00–08:00 и исключение для жёстких событий (``resource='must'``);
* «мак был выключен»: пропущенное не догоняется пачкой;
* лимит напоминаний в день на одно дело (антиспам «Отложить»);
* ``/mute``, ``/unmute``, ``/morning``, ``/evening``, ``/reminders`` и правка
  расписания командами;
* перезапуск службы: расписание и состояние живут в БД, дублей нет;
* нажатие кнопки видно через API — то есть и в вебе.

База — во временном каталоге (``tmp_path``), рабочая ``data/`` не трогается.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any, List
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.bot import handlers, intent_v4, reminders, store_v4
from app.server import db
from app.server.main import create_app
from app.server.settings import load_settings

CHAT_ID = 4242
#: «Сегодня» по часовому поясу приложения — как ``settings.today()``. Тесты
#: гоняются на текущем дне, поэтому не зависят от календаря.
DAY = datetime.now(ZoneInfo("Europe/Moscow")).date()
DAY_S = DAY.isoformat()


# --------------------------------------------------------------------------
# Окружение: временная БД, бот и API на одной базе
# --------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
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


def moment(env: Any, hhmm: str, day: date = DAY) -> datetime:
    """Момент времени в часовом поясе приложения (как ``settings.now()``)."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=env.settings.tz)


def add_task(
    env: Any,
    title: str,
    hhmm: str | None = None,
    day: date = DAY,
    resource: str = "important",
) -> int:
    """Дело на день: время начала как в жизни (``start_at``)."""
    data: dict[str, Any] = {
        "title": title,
        "due_date": day.isoformat(),
        "resource": resource,
    }
    if hhmm:
        data["start_time"] = hhmm
    with env.conn:
        outcome = store_v4.create_task(env.conn, env.settings, data)
    for operation in outcome.operations:
        if operation.get("type") == "task":
            return int(operation["id"])
    raise AssertionError("дело не создалось: %s" % outcome.text)


def add_habit(env: Any, name: str) -> int:
    with env.conn:
        cursor = env.conn.execute("INSERT INTO habits (name) VALUES (?)", (name,))
    return int(cursor.lastrowid)


def tick(env: Any, hhmm: str, day: date = DAY, engine: Any = None):
    engine = engine or env.service.reminders
    return engine.tick(now=moment(env, hhmm, day))


def deliver(env: Any, items: List[Any]) -> None:
    """«Отправляет» сообщения так же, как цикл бота: отмечает факт в БД."""
    for index, item in enumerate(items, start=1):
        env.service.reminders.mark_sent(item.event_id, message_id=1000 + index)


def event_states(env: Any) -> dict[str, str]:
    rows = env.conn.execute(
        "SELECT key, state FROM reminder_events ORDER BY id"
    ).fetchall()
    return {str(row["key"]): str(row["state"]) for row in rows}


def event_id(env: Any, key: str) -> int:
    row = env.conn.execute(
        "SELECT id FROM reminder_events WHERE key = ?", (key,)
    ).fetchone()
    assert row is not None, "события %s нет в БД" % key
    return int(row["id"])


def task_row(env: Any, task_id: int) -> Any:
    return env.conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()


# --------------------------------------------------------------------------
# 1. Расписание: утренний ритуал и вечерний итог
# --------------------------------------------------------------------------


def test_morning_ritual_arrives_at_0900(env) -> None:
    """09:00 — утро: вес, сон и план дня, а не раньше минуты."""
    assert tick(env, "08:59") == []

    outgoing = tick(env, "09:00")
    assert len(outgoing) == 1
    message = outgoing[0]
    assert message.kind == "morning"
    assert message.chat_id == CHAT_ID
    assert "Ритуал" in message.text
    assert "Вес" in message.text and "Сон" in message.text and "План" in message.text
    labels = [label for row in message.buttons for label, _ in row]
    assert "Отложить 1 ч" in labels and "Пропустить" in labels

    deliver(env, outgoing)
    assert event_states(env)["morning:%s" % DAY_S] == "sent"
    # Второй тик того же дня не повторяет сообщение.
    assert tick(env, "09:05") == []


def test_morning_shows_weight_sleep_and_plan(env) -> None:
    """В утреннем ритуале видно, что уже записано и что в плане."""
    with env.conn:
        env.conn.execute(
            "INSERT INTO metrics (kind, value, date, unit) VALUES ('weight', 81.2, ?, 'кг')",
            (DAY_S,),
        )
        env.conn.execute(
            "INSERT INTO metrics (kind, value, date, unit) VALUES ('sleep', 7.5, ?, 'ч')",
            (DAY_S,),
        )
    add_task(env, "Позвонить в банк", "15:00")

    outgoing = tick(env, "09:00")
    assert len(outgoing) == 1
    text = outgoing[0].text
    assert "81,2" in text and "7,5" in text
    assert "Позвонить в банк" in text


def test_missed_morning_is_not_backfilled(env) -> None:
    """Мак был выключен: утренний ритуал в 11:00 уже неактуален — молчим."""
    assert tick(env, "11:00") == []
    assert event_states(env)["morning:%s" % DAY_S] == "missed"
    # И позже тоже: событие дня закрыто.
    assert tick(env, "12:00") == []


def test_evening_digest_has_numbers_and_energy_buttons(env) -> None:
    """22:00 — итог дня с цифрами, вопросами и энергией 1–5 кнопкой."""
    add_task(env, "Сдать отчёт", "10:00")
    with env.conn:
        task_id = env.conn.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()[0]
        env.conn.execute(
            "UPDATE tasks SET status = 'done', done_at = ? WHERE id = ?",
            ("%sT10:40:00" % DAY_S, task_id),
        )
        store_v4.add_transaction(
            env.conn, env.settings, {"amount_kopecks": 45000, "text": "такси 450"}, "expense"
        )

    outgoing = tick(env, "22:00")
    assert len(outgoing) == 1
    message = outgoing[0]
    assert message.kind == "evening"
    assert "Итог дня" in message.text
    assert "Расходы" in message.text and "450" in message.text
    assert "Вопросы" in message.text
    energy_row = message.buttons[0]
    assert [label for label, _ in energy_row] == ["1", "2", "3", "4", "5"]
    for _label, data in energy_row:
        assert data.startswith("r:energy:") and len(data.encode("utf-8")) <= 64

    deliver(env, outgoing)

    outcome = env.service.reminders.handle_action(
        "r:energy:4:%d" % message.event_id, now=moment(env, "22:01")
    )
    assert "4" in outcome.notification
    row = env.conn.execute("SELECT energy FROM checkins WHERE date = ?", (DAY_S,)).fetchone()
    assert row is not None and int(row[0]) == 4


def test_evening_does_not_come_before_its_time(env) -> None:
    assert tick(env, "21:59") == []
    assert "evening:%s" % DAY_S not in event_states(env)


# --------------------------------------------------------------------------
# 2. Дела: за 30 минут и кнопки действий
# --------------------------------------------------------------------------


def test_task_reminder_comes_30_min_before(env) -> None:
    """Дело в 15:00 — напоминание в 14:30 с четырьмя кнопками."""
    task_id = add_task(env, "Стоматолог", "15:00")
    assert tick(env, "14:29") == []

    outgoing = tick(env, "14:30")
    assert len(outgoing) == 1
    message = outgoing[0]
    assert message.kind == "task" and message.ref_id == task_id
    assert "Стоматолог" in message.text and "15:00" in message.text
    assert "30 минут" in message.text
    labels = [label for row in message.buttons for label, _ in row]
    assert labels == ["Сделал", "Отложить 1 ч", "На завтра", "Пропустить"]
    deliver(env, outgoing)
    assert event_states(env)["task:%d:%sT15:00:00" % (task_id, DAY_S)] == "sent"


def test_done_button_closes_task_and_api_sees_it(env) -> None:
    """«Сделал» закрывает дело; это видно через API (и в вебе)."""
    task_id = add_task(env, "Стоматолог", "15:00")
    message = tick(env, "14:30")[0]
    deliver(env, [message])

    outcome = env.service.reminders.handle_action(
        "r:done:%d" % message.event_id, now=moment(env, "14:31")
    )
    assert "Закрыл" in outcome.text and "Стоматолог" in outcome.text
    assert outcome.buttons == []

    task = task_row(env, task_id)
    assert str(task["status"]) == "done" and task["done_at"]

    response = env.client.get("/api/tasks", params={"date": DAY_S, "status": "all"})
    assert response.status_code == 200
    items = {int(item["id"]): item for item in response.json()["items"]}
    assert items[task_id]["status"] == "done"

    # Повторное нажатие не ломает данные и честно отвечает.
    again = env.service.reminders.handle_action(
        "r:done:%d" % message.event_id, now=moment(env, "14:32")
    )
    assert again.notification == "Уже обработано."


def test_postpone_resends_then_stops(env) -> None:
    """«Отложить 1 ч» — напоминание вернётся, но не больше лимита в день."""
    task_id = add_task(env, "Стоматолог", "15:00")
    message = tick(env, "14:30")[0]
    deliver(env, [message])

    outcome = env.service.reminders.handle_action(
        "r:postpone:%d" % message.event_id, now=moment(env, "14:31")
    )
    assert outcome.notification == "Отложено на час"
    assert tick(env, "15:00") == []  # ещё рано: отложено до 15:31

    second = tick(env, "15:31")
    assert len(second) == 1 and second[0].event_id == message.event_id
    deliver(env, second)
    sends = env.conn.execute(
        "SELECT sends FROM reminder_events WHERE id = ?", (message.event_id,)
    ).fetchone()[0]
    assert int(sends) == 2

    # Лимит (2 в день) исчерпан: кнопка честно отказывает, тик молчит.
    blocked = env.service.reminders.handle_action(
        "r:postpone:%d" % message.event_id, now=moment(env, "15:32")
    )
    assert "Больше не напомню" in blocked.notification
    assert tick(env, "16:31") == []
    assert str(task_row(env, task_id)["status"]) == "todo"


def test_tomorrow_button_moves_task(env) -> None:
    """«На завтра» сдвигает дело на сутки и снимает напоминание."""
    task_id = add_task(env, "Стоматолог", "15:00")
    message = tick(env, "14:30")[0]
    deliver(env, [message])

    outcome = env.service.reminders.handle_action(
        "r:tomorrow:%d" % message.event_id, now=moment(env, "14:31")
    )
    assert outcome.notification == "Перенёс на завтра"
    task = task_row(env, task_id)
    assert str(task["due_date"]) == (DAY + timedelta(days=1)).isoformat()
    assert str(task["start_at"]).startswith((DAY + timedelta(days=1)).isoformat())
    assert "15:00" in str(task["start_at"])
    assert event_states(env)["task:%d:%sT15:00:00" % (task_id, DAY_S)] == "moved"
    assert tick(env, "15:00") == []


def test_skip_button_silences_reminder(env) -> None:
    """«Пропустить» — до конца дня про дело молчим."""
    task_id = add_task(env, "Стоматолог", "15:00")
    message = tick(env, "14:30")[0]
    deliver(env, [message])

    outcome = env.service.reminders.handle_action(
        "r:skip:%d" % message.event_id, now=moment(env, "14:31")
    )
    assert outcome.notification == "Пропущено"
    assert tick(env, "14:45") == []
    assert tick(env, "15:00") == []
    assert str(task_row(env, task_id)["status"]) == "todo"


def test_task_without_time_is_not_reminded(env) -> None:
    """Дело без времени напоминаний не даёт: нечего отсчитывать."""
    add_task(env, "Подумать о жизни")
    assert tick(env, "14:30") == []
    assert tick(env, "15:00") == []


def test_stale_task_reminders_are_not_sent_in_a_batch(env) -> None:
    """Мак был выключен утром: пачка пропущенных дел не приходит."""
    add_task(env, "Дело 10:00", "10:00")
    add_task(env, "Дело 11:00", "11:00")
    add_task(env, "Дело 12:00", "12:00")

    assert tick(env, "13:00") == []
    states = event_states(env)
    stale = [state for key, state in states.items() if key.startswith("task:")]
    assert sorted(stale) == ["missed", "missed", "missed"]

    # А дело, до которого ещё есть время, напомнит нормально.
    add_task(env, "Дело 14:00", "14:00")
    outgoing = tick(env, "13:30")
    assert len(outgoing) == 1 and "Дело 14:00" in outgoing[0].text


# --------------------------------------------------------------------------
# 3. Тихие часы и жёсткие события
# --------------------------------------------------------------------------


def test_quiet_hours_block_regular_task(env) -> None:
    """00:15 — тихие часы: обычное дело ночью не будим."""
    task_id = add_task(env, "Ночная проверка", "00:15")
    assert tick(env, "00:00") == []
    assert event_states(env)["task:%d:%sT00:15:00" % (task_id, DAY_S)] == "missed"


def test_quiet_hours_allow_hard_event(env) -> None:
    """Жёсткое событие (resource='must') ночью приходит — владелец сам поставил."""
    task_id = add_task(env, "Поезд в аэропорт", "00:20", resource="must")
    outgoing = tick(env, "00:00")
    assert len(outgoing) == 1
    assert outgoing[0].ref_id == task_id
    assert "Поезд в аэропорт" in outgoing[0].text


def test_quiet_hours_block_ritual(env) -> None:
    """Ритуал в 07:00 попадает в тихие часы — не приходит."""
    env.service.reminders.save_setting("morning", "07:00")
    assert tick(env, "07:00") == []
    assert event_states(env)["morning:%s" % DAY_S] == "missed"


def test_quiet_window_after_midnight(env) -> None:
    """Тихие часы переходят через полночь и считаются по минутам."""
    quiet = (time(23, 0), time(8, 0))
    assert reminders.in_quiet_hours(moment(env, "23:00"), quiet)
    assert reminders.in_quiet_hours(moment(env, "03:30"), quiet)
    assert reminders.in_quiet_hours(moment(env, "07:59"), quiet)
    assert not reminders.in_quiet_hours(moment(env, "08:00"), quiet)
    assert not reminders.in_quiet_hours(moment(env, "22:59"), quiet)
    assert not reminders.in_quiet_hours(moment(env, "12:00"), quiet)


# --------------------------------------------------------------------------
# 4. Привычки
# --------------------------------------------------------------------------


def test_habits_reminder_and_mark_by_button(env) -> None:
    """20:00 — привычки, которые не отмечены; кнопка ставит отметку в БД."""
    water = add_habit(env, "Вода 2 литра")
    charge = add_habit(env, "Зарядка")

    assert tick(env, "19:59") == []
    outgoing = tick(env, "20:00")
    assert len(outgoing) == 1
    message = outgoing[0]
    assert message.kind == "habits"
    assert "Вода 2 литра" in message.text and "Зарядка" in message.text
    assert len(message.buttons) == 3  # две привычки + «Пропустить»
    deliver(env, outgoing)

    first = env.service.reminders.handle_action(
        "r:habit:%d:%d" % (water, message.event_id), now=moment(env, "20:01")
    )
    assert "отмечена" in first.text and "Зарядка" in first.text
    marks = env.conn.execute(
        "SELECT habit_id FROM habit_marks WHERE date = ?", (DAY_S,)
    ).fetchall()
    assert [int(row[0]) for row in marks] == [water]

    second = env.service.reminders.handle_action(
        "r:habit:%d:%d" % (charge, message.event_id), now=moment(env, "20:02")
    )
    assert "все привычки" in second.text.lower()
    assert second.buttons == []
    assert tick(env, "20:30") == []


def test_habits_not_reminded_when_all_marked(env) -> None:
    """Все отмечены — сообщения нет вовсе."""
    habit_id = add_habit(env, "Вода 2 литра")
    with env.conn:
        env.conn.execute(
            "INSERT INTO habit_marks (habit_id, date, value) VALUES (?, ?, 1)",
            (habit_id, DAY_S),
        )
    assert tick(env, "20:00") == []
    assert event_states(env)["habits:%s" % DAY_S] == "done"


# --------------------------------------------------------------------------
# 5. /mute, /unmute и настройка расписания командами
# --------------------------------------------------------------------------


def test_mute_and_unmute(env) -> None:
    """/mute выключает всё расписание, /unmute включает обратно."""
    assert "Напоминания выключены" in env.service.handle_mute().text
    assert tick(env, "09:00") == []
    assert tick(env, "20:00") == []
    assert event_states(env) == {}

    assert "Напоминания включены" in env.service.handle_unmute().text
    outgoing = tick(env, "09:00")
    assert len(outgoing) == 1 and outgoing[0].kind == "morning"


def test_reminders_command_shows_schedule(env) -> None:
    reply = env.service.handle_reminders("/reminders")
    for needle in ("Напоминания", "09:00", "22:00", "20:00", "Тихие часы", "/mute"):
        assert needle in reply.text


def test_reminders_command_changes_times(env) -> None:
    """`/reminders morning 10:15` меняет расписание, старый слот не срабатывает."""
    reply = env.service.handle_reminders("/reminders morning 10:15")
    assert "10:15" in reply.text

    assert tick(env, "09:00") == []
    outgoing = tick(env, "10:15")
    assert len(outgoing) == 1 and outgoing[0].kind == "morning"


def test_reminders_command_changes_lead_and_max(env) -> None:
    """Время напоминания о деле и лимит в день настраиваются командами."""
    assert "15 мин" in env.service.handle_reminders("/reminders lead 15").text
    task_id = add_task(env, "Стоматолог", "15:00")
    assert tick(env, "14:44") == []
    message = tick(env, "14:45")[0]
    assert message.ref_id == task_id

    assert "напоминание" in env.service.handle_reminders("/reminders max 1").text
    deliver(env, [message])
    blocked = env.service.reminders.handle_action(
        "r:postpone:%d" % message.event_id, now=moment(env, "14:46")
    )
    assert "Больше не напомню" in blocked.notification


def test_reminders_command_changes_quiet_hours(env) -> None:
    """`/reminders quiet 21:00-09:00` — тишина начинается раньше, дело молчит."""
    task_id = add_task(env, "Вечернее дело", "21:20")
    # По умолчанию (23:00–08:00) напоминание в 21:00 пришло бы.
    assert len(tick(env, "21:00")) == 1

    env.conn.execute("DELETE FROM reminder_events")
    env.conn.commit()
    reply = env.service.handle_reminders("/reminders quiet 21:00-09:00")
    assert "21:00" in reply.text and "09:00" in reply.text
    assert tick(env, "21:00") == []
    assert event_states(env)["task:%d:%sT21:20:00" % (task_id, DAY_S)] == "missed"


def test_reminders_command_rejects_garbage(env) -> None:
    reply = env.service.handle_reminders("/reminders morning когда-нибудь")
    assert "ЧЧ:ММ" in reply.text
    unknown = env.service.handle_reminders("/reminders погода")
    assert "Не понял" in unknown.text


# --------------------------------------------------------------------------
# 6. Ручные ритуалы /morning и /evening
# --------------------------------------------------------------------------


def test_morning_command_sends_ritual_now(env) -> None:
    reply = env.service.handle_morning()
    assert "Ритуал" in reply.text
    assert [label for row in reply.buttons for label, _ in row][:1] == ["Отложить 1 ч"]

    # После ручного запуска планировщик не пришлёт то же самое ещё раз.
    assert tick(env, "09:00") == []
    assert event_states(env)["morning:%s" % DAY_S] == "sent"


def test_evening_command_sends_digest_now(env) -> None:
    reply = env.service.handle_evening()
    assert "Итог дня" in reply.text
    assert reply.buttons and reply.buttons[0][0][0] == "1"
    assert tick(env, "22:00") == []


# --------------------------------------------------------------------------
# 7. Перезапуск службы: состояние в БД, дублей нет
# --------------------------------------------------------------------------


def test_restart_keeps_schedule_and_does_not_repeat(env) -> None:
    """Новый процесс бота видит ту же базу: расписание цело, отправленное не дублируется."""
    env.service.handle_reminders("/reminders morning 09:30")
    first = tick(env, "09:30")
    assert len(first) == 1
    deliver(env, first)

    # «Перезапуск»: новый BotService и его движок на той же базе.
    restarted = handlers.BotService(
        env.settings, env.conn, v4_router=intent_v4.V4Router(gateway=None)
    )
    schedule = restarted.reminders.load_settings()
    assert schedule.morning == time(9, 30)
    assert restarted.reminders.tick(now=moment(env, "09:35")) == []

    # Другое дело того же дня по-прежнему напомнит.
    add_task(env, "Стоматолог", "15:00")
    assert len(restarted.reminders.tick(now=moment(env, "14:30"))) == 1


def test_mute_survives_restart(env) -> None:
    env.service.handle_mute()
    restarted = handlers.BotService(
        env.settings, env.conn, v4_router=intent_v4.V4Router(gateway=None)
    )
    assert restarted.reminders.load_settings().enabled is False
    assert restarted.reminders.tick(now=moment(env, "09:00")) == []


def test_sending_state_recovers_after_crash(env) -> None:
    """Процесс упал между тиком и отправкой: повторяется, но не сразу и не вечно."""
    task_id = add_task(env, "Стоматолог", "15:00")
    first = tick(env, "14:30")
    assert len(first) == 1  # событие помечено sending, но не отправлено
    key = "task:%d:%sT15:00:00" % (task_id, DAY_S)
    assert event_states(env)[key] == "sending"
    # Сразу после сбоя не дублируем: ждём RETRY_DELAY_S.
    assert tick(env, "14:31") == []
    retry = tick(env, "14:33")
    assert len(retry) == 1 and retry[0].event_id == first[0].event_id
    deliver(env, retry)
    assert event_states(env)[key] == "sent"


# --------------------------------------------------------------------------
# 8. Отправка и разметка кнопок
# --------------------------------------------------------------------------


def test_buttons_fit_telegram_limit(env) -> None:
    """callback_data не длиннее 64 байт — иначе Telegram отклонит кнопку."""
    add_habit(env, "Вода 2 литра")
    for outgoing in tick(env, "20:00"):
        for row in outgoing.buttons:
            for _label, data in row:
                assert len(data.encode("utf-8")) <= 64


def test_no_chat_id_means_no_messages(env) -> None:
    """Владелец не задан — писать некому, тик молчит."""
    import dataclasses

    silent = dataclasses.replace(env.settings, allowed_chat_id=0)
    engine = reminders.ReminderEngine(silent, env.conn)
    assert engine.tick(now=moment(env, "09:00")) == []
    assert engine.chat_id() == 0


def test_markdown_free_plain_text(env) -> None:
    """Тексты без markdown-звёздочек: бот шлёт plain text."""
    add_task(env, "Стоматолог", "15:00")
    for hhmm in ("09:00", "14:30", "20:00", "22:00"):
        for outgoing in tick(env, hhmm):
            assert "*" not in outgoing.text and "_" not in outgoing.text
            deliver(env, [outgoing])


# --------------------------------------------------------------------------
# 9. Telegram-цикл: команды роутера и разметка
# --------------------------------------------------------------------------


def test_router_registers_reminder_commands(env) -> None:
    """Команды /mute, /unmute, /morning, /evening, /reminders есть в роутере."""
    router = handlers.build_router(env.service)
    names = set()
    for handler in router.message.handlers:
        for filter_ in handler.filters:
            commands = getattr(filter_.callback, "commands", None)
            if commands:
                names.update(str(command) for command in commands)
    assert {"mute", "unmute", "morning", "evening", "reminders"} <= names


def test_reminder_markup_builds_inline_keyboard(env, monkeypatch) -> None:
    """Цикл бота превращает кнопки напоминания в inline-клавиатуру."""
    from app.bot import main as bot_main

    markup = bot_main.reminder_markup(
        [[("Сделал", "r:done:1"), ("Отложить 1 ч", "r:postpone:1")]]
    )
    assert markup is not None
    assert len(markup.inline_keyboard) == 1
    assert markup.inline_keyboard[0][0].callback_data == "r:done:1"
    assert bot_main.reminder_markup([]) is None
    assert bot_main.reminder_markup([[("Слишком длинная кнопка", "r:" + "x" * 80)]]) is None


# --------------------------------------------------------------------------
# 10. Отправка упала: повтор и отказ после трёх попыток
# --------------------------------------------------------------------------


def test_failed_send_retries_after_delay(env) -> None:
    """Telegram не принял сообщение — повтор позже, а не мгновенно."""
    task_id = add_task(env, "Стоматолог", "15:00")
    message = tick(env, "14:30")[0]
    env.service.reminders.mark_failed(message.event_id, now=moment(env, "14:31"))

    row = env.conn.execute(
        "SELECT state, next_at FROM reminder_events WHERE id = ?", (message.event_id,)
    ).fetchone()
    assert str(row["state"]) == "postponed" and row["next_at"]
    assert tick(env, "14:32") == []  # пауза ещё идёт
    retried = tick(env, "14:34")
    assert len(retried) == 1 and retried[0].event_id == message.event_id
    deliver(env, retried)
    assert str(task_row(env, task_id)["status"]) == "todo"


def test_failed_send_gives_up_after_three_attempts(env) -> None:
    """Три неудачных попытки — событие закрывается, без бесконечных повторов."""
    add_task(env, "Стоматолог", "15:00")
    message = tick(env, "14:30")[0]  # попытка 1

    env.service.reminders.mark_failed(message.event_id, now=moment(env, "14:31"))
    assert tick(env, "14:32") == []  # пауза перед повтором
    assert tick(env, "14:34") != []  # попытка 2

    env.service.reminders.mark_failed(message.event_id, now=moment(env, "14:35"))
    assert tick(env, "14:36") == []
    assert tick(env, "14:38") != []  # попытка 3

    env.service.reminders.mark_failed(message.event_id, now=moment(env, "14:39"))
    row = env.conn.execute(
        "SELECT state, attempts FROM reminder_events WHERE id = ?", (message.event_id,)
    ).fetchone()
    assert str(row["state"]) == "failed" and int(row["attempts"]) == 3
    assert tick(env, "14:45") == []


# --------------------------------------------------------------------------
# 11. Петля бота: tick из main.py действительно отправляет и отмечает
# --------------------------------------------------------------------------


def test_loop_sends_reminder_and_marks_it(env, monkeypatch) -> None:
    """`_reminders_loop` из main.py: сообщение ушло ботом и записано в БД."""
    import asyncio

    from app.bot import main as bot_main

    class FakeBot:
        def __init__(self) -> None:
            self.sent: List[Any] = []

        async def send_message(self, chat_id: int, text: str, reply_markup: Any = None):
            self.sent.append(
                SimpleNamespace(chat_id=chat_id, text=text, markup=reply_markup)
            )
            return SimpleNamespace(message_id=777)

    fixed = moment(env, "09:00")
    monkeypatch.setattr(env.service.reminders, "now", lambda: fixed)
    monkeypatch.setenv(bot_main.REMINDER_TICK_ENV, "0.2")

    fake = FakeBot()

    async def scenario() -> None:
        task = asyncio.create_task(bot_main._reminders_loop(fake, env.service))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if fake.sent:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert len(fake.sent) == 1, "петля не отправила напоминание"
    assert "Ритуал" in fake.sent[0].text
    assert fake.sent[0].chat_id == CHAT_ID
    assert fake.sent[0].markup is not None, "у ритуала нет кнопок"
    row = env.conn.execute(
        "SELECT state, message_id FROM reminder_events WHERE kind = 'morning'"
    ).fetchone()
    assert str(row["state"]) == "sent" and int(row["message_id"]) == 777


def test_quiet_hours_default_comes_from_config(env) -> None:
    """Если в kv ничего не задано, тихие часы берутся из config `app.quiet_hours`."""
    import dataclasses

    custom = dataclasses.replace(env.settings, quiet_hours=("22:00", "07:00"))
    engine = reminders.ReminderEngine(custom, env.conn)
    schedule = engine.load_settings()
    assert schedule.quiet == (time(22, 0), time(7, 0))
    assert "22:00–07:00" in engine.describe()

    # И веб/команда перекрывают конфиг: ключ kv сильнее.
    engine.save_setting("quiet", "23:30-06:30")
    assert engine.load_settings().quiet == (time(23, 30), time(6, 30))
