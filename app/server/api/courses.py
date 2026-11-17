"""Курсы, уроки и конспекты: ``/api/courses``, ``/api/courses/{id}``,
``/api/lessons/{id}``.

Ответы строятся из ``models.Course/Lesson/LessonDetail``: у курса в списке
есть агрегаты ``lessons_total`` / ``lessons_done`` / ``progress``, у урока —
конспект, сегменты с таймкодами и заметки, а также ссылки на соседние уроки
для кнопок «назад/дальше» в плеере.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.server.api.deps import ConnectionDep
from app.server.models import (
    Course,
    CourseDetail,
    Lesson,
    LessonDetail,
    Note,
    Progress,
    Segment,
)

__all__ = ["ProgressIn", "router"]

router = APIRouter(prefix="/api", tags=["courses"])

_COURSE_SELECT = (
    "SELECT c.*, "
    " (SELECT count(*) FROM lessons l WHERE l.course_id = c.id) AS lessons_total, "
    " (SELECT count(*) FROM lessons l WHERE l.course_id = c.id AND l.status = 'done') "
    "   AS lessons_done "
    "FROM courses c"
)


@router.get("/courses")
def list_courses(conn: ConnectionDep) -> list[Course]:
    """Все курсы с прогрессом, новые сверху."""
    rows = conn.execute(_COURSE_SELECT + " ORDER BY c.created DESC, c.id DESC").fetchall()
    return [_course_from_row(row) for row in rows]


@router.get("/courses/{course_id}")
def get_course(course_id: int, conn: ConnectionDep) -> CourseDetail:
    """Курс с уроками (отсортированы по ``idx``); 404 — курса нет."""
    row = conn.execute(_COURSE_SELECT + " WHERE c.id = ?", (course_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Курс не найден")
    course = _course_from_row(row)
    lessons = [
        Lesson.from_row(lesson_row)
        for lesson_row in conn.execute(
            "SELECT * FROM lessons WHERE course_id = ? ORDER BY idx, id", (course_id,)
        )
    ]
    return CourseDetail(**course.model_dump(), lessons=lessons)


@router.get("/lessons/{lesson_id}")
def get_lesson(lesson_id: int, conn: ConnectionDep) -> LessonDetail:
    """Урок: конспект, сегменты, заметки, прогресс и соседние уроки."""
    lesson_row = conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if lesson_row is None:
        raise HTTPException(status_code=404, detail="Урок не найден")
    lesson = Lesson.from_row(lesson_row)

    course_row = conn.execute(
        "SELECT * FROM courses WHERE id = ?", (lesson.course_id,)
    ).fetchone()
    course = Course.from_row(course_row) if course_row is not None else None

    progress_row = conn.execute(
        "SELECT * FROM lesson_progress WHERE lesson_id = ?", (lesson_id,)
    ).fetchone()
    progress = Progress.from_row(progress_row) if progress_row is not None else None
    if progress is not None and lesson.duration_s > 0:
        progress = progress.model_copy(
            update={"percent": round(progress.watched_s / lesson.duration_s, 4)}
        )

    transcript_row = conn.execute(
        "SELECT text FROM transcripts WHERE lesson_id = ? "
        "ORDER BY CASE WHEN lang = 'ru' THEN 0 ELSE 1 END, lang LIMIT 1",
        (lesson_id,),
    ).fetchone()
    segments = [
        Segment.from_row(row)
        for row in conn.execute(
            "SELECT * FROM segments WHERE lesson_id = ? ORDER BY start_s, id", (lesson_id,)
        )
    ]
    notes = [
        Note.from_row(row)
        for row in conn.execute(
            "SELECT * FROM notes WHERE lesson_id = ? ORDER BY ts, id", (lesson_id,)
        )
    ]

    previous = conn.execute(
        "SELECT id FROM lessons WHERE course_id = ? AND (idx < ? OR (idx = ? AND id < ?)) "
        "ORDER BY idx DESC, id DESC LIMIT 1",
        (lesson.course_id, lesson.idx, lesson.idx, lesson.id),
    ).fetchone()
    following = conn.execute(
        "SELECT id FROM lessons WHERE course_id = ? AND (idx > ? OR (idx = ? AND id > ?)) "
        "ORDER BY idx, id LIMIT 1",
        (lesson.course_id, lesson.idx, lesson.idx, lesson.id),
    ).fetchone()

    return LessonDetail(
        lesson=lesson,
        course=course,
        progress=progress,
        transcript=str(transcript_row["text"]) if transcript_row is not None else None,
        segments=segments,
        notes=notes,
        prev_lesson_id=int(previous["id"]) if previous is not None else None,
        next_lesson_id=int(following["id"]) if following is not None else None,
    )


def _course_from_row(row: Any) -> Course:
    course = Course.from_row(row)
    total = int(course.lessons_total or 0)
    done = int(course.lessons_done or 0)
    progress = round(done / total, 4) if total > 0 else None
    return course.model_copy(update={"progress": progress})


class ProgressIn(BaseModel):
    """Позиция просмотра, присылаемая плеером (``POST /api/lessons/{id}/progress``)."""

    position_s: float = Field(0.0, ge=0)
    watched_s: float | None = Field(None, ge=0)


@router.post("/lessons/{lesson_id}/progress")
def save_progress(lesson_id: int, payload: ProgressIn, conn: ConnectionDep) -> Progress:
    """Сохраняет позицию плеера; при просмотре почти до конца урок закрывается."""
    row = conn.execute(
        "SELECT id, duration_s, status FROM lessons WHERE id = ?", (lesson_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Урок не найден")
    duration = float(row[1] or 0)
    current = conn.execute(
        "SELECT watched_s FROM lesson_progress WHERE lesson_id = ?", (lesson_id,)
    ).fetchone()
    watched = payload.watched_s
    if watched is None:
        watched = max(float(current[0]) if current else 0.0, payload.position_s)
    with conn:
        conn.execute(
            "INSERT INTO lesson_progress (lesson_id, position_s, watched_s, updated) "
            "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "ON CONFLICT(lesson_id) DO UPDATE SET position_s = excluded.position_s, "
            "watched_s = excluded.watched_s, updated = excluded.updated",
            (lesson_id, payload.position_s, watched),
        )
        done = duration > 0 and (watched >= duration * 0.95 or payload.position_s >= duration * 0.95)
        if done and str(row[2]) != "done":
            conn.execute("UPDATE lessons SET status = 'done' WHERE id = ?", (lesson_id,))
        elif not done and str(row[2]) == "done" and payload.position_s < duration * 0.5:
            conn.execute("UPDATE lessons SET status = 'new' WHERE id = ?", (lesson_id,))
    ratio = None
    if duration > 0:
        ratio = round(min(1.0, watched / duration), 3)
    return Progress(
        lesson_id=lesson_id,
        position_s=payload.position_s,
        watched_s=watched,
        percent=ratio,
    )
