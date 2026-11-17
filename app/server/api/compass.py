"""Компас: сферы жизни, цели с вехами и анти-цели.

Контракт v4 — ``studio/HANDOFF-OPUS-5.5.md`` §12.1. Роутер отдаёт
``/api/spheres``, ``/api/compass``, ``/api/goals``, ``/api/goals/{id}/steps``,
``/api/goal-steps/{id}`` и ``/api/anti-goals``. JSON — camelCase, ошибки —
``{"detail": "..."}`` с кодом 4xx, деньги не участвуют.

Решения, которые видно снаружи:

* **Прогресс цели** считается по приоритету: ручной ``progress`` (1..100),
  затем доля закрытых вех, затем доля закрытых связанных задач, иначе ``0``.
  Ручной ``0`` означает «считать автоматически», поэтому снять оверрайд можно
  через ``PATCH {"progress": 0}``. Закрытые задачи — это ``status='done'``;
  отменённые (``cancelled``) в знаменатель не попадают.
* **Вехи** принадлежат только цели (``ON DELETE CASCADE``): удаление цели
  уносит их, а задачи, проекты и курсы остаются со снятой ссылкой
  (``goal_id = NULL``), поэтому удаление не теряет работу пользователя.
* **Иерархия целей**: ``parentId`` может указывать только на существующую
  цель и не должен образовывать цикл — иначе 422. Порядок вывода —
  год → квартал → месяц, внутри горизонта по дедлайну.
* **Сфера, которая используется** (цели, проекты, задачи, привычки), при
  ``DELETE /api/spheres/{id}`` не удаляется, а уходит в архив
  (``archived = 1``), чтобы не оставить висящих ссылок. Свободная сфера
  удаляется физически. В ``GET /api/compass`` архивные сферы не попадают,
  но цель архивной сферы продолжает отдаваться вместе со своей сферой.
* ``PATCH /api/goals/{id}`` понимает и «старые» поля цели (``current``,
  ``target``, ``unit``, ``status``): раздел ``/api/sections/goals`` и его
  экран продолжают работать, контракт только расширяется.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.server.api.deps import ConnectionDep, SettingsDep

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["compass"])

Horizon = Literal["year", "quarter", "month"]
GoalStatus = Literal["active", "done", "dropped"]
AntiPeriod = Literal["month", "quarter"]
SphereArchiveFilter = Literal["all", "active", "archived"]

#: Таблицы со ссылкой ``sphere_id``: по ним решается «сфера используется».
_SPHERE_LINKS: tuple[str, ...] = ("goals", "projects", "tasks", "habits")

#: Цель + её сфера одним запросом (сфера нужна карточке цели).
_GOAL_SELECT = (
    "SELECT g.*, s.name AS sphere_name, s.color AS sphere_color "
    "FROM goals AS g LEFT JOIN spheres AS s ON s.id = g.sphere_id"
)

#: Порядок вывода целей: горизонт, затем дедлайн (пустые — в конце), затем id.
_GOAL_ORDER = (
    " ORDER BY CASE g.horizon WHEN 'year' THEN 0 WHEN 'quarter' THEN 1 "
    "WHEN 'month' THEN 2 ELSE 3 END, "
    "CASE WHEN g.deadline IS NULL OR g.deadline = '' THEN 1 ELSE 0 END, "
    "g.deadline, g.id"
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


class SphereCreate(BaseModel):
    """Тело ``POST /api/spheres``."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=120)
    color: str = Field("#A18BB7", max_length=32)
    icon: str = Field("", max_length=40)
    sort: int | None = Field(None, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Название сферы не может быть пустым")

    @field_validator("color", "icon")
    @classmethod
    def _marker_strip(cls, value: str) -> str:
        # Цвет — токен палитры или ``#RRGGBB``; формат проверять не нужно,
        # важно не тащить пробелы вокруг значения.
        return value.strip()


class SpherePatch(BaseModel):
    """Тело ``PATCH /api/spheres/{id}``: любое подмножество полей."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(None, min_length=1, max_length=120)
    color: str | None = Field(None, max_length=32)
    icon: str | None = Field(None, max_length=40)
    sort: int | None = Field(None, ge=0, le=1_000_000)
    archived: bool | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Название сферы не может быть пустым")

    @field_validator("color", "icon")
    @classmethod
    def _marker_strip(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()


class GoalCreate(BaseModel):
    """Тело ``POST /api/goals``."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=300)
    horizon: Horizon = "year"
    sphereId: int | None = Field(None, ge=1)  # noqa: N815 - camelCase JSON
    parentId: int | None = Field(None, ge=1)  # noqa: N815
    why: str = Field("", max_length=4000)
    deadline: str | None = None
    target: float | None = Field(None, ge=0)
    unit: str = Field("", max_length=16)
    progress: int = Field(0, ge=0, le=100)
    current: float | None = Field(None, ge=0)
    status: GoalStatus = "active"

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Название цели не может быть пустым")


class GoalPatch(BaseModel):
    """Тело ``PATCH /api/goals/{id}``: любое подмножество полей.

    ``sphereId``/``parentId``/``deadline``/``target`` можно сбросить в ``null``
    — это «отвязать»; остальные поля ``null`` не принимают (422).
    """

    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(None, min_length=1, max_length=300)
    horizon: Horizon | None = None
    sphereId: int | None = Field(None, ge=1)  # noqa: N815
    parentId: int | None = Field(None, ge=1)  # noqa: N815
    why: str | None = Field(None, max_length=4000)
    deadline: str | None = None
    target: float | None = Field(None, ge=0)
    unit: str | None = Field(None, max_length=16)
    progress: int | None = Field(None, ge=0, le=100)
    current: float | None = Field(None, ge=0)
    status: GoalStatus | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Название цели не может быть пустым")


class GoalStepCreate(BaseModel):
    """Тело ``POST /api/goals/{id}/steps``."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=300)
    due: str | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Название вехи не может быть пустым")


class GoalStepPatch(BaseModel):
    """Тело ``PATCH /api/goal-steps/{id}``."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(None, min_length=1, max_length=300)
    done: bool | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Название вехи не может быть пустым")


class AntiGoalCreate(BaseModel):
    """Тело ``POST /api/anti-goals``."""

    model_config = ConfigDict(extra="ignore")

    period: AntiPeriod
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Анти-цель не может быть пустой")


class AntiGoalPatch(BaseModel):
    """Тело ``PATCH /api/anti-goals/{id}``."""

    model_config = ConfigDict(extra="ignore")

    text: str | None = Field(None, min_length=1, max_length=4000)
    active: bool | None = None

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Анти-цель не может быть пустой")


# --------------------------------------------------------------------------
# Даты
# --------------------------------------------------------------------------


def _clean_day(value: str | None) -> str | None:
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
# Преобразование строк SQLite → JSON (camelCase)
# --------------------------------------------------------------------------


def _sphere_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "color": str(row["color"] or "#A18BB7"),
        "icon": str(row["icon"] or ""),
        "sort": int(row["sort"] or 0),
        "archived": bool(row["archived"]),
    }


