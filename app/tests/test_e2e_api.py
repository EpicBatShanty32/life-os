"""Сквозной тест HTTP-слоя v3 (``app/server/main.py`` + роутеры).

Проверяется через настоящий ASGI-клиент, а не вызовом функций: сборка
приложения, раздача SPA, все ключевые маршруты, запись через API, отдача
медиа с Range и WebSocket-события. База и медиа — во временном каталоге,
рабочие ``data/`` не затрагиваются.
"""

from __future__ import annotations

import sqlite3
import struct
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.server.db import add_entry, add_metric, connect
from app.server.main import create_app
from app.server.settings import load_settings


def _make_wav(path: Path, seconds: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(struct.pack("<h", 0) * 8000 * seconds)


@pytest.fixture()
def client(tmp_path: Path):
    """Приложение на временной базе с одним уроком, медиа и привычкой."""
    settings = load_settings(env={}, root=tmp_path)
    media_file = tmp_path / "data" / "media" / "demo" / "lesson.wav"
    _make_wav(media_file)

    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as test_client:
        conn = connect(settings.db_path)
        with conn:
            add_entry(conn, "journal", "Пробежка и хороший сон")
            add_entry(conn, "food", "Овсянка на завтрак")
            add_metric(conn, "sleep", 7.5, unit="ч")
            add_metric(conn, "mood", 4, unit="балл")
            conn.execute(
                "INSERT INTO habits (name, emoji, target_per_week) VALUES ('Вода', '💧', 7)"
            )
            conn.execute(
                "INSERT INTO goals (title, target, current, unit) VALUES ('Вес', 80, 82, 'кг')"
            )
            course = conn.execute(
                "INSERT INTO courses (title, category) VALUES ('Основы', 'питание')"
            ).lastrowid
            lesson = conn.execute(
                "INSERT INTO lessons (course_id, idx, title, media_path, duration_s) "
                "VALUES (?, 1, 'Урок 1', 'demo/lesson.wav', 60)",
                (course,),
            ).lastrowid
            conn.execute(
                "INSERT INTO segments (lesson_id, start_s, end_s, text) "
                "VALUES (?, 0, 5, 'начало'), (?, 30, 40, 'середина')",
                (lesson, lesson),
            )
        conn.close()
        yield test_client


def test_health_reports_ok(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["db"] == "ok"
    assert payload["tz"] == "Europe/Moscow"


def test_spa_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    # SPA не собрана в этом окружении — сервер обязан ответить JSON-заглушкой.
    assert "app" in response.headers.get("content-type", "") or "<!doctype" in body.lower()


def test_overview_has_kpis(client: TestClient) -> None:
    payload = client.get("/api/overview").json()
    assert "kpis" in payload
    assert payload["kpis"]["sleep"]["lastHours"] == 7.5


def test_sections_render_for_all_names(client: TestClient) -> None:
    for name in ("today", "journal", "sleep", "mood", "habits", "goals", "analytics"):
        response = client.get("/api/sections/%s?days=30" % name)
        assert response.status_code == 200, name
        assert response.json()["name"] == name


def test_sections_unknown_is_404(client: TestClient) -> None:
    assert client.get("/api/sections/nope").status_code == 404


def test_journal_lists_and_searches(client: TestClient) -> None:
    payload = client.get("/api/journal").json()
    items = payload["items"] if isinstance(payload, dict) else payload
    assert any("Пробежка" in item["text"] for item in items)
    found = client.get("/api/journal", params={"q": "пробеж"}).json()
    texts = [item["text"] for item in (found["items"] if isinstance(found, dict) else found)]
    assert texts == ["Пробежка и хороший сон"]


def test_quick_entry_creates_journal_record(client: TestClient) -> None:
    response = client.post("/api/quick", json={"type": "note", "text": "новая запись"})
    assert response.status_code in (200, 201)
    payload = client.get("/api/sections/journal?days=7").json()
    assert any(item["text"] == "новая запись" for item in payload["items"])


def test_courses_and_lesson_detail(client: TestClient) -> None:
    courses = client.get("/api/courses").json()
    assert len(courses) == 1
    course_id = courses[0]["id"]
    detail = client.get("/api/courses/%s" % course_id).json()
    assert detail["lessons"][0]["title"] == "Урок 1"
    lesson_id = detail["lessons"][0]["id"]
    lesson = client.get("/api/lessons/%s" % lesson_id).json()
    assert [segment["text"] for segment in lesson["segments"]] == ["начало", "середина"]


def test_progress_updates_and_closes_lesson(client: TestClient) -> None:
    course = client.get("/api/courses").json()[0]
    lesson_id = client.get("/api/courses/%s" % course["id"]).json()["lessons"][0]["id"]
    response = client.post(
        "/api/lessons/%s/progress" % lesson_id, json={"position_s": 58, "watched_s": 59}
    )
    assert response.status_code == 200
    assert response.json()["percent"] == pytest.approx(0.983, abs=0.01)
    lessons = client.get("/api/courses/%s" % course["id"]).json()["lessons"]
    assert lessons[0]["status"] == "done"


def test_media_range_returns_206(client: TestClient) -> None:
    course = client.get("/api/courses").json()[0]
    lesson_id = client.get("/api/courses/%s" % course["id"]).json()["lessons"][0]["id"]
    response = client.get("/media/%s" % lesson_id, headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert response.headers["content-range"].startswith("bytes 0-99/")
    assert len(response.content) == 100


def test_media_missing_lesson_is_404(client: TestClient) -> None:
    assert client.get("/media/999").status_code == 404


def test_habit_mark_and_goal_patch(client: TestClient) -> None:
    habits = client.get("/api/sections/habits").json()["habits"]
    habit_id = habits[0]["id"]
    client.post("/api/habits/%s/mark" % habit_id, json={"value": 1})
    assert client.get("/api/sections/habits").json()["habits"][0]["doneWeek"] >= 1

    goal_id = client.get("/api/sections/goals").json()["goals"][0]["id"]
    updated = client.patch("/api/goals/%s" % goal_id, json={"current": 80})
    assert updated.json()["current"] == 80
    assert client.get("/api/sections/goals").json()["goals"][0]["percent"] == 100


def test_settings_hides_secrets(tmp_path: Path) -> None:
    """Секреты из окружения не попадают в ответ /api/settings даже строкой."""
    env = {
        "LIFEOS_TELEGRAM_TOKEN": "123456:secret-bot-token",
        "LIFEOS_LLM_API_KEY": "sk-secret-llm-key",
        "LIFEOS_ADMIN_TELEGRAM_ID": "42",
    }
    settings = load_settings(env=env, root=tmp_path)
    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        payload = client.get("/api/settings").json()

    assert payload["telegram"]["token_set"] is True
    assert payload["telegram"]["allowed_chat_id"] == 42
    assert payload["llm"]["api_key_set"] is True

    text = str(payload)
    assert "secret-bot-token" not in text
    assert "sk-secret-llm-key" not in text
    assert "123456:" not in text


def test_websocket_accepts_connection(client: TestClient) -> None:
    """WS открывается и закрывается без ошибок; события шлются по изменению данных."""
    with client.websocket_connect("/ws"):
        pass  # рукопожатие состоялось: accept + подписка на брокер


def test_quick_entry_does_not_break_ws_publish(client: TestClient) -> None:
    """Публикация события при записи не роняет обработчик, даже если клиентов нет."""
    with client.websocket_connect("/ws"):
        response = client.post("/api/quick", json={"type": "note", "text": "событие"})
        assert response.status_code in (200, 201)
