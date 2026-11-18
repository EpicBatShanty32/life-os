"""Тесты корреляций v2: лаги, ранги, FDR и человеческие находки (§12.6).

Синтетика с известным ответом:

* сдвиг правого ряда на 1 день находит идеальную связь (r ≈ 1, q ≈ 0);
* независимый шум не даёт находок, но честно попадает в ``notReady``;
* меньше 14 реальных дней — ``insufficient_data`` с точным числом дней;
* постоянный ряд — ``no_variance``;
* Benjamini–Hochberg проверяется на известном примере;
* t-распределение Стьюдента — по табличным значениям, интервал — по Фишеру.

Дополнительно проверяются HTTP-эндпоинты ``/api/analytics/findings``,
``/api/analytics/correlations`` (старые поля + новые) и
``/api/analytics/summary`` (поле ``correlations`` в новом виде).
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.server.api.aggregates import (
    MIN_CORRELATION_DAYS,
    bh_adjust,
    build_correlation_report,
    build_correlations,
    finding_title,
    fisher_interval,
    spearman,
    student_p_value,
)
from app.server.db import add_metric, init_db
from app.server.main import create_app
from app.server.settings import load_settings

UNITS = {"sleep": "ч", "mood": "балл", "water": "мл", "weight": "кг", "steps": "шаг"}


@pytest.fixture()
def settings(tmp_path):
    return load_settings(env={}, root=tmp_path)


def _seed(
    settings, series: dict[str, list[float]], days: int, today: date | None = None
) -> None:
    """Пишет дневные метрики: значение ``series[kind][index]`` на день ``index``."""
    today = today or settings.now().date()
    conn = init_db(settings.db_path)
    with conn:
        for index in range(days):
            day = (today - timedelta(days=days - 1 - index)).isoformat()
            for kind, values in series.items():
                add_metric(conn, kind, values[index], date=day, unit=UNITS.get(kind, ""))
    conn.close()


def _client(settings) -> TestClient:
    return TestClient(create_app(settings, start_worker=False, api_token=""))


def _noise(count: int, seed: int, base: float, spread: float) -> list[float]:
    rnd = random.Random(seed)
    return [round(base + rnd.random() * spread, 3) for _ in range(count)]


def _lagged(settings) -> tuple[list[float], list[float]]:
    """40 дней: настроение на следующий день повторяет сон предыдущего."""
    sleep = _noise(40, 42, 6.0, 2.0)
    mood = [sleep[0]] + [
        value + (0.001 if index % 2 else -0.001) for index, value in enumerate(sleep[:-1])
    ]
    _seed(settings, {"sleep": sleep, "mood": mood}, 40)
    return sleep, mood


# ---------------------------------------------------------------------------
# Чистая статистика
# ---------------------------------------------------------------------------


def test_bh_adjust_known_example() -> None:
    # p = 0.002, 0.01, 0.03, 0.04 при m = 4 → q = 0.008, 0.02, 0.04, 0.04.
    assert bh_adjust([0.002, 0.01, 0.03, 0.04]) == pytest.approx(
        [0.008, 0.02, 0.04, 0.04]
    )
    # q не меньше p, не больше 1 и монотонно по возрастанию p.
    values = [0.001, 0.02, 0.2, 0.9]
    adjusted = bh_adjust(values)
    assert all(q >= p for p, q in zip(values, adjusted))
    assert all(q <= 1.0 for q in adjusted)
    assert adjusted == sorted(adjusted)
    assert bh_adjust([]) == []


def test_student_p_value_matches_table() -> None:
    # t = 2.101, df = 18 → p ≈ 0.05; t = 2.878, df = 18 → p ≈ 0.01.
    def r_from_t(t: float, df: int) -> float:
        return t / math.sqrt(t * t + df)

    assert student_p_value(r_from_t(2.101, 18), 20) == pytest.approx(0.05, abs=0.002)
    assert student_p_value(r_from_t(2.878, 18), 20) == pytest.approx(0.01, abs=0.002)
    assert student_p_value(0.0, 20) == pytest.approx(1.0)
    assert student_p_value(1.0, 20) == 0.0
    assert student_p_value(None, 20) is None
    assert student_p_value(0.5, 2) is None  # df = 0


def test_spearman_sees_monotonic_nonlinearity() -> None:
    pairs = [(value, math.exp(value)) for value in (1, 2, 3, 4, 5)]
    assert spearman(pairs) == pytest.approx(1.0)
    # Пирсон на тех же данных заметно ниже: связь монотонная, но не линейная.
    from app.server.api.aggregates import pearson

    assert pearson(pairs) < 0.9
    # Совпадающие значения получают средние ранги — связь остаётся сильной.
    tied = [(1, 5), (2, 5), (3, 5), (4, 1), (5, 2)]
    assert spearman(tied) < -0.7
    assert spearman([(1, 2), (2, 4)]) is None


def test_fisher_interval_widens_for_small_samples() -> None:
    wide = fisher_interval(0.5, 50)
    narrow = fisher_interval(0.5, 500)
    assert wide is not None and narrow is not None
    assert wide[0] < 0.3 and wide[1] > 0.6
    assert narrow[0] > wide[0] and narrow[1] < wide[1]
    assert narrow[0] < 0.5 < narrow[1]
    assert fisher_interval(0.5, 3) is None
    assert fisher_interval(None, 50) is None


# ---------------------------------------------------------------------------
# Находки: связь с лагом, шум, мало данных, нет разброса
# ---------------------------------------------------------------------------


def test_findings_finds_lagged_link_and_passes_fdr(settings) -> None:
    _lagged(settings)
    with _client(settings) as client:
        payload = client.get("/api/analytics/findings?days=90&lags=2").json()

    today = settings.now().date()
    assert payload["window"] == {
        "from": (today - timedelta(days=89)).isoformat(),
        "to": today.isoformat(),
        "days": 90,
    }
    assert payload["tested"] >= 3
    assert payload["found"], payload
    top = payload["found"][0]
    assert (top["left"], top["right"], top["lag"]) == ("sleep", "mood", 1)
    assert top["r"] == pytest.approx(1.0, abs=0.01)
    assert top["rho"] == pytest.approx(1.0, abs=0.01)
    assert top["n"] == 39
    assert top["p"] <= 0.001
    assert top["q"] <= 0.1
    assert top["effectPct"] > 95
    assert top["strength"] == "strong"
    assert top["leftLabel"] == "Сон" and top["rightLabel"] == "Настроение"
    assert len(top["text"]) > 20 and len(top["advice"]) > 10
    assert "эмодзи" not in top["text"]
    assert top["ci"] is not None and top["ci"][0] <= 1.0
    assert not [
        item
        for item in payload["notReady"]
        if (item["left"], item["right"]) == ("sleep", "mood")
    ]


def test_findings_respects_lags_parameter(settings) -> None:
    _lagged(settings)
    with _client(settings) as client:
        lagged = client.get("/api/analytics/findings?days=90&lags=0").json()
        usual = client.get("/api/analytics/findings?days=90&lags=2").json()

    assert lagged["found"] == []  # без сдвига идеальной связи нет
    assert usual["found"]
    assert lagged["tested"] == 1  # только пара sleep↔mood без лагов


def test_findings_keeps_quiet_on_random_noise(settings) -> None:
    count = 45
    _seed(
        settings,
        {
            "sleep": _noise(count, 42, 6.0, 2.0),
            "mood": _noise(count, 7, 3.0, 4.0),
        },
        count,
    )
    with _client(settings) as client:
        payload = client.get("/api/analytics/findings?days=90&lags=2").json()

    assert payload["found"] == []
    assert payload["tested"] >= 3
    pair = next(
        item
        for item in payload["notReady"]
        if (item["left"], item["right"]) == ("sleep", "mood")
    )
    assert pair["reason"] == "fdr"
    assert pair["daysHave"] >= 14
    assert pair["needDays"] == 14


def test_findings_not_ready_when_days_short(settings) -> None:
    _seed(
        settings,
        {
            "sleep": _noise(10, 5, 6.0, 2.0),
            "mood": _noise(10, 6, 3.0, 4.0),
        },
        10,
    )
    with _client(settings) as client:
        payload = client.get("/api/analytics/findings?days=30").json()

    assert payload["tested"] == 0
    assert payload["found"] == []
    pair = next(
        item
        for item in payload["notReady"]
        if (item["left"], item["right"]) == ("sleep", "mood")
    )
    assert pair == {
        "left": "sleep",
        "right": "mood",
        "reason": "insufficient_data",
        "daysHave": 10,
        "needDays": 14,
    }


def test_findings_not_ready_without_variance(settings) -> None:
    _seed(
        settings,
        {
            "sleep": _noise(20, 8, 6.0, 2.0),
            "mood": [3.0] * 20,
        },
        20,
    )
    with _client(settings) as client:
        payload = client.get("/api/analytics/findings?days=60").json()

    assert payload["tested"] == 0
    assert payload["found"] == []
    pair = next(
        item
        for item in payload["notReady"]
        if (item["left"], item["right"]) == ("sleep", "mood")
    )
    assert pair["reason"] == "no_variance"
    assert pair["daysHave"] == 20
    assert pair["needDays"] == MIN_CORRELATION_DAYS


def test_findings_validates_query(settings) -> None:
    with _client(settings) as client:
        assert client.get("/api/analytics/findings?days=7").status_code == 422
        assert client.get("/api/analytics/findings?days=90&lags=99").status_code == 422
        assert client.get("/api/analytics/findings?days=90&lags=-1").status_code == 422


# ---------------------------------------------------------------------------
# Совместимость старых эндпоинтов
# ---------------------------------------------------------------------------


def test_correlations_endpoint_keeps_legacy_fields_and_adds_v2(settings) -> None:
    # Связь заведомо сильная: настроение = сон × 2 (плюс крошечный шум).
    sleep = _noise(20, 21, 6.0, 2.0)
    mood = [value * 2 + (0.001 if index % 2 else -0.001) for index, value in enumerate(sleep)]
    _seed(settings, {"sleep": sleep, "mood": mood}, 20)

    with _client(settings) as client:
        payload = client.get("/api/analytics/correlations?days=30").json()
        too_short = client.get("/api/analytics/correlations?days=7")

    assert payload["min_days"] == MIN_CORRELATION_DAYS
    assert len(payload["pairs"]) == 3
    pair = next(
        item for item in payload["pairs"] if (item["left"], item["right"]) == ("sleep", "mood")
    )
    # Старые поля на месте.
    for key in ("left", "right", "label", "left_label", "right_label", "n", "r", "status", "text"):
        assert key in pair
    assert pair["status"] == "ok"
    assert pair["r"] == pytest.approx(1.0, abs=0.01)
    assert pair["n"] == 20
    # Поля v2 подмешаны.
    assert pair["lag"] == 0
    assert pair["rho"] == pytest.approx(1.0, abs=0.01)
    assert pair["p"] is not None and pair["p"] <= 0.001
    assert pair["q"] is not None and pair["q"] <= 0.1
    assert pair["effectPct"] > 95
    assert pair["strength"] == "strong"
    assert len(pair["advice"]) > 10
    assert pair["ci"] is not None
    assert pair["enough_data"] is True and pair["enoughData"] is True
    # Новые данные на уровне ответа.
    assert set(payload) >= {
        "days",
        "min_days",
        "min_r",
        "window",
        "tested",
        "found",
        "notReady",
        "pairs",
    }
    assert payload["found"]
    assert too_short.status_code == 422


def test_summary_correlations_new_shape(settings) -> None:
    sleep = _noise(25, 31, 6.0, 2.0)
    mood = [value * 2 + (0.001 if index % 2 else -0.001) for index, value in enumerate(sleep)]
    _seed(settings, {"sleep": sleep, "mood": mood}, 25)

    with _client(settings) as client:
        payload = client.get("/api/analytics/summary?days=30").json()

    correlations = payload["correlations"]
    assert isinstance(correlations, list) and correlations
    pair = next(
        item for item in correlations if (item["left"], item["right"]) == ("sleep", "mood")
    )
    assert pair["status"] == "ok"
    assert pair["lag"] == 0
    assert pair["q"] is not None and pair["q"] <= 0.1
    assert pair["rho"] == pytest.approx(1.0, abs=0.01)
    assert pair["strength"] == "strong"
    assert pair["advice"]
    assert pair["left_label"] == "Сон" and pair["right_label"] == "Настроение"
    assert pair["enough_data"] is True


def test_summary_without_data_is_honest(settings) -> None:
    with _client(settings) as client:
        payload = client.get("/api/analytics/summary?days=30").json()

    correlations = payload["correlations"]
    assert isinstance(correlations, list) and correlations
    for pair in correlations:
        assert pair["r"] is None
        assert pair["status"] in {"insufficient_data", "no_variance"}
        assert pair["text"]
        assert pair["enoughData"] is False


def test_findings_route_registered(settings) -> None:
    with _client(settings) as client:
        paths = set(client.get("/openapi.json").json()["paths"])
    assert "/api/analytics/findings" in paths
    assert "/api/analytics/correlations" in paths


# ---------------------------------------------------------------------------
# Формулировки и единство Обзора с Аналитикой
# ---------------------------------------------------------------------------


def test_finding_title_has_no_causal_arrow() -> None:
    """lag=0 — нейтрально, lag>0 — порядок во времени, стрелок-причин нет."""
    assert finding_title("Сон", "Тренировки", 0, 0.5) == "Сон и тренировки идут вместе"
    assert finding_title("Настроение", "Расходы", 0, -0.4) == (
        "Настроение и расходы движутся в разные стороны"
    )
    assert finding_title("Сон", "Энергия", 1, 0.49) == "Сон вчера, энергия сегодня"
    assert finding_title("Сон", "Настроение", 2, 0.47) == (
        "Сон 2 дня назад, настроение сегодня"
    )
    for lag, r in ((0, 0.5), (0, -0.5), (1, 0.5), (2, 0.5), (7, 0.5)):
        title = finding_title("Сон", "Закрытые задачи", lag, r)
        assert "→" not in title
        assert len(title) <= 48


def test_overview_uses_same_v2_findings(settings) -> None:
    """Обзор показывает ровно топ-2 находки того же отчёта, что и Аналитика."""
    count = 40
    sleep = _noise(count, 42, 6.0, 2.0)
    mood = [sleep[0]] + [
        value + (0.001 if index % 2 else -0.001) for index, value in enumerate(sleep[:-1])
    ]
    water = [round(value * 100 + 5, 1) for value in mood]
    _seed(settings, {"sleep": sleep, "mood": mood, "water": water}, count)

    with _client(settings) as client:
        findings = client.get("/api/analytics/findings?days=90&lags=2").json()
        overview = client.get("/api/overview").json()

    found = findings["found"]
    assert len(found) >= 2
    insights = overview["insights"]
    assert len(insights) == 2
    for insight, item in zip(insights, found[:2]):
        assert (insight["left"], insight["right"], insight["lag"]) == (
            item["left"],
            item["right"],
            item["lag"],
        )
        assert insight["title"] == item["title"]
        assert insight["r"] == pytest.approx(item["r"], abs=0.001)
        assert "→" not in insight["title"]
        assert len(insight["text"]) <= 120
    assert overview["insight"] == insights[0]


def test_legacy_correlations_are_slice_of_v2(settings) -> None:
    """У старого эндпоинта нет своего порога: q и статус — из отчёта v2."""
    _lagged(settings)
    today = settings.now().date()
    conn = init_db(settings.db_path)
    pairs = build_correlations(conn, today, days=90)
    report = build_correlation_report(conn, today, days=90, lags=0)
    conn.close()

    assert len(pairs) == 3
    for pair in pairs:
        item = next(
            row
            for row in report["pairs"]
            if row["lag"] == 0
            and {row["left"], row["right"]} == {pair["left"], pair["right"]}
        )
        assert pair["status"] == item["status"]
        assert pair["q"] == item["q"]
        if item["r"] is None:
            assert pair["r"] is None
        else:
            assert pair["r"] == round(item["r"], 2)
        assert pair["lag"] == 0
        assert "→" not in pair["title"]
