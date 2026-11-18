"""Практики в боте (t35): очки, /points, повтор без дубля и напоминание.

Проверяется путь «фраза → сессия практики → очки → /points» и честный повтор
той же практики в тот же день: очки начисляются один раз, а ответ говорит
«уже учтено». Отдельно — что практика видна в итоге дня (напоминание).
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.bot import handlers, intent_v4
from app.server import db
from app.server.main import create_app
from app.server.settings import load_settings

CHAT_ID = 4343


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
        yield SimpleNamespace(settings=settings, conn=conn, service=service, client=client)


def send(env, text):
    return env.service.handle_text(text, CHAT_ID)


def points_sum(env):
    return int(
        env.conn.execute(
            "SELECT COALESCE(SUM(points), 0) FROM points_ledger WHERE kind = 'practice'"
        ).fetchone()[0]
    )


def test_practice_awards_once_and_repeat_is_honest(env) -> None:
    first = send(env, "делаю дыхательную практику вим хоффа")
    assert "очк" in first.text.lower()
    assert points_sum(env) == 10

    again = send(env, "делаю дыхательную практику вим хоффа")
    assert "уже учтено" in again.text.lower(), again.text
    assert points_sum(env) == 10, "повтор не должен начислять очки второй раз"


def test_repeat_with_explicit_rounds_keeps_best_rounds(env) -> None:
    send(env, "вим хоф 1 круг")
    second = send(env, "вимхоф 3 круга")
    assert "уже учтено" in second.text.lower(), second.text
    row = env.conn.execute(
        "SELECT rounds_done FROM practice_sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert int(row["rounds_done"]) == 3, "повтор уточняет число кругов (MAX)"
    assert points_sum(env) == 10


def test_points_command_reports_balance_and_streak(env) -> None:
    send(env, "делаю дыхательную практику боксовое дыхание")
    reply = env.service.handle_points()
    lowered = reply.text.lower()
    assert "очк" in lowered
    assert "серия" in lowered
    assert "10" in reply.text
    # Событие использования записано — статистика видит /points.
    total = env.conn.execute(
        "SELECT COUNT(*) FROM usage_events WHERE action = 'command' AND screen = '/points'"
    ).fetchone()[0]
    assert int(total) >= 1


def test_practice_is_visible_in_day_summary(env) -> None:
    day = env.settings.today()
    empty = env.service.handle_today().text
    assert "практик" in empty.lower()

    send(env, "подышал 2 раунда вим хоф")
    filled = env.service.handle_today().text
    assert "Практика:" in filled
    assert "2" in filled.split("Практика:", 1)[1][:40]

    assert date.fromisoformat(day) == date.fromisoformat(env.settings.today())
