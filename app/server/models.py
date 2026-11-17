"""Pydantic-модели API Life OS v3.

Модели описывают контракт между SQLite и FastAPI: строки ``sqlite3.Row``
превращаются в модели через ``from_row`` (JSON-колонки ``meta_json`` /
``payload_json`` декодируются в словари). Времена передаются строками ISO 8601:
``ts`` — с часовым поясом (``2026-11-17T08:30:00+03:00``), ``date`` —
``YYYY-MM-DD``, ``created`` / ``updated`` — UTC ISO.

Модели БД (Entry, Metric, Course, Lesson...) используют snake_case как в схеме.
``Overview`` повторяет camelCase-контракт SPA из ``app/web/src/api/types.ts``
(``GET /api/overview``) и допускает лишние поля: обзор расширяется виджетами
без правки уже написанных вызовов.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover - только для проверки типов
    from typing import Self

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ActivityDay",
    "AgendaItem",
    "AgendaKind",
    "ApiModel",
    "CheckinsKpi",
    "Course",
    "CourseDetail",
    "DayPoint",
    "Entry",
    "Goal",
    "Habit",
    "Insight",
    "JobStatus",
    "Lesson",
    "LessonDetail",
    "Metric",
    "MoodKpi",
    "NavBadges",
    "Note",
    "Overview",
    "OverviewCourse",
    "OverviewKpis",
    "Progress",
    "RowModel",
    "Segment",
    "SleepKpi",
    "StreakKpi",
    "SyncState",
    "SyncStatus",
    "Transcript",
    "WaterKpi",
    "WeightWidget",
    "WorkoutsKpi",
]

class ApiModel(BaseModel):
    """Базовый класс ответов API: дополнительные поля игнорируются."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class RowModel(ApiModel):
    """Модель, которую можно собрать из строки SQLite."""

    @classmethod
    def from_row(cls, row: Any) -> Self:
        data = _as_dict(row)
        fields = set(cls.model_fields)
        return cls(**{key: value for key, value in data.items() if key in fields})


class Entry(RowModel):
    """Запись журнала (мысль, еда, тренировка, задача...)."""

    id: int
    ts: str
    kind: str
    text: str
    source: str = "telegram"
    meta: dict[str, Any] = Field(default_factory=dict)
    date: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> Self:
        data = _as_dict(row)
        _move_json(data, "meta_json", "meta")
        if not data.get("date") and data.get("ts"):
            data["date"] = str(data["ts"])[:10]
        return cls(**{key: value for key, value in data.items() if key in cls.model_fields})


class Metric(RowModel):
    """Числовое измерение: вес, жир, мышцы, вода, сон, настроение."""

    id: int
    date: str
    kind: str
    value: float
    unit: str = ""
    source: str = "telegram"


class Course(RowModel):
    """Курс: обложка, категория и необязательный агрегат прогресса."""

    id: int
    title: str
    category: str = ""
    cover: str = ""
    created: str = ""
    lessons_total: int | None = None
    lessons_done: int | None = None
    progress: float | None = Field(default=None, description="Доля пройденного 0..1")


class CourseDetail(Course):
    """Курс вместе с уроками (``GET /api/courses/{id}``)."""

    lessons: list[Lesson] = Field(default_factory=list)


class Lesson(RowModel):
    """Урок курса; ``media_path`` — путь внутри ``data/media``."""

    id: int
    course_id: int
    idx: int = 0
    title: str
    media_path: str = ""
    duration_s: float = 0.0
    status: str = "new"


class Segment(RowModel):
    """Фрагмент субтитров с таймкодами."""

    id: int
    lesson_id: int
    start_s: float
    end_s: float
    text: str


class Note(RowModel):
    """Заметка к уроку (``ts`` — момент создания, ISO 8601)."""

    id: int
    lesson_id: int
    ts: str
    text: str


class Transcript(RowModel):
    """Расшифровка урока целиком (одна запись на язык)."""

    lesson_id: int
    lang: str = "ru"
    text: str = ""


class Progress(RowModel):
    """Позиция просмотра урока."""

    lesson_id: int
    position_s: float = 0.0
    watched_s: float = 0.0
    updated: str = ""
    percent: float | None = Field(default=None, description="Доля просмотра 0..1")


class LessonDetail(ApiModel):
    """Урок с конспектом, сегментами и заметками (``GET /api/lessons/{id}``)."""

    lesson: Lesson
    course: Course | None = None
    progress: Progress | None = None
    transcript: str | None = None
    segments: list[Segment] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    prev_lesson_id: int | None = None
    next_lesson_id: int | None = None


class JobStatus(RowModel):
    """Задача фоновой очереди (транскрибация, конспект, синк).

    ``meta`` — прогресс и пояснения воркера: ``{"progress": 0.42, "message": "..."}``.
    """

    id: int
    kind: str
    status: str = "queued"
    attempts: int = 0
    error: str = ""
    created: str = ""
    finished: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Any) -> Self:
        data = _as_dict(row)
        _move_json(data, "payload_json", "payload")
        _move_json(data, "meta_json", "meta")
        return cls(**{key: value for key, value in data.items() if key in cls.model_fields})


