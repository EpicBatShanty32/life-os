"""Библиотека и обучение: ``/api/books*``, ``/api/book-*``, ``/api/learning``.

Контракт v4 (``studio/HANDOFF-OPUS-5.5.md`` §12.3). JSON — camelCase, ошибки —
``{"detail": "..."}`` с кодом 4xx; деньги и даты не участвуют.

Полки книг: ``reading`` / ``queue`` / ``done`` / ``dropped``. Прогресс страниц
никогда не превышает ``pagesTotal``: на 100 % книга автоматически уходит на
полку «Прочитано» с датой завершения, а при откате прогресса ниже 100 %
возвращается на «Читаю сейчас». Если ``pagesTotal`` не задан (0), записать
прочитанные страницы нельзя — сначала нужно указать объём книги (422).

Действие на внедрение (``book_actions``) превращается в задачу
``POST /api/book-actions/{id}/to-task``: задача создаётся в ``tasks`` и её id
сохраняется в ``book_actions.task_id``. Повторный вызов возвращает уже
связанную задачу и не создаёт дубликат.

``GET /api/learning`` — курсы с прогрессом по урокам (``pct`` — проценты
0..100), ``PATCH /api/courses/{id}`` — метаданные обучения (организатор,
дедлайн доступа, привязка к проекту/цели, конспект).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.server.api.deps import ConnectionDep, SettingsDep

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["library"])

Shelf = Literal["reading", "queue", "done", "dropped"]
BookCategory = Literal["pro", "fiction", "nonfiction"]

_SHELVES: tuple[str, ...] = ("reading", "queue", "done", "dropped")

#: Курс с агрегатами по урокам — тот же счёт, что в ``api/courses.py``.
_COURSE_SELECT = (
    "SELECT c.*, "
    " (SELECT count(*) FROM lessons AS l WHERE l.course_id = c.id) AS lessons_total, "
    " (SELECT count(*) FROM lessons AS l WHERE l.course_id = c.id AND l.status = 'done') "
    "   AS lessons_done "
    "FROM courses AS c"
)


# --------------------------------------------------------------------------
# Входные модели
# --------------------------------------------------------------------------


def _non_empty(value: str, message: str = "Текст не может быть пустым") -> str:
    """Обрезает пробелы и запрещает пустую строку (иначе FastAPI отдаст 422)."""
    text = value.strip()
    if not text:
        raise ValueError(message)
    return text


class BookCreate(BaseModel):
    """Тело ``POST /api/books``."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=300)
    author: str = Field("", max_length=200)
    category: BookCategory = "nonfiction"
    pagesTotal: int = Field(0, ge=0, le=1_000_000)  # noqa: N815 - camelCase JSON
    shelf: Shelf = "queue"
    why: str = Field("", max_length=4000)
    priority: int = Field(0, ge=0, le=1000)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Название книги не может быть пустым")


class BookPatch(BaseModel):
    """Тело ``PATCH /api/books/{id}``: любое подмножество полей."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(None, min_length=1, max_length=300)
    author: str | None = Field(None, max_length=200)
    category: BookCategory | None = None
    pagesTotal: int | None = Field(None, ge=0, le=1_000_000)  # noqa: N815
    pagesRead: int | None = Field(None, ge=0, le=1_000_000)  # noqa: N815
    shelf: Shelf | None = None
    rating: int | None = Field(None, ge=1, le=5)
    why: str | None = Field(None, max_length=4000)
    priority: int | None = Field(None, ge=0, le=1000)
    finishedAt: str | None = None  # noqa: N815

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Название книги не может быть пустым")


class BookProgressIn(BaseModel):
    """Тело ``POST /api/books/{id}/progress``."""

    model_config = ConfigDict(extra="ignore")

    pagesRead: int = Field(ge=0, le=1_000_000)  # noqa: N815


class InsightCreate(BaseModel):
    """Тело ``POST /api/books/{id}/insights``."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Мысль не может быть пустой")


