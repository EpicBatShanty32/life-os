"""Тесты сидера демо-данных Life OS v3 (app/server/seeder.py).

Проверяется: объём и правдоподобность набора за 120 дней, детерминированность
(один и тот же seed → одинаковые тексты), идемпотентность двойного запуска,
``--reset`` (удаляет только демо-строки), структура курсов с таймкодами,
поиск FTS по засеянному тексту и CLI-поведение на временной БД.

Все базы — во временных каталогах; ``data/lifeos.db`` и Obsidian-vault
тесты не трогают.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from app.server.db import connect, migrate, search_entries, search_segments
from app.server.seeder import (
    DEMO_COURSE_TITLES,
    DEMO_GOALS,
    DEMO_GOAL_TITLES,
    DEMO_HABIT_NAMES,
    DEMO_SOURCE,
    SeedStats,
    clear_demo,
    main,
    resolve_db_path,
    seed,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 11, 17)
DAYS = 120


# ---------------------------------------------------------------------------
# Фикстуры и помощники
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """Пустая база схемы v3 во временном каталоге."""
    conn = connect(tmp_path / "seeder.db")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def seeded(db: sqlite3.Connection) -> sqlite3.Connection:
    """База с демо-набором за 120 дней (фиксированная дата — детерминизм)."""
    seed(db, days=DAYS, today=TODAY)
    return db


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> object:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sql, params).fetchall()]


def count(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> int:
    sql = "SELECT COUNT(*) FROM %s" % table
    if where:
        sql += " WHERE " + where
    return int(scalar(conn, sql, params) or 0)


# ---------------------------------------------------------------------------
# Объём набора
# ---------------------------------------------------------------------------


def test_counts_meet_spec(seeded: sqlite3.Connection) -> None:
    """Метрики приёмки: ≥600 записей, ≥80 замеров веса, ≥50 тренировок, 2 курса."""
    assert count(seeded, "entries") >= 600
    assert count(seeded, "entries", "kind = 'journal'") >= 600
    assert count(seeded, "metrics", "kind = 'weight'") >= 80
    assert count(seeded, "entries", "kind = 'workout'") >= 50
    assert count(seeded, "courses") == 2
    assert count(seeded, "lessons") == 12
    assert count(seeded, "segments") >= 120
    assert count(seeded, "metrics") >= 600


def test_stats_match_database(seeded: sqlite3.Connection, db: sqlite3.Connection) -> None:
    """SeedStats совпадает с фактическим числом строк в таблицах."""
    stats = seed(db, days=DAYS, today=TODAY)
    assert stats.entries == count(db, "entries")
    assert stats.metrics == count(db, "metrics")
    assert stats.courses == count(db, "courses")
    assert stats.lessons == count(db, "lessons")
    assert stats.segments == count(db, "segments")
    assert stats.notes == count(db, "notes")
    assert stats.goals == count(db, "goals")
    assert stats.habits == count(db, "habits")
    assert stats.marks == count(db, "habit_marks")
    assert "курсов" in stats.summary()


def test_all_rows_marked_demo(seeded: sqlite3.Connection) -> None:
    """Демо-строки журнала и метрик помечены source='demo'."""
    assert count(seeded, "entries", "source <> ?", (DEMO_SOURCE,)) == 0
    assert count(seeded, "metrics", "source <> ?", (DEMO_SOURCE,)) == 0


def test_days_parameter_limits_window(db: sqlite3.Connection) -> None:
    """--days 30 даёт ровно 30 календарных дней демо-данных."""
    seed(db, days=30, today=TODAY)
    dates = rows(db, "SELECT DISTINCT date FROM metrics ORDER BY date")
    assert len(dates) == 30
    assert dates[0][0] == "2026-10-19"
    assert dates[-1][0] == TODAY.isoformat()


def test_entries_per_day_within_range(seeded: sqlite3.Connection) -> None:
    """Записей в день — от 6 до 16: журнал живой, но без мусора."""
    per_day = rows(
        seeded,
        "SELECT COUNT(*) FROM entries GROUP BY date ORDER BY 1",
    )
    counts = [int(row[0]) for row in per_day]
    assert len(counts) == DAYS
    assert min(counts) >= 6
    assert max(counts) <= 16


# ---------------------------------------------------------------------------
# Идемпотентность и --reset
# ---------------------------------------------------------------------------


def test_double_run_does_not_duplicate(db: sqlite3.Connection) -> None:
    """Повторный seed не удваивает строки и даёт тот же набор."""
    first = seed(db, days=DAYS, today=TODAY)
    snapshot = rows(db, "SELECT kind, COUNT(*) FROM entries GROUP BY kind ORDER BY kind")
    second = seed(db, days=DAYS, today=TODAY)
    assert first.as_dict() == second.as_dict()
    assert rows(db, "SELECT kind, COUNT(*) FROM entries GROUP BY kind ORDER BY kind") == snapshot
    assert count(db, "courses") == 2
    assert count(db, "habits") == 4


def test_reset_only_removes_demo(db: sqlite3.Connection) -> None:
    """--reset чистит демо-строки и оставляет данные пользователя."""
    with db:
        db.execute(
            "INSERT INTO entries (ts, kind, text, source) VALUES (?, ?, ?, ?)",
            ("2026-11-17T09:00:00+03:00", "journal", "моя запись", "telegram"),
        )
        db.execute(
            "INSERT INTO metrics (date, kind, value, unit, source) VALUES (?, ?, ?, ?, ?)",
            ("2026-11-17", "weight", 80.1, "кг", "telegram"),
        )
        db.execute("INSERT INTO habits (name) VALUES ('Моя привычка')")
        db.execute("INSERT INTO goals (title) VALUES ('Моя цель')")
        db.execute("INSERT INTO courses (title) VALUES ('Мой курс')")
        db.execute("INSERT INTO jobs (kind, status) VALUES ('transcribe', 'queued')")
    seed(db, days=30, today=TODAY)
    removed = clear_demo(db)
    assert removed.entries > 0
    assert removed.courses == 2

    assert count(db, "entries", "source = ?", (DEMO_SOURCE,)) == 0
    assert count(db, "metrics", "source = ?", (DEMO_SOURCE,)) == 0
    assert count(db, "entries", "source = 'telegram'") == 1
    assert count(db, "metrics", "source = 'telegram'") == 1
    assert count(db, "habits") == 1
    assert count(db, "goals") == 1
    assert count(db, "courses") == 1
    assert count(db, "lessons") == 0
    assert count(db, "jobs") == 1


def test_seed_reset_flag_writes_nothing(db: sqlite3.Connection) -> None:
    """seed(..., reset=True) только удаляет: после него демо-строк нет."""
    seed(db, days=20, today=TODAY)
    stats = seed(db, days=20, today=TODAY, reset=True)
    assert stats.entries > 0
    assert count(db, "entries") == 0
    assert count(db, "metrics") == 0
    assert count(db, "courses") == 0
    assert count(db, "habits") == 0


def test_deterministic_content(tmp_path: Path) -> None:
    """Один seed → одинаковые тексты в двух независимых базах."""
    def snapshot(path: Path) -> list[tuple]:
        conn = connect(path)
        try:
            migrate(conn)
            seed(conn, days=45, today=TODAY)
            return rows(conn, "SELECT ts, kind, text, meta_json FROM entries ORDER BY ts, kind, text")
        finally:
            conn.close()

    assert snapshot(tmp_path / "a.db") == snapshot(tmp_path / "b.db")


def test_different_seed_changes_content(db: sqlite3.Connection) -> None:
    """Другое зерно генератора — другой набор текстов."""
    seed(db, days=30, today=TODAY)
    first = rows(db, "SELECT text FROM entries ORDER BY id")
    seed(db, days=30, today=TODAY, seed=7)
    second = rows(db, "SELECT text FROM entries ORDER BY id")
    assert first != second


# ---------------------------------------------------------------------------
# Вес, сон, вода, настроение
# ---------------------------------------------------------------------------


def test_weight_trend_and_noise(seeded: sqlite3.Connection) -> None:
    """Вес: 81.4 → 79.8 по тренду, шум ±0.4, замеров 5 из 7 дней."""
    series = rows(
        seeded,
        "SELECT date, value FROM metrics WHERE kind = 'weight' ORDER BY date",
    )
    assert len(series) >= 80
    assert series[0][0] < series[-1][0]
    values = [float(value) for _, value in series]
    assert 78.6 <= min(values) <= 82.4
    assert 78.6 <= max(values) <= 82.4

    steps = len(values) - 1
    for index, (_, value) in enumerate(series):
        trend = 81.4 - 1.6 * (index / steps)
        assert abs(float(value) - trend) <= 0.45
    assert abs(values[0] - 81.4) <= 0.5
    assert abs(values[-1] - 79.8) <= 0.5


def test_body_composition_ranges(seeded: sqlite3.Connection) -> None:
    """Жир 18.5→17.2, мышцы 34.8→35.3, вода в организме — правдоподобные."""
    fat = rows(seeded, "SELECT value FROM metrics WHERE kind = 'fat' ORDER BY date")
    muscle = rows(seeded, "SELECT value FROM metrics WHERE kind = 'muscle' ORDER BY date")
    water = rows(seeded, "SELECT value FROM metrics WHERE kind = 'body_water' ORDER BY date")
    assert len(fat) == len(muscle) == len(water) >= 80
    assert 16.9 <= float(fat[0][0]) <= 18.8
    assert 16.6 <= float(fat[-1][0]) <= 17.7
    assert 34.5 <= float(muscle[0][0]) <= 35.1
    assert 35.0 <= float(muscle[-1][0]) <= 35.6
    assert 54.0 <= float(water[0][0]) <= 56.5
    assert 55.0 <= float(water[-1][0]) <= 57.5


def test_sleep_water_mood_energy(seeded: sqlite3.Connection) -> None:
    """Сон 6.2–8.0 ч с плохими ночами, вода 1200–2600 мл, настроение 2–5."""
    sleep = [float(row[0]) for row in rows(seeded, "SELECT value FROM metrics WHERE kind = 'sleep'")]
    water = [float(row[0]) for row in rows(seeded, "SELECT value FROM metrics WHERE kind = 'water'")]
    mood = [float(row[0]) for row in rows(seeded, "SELECT value FROM metrics WHERE kind = 'mood'")]
    energy = [float(row[0]) for row in rows(seeded, "SELECT value FROM metrics WHERE kind = 'energy'")]
    assert len(sleep) == len(water) == len(mood) == len(energy) == DAYS
    assert min(sleep) >= 6.2 and max(sleep) <= 8.0
    assert sleep.count(6.2) >= 5  # редкие плохие ночи
    assert min(water) >= 1200 and max(water) <= 2600
    assert min(mood) >= 2 and max(mood) <= 5
    assert min(energy) >= 2 and max(energy) <= 5


# ---------------------------------------------------------------------------
# Питание, тренировки, привычки, цели
# ---------------------------------------------------------------------------


def test_diary_has_macros_and_products(seeded: sqlite3.Connection) -> None:
    """Питание: 3–4 приёма в день, КБЖУ из продукта, 1800–2400 ккал в день."""
    diary = rows(
        seeded,
        "SELECT date, meal, text, grams, kcal, protein, fat, carbs, product_id "
        "FROM food_log ORDER BY date, ts",
    )
    assert len(diary) >= 240
    assert min(int(row[0]) for row in rows(
        seeded, "SELECT COUNT(DISTINCT meal) FROM food_log GROUP BY date"
    )) >= 3
    assert max(int(row[0]) for row in rows(
        seeded, "SELECT COUNT(DISTINCT meal) FROM food_log GROUP BY date"
    )) <= 4

    days = rows(
        seeded,
        "SELECT date, ROUND(SUM(kcal)) FROM food_log GROUP BY date ORDER BY date",
    )
    assert 60 <= len(days) <= 90
    for _, kcal in days:
        assert 1800 <= float(kcal) <= 2400

    for _, meal, text, grams, kcal, protein, fat, carbs, product_id in diary:
        assert meal in ("breakfast", "lunch", "dinner", "snack")
        assert text and grams > 0
        assert 0 <= kcal <= 1500
        assert 0 <= protein <= 200 and 0 <= fat <= 200 and 0 <= carbs <= 300
        assert product_id is not None

    # «Пищевых» записей в журнале больше нет — еда живёт в дневнике.
    assert count(seeded, "entries", "kind = 'food'") == 0
    # Продукты справочника — ~120 позиций, у всех КБЖУ непустые.
    assert count(seeded, "products") >= 120
    assert count(seeded, "products", "kcal <= 0") == 0


def test_workouts_three_per_week_with_progress(seeded: sqlite3.Connection) -> None:
    """Тренировки: 3 в неделю, три упражнения, жим растёт 70→80 кг."""
    workouts = rows(
        seeded,
        "SELECT date, text, meta_json FROM entries WHERE kind = 'workout' ORDER BY date",
    )
    assert len(workouts) >= 50
    per_week: dict[str, int] = {}
    for date_str, _, _ in workouts:
        week = date.fromisoformat(str(date_str)).isocalendar()[:2]
        key = "%s-%s" % week
        per_week[key] = per_week.get(key, 0) + 1
    assert set(per_week.values()) <= {1, 2, 3}
    full_weeks = sorted(per_week)[1:-1]  # крайние недели окна обрезаны
    assert full_weeks
    assert {per_week[key] for key in full_weeks} == {3}

    bench = [
        json.loads(raw)["weight_kg"]
        for _, text, raw in workouts
        if text.startswith("Жим лёжа")
    ]
    assert len(bench) >= 15
    assert 70.0 <= bench[0] <= 75.0
    assert 77.5 <= bench[-1] <= 80.0
    assert max(bench) - min(bench) >= 5.0
    for _, _, raw in workouts:
        meta = json.loads(raw)
        assert meta["exercise"] in {"bench", "squat", "deadlift"}
        assert 3 <= meta["sets"] <= 4
        assert meta["duration_min"] >= 40


def test_habits_and_marks(seeded: sqlite3.Connection) -> None:
    """4 привычки, отметки примерно на 80% дней, без дублей на день."""
    habits = rows(seeded, "SELECT id, name, emoji, target_per_week FROM habits ORDER BY id")
    assert len(habits) == 4
    assert [row[1] for row in habits] == list(DEMO_HABIT_NAMES)
    for habit_id, _, emoji, target in habits:
        assert emoji
        assert 1 <= int(target) <= 7
        marks = count(seeded, "habit_marks", "habit_id = ?", (habit_id,))
        assert 0.7 * DAYS <= marks <= 0.9 * DAYS
    total = count(seeded, "habit_marks")
    assert 0.7 * DAYS * 4 <= total <= 0.9 * DAYS * 4
    duplicates = rows(
        seeded,
        "SELECT habit_id, date, COUNT(*) FROM habit_marks GROUP BY habit_id, date "
        "HAVING COUNT(*) > 1",
    )
    assert duplicates == []


def test_goals_have_progress(seeded: sqlite3.Connection) -> None:
    """Три цели: текущее значение между стартом и целью, срок в будущем."""
    goals = rows(
        seeded,
        "SELECT title, target, current, unit, deadline, status FROM goals ORDER BY id",
    )
    assert [row[0] for row in goals] == list(DEMO_GOAL_TITLES)
    starts = {title: start for title, _, start, _ in DEMO_GOALS}
    for title, target, current, unit, deadline, status in goals:
        start = starts[title]
        assert unit
        assert status == "active"
        # текущее значение лежит между стартом и целью, но ни одного не достигло
        assert min(start, target) <= current <= max(start, target)
        assert current not in (start, target)
        assert date.fromisoformat(str(deadline)) > TODAY


# ---------------------------------------------------------------------------
# Курсы
# ---------------------------------------------------------------------------


def test_courses_structure(seeded: sqlite3.Connection) -> None:
    """2 курса по 6 уроков с нумерацией, длительностью и категорией."""
    courses = rows(seeded, "SELECT id, title, category FROM courses ORDER BY id")
    assert [row[1] for row in courses] == list(DEMO_COURSE_TITLES)
    for course_id, _, category in courses:
        assert category
        lessons = rows(
            seeded,
            "SELECT idx, title, duration_s FROM lessons WHERE course_id = ? ORDER BY idx",
            (course_id,),
        )
        assert [int(row[0]) for row in lessons] == [1, 2, 3, 4, 5, 6]
        for _, title, duration in lessons:
            assert title
            assert 600 <= float(duration) <= 1300


def test_segments_have_timecodes(seeded: sqlite3.Connection) -> None:
    """У каждого урока 12+ сегментов с возрастающими таймкодами и текстом."""
    lessons = rows(seeded, "SELECT id, duration_s FROM lessons ORDER BY id")
    for lesson_id, duration in lessons:
        segments = rows(
            seeded,
            "SELECT start_s, end_s, text FROM segments WHERE lesson_id = ? ORDER BY start_s",
            (lesson_id,),
        )
        assert len(segments) >= 12
        starts = [float(row[0]) for row in segments]
        assert starts == sorted(starts)
        assert starts[0] >= 0
        assert starts[-1] < float(duration) + 30
        for start, end, text in segments:
            assert float(end) > float(start)
            assert len(str(text).strip()) > 10


def test_transcripts_notes_and_progress(seeded: sqlite3.Connection) -> None:
    """Расшифровки у всех уроков, заметки и частичный прогресс просмотра."""
    assert count(seeded, "transcripts") == 12
    assert count(seeded, "notes") >= 6
    progress = rows(
        seeded,
        "SELECT l.idx, c.title, p.position_s, p.watched_s FROM lesson_progress AS p "
        "JOIN lessons AS l ON l.id = p.lesson_id JOIN courses AS c ON c.id = l.course_id",
    )
    assert len(progress) >= 4
    for idx, title, position, watched in progress:
        assert float(position) > 0
        assert float(watched) >= float(position)
        assert title in DEMO_COURSE_TITLES


def test_lesson_files_ready_for_vault(seeded: sqlite3.Connection) -> None:
    """Данные курса пригодны для vault: непустые заголовки и сегменты по уроку."""
    lesson = rows(
        seeded,
        "SELECT l.id, l.title, c.title FROM lessons AS l JOIN courses AS c ON c.id = l.course_id "
        "ORDER BY l.id LIMIT 1",
    )[0]
    assert "/" not in lesson[1] and ":" not in lesson[1]
    assert count(seeded, "segments", "lesson_id = ?", (lesson[0],)) >= 12


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------


def test_fts_finds_seeded_text(seeded: sqlite3.Connection) -> None:
    """FTS находит засеянные слова в журнале и в субтитрах уроков."""
    assert len(search_entries(seeded, "жим")) >= 10
    assert len(search_entries(seeded, "EMDR")) >= 1
    assert len(search_entries(seeded, "прогулка")) >= 1
    assert len(search_segments(seeded, "EMDR")) >= 5
    assert len(search_segments(seeded, "нутрициология")) >= 3
    assert search_segments(seeded, "нетакогослова") == []


def test_entries_have_sorted_timestamps(seeded: sqlite3.Connection) -> None:
    """Времена записей монотонны по дням и содержат смещение +03:00."""
    stamps = [str(row[0]) for row in rows(seeded, "SELECT ts FROM entries ORDER BY ts")]
    assert stamps == sorted(stamps)
    assert all(len(stamp) >= 19 for stamp in stamps)
    assert all(stamp.endswith(("+03:00", "Z")) for stamp in stamps)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_seed_and_reset(tmp_path: Path) -> None:
    """CLI: засев на временной БД через LIFEOS_DB_PATH, затем --reset."""
    db_path = tmp_path / "cli.db"
    env = dict(os.environ)
    env["LIFEOS_DB_PATH"] = str(db_path)
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    seeded = subprocess.run(
        [sys.executable, "-m", "app.server.seeder", "--days", "30"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert seeded.returncode == 0, seeded.stderr
    assert "Засеяно за 30 дней" in seeded.stdout
    assert db_path.is_file()

    conn = connect(db_path)
    try:
        assert int(scalar(conn, "SELECT COUNT(*) FROM entries") or 0) > 0
        assert int(scalar(conn, "SELECT COUNT(*) FROM courses") or 0) == 2
    finally:
        conn.close()

    reset = subprocess.run(
        [sys.executable, "-m", "app.server.seeder", "--days", "30", "--reset"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert reset.returncode == 0, reset.stderr
    assert "Удалены демо-данные" in reset.stdout

    conn = connect(db_path)
    try:
        assert int(scalar(conn, "SELECT COUNT(*) FROM entries") or 0) == 0
        assert int(scalar(conn, "SELECT COUNT(*) FROM metrics") or 0) == 0
    finally:
        conn.close()


def test_cli_rejects_bad_days(tmp_path: Path) -> None:
    """CLI: --days 0 — ошибка ввода, код 2, база не создаётся."""
    db_path = tmp_path / "bad.db"
    assert main(["--days", "0", "--db", str(db_path)]) == 2
    assert not db_path.exists()


def test_cli_legacy_database_guard(tmp_path: Path) -> None:
    """CLI: на базе v0.x возвращает код 2 и не перезаписывает файл."""
    legacy_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(legacy_path))
    try:
        legacy.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT)")
        legacy.execute("INSERT INTO messages (text) VALUES ('старое сообщение')")
        legacy.commit()
    finally:
        legacy.close()

    assert main(["--days", "5", "--db", str(legacy_path)]) == 2

    legacy = sqlite3.connect(str(legacy_path))
    try:
        assert legacy.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        legacy.close()


def test_resolve_db_path_prefers_argument(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Путь к БД: --db важнее LIFEOS_DB_PATH."""
    monkeypatch.setenv("LIFEOS_DB_PATH", str(tmp_path / "env.db"))
    assert resolve_db_path(tmp_path / "arg.db") == tmp_path / "arg.db"
    assert resolve_db_path() == tmp_path / "env.db"