class Goal(RowModel):
    """Цель с числовым прогрессом."""

    id: int
    title: str
    target: float | None = None
    current: float = 0.0
    unit: str = ""
    deadline: str | None = None
    status: str = "active"
    created: str = ""


class Habit(RowModel):
    """Привычка; ``streak``/``last_done`` заполняет аналитика, если считает."""

    id: int
    name: str
    emoji: str = ""
    target_per_week: int = 7
    archived: bool = False
    created: str = ""
    streak: int | None = None
    last_done: str | None = None


class DayPoint(ApiModel):
    """Точка временного ряда: локальная дата и значение."""

    date: str
    value: float


class StreakKpi(ApiModel):
    """Серия чек-инов: текущая и лучшая."""

    days: int
    bestDays: int


class CheckinsKpi(ApiModel):
    """Чек-ины: сколько заполнено из скольких."""

    completed: int
    total: int


class WorkoutsKpi(ApiModel):
    """Тренировки: за неделю и цель."""

    week: int
    target: int


class SleepKpi(ApiModel):
    """Сон: прошлая ночь и среднее за 7 дней, часы."""

    lastHours: float
    avg7Hours: float


class WaterKpi(ApiModel):
    """Вода: выпито и цель, мл."""

    ml: float
    targetMl: float


class MoodKpi(ApiModel):
    """Настроение: оценка, шкала, подпись и среднее."""

    score: float
    max: float
    label: str
    avg7: float


class OverviewKpis(ApiModel):
    """Шесть KPI главного экрана."""

    streak: StreakKpi
    checkins: CheckinsKpi
    workouts: WorkoutsKpi
    sleep: SleepKpi
    water: WaterKpi
    mood: MoodKpi


class WeightWidget(ApiModel):
    """Виджет веса: текущий, цель, дельта за 30 дней и ряд."""

    currentKg: float
    targetKg: float
    delta30Kg: float
    series: list[DayPoint] = Field(default_factory=list)


class OverviewCourse(ApiModel):
    """Карточка «продолжить курс».

    ``id``/``nextLessonId`` — строки: API приводит числовые ключи БД к строке,
    потому что UI использует их в маршрутах и ключах списков.
    """

    id: str
    title: str
    lessonTitle: str
    doneLessons: int
    totalLessons: int
    nextLessonId: str | None = None


AgendaKind = Literal["habit", "goal", "workout", "journal"]


class AgendaItem(ApiModel):
    """Пункт повестки дня на обзоре."""

    id: str
    title: str
    meta: str = ""
    done: bool = False
    kind: AgendaKind


class ActivityDay(ApiModel):
    """Активность за день: подпись дня недели и минуты."""

    date: str
    label: str
    minutes: float


class Insight(ApiModel):
    """Инсайт аналитики с корреляцией."""

    title: str
    text: str
    footnote: str = ""


SyncState = Literal["ok", "syncing", "error"]


class SyncStatus(ApiModel):
    """Статус синхронизации с Telegram/Obsidian."""

    state: SyncState
    at: str


class NavBadges(ApiModel):
    """Счётчики-бейджи в сайдбаре."""

    today: int = 0
    habits: int = 0
    goals: int = 0
    courses: int = 0


class Overview(BaseModel):
    """Ответ ``GET /api/overview`` — контракт совпадает с ``app/web/src/api/types.ts``.

    Поля названы camelCase, как в TypeScript-контракте SPA, чтобы ответ можно
    было сериализовать без переименований. ``extra="allow"``: обзор можно
    расширять новыми виджетами, не ломая уже написанные вызовы.
    """

    model_config = ConfigDict(extra="allow")

    date: str
    kpis: OverviewKpis
    weight: WeightWidget
    course: OverviewCourse | None = None
    agenda: list[AgendaItem] = Field(default_factory=list)
    activity: list[ActivityDay] = Field(default_factory=list)
    insight: Insight | None = None
    sync: SyncStatus
    nav: NavBadges = Field(default_factory=NavBadges)
    generated_at: str | None = None


def _as_dict(row: Any) -> dict[str, Any]:
    """Приводит ``sqlite3.Row``/dict к обычному словарю."""
    if isinstance(row, dict):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {key: row[key] for key in keys()}
    raise TypeError("Ожидалась строка sqlite3.Row или dict, получено %r" % (type(row),))


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _move_json(data: dict[str, Any], source: str, target: str) -> None:
    """Переносит JSON-колонку БД (``meta_json``) в поле API (``meta``)."""
    if source in data:
        data[target] = _json_dict(data.pop(source))
    elif isinstance(data.get(target), str):
        data[target] = _json_dict(data[target])
