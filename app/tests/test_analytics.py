"""Юнит-тесты аналитики на синтетических рядах с известным ответом.

Проверяются функции из ``app/server/api/aggregates.py`` (EMA, МНК-тренд,
прогноз даты цели, корреляции, дельты состава тела) и HTTP-эндпоинты
``/api/health/weight``, ``/api/health/composition``,
``/api/analytics/correlations`` на временной базе.

Ничего не пишется вне ``tmp_path``, сеть не нужна.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.server.api.aggregates import (
    MIN_CORRELATION_DAYS,
    build_correlations,
    composition_deltas,
    ema_series,
    linear_trend,
    pearson,
    weight_forecast,
)
from app.server.db import add_entry, add_metric, init_db
from app.server.main import create_app
from app.server.settings import load_settings

TODAY = date(2026, 11, 17)


def _days(count: int, today: date = TODAY) -> list[str]:
    """``count`` подряд идущих дат, старые → новые, последняя — ``today``."""
    return [(today - timedelta(days=count - 1 - index)).isoformat() for index in range(count)]


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "lifeos.db")
    yield connection
    connection.close()


@pytest.fixture()
def settings(tmp_path):
    return load_settings(env={}, root=tmp_path)


# ---------------------------------------------------------------------------
# Чистые функции
# ---------------------------------------------------------------------------


def test_ema_series_known_values() -> None:
    assert ema_series([]) == []
    assert ema_series([10.0]) == [10.0]
    # alpha = 2 / (3 + 1) = 0.5
    assert ema_series([10, 20, 30, 40], 3) == [10.0, 15.0, 22.5, 31.25]
    # alpha = 2 / (7 + 1) = 0.25
    assert ema_series([100, 100, 100, 100, 100, 100, 100], 7) == [100.0] * 7
    assert ema_series([100, 200], 7) == [100.0, 125.0]


def test_pearson_known_values() -> None:
    assert pearson([(1, 2), (2, 4), (3, 6)]) == pytest.approx(1.0)
    assert pearson([(1, 3), (2, 2), (3, 1)]) == pytest.approx(-1.0)
    assert pearson([(1, 5), (2, 5), (3, 5)]) is None
    assert pearson([(1, 2), (2, 4)]) is None


def test_linear_trend_known_slope() -> None:
    points = [(day, 85.0 - 0.1 * index) for index, day in enumerate(_days(10))]
    trend = linear_trend(points)
    assert trend is not None
    slope, intercept = trend
    assert slope == pytest.approx(-0.1)
    # прямая проходит через последнюю точку
    assert slope * TODAY.toordinal() + intercept == pytest.approx(points[-1][1])
    assert linear_trend(points[:2]) is None


def test_weight_forecast_reaches_target() -> None:
    points = [(day, 85.0 - 0.1 * index) for index, day in enumerate(_days(30))]
    forecast = weight_forecast(points, 80.0, TODAY)
    # сегодня 82.1 кг, цель 80 кг, тренд −0.1 кг/день → 21 день
    assert forecast["status"] == "ok"
    assert forecast["rate_per_week"] == pytest.approx(-0.7)
    assert forecast["weeks"] == pytest.approx(3.0)
    assert forecast["date"] == (TODAY + timedelta(days=21)).isoformat()
    assert forecast["current"] == pytest.approx(82.1)


def test_weight_forecast_honest_statuses() -> None:
    flat = [(day, 80.0) for day in _days(20)]
    assert weight_forecast(flat, 78.0, TODAY)["status"] == "flat"
    assert weight_forecast(flat, 78.0, TODAY)["date"] is None

    growing = [(day, 80.0 + 0.05 * index) for index, day in enumerate(_days(15))]
    away = weight_forecast(growing, 78.0, TODAY)
    assert away["status"] == "away_from_target"
    assert away["date"] is None

    rare = [(day, 80.0 - 0.1 * index) for index, day in enumerate(_days(3))]
    assert weight_forecast(rare, 78.0, TODAY)["status"] == "insufficient_data"

    points = [(day, 85.0 - 0.1 * index) for index, day in enumerate(_days(30))]
    assert weight_forecast(points, None, TODAY)["status"] == "no_target"


def test_composition_deltas_on_known_series(conn) -> None:
    days = _days(30)
    with conn:
        for index, day in enumerate(days):
            add_metric(conn, "fat", 20.0 - 0.02 * index, date=day, unit="%")
            add_metric(conn, "muscle", 35.0, date=day, unit="%")
        add_metric(conn, "body_water", 55.0, date=days[-1], unit="%")

    items = {item["key"]: item for item in composition_deltas(conn, TODAY)}
    assert set(items) == {"fat", "muscle", "body_water"}

    fat = items["fat"]
    assert fat["label"] == "Жир"
    assert fat["unit"] == "%"
    assert fat["current"] == pytest.approx(19.42)
    assert fat["deltas"]["7"] == pytest.approx(-0.12)
    assert fat["deltas"]["30"] == pytest.approx(-0.58)
    # в окне 90 дней лежат те же 30 замеров — дельта считается по ним
    assert fat["counts"]["90"] == 30
    assert fat["deltas"]["90"] == pytest.approx(-0.58)

    assert items["muscle"]["deltas"]["7"] == pytest.approx(0.0)
    # одного замера мало для дельты: честный None, а не ноль
    assert items["body_water"]["deltas"]["7"] is None
    assert items["body_water"]["counts"]["7"] == 1


# ---------------------------------------------------------------------------
# Корреляции: сильная связь, слабая, отсутствие разброса, мало данных
# ---------------------------------------------------------------------------


def _pair(pairs: list[dict], left: str, right: str) -> dict:
    return next(item for item in pairs if item["left"] == left and item["right"] == right)


def test_correlations_strong_link(conn) -> None:
    with conn:
        for index, day in enumerate(_days(20)):
            add_metric(conn, "sleep", 6.0 + 0.05 * index, date=day, unit="ч")
            add_metric(conn, "mood", 1.0 + 0.1 * index, date=day, unit="балл")

    pairs = build_correlations(conn, TODAY, days=30)
    sleep_mood = _pair(pairs, "sleep", "mood")
    assert sleep_mood["status"] == "ok"
    assert sleep_mood["r"] == pytest.approx(1.0)
    assert sleep_mood["n"] == 20
    assert sleep_mood["enough_data"] is True

    water_mood = _pair(pairs, "water", "mood")
    assert water_mood["status"] == "insufficient_data"
    assert water_mood["r"] is None


def test_correlations_weak_link(conn) -> None:
    with conn:
        for index, day in enumerate(_days(20)):
            add_metric(conn, "sleep", 6.0 + 0.05 * index, date=day, unit="ч")
            add_metric(conn, "mood", 5.0 if index % 2 == 0 else 1.0, date=day, unit="балл")

    pairs = build_correlations(conn, TODAY, days=30)
    sleep_mood = _pair(pairs, "sleep", "mood")
    assert sleep_mood["status"] == "weak"
    assert abs(sleep_mood["r"]) < 0.3


def test_correlations_no_variance(conn) -> None:
    with conn:
        for index, day in enumerate(_days(20)):
            add_metric(conn, "sleep", 6.0 + 0.05 * index, date=day, unit="ч")
            add_metric(conn, "mood", 3.0, date=day, unit="балл")

    pairs = build_correlations(conn, TODAY, days=30)
    sleep_mood = _pair(pairs, "sleep", "mood")
    assert sleep_mood["status"] == "no_variance"
    assert sleep_mood["r"] is None


def test_correlations_water_sum_and_training_days(conn) -> None:
    with conn:
        for index, day in enumerate(_days(20)):
            # два стакана воды в день: сумма должна стать одним значением
            add_metric(conn, "water", 800.0 + 25.0 * index, date=day, unit="мл")
            add_metric(conn, "water", 700.0 + 25.0 * index, date=day, unit="мл")
            add_metric(conn, "mood", 1.0 + 0.1 * index, date=day, unit="балл")
            trained = index % 2 == 0
            if trained:
                add_entry(conn, "training", "Силовая", ts=day + "T18:00:00+03:00")
            # тренировка ↔ вес: два уровня, связь заведомо сильная
            add_metric(conn, "weight", 90.0 if trained else 88.0, date=day, unit="кг")

    pairs = build_correlations(conn, TODAY, days=30)
    water_mood = _pair(pairs, "water", "mood")
    assert water_mood["status"] == "ok"
    assert water_mood["r"] == pytest.approx(1.0)

    training_weight = _pair(pairs, "training", "weight")
    assert training_weight["status"] == "ok"
    assert training_weight["r"] == pytest.approx(1.0)
    assert MIN_CORRELATION_DAYS == 14


# ---------------------------------------------------------------------------
# HTTP-эндпоинты
# ---------------------------------------------------------------------------


def _seed_weight(settings, today: date, *, with_goal: bool = True) -> None:
    connection = init_db(settings.db_path)
    with connection:
        for index, day in enumerate(_days(30, today)):
            add_metric(connection, "weight", 85.0 - 0.1 * index, date=day, unit="кг")
        if with_goal:
            connection.execute(
                "INSERT INTO goals (title, target, current, unit, status) "
                "VALUES ('Вес к зиме', 80, 82.1, 'кг', 'active')"
            )
    connection.close()


def test_weight_endpoint_returns_ema_and_forecast(settings) -> None:
    today = settings.now().date()
    _seed_weight(settings, today)
    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        payload = client.get("/api/health/weight?days=30&window=7").json()

    assert payload["target_kg"] == 80.0
    assert payload["current"] == pytest.approx(82.1)
    assert payload["rate_per_week"] == pytest.approx(-0.7)
    assert payload["forecast"]["status"] == "ok"
    assert payload["forecast"]["date"] == (today + timedelta(days=21)).isoformat()
    assert len(payload["points"]) == 30
    assert payload["points"][0]["date"] == _days(30, today)[0]


def test_weight_endpoint_without_goal_is_honest(settings) -> None:
    today = settings.now().date()
    _seed_weight(settings, today, with_goal=False)
    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        payload = client.get("/api/health/weight?days=30").json()

    assert payload["target_kg"] is None
    assert payload["forecast"]["status"] == "no_target"
    assert payload["forecast"]["date"] is None


def test_composition_endpoint(settings) -> None:
    today = settings.now().date()
    connection = init_db(settings.db_path)
    with connection:
        for index, day in enumerate(_days(30, today)):
            add_metric(connection, "fat", 20.0 - 0.02 * index, date=day, unit="%")
            add_metric(connection, "muscle", 35.0, date=day, unit="%")
    connection.close()

    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        payload = client.get("/api/health/composition").json()

    assert payload["windows"] == [7, 30, 90]
    items = {item["key"]: item for item in payload["items"]}
    assert items["fat"]["deltas"]["30"] == pytest.approx(-0.58)
    assert items["body_water"]["deltas"]["7"] is None


def test_correlations_endpoint(settings) -> None:
    today = settings.now().date()
    connection = init_db(settings.db_path)
    with connection:
        for index, day in enumerate(_days(20, today)):
            add_metric(connection, "sleep", 6.0 + 0.05 * index, date=day, unit="ч")
            add_metric(connection, "mood", 1.0 + 0.1 * index, date=day, unit="балл")
    connection.close()

    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        payload = client.get("/api/analytics/correlations?days=30").json()
        too_short = client.get("/api/analytics/correlations?days=7")

    assert payload["min_days"] == MIN_CORRELATION_DAYS
    assert len(payload["pairs"]) == 3
    sleep_mood = _pair(payload["pairs"], "sleep", "mood")
    assert sleep_mood["status"] == "ok"
    assert sleep_mood["r"] == pytest.approx(1.0)
    assert too_short.status_code == 422


def test_overview_insight_reports_unconfirmed_correlation(settings) -> None:
    """Слабая связь — не «мало данных»: Обзор говорит о ней честно, без порога."""
    today = settings.now().date()
    connection = init_db(settings.db_path)
    with connection:
        for index, day in enumerate(_days(20, today)):
            add_metric(connection, "sleep", 6.0 + 0.05 * index, date=day, unit="ч")
            add_metric(connection, "mood", 5.0 if index % 2 == 0 else 1.0, date=day, unit="балл")
    connection.close()

    app = create_app(settings, start_worker=False, api_token="")
    with TestClient(app) as client:
        payload = client.get("/api/overview").json()

    insight = payload["insight"]
    assert insight is not None
    assert (insight["left"], insight["right"]) == ("sleep", "mood")
    assert "Сон" in insight["title"] and "настроение" in insight["title"].lower()
    assert "→" not in insight["title"]
    assert "ложные находки" in insight["text"]
    assert "ложные находки" in insight["footnote"]
    assert "порог" not in insight["text"] + insight["footnote"]