class InsightPatch(BaseModel):
    """Тело ``PATCH /api/book-insights/{id}``."""

    model_config = ConfigDict(extra="ignore")

    text: str | None = Field(None, min_length=1, max_length=4000)
    sort: int | None = Field(None, ge=0, le=1_000_000)

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Мысль не может быть пустой")


class QuoteCreate(BaseModel):
    """Тело ``POST /api/books/{id}/quotes``."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=8000)
    page: int | None = Field(None, ge=0, le=1_000_000)

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Цитата не может быть пустой")


class ActionCreate(BaseModel):
    """Тело ``POST /api/books/{id}/actions``."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Действие не может быть пустым")


class ToTaskIn(BaseModel):
    """Тело ``POST /api/book-actions/{id}/to-task``."""

    model_config = ConfigDict(extra="ignore")

    dueDate: str | None = None  # noqa: N815


class CoursePatch(BaseModel):
    """Тело ``PATCH /api/courses/{id}`` — метаданные обучения."""

    model_config = ConfigDict(extra="ignore")

    organizer: str | None = Field(None, max_length=200)
    deadline: str | None = None
    projectId: int | None = Field(None, ge=1)  # noqa: N815
    goalId: int | None = Field(None, ge=1)  # noqa: N815
    notes: str | None = Field(None, max_length=20000)


# --------------------------------------------------------------------------
# Преобразование строк SQLite → JSON (camelCase)
# --------------------------------------------------------------------------


def _book_dict(row: Any) -> dict[str, Any]:
    total = int(row["pages_total"] or 0)
    read = int(row["pages_read"] or 0)
    pct = round(read / total * 100, 1) if total > 0 else None
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "author": str(row["author"] or ""),
        "category": str(row["category"] or "nonfiction"),
        "pagesTotal": total,
        "pagesRead": read,
        "shelf": str(row["shelf"] or "queue"),
        "rating": int(row["rating"]) if row["rating"] is not None else None,
        "why": str(row["why"] or ""),
        "priority": int(row["priority"] or 0),
        "finishedAt": row["finished_at"],
        "created": str(row["created"] or ""),
        "updated": row["updated"],
        "pct": pct,
    }


def _insight_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "bookId": int(row["book_id"]),
        "text": str(row["text"]),
        "sort": int(row["sort"] or 0),
    }


def _quote_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "bookId": int(row["book_id"]),
        "text": str(row["text"]),
        "page": int(row["page"]) if row["page"] is not None else None,
        "created": str(row["created"] or ""),
    }


def _action_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "bookId": int(row["book_id"]),
        "text": str(row["text"]),
        "taskId": int(row["task_id"]) if row["task_id"] is not None else None,
        "created": str(row["created"] or ""),
    }


def _task_dict(row: Any) -> dict[str, Any]:
    """Задача в том же camelCase-виде, что описан в §12.2 для ``/api/tasks``."""
    raw_tags = str(row["tags"] or "")
    tags = [tag.strip() for tag in raw_tags.split(",") if tag.strip()]
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "sphereId": row["sphere_id"],
        "projectId": row["project_id"],
        "goalId": row["goal_id"],
        "listKind": str(row["list_kind"] or "work"),
        "tags": tags,
        "dueDate": row["due_date"],
        "startAt": row["start_at"],
        "endAt": row["end_at"],
        "resource": str(row["resource"] or "important"),
        "status": str(row["status"] or "todo"),
        "recurrence": str(row["recurrence"] or ""),
        "notes": str(row["notes"] or ""),
        "doneAt": row["done_at"],
        "created": str(row["created"] or ""),
    }


