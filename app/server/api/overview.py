"""``GET /api/overview`` — главный экран дашборда одним запросом.

Ответ — :class:`app.server.models.Overview`, его форма повторяет
``app/web/src/api/types.ts`` (camelCase, ``kpis``/``weight``/``course``/``agenda``/
``activity``/``insight``/``sync``/``nav``). Всё считается из реальных таблиц;
пустая база даёт нулевые KPI и пустые списки, а не 500.

Приближения, зафиксированные осознанно (других данных в схеме нет):

* «чек-ины» — число записей журнала за сегодня, ограниченное 6 слотами;
* «активность» — минуты из ``meta_json`` записи, а если их нет — оценка по типу
  записи (см. :func:`app.server.api.aggregates.entry_daily_minutes`);
* стрик — дни подряд с хотя бы одной записью журнала.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter

from app.server.api.aggregates import (
    TRAINING_KINDS,
    WATER_KINDS,
    build_correlations,
    entry_daily_counts,
    entry_daily_minutes,
    latest_metric,
    metric_daily_avg,
    metric_daily_sum,
    parse_day,
    pref_float,
    pref_int,
    sleep_hours,
    weekday_label,
)
from app.server.api.deps import ConnectionDep, SettingsDep
from app.server.models import (
    ActivityDay,
    AgendaItem,
    CheckinsKpi,
    DayPoint,
    Insight,
    MoodKpi,
    NavBadges,
    Overview,
    OverviewCourse,
    OverviewKpis,
    SleepKpi,
    StreakKpi,
    SyncStatus,
    WaterKpi,
    WeightWidget,
    WorkoutsKpi,
)

__all__ = ["build_overview", "router"]

router = APIRouter(prefix="/api", tags=["overview"])

CHECKINS_TOTAL = 6
ACTIVITY_DAYS = 7
WEIGHT_SERIES_DAYS = 14
AGENDA_LIMIT = 6


@router.get("/overview")
def get_overview(conn: ConnectionDep, settings: SettingsDep) -> Overview:
    """Собирает обзор дашборда из журнала, метрик, курсов и предпочтений."""
    return build_overview(conn, settings)


def build_overview(conn, settings) -> Overview:
    today = settings.now().date()
    today_text = today.isoformat()
    week_start = (today - timedelta(days=6)).isoformat()

    streak, best_streak = _streak(conn, today)
    completed_checkins = min(_entries_today(conn, today_text), CHECKINS_TOTAL)

    preferences = {
        "water_goal_ml": pref_int(conn, "water_goal_ml"),
        "workouts_per_week": pref_int(conn, "workouts_per_week"),
        "weight_goal_kg": pref_float(conn, "weight_goal_kg"),
    }

    return Overview(
        date=today_text,
        generated_at=settings.now().isoformat(timespec="seconds"),
        kpis=OverviewKpis(
            streak=StreakKpi(days=streak, bestDays=best_streak),
            checkins=CheckinsKpi(completed=completed_checkins, total=CHECKINS_TOTAL),
            workouts=WorkoutsKpi(
                week=_workouts_week(conn, week_start),
                target=max(1, preferences["workouts_per_week"]),
            ),
            sleep=_sleep_kpi(conn, week_start),
            water=_water_kpi(conn, today_text, preferences["water_goal_ml"]),
            mood=_mood_kpi(conn, week_start),
        ),
        weight=_weight_widget(conn, today, preferences["weight_goal_kg"]),
        course=_continue_course(conn),
        agenda=_agenda(conn, today_text),
        activity=_activity(conn, today),
        insight=_insight(conn, today),
        sync=_sync_status(conn, settings),
        nav=_nav(conn, today_text),
    )


# --------------------------------------------------------------------------
# KPI
# --------------------------------------------------------------------------


def _entries_today(conn, today_text: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM entries WHERE date = ?", (today_text,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _streak(conn, today: date) -> tuple[int, int]:
    """Текущий стрик (дни подряд с записями) и лучший стрик за всё время."""
    rows = conn.execute("SELECT DISTINCT date FROM entries").fetchall()
    days = {str(row[0]) for row in rows if str(row[0])}
    if not days:
        return 0, 0

    cursor = today
    if cursor.isoformat() not in days:
        cursor -= timedelta(days=1)
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)

    best = 0
    current = 0
    previous: date | None = None
    for text in sorted(days):
        day = parse_day(text)
        if day is None:
            continue
        if previous is not None and (day - previous).days == 1:
            current += 1
        else:
            current = 1
        best = max(best, current)
        previous = day
    return streak, max(best, streak)


def _workouts_week(conn, week_start: str) -> int:
    placeholders = ", ".join("?" for _ in TRAINING_KINDS)
    row = conn.execute(
        "SELECT count(*) FROM entries WHERE date >= ? AND kind IN (%s)" % placeholders,
        (week_start, *TRAINING_KINDS),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _sleep_kpi(conn, week_start: str) -> SleepKpi:
    rows = conn.execute(
        "SELECT date, value, unit FROM metrics WHERE kind = 'sleep' AND date >= ? "
        "ORDER BY date, id",
        (week_start,),
    ).fetchall()
    buckets: dict[str, list[float]] = {}
    for row in rows:
        buckets.setdefault(str(row["date"]), []).append(
            sleep_hours(float(row["value"]), str(row["unit"] or ""))
        )
    averages = [sum(values) / len(values) for values in buckets.values() if values]
    avg7 = sum(averages) / len(averages) if averages else 0.0

    latest = conn.execute(
        "SELECT value, unit FROM metrics WHERE kind = 'sleep' "
        "ORDER BY date DESC, id DESC LIMIT 1"
    ).fetchone()
    last_hours = sleep_hours(float(latest["value"]), str(latest["unit"] or "")) if latest else 0.0
    return SleepKpi(lastHours=round(last_hours, 1), avg7Hours=round(avg7, 1))


def _water_kpi(conn, today_text: str, goal_ml: int) -> WaterKpi:
    daily = metric_daily_sum(conn, WATER_KINDS, since=today_text)
    today_ml = daily.get(today_text, 0.0)
    return WaterKpi(ml=round(today_ml, 1), targetMl=float(max(1, goal_ml)))


def _mood_kpi(conn, week_start: str) -> MoodKpi:
    bucketed = metric_daily_avg(conn, "mood", since=week_start)
    values = list(bucketed.values())
    avg7 = round(sum(values) / len(values), 1) if values else 0.0
    latest = latest_metric(conn, "mood")
    score = round(latest[0], 1) if latest is not None else 0.0
    return MoodKpi(score=score, max=10.0, label=_mood_label(score), avg7=avg7)


def _mood_label(score: float) -> str:
    if score <= 0:
        return "нет данных"
    if score <= 2:
        return "тяжело"
    if score <= 4:
        return "так себе"
    if score <= 6:
        return "ровно"
    if score <= 8:
        return "хорошо"
    return "отлично"


def _weight_widget(conn, today: date, goal_kg: float) -> WeightWidget:
    since = (today - timedelta(days=WEIGHT_SERIES_DAYS - 1)).isoformat()
    bucketed = metric_daily_avg(conn, "weight", since=since)
    series = [
        DayPoint(date=day, value=round(value, 2))
        for day, value in sorted(bucketed.items())
    ]

    latest = conn.execute(
        "SELECT value FROM metrics WHERE kind = 'weight' "
        "ORDER BY date DESC, id DESC LIMIT 1"
    ).fetchone()
    current = float(latest["value"]) if latest is not None else 0.0

    threshold = (today - timedelta(days=30)).isoformat()
    previous = conn.execute(
        "SELECT value FROM metrics WHERE kind = 'weight' AND date <= ? "
        "ORDER BY date DESC, id DESC LIMIT 1",
        (threshold,),
    ).fetchone()
    delta30 = round(current - float(previous["value"]), 2) if previous is not None else 0.0

    target = _weight_goal(conn) or goal_kg or 0.0
    return WeightWidget(
        currentKg=round(current, 2),
        targetKg=round(float(target), 2),
        delta30Kg=delta30,
        series=series,
    )


def _weight_goal(conn) -> float | None:
    row = conn.execute(
        "SELECT target FROM goals WHERE status = 'active' AND unit = 'кг' AND target IS NOT NULL "
        "ORDER BY deadline IS NULL, deadline LIMIT 1"
    ).fetchone()
    return float(row["target"]) if row is not None and row["target"] is not None else None


# --------------------------------------------------------------------------
# «Продолжить курс», повестка, активность, инсайт, синк, бейджи
# --------------------------------------------------------------------------


def _continue_course(conn) -> OverviewCourse | None:
    row = conn.execute(
        "SELECT p.lesson_id AS lesson_id, l.idx AS lesson_idx, l.title AS lesson_title, "
        "       c.id AS course_id, c.title AS course_title "
        "FROM lesson_progress p "
        "JOIN lessons l ON l.id = p.lesson_id "
        "JOIN courses c ON c.id = l.course_id "
        "WHERE l.status <> 'done' "
        "ORDER BY p.updated DESC, p.lesson_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT l.id AS lesson_id, l.idx AS lesson_idx, l.title AS lesson_title, "
            "       c.id AS course_id, c.title AS course_title "
            "FROM lessons l JOIN courses c ON c.id = l.course_id "
            "WHERE l.status <> 'done' "
            "ORDER BY c.created DESC, c.id DESC, l.idx LIMIT 1"
        ).fetchone()
    if row is None:
        return None

    counts = conn.execute(
        "SELECT count(*) AS total, "
        "       SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done "
        "FROM lessons WHERE course_id = ?",
        (row["course_id"],),
    ).fetchone()
    total = int(counts["total"] or 0) if counts is not None else 0
    done = int(counts["done"] or 0) if counts is not None else 0
    lesson_title = str(row["lesson_title"])
    if not lesson_title.lower().startswith("урок"):
        lesson_title = "Урок %d · %s" % (int(row["lesson_idx"] or 0), lesson_title)
    return OverviewCourse(
        id=str(row["course_id"]),
        title=str(row["course_title"]),
        lessonTitle=lesson_title,
        doneLessons=done,
        totalLessons=total,
        nextLessonId=str(row["lesson_id"]),
    )


def _agenda(conn, today_text: str) -> list[AgendaItem]:
    items: list[AgendaItem] = []

    for row in conn.execute(
        "SELECT h.id AS id, h.name AS name, "
        "       EXISTS(SELECT 1 FROM habit_marks m WHERE m.habit_id = h.id AND m.date = ?) AS done "
        "FROM habits h WHERE h.archived = 0 ORDER BY h.name LIMIT 3",
        (today_text,),
    ):
        items.append(
            AgendaItem(
                id="habit-%d" % int(row["id"]),
                title=str(row["name"]),
                meta="привычка",
                done=bool(row["done"]),
                kind="habit",
            )
        )

    placeholders = ", ".join("?" for _ in TRAINING_KINDS)
    for row in conn.execute(
        "SELECT id, text FROM entries WHERE date = ? AND kind IN (%s) ORDER BY id LIMIT 2"
        % placeholders,
        (today_text, *TRAINING_KINDS),
    ):
        items.append(
            AgendaItem(
                id="entry-%d" % int(row["id"]),
                title=str(row["text"])[:80],
                meta="тренировка",
                done=True,
                kind="workout",
            )
        )

    for row in conn.execute(
        "SELECT id, title, deadline FROM goals WHERE status = 'active' "
        "ORDER BY deadline IS NULL, deadline LIMIT 2"
    ):
        deadline = row["deadline"]
        items.append(
            AgendaItem(
                id="goal-%d" % int(row["id"]),
                title=str(row["title"]),
                meta="цель · до %s" % deadline if deadline else "цель",
                done=False,
                kind="goal",
            )
        )

    return items[:AGENDA_LIMIT]


def _activity(conn, today: date) -> list[ActivityDay]:
    since = (today - timedelta(days=ACTIVITY_DAYS - 1)).isoformat()
    minutes = entry_daily_minutes(conn, since=since)
    result: list[ActivityDay] = []
    for offset in range(ACTIVITY_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        result.append(
            ActivityDay(
                date=key,
                label=weekday_label(key),
                minutes=round(minutes.get(key, 0.0), 1),
            )
        )
    return result


def _insight(conn, today: date) -> Insight | None:
    pairs = build_correlations(conn, today, days=90)
    for item in pairs:
        if item["status"] != "ok":
            continue
        return Insight(
            title="%s → %s" % (item["left_label"], item["right_label"]),
            text=str(item["sentence"]),
            footnote="Корреляция r = %.2f · n = %d" % (item["r"], item["n"]),
        )
    # Данные есть, но связь слабее порога: это не «мало данных», и молчать об
    # этом нельзя — показываем самую заметную пару и честный коэффициент.
    weak = [
        item
        for item in pairs
        if item["status"] == "weak" and isinstance(item.get("r"), (int, float))
    ]
    if not weak:
        return None
    best = max(weak, key=lambda item: abs(float(item["r"])))
    return Insight(
        title="%s ↔ %s" % (best["left_label"], best["right_label"]),
        text=(
            "%s. Пока связь слабее порога — вывод появится, когда |r| станет ≥ 0,3."
            % (best["sentence"] or "Связь между показателями пока неустойчива")
        ),
        footnote="r = %.2f · n = %d · порог |r| ≥ 0,3" % (best["r"], best["n"]),
    )


def _sync_status(conn, settings) -> SyncStatus:
    row = conn.execute(
        "SELECT ts FROM entries ORDER BY ts DESC, id DESC LIMIT 1"
    ).fetchone()
    at = str(row["ts"]) if row is not None else settings.now().isoformat(timespec="seconds")
    return SyncStatus(state="ok", at=at)


def _nav(conn, today_text: str) -> NavBadges:
    row = conn.execute(
        "SELECT "
        " (SELECT count(*) FROM entries WHERE date = ?) AS today, "
        " (SELECT count(*) FROM habits h WHERE h.archived = 0 AND NOT EXISTS ("
        "    SELECT 1 FROM habit_marks m WHERE m.habit_id = h.id AND m.date = ?)) AS habits, "
        " (SELECT count(*) FROM goals WHERE status = 'active') AS goals, "
        " (SELECT count(DISTINCT c.id) FROM courses c JOIN lessons l ON l.course_id = c.id "
        "  WHERE l.status <> 'done') AS courses",
        (today_text, today_text),
    ).fetchone()
    if row is None:
        return NavBadges()
    return NavBadges(
        today=int(row["today"] or 0),
        habits=int(row["habits"] or 0),
        goals=int(row["goals"] or 0),
        courses=int(row["courses"] or 0),
    )
