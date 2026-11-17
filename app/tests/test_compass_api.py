"""Тесты API Компаса (``app/server/api/compass.py``).

Проверяется HTTP-слой целиком: коды ответов (201/400/404/409/422), camelCase-
контракт §12.1, прогресс цели по вехам/задачам/вручную, архив используемой
сферы, каскад вех при удалении цели и совместимость со старым разделом
``/api/sections/goals`` (``PATCH {"current": ...}`` продолжает работать).

База — во временном каталоге (``tmp_path``), рабочая ``data/`` не затрагивается.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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


def _sql(settings, query: str, params: tuple = ()):
    """Одна строка из БД (проверка того, что реально записалось)."""
    conn = connect(settings.db_path)
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def _exec(settings, query: str, params: tuple = ()) -> int:
    """``INSERT``/``UPDATE`` в обход API — для подготовки связанных данных."""
    conn = connect(settings.db_path)
    try:
        with conn:
            cursor = conn.execute(query, params)
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def _create_sphere(client: TestClient, name: str = "Работа и проекты", **fields) -> dict:
    payload: dict = {"name": name}
    payload.update(fields)
    response = client.post("/api/spheres", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_goal(client: TestClient, title: str = "Выйти на три проекта", **fields) -> dict:
    payload: dict = {"title": title}
    payload.update(fields)
    response = client.post("/api/goals", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_step(client: TestClient, goal_id: int, title: str = "Веха") -> dict:
    response = client.post("/api/goals/%d/steps" % goal_id, json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def _create_task(settings, goal_id: int, status: str = "todo") -> int:
    return _exec(
        settings,
        "INSERT INTO tasks (title, goal_id, status, done_at) VALUES (?, ?, ?, ?)",
        ("Задача", goal_id, status, "2026-11-15T10:00:00Z" if status == "done" else None),
    )


# --------------------------------------------------------------------------
# Компас: пустая база и опорная дата
# --------------------------------------------------------------------------


def test_compass_empty_database(client: TestClient, settings) -> None:
    """Пустая база отдаёт 200 и пустые списки, а не 404/500."""
    response = client.get("/api/compass")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["date"] == settings.today()
    assert payload["spheres"] == []
    assert payload["goals"] == []
    assert payload["antiGoals"] == {"month": [], "quarter": []}


def test_compass_date_parameter(client: TestClient) -> None:
    """``?date=`` задаёт опорный день; мусор в дате — 422."""
    payload = client.get("/api/compass", params={"date": "2026-11-15"}).json()
    assert payload["date"] == "2026-11-15"
    assert client.get("/api/compass", params={"date": "2026-13-01"}).status_code == 422
    assert client.get("/api/compass", params={"date": "15.11.2026"}).status_code == 422


# --------------------------------------------------------------------------
# Сферы
# --------------------------------------------------------------------------


def test_sphere_create_and_list(client: TestClient) -> None:
    """Создание сферы: 201, camelCase, автоматический порядок сортировки."""
    first = _create_sphere(client, "Работа и проекты", color="#1476FF", icon="briefcase")
    assert set(first) == {"id", "name", "color", "icon", "sort", "archived"}
    assert first["archived"] is False
    second = _create_sphere(client, "Здоровье и ресурс")
    assert second["sort"] == first["sort"] + 1
    assert second["color"] == "#A18BB7"

    items = client.get("/api/spheres").json()["items"]
    assert [item["name"] for item in items] == ["Работа и проекты", "Здоровье и ресурс"]
    assert items[0]["icon"] == "briefcase"


def test_sphere_duplicate_name_is_409(client: TestClient) -> None:
    """Имя сферы уникально без учёта регистра и пробелов по краям."""
    _create_sphere(client, "Обучение и развитие")
    response = client.post("/api/spheres", json={"name": "  обучение и развитие "})
    assert response.status_code == 409
    assert "detail" in response.json()


@pytest.mark.parametrize(
    "payload",
    [{"name": ""}, {"name": "   "}, {"name": "x" * 121}],
)
def test_sphere_validation_422(client: TestClient, payload: dict) -> None:
    assert client.post("/api/spheres", json=payload).status_code == 422


def test_sphere_patch_and_archive_filter(client: TestClient) -> None:
    """PATCH правит поля и архивирует сферу; фильтр ``archived`` их разделяет."""
    sphere = _create_sphere(client, "Финансы и быт")
    response = client.patch(
        "/api/spheres/%d" % sphere["id"],
        json={"name": "Финансы", "color": "#22AA66", "icon": "wallet", "sort": 7, "archived": True},
    )
    assert response.status_code == 200, response.text
    patched = response.json()
    assert patched["name"] == "Финансы"
    assert patched["color"] == "#22AA66"
    assert patched["sort"] == 7
    assert patched["archived"] is True

    archived = client.get("/api/spheres", params={"archived": "archived"}).json()["items"]
    assert [item["id"] for item in archived] == [sphere["id"]]
    assert client.get("/api/spheres", params={"archived": "active"}).json()["items"] == []

    returned = client.patch("/api/spheres/%d" % sphere["id"], json={"archived": False})
    assert returned.json()["archived"] is False


def test_sphere_patch_errors(client: TestClient) -> None:
    """404 — нет сферы, 400 — пустое тело, 422 — ``null`` вместо значения."""
    sphere = _create_sphere(client, "Личное и семья")
    assert client.patch("/api/spheres/999", json={"name": "Нет"}).status_code == 404
    assert client.patch("/api/spheres/%d" % sphere["id"], json={}).status_code == 400
    assert client.patch("/api/spheres/%d" % sphere["id"], json={"name": None}).status_code == 422
    assert client.patch("/api/spheres/%d" % sphere["id"], json={"name": ""}).status_code == 422


def test_sphere_delete_unused_removes_row(client: TestClient, settings) -> None:
    """Свободная сфера удаляется физически."""
    sphere = _create_sphere(client, "Временная")
    response = client.delete("/api/spheres/%d" % sphere["id"])
    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": sphere["id"],
        "deleted": True,
        "archived": False,
        "usage": {},
    }
    assert _sql(settings, "SELECT 1 FROM spheres WHERE id = ?", (sphere["id"],)) is None
    assert client.delete("/api/spheres/%d" % sphere["id"]).status_code == 404


def test_sphere_delete_used_archives(client: TestClient, settings) -> None:
    """Используемая сфера уходит в архив: ссылки цели остаются живыми."""
    sphere = _create_sphere(client, "Здоровье")
    goal = _create_goal(client, sphereId=sphere["id"])
    response = client.delete("/api/spheres/%d" % sphere["id"])
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["archived"] is True
    assert payload["deleted"] is False
    assert payload["usage"] == {"goals": 1}
    row = _sql(settings, "SELECT archived FROM spheres WHERE id = ?", (sphere["id"],))
    assert int(row[0]) == 1

    compass = client.get("/api/compass").json()
    assert compass["spheres"] == []
    assert [item["id"] for item in compass["goals"]] == [goal["id"]]
    assert compass["goals"][0]["sphere"]["name"] == "Здоровье"

    listed = client.get("/api/spheres").json()["items"]
    assert listed[0]["archived"] is True


# --------------------------------------------------------------------------
# Цели
# --------------------------------------------------------------------------


def test_goal_create_defaults(client: TestClient, settings) -> None:
    """Минимум полей: горизонт «год», статус active, прогресс 0."""
    goal = _create_goal(client, "Собрать портфолио")
    assert goal["horizon"] == "year"
    assert goal["status"] == "active"
    assert goal["progress"] == 0
    assert goal["progressPct"] == 0
    assert goal["sphereId"] is None
    assert goal["sphere"] is None
    assert goal["parentId"] is None
    assert goal["steps"] == []
    assert goal["counts"] == {
        "stepsDone": 0,
        "stepsTotal": 0,
        "tasksDone": 0,
        "tasksTotal": 0,
    }
    assert goal["why"] == ""
    assert goal["deadline"] is None
    assert goal["unit"] == ""
    assert goal["created"]
    row = _sql(settings, "SELECT horizon, status FROM goals WHERE id = ?", (goal["id"],))
    assert tuple(row) == ("year", "active")


def test_goal_create_full(client: TestClient, settings) -> None:
    """Все поля контракта сохраняются, дата нормализуется до ``YYYY-MM-DD``."""
    sphere = _create_sphere(client, "Обучение и развитие")
    goal = _create_goal(
        client,
        title="Сдать IELTS на 7.0",
        horizon="quarter",
        sphereId=sphere["id"],
        why="Учёба за рубежом",
        deadline="2026-12-31",
        target=7,
        unit="балл",
    )
    assert goal["horizon"] == "quarter"
    assert goal["sphere"] == {
        "id": sphere["id"],
        "name": "Обучение и развитие",
        "color": "#A18BB7",
    }
    assert goal["deadline"] == "2026-12-31"
    assert goal["target"] == 7.0
    assert goal["unit"] == "балл"
    assert goal["why"] == "Учёба за рубежом"
    row = _sql(
        settings,
        "SELECT horizon, sphere_id, why, target FROM goals WHERE id = ?",
        (goal["id"],),
    )
    assert tuple(row) == ("quarter", sphere["id"], "Учёба за рубежом", 7.0)


@pytest.mark.parametrize(
    "payload",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "Цель", "horizon": "week"},
        {"title": "Цель", "deadline": "31.12.2026"},
        {"title": "Цель", "status": "archived"},
        {"title": "Цель", "progress": 101},
        {"title": "Цель", "target": -1},
    ],
)
def test_goal_create_validation_422(client: TestClient, payload: dict) -> None:
    assert client.post("/api/goals", json=payload).status_code == 422


def test_goal_create_unknown_references(client: TestClient) -> None:
    """Ссылка на несуществующую сферу/родителя — 422 (это тело, не URL)."""
    assert client.post("/api/goals", json={"title": "Цель", "sphereId": 999}).status_code == 422
    assert client.post("/api/goals", json={"title": "Цель", "parentId": 999}).status_code == 422


def test_goal_patch_fields(client: TestClient, settings) -> None:
    """PATCH меняет любые поля, включая горизонт, статус и ручной прогресс."""
    goal = _create_goal(client, "Цель года")
    sphere = _create_sphere(client, "Работа")
    response = client.patch(
        "/api/goals/%d" % goal["id"],
        json={
            "title": "Цель года v2",
            "horizon": "month",
            "sphereId": sphere["id"],
            "why": "Проверка",
            "deadline": "2026-11-30T00:00:00Z",
            "status": "done",
            "progress": 40,
            "target": 100,
            "unit": "%",
        },
    )
    assert response.status_code == 200, response.text
    patched = response.json()
    assert patched["title"] == "Цель года v2"
    assert patched["horizon"] == "month"
    assert patched["sphere"]["id"] == sphere["id"]
    assert patched["deadline"] == "2026-11-30"
    assert patched["status"] == "done"
    assert patched["progress"] == 40
    assert patched["progressPct"] == 40
    row = _sql(settings, "SELECT title, horizon, status, progress FROM goals WHERE id = ?", (goal["id"],))
    assert tuple(row) == ("Цель года v2", "month", "done", 40)


def test_goal_patch_legacy_current_keeps_sections(client: TestClient) -> None:
    """Старый экран ``/api/sections/goals`` продолжает управлять целью."""
    goal = _create_goal(client, "Вес", target=80, unit="кг")
    response = client.patch("/api/goals/%d" % goal["id"], json={"current": 80})
    assert response.status_code == 200, response.text
    assert response.json()["current"] == 80
    section = client.get("/api/sections/goals").json()
    assert section["goals"][0]["percent"] == 100


def test_goal_patch_errors(client: TestClient) -> None:
    """404 — нет цели, 400 — пустое тело, 422 — ``null``/плохие значения."""
    goal = _create_goal(client, "Цель")
    url = "/api/goals/%d" % goal["id"]
    assert client.patch("/api/goals/999", json={"title": "Нет"}).status_code == 404
    assert client.patch(url, json={}).status_code == 400
    assert client.patch(url, json={"title": None}).status_code == 422
    assert client.patch(url, json={"horizon": "day"}).status_code == 422
    assert client.patch(url, json={"status": "archived"}).status_code == 422
    assert client.patch(url, json={"progress": -1}).status_code == 422
    assert client.patch(url, json={"progress": 101}).status_code == 422


def test_goal_hierarchy_and_cycle_guard(client: TestClient) -> None:
    """Родитель-цель связывается, цикл и ссылка на себя — 422, ``null`` отвязывает."""
    year = _create_goal(client, "Год")
    quarter = _create_goal(client, "Квартал", horizon="quarter", parentId=year["id"])
    assert quarter["parentId"] == year["id"]

    self_link = client.patch("/api/goals/%d" % year["id"], json={"parentId": year["id"]})
    assert self_link.status_code == 422

    cycle = client.patch("/api/goals/%d" % year["id"], json={"parentId": quarter["id"]})
    assert cycle.status_code == 422
    assert "цикл" in cycle.json()["detail"]

    detached = client.patch("/api/goals/%d" % quarter["id"], json={"parentId": None})
    assert detached.status_code == 200
    assert detached.json()["parentId"] is None


def test_goal_delete_cascades_steps_and_unlinks_tasks(
    client: TestClient, settings
) -> None:
    """Удаление цели уносит вехи, но задачи остаются с ``goal_id = NULL``."""
    goal = _create_goal(client, "Удаляемая")
    _create_step(client, goal["id"])
    task_id = _create_task(settings, goal["id"])

    response = client.delete("/api/goals/%d" % goal["id"])
    assert response.status_code == 200, response.text
    assert response.json() == {"id": goal["id"], "deleted": True}
    assert _sql(settings, "SELECT count(*) FROM goal_steps WHERE goal_id = ?", (goal["id"],))[0] == 0
    row = _sql(settings, "SELECT goal_id FROM tasks WHERE id = ?", (task_id,))
    assert row[0] is None
    assert client.delete("/api/goals/%d" % goal["id"]).status_code == 404


# --------------------------------------------------------------------------
# Вехи
# --------------------------------------------------------------------------


def test_goal_steps_crud(client: TestClient) -> None:
    """Веха добавляется в конец, отмечается и удаляется."""
    goal = _create_goal(client, "Цель с вехами")
    first = _create_step(client, goal["id"], "Первая")
    second = _create_step(client, goal["id"], "Вторая")
    assert second["sort"] == first["sort"] + 1
    assert first["done"] is False
    assert first["goalId"] == goal["id"]

    done = client.patch("/api/goal-steps/%d" % first["id"], json={"done": True})
    assert done.status_code == 200, done.text
    assert done.json()["done"] is True
    renamed = client.patch("/api/goal-steps/%d" % second["id"], json={"title": "Вторая v2"})
    assert renamed.json()["title"] == "Вторая v2"

    detail = client.get("/api/compass").json()["goals"][0]
    assert [step["title"] for step in detail["steps"]] == ["Первая", "Вторая v2"]

    removed = client.delete("/api/goal-steps/%d" % second["id"])
    assert removed.status_code == 200
    assert removed.json() == {"id": second["id"], "deleted": True}
    assert client.delete("/api/goal-steps/%d" % second["id"]).status_code == 404


def test_goal_step_errors(client: TestClient) -> None:
    """404 — нет цели/вехи, 422 — пустое название или плохая дата, 400 — пусто."""
    goal = _create_goal(client, "Цель")
    step = _create_step(client, goal["id"])
    assert client.post("/api/goals/999/steps", json={"title": "Веха"}).status_code == 404
    assert client.post("/api/goals/%d/steps" % goal["id"], json={"title": "  "}).status_code == 422
    assert (
        client.post(
            "/api/goals/%d/steps" % goal["id"], json={"title": "Веха", "due": "2026-02-30"}
        ).status_code
        == 422
    )
    assert client.patch("/api/goal-steps/999", json={"done": True}).status_code == 404
    assert client.patch("/api/goal-steps/%d" % step["id"], json={}).status_code == 400
    assert client.patch("/api/goal-steps/%d" % step["id"], json={"done": None}).status_code == 422


# --------------------------------------------------------------------------
# Прогресс цели
# --------------------------------------------------------------------------


def test_goal_progress_from_steps(client: TestClient) -> None:
    """Прогресс — доля закрытых вех, округлённая до процента."""
    goal = _create_goal(client, "Четыре вехи")
    steps = [_create_step(client, goal["id"], "Веха %d" % idx) for idx in range(1, 5)]
    client.patch("/api/goal-steps/%d" % steps[0]["id"], json={"done": True})
    detail = client.get("/api/compass").json()["goals"][0]
    assert detail["progressPct"] == 25
    assert detail["counts"]["stepsDone"] == 1
    assert detail["counts"]["stepsTotal"] == 4


def test_goal_progress_manual_override(client: TestClient) -> None:
    """Ручное значение важнее вех; ``progress = 0`` возвращает автоматику."""
    goal = _create_goal(client, "Ручной прогресс")
    steps = [_create_step(client, goal["id"], "Веха %d" % idx) for idx in range(2)]
    client.patch("/api/goal-steps/%d" % steps[0]["id"], json={"done": True})
    assert client.get("/api/compass").json()["goals"][0]["progressPct"] == 50

    overridden = client.patch("/api/goals/%d" % goal["id"], json={"progress": 60})
    assert overridden.json()["progressPct"] == 60

    automatic = client.patch("/api/goals/%d" % goal["id"], json={"progress": 0})
    assert automatic.json()["progressPct"] == 50


def test_goal_progress_from_tasks(client: TestClient, settings) -> None:
    """Без вех прогресс считают связанные задачи; отменённые не в счёте."""
    goal = _create_goal(client, "Задачи вместо вех")
    for status in ("todo", "todo", "todo", "done", "cancelled"):
        _create_task(settings, goal["id"], status)
    detail = client.get("/api/compass").json()["goals"][0]
    assert detail["counts"]["tasksTotal"] == 4
    assert detail["counts"]["tasksDone"] == 1
    assert detail["progressPct"] == 25


def test_goal_progress_steps_beat_tasks(client: TestClient, settings) -> None:
    """Если появились вехи — они важнее задач."""
    goal = _create_goal(client, "Вехи и задачи")
    for status in ("todo", "todo", "todo", "done"):
        _create_task(settings, goal["id"], status)
    steps = [_create_step(client, goal["id"], "Веха %d" % idx) for idx in range(2)]
    client.patch("/api/goal-steps/%d" % steps[0]["id"], json={"done": True})
    detail = client.get("/api/compass").json()["goals"][0]
    assert detail["counts"]["tasksTotal"] == 4
    assert detail["progressPct"] == 50


# --------------------------------------------------------------------------
# Анти-цели
# --------------------------------------------------------------------------


def test_anti_goals_crud(client: TestClient) -> None:
    """Анти-цели месяца и квартала: создание, фильтр, правка, удаление."""
    first = client.post("/api/anti-goals", json={"period": "month", "text": "Не брать срочное"})
    assert first.status_code == 201, first.text
    month = first.json()
    assert month["period"] == "month"
    assert month["active"] is True
    quarter = client.post(
        "/api/anti-goals", json={"period": "quarter", "text": "Не больше 2 проектов"}
    ).json()

    everything = client.get("/api/anti-goals").json()
    assert [item["id"] for item in everything["items"]] == [month["id"], quarter["id"]]
    assert everything["counts"] == {"month": 1, "quarter": 1}

    only_quarter = client.get("/api/anti-goals", params={"period": "quarter"}).json()
    assert [item["id"] for item in only_quarter["items"]] == [quarter["id"]]

    patched = client.patch(
        "/api/anti-goals/%d" % month["id"], json={"text": "Не брать срочное после 18:00", "active": False}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["text"] == "Не брать срочное после 18:00"
    assert patched.json()["active"] is False

    removed = client.delete("/api/anti-goals/%d" % month["id"])
    assert removed.json() == {"id": month["id"], "deleted": True}
    assert client.delete("/api/anti-goals/%d" % month["id"]).status_code == 404


def test_anti_goals_validation(client: TestClient) -> None:
    """Период только month/quarter, текст не пустой, чужая запись — 404."""
    assert client.post("/api/anti-goals", json={"period": "year", "text": "Нет"}).status_code == 422
    assert client.post("/api/anti-goals", json={"period": "month", "text": "   "}).status_code == 422
    assert client.post("/api/anti-goals", json={"period": "month"}).status_code == 422
    assert client.patch("/api/anti-goals/999", json={"text": "Нет"}).status_code == 404
    assert client.delete("/api/anti-goals/999").status_code == 404


# --------------------------------------------------------------------------
# Снимок Компаса
# --------------------------------------------------------------------------


def test_compass_payload_shape_and_order(client: TestClient, settings) -> None:
    """В компас попадают неархивные сферы, цели по горизонтам и оба списка."""
    active_sphere = _create_sphere(client, "Работа")
    archived_sphere = _create_sphere(client, "Старое")
    client.patch("/api/spheres/%d" % archived_sphere["id"], json={"archived": True})

    year = _create_goal(client, "Год", horizon="year", sphereId=active_sphere["id"])
    quarter = _create_goal(client, "Квартал", horizon="quarter", parentId=year["id"])
    month = _create_goal(client, "Месяц", horizon="month", parentId=quarter["id"])
    _create_step(client, month["id"], "Веха")
    client.post("/api/anti-goals", json={"period": "month", "text": "Не брать срочное"})

    payload = client.get("/api/compass", params={"date": "2026-11-15"}).json()
    assert payload["date"] == "2026-11-15"
    assert [item["id"] for item in payload["spheres"]] == [active_sphere["id"]]
    assert [item["id"] for item in payload["goals"]] == [year["id"], quarter["id"], month["id"]]
    assert [item["id"] for item in payload["antiGoals"]["month"]] != []
    assert payload["antiGoals"]["quarter"] == []

    goal = payload["goals"][2]
    for field in (
        "id",
        "title",
        "horizon",
        "sphereId",
        "sphere",
        "parentId",
        "deadline",
        "status",
        "progressPct",
        "why",
        "steps",
        "counts",
    ):
        assert field in goal
    assert goal["steps"][0]["title"] == "Веха"
    assert goal["parentId"] == quarter["id"]
    # ``settings`` в фикстуре — чтобы тест не зависел от реального «сегодня».
    assert _sql(settings, "SELECT count(*) FROM spheres")[0] == 2
