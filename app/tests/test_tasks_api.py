"""Тесты API «Дела»: задачи, повторения, инбокс и проекты (§12.2).

Проверяется HTTP-слой: CRUD задач и фильтры (день/неделя/канбан, сфера,
проект, ресурс, статус, список, поиск), счётчики ``must/important/low/done/
total``, завершение с повторениями ``daily`` / ``weekly:1,3,5`` /
``monthly:15`` (включая клампинг 31-го числа и сдвиг времени события),
перенос и отмена; инбокс с конвертацией в задачу/личное/проект/книгу/удаление;
проекты с шагами и заметками, 409 при удалении проекта с задачами.

База — во временном каталоге (``tmp_path``), рабочая ``data/`` не затрагивается.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.server.db import connect
from app.server.main import create_app
from app.server.settings import load_settings

DAY = "2026-05-06"  # среда
NEXT_DAY = "2026-05-07"
FRI = "2026-05-08"
MON = "2026-05-11"


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


def _count(settings, table: str, where: str = "", params: tuple = ()) -> int:
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT count(*) FROM %s %s" % (table, where), params).fetchone()
        return int(row[0]) if row is not None else -1
    finally:
        conn.close()


def _sphere(settings, name: str = "Здоровье") -> int:
    return _exec(settings, "INSERT INTO spheres (name) VALUES (?)", (name,))


def _goal(settings, title: str = "Выспаться") -> int:
    return _exec(settings, "INSERT INTO goals (title) VALUES (?)", (title,))


def _project(client: TestClient, name: str = "Ремонт", **fields) -> dict:
    payload = {"name": name}
    payload.update(fields)
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _task(client: TestClient, title: str = "Позвонить", **fields) -> dict:
    payload = {"title": title}
    payload.update(fields)
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# Задачи: создание и карточка
# --------------------------------------------------------------------------


def test_tasks_empty(client: TestClient) -> None:
    payload = client.get("/api/tasks").json()
    assert payload["items"] == []
    assert payload["counts"] == {"must": 0, "important": 0, "low": 0, "done": 0, "total": 0}
    assert payload["view"] == "day"
    for view in ("week", "kanban", "calendar"):
        assert client.get("/api/tasks", params={"view": view}).json()["items"] == []
    assert client.get("/api/inbox").json()["items"] == []
    assert client.get("/api/projects").json()["items"] == []


def test_create_task_all_fields(client: TestClient, settings) -> None:
    sphere = _sphere(settings)
    goal = _goal(settings)
    project = _project(client, "Ремонт")
    task = _task(
        client,
        "Покрасить стену",
        dueDate=DAY,
        startAt=DAY + "T10:00:00+03:00",
        endAt=DAY + "T12:00:00+03:00",
        resource="must",
        sphereId=sphere,
        projectId=project["id"],
        goalId=goal,
        listKind="personal",
        tags=["дом", "ремонт"],
        notes="Купить краску заранее",
        recurrence="weekly:1,3,5",
    )
    assert task["title"] == "Покрасить стену"
    assert task["sphereId"] == sphere
    assert task["sphereName"] == "Здоровье"
    assert task["projectId"] == project["id"]
    assert task["projectName"] == "Ремонт"
    assert task["goalId"] == goal
    assert task["listKind"] == "personal"
    assert task["tags"] == ["дом", "ремонт"]
    assert task["dueDate"] == DAY
    assert task["startAt"] == DAY + "T10:00:00+03:00"
    assert task["endAt"] == DAY + "T12:00:00+03:00"
    assert task["resource"] == "must"
    assert task["status"] == "todo"
    assert task["recurrence"] == "weekly:1,3,5"
    assert task["notes"] == "Купить краску заранее"
    assert task["doneAt"] is None
    assert task["created"]


def test_create_task_tags_string(client: TestClient) -> None:
    task = _task(client, "Купить продукты", tags="дом,  семья ")
    assert task["tags"] == ["дом", "семья"]
    assert _task(client, "Без тегов")["tags"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": ""},
        {"title": "   "},
        {"title": "X", "resource": "urgent"},
        {"title": "X", "listKind": "office"},
        {"title": "X", "dueDate": "06.05.2026"},
        {"title": "X", "startAt": "криво"},
        {"title": "X", "sphereId": 9999},
        {"title": "X", "projectId": 9999},
        {"title": "X", "goalId": 9999},
        {"title": "X", "sphereId": 0},
    ],
)
def test_create_task_validation(client: TestClient, payload: dict) -> None:
    assert client.post("/api/tasks", json=payload).status_code == 422


def test_extra_fields_ignored(client: TestClient) -> None:
    task = _task(client, "Задача", unknownField="игнор")
    assert task["title"] == "Задача"


# --------------------------------------------------------------------------
# Задачи: фильтры и счётчики
# --------------------------------------------------------------------------


def _seed_day_tasks(settings) -> dict:
    ids = {}
    ids["must"] = _exec(
        settings,
        "INSERT INTO tasks (title, due_date, resource, list_kind) VALUES (?, ?, 'must', 'work')",
        ("Жёсткое: врач", DAY),
    )
    ids["important"] = _exec(
        settings,
        "INSERT INTO tasks (title, due_date, resource, notes) VALUES (?, ?, 'important', ?)",
        ("Гибкое: отчёт", DAY, "Проверить цифры"),
    )
    ids["low"] = _exec(
        settings,
        "INSERT INTO tasks (title, due_date, resource, tags) VALUES (?, ?, 'low', 'дом')",
        ("Мелкое: полка", DAY),
    )
    ids["done"] = _exec(
        settings,
        "INSERT INTO tasks (title, due_date, resource, status, done_at) "
        "VALUES (?, ?, 'important', 'done', ?)",
        ("Закрыто: письмо", DAY, DAY + "T12:00:00+03:00"),
    )
    ids["cancelled"] = _exec(
        settings,
        "INSERT INTO tasks (title, due_date, status) VALUES (?, ?, 'cancelled')",
        ("Отменено", DAY),
    )
    ids["tomorrow"] = _exec(
        settings,
        "INSERT INTO tasks (title, due_date) VALUES ('Завтра', ?)",
        (NEXT_DAY,),
    )
    ids["event"] = _exec(
        settings,
        "INSERT INTO tasks (title, start_at, end_at, resource) "
        "VALUES ('Событие', ?, ?, 'must')",
        (DAY + "T15:00:00+03:00", DAY + "T16:00:00+03:00"),
    )
    return ids


def test_list_tasks_day_and_counts(client: TestClient, settings) -> None:
    _seed_day_tasks(settings)

    payload = client.get("/api/tasks", params={"date": DAY}).json()
    titles = {item["title"] for item in payload["items"]}
    assert titles == {"Жёсткое: врач", "Гибкое: отчёт", "Мелкое: полка", "Закрыто: письмо", "Событие"}
    # Считаются только этот день: завтрашняя задача и отменённая не в счётчиках.
    assert payload["counts"] == {"must": 2, "important": 1, "low": 1, "done": 1, "total": 5}

    # kanban — все задачи, включая завтрашнюю, но без отменённых.
    kanban = client.get("/api/tasks", params={"view": "kanban"}).json()
    assert len(kanban["items"]) == 6
    assert kanban["counts"]["total"] == 6

    # Явно запрошенные отменённые видны, но в счётчики не попадают.
    cancelled = client.get("/api/tasks", params={"date": DAY, "status": "cancelled"}).json()
    assert [item["title"] for item in cancelled["items"]] == ["Отменено"]
    assert cancelled["counts"]["total"] == 5


def test_list_tasks_filters(client: TestClient, settings) -> None:
    sphere = _sphere(settings)
    project = _project(client, "Работа")
    first = _task(client, "Задача в сфере", dueDate=DAY, sphereId=sphere)
    second = _task(client, "Задача в проекте", dueDate=DAY, projectId=project["id"])
    _task(client, "Личное дело", dueDate=DAY, listKind="personal")

    by_sphere = client.get("/api/tasks", params={"date": DAY, "sphereId": sphere}).json()
    assert [item["id"] for item in by_sphere["items"]] == [first["id"]]

    by_project = client.get(
        "/api/tasks", params={"date": DAY, "projectId": project["id"]}
    ).json()
    assert [item["id"] for item in by_project["items"]] == [second["id"]]

    personal = client.get(
        "/api/tasks", params={"date": DAY, "listKind": "personal"}
    ).json()
    assert [item["title"] for item in personal["items"]] == ["Личное дело"]

    # resource/status сужают items, но не counts.
    must_only = client.get("/api/tasks", params={"date": DAY, "resource": "must"}).json()
    assert all(item["resource"] == "must" for item in must_only["items"])
    assert must_only["counts"]["important"] == 3

    done_only = client.get("/api/tasks", params={"date": DAY, "status": "done"}).json()
    assert all(item["status"] == "done" for item in done_only["items"])
    assert done_only["counts"]["total"] == 3

    # Поиск без учёта регистра (кириллица).
    found = client.get("/api/tasks", params={"date": DAY, "q": "СФЕРЕ"}).json()
    assert [item["id"] for item in found["items"]] == [first["id"]]
    by_notes = client.get("/api/tasks", params={"date": DAY, "q": "цифры"}).json()
    assert by_notes["items"] == []
    assert client.get("/api/tasks", params={"date": DAY, "view": "nope"}).status_code == 422
    assert client.get("/api/tasks", params={"date": "06.05.2026"}).status_code == 422
    assert (
        client.get("/api/tasks", params={"from": "2026-05-10", "to": "2026-05-01"}).status_code
        == 422
    )


def test_list_tasks_week_and_calendar(client: TestClient, settings) -> None:
    _task(client, "Внутри недели", dueDate=DAY)
    _task(client, "Событие внутри", startAt=DAY + "T09:00:00+03:00")
    _task(client, "Далеко", dueDate="2026-06-01")

    week = client.get(
        "/api/tasks", params={"view": "week", "from": "2026-05-04", "to": "2026-05-10"}
    ).json()
    assert {item["title"] for item in week["items"]} == {"Внутри недели", "Событие внутри"}
    assert week["from"] == "2026-05-04"
    assert week["to"] == "2026-05-10"

    calendar = client.get(
        "/api/tasks", params={"view": "calendar", "from": "2026-06-01", "to": "2026-06-30"}
    ).json()
    assert [item["title"] for item in calendar["items"]] == ["Далеко"]


# --------------------------------------------------------------------------
# Задачи: правка, перенос, отмена
# --------------------------------------------------------------------------


def test_patch_task(client: TestClient, settings) -> None:
    sphere = _sphere(settings)
    task = _task(client, "Черновик", dueDate=DAY)

    patched = client.patch(
        "/api/tasks/%d" % task["id"],
        json={
            "title": "  Готово ",
            "resource": "low",
            "listKind": "personal",
            "dueDate": NEXT_DAY,
            "startAt": NEXT_DAY + "T08:00:00+03:00",
            "endAt": NEXT_DAY + "T09:00:00+03:00",
            "tags": ["дом"],
            "notes": "Обновлено",
            "recurrence": "daily",
            "sphereId": sphere,
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["title"] == "Готово"
    assert body["resource"] == "low"
    assert body["listKind"] == "personal"
    assert body["dueDate"] == NEXT_DAY
    assert body["startAt"] == NEXT_DAY + "T08:00:00+03:00"
    assert body["tags"] == ["дом"]
    assert body["recurrence"] == "daily"
    assert body["sphereName"] == "Здоровье"

    # null очищает даты, ссылки и теги.
    cleared = client.patch(
        "/api/tasks/%d" % task["id"],
        json={"dueDate": None, "startAt": None, "endAt": None, "sphereId": None, "tags": None},
    ).json()
    assert cleared["dueDate"] is None
    assert cleared["startAt"] is None
    assert cleared["sphereId"] is None
    assert cleared["tags"] == []

    # status done ставит doneAt, возврат в todo — снимает.
    done = client.patch("/api/tasks/%d" % task["id"], json={"status": "done"}).json()
    assert done["status"] == "done"
    assert done["doneAt"]
    reopened = client.patch("/api/tasks/%d" % task["id"], json={"status": "todo"}).json()
    assert reopened["status"] == "todo"
    assert reopened["doneAt"] is None

    assert client.patch("/api/tasks/%d" % task["id"], json={}).status_code == 400
    assert client.patch("/api/tasks/9999", json={"title": "X"}).status_code == 404
    assert (
        client.patch("/api/tasks/%d" % task["id"], json={"title": None}).status_code == 422
    )
    assert (
        client.patch("/api/tasks/%d" % task["id"], json={"sphereId": 9999}).status_code == 422
    )
    assert (
        client.patch("/api/tasks/%d" % task["id"], json={"dueDate": "криво"}).status_code == 422
    )


def test_reschedule_task(client: TestClient) -> None:
    task = _task(client, "Перенос", dueDate=DAY)
    moved = client.post(
        "/api/tasks/%d/reschedule" % task["id"], json={"dueDate": NEXT_DAY}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["dueDate"] == NEXT_DAY
    assert client.post("/api/tasks/%d/reschedule" % task["id"], json={}).status_code == 422
    assert (
        client.post(
            "/api/tasks/%d/reschedule" % task["id"], json={"dueDate": "07.05.2026"}
        ).status_code
        == 422
    )
    assert client.post("/api/tasks/9999/reschedule", json={"dueDate": DAY}).status_code == 404


def test_delete_task_cancels(client: TestClient) -> None:
    task = _task(client, "Удаляемая", dueDate=DAY)
    deleted = client.delete("/api/tasks/%d" % task["id"])
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "cancelled"
    assert client.get("/api/tasks", params={"date": DAY}).json()["items"] == []
    assert client.get("/api/tasks", params={"date": DAY, "status": "cancelled"}).json()["items"]
    assert client.delete("/api/tasks/9999").status_code == 404


# --------------------------------------------------------------------------
# Задачи: повторения
# --------------------------------------------------------------------------


def test_complete_daily_creates_next(client: TestClient, settings) -> None:
    task = _task(client, "Зарядка", dueDate=DAY, recurrence="daily")
    response = client.post("/api/tasks/%d/complete" % task["id"], json={"done": True})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task"]["status"] == "done"
    assert payload["task"]["doneAt"]
    assert payload["next"] is not None
    assert payload["next"]["dueDate"] == NEXT_DAY
    assert payload["next"]["status"] == "todo"
    assert payload["next"]["recurrence"] == "daily"
    assert payload["next"]["id"] != task["id"]
    assert _count(settings, "tasks") == 2

    # Повторное закрытие не создаёт третью задачу.
    again = client.post("/api/tasks/%d/complete" % task["id"], json={"done": True}).json()
    assert again["next"] is None
    assert _count(settings, "tasks") == 2


def test_complete_weekly_creates_next(client: TestClient) -> None:
    task = _task(client, "Тренировка", dueDate=DAY, recurrence="weekly:1,3,5")
    # Среда → следующая пятница.
    payload = client.post("/api/tasks/%d/complete" % task["id"], json={"done": True}).json()
    assert payload["next"]["dueDate"] == FRI

    # Пятница → понедельник (через выходные).
    second = client.post(
        "/api/tasks/%d/complete" % payload["next"]["id"], json={"done": True}
    ).json()
    assert second["next"]["dueDate"] == MON


def test_complete_monthly_clamps_day(client: TestClient) -> None:
    task = client.post(
        "/api/tasks",
        json={"title": "Оплатить", "dueDate": "2026-01-31", "recurrence": "monthly:31"},
    ).json()
    payload = client.post("/api/tasks/%d/complete" % task["id"], json={"done": True}).json()
    assert payload["next"]["dueDate"] == "2026-02-28"


def test_complete_invalid_recurrence_does_not_crash(client: TestClient, settings) -> None:
    for index, rule in enumerate(("yearly", "weekly:", "weekly:9", "monthly:32", "monthly:x", "daily:2")):
        task = _task(client, "Странное правило %d" % index, dueDate=DAY, recurrence=rule)
        response = client.post("/api/tasks/%d/complete" % task["id"], json={"done": True})
        assert response.status_code == 200, (rule, response.text)
        assert response.json()["task"]["status"] == "done"
        assert response.json()["next"] is None
    assert _count(settings, "tasks") == 6


def test_complete_undo_and_event_shift(client: TestClient) -> None:
    task = _task(
        client,
        "Событие",
        dueDate=DAY,
        startAt=DAY + "T10:00:00+03:00",
        endAt=DAY + "T11:00:00+03:00",
        recurrence="daily",
    )
    payload = client.post("/api/tasks/%d/complete" % task["id"], json={"done": True}).json()
    assert payload["next"]["startAt"] == NEXT_DAY + "T10:00:00+03:00"
    assert payload["next"]["endAt"] == NEXT_DAY + "T11:00:00+03:00"

    undone = client.post("/api/tasks/%d/complete" % task["id"], json={"done": False}).json()
    assert undone["task"]["status"] == "todo"
    assert undone["task"]["doneAt"] is None
    assert undone["next"] is None

    assert client.post("/api/tasks/9999/complete", json={"done": True}).status_code == 404


def test_complete_uses_start_date_without_due(client: TestClient) -> None:
    task = _task(client, "Вебинар", startAt=DAY + "T19:00:00+03:00", recurrence="daily")
    payload = client.post("/api/tasks/%d/complete" % task["id"], json={"done": True}).json()
    assert payload["next"]["dueDate"] == NEXT_DAY


# --------------------------------------------------------------------------
# Инбокс
# --------------------------------------------------------------------------


def test_inbox_crud(client: TestClient) -> None:
    empty = client.get("/api/inbox").json()
    assert empty["items"] == []
    assert empty["counts"] == {"new": 0, "processed": 0, "total": 0}

    created = client.post("/api/inbox", json={"text": "  Идея: разобрать папку "})
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["text"] == "Идея: разобрать папку"
    assert item["status"] == "new"
    assert item["convertedType"] == ""
    assert item["convertedId"] is None

    listed = client.get("/api/inbox").json()
    assert [entry["id"] for entry in listed["items"]] == [item["id"]]
    assert listed["counts"] == {"new": 1, "processed": 0, "total": 1}
    assert client.get("/api/inbox", params={"status": "processed"}).json()["items"] == []
    assert client.get("/api/inbox", params={"status": "all"}).json()["counts"]["total"] == 1
    assert client.get("/api/inbox", params={"status": "bad"}).status_code == 422

    assert client.post("/api/inbox", json={"text": ""}).status_code == 422
    assert client.delete("/api/inbox/%d" % item["id"]).status_code == 200
    assert client.delete("/api/inbox/%d" % item["id"]).status_code == 404


def test_inbox_convert_to_task(client: TestClient) -> None:
    item = client.post("/api/inbox", json={"text": "Позвонить в клинику"}).json()
    response = client.post(
        "/api/inbox/%d/convert" % item["id"],
        json={"to": "task", "payload": {"resource": "must", "dueDate": DAY}},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["item"]["status"] == "processed"
    assert payload["item"]["convertedType"] == "task"
    assert payload["item"]["convertedId"] == payload["task"]["id"]
    assert payload["task"]["title"] == "Позвонить в клинику"
    assert payload["task"]["resource"] == "must"
    assert payload["task"]["dueDate"] == DAY
    assert payload["task"]["listKind"] == "work"

    # Повторная конвертация — 409.
    again = client.post("/api/inbox/%d/convert" % item["id"], json={"to": "task"})
    assert again.status_code == 409
    assert client.get("/api/inbox", params={"status": "processed"}).json()["counts"]["processed"] == 1


def test_inbox_convert_to_personal(client: TestClient) -> None:
    item = client.post("/api/inbox", json={"text": "Купить лампу"}).json()
    payload = client.post(
        "/api/inbox/%d/convert" % item["id"],
        json={"to": "personal", "payload": {"tags": "дом"}},
    ).json()
    assert payload["task"]["listKind"] == "personal"
    assert payload["task"]["tags"] == ["дом"]
    assert payload["item"]["convertedType"] == "task"


def test_inbox_convert_to_project(client: TestClient) -> None:
    item = client.post("/api/inbox", json={"text": "Собрать отчёт за квартал"}).json()
    payload = client.post(
        "/api/inbox/%d/convert" % item["id"],
        json={"to": "project", "payload": {"outcome": "Отчёт сдан", "status": "active"}},
    ).json()
    assert payload["project"]["name"] == "Собрать отчёт за квартал"
    assert payload["project"]["outcome"] == "Отчёт сдан"
    assert payload["project"]["status"] == "active"
    assert payload["item"]["convertedType"] == "project"
    assert payload["item"]["convertedId"] == payload["project"]["id"]


def test_inbox_convert_to_book(client: TestClient, settings) -> None:
    item = client.post("/api/inbox", json={"text": "Атомные привычки"}).json()
    payload = client.post(
        "/api/inbox/%d/convert" % item["id"],
        json={
            "to": "book",
            "payload": {
                "author": "Джеймс Клир",
                "category": "nonfiction",
                "pagesTotal": 320,
                "shelf": "done",
            },
        },
    ).json()
    book = payload["book"]
    assert book["title"] == "Атомные привычки"
    assert book["author"] == "Джеймс Клир"
    assert book["pagesTotal"] == 320
    assert book["pagesRead"] == 320
    assert book["finishedAt"] == settings.today()
    assert payload["item"]["convertedType"] == "book"
    assert _count(settings, "books") == 1


def test_inbox_convert_delete(client: TestClient, settings) -> None:
    item = client.post("/api/inbox", json={"text": "Мусор"})
    item_id = item.json()["id"]
    response = client.post("/api/inbox/%d/convert" % item_id, json={"to": "delete"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": item_id}
    assert _count(settings, "inbox_items") == 0
    assert client.post("/api/inbox/%d/convert" % item_id, json={"to": "task"}).status_code == 404


def test_inbox_convert_validation(client: TestClient) -> None:
    item = client.post("/api/inbox", json={"text": "Что-то"}).json()
    assert (
        client.post("/api/inbox/%d/convert" % item["id"], json={"to": "note"}).status_code == 422
    )
    assert client.post("/api/inbox/9999/convert", json={"to": "task"}).status_code == 404
    bad = client.post(
        "/api/inbox/%d/convert" % item["id"],
        json={"to": "book", "payload": {"category": "poetry"}},
    )
    assert bad.status_code == 422


# --------------------------------------------------------------------------
# Проекты
# --------------------------------------------------------------------------


def test_project_crud(client: TestClient, settings) -> None:
    sphere = _sphere(settings)
    goal = _goal(settings)
    project = _project(
        client,
        "Ремонт квартиры",
        sphereId=sphere,
        goalId=goal,
        deadline="2026-12-31",
        status="active",
        outcome="Уютная гостиная",
        description="Покрасить и повесить полки",
    )
    assert project["sphereId"] == sphere
    assert project["sphereName"] == "Здоровье"
    assert project["goalId"] == goal
    assert project["status"] == "active"
    assert project["deadline"] == "2026-12-31"
    assert project["stepsTotal"] == 0
    assert project["tasksTotal"] == 0

    listed = client.get("/api/projects").json()
    assert [item["id"] for item in listed["items"]] == [project["id"]]
    assert listed["counts"] == {"idea": 0, "active": 1, "paused": 0, "done": 0, "total": 1}

    detail = client.get("/api/projects/%d" % project["id"]).json()
    assert detail["project"]["id"] == project["id"]
    assert detail["steps"] == []
    assert detail["notes"] == []
    assert detail["tasks"] == {
        "items": [],
        "counts": {"must": 0, "important": 0, "low": 0, "done": 0, "total": 0},
    }

    patched = client.patch(
        "/api/projects/%d" % project["id"],
        json={"name": "Ремонт", "status": "paused", "deadline": None, "sphereId": None},
    ).json()
    assert patched["name"] == "Ремонт"
    assert patched["status"] == "paused"
    assert patched["deadline"] is None
    assert patched["sphereId"] is None

    assert client.delete("/api/projects/%d" % project["id"]).status_code == 200
    assert client.get("/api/projects/%d" % project["id"]).status_code == 404
    assert client.delete("/api/projects/%d" % project["id"]).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "X", "status": "draft"},
        {"name": "X", "deadline": "31.12.2026"},
        {"name": "X", "sphereId": 9999},
        {"name": "X", "goalId": 9999},
    ],
)
def test_project_validation(client: TestClient, payload: dict) -> None:
    assert client.post("/api/projects", json=payload).status_code == 422


def test_project_patch_validation(client: TestClient) -> None:
    project = _project(client)
    assert client.patch("/api/projects/%d" % project["id"], json={}).status_code == 400
    assert (
        client.patch("/api/projects/%d" % project["id"], json={"name": None}).status_code == 422
    )
    assert (
        client.patch("/api/projects/%d" % project["id"], json={"status": "draft"}).status_code
        == 422
    )
    assert client.patch("/api/projects/9999", json={"name": "X"}).status_code == 404


def test_project_steps(client: TestClient) -> None:
    project = _project(client, "Ремонт")
    first = client.post("/api/projects/%d/steps" % project["id"], json={"title": "Стены"})
    assert first.status_code == 201, first.text
    assert first.json()["sort"] == 1
    assert first.json()["done"] is False
    second = client.post("/api/projects/%d/steps" % project["id"], json={"title": "Полки"}).json()
    assert second["sort"] == 2

    detail = client.get("/api/projects/%d" % project["id"]).json()
    assert [step["title"] for step in detail["steps"]] == ["Стены", "Полки"]
    assert detail["project"]["stepsTotal"] == 2
    assert detail["project"]["stepsDone"] == 0

    toggled = client.patch(
        "/api/project-steps/%d" % first.json()["id"], json={"done": True, "title": "Стены и потолок"}
    ).json()
    assert toggled["done"] is True
    assert toggled["title"] == "Стены и потолок"
    assert client.get("/api/projects/%d" % project["id"]).json()["project"]["stepsDone"] == 1

    assert client.patch("/api/project-steps/%d" % first.json()["id"], json={}).status_code == 400
    assert (
        client.patch("/api/project-steps/%d" % first.json()["id"], json={"done": None}).status_code
        == 422
    )
    assert client.patch("/api/project-steps/9999", json={"done": True}).status_code == 404

    assert client.delete("/api/project-steps/%d" % second["id"]).status_code == 200
    assert client.delete("/api/project-steps/%d" % second["id"]).status_code == 404
    assert client.post("/api/projects/9999/steps", json={"title": "X"}).status_code == 404


def test_project_notes(client: TestClient, settings) -> None:
    project = _project(client, "Ремонт")
    note = client.post(
        "/api/projects/%d/notes" % project["id"],
        json={"kind": "note", "title": "Смета", "body": "Плитка и краска"},
    )
    assert note.status_code == 201, note.text
    assert note.json()["kind"] == "note"
    assert note.json()["title"] == "Смета"
    assert note.json()["body"] == "Плитка и краска"

    link = client.post(
        "/api/projects/%d/notes" % project["id"],
        json={"kind": "link", "title": "Магазин", "url": "https://example.com"},
    ).json()
    assert link["kind"] == "link"
    assert link["url"] == "https://example.com"

    detail = client.get("/api/projects/%d" % project["id"]).json()
    assert {entry["kind"] for entry in detail["notes"]} == {"note", "link"}

    assert (
        client.post(
            "/api/projects/%d/notes" % project["id"], json={"kind": "link", "title": "Без ссылки"}
        ).status_code
        == 422
    )
    assert (
        client.post("/api/projects/%d/notes" % project["id"], json={"kind": "note"}).status_code
        == 422
    )
    assert client.post("/api/projects/9999/notes", json={"title": "X"}).status_code == 404

    assert client.delete("/api/project-notes/%d" % note.json()["id"]).status_code == 200
    assert client.delete("/api/project-notes/%d" % note.json()["id"]).status_code == 404


def test_project_tasks_and_delete_guard(client: TestClient) -> None:
    project = _project(client, "Запуск")
    task = _task(client, "Согласовать смету", dueDate=DAY, projectId=project["id"])

    detail = client.get("/api/projects/%d" % project["id"]).json()
    assert [item["id"] for item in detail["tasks"]["items"]] == [task["id"]]
    assert detail["tasks"]["counts"]["total"] == 1
    assert detail["project"]["tasksTotal"] == 1
    assert detail["project"]["tasksDone"] == 0

    blocked = client.delete("/api/projects/%d" % project["id"])
    assert blocked.status_code == 409
    assert "задачи" in blocked.json()["detail"].lower()

    # После отмены единственной задачи проект удаляется (FK снимет ссылку).
    client.delete("/api/tasks/%d" % task["id"])
    assert client.delete("/api/projects/%d" % project["id"]).status_code == 200


def test_project_filters(client: TestClient, settings) -> None:
    sphere = _sphere(settings)
    _project(client, "Ремонт", sphereId=sphere, status="active")
    _project(client, "Отпуск", status="idea")
    _project(client, "Английский", status="done")

    active = client.get("/api/projects", params={"status": "active"}).json()
    assert [item["name"] for item in active["items"]] == ["Ремонт"]
    assert active["counts"] == {"idea": 1, "active": 1, "paused": 0, "done": 1, "total": 3}

    by_sphere = client.get("/api/projects", params={"sphereId": sphere}).json()
    assert [item["name"] for item in by_sphere["items"]] == ["Ремонт"]

    found = client.get("/api/projects", params={"q": "ОТПУСК"}).json()
    assert [item["name"] for item in found["items"]] == ["Отпуск"]
    assert client.get("/api/projects", params={"q": "нет такого"}).json()["items"] == []


# --------------------------------------------------------------------------
# Регистрация маршрутов
# --------------------------------------------------------------------------


def test_tasks_routes_registered(client: TestClient) -> None:
    paths = set(client.get("/openapi.json").json()["paths"])
    expected = {
        "/api/tasks",
        "/api/tasks/{task_id}",
        "/api/tasks/{task_id}/complete",
        "/api/tasks/{task_id}/reschedule",
        "/api/inbox",
        "/api/inbox/{item_id}",
        "/api/inbox/{item_id}/convert",
        "/api/projects",
        "/api/projects/{project_id}",
        "/api/projects/{project_id}/steps",
        "/api/projects/{project_id}/notes",
        "/api/project-steps/{step_id}",
        "/api/project-notes/{note_id}",
    }
    missing = expected - paths
    assert not missing, "не зарегистрированы: %s" % sorted(missing)
