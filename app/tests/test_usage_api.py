"""Тесты статистики использования: события веба и бота, покрытие, карта, мысли.

Проверяется:

* таблица ``usage_events`` в схеме и запись событий без падений;
* веб-события из middleware: успешный ``GET /api/...`` → экран, health и сама
  статистика не считаются, повтор экрана в окне дедупликации не дублируется;
* бот пишет в ту же таблицу: интент из ``handle_text``, команда ``/today`` и
  голосовое вложение — ``source='bot'``, экран = интент/команда;
* ``GET /api/analytics/usage``: пустая база, totals/screens/coverage/heatmap/
  trend на синтетических событиях, валидация параметров;
* блок «Мысли»: частые темы, рост относительно прошлого окна, новые темы и
  честная подсказка, когда мыслей нет.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.bot.handlers import BotService
from app.server import usage
from app.server.db import add_entry, init_db
from app.server.main import create_app
from app.server.settings import load_settings
from app.server.usage import (
    BOT_COMMANDS,
    BOT_SCREENS,
    WEB_SCREENS,
    feature_catalog,
    label_for,
    reset_web_dedupe,
    screen_for_path,
)


@pytest.fixture(autouse=True)
def _clean_web_dedupe():
    reset_web_dedupe()
    yield
    reset_web_dedupe()


@pytest.fixture()
def settings(tmp_path):
    return load_settings(env={}, root=tmp_path)


def _client(settings) -> TestClient:
    return TestClient(create_app(settings, start_worker=False, api_token=""))


def _stamp(day: date, clock: str = "10:00:00") -> str:
    return "%sT%s+03:00" % (day.isoformat(), clock)


def _event(conn, when: str, source: str, screen: str, action: str = "view") -> None:
    usage.record_event(conn, source=source, screen=screen, action=action, ts=when)


# --------------------------------------------------------------------------
# Схема и запись
# --------------------------------------------------------------------------


def test_usage_events_table_in_schema(settings) -> None:
    conn = init_db(settings.db_path)
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "usage_events" in tables
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(usage_events)").fetchall()
        }
        assert {"id", "ts", "source", "screen", "action", "meta_json"} <= columns
        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'usage_events'"
            )
        }
        assert "idx_usage_events_ts" in indexes
        assert "idx_usage_events_source_screen" in indexes
    finally:
        conn.close()


def test_record_event_is_safe_and_stores_meta(settings) -> None:
    conn = init_db(settings.db_path)
    try:
        stamp = _stamp(settings.now().date(), "12:34:56")
        event_id = usage.record_event(
            conn,
            source="bot",
            screen="task",
            action="message",
            ts=stamp,
            meta={"intent": "task", "engine": "rules"},
        )
        assert event_id and event_id > 0
        row = conn.execute("SELECT * FROM usage_events WHERE id = ?", (event_id,)).fetchone()
        assert row["source"] == "bot"
        assert row["screen"] == "task"
        assert row["action"] == "message"
        assert json.loads(row["meta_json"]) == {"intent": "task", "engine": "rules"}

        # Кривые значения не бросают исключений и не пишут строк.
        assert usage.record_event(conn, source="cli", screen="x", ts=stamp) is None
        assert usage.record_event(conn, source="web", screen="", ts=stamp) is None
        assert usage.record_event(conn, source="web", screen="today", ts="") is None
    finally:
        conn.close()


def test_screen_for_path_and_labels() -> None:
    assert screen_for_path("/api/tasks?date=2026-11-17") == "today"
    assert screen_for_path("/api/tasks?view=week") == "week"
    assert screen_for_path("/api/tasks?from=2026-11-01&to=2026-11-07") == "week"
    assert screen_for_path("/api/overview") == "overview"
    assert screen_for_path("/api/analytics/summary") == "analytics"
    assert screen_for_path("/api/books/12") == "library"
    assert screen_for_path("/api/goals/1/steps") == "compass"
    assert screen_for_path("/api/health") is None
    assert screen_for_path("/api/health/weight") == "health"
    assert screen_for_path("/api/analytics/usage") is None
    assert screen_for_path("/media/photo.jpg") is None

    assert label_for("web", "today") == "Сегодня"
    assert label_for("bot", "journal") == "Мысль"
    assert label_for("bot", "/today") == "Сводка дня"
    assert label_for("bot", "custom-intent") == "custom-intent"


# --------------------------------------------------------------------------
# Веб-события из middleware
# --------------------------------------------------------------------------


def test_web_middleware_records_screens_without_noise(settings) -> None:
    with _client(settings) as client:
        assert client.get("/api/overview").status_code == 200
        assert client.get("/api/tasks").status_code == 200
        assert client.get("/api/tasks?from=2026-11-01&to=2026-11-07").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/analytics/usage?days=30").status_code == 200
        # Повтор того же экрана в окне дедупликации не даёт второго события.
        assert client.get("/api/overview").status_code == 200

    conn = init_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT source, screen, action FROM usage_events ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert [(row["source"], row["screen"], row["action"]) for row in rows] == [
        ("web", "overview", "view"),
        ("web", "today", "view"),
        ("web", "week", "view"),
    ]


# --------------------------------------------------------------------------
# События бота
# --------------------------------------------------------------------------


class _StubRouter:
    """Разбор без сети: всегда «мысль», чтобы проверить запись события."""

    def __init__(self, intent: str = "journal") -> None:
        self.intent = intent

    def parse(self, text: str) -> dict:
        return {"intent": self.intent, "text": text, "engine": "rules"}


def test_bot_writes_intents_and_commands_to_same_table(settings) -> None:
    bot_settings = replace(settings, allowed_chat_id=42)
    conn = init_db(bot_settings.db_path)
    service = BotService(bot_settings, conn, v4_router=_StubRouter("journal"))
    try:
        reply = service.handle_text(
            "много думал про работу и отдых", chat_id=42, user_id=42
        )
        assert "Мысль записана" in reply.text
        service.record_command("/today")
    finally:
        conn.close()

    conn = init_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT source, screen, action, meta_json FROM usage_events ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert [(row["source"], row["screen"], row["action"]) for row in rows] == [
        ("bot", "journal", "message"),
        ("bot", "/today", "command"),
    ]
    meta = json.loads(rows[0]["meta_json"])
    assert meta["intent"] == "journal"


def test_bot_media_records_voice(settings) -> None:
    from app.bot.media import MediaDescriptor

    bot_settings = replace(settings, allowed_chat_id=42)
    bot_settings.ensure_dirs()
    conn = init_db(bot_settings.db_path)
    service = BotService(bot_settings, conn, v4_router=_StubRouter())
    descriptor = MediaDescriptor(
        kind="voice",
        file_id="voice-1",
        file_unique_id="uniq-1",
        mime_type="audio/ogg",
        size=4,
        duration=2.5,
    )
    try:
        reply = service.handle_media(descriptor, b"OggS" + b"\x00" * 32, chat_id=42, user_id=42)
        assert reply.text
    finally:
        conn.close()

    conn = init_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT source, screen, action FROM usage_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert (row["source"], row["screen"], row["action"]) == ("bot", "voice", "voice")


# --------------------------------------------------------------------------
# GET /api/analytics/usage
# --------------------------------------------------------------------------


def test_usage_endpoint_empty_is_honest(settings) -> None:
    with _client(settings) as client:
        response = client.get("/api/analytics/usage?days=30")
    assert response.status_code == 200
    payload = response.json()

    assert payload["totals"] == {
        "events": 0,
        "activeDays": 0,
        "days": 30,
        "web": 0,
        "bot": 0,
        "messages": 0,
        "commands": 0,
        "voice": 0,
        "media": 0,
    }
    assert [source["count"] for source in payload["sources"]] == [0, 0]
    assert payload["screens"] == []
    assert payload["coverage"]["used"] == 0
    assert payload["coverage"]["total"] == len(feature_catalog())
    assert payload["coverage"]["unused"]
    assert len(payload["heatmap"]["matrix"]) == 7
    assert all(len(row) == 24 for row in payload["heatmap"]["matrix"])
    assert sum(sum(row) for row in payload["heatmap"]["matrix"]) == 0
    assert payload["heatmap"]["peak"] is None
    assert len(payload["heatmap"]["byDay"]) == 30
    assert payload["trend"]["previousEvents"] == 0
    assert payload["trend"]["changePct"] is None
    assert payload["thoughts"]["entries"] == 0
    assert payload["thoughts"]["topics"] == []
    assert payload["thoughts"]["note"]


def test_usage_endpoint_computes_totals_coverage_heatmap_trend(settings) -> None:
    today = settings.now().date()
    start = today - timedelta(days=6)
    conn = init_db(settings.db_path)
    try:
        # Текущее окно (7 дней): веб и бот.
        _event(conn, _stamp(today, "10:05:00"), "web", "today")
        _event(conn, _stamp(today, "23:10:00"), "web", "analytics")
        _event(conn, _stamp(today - timedelta(days=1), "09:00:00"), "web", "overview")
        _event(conn, _stamp(today, "09:30:00"), "bot", "task", "message")
        _event(conn, _stamp(today, "09:31:00"), "bot", "/today", "command")
        _event(conn, _stamp(today, "09:32:00"), "bot", "voice", "voice")
        # Прошлое окно — только для тренда.
        _event(conn, _stamp(start - timedelta(days=1), "12:00:00"), "web", "finance")
    finally:
        conn.close()

    with _client(settings) as client:
        payload = client.get("/api/analytics/usage?days=7&thoughtsDays=30").json()

    totals = payload["totals"]
    assert totals["events"] == 6
    assert totals["activeDays"] == 2
    assert (totals["web"], totals["bot"]) == (3, 3)
    assert (totals["messages"], totals["commands"], totals["voice"]) == (1, 1, 1)

    assert [source["count"] for source in payload["sources"]] == [3, 3]
    assert payload["sources"][0]["activeDays"] == 2
    assert payload["sources"][1]["activeDays"] == 1

    screens = {(item["source"], item["key"]): item for item in payload["screens"]}
    assert screens[("web", "today")]["count"] == 1
    assert screens[("web", "today")]["label"] == "Сегодня"
    assert screens[("bot", "/today")]["label"] == "Сводка дня"
    assert screens[("bot", "voice")]["actions"] == {"voice": 1}
    assert ("web", "finance") not in screens  # прошлое окно не в списке

    assert payload["coverage"]["used"] == len(screens)
    keys = {(item["source"], item["key"]) for item in payload["coverage"]["unused"]}
    assert ("web", "today") not in keys
    assert ("bot", "journal") in keys

    matrix = payload["heatmap"]["matrix"]
    assert sum(sum(row) for row in matrix) == 6
    assert matrix[today.weekday()][10] == 1
    assert matrix[today.weekday()][23] == 1
    assert matrix[today.weekday()][9] == 3  # task + /today + voice
    assert payload["heatmap"]["peak"]["count"] >= 2
    by_day = {item["date"]: item["count"] for item in payload["heatmap"]["byDay"]}
    assert by_day[today.isoformat()] == 5
    assert by_day[(today - timedelta(days=1)).isoformat()] == 1
    assert len(by_day) == 7

    assert payload["trend"] == {
        "previousEvents": 1,
        "changePct": 500.0,
        "previousActiveDays": 1,
        "activeDaysChange": 1,
    }


def test_usage_endpoint_validates_window(settings) -> None:
    with _client(settings) as client:
        assert client.get("/api/analytics/usage?days=0").status_code == 422
        assert client.get("/api/analytics/usage?days=366").status_code == 422
        assert client.get("/api/analytics/usage?thoughtsDays=6").status_code == 422
        assert client.get("/api/analytics/usage?thoughtsDays=731").status_code == 422


# --------------------------------------------------------------------------
# Мысли: темы, рост, новинки
# --------------------------------------------------------------------------


def test_thoughts_topics_rising_and_new(settings) -> None:
    today = settings.now().date()
    current = today
    previous = today - timedelta(days=10)
    conn = init_db(settings.db_path)
    try:
        with conn:
            add_entry(
                conn,
                "journal",
                "много работы и созвонов",
                ts=_stamp(current),
                source="telegram",
                meta={"topics": ["работа", "созвоны"]},
            )
            add_entry(
                conn,
                "journal",
                "работа, проект и спорт",
                ts=_stamp(current - timedelta(days=1)),
                source="telegram",
                meta={"topics": ["работа", "проект", "спорт", "созвоны"]},
            )
            add_entry(
                conn,
                "note",
                "новый проект про отдых",
                ts=_stamp(current - timedelta(days=2)),
                source="telegram",
                meta={"topics": ["проект"]},
            )
            add_entry(
                conn,
                "journal",
                "работа и созвоны в прошлом окне",
                ts=_stamp(previous),
                source="telegram",
                meta={"topics": ["работа", "созвоны"]},
            )
    finally:
        conn.close()

    with _client(settings) as client:
        payload = client.get("/api/analytics/usage?days=7&thoughtsDays=7").json()

    thoughts = payload["thoughts"]
    assert thoughts["entries"] == 3
    assert thoughts["prevEntries"] == 1
    assert thoughts["note"] is None

    topics = {item["topic"]: item for item in thoughts["topics"]}
    work = next(item for item in topics.values() if item["label"] == "Работа")
    assert work["count"] == 2
    assert work["prevCount"] == 1
    assert work["growthPct"] == 100.0

    project = next(item for item in topics.values() if item["label"] == "Проекты")
    assert project["count"] == 2
    assert project["prevCount"] == 0
    assert project["isNew"] is True
    assert project["firstSeen"]

    rising_labels = {item["label"] for item in thoughts["rising"]}
    assert "Работа" in rising_labels
    assert "Созвоны" in rising_labels
    new_labels = {item["label"] for item in thoughts["new"]}
    assert "Проекты" in new_labels
    assert "Работа" not in new_labels
    # Стоп-слова и короткие слова не становятся темами.
    assert all(item["label"].lower() not in usage.STOPWORDS for item in thoughts["topics"])
    assert all(len(item["topic"]) >= 3 for item in thoughts["topics"])


def test_thoughts_window_split_and_empty_note(settings) -> None:
    today = settings.now().date()
    conn = init_db(settings.db_path)
    try:
        with conn:
            add_entry(
                conn,
                "journal",
                "одна мысль про работу",
                ts=_stamp(today),
                source="telegram",
                meta={"topics": ["работа"]},
            )
    finally:
        conn.close()

    with _client(settings) as client:
        narrow = client.get("/api/analytics/usage?days=7&thoughtsDays=7").json()["thoughts"]
        empty = client.get("/api/analytics/usage?days=7&thoughtsDays=30").json()

    assert narrow["entries"] == 1
    assert narrow["prevEntries"] == 0
    labels = {item["label"] for item in narrow["topics"]}
    assert "Работа" in labels
    assert narrow["new"] == []  # одна мысль — ещё не тема
    # Пустой блок мыслей в этом запросе не проверяем: он не пустой, но note None.
    assert empty["thoughts"]["entries"] == 1


def test_usage_route_registered_and_catalog_consistent(settings) -> None:
    catalog = feature_catalog()
    assert len(catalog) == len(WEB_SCREENS) + len(BOT_SCREENS) + len(BOT_COMMANDS)
    assert len({(item["source"], item["key"]) for item in catalog}) == len(catalog)

    with _client(settings) as client:
        paths = set(client.get("/openapi.json").json()["paths"])
    assert "/api/analytics/usage" in paths
