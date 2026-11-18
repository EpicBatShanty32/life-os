"""Тесты практик и очков: API (``app/server/api/practices.py``) и движок.

Проверяется контракт фичи целиком:

* каталог из шести практик с личными цифрами и блоком очков в шапке;
* карточка практики: шаги, уровни с ровно одним ``default``, ``safetyNote``;
* сессия плеера даёт ровно ожидаемые очки **один раз**: повтор за день
  обновляет максимумы, но не начисляет заново;
* бонус за личный рекорд задержки — только когда задержка действительно
  выросла; бонус за серию — по дням подряд и не больше 20 за день;
* кап дня 60 очков: частичное начисление помечается ``capped``;
* ``/api/points`` всегда равен сумме ``points_ledger`` (счётчика-строки нет);
* ``/api/points/award`` идемпотентен по ``refId`` — один ``ref_id``, одни очки;
* пороги уровней: дефолты 0/1000/5000/15000, замена через ``PUT``, отказ на
  неверных порогах;
* демо-сид: 6 практик, 30 сессий за 90 дней, честный баланс из журнала.

Все базы — во временных каталогах (``tmp_path``), ``data/lifeos-v3.db`` не
затрагивается. «Сегодня» берётся из настроек приложения, поэтому тесты не
зависят от даты запуска; сессии за прошлые дни пишутся движком с явным
``now`` — так же, как это делает сидер.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.server.db import connect, init_db, migrate
from app.server.main import create_app
from app.server.practices import (
    CATALOG_SLUGS,
    DAY_CAP,
    DEFAULT_LEVELS,
    award,
    day_points,
    ensure_practice_catalog,
    get_practice,
    level_for,
    list_practices,
    points_summary,
    practice_streak,
    save_practice_session,
    total_points,
)
from app.server.seeder import seed
from app.server.settings import load_settings

#: Слаги демо-набора: 4 дыхательные, растяжка и холодный душ.
BREATHING_SLUGS = ("wim-hof", "breath-478", "box-breathing", "breath-55")

DEMO_TODAY = date(2026, 11, 18)


# --------------------------------------------------------------------------
# Фикстуры и помощники
# --------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path: Path):
    """Приложение на временной базе: ``(TestClient, Settings)``."""
    settings = load_settings(env={}, root=tmp_path)
    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        yield client, settings


@pytest.fixture()
def client(api) -> TestClient:
    return api[0]


@pytest.fixture()
def settings(api):
    return api[1]


def _today(client: TestClient) -> str:
    """«Сегодня» по часам приложения — из ответа ``/api/points``."""
    return client.get("/api/points").json()["date"]


def _sql(settings, query: str, params: tuple = ()):
    conn = connect(settings.db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _scalar(settings, query: str, params: tuple = ()):
    rows = _sql(settings, query, params)
    return rows[0][0] if rows else None


def _execute(settings, query: str, params: tuple = ()) -> None:
    conn = connect(settings.db_path)
    try:
        with conn:
            conn.execute(query, params)
    finally:
        conn.close()


def _catalog(settings) -> None:
    """Заводит каталог практик движком — как это делает сидер."""
    conn = connect(settings.db_path)
    try:
        with conn:
            ensure_practice_catalog(conn)
    finally:
        conn.close()


def _save_session(
    settings,
    slug: str,
    *,
    days_ago: int = 0,
    hold: int = 0,
    rounds: int = 3,
    note: str = "",
):
    """Сессия за день ``сегодня - days_ago`` движком (как сидер и бот)."""
    _catalog(settings)
    conn = connect(settings.db_path)
    try:
        with conn:
            return save_practice_session(
                conn,
                slug=slug,
                rounds_done=rounds,
                best_hold_s=hold,
                source="web",
                note=note,
                now=settings.now() - timedelta(days=days_ago),
            )
    finally:
        conn.close()


def _award(settings, kind: str, ref_id: str, *, points: int | None = None, days_ago: int = 0):
    """Начисление движком от лица веба (``source='app'``)."""
    conn = connect(settings.db_path)
    try:
        with conn:
            return award(
                conn,
                kind=kind,
                ref_id=ref_id,
                points=points,
                reason="тест",
                now=settings.now() - timedelta(days=days_ago),
            )
    finally:
        conn.close()


def _ledger_sum(settings, kind: str | None = None) -> int:
    if kind is None:
        return int(_scalar(settings, "SELECT COALESCE(SUM(points), 0) FROM points_ledger") or 0)
    return int(
        _scalar(
            settings,
            "SELECT COALESCE(SUM(points), 0) FROM points_ledger WHERE kind = ?",
            (kind,),
        )
        or 0
    )


def _day_points(settings, day: str) -> int:
    conn = connect(settings.db_path)
    try:
        return day_points(conn, day)
    finally:
        conn.close()


def _practice_stats(client: TestClient, slug: str) -> dict:
    catalog = client.get("/api/practices").json()
    return next(item for item in catalog["practices"] if item["slug"] == slug)["stats"]


# --------------------------------------------------------------------------
# Каталог
# --------------------------------------------------------------------------


def test_catalog_returns_six_practices_with_points_header(client: TestClient) -> None:
    """Каталог: шесть практик, у каждой шаги/уровни, плюс блок очков."""
    response = client.get("/api/practices")
    assert response.status_code == 200
    data = response.json()

    assert data["count"] == 6
    assert data["dayCap"] == DAY_CAP == 60
    slugs = [item["slug"] for item in data["practices"]]
    assert slugs == list(CATALOG_SLUGS)

    for practice in data["practices"]:
        assert practice["title"]
        assert practice["kind"] in ("breathing", "stretch", "other")
        assert practice["steps"], practice["slug"]
        assert practice["levels"], practice["slug"]
        assert practice["durationS"] > 0
        assert practice["stats"]["sessions"] == 0
        assert practice["archived"] is False

    points = data["points"]
    assert points["balance"] == 0
    assert points["level"]["key"] == "novice"
    assert points["streak"]["days"] == 0
    assert points["cap"] == {"day": DAY_CAP, "today": 0, "remaining": DAY_CAP}


def test_catalog_is_created_lazily_and_idempotently(client: TestClient, settings) -> None:
    """Пустая база получает каталог при первом обращении, второй раз — без дублей."""
    assert _scalar(settings, "SELECT COUNT(*) FROM practices") == 0

    assert client.get("/api/practices").json()["count"] == 6
    assert client.get("/api/practices").json()["count"] == 6
    assert _scalar(settings, "SELECT COUNT(*) FROM practices") == 6
    assert _scalar(settings, "SELECT COUNT(DISTINCT slug) FROM practices") == 6


def test_every_breathing_practice_has_safety_note(client: TestClient) -> None:
    """У дыхательных практик ``safetyNote`` не пустой и говорит о воде и враче."""
    catalog = client.get("/api/practices").json()["practices"]
    breathing = [item for item in catalog if item["kind"] == "breathing"]
    assert {item["slug"] for item in breathing} == set(BREATHING_SLUGS)

    for practice in breathing:
        note = practice["safetyNote"].strip()
        assert note, practice["slug"]
        text = note.lower()
        assert "не в воде" in text
        assert "врач" in text
        assert "за рулём" in text

    # Растяжка и холодный душ тоже предупреждают, но своими словами.
    for practice in catalog:
        assert practice["safetyNote"].strip(), practice["slug"]


def test_wim_hof_advanced_level_matches_design(client: TestClient) -> None:
    """Продвинутый уровень Вим Хофа: 3 раунда, задержки 1:30 / 2:00 / 2:30."""
    practice = client.get("/api/practices/wim-hof").json()["practice"]
    levels = {level["key"]: level for level in practice["levels"]}
    advanced = levels["advanced"]

    assert advanced["rounds"] == 3
    assert advanced["holds"] == [90, 120, 150]
    assert practice["defaultLevel"] == "advanced"
    assert sum(1 for level in practice["levels"] if level.get("default")) == 1


# --------------------------------------------------------------------------
# Карточка практики
# --------------------------------------------------------------------------


def test_practice_detail_has_steps_levels_and_sessions(client: TestClient) -> None:
    """Карточка: фазы с длительностью, уровни, последние сессии и очки."""
    client.post(
        "/api/practice-sessions",
        json={"slug": "box-breathing", "roundsDone": 6, "bestHoldS": 0},
    )

    response = client.get("/api/practices/box-breathing")
    assert response.status_code == 200
    data = response.json()
    practice = data["practice"]

    assert practice["slug"] == "box-breathing"
    assert practice["kind"] == "breathing"
    phases = {step["phase"] for step in practice["steps"]}
    assert {"inhale", "hold", "exhale"} <= phases  # боксовое дыхание: 4-4-4-4
    for step in practice["steps"]:
        assert isinstance(step["phase"], str) and step["phase"]
        assert step["seconds"] > 0
        assert step["label"].strip()
    assert len(practice["sessions"]) == 1
    assert practice["sessions"][0]["roundsDone"] == 6
    assert practice["sessions"][0]["source"] == "web"
    assert practice["stats"]["sessions"] == 1
    assert data["dayCap"] == 60
    assert data["points"]["balance"] >= 10


def test_unknown_slug_returns_404_on_detail_and_session(client: TestClient) -> None:
    """Неизвестный слаг — 404 и в карточке, и в сохранении сессии."""
    detail = client.get("/api/practices/no-such-practice")
    assert detail.status_code == 404
    assert "не найдена" in detail.json()["detail"]

    session = client.post("/api/practice-sessions", json={"slug": "no-such-practice"})
    assert session.status_code == 404
    assert "не найдена" in session.json()["detail"]


# --------------------------------------------------------------------------
# Сессия и начисления
# --------------------------------------------------------------------------


def test_session_awards_ten_points_once(client: TestClient, settings) -> None:
    """Первая сессия: 10 очков, одна строка сессии, ссылка ``practice:id:date``."""
    today = _today(client)
    response = client.post(
        "/api/practice-sessions",
        json={"slug": "breath-478", "roundsDone": 4, "bestHoldS": 0, "source": "web"},
    )
    assert response.status_code == 201
    data = response.json()

    assert data["pointsEarned"] == 10
    assert data["total"] == 10
    assert data["awards"][0]["kind"] == "practice"
    assert data["awards"][0]["points"] == 10
    assert data["awards"][0]["duplicate"] is False
    assert data["awards"][0]["capped"] is False
    assert data["recordUpdated"] is False
    assert data["level"]["key"] == "novice"
    assert data["session"]["date"] == today
    assert data["session"]["roundsDone"] == 4

    assert _scalar(settings, "SELECT COUNT(*) FROM practice_sessions") == 1
    assert _ledger_sum(settings) == 10
    refs = [row[0] for row in _sql(settings, "SELECT ref_id FROM points_ledger")]
    practice_id = _scalar(settings, "SELECT id FROM practices WHERE slug = 'breath-478'")
    assert refs == ["practice:%d:%s" % (practice_id, today)]


def test_session_repeat_same_day_does_not_double_points(client: TestClient, settings) -> None:
    """Повторное сохранение за день: 0 очков, пометка ``duplicate``, одна строка."""
    payload = {"slug": "stretch-7", "roundsDone": 1, "bestHoldS": 0, "source": "web"}
    first = client.post("/api/practice-sessions", json=payload).json()
    second = client.post("/api/practice-sessions", json=payload).json()

    assert first["pointsEarned"] == 10
    assert second["pointsEarned"] == 0
    assert second["awards"][0]["duplicate"] is True
    assert second["awards"][0]["points"] == 0
    assert second["total"] == first["total"] == 10

    assert _scalar(settings, "SELECT COUNT(*) FROM practice_sessions") == 1
    assert _ledger_sum(settings) == 10


def test_empty_session_gives_no_points_and_no_streak(client: TestClient, settings) -> None:
    """«Старт → сразу финиш»: очков нет, серия не растёт, повтор с кругами — да."""
    empty = client.post(
        "/api/practice-sessions", json={"slug": "breath-55", "roundsDone": 0}
    ).json()
    assert empty["pointsEarned"] == 0
    assert empty["awards"][0]["kind"] == "practice"
    assert empty["awards"][0]["points"] == 0
    assert empty["awards"][0]["duplicate"] is False
    assert empty["recordUpdated"] is False
    assert empty["total"] == 0
    assert empty["streak"]["days"] == 0
    assert client.get("/api/points").json()["balance"] == 0
    assert _scalar(settings, "SELECT COUNT(*) FROM practice_sessions") == 1

    # Тот же день, но уже с кругами: сессия обновляется, очки начисляются.
    done = client.post(
        "/api/practice-sessions", json={"slug": "breath-55", "roundsDone": 10}
    ).json()
    assert done["pointsEarned"] == 10
    assert done["session"]["roundsDone"] == 10
    assert _scalar(settings, "SELECT COUNT(*) FROM practice_sessions") == 1
    assert client.get("/api/points").json()["streak"]["days"] == 1
    assert _practice_stats(client, "breath-55")["streakDays"] == 1


def test_record_bonus_only_for_new_hold(client: TestClient, settings) -> None:
    """+5 только когда задержка выросла: 60 → 30 без бонуса, → 95 с бонусом."""
    first = _save_session(settings, "wim-hof", days_ago=6, hold=60)
    assert first["pointsEarned"] == 15
    assert first["recordUpdated"] is True
    assert first["previousRecordS"] == 0

    second = _save_session(settings, "wim-hof", days_ago=3, hold=30)
    assert second["pointsEarned"] == 10
    assert second["recordUpdated"] is False
    assert second["previousRecordS"] == 60  # рекорд практики не сбит
    assert second["bestHoldS"] == 30  # задержка этой сессии
    assert second["bestHoldLabel"] == "30 с"  # до минуты формат в секундах

    third = _save_session(settings, "wim-hof", hold=95)
    assert third["pointsEarned"] == 15
    assert third["recordUpdated"] is True
    assert third["previousRecordS"] == 60
    assert third["bestHoldS"] == 95
    assert third["bestHoldLabel"] == "1:35"

    assert _ledger_sum(settings, "record") == 10
    assert _ledger_sum(settings, "practice") == 30


def test_streak_bonus_grows_with_consecutive_days(client: TestClient, settings) -> None:
    """Серия: второй день подряд +4, третий +6; серия считается по дням."""
    first = _save_session(settings, "box-breathing", days_ago=2, rounds=6)
    assert first["pointsEarned"] == 10
    assert first["streak"]["days"] == 1
    assert [item["kind"] for item in first["awards"]] == ["practice"]

    second = _save_session(settings, "box-breathing", days_ago=1, rounds=8)
    assert second["pointsEarned"] == 14
    assert second["streak"]["days"] == 2
    assert second["awards"][-1]["kind"] == "streak"
    assert second["awards"][-1]["points"] == 4

    third = _save_session(settings, "box-breathing", rounds=10)
    assert third["pointsEarned"] == 16
    assert third["streak"]["days"] == 3
    assert third["awards"][-1]["points"] == 6
    assert _ledger_sum(settings, "streak") == 10

    conn = connect(settings.db_path)
    try:
        streak = practice_streak(conn, settings.now().date())
    finally:
        conn.close()
    assert streak["days"] == 3
    assert streak["best"] == 3
    assert streak["alive"] is True


def test_streak_bonus_is_capped_at_twenty(client: TestClient, settings) -> None:
    """Одиннадцать дней подряд: бонус растёт до 20 и дальше не идёт."""
    results = [_save_session(settings, "breath-55", days_ago=10 - index) for index in range(11)]

    assert _ledger_sum(settings, "streak") == 4 + 6 + 8 + 10 + 12 + 14 + 16 + 18 + 20 + 20
    last = results[-1]
    assert last["streak"]["days"] == 11
    assert last["awards"][-1]["kind"] == "streak"
    assert last["awards"][-1]["points"] == 20


def test_day_cap_limits_points_to_sixty(client: TestClient, settings) -> None:
    """Кап дня 60: сессия после 55 очков получает только 5 и помечается ``capped``."""
    _catalog(settings)
    assert _award(settings, "journal", "fill:day", points=55)["awarded"] == 55
    assert _day_points(settings, _today(client)) == 55

    response = client.post(
        "/api/practice-sessions",
        json={"slug": "cold-shower-60", "roundsDone": 1, "bestHoldS": 0},
    )
    data = response.json()
    assert data["pointsEarned"] == 5
    assert data["awards"][0]["points"] == 5
    assert data["awards"][0]["capped"] is True
    assert data["cap"] == {"day": 60, "today": 60, "remaining": 0}

    extra = client.post(
        "/api/points/award",
        json={"kind": "habit", "refId": "habit:after-cap:1"},
    ).json()
    assert extra["awarded"] == 0
    assert extra["capped"] is True
    assert extra["duplicate"] is False
    assert _ledger_sum(settings) == 60


# --------------------------------------------------------------------------
# Журнал начислений
# --------------------------------------------------------------------------


def test_points_summary_matches_ledger_sum(client: TestClient, settings) -> None:
    """``/api/points`` сходится с суммой ``points_ledger`` и по видам, и по дням."""
    client.post("/api/practice-sessions", json={"slug": "breath-478", "roundsDone": 4})
    for kind, ref in (("habit", "habit:1"), ("checkin", "checkin:1"), ("journal", "journal:1")):
        client.post("/api/points/award", json={"kind": kind, "refId": ref})

    data = client.get("/api/points?days=7").json()
    ledger = _ledger_sum(settings)

    assert ledger == 10 + 2 + 3 + 1 == 16
    assert data["balance"] == ledger
    assert sum(data["totalsByKind"].values()) == ledger
    assert sum(item["points"] for item in data["days"]) == ledger
    assert data["cap"]["today"] == ledger
    assert data["cap"]["remaining"] == 60 - ledger
    assert data["totalsByKind"] == {
        "practice": 10,
        "record": 0,
        "streak": 0,
        "habit": 2,
        "task": 0,
        "checkin": 3,
        "journal": 1,
    }


def test_points_history_window_and_recent_list(client: TestClient) -> None:
    """Окно истории ровно ``days``, последний день — сегодня, recent — свежие."""
    today = _today(client)
    for index in range(5):
        client.post("/api/points/award", json={"kind": "task", "refId": "task:%d" % index})

    week = client.get("/api/points?days=7").json()
    assert len(week["days"]) == 7
    assert week["days"][-1]["date"] == today
    assert week["days"][-1]["points"] == 5

    day = client.get("/api/points?days=1").json()
    assert len(day["days"]) == 1
    assert day["date"] == today
    assert [item["date"] for item in day["days"]] == [today]

    recent = client.get("/api/points?days=30&recent=2").json()["recent"]
    assert len(recent) == 2
    assert all(item["kind"] == "task" and item["points"] == 1 for item in recent)
    assert [item["refId"] for item in recent] == ["task:4", "task:3"]


def test_award_endpoint_is_idempotent_by_ref_id(client: TestClient, settings) -> None:
    """Один ``refId`` — одно начисление; повтор отвечает ``duplicate`` и 0 очков."""
    payload = {"kind": "habit", "refId": "habit:42:2026-11-18", "reason": "Зарядка"}
    first = client.post("/api/points/award", json=payload).json()
    second = client.post("/api/points/award", json=payload).json()

    assert first["awarded"] == 2
    assert first["duplicate"] is False
    assert first["total"] == 2
    assert second["awarded"] == 0
    assert second["duplicate"] is True
    assert second["total"] == 2
    assert _scalar(settings, "SELECT COUNT(*) FROM points_ledger") == 1
    assert client.get("/api/points").json()["balance"] == 2


def test_award_endpoint_validates_kind_bounds_and_source(client: TestClient) -> None:
    """Мусор на входе — 422: вид, длина ``refId``, границы очков, источник."""
    assert client.post("/api/points/award", json={"kind": "nope", "refId": "x"}).status_code == 422
    assert client.post("/api/points/award", json={"kind": "habit", "refId": ""}).status_code == 422
    assert (
        client.post(
            "/api/points/award", json={"kind": "habit", "refId": "x", "points": -1}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/points/award", json={"kind": "habit", "refId": "x", "points": DAY_CAP + 1}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/points/award", json={"kind": "habit", "refId": "x", "source": "bot"}
        ).status_code
        == 422
    )


def test_session_validates_payload(client: TestClient) -> None:
    """Сессия не принимает мусор: раунды, задержка, источник, длина заметки."""
    assert (
        client.post("/api/practice-sessions", json={"slug": "wim-hof", "roundsDone": 101}).status_code
        == 422
    )
    assert (
        client.post("/api/practice-sessions", json={"slug": "wim-hof", "roundsDone": -1}).status_code
        == 422
    )
    assert (
        client.post("/api/practice-sessions", json={"slug": "wim-hof", "bestHoldS": -5}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/practice-sessions", json={"slug": "wim-hof", "source": "smartwatch"}
        ).status_code
        == 422
    )
    assert client.post("/api/practice-sessions", json={"slug": ""}).status_code == 422
    assert (
        client.post(
            "/api/practice-sessions", json={"slug": "wim-hof", "note": "x" * 501}
        ).status_code
        == 422
    )


# --------------------------------------------------------------------------
# Уровни
# --------------------------------------------------------------------------


def test_levels_defaults_and_level_growth(client: TestClient) -> None:
    """Пороги по умолчанию и рост уровня по мере накопления очков."""
    levels = client.get("/api/points/levels").json()
    assert [(item["key"], item["min"]) for item in levels["levels"]] == [
        ("novice", 0),
        ("steady", 1000),
        ("fit", 5000),
        ("master", 15000),
    ]
    assert [item["title"] for item in levels["levels"]] == [
        "Новичок",
        "Устойчивый",
        "В форме",
        "Мастер",
    ]
    assert levels["level"]["key"] == "novice"

    custom = {
        "levels": [
            {"key": "novice", "title": "Новичок", "min": 0},
            {"key": "steady", "title": "Устойчивый", "min": 10},
            {"key": "fit", "title": "В форме", "min": 20},
            {"key": "master", "title": "Мастер", "min": 30},
        ]
    }
    assert client.put("/api/points/levels", json=custom).json()["level"]["key"] == "novice"

    client.post("/api/points/award", json={"kind": "journal", "refId": "lvl:1", "points": 10})
    assert client.get("/api/points").json()["level"]["key"] == "steady"

    client.post("/api/points/award", json={"kind": "journal", "refId": "lvl:2", "points": 10})
    assert client.get("/api/points").json()["level"] == {
        "key": "fit",
        "title": "В форме",
        "min": 20,
        "next": {"key": "master", "title": "Мастер", "min": 30},
        "toNext": 10,
        "progress": 0.0,
        "total": 20,
    }

    client.post("/api/points/award", json={"kind": "journal", "refId": "lvl:3", "points": 10})
    top = client.get("/api/points").json()["level"]
    assert top["key"] == "master"
    assert top["next"] is None
    assert top["toNext"] == 0
    assert top["progress"] == 1.0


def test_levels_put_rejects_bad_thresholds(client: TestClient) -> None:
    """Неверные пороги — 422: мало уровней, первый не с нуля, дубль, убывание."""
    assert client.put("/api/points/levels", json={"levels": []}).status_code == 422
    assert (
        client.put(
            "/api/points/levels",
            json={
                "levels": [
                    {"key": "a", "title": "A", "min": 10},
                    {"key": "b", "title": "B", "min": 20},
                ]
            },
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/points/levels",
            json={
                "levels": [
                    {"key": "a", "title": "A", "min": 0},
                    {"key": "a", "title": "A2", "min": 10},
                ]
            },
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/points/levels",
            json={
                "levels": [
                    {"key": "a", "title": "A", "min": 0},
                    {"key": "b", "title": "B", "min": 0},
                ]
            },
        ).status_code
        == 422
    )
    # Пороги остались дефолтными.
    assert client.get("/api/points/levels").json()["levels"][1]["min"] == 1000


# --------------------------------------------------------------------------
# Личные цифры и архив
# --------------------------------------------------------------------------


def test_catalog_shows_personal_stats_after_sessions(client: TestClient) -> None:
    """Каталог показывает мои цифры: сколько раз, рекорд, очки практики."""
    today = _today(client)
    _save_session(settings=client.app.state.settings, slug="wim-hof", days_ago=5, hold=95, rounds=2)
    _save_session(settings=client.app.state.settings, slug="wim-hof", hold=0, rounds=3)

    stats = _practice_stats(client, "wim-hof")
    assert stats["sessions"] == 2
    assert stats["days"] == 2
    assert stats["lastDate"] == today
    assert stats["bestHoldS"] == 95
    assert stats["bestHoldLabel"] == "1:35"
    assert stats["bestRounds"] == 3
    assert stats["points"] == 25
    assert stats["streakDays"] == 1


def test_archived_practice_hidden_by_default(client: TestClient, settings) -> None:
    """Архивная практика не мешает каталогу, но доступна с ``includeArchived``."""
    client.get("/api/practices")
    _execute(settings, "UPDATE practices SET archived = 1 WHERE slug = 'stretch-7'")

    assert "stretch-7" not in [item["slug"] for item in client.get("/api/practices").json()["practices"]]
    full = client.get("/api/practices?includeArchived=true").json()
    assert "stretch-7" in [item["slug"] for item in full["practices"]]
    assert full["count"] == 6


# --------------------------------------------------------------------------
# Демо-сид и миграция
# --------------------------------------------------------------------------


def test_demo_seed_fills_practices_and_balance(tmp_path: Path) -> None:
    """Демо-база: 6 практик, 30 сессий за 90 дней, баланс = сумма журнала."""
    conn = init_db(tmp_path / "demo.db")
    try:
        stats = seed(conn, days=120, today=DEMO_TODAY)
        assert stats.practices == 6
        assert stats.practice_sessions == 30

        slugs = {row["slug"] for row in conn.execute("SELECT slug FROM practices")}
        assert slugs == set(CATALOG_SLUGS)
        assert {
            row["slug"]
            for row in conn.execute(
                "SELECT DISTINCT p.slug FROM practice_sessions s "
                "JOIN practices p ON p.id = s.practice_id"
            )
        } == set(CATALOG_SLUGS)

        assert conn.execute("SELECT COUNT(*) FROM practice_sessions").fetchone()[0] == 30
        assert conn.execute(
            "SELECT COALESCE(SUM(points), 0) FROM points_ledger WHERE kind = 'practice'"
        ).fetchone()[0] == 300
        assert conn.execute(
            "SELECT COALESCE(SUM(points), 0) FROM points_ledger WHERE kind = 'record'"
        ).fetchone()[0] == 30

        # Задержки Вим Хофа растут по истории, сегодняшний день свободен.
        holds = [
            row[0]
            for row in conn.execute(
                "SELECT s.best_hold_s FROM practice_sessions s "
                "JOIN practices p ON p.id = s.practice_id "
                "WHERE p.slug = 'wim-hof' AND s.best_hold_s > 0 ORDER BY s.date"
            )
        ]
        assert holds == [60, 75, 90, 105, 120, 150]
        assert conn.execute(
            "SELECT COUNT(*) FROM practice_sessions WHERE date = ?", (DEMO_TODAY.isoformat(),)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM practice_sessions WHERE date > ?", (DEMO_TODAY.isoformat(),)
        ).fetchone()[0] == 0

        summary = points_summary(conn, DEMO_TODAY, days=30)
        ledger = conn.execute("SELECT COALESCE(SUM(points), 0) FROM points_ledger").fetchone()[0]
        assert summary["balance"] == ledger == total_points(conn)
        assert sum(summary["totalsByKind"].values()) == ledger
        assert ledger >= 1000  # демо-баланс дорос до второго уровня
        assert summary["level"]["key"] == "steady"
        assert summary["level"]["title"] == "Устойчивый"
        assert summary["streak"]["days"] >= 4
        assert summary["streak"]["alive"] is True
        # Сегодняшний день свободен для живой практики: демо-сессий и очков за
        # практики/рекорды/серии на сегодня нет (журнал и чек-ины — есть).
        assert conn.execute(
            "SELECT COUNT(*) FROM points_ledger WHERE date = ? "
            "AND kind IN ('practice', 'record', 'streak')",
            (DEMO_TODAY.isoformat(),),
        ).fetchone()[0] == 0
        assert 0 < summary["cap"]["today"] < DAY_CAP
        assert summary["cap"]["remaining"] > 0

        # Второй прогон идемпотентен: те же цифры, никаких дублей.
        again = seed(conn, days=120, today=DEMO_TODAY)
        assert again.practice_sessions == 30
        assert total_points(conn) == ledger
        assert conn.execute("SELECT COUNT(*) FROM practice_sessions").fetchone()[0] == 30
    finally:
        conn.close()


def test_migrate_twice_keeps_practice_tables(tmp_path: Path) -> None:
    """Миграция №5 идемпотентна: повторный ``init_db`` не теряет сессии и очки."""
    path = tmp_path / "schema.db"
    conn = init_db(path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"practices", "practice_sessions", "points_ledger"} <= tables
        with conn:
            ensure_practice_catalog(conn)
            save_practice_session(
                conn,
                slug="wim-hof",
                rounds_done=3,
                best_hold_s=90,
                source="web",
                now=datetime(2026, 11, 18, 8, 0),
            )
        assert conn.execute("SELECT COUNT(*) FROM practice_sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COALESCE(SUM(points), 0) FROM points_ledger").fetchone()[0] == 15
    finally:
        conn.close()

    again = init_db(path)  # миграции применяются поверх
    try:
        assert again.execute("SELECT COUNT(*) FROM practices").fetchone()[0] == 6
        assert again.execute("SELECT COUNT(*) FROM practice_sessions").fetchone()[0] == 1
        assert again.execute("SELECT COALESCE(SUM(points), 0) FROM points_ledger").fetchone()[0] == 15
        assert level_for(15, DEFAULT_LEVELS)["key"] == "novice"
    finally:
        again.close()


# --------------------------------------------------------------------------
# Апгрейд базы, которую успела создать ленивая схема бота (до миграции №5)
# --------------------------------------------------------------------------

#: Как выглядели таблицы практик в базе, которую бот создал до t33: свои
#: ``kind`` (``breath``/``meditation``/``body``), нет шагов и уровней.
_LEGACY_DDL = (
    """
    CREATE TABLE practices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'breath',
        default_rounds INTEGER NOT NULL DEFAULT 3,
        points INTEGER NOT NULL DEFAULT 10,
        archived INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE practice_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        practice_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        ts TEXT NOT NULL DEFAULT '',
        rounds INTEGER NOT NULL DEFAULT 0,
        points INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'telegram'
    )
    """,
)

_LEGACY_PRACTICES = (
    (1, "wim-hof", "Дыхание Вим Хофа", "breath"),
    (2, "breathing", "Дыхание 4-7-8", "breath"),
    (3, "meditation", "Медитация 10 минут", "meditation"),
    (4, "stretch", "Растяжка", "body"),
)


def _legacy_db(path: Path) -> None:
    """База бота до миграции №5: старые виды практик и дубль сессии за день."""
    conn = connect(path)
    try:
        with conn:
            for statement in _LEGACY_DDL:
                conn.execute(statement)
            conn.executemany(
                "INSERT INTO practices(id, slug, title, kind) VALUES (?, ?, ?, ?)",
                _LEGACY_PRACTICES,
            )
            conn.executemany(
                "INSERT INTO practice_sessions(practice_id, date, ts, rounds, source) "
                "VALUES (?, ?, ?, ?, 'telegram')",
                (
                    (1, "2026-11-17", "2026-11-17T08:00:00+03:00", 2),
                    (1, "2026-11-17", "2026-11-17T20:00:00+03:00", 3),
                    (2, "2026-11-18", "2026-11-18T01:40:00+03:00", 0),
                ),
            )
    finally:
        conn.close()


def test_migration_normalises_legacy_practice_kinds(tmp_path: Path) -> None:
    """Миграция чинит ``kind`` старой базы, добавляет колонки и схлопывает дубли."""
    path = tmp_path / "legacy.db"
    _legacy_db(path)

    conn = init_db(path)
    try:
        kinds = {row["slug"]: row["kind"] for row in conn.execute("SELECT slug, kind FROM practices")}
        assert kinds == {
            "wim-hof": "breathing",
            "breathing": "breathing",
            "meditation": "other",
            "stretch": "stretch",
        }

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(practices)")}
        assert {"steps_json", "levels_json", "safety_note", "duration_s", "sort"} <= columns
        sessions = {row["name"] for row in conn.execute("PRAGMA table_info(practice_sessions)")}
        assert {"rounds_done", "best_hold_s", "started_ts", "finished_ts"} <= sessions

        # Дубль сессии за один день схлопнут: осталась последняя строка.
        rows = conn.execute(
            "SELECT id, rounds FROM practice_sessions WHERE practice_id = 1"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["rounds"] == 3

        # Повторная миграция ничего не ломает.
        migrate(conn)
        assert conn.execute("SELECT COUNT(*) FROM practice_sessions").fetchone()[0] == 2
    finally:
        conn.close()


def test_catalog_refreshes_legacy_stubs_and_archives_junk(tmp_path: Path) -> None:
    """Стаб ``wim-hof`` получает шаги каталога, а стабы вне каталога уходят в архив."""
    path = tmp_path / "legacy.db"
    _legacy_db(path)

    conn = init_db(path)
    try:
        with conn:
            ensure_practice_catalog(conn)

        wim_hof = get_practice(conn, "wim-hof")
        assert wim_hof is not None
        assert wim_hof["steps"], "стаб должен получить шаги каталога"
        assert wim_hof["levels"] and wim_hof["defaultLevel"] == "advanced"
        assert "не в воде" in wim_hof["safetyNote"].lower()
        assert wim_hof["durationS"] > 0
        assert wim_hof["archived"] is False

        visible = [item["slug"] for item in list_practices(conn)]
        assert visible == list(CATALOG_SLUGS)

        archived = {
            row["slug"] for row in conn.execute("SELECT slug FROM practices WHERE archived = 1")
        }
        assert archived == {"breathing", "meditation", "stretch"}

        # История архивных практик жива: сессия telegram осталась на месте.
        assert conn.execute(
            "SELECT COUNT(*) FROM practice_sessions s "
            "JOIN practices p ON p.id = s.practice_id WHERE p.archived = 1"
        ).fetchone()[0] == 1
    finally:
        conn.close()