def _course_dict(row: Any) -> dict[str, Any]:
    total = int(row["lessons_total"] or 0)
    done = int(row["lessons_done"] or 0)
    pct = int(round(done * 100 / total)) if total > 0 else 0
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "organizer": str(row["organizer"] or ""),
        "deadline": row["deadline"],
        "projectId": row["project_id"],
        "goalId": row["goal_id"],
        "notes": str(row["notes"] or ""),
        "lessonsTotal": total,
        "lessonsDone": done,
        "pct": pct,
        "cover": str(row["cover"] or ""),
        "category": str(row["category"] or ""),
        "created": str(row["created"] or ""),
    }


def _clean_date(value: str | None) -> str | None:
    """``YYYY-MM-DD`` (или полный ISO) → дата; пусто и ``null`` → ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none"}:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Ожидается дата в формате YYYY-MM-DD"
        ) from exc


# --------------------------------------------------------------------------
# Общие операции
# --------------------------------------------------------------------------


def _require_book(conn: Any, book_id: int) -> Any:
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Книга не найдена")
    return row


def _require_row(conn: Any, table: str, row_id: int, detail: str) -> Any:
    # ``table`` — константа модуля, пользовательские данные в SQL не попадают.
    row = conn.execute("SELECT * FROM %s WHERE id = ?" % table, (row_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row


def _shelf_counts(conn: Any) -> dict[str, int]:
    """Счётчики по полкам для вкладок: всегда по всей библиотеке, а не по ``q``."""
    counts = {shelf: 0 for shelf in _SHELVES}
    for row in conn.execute("SELECT shelf, count(*) AS total FROM books GROUP BY shelf"):
        shelf = str(row["shelf"])
        if shelf in counts:  # CHECK в схеме, но старые строки не должны ронять API
            counts[shelf] = int(row["total"])
    counts["all"] = sum(counts[shelf] for shelf in _SHELVES)
    return counts


def _update_book(conn: Any, book_id: int, updates: dict[str, Any]) -> None:
    """``UPDATE books`` по словарю колонок; ``updated`` обновляется всегда."""
    assignments = ", ".join("%s = ?" % column for column in updates)
    sql = (
        "UPDATE books SET "
        + assignments
        + ", updated = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?"
    )
    with conn:
        conn.execute(sql, (*updates.values(), book_id))


def _resolve_shelf(
    *,
    total: int,
    read: int,
    shelf: str,
    shelf_explicit: bool,
    finished_at: str | None,
    today: str,
) -> tuple[int, str, str | None]:
    """Сводит объём, прогресс и полку: 100 % → «прочитано», откат → «читаю».

    ``shelf_explicit`` — полку прислал клиент: тогда её значение важнее
    автоматики, но при ``done`` объём и дата завершения всё равно выравниваются.
    """
    if total <= 0 and read > 0:
        raise HTTPException(
            status_code=422,
            detail="Сначала укажите общее число страниц книги (pagesTotal)",
        )
    if total > 0:
        read = min(read, total)
    if shelf_explicit:
        if shelf == "done":
            return (total if total > 0 else read), "done", finished_at or today
        return read, shelf, None
    if total > 0 and read >= total:
        return read, "done", finished_at or today
    if str(shelf) == "done":
        return read, "reading", None
    return read, shelf, finished_at


# --------------------------------------------------------------------------
# Книги
# --------------------------------------------------------------------------


@router.get("/books")
def list_books(
    conn: ConnectionDep,
    shelf: Literal["reading", "queue", "done", "dropped", "all"] = Query(
        "all", description="Полка; ``all`` — вся библиотека"
    ),
    q: str | None = Query(None, max_length=200, description="Поиск по названию, автору, «зачем»"),
) -> dict[str, Any]:
    """Список книг с счётчиками по полкам.

    ``q`` фильтрует регистронезависимо (включая кириллицу), поэтому сравнение
    идёт в Python: SQLite ``LIKE``/``lower()`` для не-ASCII регистр не меняет.
    """
    sql = "SELECT * FROM books"
    params: list[Any] = []
    if shelf != "all":
        sql += " WHERE shelf = ?"
        params.append(shelf)
    sql += " ORDER BY priority DESC, created DESC, id DESC"
    items = [_book_dict(row) for row in conn.execute(sql, params).fetchall()]

    needle = (q or "").strip().casefold()
    if needle:
        items = [
            item
            for item in items
            if needle
            in " ".join((item["title"], item["author"], item["why"])).casefold()
        ]
    return {
        "items": items,
        "counts": _shelf_counts(conn),
        "shelf": shelf,
        "q": needle or None,
    }


@router.post("/books", status_code=201)
def create_book(
    payload: BookCreate, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Добавляет книгу; на полке ``done`` сразу проставляет 100 % и дату."""
    read = payload.pagesTotal if payload.shelf == "done" else 0
    finished = settings.today() if payload.shelf == "done" else None
    with conn:
        cursor = conn.execute(
            "INSERT INTO books "
            "(title, author, category, pages_total, pages_read, shelf, why, priority, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload.title,
                payload.author,
                payload.category,
                payload.pagesTotal,
                read,
                payload.shelf,
                payload.why,
                payload.priority,
                finished,
            ),
        )
        book_id = int(cursor.lastrowid or 0)
    return _book_dict(_require_book(conn, book_id))