def _step_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "goalId": int(row["goal_id"]),
        "title": str(row["title"]),
        "done": bool(row["done"]),
        "due": row["due"],
        "sort": int(row["sort"] or 0),
    }


def _anti_goal_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "period": str(row["period"]),
        "text": str(row["text"]),
        "active": bool(row["active"]),
        "created": str(row["created"] or ""),
    }


def _progress_pct(
    manual: int, steps_done: int, steps_total: int, tasks_done: int, tasks_total: int
) -> int:
    """Процент цели: ручной оверрайд → вехи → задачи → ``0``."""
    if manual > 0:
        return min(int(manual), 100)
    if steps_total > 0:
        return int(round(steps_done * 100 / steps_total))
    if tasks_total > 0:
        return int(round(tasks_done * 100 / tasks_total))
    return 0


def _goal_dict(row: Any, steps: list[dict[str, Any]], tasks: tuple[int, int]) -> dict[str, Any]:
    """Карточка цели §12.1 + поля, нужные старым экранам (target/current/unit)."""
    steps_total = len(steps)
    steps_done = sum(1 for step in steps if step["done"])
    tasks_total, tasks_done = tasks
    manual = int(row["progress"] or 0)
    sphere = None
    if row["sphere_id"] is not None and row["sphere_name"] is not None:
        sphere = {
            "id": int(row["sphere_id"]),
            "name": str(row["sphere_name"]),
            "color": str(row["sphere_color"] or "#A18BB7"),
        }
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "horizon": str(row["horizon"] or "year"),
        "sphereId": int(row["sphere_id"]) if row["sphere_id"] is not None else None,
        "sphere": sphere,
        "parentId": int(row["parent_id"]) if row["parent_id"] is not None else None,
        "deadline": row["deadline"],
        "status": str(row["status"] or "active"),
        "progress": manual,
        "progressPct": _progress_pct(manual, steps_done, steps_total, tasks_done, tasks_total),
        "why": str(row["why"] or ""),
        "target": float(row["target"]) if row["target"] is not None else None,
        "current": float(row["current"] or 0),
        "unit": str(row["unit"] or ""),
        "steps": steps,
        "counts": {
            "stepsDone": steps_done,
            "stepsTotal": steps_total,
            "tasksDone": tasks_done,
            "tasksTotal": tasks_total,
        },
        "created": str(row["created"] or ""),
    }


