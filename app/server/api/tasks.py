"""Дела: задачи с повторениями, инбокс и проекты (§12.2).

Маршруты:

* ``GET/POST /api/tasks``, ``PATCH/DELETE /api/tasks/{id}``,
  ``POST /api/tasks/{id}/complete``, ``POST /api/tasks/{id}/reschedule``;
* ``GET/POST/DELETE /api/inbox``, ``POST /api/inbox/{id}/convert``;
* ``GET/POST /api/projects``, ``GET/PATCH/DELETE /api/projects/{id}``,
  ``/api/projects/{id}/steps``, ``/api/project-steps/{id}``,
  ``/api/projects/{id}/notes``, ``/api/project-notes/{id}``.

JSON — camelCase, ошибки — ``{"detail": "..."}`` с кодом 4xx.

Решения, которые видно снаружи:

* **Фильтр дня** (``view=day`` или ``date``) берёт задачи, у которых ``due_date``
  равен дню **или** событие начинается в этот день (``start_at``). Неделя —
  период ``from``..``to`` (по умолчанию текущая неделя Пн–Вс), календарь —
  переданный период или текущий месяц, канбан — все задачи без ограничения
  по датам. ``counts`` считаются по той же выборке, что и ``items``, но без
  фильтров ``resource``/``status``: ``must/important/low`` — открытые задачи по
  ресурсу, ``done`` — закрытые, ``total`` — без отменённых (``cancelled``).
* **Повторения**: ``daily``, ``weekly:1,3,5`` (дни ISO 1=Пн..7=Вс),
  ``monthly:15``. ``complete`` создаёт следующее вхождение (дата — ближайшее
  после текущей; для 31-го числа в коротком месяце — последний день месяца) и
  сдвигает ``startAt``/``endAt`` на столько же дней. Невалидное правило не
  ломает ответ: задача закрывается, ``next`` = ``null``. Повторное закрытие уже
  закрытой задачи не плодит дубликаты.
* **Инбокс** разбирается конвертацией: ``task`` (рабочая задача), ``personal``
  (личное дело — та же задача с ``listKind='personal'``), ``project``, ``book``
  или ``delete``. После конвертации элемент получает ``status='processed'`` и
  ``convertedType/convertedId``; повторная конвертация — 409.
* **Проект с задачами не удаляется** (409): сначала закройте или перенесите
  задачи. Шаги и заметки уходят каскадом вместе с проектом.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.server.api.deps import ConnectionDep, SettingsDep
from app.server.settings import Settings

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["tasks"])

Resource = Literal["must", "important", "low"]
ListKind = Literal["work", "personal"]
TaskStatus = Literal["todo", "done", "cancelled"]
StatusFilter = Literal["todo", "done", "cancelled", "all"]
ProjectStatus = Literal["idea", "active", "paused", "done"]
View = Literal["day", "week", "kanban", "calendar"]
NoteKind = Literal["note", "link"]
ConvertTo = Literal["task", "project", "personal", "book", "delete"]

RESOURCES: tuple[str, ...] = ("must", "important", "low")
DEFAULT_COLOR = "#A18BB7"

_TASK_SELECT = (
    "SELECT t.*, s.name AS sphere_name, p.name AS project_name "
    "FROM tasks AS t "
    "LEFT JOIN spheres AS s ON s.id = t.sphere_id "
    "LEFT JOIN projects AS p ON p.id = t.project_id"
)

_TASK_ORDER = (
    " ORDER BY COALESCE(t.due_date, substr(t.start_at, 1, 10), '9999-12-31'), "
    "COALESCE(t.start_at, ''), "
    "CASE t.resource WHEN 'must' THEN 0 WHEN 'important' THEN 1 ELSE 2 END, t.id"
)

_PROJECT_SELECT = (
    "SELECT p.*, s.name AS sphere_name, "
    " (SELECT count(*) FROM project_steps AS st WHERE st.project_id = p.id) AS steps_total, "
    " (SELECT count(*) FROM project_steps AS st "
    "   WHERE st.project_id = p.id AND st.done = 1) AS steps_done, "
    " (SELECT count(*) FROM tasks AS t "
    "   WHERE t.project_id = p.id AND t.status <> 'cancelled') AS tasks_total, "
    " (SELECT count(*) FROM tasks AS t "
    "   WHERE t.project_id = p.id AND t.status = 'done') AS tasks_done "
    "FROM projects AS p LEFT JOIN spheres AS s ON s.id = p.sphere_id"
)


# --------------------------------------------------------------------------
# Мелкие помощники
# --------------------------------------------------------------------------


def _non_empty(value: str, message: str = "Текст не может быть пустым") -> str:
    text = value.strip()
    if not text:
        raise ValueError(message)
    return text


def _iso(day: date) -> str:
    return day.isoformat()


def _parse_day(value: str | None, *, field: str = "date") -> date:
    text = (value or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="%s: ожидается дата YYYY-MM-DD" % field)
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="%s: ожидается дата YYYY-MM-DD" % field
        ) from exc


def _parse_optional_day(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none"}:
        return None
    return _iso(_parse_day(text, field=field))


def _parse_moment(value: str | None, *, field: str) -> str | None:
    """Дата-время события: ``YYYY-MM-DD`` или ISO 8601; хранится как прислано."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none"}:
        return None
    _parse_day(text, field=field)  # проверяем дату в начале строки
    return text


