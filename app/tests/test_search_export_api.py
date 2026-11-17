"""Тесты глобального поиска и экспорта (§12.6).

Поиск: находит задачи, проекты, книги, цитаты, цели, инбокс и заметки
проектов, регистронезависим (включая кириллицу), отдаёт крепкую форму
``kind/id/title/snippet/route``, не падает на пустом запросе и уважает лимиты.

Экспорт: JSON валиден и содержит только пользовательские сущности (без
``settings_kv``/``jobs`` и секретов), Markdown — непустая читаемая выгрузка;
оба ответа — файлы с ``Content-Disposition: attachment``.

База — во временном каталоге (``tmp_path``), рабочая ``data/`` не затрагивается.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.server.api.export import EXPORT_TABLES
from app.server.db import connect
from app.server.main import create_app
from app.server.settings import load_settings


@pytest.fixture()
def api(tmp_path: Path):
    """Приложение на временной базе: ``(TestClient, Settings)``."""
    settings = load_settings(env={}, root=tmp_path)
    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        yield client, settings


@pytest.fixture()
def client(api):
    return api[0]


@pytest.fixture()
def settings(api):
    return api[1]


# --------------------------------------------------------------------------
# Помощники
# --------------------------------------------------------------------------


def _exec(settings, sql: str, params: tuple = ()) -> int:
    conn = connect(settings.db_path)
    try:
        with conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def _seed_search_data(settings) -> dict:
    """Мини-база для поиска: по одной сущности каждого типа."""
    project = _exec(
        settings,
        "INSERT INTO projects (name, description, outcome) "
        "VALUES ('Ремонт квартиры', 'Покрасить стены и повесить полки', 'Уютная гостиная')",
    )
    _exec(
        settings,
        "INSERT INTO project_notes (project_id, kind, title, body, url) "
        "VALUES (?, 'note', 'Смета на ремонт', 'Плитка, краска, доставка', '')",
        (project,),
    )
    goal = _exec(
        settings,
        "INSERT INTO goals (title, why) VALUES ('Выучить английский', 'Нужен для работы')",
    )
    task = _exec(
        settings,
        "INSERT INTO tasks (title, notes, tags) "
        "VALUES ('Позвонить в клинику', 'Записаться к стоматологу', 'здоровье')",
    )
    _exec(
        settings,
        "INSERT INTO tasks (title, notes) VALUES ('Купить краску', 'Для ремонта')",
    )
    book = _exec(
        settings,
        "INSERT INTO books (title, author, why) "
        "VALUES ('Атомные привычки', 'Джеймс Клир', 'Выстроить систему')",
    )
    _exec(
        settings,
        "INSERT INTO book_quotes (book_id, text, page) "
        "VALUES (?, 'Мотивация переоценена', 12)",
        (book,),
    )
    _exec(
        settings,
        "INSERT INTO inbox_items (text) VALUES ('Записаться на диагностику машины')",
    )
    for title in ("Отчёт за неделю", "Отчёт по проекту", "Отчёт руководителю"):
        _exec(settings, "INSERT INTO tasks (title) VALUES (?)", (title,))
    return {"project": project, "goal": goal, "task": task, "book": book}


# --------------------------------------------------------------------------
# Поиск
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, expected_kind",
    [
        ("клинику", "task"),
        ("стоматологу", "task"),
        ("гостиная", "project"),
        ("атомные", "book"),
        ("мотивация", "quote"),
        ("английский", "goal"),
        ("диагностику", "inbox"),
        ("смета", "note"),
        ("плитка", "note"),
    ],
)
def test_search_finds_entities(
    client: TestClient, settings, query: str, expected_kind: str
) -> None:
    _seed_search_data(settings)
    payload = client.get("/api/search", params={"q": query}).json()
    assert payload["q"] == query
    assert payload["total"] >= 1
    kinds = [item["kind"] for item in payload["items"]]
    assert expected_kind in kinds, payload
    assert kinds[0] == expected_kind  # порядок §12.6: действия раньше справочных
    assert payload["kinds"].get(expected_kind, 0) >= 1


def test_search_item_shape_and_routes(client: TestClient, settings) -> None:
    seed = _seed_search_data(settings)

    tasks = client.get("/api/search", params={"q": "клинику"}).json()["items"]
    assert set(tasks[0]) == {"kind", "id", "title", "snippet", "route"}
    assert tasks[0]["id"] == seed["task"]
    assert tasks[0]["route"] == "/today?task=%d" % seed["task"]
    assert "стоматолог" in tasks[0]["snippet"]

    quote = client.get("/api/search", params={"q": "мотивация"}).json()["items"][0]
    assert quote["kind"] == "quote"
    assert quote["route"] == "/library?book=%d&quote=%d" % (seed["book"], quote["id"])

    project = client.get("/api/search", params={"q": "гостиная"}).json()["items"][0]
    assert project["kind"] == "project"
    assert project["route"] == "/projects?project=%d" % seed["project"]

    goal = client.get("/api/search", params={"q": "английский"}).json()["items"][0]
    assert goal["route"] == "/compass?goal=%d" % seed["goal"]

    note = client.get("/api/search", params={"q": "плитка"}).json()["items"][0]
    assert note["kind"] == "note"
    assert note["route"].startswith("/projects?project=%d&note=" % seed["project"])

    inbox = client.get("/api/search", params={"q": "диагностику"}).json()["items"][0]
    assert inbox["route"] == "/inbox?item=%d" % inbox["id"]

    book = client.get("/api/search", params={"q": "атомные"}).json()["items"][0]
    assert book["route"] == "/library?book=%d" % seed["book"]


def test_search_is_case_insensitive(client: TestClient, settings) -> None:
    _seed_search_data(settings)
    variants = ["клинику", "КЛИНИКУ", "Клинику", "клИнИкУ"]
    answers = [
        [item["id"] for item in client.get("/api/search", params={"q": q}).json()["items"]]
        for q in variants
    ]
    assert answers[0] == answers[1] == answers[2] == answers[3]
    assert answers[0]


def test_search_empty_query(client: TestClient, settings) -> None:
    _seed_search_data(settings)
    for params in ({}, {"q": ""}, {"q": "   "}):
        response = client.get("/api/search", params=params)
        assert response.status_code == 200
        payload = response.json()
        assert payload["items"] == []
        assert payload["total"] == 0
        assert payload["q"] == ""


def test_search_limit_and_total(client: TestClient, settings) -> None:
    _seed_search_data(settings)
    payload = client.get("/api/search", params={"q": "отчёт", "limit": 2}).json()
    assert payload["total"] == 3
    assert len(payload["items"]) == 2
    assert payload["limit"] == 2
    assert payload["kinds"] == {"task": 3}

    assert client.get("/api/search", params={"q": "отчёт", "limit": 0}).status_code == 422
    assert client.get("/api/search", params={"q": "отчёт", "limit": 51}).status_code == 422
    assert client.get("/api/search", params={"q": "ж" * 201}).status_code == 422


def test_search_no_matches(client: TestClient, settings) -> None:
    _seed_search_data(settings)
    payload = client.get("/api/search", params={"q": "нет такой сущности"}).json()
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["kinds"] == {}


def test_search_possible_delimiters(client: TestClient, settings) -> None:
    """SQL-опасные символы не ломают поиск (LIKE/%/кавычки)."""
    _seed_search_data(settings)
    for query in ("%", "'", '"', "100%", "клинику'"):
        response = client.get("/api/search", params={"q": query})
        assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# Экспорт
# --------------------------------------------------------------------------


def test_export_json(client: TestClient, settings) -> None:
    seed = _seed_search_data(settings)
    response = client.get("/api/export", params={"format": "json"})
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        'attachment; filename="life-os-export-%s.json"' % settings.today()
    )
    assert "application/json" in response.headers["content-type"]

    payload = json.loads(response.text)
    assert payload["app"]["schemaVersion"] == 3
    assert payload["app"]["name"] == "Life OS"
    assert payload["exportedAt"]
    assert payload["preferences"]["water_goal_ml"] == 2000

    data = payload["data"]
    assert set(data) == set(EXPORT_TABLES)
    for table in EXPORT_TABLES:
        assert payload["counts"][table] == len(data[table])
    assert payload["counts"]["tasks"] == 5
    assert payload["counts"]["books"] == 1
    assert [row["id"] for row in data["projects"]] == [seed["project"]]
    assert data["tasks"][0]["title"] == "Позвонить в клинику"

    # Служебные таблицы в выгрузку не попадают.
    assert "settings_kv" not in data
    assert "jobs" not in data


def test_export_default_format_is_json(client: TestClient) -> None:
    response = client.get("/api/export")
    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith('.json"')
    assert json.loads(response.text)["app"]["name"] == "Life OS"


def test_export_markdown(client: TestClient, settings) -> None:
    _seed_search_data(settings)
    response = client.get("/api/export", params={"format": "md"})
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        'attachment; filename="life-os-export-%s.md"' % settings.today()
    )
    assert response.headers["content-type"].startswith("text/markdown")
    body = response.text
    assert body.startswith("# Life OS — выгрузка")
    assert "Позвонить в клинику" in body
    assert "## Открытые задачи" in body
    assert "## Финансы" in body
    assert "## Библиотека" in body
    assert len(body) > 500


def test_export_markdown_empty_database(client: TestClient) -> None:
    response = client.get("/api/export", params={"format": "md"})
    assert response.status_code == 200
    body = response.text
    assert "# Life OS — выгрузка" in body
    assert "_нет данных_" in body
    assert len(body) > 200
    payload = json.loads(client.get("/api/export", params={"format": "json"}).text)
    assert payload["counts"]["tasks"] == 0
    assert all(rows == [] for rows in payload["data"].values())


def test_export_invalid_format(client: TestClient) -> None:
    assert client.get("/api/export", params={"format": "xml"}).status_code == 422


def test_export_contains_no_secrets(client: TestClient, settings) -> None:
    secret = "super-secret-token-42"
    _exec(
        settings,
        "INSERT INTO settings_kv (key, value) VALUES ('telegram_token', ?)",
        (secret,),
    )
    _exec(
        settings,
        "INSERT INTO jobs (kind, payload_json) VALUES ('noop', ?)",
        ('{"api_key": "%s"}' % secret,),
    )

    json_body = client.get("/api/export", params={"format": "json"}).text
    assert secret not in json_body
    assert "telegram_token" not in json_body

    md_body = client.get("/api/export", params={"format": "md"}).text
    assert secret not in md_body


def test_export_json_money_stays_in_kopecks(client: TestClient, settings) -> None:
    _exec(
        settings,
        "INSERT INTO transactions (ts, amount_kopecks, kind) "
        "VALUES ('2026-05-01T12:00:00+03:00', 125050, 'expense')",
    )
    payload = json.loads(client.get("/api/export", params={"format": "json"}).text)
    assert payload["data"]["transactions"][0]["amount_kopecks"] == 125050
