"""Тесты API Библиотеки и Обучения (``app/server/api/library.py``).

Проверяется HTTP-слой целиком, а не вызов функций: коды ответов (201/400/404/
422), camelCase-контракт §12.3, полки и поиск, прогресс страниц с автозакрытием
на 100 %, каскадное удаление, цитаты/мысли/действия и «превратить в задачу»
с реальной связью ``book_actions.task_id``.

База — во временном каталоге (``tmp_path``), рабочая ``data/`` не затрагивается.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.server.db import connect
from app.server.main import create_app
from app.server.settings import load_settings

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def _count(settings, table: str, where: str = "", params: tuple = ()) -> int:
    row = _sql(settings, "SELECT count(*) FROM %s %s" % (table, where), params)
    return int(row[0]) if row is not None else -1


def _create_book(client: TestClient, **fields) -> dict:
    payload = {"title": "Атомные привычки"}
    payload.update(fields)
    response = client.post("/api/books", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_insight(client: TestClient, book_id: int, text: str = "Маленькие привычки") -> dict:
    response = client.post("/api/books/%d/insights" % book_id, json={"text": text})
    assert response.status_code == 201, response.text
    return response.json()


def _create_quote(client: TestClient, book_id: int, text: str = "Ты — то, что делаешь", page=None) -> dict:
    payload: dict = {"text": text}
    if page is not None:
        payload["page"] = page
    response = client.post("/api/books/%d/quotes" % book_id, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_action(client: TestClient, book_id: int, text: str = "Завести трекер") -> dict:
    response = client.post("/api/books/%d/actions" % book_id, json={"text": text})
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# Книги: создание
# --------------------------------------------------------------------------


def test_create_book_defaults(client: TestClient) -> None:
    book = _create_book(client, title="  Атомные привычки  ")
    assert isinstance(book["id"], int)
    assert book["title"] == "Атомные привычки"
    assert book["author"] == ""
    assert book["category"] == "nonfiction"
    assert book["pagesTotal"] == 0
    assert book["pagesRead"] == 0
    assert book["shelf"] == "queue"
    assert book["rating"] is None
    assert book["priority"] == 0
    assert book["finishedAt"] is None
    assert book["pct"] is None
    assert book["created"]

    # У новой книги ещё нет мыслей, цитат и действий.
    detail = client.get("/api/books/%d" % book["id"]).json()
    assert detail["insights"] == []
    assert detail["quotes"] == []
    assert detail["actions"] == []


def test_create_book_with_all_fields(client: TestClient) -> None:
    book = _create_book(
        client,
        title="Дюна",
        author="Фрэнк Герберт",
        category="fiction",
        pagesTotal=500,
        shelf="reading",
        why="Хочу перечитать перед экранизацией",
        priority=3,
    )
    assert book["author"] == "Фрэнк Герберт"
    assert book["category"] == "fiction"
    assert book["pagesTotal"] == 500
    assert book["pagesRead"] == 0
    assert book["shelf"] == "reading"
    assert book["priority"] == 3
    assert book["why"].startswith("Хочу перечитать")


def test_create_book_done_shelf_closes_book(client: TestClient, settings) -> None:
    book = _create_book(client, title="Прочитанная", pagesTotal=320, shelf="done")
    assert book["shelf"] == "done"
    assert book["pagesRead"] == 320
    assert book["pct"] == 100.0
    assert DATE_RE.match(book["finishedAt"] or ""), book["finishedAt"]
    assert book["finishedAt"] == settings.today()


@pytest.mark.parametrize(
    "payload",
    [
        {},  # нет названия
        {"title": ""},
        {"title": "   "},
        {"title": "Книга", "category": "poetry"},
        {"title": "Книга", "shelf": "later"},
        {"title": "Книга", "pagesTotal": -1},
        {"title": "Книга", "priority": -1},
        {"title": "Книга", "pagesTotal": "много"},
    ],
)
def test_create_book_validation_422(client: TestClient, payload: dict) -> None:
    response = client.post("/api/books", json=payload)
    assert response.status_code == 422, response.text


def test_extra_fields_are_ignored(client: TestClient) -> None:
    book = _create_book(client, title="Книга", unknownField="игнор")
    assert book["title"] == "Книга"


# --------------------------------------------------------------------------
# Книги: список, полки, поиск
# --------------------------------------------------------------------------


def test_list_books_shelves_and_counts(client: TestClient) -> None:
    reading = _create_book(client, title="Читаю", shelf="reading")
    _create_book(client, title="В очереди", shelf="queue")
    _create_book(client, title="Прочитал", pagesTotal=100, shelf="done")
    _create_book(client, title="Отложил", shelf="dropped")

    everything = client.get("/api/books").json()
    assert len(everything["items"]) == 4
    assert everything["counts"] == {"reading": 1, "queue": 1, "done": 1, "dropped": 1, "all": 4}
    assert everything["shelf"] == "all"
    assert everything["q"] is None

    only_reading = client.get("/api/books", params={"shelf": "reading"}).json()
    assert [item["id"] for item in only_reading["items"]] == [reading["id"]]
    assert only_reading["shelf"] == "reading"
    # Счётчики вкладок не зависят от выбранной полки.
    assert only_reading["counts"]["all"] == 4

    empty = client.get("/api/books", params={"shelf": "dropped"}).json()
    assert len(empty["items"]) == 1

    assert client.get("/api/books", params={"shelf": "later"}).status_code == 422


def test_list_books_order_by_priority(client: TestClient) -> None:
    _create_book(client, title="Низкий", priority=1)
    top = _create_book(client, title="Важная", priority=9)
    _create_book(client, title="Средняя", priority=4)

    items = client.get("/api/books", params={"shelf": "queue"}).json()["items"]
    assert [item["title"] for item in items] == ["Важная", "Средняя", "Низкий"]
    assert items[0]["id"] == top["id"]


def test_list_books_search_q(client: TestClient) -> None:
    habit = _create_book(
        client, title="Атомные привычки", author="Джеймс Клир", why="Изменить рутину"
    )
    dune = _create_book(client, title="Дюна", author="Фрэнк Герберт")

    by_title = client.get("/api/books", params={"q": "атомные"}).json()
    assert [item["id"] for item in by_title["items"]] == [habit["id"]]

    by_author = client.get("/api/books", params={"q": "ГЕРБЕРТ"}).json()
    assert [item["id"] for item in by_author["items"]] == [dune["id"]]

    by_why = client.get("/api/books", params={"q": "рутин"}).json()
    assert [item["id"] for item in by_why["items"]] == [habit["id"]]

    empty = client.get("/api/books", params={"q": "нет такой книги"}).json()
    assert empty["items"] == []
    assert empty["counts"]["all"] == 2

    # Поиск вместе с полкой: фильтры пересекаются.
    _create_book(client, title="Атомные привычки 2", shelf="reading")
    narrowed = client.get("/api/books", params={"q": "атомные", "shelf": "queue"}).json()
    assert [item["id"] for item in narrowed["items"]] == [habit["id"]]


# --------------------------------------------------------------------------
# Книги: карточка, правка, удаление
# --------------------------------------------------------------------------


def test_get_book_detail(client: TestClient) -> None:
    book = _create_book(client, title="Атомные привычки", pagesTotal=300)
    insight = _create_insight(client, book["id"], "Сначала среда, потом мотивация")
    quote = _create_quote(client, book["id"], "Мотивация переоценена", page=12)
    action = _create_action(client, book["id"], "Ввести утренний ритуал")

    detail = client.get("/api/books/%d" % book["id"]).json()
    assert set(detail) == {"book", "insights", "quotes", "actions"}
    assert detail["book"]["id"] == book["id"]
    assert [item["id"] for item in detail["insights"]] == [insight["id"]]
    assert [item["id"] for item in detail["quotes"]] == [quote["id"]]
    assert [item["id"] for item in detail["actions"]] == [action["id"]]
    assert detail["quotes"][0]["page"] == 12
    assert detail["actions"][0]["taskId"] is None

    assert client.get("/api/books/9999").status_code == 404


def test_patch_book_fields(client: TestClient) -> None:
    book = _create_book(client, title="Черновик")
    response = client.patch(
        "/api/books/%d" % book["id"],
        json={
            "title": "  Новое название ",
            "author": "Автор",
            "category": "pro",
            "pagesTotal": 250,
            "why": "Для работы",
            "priority": 5,
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["title"] == "Новое название"
    assert updated["author"] == "Автор"
    assert updated["category"] == "pro"
    assert updated["pagesTotal"] == 250
    assert updated["why"] == "Для работы"
    assert updated["priority"] == 5

    page = client.get("/api/books/%d" % book["id"]).json()["book"]
    assert page["title"] == "Новое название"
    assert page["priority"] == 5


def test_patch_book_requires_changes(client: TestClient) -> None:
    book = _create_book(client)
    response = client.patch("/api/books/%d" % book["id"], json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "Нечего обновлять"
    assert client.patch("/api/books/9999", json={"title": "Нет"}).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "   "},
        {"title": None},
        {"rating": 0},
        {"rating": 6},
        {"rating": "пять"},
        {"category": "poetry"},
        {"shelf": "later"},
        {"pagesTotal": -5},
        {"pagesRead": -1},
    ],
)
def test_patch_book_validation_422(client: TestClient, payload: dict) -> None:
    book = _create_book(client)
    assert client.patch("/api/books/%d" % book["id"], json=payload).status_code == 422


def test_patch_book_rating_can_be_cleared(client: TestClient) -> None:
    book = _create_book(client, title="Оценённая", pagesTotal=10)
    rated = client.patch("/api/books/%d" % book["id"], json={"rating": 5}).json()
    assert rated["rating"] == 5
    cleared = client.patch("/api/books/%d" % book["id"], json={"rating": None}).json()
    assert cleared["rating"] is None


def test_patch_book_shelf_done_shifts_progress(client: TestClient, settings) -> None:
    book = _create_book(client, title="Дочитываю", pagesTotal=100, shelf="reading")
    client.post("/api/books/%d/progress" % book["id"], json={"pagesRead": 20})

    done = client.patch("/api/books/%d" % book["id"], json={"shelf": "done"}).json()
    assert done["shelf"] == "done"
    assert done["pagesRead"] == 100
    assert done["finishedAt"] == settings.today()

    back = client.patch("/api/books/%d" % book["id"], json={"shelf": "reading"}).json()
    assert back["shelf"] == "reading"
    assert back["finishedAt"] is None


def test_patch_book_explicit_finished_at(client: TestClient) -> None:
    book = _create_book(client, pagesTotal=100)
    done = client.patch(
        "/api/books/%d" % book["id"], json={"shelf": "done", "finishedAt": "2026-01-15"}
    ).json()
    assert done["finishedAt"] == "2026-01-15"

    bad = client.patch("/api/books/%d" % book["id"], json={"finishedAt": "15.01.2026"})
    assert bad.status_code == 422


def test_patch_book_pages_read_clamped(client: TestClient) -> None:
    book = _create_book(client, title="Тонкая", pagesTotal=50)
    updated = client.patch("/api/books/%d" % book["id"], json={"pagesRead": 80}).json()
    assert updated["pagesRead"] == 50
    assert updated["shelf"] == "done"
    assert updated["pct"] == 100.0

    reduced = client.patch("/api/books/%d" % book["id"], json={"pagesTotal": 40}).json()
    assert reduced["pagesTotal"] == 40
    assert reduced["pagesRead"] == 40
    assert reduced["shelf"] == "done"


def test_patch_book_pages_read_requires_total(client: TestClient) -> None:
    book = _create_book(client, title="Без объёма")
    response = client.patch("/api/books/%d" % book["id"], json={"pagesRead": 5})
    assert response.status_code == 422
    assert "pagesTotal" in response.json()["detail"]


def test_delete_book_cascades(client: TestClient, settings) -> None:
    book = _create_book(client, title="Удаляемая", pagesTotal=10)
    _create_insight(client, book["id"])
    _create_quote(client, book["id"])
    _create_action(client, book["id"])

    response = client.delete("/api/books/%d" % book["id"])
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": book["id"]}
    assert client.get("/api/books/%d" % book["id"]).status_code == 404

    assert _count(settings, "books") == 0
    assert _count(settings, "book_insights") == 0
    assert _count(settings, "book_quotes") == 0
    assert _count(settings, "book_actions") == 0
    assert client.delete("/api/books/%d" % book["id"]).status_code == 404


# --------------------------------------------------------------------------
# Прогресс страниц
# --------------------------------------------------------------------------


def test_progress_sets_pages_and_percent(client: TestClient) -> None:
    book = _create_book(client, title="Читаю", pagesTotal=400)
    updated = client.post("/api/books/%d/progress" % book["id"], json={"pagesRead": 120})
    assert updated.status_code == 200, updated.text
    payload = updated.json()
    assert payload["pagesRead"] == 120
    assert payload["pagesTotal"] == 400
    assert payload["pct"] == 30.0
    assert payload["shelf"] == "queue"
    assert payload["finishedAt"] is None


def test_progress_clamps_to_pages_total(client: TestClient) -> None:
    book = _create_book(client, title="Тонкая", pagesTotal=90)
    payload = client.post("/api/books/%d/progress" % book["id"], json={"pagesRead": 500}).json()
    assert payload["pagesRead"] == 90
    assert payload["pct"] == 100.0


def test_progress_done_at_100_percent(client: TestClient, settings) -> None:
    book = _create_book(client, title="Дочитываю", pagesTotal=200)
    partial = client.post("/api/books/%d/progress" % book["id"], json={"pagesRead": 199}).json()
    assert partial["shelf"] == "queue"

    finished = client.post("/api/books/%d/progress" % book["id"], json={"pagesRead": 200}).json()
    assert finished["shelf"] == "done"
    assert finished["pagesRead"] == 200
    assert finished["finishedAt"] == settings.today()

    # Откат прогресса возвращает на полку «Читаю сейчас» и снимает дату.
    rolled_back = client.post("/api/books/%d/progress" % book["id"], json={"pagesRead": 150}).json()
    assert rolled_back["shelf"] == "reading"
    assert rolled_back["finishedAt"] is None
    assert rolled_back["pagesRead"] == 150


def test_progress_requires_pages_total(client: TestClient) -> None:
    book = _create_book(client, title="Без объёма")
    response = client.post("/api/books/%d/progress" % book["id"], json={"pagesRead": 10})
    assert response.status_code == 422
    assert "pagesTotal" in response.json()["detail"]

    # Ноль страниц при неизвестном объёме — допустимо.
    zero = client.post("/api/books/%d/progress" % book["id"], json={"pagesRead": 0})
    assert zero.status_code == 200
    assert zero.json()["pagesRead"] == 0


def test_progress_validation_and_404(client: TestClient) -> None:
    book = _create_book(client, title="Книга", pagesTotal=100)
    assert (
        client.post("/api/books/%d/progress" % book["id"], json={"pagesRead": -1}).status_code
        == 422
    )
    assert (
        client.post("/api/books/%d/progress" % book["id"], json={"pagesRead": "много"}).status_code
        == 422
    )
    assert client.post("/api/books/9999/progress", json={"pagesRead": 1}).status_code == 404


# --------------------------------------------------------------------------
# Мысли
# --------------------------------------------------------------------------


def test_insights_crud(client: TestClient) -> None:
    book = _create_book(client)
    first = _create_insight(client, book["id"], "Мысль первая")
    second = _create_insight(client, book["id"], "Мысль вторая")
    assert first["bookId"] == book["id"]
    assert first["sort"] == 1
    assert second["sort"] == 2

    patched = client.patch(
        "/api/book-insights/%d" % first["id"], json={"text": "Уточнённая мысль", "sort": 7}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["text"] == "Уточнённая мысль"
    assert patched.json()["sort"] == 7

    detail = client.get("/api/books/%d" % book["id"]).json()
    # Правка sort переставляет мысль в конец списка.
    assert [item["text"] for item in detail["insights"]] == ["Мысль вторая", "Уточнённая мысль"]

    deleted = client.delete("/api/book-insights/%d" % second["id"])
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "id": second["id"]}
    assert client.delete("/api/book-insights/%d" % second["id"]).status_code == 404


def test_insights_validation(client: TestClient) -> None:
    book = _create_book(client)
    assert client.post("/api/books/%d/insights" % book["id"], json={"text": ""}).status_code == 422
    assert (
        client.post("/api/books/%d/insights" % book["id"], json={"text": "  "}).status_code == 422
    )
    assert client.post("/api/books/9999/insights", json={"text": "Мысль"}).status_code == 404

    insight = _create_insight(client, book["id"])
    assert client.patch("/api/book-insights/%d" % insight["id"], json={}).status_code == 400
    assert (
        client.patch("/api/book-insights/%d" % insight["id"], json={"text": None}).status_code
        == 422
    )
    assert (
        client.patch("/api/book-insights/%d" % insight["id"], json={"text": "  "}).status_code
        == 422
    )
    assert client.patch("/api/book-insights/9999", json={"text": "x"}).status_code == 404


# --------------------------------------------------------------------------
# Цитаты
# --------------------------------------------------------------------------


def test_quotes_crud(client: TestClient, settings) -> None:
    book = _create_book(client)
    with_page = _create_quote(client, book["id"], "Цитата со страницей", page=42)
    without_page = _create_quote(client, book["id"], "Цитата без страницы")
    assert with_page["page"] == 42
    assert with_page["bookId"] == book["id"]
    assert without_page["page"] is None

    detail = client.get("/api/books/%d" % book["id"]).json()
    assert len(detail["quotes"]) == 2
    # Сначала цитаты со страницами — по возрастанию номера.
    assert detail["quotes"][0]["id"] == with_page["id"]

    assert client.delete("/api/book-quotes/%d" % with_page["id"]).status_code == 200
    assert client.delete("/api/book-quotes/%d" % with_page["id"]).status_code == 404
    assert _count(settings, "book_quotes") == 1


def test_quotes_validation(client: TestClient) -> None:
    book = _create_book(client)
    assert (
        client.post("/api/books/%d/quotes" % book["id"], json={"text": ""}).status_code == 422
    )
    assert (
        client.post(
            "/api/books/%d/quotes" % book["id"], json={"text": "Цитата", "page": -2}
        ).status_code
        == 422
    )
    assert client.post("/api/books/9999/quotes", json={"text": "Цитата"}).status_code == 404


# --------------------------------------------------------------------------
# Действия и «превратить в задачу»
# --------------------------------------------------------------------------


def test_action_to_task_links_task(client: TestClient, settings) -> None:
    book = _create_book(client, title="Атомные привычки")
    action = _create_action(client, book["id"], "Завести трекер привычек")
    assert action["taskId"] is None

    response = client.post(
        "/api/book-actions/%d/to-task" % action["id"], json={"dueDate": "2026-12-01"}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"task", "action"}

    task = payload["task"]
    assert task["title"] == "Завести трекер привычек"
    assert task["dueDate"] == "2026-12-01"
    assert task["listKind"] == "personal"
    assert task["resource"] == "important"
    assert task["status"] == "todo"
    assert "Атомные привычки" in task["notes"]
    assert payload["action"]["taskId"] == task["id"]

    # Связь реально записана в book_actions.task_id.
    stored = _sql(settings, "SELECT task_id FROM book_actions WHERE id = ?", (action["id"],))
    assert stored is not None and int(stored["task_id"]) == task["id"]
    # Задача создана в tasks.
    task_row = _sql(settings, "SELECT * FROM tasks WHERE id = ?", (task["id"],))
    assert task_row is not None
    assert task_row["title"] == "Завести трекер привычек"
    assert task_row["due_date"] == "2026-12-01"

    detail = client.get("/api/books/%d" % book["id"]).json()
    assert detail["actions"][0]["taskId"] == task["id"]


def test_action_to_task_is_idempotent(client: TestClient, settings) -> None:
    book = _create_book(client)
    action = _create_action(client, book["id"])

    first = client.post("/api/book-actions/%d/to-task" % action["id"], json={}).json()
    second = client.post("/api/book-actions/%d/to-task" % action["id"], json={}).json()

    assert first["task"]["id"] == second["task"]["id"]
    assert _count(settings, "tasks") == 1
    # Повторный вызов не переписывает ссылку.
    stored = _sql(settings, "SELECT task_id FROM book_actions WHERE id = ?", (action["id"],))
    assert int(stored["task_id"]) == first["task"]["id"]


def test_action_to_task_without_body(client: TestClient, settings) -> None:
    book = _create_book(client)
    action = _create_action(client, book["id"], "Позвонить в клинику")

    response = client.post("/api/book-actions/%d/to-task" % action["id"])
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["title"] == "Позвонить в клинику"
    assert task["dueDate"] is None
    assert _count(settings, "tasks") == 1


def test_action_to_task_validation(client: TestClient) -> None:
    book = _create_book(client)
    action = _create_action(client, book["id"])
    assert (
        client.post(
            "/api/book-actions/%d/to-task" % action["id"], json={"dueDate": "01.12.2026"}
        ).status_code
        == 422
    )
    assert client.post("/api/book-actions/9999/to-task", json={}).status_code == 404
    assert (
        client.post("/api/books/%d/actions" % book["id"], json={"text": ""}).status_code == 422
    )
    assert client.post("/api/books/9999/actions", json={"text": "Действие"}).status_code == 404


# --------------------------------------------------------------------------
# Обучение
# --------------------------------------------------------------------------


def _seed_course(settings, *, title: str = "Основы питания") -> tuple[int, int]:
    """Курс с тремя уроками (один пройден) и один дополнительный курс."""
    conn = connect(settings.db_path)
    try:
        with conn:
            course_id = int(
                conn.execute(
                    "INSERT INTO courses (title, category, cover) "
                    "VALUES (?, 'питание', 'covers/base.png')",
                    (title,),
                ).lastrowid
            )
            for idx, status in enumerate(("done", "new", "new"), start=1):
                conn.execute(
                    "INSERT INTO lessons (course_id, idx, title, status) VALUES (?, ?, ?, ?)",
                    (course_id, idx, "Урок %d" % idx, status),
                )
            empty_course_id = int(
                conn.execute("INSERT INTO courses (title) VALUES ('Пустой курс')").lastrowid
            )
    finally:
        conn.close()
    return course_id, empty_course_id


def test_learning_list(client: TestClient, settings) -> None:
    course_id, empty_course_id = _seed_course(settings)

    payload = client.get("/api/learning").json()
    assert set(payload) == {"courses"}
    courses = {item["id"]: item for item in payload["courses"]}
    assert set(courses) == {course_id, empty_course_id}

    course = courses[course_id]
    assert course["title"] == "Основы питания"
    assert course["organizer"] == ""
    assert course["deadline"] is None
    assert course["projectId"] is None
    assert course["goalId"] is None
    assert course["notes"] == ""
    assert course["lessonsTotal"] == 3
    assert course["lessonsDone"] == 1
    assert course["pct"] == 33
    assert course["cover"] == "covers/base.png"
    assert course["category"] == "питание"

    empty = courses[empty_course_id]
    assert empty["lessonsTotal"] == 0
    assert empty["lessonsDone"] == 0
    assert empty["pct"] == 0


def test_patch_course_metadata(client: TestClient, settings) -> None:
    course_id, _ = _seed_course(settings)
    response = client.patch(
        "/api/courses/%d" % course_id,
        json={
            "organizer": "Школа питания",
            "deadline": "2027-03-31",
            "notes": "Конспект: считать белок",
        },
    )
    assert response.status_code == 200, response.text
    course = response.json()
    assert course["organizer"] == "Школа питания"
    assert course["deadline"] == "2027-03-31"
    assert course["notes"] == "Конспект: считать белок"
    # Прогресс не сломали.
    assert course["lessonsTotal"] == 3
    assert course["lessonsDone"] == 1
    assert course["pct"] == 33

    listed = {item["id"]: item for item in client.get("/api/learning").json()["courses"]}
    assert listed[course_id]["organizer"] == "Школа питания"

    # Null снимает дедлайн, пустая строка — организатора и конспект.
    cleared = client.patch(
        "/api/courses/%d" % course_id,
        json={"deadline": None, "organizer": "", "notes": ""},
    ).json()
    assert cleared["deadline"] is None
    assert cleared["organizer"] == ""
    assert cleared["notes"] == ""


def test_patch_course_relations(client: TestClient, settings) -> None:
    course_id, _ = _seed_course(settings)
    conn = connect(settings.db_path)
    try:
        with conn:
            project_id = int(
                conn.execute("INSERT INTO projects (name) VALUES ('Здоровье')").lastrowid
            )
            goal_id = int(
                conn.execute("INSERT INTO goals (title) VALUES ('Питание')").lastrowid
            )
    finally:
        conn.close()

    linked = client.patch(
        "/api/courses/%d" % course_id,
        json={"projectId": project_id, "goalId": goal_id},
    ).json()
    assert linked["projectId"] == project_id
    assert linked["goalId"] == goal_id

    unlinked = client.patch(
        "/api/courses/%d" % course_id, json={"projectId": None, "goalId": None}
    ).json()
    assert unlinked["projectId"] is None
    assert unlinked["goalId"] is None


def test_patch_course_validation(client: TestClient, settings) -> None:
    course_id, _ = _seed_course(settings)
    assert client.patch("/api/courses/%d" % course_id, json={}).status_code == 400
    assert client.patch("/api/courses/9999", json={"organizer": "X"}).status_code == 404
    assert (
        client.patch("/api/courses/%d" % course_id, json={"deadline": "31.03.2027"}).status_code
        == 422
    )
    assert (
        client.patch("/api/courses/%d" % course_id, json={"projectId": 9999}).status_code == 422
    )
    assert client.patch("/api/courses/%d" % course_id, json={"goalId": 9999}).status_code == 422
    # FK-поля не должны быть нулевыми отрицательными id.
    assert (
        client.patch("/api/courses/%d" % course_id, json={"projectId": 0}).status_code == 422
    )


# --------------------------------------------------------------------------
# Регистрация маршрутов
# --------------------------------------------------------------------------


def test_library_routes_registered(client: TestClient) -> None:
    paths = set(client.get("/openapi.json").json()["paths"])
    expected = {
        "/api/books",
        "/api/books/{book_id}",
        "/api/books/{book_id}/progress",
        "/api/books/{book_id}/insights",
        "/api/books/{book_id}/quotes",
        "/api/books/{book_id}/actions",
        "/api/book-insights/{insight_id}",
        "/api/book-quotes/{quote_id}",
        "/api/book-actions/{action_id}/to-task",
        "/api/learning",
        "/api/courses/{course_id}",
    }
    missing = expected - paths
    assert not missing, "не зарегистрированы: %s" % sorted(missing)