def _day_part(value: str | None) -> str:
    return str(value or "")[:10]


def _shift_moment(value: str | None, delta_days: int) -> str | None:
    """Сдвигает дату в начале ISO-строки, сохраняя время и часовой пояс."""
    text = str(value or "")
    if not text:
        return None
    try:
        moved = date.fromisoformat(text[:10]) + timedelta(days=delta_days)
    except ValueError:  # pragma: no cover - значение уже провалидировано
        return text
    return _iso(moved) + text[10:]


def _tags_text(value: Any) -> str:
    """``"дом, работа"`` и ``["дом", "работа"]`` → ``"дом,работа"``."""
    if value is None:
        return ""
    parts: list[str] = []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        parts = [str(value)]
    cleaned = [part.strip() for part in parts if part.strip()]
    return ",".join(cleaned)


def _tags_list(value: Any) -> list[str]:
    return [part for part in str(value or "").split(",") if part]


def _matches(needle: str, *fields: Any) -> bool:
    lowered = needle.casefold()
    return any(lowered in str(field or "").casefold() for field in fields)


# --------------------------------------------------------------------------
# Модели задач
# --------------------------------------------------------------------------


class TaskCreate(BaseModel):
    """Тело ``POST /api/tasks``."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=300)
    dueDate: str | None = None  # noqa: N815
    startAt: str | None = None  # noqa: N815
    endAt: str | None = None  # noqa: N815
    resource: Resource = "important"
    sphereId: int | None = Field(None, ge=1)  # noqa: N815
    projectId: int | None = Field(None, ge=1)  # noqa: N815
    goalId: int | None = Field(None, ge=1)  # noqa: N815
    listKind: ListKind = "work"  # noqa: N815
    tags: str | list[str] | None = None
    notes: str = Field("", max_length=4000)
    recurrence: str = Field("", max_length=60)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Название задачи не может быть пустым")


class TaskPatch(BaseModel):
    """Тело ``PATCH /api/tasks/{id}``: любое подмножество полей."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(None, min_length=1, max_length=300)
    dueDate: str | None = None  # noqa: N815
    startAt: str | None = None  # noqa: N815
    endAt: str | None = None  # noqa: N815
    resource: Resource | None = None
    sphereId: int | None = Field(None, ge=1)  # noqa: N815
    projectId: int | None = Field(None, ge=1)  # noqa: N815
    goalId: int | None = Field(None, ge=1)  # noqa: N815
    listKind: ListKind | None = None  # noqa: N815
    tags: str | list[str] | None = None
    notes: str | None = Field(None, max_length=4000)
    recurrence: str | None = Field(None, max_length=60)
    status: TaskStatus | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Название задачи не может быть пустым")


class CompleteIn(BaseModel):
    """Тело ``POST /api/tasks/{id}/complete``; по умолчанию — закрыть."""

    model_config = ConfigDict(extra="ignore")

    done: bool = True


class RescheduleIn(BaseModel):
    """Тело ``POST /api/tasks/{id}/reschedule``."""

    model_config = ConfigDict(extra="ignore")

    dueDate: str  # noqa: N815


class InboxCreate(BaseModel):
    """Тело ``POST /api/inbox``."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Текст не может быть пустым")


class InboxConvert(BaseModel):
    """Тело ``POST /api/inbox/{id}/convert``."""

    model_config = ConfigDict(extra="ignore")

    to: ConvertTo
    payload: dict[str, Any] | None = None


class InboxBookIn(BaseModel):
    """Дополнительные поля книги при конвертации инбокса (``to='book'``)."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field("", max_length=300)
    author: str = Field("", max_length=200)
    category: Literal["pro", "fiction", "nonfiction"] = "nonfiction"
    pagesTotal: int = Field(0, ge=0, le=1_000_000)  # noqa: N815
    shelf: Literal["reading", "queue", "done", "dropped"] = "queue"
    why: str = Field("", max_length=4000)
    priority: int = Field(0, ge=0, le=1000)


