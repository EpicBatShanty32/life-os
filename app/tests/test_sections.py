"""Тесты универсальных разделов дашборда (app/server/api/sections.py).

Проверяется: форма ответа для каждого раздела, агрегация метрик (среднее против
суммы), окно дней, поиск по журналу, отметки привычек и серии дней, проценты
целей, а также запись — отметка привычки, новая привычка, цель и её правка.

Все базы — во временных каталогах, ``data/lifeos.db`` не затрагивается.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.server.api import sections
from app.server.api.sections import (
    DEFAULT_DAYS,
    SECTION_NAMES,
    SECTIONS,
    HabitIn,
    HabitMarkIn,
    GoalIn,
    GoalPatch,
    build_section,
    create_goal,
    create_habit,
    get_section,
    mark_habit,
    patch_goal,
)
from app.server.db import init_db

TODAY = date(2026, 11, 17)


@pytest.fixture()
def conn(tmp_path: Path):
    connection = init_db(tmp_path / "sections.db")
    try:
        yield connection
    finally:
        connection.close()


def _metric(conn: sqlite3.Connection, day: str, kind: str, value: float, unit: str = "") -> None:
    conn.execute(
        "INSERT INTO metrics (date, kind, value, unit) VALUES (?, ?, ?, ?)",
        (day, kind, value, unit),
    )


def _entry(conn: sqlite3.Connection, ts: str, kind: str, text: str) -> int:
    cursor = conn.execute(
        "INSERT INTO entries (ts, kind, text, source, meta_json) VALUES (?, ?, ?, 'web', '{}')",
        (ts, kind, text),
    )
    return int(cursor.lastrowid or 0)


def _habit(conn: sqlite3.Connection, name: str, emoji: str = "", target: int = 7) -> int:
    cursor = conn.execute(
        "INSERT INTO habits (name, emoji, target_per_week) VALUES (?, ?, ?)",
        (name, emoji, target),
    )
    return int(cursor.lastrowid or 0)


def _goal(conn: sqlite3.Connection, title: str, target: float | None, current: float) -> int:
    cursor = conn.execute(
        "INSERT INTO goals (title, target, current, unit) VALUES (?, ?, ?, 'кг')",
        (title, target, current),
    )
    return int(cursor.lastrowid or 0)


# --------------------------------------------------------------------------
# Форма ответа
# --------------------------------------------------------------------------


def test_every_section_has_title_and_shape(conn) -> None:
    for name in SECTION_NAMES:
        payload = build_section(conn, name, today=TODAY)
        assert payload["name"] == name
        assert payload["title"] == SECTIONS[name]["title"]
        assert isinstance(payload["kpis"], list)
        assert isinstance(payload["series"], list)
        assert isinstance(payload["items"], list)
        assert payload["days"] == DEFAULT_DAYS
        assert payload["empty"] is True, "пустая база — пустой раздел: %s" % name


def test_unknown_section_raises() -> None:
    with pytest.raises(KeyError):
        build_section(None, "нет-такого")  # type: ignore[arg-type]


def test_router_returns_404_for_unknown_section(conn) -> None:
    with pytest.raises(HTTPException) as info:
        get_section("нет-такого", conn)
    assert info.value.status_code == 404


# --------------------------------------------------------------------------
# Метрики: среднее и сумма
# --------------------------------------------------------------------------


def test_sleep_is_averaged_per_day(conn) -> None:
    _metric(conn, "2026-11-16", "sleep", 7.0, "ч")
    _metric(conn, "2026-11-16", "sleep", 8.0, "ч")
    payload = build_section(conn, "sleep", today=TODAY, days=7)
    series = payload["series"][0]
    assert series["key"] == "sleep"
    assert series["points"] == [{"date": "2026-11-16", "value": 7.5}]
    assert series["unit"] == "ч"
    assert payload["kpis"][0]["value"] == 7.5


def test_water_is_summed_per_day(conn) -> None:
    _metric(conn, "2026-11-16", "water", 500.0, "мл")
    _metric(conn, "2026-11-16", "water", 700.0, "мл")
    _metric(conn, "2026-11-16", "water", 300.0, "мл")
    payload = build_section(conn, "nutrition", today=TODAY, days=7)
    series = payload["series"][0]
    assert series["key"] == "water"
    assert series["points"][0]["value"] == 1500.0
    assert series["total"] == 1500.0


def test_days_window_limits_series(conn) -> None:
    _metric(conn, "2026-11-17", "sleep", 7.0, "ч")
    _metric(conn, "2026-11-01", "sleep", 5.0, "ч")  # вне 7-дневного окна
    payload = build_section(conn, "sleep", today=TODAY, days=7)
    dates = [point["date"] for point in payload["series"][0]["points"]]
    assert dates == ["2026-11-17"]
    assert payload["since"] == "2026-11-11"


def test_days_is_clamped(conn) -> None:
    _metric(conn, "2026-11-17", "sleep", 7.0, "ч")
    assert build_section(conn, "sleep", today=TODAY, days=0)["days"] == 1
    assert build_section(conn, "sleep", today=TODAY, days=9999)["days"] == sections.MAX_DAYS


def test_series_without_unit_is_still_valid(conn) -> None:
    _metric(conn, "2026-11-17", "mood", 4.0)
    payload = build_section(conn, "mood", today=TODAY, days=7)
    assert payload["series"][0]["unit"] == ""


# --------------------------------------------------------------------------
# Записи и поиск
# --------------------------------------------------------------------------


def test_journal_lists_entries_newest_first(conn) -> None:
    _entry(conn, "2026-11-16T09:00:00Z", "journal", "первая")
    _entry(conn, "2026-11-17T09:00:00Z", "journal", "вторая")
    _entry(conn, "2026-11-17T10:00:00Z", "food", "не журнал")
    payload = build_section(conn, "journal", today=TODAY, days=30)
    assert [item["text"] for item in payload["items"]] == ["вторая", "первая"]
    assert payload["count"] == 2
    assert payload["items"][0]["time"] == "09:00"


def test_journal_search_filters(conn) -> None:
    _entry(conn, "2026-11-16T09:00:00Z", "journal", "купил кроссовки")
    _entry(conn, "2026-11-17T09:00:00Z", "journal", "пробежка 5 км")
    payload = build_section(conn, "journal", today=TODAY, days=30, search="кроссов")
    assert [item["text"] for item in payload["items"]] == ["купил кроссовки"]


def test_journal_search_escapes_nothing_but_stays_safe(conn) -> None:
    _entry(conn, "2026-11-17T09:00:00Z", "journal", "100% готово")
    payload = build_section(conn, "journal", today=TODAY, days=30, search="100%")
    assert len(payload["items"]) == 1


def test_limit_is_respected(conn) -> None:
    for index in range(5):
        _entry(conn, "2026-11-17T09:0%d:00Z" % index, "journal", "запись %d" % index)
    payload = build_section(conn, "journal", today=TODAY, days=30, limit=2)
    assert len(payload["items"]) == 2
    assert payload["items"][0]["text"] == "запись 4"


# --------------------------------------------------------------------------
# Привычки
# --------------------------------------------------------------------------


def test_habits_marks_and_streak(conn) -> None:
    habit_id = _habit(conn, "Вода", "💧")
    for day in ("2026-11-15", "2026-11-16", "2026-11-17"):
        conn.execute(
            "INSERT INTO habit_marks (habit_id, date, value) VALUES (?, ?, 1)",
            (habit_id, day),
        )
    payload = build_section(conn, "habits", today=TODAY)
    habit = payload["habits"][0]
    assert habit["name"] == "Вода"
    assert habit["marks"] == [False, False, False, False, True, True, True]
    assert habit["doneWeek"] == 3
    assert habit["streak"] == 3
    assert habit["week"][-1] == "2026-11-17"
    assert payload["empty"] is False


def test_habit_streak_breaks_on_gap(conn) -> None:
    habit_id = _habit(conn, "Чтение")
    conn.execute(
        "INSERT INTO habit_marks (habit_id, date, value) VALUES (?, '2026-11-17', 1)",
        (habit_id,),
    )
    conn.execute(
        "INSERT INTO habit_marks (habit_id, date, value) VALUES (?, '2026-11-15', 1)",
        (habit_id,),
    )
    payload = build_section(conn, "habits", today=TODAY)
    assert payload["habits"][0]["streak"] == 1


def test_zero_value_does_not_count(conn) -> None:
    habit_id = _habit(conn, "Зарядка")
    conn.execute(
        "INSERT INTO habit_marks (habit_id, date, value) VALUES (?, '2026-11-17', 0)",
        (habit_id,),
    )
    payload = build_section(conn, "habits", today=TODAY)
    assert payload["habits"][0]["streak"] == 0
    assert payload["habits"][0]["doneWeek"] == 0


def test_archived_habits_hidden(conn) -> None:
    habit_id = _habit(conn, "Старая")
    conn.execute("UPDATE habits SET archived = 1 WHERE id = ?", (habit_id,))
    payload = build_section(conn, "habits", today=TODAY)
    assert payload["habits"] == []
    assert payload["empty"] is True


def test_mark_habit_toggles(conn) -> None:
    habit_id = _habit(conn, "Вода")
    mark_habit(habit_id, HabitMarkIn(day="2026-11-17", value=1), conn)
    assert build_section(conn, "habits", today=TODAY)["habits"][0]["doneWeek"] == 1
    mark_habit(habit_id, HabitMarkIn(day="2026-11-17", value=0), conn)
    assert build_section(conn, "habits", today=TODAY)["habits"][0]["doneWeek"] == 0


def test_mark_habit_upsert_is_idempotent(conn) -> None:
    habit_id = _habit(conn, "Вода")
    mark_habit(habit_id, HabitMarkIn(day="2026-11-17", value=1), conn)
    mark_habit(habit_id, HabitMarkIn(day="2026-11-17", value=1), conn)
    rows = conn.execute("SELECT count(*) FROM habit_marks").fetchone()[0]
    assert rows == 1


def test_mark_missing_habit_is_404(conn) -> None:
    with pytest.raises(HTTPException) as info:
        mark_habit(999, HabitMarkIn(), conn)
    assert info.value.status_code == 404


def test_create_habit_is_idempotent_by_name(conn) -> None:
    first = create_habit(HabitIn(name="Медитация", emoji="🧘", target_per_week=5), conn)
    second = create_habit(HabitIn(name="Медитация"), conn)
    assert first["id"] == second["id"]
    assert second["target"] == 5
    assert conn.execute("SELECT count(*) FROM habits").fetchone()[0] == 1


# --------------------------------------------------------------------------
# Цели
# --------------------------------------------------------------------------


def test_goals_percent_and_order(conn) -> None:
    _goal(conn, "Вес 70", 70.0, 74.0)
    _goal(conn, "Книга", 10.0, 10.0)
    payload = build_section(conn, "goals", today=TODAY)
    goals = payload["goals"]
    assert [goal["title"] for goal in goals] == ["Вес 70", "Книга"]
    assert goals[0]["percent"] == 100  # 74/70 → ограничено 100
    assert goals[1]["percent"] == 100


def test_goal_without_target_has_zero_percent(conn) -> None:
    _goal(conn, "Без цели", None, 5.0)
    goal = build_section(conn, "goals", today=TODAY)["goals"][0]
    assert goal["target"] is None
    assert goal["percent"] == 0


def test_create_and_patch_goal(conn) -> None:
    created = create_goal(GoalIn(title="Отжимания", target=100, unit="раз"), conn)
    assert created["status"] == "active"
    updated = patch_goal(created["id"], GoalPatch(current=40), conn)
    assert updated["current"] == 40.0
    done = patch_goal(created["id"], GoalPatch(status="done"), conn)
    assert done["status"] == "done"
    assert build_section(conn, "goals", today=TODAY)["goals"][0]["percent"] == 40


def test_patch_missing_goal_is_404(conn) -> None:
    with pytest.raises(HTTPException) as info:
        patch_goal(404, GoalPatch(current=1), conn)
    assert info.value.status_code == 404


def test_patch_without_changes_is_400(conn) -> None:
    goal_id = _goal(conn, "Вес", 70.0, 71.0)
    with pytest.raises(HTTPException) as info:
        patch_goal(goal_id, GoalPatch(), conn)
    assert info.value.status_code == 400


# --------------------------------------------------------------------------
# Сегодня и аналитика
# --------------------------------------------------------------------------


def test_today_aggregates_several_kinds(conn) -> None:
    _metric(conn, "2026-11-17", "sleep", 8.0, "ч")
    _metric(conn, "2026-11-17", "water", 900.0, "мл")
    _entry(conn, "2026-11-17T08:00:00Z", "journal", "доброе утро")
    payload = build_section(conn, "today", today=TODAY, days=30)
    keys = {item["key"] for item in payload["series"]}
    assert keys == {"sleep", "water"}
    assert [item["text"] for item in payload["items"]] == ["доброе утро"]


def test_analytics_has_several_series(conn) -> None:
    for kind, unit in (
        ("sleep", "ч"),
        ("mood", "балл"),
        ("energy", "балл"),
        ("weight", "кг"),
        ("water", "мл"),
    ):
        _metric(conn, "2026-11-17", kind, 5.0, unit)
    payload = build_section(conn, "analytics", today=TODAY, days=30)
    assert len(payload["series"]) == 5
    assert len(payload["kpis"]) == 5