# --------------------------------------------------------------------------
# Общие операции
# --------------------------------------------------------------------------


def _require_row(conn: Any, table: str, row_id: int, detail: str) -> Any:
    # ``table`` — константа модуля, пользовательские данные в SQL не попадают.
    row = conn.execute("SELECT * FROM %s WHERE id = ?" % table, (row_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row


def _require_sphere(conn: Any, sphere_id: int) -> Any:
    return _require_row(conn, "spheres", sphere_id, "Сфера не найдена")


def _require_ref_sphere(conn: Any, sphere_id: int) -> None:
    """Ссылка из тела запроса: несуществующая сфера — 422, а не 404."""
    if conn.execute("SELECT 1 FROM spheres WHERE id = ?", (sphere_id,)).fetchone() is None:
        raise HTTPException(status_code=422, detail="Сфера не найдена")


def _require_goal(conn: Any, goal_id: int) -> Any:
    row = conn.execute(_GOAL_SELECT + " WHERE g.id = ?", (goal_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Цель не найдена")
    return row


def _require_anti_goal(conn: Any, anti_id: int) -> Any:
    return _require_row(conn, "anti_goals", anti_id, "Анти-цель не найдена")


def _sphere_name_taken(conn: Any, name: str, exclude_id: int | None = None) -> bool:
    """Сравнение имён без регистра: SQLite ``lower()`` не складывает кириллицу."""
    needle = name.casefold()
    for row in conn.execute("SELECT id, name FROM spheres"):
        if exclude_id is not None and int(row["id"]) == exclude_id:
            continue
        if str(row["name"]).strip().casefold() == needle:
            return True
    return False


def _sphere_usage(conn: Any, sphere_id: int) -> dict[str, int]:
    """Сколько записей ссылается на сферу: ``{goals: 2, tasks: 5}``."""
    usage: dict[str, int] = {}
    for table in _SPHERE_LINKS:
        row = conn.execute(
            "SELECT count(*) FROM %s WHERE sphere_id = ?" % table, (sphere_id,)
        ).fetchone()
        total = int(row[0]) if row is not None else 0
        if total:
            usage[table] = total
    return usage


def _steps_by_goal(conn: Any, goal_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Вехи всех запрошенных целей одним запросом (без N+1)."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    if not goal_ids:
        return grouped
    placeholders = ",".join("?" * len(goal_ids))
    rows = conn.execute(
        "SELECT * FROM goal_steps WHERE goal_id IN (%s) ORDER BY goal_id, sort, id"
        % placeholders,
        goal_ids,
    ).fetchall()
    for row in rows:
        grouped.setdefault(int(row["goal_id"]), []).append(_step_dict(row))
    return grouped


def _task_counts(conn: Any, goal_ids: list[int]) -> dict[int, tuple[int, int]]:
    """``{goal_id: (всего, закрыто)}``; отменённые задачи не считаются."""
    counts: dict[int, tuple[int, int]] = {}
    if not goal_ids:
        return counts
    placeholders = ",".join("?" * len(goal_ids))
    rows = conn.execute(
        "SELECT goal_id, count(*) AS total, "
        " sum(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done "
        "FROM tasks WHERE goal_id IN (%s) AND status IN ('todo', 'done') "
        "GROUP BY goal_id" % placeholders,
        goal_ids,
    ).fetchall()
    for row in rows:
        counts[int(row["goal_id"])] = (int(row["total"] or 0), int(row["done"] or 0))
    return counts


def _load_goals(conn: Any, where: str = "", params: tuple = ()) -> list[dict[str, Any]]:
    """Цели в порядке горизонта с вехами и счётчиками задач."""
    rows = conn.execute(_GOAL_SELECT + where + _GOAL_ORDER, params).fetchall()
    if not rows:
        return []
    goal_ids = [int(row["id"]) for row in rows]
    steps = _steps_by_goal(conn, goal_ids)
    tasks = _task_counts(conn, goal_ids)
    return [
        _goal_dict(row, steps.get(int(row["id"]), []), tasks.get(int(row["id"]), (0, 0)))
        for row in rows
    ]


def _goal_payload(conn: Any, goal_id: int) -> dict[str, Any]:
    return _load_goals(conn, " WHERE g.id = ?", (goal_id,))[0]


def _check_parent(conn: Any, goal_id: int | None, parent_id: int | None) -> None:
    """Родитель существует и не образует цикл; ``None`` — корневая цель."""
    if parent_id is None:
        return
    if goal_id is not None and parent_id == goal_id:
        raise HTTPException(status_code=422, detail="Цель не может быть родителем самой себе")
    current: int | None = parent_id
    for _ in range(100):
        if current is None:
            return
        if goal_id is not None and current == goal_id:
            raise HTTPException(
                status_code=422, detail="Иерархия целей не может образовывать цикл"
            )
        row = conn.execute("SELECT parent_id FROM goals WHERE id = ?", (current,)).fetchone()
        if row is None:
            raise HTTPException(status_code=422, detail="Родительская цель не найдена")
        current = int(row["parent_id"]) if row["parent_id"] is not None else None
    raise HTTPException(status_code=422, detail="Иерархия целей слишком глубокая")


# --------------------------------------------------------------------------
# Сферы
# --------------------------------------------------------------------------


@router.get("/spheres")
def list_spheres(
    conn: ConnectionDep,
    archived: SphereArchiveFilter = Query(
        "all", description="``all`` — все, ``active`` — без архива, ``archived`` — только архив"
    ),
) -> dict[str, Any]:
    """Список сфер жизни с цветом-маркером, иконкой и порядком."""
    sql = "SELECT * FROM spheres"
    if archived == "active":
        sql += " WHERE archived = 0"
    elif archived == "archived":
        sql += " WHERE archived = 1"
    sql += " ORDER BY sort, id"
    return {"items": [_sphere_dict(row) for row in conn.execute(sql).fetchall()]}


@router.post("/spheres", status_code=201)
def create_sphere(payload: SphereCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Создаёт сферу; имя уникально без учёта регистра (иначе 409)."""
    if _sphere_name_taken(conn, payload.name):
        raise HTTPException(status_code=409, detail="Сфера с таким названием уже есть")
    sort = payload.sort
    if sort is None:
        row = conn.execute("SELECT coalesce(max(sort), -1) + 1 FROM spheres").fetchone()
        sort = int(row[0] or 0)
    with conn:
        cursor = conn.execute(
            "INSERT INTO spheres (name, color, icon, sort, archived) VALUES (?, ?, ?, ?, 0)",
            (payload.name, payload.color or "#A18BB7", payload.icon, sort),
        )
    sphere_id = int(cursor.lastrowid or 0)
    return _sphere_dict(_require_sphere(conn, sphere_id))


@router.patch("/spheres/{sphere_id}")
def patch_sphere(sphere_id: int, payload: SpherePatch, conn: ConnectionDep) -> dict[str, Any]:
    """Правит сферу; ``archived: false`` возвращает её из архива."""
    row = _require_sphere(conn, sphere_id)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    for field in ("name", "color", "icon", "sort", "archived"):
        if field in data and data[field] is None:
            raise HTTPException(status_code=422, detail="Поле %s не может быть null" % field)
    if "name" in data and _sphere_name_taken(conn, str(data["name"]), exclude_id=sphere_id):
        raise HTTPException(status_code=409, detail="Сфера с таким названием уже есть")

    updates: dict[str, Any] = {}
    for field in ("name", "color", "icon", "sort"):
        if field in data:
            updates[field] = data[field]
    if "archived" in data:
        updates["archived"] = 1 if data["archived"] else 0
    if not updates:
        raise HTTPException(status_code=400, detail="Нечего обновлять")

    assignments = ", ".join("%s = ?" % column for column in updates)
    with conn:
        conn.execute(
            "UPDATE spheres SET %s WHERE id = ?" % assignments,
            (*updates.values(), int(row["id"])),
        )
    return _sphere_dict(_require_sphere(conn, sphere_id))


@router.delete("/spheres/{sphere_id}")
def delete_sphere(sphere_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет сферу; используемая — уходит в архив, чтобы не рвать ссылки."""
    _require_sphere(conn, sphere_id)
    usage = _sphere_usage(conn, sphere_id)
    with conn:
        if usage:
            conn.execute("UPDATE spheres SET archived = 1 WHERE id = ?", (sphere_id,))
        else:
            conn.execute("DELETE FROM spheres WHERE id = ?", (sphere_id,))
    return {"id": sphere_id, "deleted": not usage, "archived": bool(usage), "usage": usage}


# --------------------------------------------------------------------------
# Компас
# --------------------------------------------------------------------------


@router.get("/compass")
def get_compass(
    conn: ConnectionDep,
    settings: SettingsDep,
    day: str | None = Query(None, alias="date", description="Опорный день ``YYYY-MM-DD``"),
) -> dict[str, Any]:
    """Снимок Компаса: сферы, цели с вехами и анти-цели на месяц и квартал."""
    today = _clean_day(day) or settings.today()
    spheres = [
        _sphere_dict(row)
        for row in conn.execute(
            "SELECT * FROM spheres WHERE archived = 0 ORDER BY sort, id"
        ).fetchall()
    ]
    anti_rows = conn.execute(
        "SELECT * FROM anti_goals ORDER BY CASE period WHEN 'month' THEN 0 ELSE 1 END, id"
    ).fetchall()
    anti_goals: dict[str, list[dict[str, Any]]] = {"month": [], "quarter": []}
    for row in anti_rows:
        period = str(row["period"])
        if period in anti_goals:  # CHECK в схеме, но старые строки не должны ронять API
            anti_goals[period].append(_anti_goal_dict(row))
    return {
        "date": today,
        "spheres": spheres,
        "goals": _load_goals(conn),
        "antiGoals": anti_goals,
    }


# --------------------------------------------------------------------------
# Цели
# --------------------------------------------------------------------------


@router.post("/goals", status_code=201)
def create_compass_goal(payload: GoalCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Создаёт цель года/квартала/месяца; прогресс пока считается автоматически."""
    if payload.sphereId is not None:
        _require_ref_sphere(conn, payload.sphereId)
    _check_parent(conn, None, payload.parentId)
    deadline = _clean_day(payload.deadline)
    with conn:
        cursor = conn.execute(
            "INSERT INTO goals (title, target, current, unit, deadline, status, sphere_id, "
            "horizon, parent_id, why, progress) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload.title,
                payload.target,
                float(payload.current or 0),
                payload.unit,
                deadline,
                payload.status,
                payload.sphereId,
                payload.horizon,
                payload.parentId,
                payload.why,
                payload.progress,
            ),
        )
    goal_id = int(cursor.lastrowid or 0)
    return _goal_payload(conn, goal_id)


@router.patch("/goals/{goal_id}")
def patch_compass_goal(goal_id: int, payload: GoalPatch, conn: ConnectionDep) -> dict[str, Any]:
    """Правит цель: горизонт, сферу, родителя, вехи плана и ручной прогресс."""
    _require_goal(conn, goal_id)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    nullable = {"sphereId", "parentId", "deadline", "target"}
    for field, value in data.items():
        if value is None and field not in nullable:
            raise HTTPException(status_code=422, detail="Поле %s не может быть null" % field)

    updates: dict[str, Any] = {}
    for field in ("title", "horizon", "why", "status", "progress", "unit", "current"):
        if field in data:
            updates[field] = data[field]
    if "target" in data:
        updates["target"] = data["target"]
    if "deadline" in data:
        updates["deadline"] = _clean_day(data["deadline"])
    if "sphereId" in data:
        if data["sphereId"] is not None:
            _require_ref_sphere(conn, data["sphereId"])
        updates["sphere_id"] = data["sphereId"]
    if "parentId" in data:
        _check_parent(conn, goal_id, data["parentId"])
        updates["parent_id"] = data["parentId"]

    assignments = ", ".join("%s = ?" % column for column in updates)
    with conn:
        conn.execute(
            "UPDATE goals SET %s WHERE id = ?" % assignments,
            (*updates.values(), goal_id),
        )
    return _goal_payload(conn, goal_id)


@router.delete("/goals/{goal_id}")
def delete_goal(goal_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет цель; вехи уходят каскадом, задачи и проекты остаются без ссылки."""
    _require_goal(conn, goal_id)
    with conn:
        conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    return {"id": goal_id, "deleted": True}


# --------------------------------------------------------------------------
# Вехи целей
# --------------------------------------------------------------------------


@router.post("/goals/{goal_id}/steps", status_code=201)
def create_goal_step(
    goal_id: int, payload: GoalStepCreate, conn: ConnectionDep
) -> dict[str, Any]:
    """Добавляет веху в конец списка цели."""
    _require_goal(conn, goal_id)
    due = _clean_day(payload.due)
    row = conn.execute(
        "SELECT coalesce(max(sort), -1) + 1 FROM goal_steps WHERE goal_id = ?", (goal_id,)
    ).fetchone()
    sort = int(row[0] or 0)
    with conn:
        cursor = conn.execute(
            "INSERT INTO goal_steps (goal_id, title, done, due, sort) VALUES (?, ?, 0, ?, ?)",
            (goal_id, payload.title, due, sort),
        )
    step_id = int(cursor.lastrowid or 0)
    return _step_dict(_require_row(conn, "goal_steps", step_id, "Веха не найдена"))


@router.patch("/goal-steps/{step_id}")
def patch_goal_step(
    step_id: int, payload: GoalStepPatch, conn: ConnectionDep
) -> dict[str, Any]:
    """Правит веху: название и/или отметку «сделано»."""
    _require_row(conn, "goal_steps", step_id, "Веха не найдена")
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    for field, value in data.items():
        if value is None:
            raise HTTPException(status_code=422, detail="Поле %s не может быть null" % field)
    updates: dict[str, Any] = {}
    if "title" in data:
        updates["title"] = data["title"]
    if "done" in data:
        updates["done"] = 1 if data["done"] else 0
    assignments = ", ".join("%s = ?" % column for column in updates)
    with conn:
        conn.execute(
            "UPDATE goal_steps SET %s WHERE id = ?" % assignments,
            (*updates.values(), step_id),
        )
    return _step_dict(_require_row(conn, "goal_steps", step_id, "Веха не найдена"))


@router.delete("/goal-steps/{step_id}")
def delete_goal_step(step_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет веху."""
    _require_row(conn, "goal_steps", step_id, "Веха не найдена")
    with conn:
        conn.execute("DELETE FROM goal_steps WHERE id = ?", (step_id,))
    return {"id": step_id, "deleted": True}


# --------------------------------------------------------------------------
# Анти-цели
# --------------------------------------------------------------------------


@router.get("/anti-goals")
def list_anti_goals(
    conn: ConnectionDep,
    period: AntiPeriod | None = Query(
        None, description="``month`` или ``quarter``; пусто — оба списка"
    ),
) -> dict[str, Any]:
    """Анти-цели месяца и квартала с счётчиками."""
    sql = "SELECT * FROM anti_goals"
    params: list[Any] = []
    if period is not None:
        sql += " WHERE period = ?"
        params.append(period)
    sql += " ORDER BY CASE period WHEN 'month' THEN 0 ELSE 1 END, id"
    items = [_anti_goal_dict(row) for row in conn.execute(sql, params).fetchall()]
    counts = {"month": 0, "quarter": 0}
    for row in conn.execute("SELECT period, count(*) AS total FROM anti_goals GROUP BY period"):
        key = str(row["period"])
        if key in counts:
            counts[key] = int(row["total"])
    return {"items": items, "counts": counts}


@router.post("/anti-goals", status_code=201)
def create_anti_goal(payload: AntiGoalCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Добавляет анти-цель в список месяца или квартала."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO anti_goals (period, text, active) VALUES (?, ?, 1)",
            (payload.period, payload.text),
        )
    anti_id = int(cursor.lastrowid or 0)
    return _anti_goal_dict(_require_anti_goal(conn, anti_id))


@router.patch("/anti-goals/{anti_id}")
def patch_anti_goal(
    anti_id: int, payload: AntiGoalPatch, conn: ConnectionDep
) -> dict[str, Any]:
    """Правит текст анти-цели или снимает/возвращает её активность."""
    _require_anti_goal(conn, anti_id)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    for field, value in data.items():
        if value is None:
            raise HTTPException(status_code=422, detail="Поле %s не может быть null" % field)
    updates: dict[str, Any] = {}
    if "text" in data:
        updates["text"] = data["text"]
    if "active" in data:
        updates["active"] = 1 if data["active"] else 0
    assignments = ", ".join("%s = ?" % column for column in updates)
    with conn:
        conn.execute(
            "UPDATE anti_goals SET %s WHERE id = ?" % assignments,
            (*updates.values(), anti_id),
        )
    return _anti_goal_dict(_require_anti_goal(conn, anti_id))


@router.delete("/anti-goals/{anti_id}")
def delete_anti_goal(anti_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет анти-цель."""
    _require_anti_goal(conn, anti_id)
    with conn:
        conn.execute("DELETE FROM anti_goals WHERE id = ?", (anti_id,))
    return {"id": anti_id, "deleted": True}