# --------------------------------------------------------------------------
# Модели проектов
# --------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    """Тело ``POST /api/projects``."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    sphereId: int | None = Field(None, ge=1)  # noqa: N815
    goalId: int | None = Field(None, ge=1)  # noqa: N815
    deadline: str | None = None
    status: ProjectStatus = "idea"
    outcome: str = Field("", max_length=2000)
    description: str = Field("", max_length=4000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Название проекта не может быть пустым")


class ProjectPatch(BaseModel):
    """Тело ``PATCH /api/projects/{id}``."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(None, min_length=1, max_length=200)
    sphereId: int | None = Field(None, ge=1)  # noqa: N815
    goalId: int | None = Field(None, ge=1)  # noqa: N815
    deadline: str | None = None
    status: ProjectStatus | None = None
    outcome: str | None = Field(None, max_length=2000)
    description: str | None = Field(None, max_length=4000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Название проекта не может быть пустым")


class StepCreate(BaseModel):
    """Тело ``POST /api/projects/{id}/steps``."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=300)

    @field_validator("title")
    @classmethod
    def _step_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Шаг не может быть пустым")


class StepPatch(BaseModel):
    """Тело ``PATCH /api/project-steps/{id}``."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(None, min_length=1, max_length=300)
    done: bool | None = None

    @field_validator("title")
    @classmethod
    def _step_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Шаг не может быть пустым")


class NoteCreate(BaseModel):
    """Тело ``POST /api/projects/{id}/notes``."""

    model_config = ConfigDict(extra="ignore")

    kind: NoteKind = "note"
    title: str = Field("", max_length=300)
    body: str = Field("", max_length=8000)
    url: str = Field("", max_length=2000)


# --------------------------------------------------------------------------
# Строки БД → JSON
# --------------------------------------------------------------------------


def _task_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "sphereId": row["sphere_id"],
        "sphereName": row["sphere_name"] if "sphere_name" in row.keys() else None,
        "projectId": row["project_id"],
        "projectName": row["project_name"] if "project_name" in row.keys() else None,
        "goalId": row["goal_id"],
        "listKind": str(row["list_kind"]),
        "tags": _tags_list(row["tags"]),
        "dueDate": row["due_date"],
        "startAt": row["start_at"],
        "endAt": row["end_at"],
        "resource": str(row["resource"]),
        "status": str(row["status"]),
        "recurrence": str(row["recurrence"] or ""),
        "notes": str(row["notes"] or ""),
        "doneAt": row["done_at"],
        "created": str(row["created"] or ""),
    }


def _project_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "sphereId": row["sphere_id"],
        "sphereName": row["sphere_name"] if "sphere_name" in row.keys() else None,
        "goalId": row["goal_id"],
        "deadline": row["deadline"],
        "status": str(row["status"]),
        "outcome": str(row["outcome"] or ""),
        "description": str(row["description"] or ""),
        "created": str(row["created"] or ""),
        "updated": row["updated"],
        "stepsTotal": int(row["steps_total"] or 0) if "steps_total" in row.keys() else 0,
        "stepsDone": int(row["steps_done"] or 0) if "steps_done" in row.keys() else 0,
        "tasksTotal": int(row["tasks_total"] or 0) if "tasks_total" in row.keys() else 0,
        "tasksDone": int(row["tasks_done"] or 0) if "tasks_done" in row.keys() else 0,
    }


def _step_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "projectId": int(row["project_id"]),
        "title": str(row["title"]),
        "done": bool(row["done"]),
        "sort": int(row["sort"] or 0),
    }


def _note_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "projectId": int(row["project_id"]),
        "kind": str(row["kind"]),
        "title": str(row["title"] or ""),
        "body": str(row["body"] or ""),
        "url": str(row["url"] or ""),
        "created": str(row["created"] or ""),
    }


def _inbox_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "text": str(row["text"]),
        "status": str(row["status"]),
        "convertedType": str(row["converted_type"] or ""),
        "convertedId": row["converted_id"],
        "created": str(row["created"] or ""),
    }


def _task_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    """Счётчики ресурсов и закрытых; отменённые и ``done`` в ресурсы не входят."""
    counts = {"must": 0, "important": 0, "low": 0, "done": 0, "total": 0}
    for item in items:
        status = item["status"]
        if status == "cancelled":
            continue
        counts["total"] += 1
        if status == "done":
            counts["done"] += 1
        elif item["resource"] in counts:
            counts[item["resource"]] += 1
    return counts


# --------------------------------------------------------------------------
# Проверки ссылок и загрузка задач
# --------------------------------------------------------------------------


def _require_task(conn: Any, task_id: int) -> Any:
    row = conn.execute(_TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return row


def _require_project(conn: Any, project_id: int) -> Any:
    row = conn.execute(_PROJECT_SELECT + " WHERE p.id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return row


def _check_refs(
    conn: Any,
    *,
    sphere_id: int | None,
    project_id: int | None,
    goal_id: int | None,
) -> None:
    if sphere_id is not None and conn.execute(
        "SELECT 1 FROM spheres WHERE id = ?", (sphere_id,)
    ).fetchone() is None:
        raise HTTPException(status_code=422, detail="Сфера не найдена")
    if project_id is not None and conn.execute(
        "SELECT 1 FROM projects WHERE id = ?", (project_id,)
    ).fetchone() is None:
        raise HTTPException(status_code=422, detail="Проект не найден")
    if goal_id is not None and conn.execute(
        "SELECT 1 FROM goals WHERE id = ?", (goal_id,)
    ).fetchone() is None:
        raise HTTPException(status_code=422, detail="Цель не найдена")


def _now(settings: Settings) -> str:
    return settings.now().replace(microsecond=0).isoformat()


def _insert_task(conn: Any, payload: TaskCreate, settings: Settings) -> dict[str, Any]:
    """Общая вставка задачи: и ``POST /api/tasks``, и конвертация инбокса."""
    _check_refs(
        conn, sphere_id=payload.sphereId, project_id=payload.projectId, goal_id=payload.goalId
    )
    with conn:
        cursor = conn.execute(
            "INSERT INTO tasks "
            "(title, sphere_id, project_id, goal_id, list_kind, tags, due_date, "
            " start_at, end_at, resource, status, recurrence, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'todo', ?, ?)",
            (
                payload.title,
                payload.sphereId,
                payload.projectId,
                payload.goalId,
                payload.listKind,
                _tags_text(payload.tags),
                _parse_optional_day(payload.dueDate, field="dueDate"),
                _parse_moment(payload.startAt, field="startAt"),
                _parse_moment(payload.endAt, field="endAt"),
                payload.resource,
                payload.recurrence.strip(),
                payload.notes,
            ),
        )
        task_id = int(cursor.lastrowid or 0)
    return _task_dict(_require_task(conn, task_id))


# --------------------------------------------------------------------------
# Задачи: список
# --------------------------------------------------------------------------


def _resolve_range(
    view: str,
    date_value: str | None,
    from_value: str | None,
    to_value: str | None,
    settings: Settings,
) -> tuple[str | None, str | None, str | None]:
    """Возвращает ``(day, from, to)``: день важнее периода."""
    today = settings.now().date()
    if date_value:
        return _iso(_parse_day(date_value, field="date")), None, None
    if from_value is None and to_value is None:
        if view == "day":
            return _iso(today), None, None
        if view == "week":
            start = today - timedelta(days=today.weekday())
            return None, _iso(start), _iso(start + timedelta(days=6))
        if view == "calendar":
            start = today.replace(day=1)
            last = monthrange(today.year, today.month)[1]
            return None, _iso(start), _iso(today.replace(day=last))
        return None, None, None
    if from_value:
        start = _parse_day(from_value, field="from")
        end = _parse_day(to_value, field="to") if to_value else start + timedelta(days=6)
    else:
        end = _parse_day(to_value, field="to")
        start = end - timedelta(days=6)
    if start > end:
        raise HTTPException(status_code=422, detail="from позже to")
    return None, _iso(start), _iso(end)


@router.get("/tasks")
def list_tasks(
    conn: ConnectionDep,
    settings: SettingsDep,
    date: str | None = Query(None, max_length=10, description="День YYYY-MM-DD"),
    view: View = Query("day", description="day | week | kanban | calendar"),
    from_: str | None = Query(None, alias="from", max_length=10),
    to: str | None = Query(None, max_length=10),
    sphereId: int | None = Query(None, ge=1),  # noqa: N815
    projectId: int | None = Query(None, ge=1),  # noqa: N815
    resource: Resource | None = Query(None),
    status: StatusFilter | None = Query(None),
    listKind: ListKind | None = Query(None),  # noqa: N815
    q: str | None = Query(None, max_length=200),
) -> dict[str, Any]:
    """Список задач с фильтрами и счётчиками ``must/important/low/done/total``."""
    day, start, end = _resolve_range(view, date, from_, to, settings)

    sql = _TASK_SELECT
    conds: list[str] = []
    params: list[Any] = []
    if day:
        conds.append("(t.due_date = ? OR substr(COALESCE(t.start_at, ''), 1, 10) = ?)")
        params.extend([day, day])
    elif start and end:
        conds.append(
            "((t.due_date BETWEEN ? AND ?) "
            " OR (substr(COALESCE(t.start_at, ''), 1, 10) BETWEEN ? AND ?))"
        )
        params.extend([start, end, start, end])
    if sphereId is not None:
        conds.append("t.sphere_id = ?")
        params.append(sphereId)
    if projectId is not None:
        conds.append("t.project_id = ?")
        params.append(projectId)
    if listKind is not None:
        conds.append("t.list_kind = ?")
        params.append(listKind)
    # По умолчанию отменённые скрыты; явный статус фильтрует items после
    # подсчёта счётчиков, чтобы вкладки видели полную картину.
    if status is None:
        conds.append("t.status <> 'cancelled'")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += _TASK_ORDER

    items = [_task_dict(row) for row in conn.execute(sql, params).fetchall()]
    needle = (q or "").strip()
    if needle:
        items = [
            item
            for item in items
            if _matches(needle, item["title"], item["notes"], ",".join(item["tags"]))
        ]

    counts = _task_counts(items)
    if resource is not None:
        items = [item for item in items if item["resource"] == resource]
    if status is not None and status != "all":
        items = [item for item in items if item["status"] == status]

    return {
        "items": items,
        "counts": counts,
        "view": view,
        "date": day,
        "from": start,
        "to": end,
    }


@router.post("/tasks", status_code=201)
def create_task(
    payload: TaskCreate, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Добавляет задачу; ``tags`` принимается строкой или массивом."""
    return _insert_task(conn, payload, settings)


@router.patch("/tasks/{task_id}")
def patch_task(
    task_id: int, payload: TaskPatch, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Правит задачу; ``null`` очищает дедлайн, время и ссылки."""
    row = _require_task(conn, task_id)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    for field in ("title", "resource", "listKind", "status", "recurrence"):
        if field in data and data[field] is None:
            raise HTTPException(status_code=422, detail="Поле %s не может быть null" % field)

    updates: dict[str, Any] = {}
    if "title" in data:
        updates["title"] = data["title"]
    if "resource" in data:
        updates["resource"] = data["resource"]
    if "listKind" in data:
        updates["list_kind"] = data["listKind"]
    if "notes" in data:
        updates["notes"] = data["notes"] or ""
    if "recurrence" in data:
        updates["recurrence"] = str(data["recurrence"] or "").strip()
    if "tags" in data:
        updates["tags"] = _tags_text(data["tags"])
    if "dueDate" in data:
        updates["due_date"] = _parse_optional_day(data["dueDate"], field="dueDate")
    if "startAt" in data:
        updates["start_at"] = _parse_moment(data["startAt"], field="startAt")
    if "endAt" in data:
        updates["end_at"] = _parse_moment(data["endAt"], field="endAt")
    if "sphereId" in data:
        updates["sphere_id"] = data["sphereId"]
    if "projectId" in data:
        updates["project_id"] = data["projectId"]
    if "goalId" in data:
        updates["goal_id"] = data["goalId"]
    if "status" in data:
        updates["status"] = data["status"]
        if data["status"] == "done":
            updates["done_at"] = row["done_at"] or _now(settings)
        else:
            updates["done_at"] = None

    _check_refs(
        conn,
        sphere_id=updates.get("sphere_id", row["sphere_id"]),
        project_id=updates.get("project_id", row["project_id"]),
        goal_id=updates.get("goal_id", row["goal_id"]),
    )

    assignments = ", ".join("%s = ?" % column for column in updates)
    with conn:
        conn.execute(
            "UPDATE tasks SET " + assignments + " WHERE id = ?",
            (*updates.values(), task_id),
        )
    return _task_dict(_require_task(conn, task_id))


# --------------------------------------------------------------------------
# Задачи: завершение, повторения, перенос, отмена
# --------------------------------------------------------------------------


def _next_due(rule: str, base: date) -> date | None:
    """Следующая дата по правилу повторения; ``None`` — правило не понято."""
    text = (rule or "").strip().lower()
    if not text:
        return None
    if text == "daily":
        return base + timedelta(days=1)
    if text.startswith("weekly:"):
        raw = text.split(":", 1)[1]
        try:
            days = sorted({int(part) for part in raw.split(",") if part.strip()})
        except ValueError:
            return None
        days = [day for day in days if 1 <= day <= 7]
        if not days:
            return None
        for offset in range(1, 8):
            candidate = base + timedelta(days=offset)
            if candidate.isoweekday() in days:
                return candidate
        return None
    if text.startswith("monthly:"):
        raw = text.split(":", 1)[1]
        try:
            anchor = int(raw.strip())
        except ValueError:
            return None
        if not 1 <= anchor <= 31:
            return None
        year, month = base.year, base.month + 1
        if month > 12:
            month = 1
            year += 1
        last_day = monthrange(year, month)[1]
        return date(year, month, min(anchor, last_day))
    return None


def _task_base_date(row: Any, settings: Settings) -> date:
    for value in (row["due_date"], _day_part(row["start_at"])):
        text = str(value or "")
        if len(text) >= 10:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                continue
    return settings.now().date()


def _insert_next(
    conn: Any, row: Any, next_day: date, base: date, settings: Settings
) -> dict[str, Any]:
    """Копия задачи на следующее вхождение; времена событий сдвигаются."""
    delta = (next_day - base).days
    with conn:
        cursor = conn.execute(
            "INSERT INTO tasks "
            "(title, sphere_id, project_id, goal_id, list_kind, tags, due_date, "
            " start_at, end_at, resource, status, recurrence, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'todo', ?, ?)",
            (
                str(row["title"]),
                row["sphere_id"],
                row["project_id"],
                row["goal_id"],
                str(row["list_kind"]),
                str(row["tags"] or ""),
                _iso(next_day),
                _shift_moment(row["start_at"], delta),
                _shift_moment(row["end_at"], delta),
                str(row["resource"]),
                str(row["recurrence"] or ""),
                str(row["notes"] or ""),
            ),
        )
        task_id = int(cursor.lastrowid or 0)
    return _task_dict(_require_task(conn, task_id))


@router.post("/tasks/{task_id}/complete")
def complete_task(
    task_id: int, payload: CompleteIn, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Закрывает/открывает задачу; у повторяющейся создаёт следующее вхождение."""
    row = _require_task(conn, task_id)
    next_task: dict[str, Any] | None = None

    if not payload.done:
        with conn:
            conn.execute(
                "UPDATE tasks SET status = 'todo', done_at = NULL WHERE id = ?", (task_id,)
            )
        return {"task": _task_dict(_require_task(conn, task_id)), "next": None}

    if str(row["status"]) != "done":
        with conn:
            conn.execute(
                "UPDATE tasks SET status = 'done', done_at = ? WHERE id = ?",
                (_now(settings), task_id),
            )
        base = _task_base_date(row, settings)
        next_day = _next_due(str(row["recurrence"] or ""), base)
        if next_day is not None:
            next_task = _insert_next(conn, row, next_day, base, settings)

    return {"task": _task_dict(_require_task(conn, task_id)), "next": next_task}


@router.post("/tasks/{task_id}/reschedule")
def reschedule_task(
    task_id: int, payload: RescheduleIn, conn: ConnectionDep
) -> dict[str, Any]:
    """Переносит задачу на новую дату (``dueDate`` обязателен)."""
    _require_task(conn, task_id)
    due = _iso(_parse_day(payload.dueDate, field="dueDate"))
    with conn:
        conn.execute("UPDATE tasks SET due_date = ? WHERE id = ?", (due, task_id))
    return _task_dict(_require_task(conn, task_id))


@router.delete("/tasks/{task_id}")
def delete_task(task_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Отменяет задачу (``status='cancelled'``), не удаляя её из истории."""
    _require_task(conn, task_id)
    with conn:
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', done_at = NULL WHERE id = ?", (task_id,)
        )
    return _task_dict(_require_task(conn, task_id))


# --------------------------------------------------------------------------
# Инбокс
# --------------------------------------------------------------------------


@router.get("/inbox")
def list_inbox(
    conn: ConnectionDep,
    status: Literal["new", "processed", "all"] = Query("new"),
) -> dict[str, Any]:
    """Инбокс: по умолчанию неразобранные элементы, свежие сверху."""
    counts_row = conn.execute(
        "SELECT COALESCE(SUM(status = 'new'), 0) AS new_count, "
        " COALESCE(SUM(status = 'processed'), 0) AS processed_count FROM inbox_items"
    ).fetchone()
    sql = "SELECT * FROM inbox_items"
    params: list[Any] = []
    if status != "all":
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created DESC, id DESC"
    items = [_inbox_dict(row) for row in conn.execute(sql, params).fetchall()]
    return {
        "items": items,
        "status": status,
        "counts": {
            "new": int(counts_row["new_count"] or 0) if counts_row else 0,
            "processed": int(counts_row["processed_count"] or 0) if counts_row else 0,
            "total": int(counts_row["new_count"] or 0)
            + int(counts_row["processed_count"] or 0)
            if counts_row
            else 0,
        },
    }


@router.post("/inbox", status_code=201)
def create_inbox(payload: InboxCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Парковка мысли: ``POST /api/inbox {text}``."""
    with conn:
        cursor = conn.execute("INSERT INTO inbox_items (text) VALUES (?)", (payload.text,))
        item_id = int(cursor.lastrowid or 0)
    row = conn.execute("SELECT * FROM inbox_items WHERE id = ?", (item_id,)).fetchone()
    return _inbox_dict(row)


def _require_inbox(conn: Any, item_id: int) -> Any:
    row = conn.execute("SELECT * FROM inbox_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Элемент инбокса не найден")
    return row


def _mark_inbox(conn: Any, item_id: int, converted_type: str, converted_id: int) -> dict[str, Any]:
    with conn:
        conn.execute(
            "UPDATE inbox_items SET status = 'processed', converted_type = ?, "
            "converted_id = ? WHERE id = ?",
            (converted_type, converted_id, item_id),
        )
    return _inbox_dict(_require_inbox(conn, item_id))


def _convert_task(
    conn: Any, text: str, payload: dict[str, Any], settings: Settings, personal: bool
) -> dict[str, Any]:
    data = dict(payload)
    data.setdefault("title", text)
    if personal:
        data.setdefault("listKind", "personal")
    else:
        data.setdefault("listKind", "work")
    try:
        model = TaskCreate(**data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="payload: %s" % exc.errors()[0]["msg"]) from exc
    return _insert_task(conn, model, settings)


def _convert_project(conn: Any, text: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    data.setdefault("name", text)
    try:
        model = ProjectCreate(**data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="payload: %s" % exc.errors()[0]["msg"]) from exc
    return _insert_project(conn, model)


def _convert_book(
    conn: Any, text: str, payload: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    data = dict(payload)
    data.setdefault("title", text)
    try:
        model = InboxBookIn(**data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="payload: %s" % exc.errors()[0]["msg"]) from exc
    title = model.title or text
    read = model.pagesTotal if model.shelf == "done" else 0
    finished = settings.today() if model.shelf == "done" else None
    with conn:
        cursor = conn.execute(
            "INSERT INTO books "
            "(title, author, category, pages_total, pages_read, shelf, why, priority, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                model.author,
                model.category,
                model.pagesTotal,
                read,
                model.shelf,
                model.why,
                model.priority,
                finished,
            ),
        )
        book_id = int(cursor.lastrowid or 0)
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "author": str(row["author"] or ""),
        "category": str(row["category"]),
        "pagesTotal": int(row["pages_total"] or 0),
        "pagesRead": int(row["pages_read"] or 0),
        "shelf": str(row["shelf"]),
        "rating": row["rating"],
        "why": str(row["why"] or ""),
        "priority": int(row["priority"] or 0),
        "finishedAt": row["finished_at"],
        "created": str(row["created"] or ""),
    }


@router.post("/inbox/{item_id}/convert")
def convert_inbox(
    item_id: int, payload: InboxConvert, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Разбирает инбокс: задача, личное, проект, книга или удаление."""
    row = _require_inbox(conn, item_id)
    if str(row["status"]) == "processed":
        raise HTTPException(status_code=409, detail="Элемент инбокса уже разобран")
    text = str(row["text"])
    extra = payload.payload or {}

    if payload.to == "delete":
        with conn:
            conn.execute("DELETE FROM inbox_items WHERE id = ?", (item_id,))
        return {"ok": True, "id": item_id}

    if payload.to in ("task", "personal"):
        task = _convert_task(conn, text, extra, settings, payload.to == "personal")
        item = _mark_inbox(conn, item_id, "task", int(task["id"]))
        return {"item": item, "task": task}

    if payload.to == "project":
        project = _convert_project(conn, text, extra)
        item = _mark_inbox(conn, item_id, "project", int(project["id"]))
        return {"item": item, "project": project}

    book = _convert_book(conn, text, extra, settings)
    item = _mark_inbox(conn, item_id, "book", int(book["id"]))
    return {"item": item, "book": book}


@router.delete("/inbox/{item_id}")
def delete_inbox(item_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет элемент инбокса без следа."""
    _require_inbox(conn, item_id)
    with conn:
        conn.execute("DELETE FROM inbox_items WHERE id = ?", (item_id,))
    return {"ok": True, "id": item_id}


# --------------------------------------------------------------------------
# Проекты
# --------------------------------------------------------------------------


def _insert_project(conn: Any, payload: ProjectCreate) -> dict[str, Any]:
    _check_refs(conn, sphere_id=payload.sphereId, project_id=None, goal_id=payload.goalId)
    deadline = _parse_optional_day(payload.deadline, field="deadline")
    with conn:
        cursor = conn.execute(
            "INSERT INTO projects "
            "(name, sphere_id, goal_id, deadline, status, outcome, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payload.name,
                payload.sphereId,
                payload.goalId,
                deadline,
                payload.status,
                payload.outcome,
                payload.description,
            ),
        )
        project_id = int(cursor.lastrowid or 0)
    return _project_dict(_require_project(conn, project_id))


@router.get("/projects")
def list_projects(
    conn: ConnectionDep,
    status: ProjectStatus | None = Query(None),
    sphereId: int | None = Query(None, ge=1),  # noqa: N815
    q: str | None = Query(None, max_length=200),
) -> dict[str, Any]:
    """Список проектов с прогрессом по шагам и задачам."""
    sql = _PROJECT_SELECT
    conds: list[str] = []
    params: list[Any] = []
    if sphereId is not None:
        conds.append("p.sphere_id = ?")
        params.append(sphereId)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY p.created DESC, p.id DESC"
    items = [_project_dict(row) for row in conn.execute(sql, params).fetchall()]

    needle = (q or "").strip()
    if needle:
        items = [
            item
            for item in items
            if _matches(needle, item["name"], item["description"], item["outcome"])
        ]

    counts = {"idea": 0, "active": 0, "paused": 0, "done": 0, "total": 0}
    for item in items:
        counts["total"] += 1
        if item["status"] in counts:
            counts[item["status"]] += 1

    if status is not None:
        items = [item for item in items if item["status"] == status]
    return {"items": items, "counts": counts}


@router.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Создаёт проект."""
    return _insert_project(conn, payload)


@router.get("/projects/{project_id}")
def get_project(project_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Проект + шаги, заметки и задачи со счётчиками."""
    project = _project_dict(_require_project(conn, project_id))
    steps = [
        _step_dict(row)
        for row in conn.execute(
            "SELECT * FROM project_steps WHERE project_id = ? ORDER BY sort, id", (project_id,)
        )
    ]
    notes = [
        _note_dict(row)
        for row in conn.execute(
            "SELECT * FROM project_notes WHERE project_id = ? ORDER BY created DESC, id DESC",
            (project_id,),
        )
    ]
    tasks = [
        _task_dict(row)
        for row in conn.execute(
            _TASK_SELECT + " WHERE t.project_id = ? AND t.status <> 'cancelled'" + _TASK_ORDER,
            (project_id,),
        )
    ]
    return {
        "project": project,
        "steps": steps,
        "notes": notes,
        "tasks": {"items": tasks, "counts": _task_counts(tasks)},
    }


@router.patch("/projects/{project_id}")
def patch_project(
    project_id: int, payload: ProjectPatch, conn: ConnectionDep
) -> dict[str, Any]:
    """Правит проект; ``null`` снимает дедлайн, сферу и цель."""
    row = _require_project(conn, project_id)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    for field in ("name", "status", "outcome", "description"):
        if field in data and data[field] is None:
            raise HTTPException(status_code=422, detail="Поле %s не может быть null" % field)

    updates: dict[str, Any] = {}
    for field in ("name", "status", "outcome", "description"):
        if field in data:
            updates[field] = data[field]
    if "deadline" in data:
        updates["deadline"] = _parse_optional_day(data["deadline"], field="deadline")
    if "sphereId" in data:
        updates["sphere_id"] = data["sphereId"]
    if "goalId" in data:
        updates["goal_id"] = data["goalId"]
    _check_refs(
        conn,
        sphere_id=updates.get("sphere_id", row["sphere_id"]),
        project_id=None,
        goal_id=updates.get("goal_id", row["goal_id"]),
    )

    assignments = ", ".join("%s = ?" % column for column in updates)
    with conn:
        conn.execute(
            "UPDATE projects SET " + assignments
            + ", updated = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (*updates.values(), project_id),
        )
    return _project_dict(_require_project(conn, project_id))


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет проект без задач; если задачи есть — 409."""
    _require_project(conn, project_id)
    used = conn.execute(
        "SELECT count(*) FROM tasks WHERE project_id = ? AND status <> 'cancelled'",
        (project_id,),
    ).fetchone()[0]
    if int(used or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail="В проекте есть задачи — сначала закройте или перенесите их",
        )
    with conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return {"ok": True, "id": project_id}


# --------------------------------------------------------------------------
# Шаги проектов
# --------------------------------------------------------------------------


@router.post("/projects/{project_id}/steps", status_code=201)
def create_step(project_id: int, payload: StepCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Добавляет шаг в конец чек-листа проекта."""
    _require_project(conn, project_id)
    top = conn.execute(
        "SELECT COALESCE(MAX(sort), 0) FROM project_steps WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    with conn:
        cursor = conn.execute(
            "INSERT INTO project_steps (project_id, title, done, sort) VALUES (?, ?, 0, ?)",
            (project_id, payload.title, int(top or 0) + 1),
        )
        step_id = int(cursor.lastrowid or 0)
    row = conn.execute("SELECT * FROM project_steps WHERE id = ?", (step_id,)).fetchone()
    return _step_dict(row)


def _require_step(conn: Any, step_id: int) -> Any:
    row = conn.execute("SELECT * FROM project_steps WHERE id = ?", (step_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Шаг не найден")
    return row


@router.patch("/project-steps/{step_id}")
def patch_step(step_id: int, payload: StepPatch, conn: ConnectionDep) -> dict[str, Any]:
    """Правит шаг: заголовок и отметку «сделано»."""
    _require_step(conn, step_id)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    if "title" in data and data["title"] is None:
        raise HTTPException(status_code=422, detail="Поле title не может быть null")
    if "done" in data and data["done"] is None:
        raise HTTPException(status_code=422, detail="Поле done не может быть null")
    updates: dict[str, Any] = {}
    if "title" in data:
        updates["title"] = data["title"]
    if "done" in data:
        updates["done"] = 1 if data["done"] else 0
    assignments = ", ".join("%s = ?" % column for column in updates)
    with conn:
        conn.execute(
            "UPDATE project_steps SET " + assignments + " WHERE id = ?",
            (*updates.values(), step_id),
        )
    return _step_dict(_require_step(conn, step_id))


@router.delete("/project-steps/{step_id}")
def delete_step(step_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет шаг."""
    _require_step(conn, step_id)
    with conn:
        conn.execute("DELETE FROM project_steps WHERE id = ?", (step_id,))
    return {"ok": True, "id": step_id}


# --------------------------------------------------------------------------
# Заметки проектов
# --------------------------------------------------------------------------


@router.post("/projects/{project_id}/notes", status_code=201)
def create_note(project_id: int, payload: NoteCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Добавляет заметку или ссылку; у ссылки обязателен ``url``."""
    _require_project(conn, project_id)
    title = payload.title.strip()
    body = payload.body.strip()
    url = payload.url.strip()
    if payload.kind == "link" and not url:
        raise HTTPException(status_code=422, detail="Для ссылки нужен url")
    if payload.kind == "note" and not (title or body):
        raise HTTPException(status_code=422, detail="Заметка не может быть пустой")
    with conn:
        cursor = conn.execute(
            "INSERT INTO project_notes (project_id, kind, title, body, url) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, payload.kind, title, body, url),
        )
        note_id = int(cursor.lastrowid or 0)
    row = conn.execute("SELECT * FROM project_notes WHERE id = ?", (note_id,)).fetchone()
    return _note_dict(row)


@router.delete("/project-notes/{note_id}")
def delete_note(note_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет заметку/ссылку проекта."""
    row = conn.execute("SELECT * FROM project_notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Заметка не найдена")
    with conn:
        conn.execute("DELETE FROM project_notes WHERE id = ?", (note_id,))
    return {"ok": True, "id": note_id}