def test_cli_quiet_and_stats(tmp_path: Path) -> None:
    """--quiet не печатает статистику, но данные пишет."""
    from app.server.db import connect as db_connect

    path = tmp_path / "quiet.db"
    assert main(["--days", "10", "--db", str(path), "--quiet"]) == 0
    conn = db_connect(path)
    try:
        assert int(scalar(conn, "SELECT COUNT(*) FROM lessons") or 0) == 12
    finally:
        conn.close()


def test_seed_does_not_touch_other_files(tmp_path: Path, db: sqlite3.Connection) -> None:
    """Сидер пишет только БД: рядом не появляются каталоги vault/media."""
    seed(db, days=10, today=TODAY)
    created = sorted(item.name for item in tmp_path.iterdir())
    assert created == ["seeder.db", "seeder.db-shm", "seeder.db-wal"] or created == ["seeder.db"]
    assert not (tmp_path / "vault").exists()
    assert not (tmp_path / "media").exists()


def test_stats_dataclass_roundtrip() -> None:
    """SeedStats: as_dict и summary согласованы между собой."""
    stats = SeedStats(entries=3, metrics=2, courses=1, lessons=6)
    payload = stats.as_dict()
    assert payload["entries"] == 3
    assert payload["lessons"] == 6
    assert isinstance(stats.summary(), str)
    assert "3 записей" in stats.summary()
