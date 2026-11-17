"""Тесты API «Личное» и сводной аналитики (§12.5–12.6).

Проверяется: upsert чек-ина по дате, привычки с отметками и серией, счётчики
задач дня, страницы чтения, энергия за 7 дней; факты итогов (неделя/месяц),
незавершённые задачи, сохранение и история итогов; сводная аналитика —
энергия, сферы, чтение, деньги, привычки, задачи и корреляции, не ломая
существующий ``/api/analytics/correlations``.

База — во временном каталоге (``tmp_path``), рабочая ``data/`` не затрагивается.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
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


def _exec(settings, sql: str, params: tuple = ()) -> int:
    conn = connect(settings.db_path)
    try:
        with conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def _fetch(settings, sql: str, params: tuple = ()):
    conn = connect(settings.db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _count(settings, table: str, where: str = "", params: tuple = ()) -> int:
    row = _fetch(settings, "SELECT count(*) FROM %s %s" % (table, where), params)
    return int(row[0][0]) if row else -1


def _task(
    settings,
    title: str,
    *,
    due: str | None = None,
    status: str = "todo",
    resource: str = "important",
    start_at: str | None = None,
    sphere_id: int | None = None,
    done_at: str | None = None,
) -> int:
    return _exec(
        settings,
        "INSERT INTO tasks (title, due_date, status, resource, start_at, sphere_id, done_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title, due, status, resource, start_at, sphere_id, done_at),
    )


def _book(
    settings,
    title: str,
    *,
    pages: int = 100,
    pages_read: int | None = None,
    shelf: str = "done",
    finished_at: str | None = None,
) -> int:
    return _exec(
        settings,
        "INSERT INTO books (title, shelf, pages_total, pages_read, finished_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (title, shelf, pages, pages if pages_read is None else pages_read, finished_at),
    )


def _habit(settings, name: str, *, color: str = "#A18BB7", target: int = 7, archived: int = 0) -> int:
    return _exec(
        settings,
        "INSERT INTO habits (name, color, target_per_week, archived) VALUES (?, ?, ?, ?)",
        (name, color, target, archived),
    )


def _mark(settings, habit_id: int, day: str, value: float = 1) -> int:
    return _exec(
        settings,
        "INSERT INTO habit_marks (habit_id, date, value) VALUES (?, ?, ?)",
        (habit_id, day, value),
    )


def _checkin_sql(settings, day: str, energy: int) -> int:
    return _exec(
        settings,
        "INSERT INTO checkins (date, energy, mood, note) VALUES (?, ?, '', '') "
        "ON CONFLICT(date) DO UPDATE SET energy = excluded.energy",
        (day, energy),
    )


def _t(settings) -> date:
    return date.fromisoformat(settings.today())


def _week_start(settings) -> date:
    today = _t(settings)
    return today - timedelta(days=today.weekday())


def _iso(day: date) -> str:
    return day.isoformat()


# --------------------------------------------------------------------------
# Сводка дня
# --------------------------------------------------------------------------


def test_personal_summary_empty(client: TestClient, settings) -> None:
    payload = client.get("/api/personal/summary").json()
    assert payload["date"] == settings.today()
    assert payload["checkin"] is None
    assert payload["habits"] == []
    assert payload["tasks"] == {"must": 0, "important": 0, "low": 0, "done": 0, "total": 0}
    assert payload["reading"] == {"pagesToday": 0, "pagesWeek": 0}
    assert len(payload["energy7"]) == 7
    assert payload["energy7"][-1] == {"date": settings.today(), "value": None}
    assert all(point["value"] is None for point in payload["energy7"])


def test_checkin_upsert(client: TestClient, settings) -> None:
    today = settings.today()
    first = client.post(
        "/api/personal/checkin",
        json={"date": today, "energy": 3, "mood": "ровно", "note": "после обеда"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["energy"] == 3
    assert first.json()["mood"] == "ровно"

    second = client.post(
        "/api/personal/checkin",
        json={"date": today, "energy": 5, "mood": "в потоке", "note": "к вечеру лучше"},
    )
    assert second.status_code == 200
    assert second.json()["energy"] == 5
    assert _count(settings, "checkins") == 1

    summary = client.get("/api/personal/summary").json()
    assert summary["checkin"]["energy"] == 5
    assert summary["checkin"]["mood"] == "в потоке"
    assert summary["checkin"]["date"] == today

    yesterday = _iso(_t(settings) - timedelta(days=1))
    client.post("/api/personal/checkin", json={"date": yesterday, "energy": 2})
    assert _count(settings, "checkins") == 2

    # Без даты — сегодня (upsert в ту же строку).
    client.post("/api/personal/checkin", json={"energy": 1})
    assert _count(settings, "checkins") == 2
    assert client.get("/api/personal/summary").json()["checkin"]["energy"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"energy": 0},
        {"energy": 6},
        {"energy": "много"},
        {"energy": 3, "date": "17.11.2026"},
        {"energy": 3, "mood": "ж" * 61},
    ],
)
def test_checkin_validation(client: TestClient, payload: dict) -> None:
    assert client.post("/api/personal/checkin", json=payload).status_code == 422


def test_summary_habits_marks_and_streak(client: TestClient, settings) -> None:
    today = _t(settings)
    sleep = _habit(settings, "Сон 7+", color="#5b8cff", target=7)
    walk = _habit(settings, "Прогулка", target=5)
    _habit(settings, "Архивная", archived=1)

    _mark(settings, sleep, _iso(today))
    _mark(settings, sleep, _iso(today - timedelta(days=1)))
    _mark(settings, sleep, _iso(today - timedelta(days=10)))
    _mark(settings, walk, _iso(today))

    habits = {item["name"]: item for item in client.get("/api/personal/summary").json()["habits"]}
    assert set(habits) == {"Сон 7+", "Прогулка"}

    sleep_item = habits["Сон 7+"]
    assert sleep_item["color"] == "#5b8cff"
    assert sleep_item["targetPerWeek"] == 7
    assert sleep_item["streak"] == 2
    assert sleep_item["doneWeek"] == 2
    assert sleep_item["marks"] == {_iso(today): 1, _iso(today - timedelta(days=1)): 1}
    assert len(sleep_item["week"]) == 7
    assert sleep_item["week"][-1] == _iso(today)
    # Отметка 10 дней назад в недельное окно не попадает.
    assert _iso(today - timedelta(days=10)) not in sleep_item["marks"]

    assert habits["Прогулка"]["streak"] == 1
    assert habits["Прогулка"]["targetPerWeek"] == 5


def test_summary_tasks_counts(client: TestClient, settings) -> None:
    today = settings.today()
    tomorrow = _iso(_t(settings) + timedelta(days=1))
    _task(settings, "Жёсткое: врач", due=today, resource="must")
    _task(settings, "Гибкое: отчёт", due=today, resource="important")
    _task(settings, "Мелкое: полка", due=today, resource="low")
    _task(settings, "Готово: письмо", due=today, resource="important", status="done")
    _task(settings, "Отменено", due=today, resource="must", status="cancelled")
    _task(settings, "Завтра", due=tomorrow)
    _task(settings, "Событие", start_at=today + "T10:00:00+03:00", resource="must")

    counts = client.get("/api/personal/summary").json()["tasks"]
    assert counts == {"must": 2, "important": 1, "low": 1, "done": 1, "total": 5}


def test_summary_reading_pages(client: TestClient, settings) -> None:
    today = _t(settings)
    _book(settings, "Сегодня", pages=320, finished_at=_iso(today))
    _book(settings, "Вчера", pages=100, finished_at=_iso(today - timedelta(days=1)))
    _book(settings, "Давно", pages=50, finished_at=_iso(today - timedelta(days=10)))
    _book(settings, "Читаю", pages=200, pages_read=40, shelf="reading", finished_at=None)

    reading = client.get("/api/personal/summary").json()["reading"]
    assert reading["pagesToday"] == 320
    assert reading["pagesWeek"] == 420


def test_summary_energy7(client: TestClient, settings) -> None:
    today = _t(settings)
    _checkin_sql(settings, _iso(today), 4)
    _checkin_sql(settings, _iso(today - timedelta(days=3)), 2)

    series = client.get("/api/personal/summary").json()["energy7"]
    assert len(series) == 7
    assert series[-1] == {"date": _iso(today), "value": 4}
    values = {point["date"]: point["value"] for point in series}
    assert values[_iso(today - timedelta(days=3))] == 2
    assert values[_iso(today - timedelta(days=1))] is None


def test_summary_explicit_date(client: TestClient, settings) -> None:
    day = "2026-03-05"
    _checkin_sql(settings, day, 3)
    _task(settings, "Задача дня", due=day, resource="must")
    payload = client.get("/api/personal/summary", params={"date": day}).json()
    assert payload["date"] == day
    assert payload["checkin"]["energy"] == 3
    assert payload["tasks"]["must"] == 1
    assert payload["energy7"][-1] == {"date": day, "value": 3}
    assert client.get("/api/personal/summary", params={"date": "05.03.2026"}).status_code == 422


# --------------------------------------------------------------------------
# Итоги периодов
# --------------------------------------------------------------------------


def _seed_review_period(settings, start: date) -> dict:
    """Данные для фактов недели, начинающейся в ``start``."""
    _habit(settings, "Сон", target=7)
    _habit(settings, "Прогулка", target=7)
    _mark(settings, 1, _iso(start))
    _mark(settings, 1, _iso(start + timedelta(days=1)))
    _mark(settings, 2, _iso(start + timedelta(days=2)))

    _task(settings, "Закрыта в срок", due=_iso(start), status="done", done_at=_iso(start))
    _task(settings, "Открыта в срок", due=_iso(start + timedelta(days=1)), resource="must")
    _task(settings, "Просрочена до периода", due=_iso(start - timedelta(days=2)))
    _task(settings, "Отменена", due=_iso(start + timedelta(days=2)), status="cancelled")
    _task(settings, "Вне периода", due=_iso(start + timedelta(days=20)), status="done")

    _book(settings, "Закончена в периоде", pages=200, finished_at=_iso(start + timedelta(days=1)))
    _book(settings, "Закончена до", pages=500, finished_at=_iso(start - timedelta(days=1)))

    _checkin_sql(settings, _iso(start), 2)
    _checkin_sql(settings, _iso(start + timedelta(days=1)), 5)
    return {}


def test_review_week_facts_and_open_tasks(client: TestClient, settings) -> None:
    week_start = _week_start(settings)
    week_end = week_start + timedelta(days=6)
    _seed_review_period(settings, week_start)

    payload = client.get("/api/personal/review", params={"period": "week"}).json()
    assert payload["period"] == "week"
    assert payload["startDate"] == _iso(week_start)
    assert payload["endDate"] == _iso(week_end)
    assert payload["existing"] is None

    facts = payload["facts"]
    assert facts["tasksDone"] == 1
    assert facts["tasksTotal"] == 2
    assert facts["pagesRead"] == 200
    assert facts["energyAvg"] == 3.5
    # 3 отметки / (2 привычки × 7 дней) = 21.43 → 21 %.
    assert facts["habitsPct"] == 21
    assert facts["days"] == 7

    open_tasks = payload["openTasks"]
    assert [item["title"] for item in open_tasks] == ["Просрочена до периода", "Открыта в срок"]
    assert open_tasks[0]["dueDate"] == _iso(week_start - timedelta(days=2))
    assert all(set(item) == {"id", "title", "dueDate"} for item in open_tasks)


def test_review_month_and_ranges(client: TestClient, settings) -> None:
    today = _t(settings)
    month_start = today.replace(day=1)
    month_end = today.replace(day=monthrange(today.year, today.month)[1])

    payload = client.get("/api/personal/review", params={"period": "month"}).json()
    assert payload["startDate"] == _iso(month_start)
    assert payload["endDate"] == _iso(month_end)
    assert payload["facts"]["days"] == month_end.day

    explicit = client.get(
        "/api/personal/review", params={"period": "week", "start": "2026-05-10"}
    ).json()
    assert explicit["startDate"] == "2026-05-10"
    assert explicit["endDate"] == "2026-05-16"

    assert (
        client.get("/api/personal/review", params={"period": "quarter"}).status_code == 422
    )
    assert (
        client.get("/api/personal/review", params={"period": "week", "start": "10.05.2026"}).status_code
        == 422
    )


def test_review_save_and_upsert(client: TestClient, settings) -> None:
    week_start = _week_start(settings)
    _seed_review_period(settings, week_start)

    first = client.post(
        "/api/personal/review",
        json={
            "period": "week",
            "startDate": _iso(week_start),
            "wins": "Сделал главное",
            "misses": "Не всё успел",
            "focus": "Один проект",
            "care": "Прогулки",
        },
    )
    assert first.status_code == 200, first.text
    saved = first.json()
    review_id = saved["id"]
    assert saved["wins"] == "Сделал главное"
    assert saved["startDate"] == _iso(week_start)
    assert saved["endDate"] == _iso(week_start + timedelta(days=6))
    assert saved["facts"]["tasksDone"] == 1
    assert "facts" in saved and "id" in saved
    assert _count(settings, "reviews") == 1

    # Повторное сохранение — та же запись, новый текст.
    second = client.post(
        "/api/personal/review",
        json={
            "period": "week",
            "startDate": _iso(week_start),
            "wins": "Уточнил победы",
            "misses": "",
            "focus": "",
            "care": "",
        },
    )
    assert second.status_code == 200
    assert second.json()["id"] == review_id
    assert second.json()["wins"] == "Уточнил победы"
    assert _count(settings, "reviews") == 1

    # Факты сохранены и не помечены как демо (сидер не должен их удалять).
    row = _fetch(settings, "SELECT facts_json FROM reviews WHERE id = ?", (review_id,))[0]
    assert '"demo"' not in str(row["facts_json"])
    assert '"tasksDone": 1' in str(row["facts_json"])

    review = client.get("/api/personal/review", params={"period": "week"}).json()
    assert review["existing"]["id"] == review_id
    assert review["existing"]["wins"] == "Уточнил победы"
    assert review["existing"]["facts"]["tasksDone"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"period": "quarter", "startDate": "2026-05-10"},
        {"period": "week"},
        {"period": "week", "startDate": ""},
        {"period": "week", "startDate": "10.05.2026"},
        {"period": "month", "startDate": "2026-13-01"},
    ],
)
def test_review_save_validation(client: TestClient, payload: dict) -> None:
    assert client.post("/api/personal/review", json=payload).status_code == 422


def test_reviews_history(client: TestClient, settings) -> None:
    week_start = _week_start(settings)
    today = _t(settings)
    starts = [
        ("week", _iso(week_start)),
        ("week", _iso(week_start - timedelta(days=7))),
        ("month", _iso(today.replace(day=1))),
    ]
    for period, start in starts:
        response = client.post(
            "/api/personal/review",
            json={"period": period, "startDate": start, "wins": "Победа %s" % start},
        )
        assert response.status_code == 200, response.text

    everything = client.get("/api/personal/reviews").json()
    assert everything["total"] == 3
    dates = [item["startDate"] for item in everything["items"]]
    assert dates == sorted(dates, reverse=True)

    limited = client.get("/api/personal/reviews", params={"limit": 2}).json()
    assert limited["limit"] == 2
    assert len(limited["items"]) == 2
    assert limited["total"] == 2
    assert limited["items"][0]["startDate"] == dates[0]

    assert client.get("/api/personal/reviews", params={"limit": 0}).status_code == 422
    assert client.get("/api/personal/reviews", params={"limit": 101}).status_code == 422


# --------------------------------------------------------------------------
# Сводная аналитика
# --------------------------------------------------------------------------


def test_analytics_summary_empty(client: TestClient) -> None:
    payload = client.get("/api/analytics/summary").json()
    assert payload["days"] == 30
    assert payload["energy"] == {"series": [], "avg": None, "daysWithData": 0}
    assert payload["spheres"] == []
    assert payload["reading"]["pages"] == 0
    assert payload["reading"]["booksDone"] == 0
    assert payload["finance"]["income"] == 0.0
    assert payload["finance"]["expense"] == 0.0
    assert payload["finance"]["balance"] == 0.0
    assert payload["finance"]["series"] == []
    assert payload["habits"]["pct"] == 0.0
    assert len(payload["habits"]["series"]) == 30
    assert payload["tasks"] == {"done": 0, "total": 0, "pct": 0.0}
    assert isinstance(payload["correlations"], list) and payload["correlations"]
    # Существующий эндпоинт корреляций не изменился.
    legacy = client.get("/api/analytics/correlations")
    assert legacy.status_code == 200
    assert "pairs" in legacy.json()


def test_analytics_summary_with_data(client: TestClient, settings) -> None:
    today = _t(settings)
    today_s = _iso(today)
    yesterday = _iso(today - timedelta(days=1))

    sphere = _exec(settings, "INSERT INTO spheres (name, color, sort) VALUES ('Здоровье', '#5b8cff', 0)")
    _task(settings, "Спорт", due=today_s, status="done", sphere_id=sphere, done_at=today_s)
    _task(settings, "Сон", due=today_s, sphere_id=sphere)
    _task(settings, "Без сферы", due=today_s)

    _checkin_sql(settings, today_s, 4)
    _checkin_sql(settings, yesterday, 2)

    _book(settings, "Книга окна", pages=320, finished_at=today_s)
    _book(settings, "Книга давно", pages=500, finished_at=_iso(today - timedelta(days=100)))

    _exec(settings, "INSERT INTO finance_categories (name, kind) VALUES ('Зарплата', 'income')")
    _exec(
        settings,
        "INSERT INTO transactions (ts, amount_kopecks, kind) VALUES (?, 100000, 'income')",
        (today_s + "T10:00:00+03:00",),
    )
    _exec(
        settings,
        "INSERT INTO transactions (ts, amount_kopecks, kind) VALUES (?, 25050, 'expense')",
        (today_s + "T12:00:00+03:00",),
    )

    habit = _habit(settings, "Сон", target=7)
    _mark(settings, habit, today_s)
    _mark(settings, habit, yesterday)

    payload = client.get("/api/analytics/summary").json()
    assert payload["energy"]["avg"] == 3.0
    assert [point["date"] for point in payload["energy"]["series"]] == [yesterday, today_s]

    spheres = {item["name"]: item for item in payload["spheres"]}
    assert spheres["Здоровье"]["done"] == 1
    assert spheres["Здоровье"]["total"] == 2
    assert spheres["Здоровье"]["pct"] == 50.0
    assert spheres["Здоровье"]["color"] == "#5b8cff"
    assert spheres["Без сферы"]["id"] is None
    assert spheres["Без сферы"]["total"] == 1
    assert round(sum(item["share"] for item in payload["spheres"]), 6) == 1.0

    assert payload["reading"]["pages"] == 320
    assert payload["reading"]["booksDone"] == 1
    assert payload["reading"]["pagesAll"] == 820
    assert payload["reading"]["booksDoneAll"] == 2

    finance = payload["finance"]
    assert finance["income"] == 1000.0
    assert finance["expense"] == 250.5
    assert finance["balance"] == 749.5
    assert finance["series"] == [
        {"date": today_s, "income": 1000.0, "expense": 250.5}
    ]

    assert payload["habits"]["habits"] == 1
    assert payload["habits"]["pct"] == round(min(100.0, 2 / 30 * 100), 1)
    assert len(payload["habits"]["series"]) == 30
    assert payload["habits"]["series"][-1]["marks"] == 1

    assert payload["tasks"] == {"done": 1, "total": 3, "pct": 33.3}

    correlations = payload["correlations"]
    assert all("status" in pair and "text" in pair for pair in correlations)


def test_analytics_summary_archived_spheres(client: TestClient, settings) -> None:
    today_s = settings.today()
    _exec(settings, "INSERT INTO spheres (name, archived) VALUES ('Активная', 0)")
    _exec(settings, "INSERT INTO spheres (name, archived) VALUES ('Архив пустой', 1)")
    used = _exec(settings, "INSERT INTO spheres (name, archived) VALUES ('Архив с делом', 1)")
    _task(settings, "Задача архивной сферы", due=today_s, sphere_id=used)

    names = [item["name"] for item in client.get("/api/analytics/summary").json()["spheres"]]
    assert "Активная" in names
    assert "Архив с делом" in names
    assert "Архив пустой" not in names


def test_analytics_summary_window(client: TestClient, settings) -> None:
    today = _t(settings)
    old_day = _iso(today - timedelta(days=100))
    _exec(
        settings,
        "INSERT INTO transactions (ts, amount_kopecks, kind) VALUES (?, 50000, 'expense')",
        (old_day + "T12:00:00+03:00",),
    )

    assert client.get("/api/analytics/summary").json()["finance"]["expense"] == 0.0
    assert (
        client.get("/api/analytics/summary", params={"days": 365}).json()["finance"]["expense"]
        == 500.0
    )
    assert (
        client.get("/api/analytics/summary", params={"days": 180}).json()["finance"]["expense"]
        == 500.0
    )
    assert client.get("/api/analytics/summary", params={"days": 1}).status_code == 422
    assert client.get("/api/analytics/summary", params={"days": 400}).status_code == 422