@router.get("/books/{book_id}")
def get_book(book_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Карточка книги: ``book`` + ``insights`` + ``quotes`` + ``actions``."""
    book = _book_dict(_require_book(conn, book_id))
    insights = [
        _insight_dict(row)
        for row in conn.execute(
            "SELECT * FROM book_insights WHERE book_id = ? ORDER BY sort, id", (book_id,)
        )
    ]
    quotes = [
        _quote_dict(row)
        for row in conn.execute(
            "SELECT * FROM book_quotes WHERE book_id = ? "
            "ORDER BY CASE WHEN page IS NULL THEN 1 ELSE 0 END, page, id",
            (book_id,),
        )
    ]
    actions = [
        _action_dict(row)
        for row in conn.execute(
            "SELECT * FROM book_actions WHERE book_id = ? ORDER BY id", (book_id,)
        )
    ]
    return {"book": book, "insights": insights, "quotes": quotes, "actions": actions}


@router.patch("/books/{book_id}")
def patch_book(
    book_id: int, payload: BookPatch, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Правит книгу; полка ``done`` выравнивает прогресс и дату завершения."""
    row = _require_book(conn, book_id)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    for field, value in data.items():
        if value is None and field not in {"rating", "finishedAt"}:
            raise HTTPException(
                status_code=422, detail="Поле %s не может быть null" % field
            )

    updates: dict[str, Any] = {}
    for field in ("title", "author", "category", "why", "priority"):
        if field in data:
            updates[field] = data[field]
    if "rating" in data:
        updates["rating"] = data["rating"]

    total = int(data["pagesTotal"]) if "pagesTotal" in data else int(row["pages_total"] or 0)
    read = int(data["pagesRead"]) if "pagesRead" in data else int(row["pages_read"] or 0)
    shelf_explicit = "shelf" in data
    shelf = str(data["shelf"]) if shelf_explicit else str(row["shelf"])
    finished = _clean_date(data["finishedAt"]) if "finishedAt" in data else row["finished_at"]

    read, shelf, finished = _resolve_shelf(
        total=total,
        read=read,
        shelf=shelf,
        shelf_explicit=shelf_explicit,
        finished_at=finished,
        today=settings.today(),
    )
    updates.update(
        {"pages_total": total, "pages_read": read, "shelf": shelf, "finished_at": finished}
    )
    _update_book(conn, book_id, updates)
    return _book_dict(_require_book(conn, book_id))


@router.delete("/books/{book_id}")
def delete_book(book_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет книгу; мысли, цитаты и действия уходят каскадом."""
    _require_book(conn, book_id)
    with conn:
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    return {"ok": True, "id": book_id}


@router.post("/books/{book_id}/progress")
def save_progress(
    book_id: int, payload: BookProgressIn, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Сохраняет прочитанные страницы; 100 % закрывает книгу автоматически."""
    row = _require_book(conn, book_id)
    read, shelf, finished = _resolve_shelf(
        total=int(row["pages_total"] or 0),
        read=payload.pagesRead,
        shelf=str(row["shelf"]),
        shelf_explicit=False,
        finished_at=row["finished_at"],
        today=settings.today(),
    )
    _update_book(conn, book_id, {"pages_read": read, "shelf": shelf, "finished_at": finished})
    return _book_dict(_require_book(conn, book_id))


# --------------------------------------------------------------------------
# Мысли, цитаты, действия
# --------------------------------------------------------------------------


@router.post("/books/{book_id}/insights", status_code=201)
def create_insight(book_id: int, payload: InsightCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Добавляет главную мысль в конец списка (``sort`` = максимум + 1)."""
    _require_book(conn, book_id)
    row = conn.execute(
        "SELECT COALESCE(MAX(sort), 0) AS top FROM book_insights WHERE book_id = ?",
        (book_id,),
    ).fetchone()
    sort = int(row["top"] or 0) + 1
    with conn:
        cursor = conn.execute(
            "INSERT INTO book_insights (book_id, text, sort) VALUES (?, ?, ?)",
            (book_id, payload.text, sort),
        )
        insight_id = int(cursor.lastrowid or 0)
    return _insight_dict(_require_row(conn, "book_insights", insight_id, "Мысль не найдена"))


@router.patch("/book-insights/{insight_id}")
def patch_insight(
    insight_id: int, payload: InsightPatch, conn: ConnectionDep
) -> dict[str, Any]:
    """Правит текст или порядок мысли."""
    _require_row(conn, "book_insights", insight_id, "Мысль не найдена")
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    if any(value is None for value in data.values()):
        raise HTTPException(status_code=422, detail="Поля text и sort не могут быть null")
    assignments = ", ".join("%s = ?" % column for column in data)
    with conn:
        conn.execute(
            "UPDATE book_insights SET " + assignments + " WHERE id = ?",
            (*data.values(), insight_id),
        )
    return _insight_dict(_require_row(conn, "book_insights", insight_id, "Мысль не найдена"))


@router.delete("/book-insights/{insight_id}")
def delete_insight(insight_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет мысль."""
    _require_row(conn, "book_insights", insight_id, "Мысль не найдена")
    with conn:
        conn.execute("DELETE FROM book_insights WHERE id = ?", (insight_id,))
    return {"ok": True, "id": insight_id}


@router.post("/books/{book_id}/quotes", status_code=201)
def create_quote(book_id: int, payload: QuoteCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Добавляет цитату (необязательно с номером страницы)."""
    _require_book(conn, book_id)
    with conn:
        cursor = conn.execute(
            "INSERT INTO book_quotes (book_id, text, page) VALUES (?, ?, ?)",
            (book_id, payload.text, payload.page),
        )
        quote_id = int(cursor.lastrowid or 0)
    return _quote_dict(_require_row(conn, "book_quotes", quote_id, "Цитата не найдена"))


@router.delete("/book-quotes/{quote_id}")
def delete_quote(quote_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет цитату."""
    _require_row(conn, "book_quotes", quote_id, "Цитата не найдена")
    with conn:
        conn.execute("DELETE FROM book_quotes WHERE id = ?", (quote_id,))
    return {"ok": True, "id": quote_id}


@router.post("/books/{book_id}/actions", status_code=201)
def create_action(book_id: int, payload: ActionCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Добавляет действие на внедрение (задачу ещё не создаёт)."""
    _require_book(conn, book_id)
    with conn:
        cursor = conn.execute(
            "INSERT INTO book_actions (book_id, text) VALUES (?, ?)",
            (book_id, payload.text),
        )
        action_id = int(cursor.lastrowid or 0)
    return _action_dict(_require_row(conn, "book_actions", action_id, "Действие не найдено"))


@router.post("/book-actions/{action_id}/to-task")
def action_to_task(
    action_id: int, conn: ConnectionDep, payload: ToTaskIn | None = None
) -> dict[str, Any]:
    """Превращает действие в задачу и связывает её с книгой.

    Тело запроса необязательно: ``{"dueDate": "YYYY-MM-DD"}`` задаёт дедлайн.
    Задача создаётся в списке «личное» с ресурсом ``important``; повторный
    вызов для уже связанного действия возвращает ту же задачу (без дублей).
    """
    row = conn.execute(
        "SELECT a.*, b.title AS book_title FROM book_actions AS a "
        "JOIN books AS b ON b.id = a.book_id WHERE a.id = ?",
        (action_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Действие не найдено")

    existing = row["task_id"]
    if existing is not None:
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (existing,)).fetchone()
        if task_row is not None:
            return {"task": _task_dict(task_row), "action": _action_dict(row)}

    due = _clean_date(payload.dueDate) if payload is not None else None
    notes = "Действие по книге «%s»" % str(row["book_title"])
    with conn:
        cursor = conn.execute(
            "INSERT INTO tasks (title, list_kind, resource, due_date, notes) "
            "VALUES (?, 'personal', 'important', ?, ?)",
            (str(row["text"]), due, notes),
        )
        task_id = int(cursor.lastrowid or 0)
        conn.execute("UPDATE book_actions SET task_id = ? WHERE id = ?", (task_id, action_id))

    task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    action_row = conn.execute("SELECT * FROM book_actions WHERE id = ?", (action_id,)).fetchone()
    return {"task": _task_dict(task_row), "action": _action_dict(action_row)}


# --------------------------------------------------------------------------
# Обучение
# --------------------------------------------------------------------------


@router.get("/learning")
def list_learning(conn: ConnectionDep) -> dict[str, Any]:
    """Курсы с прогрессом по урокам — вкладка «Обучение» в Библиотеке."""
    rows = conn.execute(_COURSE_SELECT + " ORDER BY c.created DESC, c.id DESC").fetchall()
    return {"courses": [_course_dict(row) for row in rows]}


@router.patch("/courses/{course_id}")
def patch_course(
    course_id: int, payload: CoursePatch, conn: ConnectionDep
) -> dict[str, Any]:
    """Метаданные обучения: организатор, дедлайн, проект/цель, конспект."""
    row = conn.execute(_COURSE_SELECT + " WHERE c.id = ?", (course_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Курс не найден")
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")

    updates: dict[str, Any] = {}
    if "organizer" in data:
        updates["organizer"] = data["organizer"] or ""
    if "notes" in data:
        updates["notes"] = data["notes"] or ""
    if "deadline" in data:
        updates["deadline"] = _clean_date(data["deadline"])
    if "projectId" in data:
        project_id = data["projectId"]
        if project_id is not None and (
            conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
            is None
        ):
            raise HTTPException(status_code=422, detail="Проект не найден")
        updates["project_id"] = project_id
    if "goalId" in data:
        goal_id = data["goalId"]
        if goal_id is not None and (
            conn.execute("SELECT 1 FROM goals WHERE id = ?", (goal_id,)).fetchone() is None
        ):
            raise HTTPException(status_code=422, detail="Цель не найдена")
        updates["goal_id"] = goal_id

    assignments = ", ".join("%s = ?" % column for column in updates)
    with conn:
        conn.execute(
            "UPDATE courses SET " + assignments + " WHERE id = ?",
            (*updates.values(), course_id),
        )
    return _course_dict(conn.execute(_COURSE_SELECT + " WHERE c.id = ?", (course_id,)).fetchone())
