"""Тесты интеграций веса: импорт JSON/CSV и список источников (§12 «Тело»).

Проверяется HTTP-слой целиком:

* три базовые интеграции (весы по файлу, Apple Health по экспорту, ручной
  ввод) и их поля;
* импорт десяти точек, повторная загрузка того же файла не плодит дубли;
* дедупликация по паре ``(date, source)``, а не только по дате;
* 422 на кривые даты/вес/слишком большой файл — и ни одной записи в БД;
* CSV с разделителем «;» и десятичной запятой, а также «,» без заголовка;
* PATCH ``enabled``: выключенный источник отвечает 409 и не пишет вес;
* существующий ``GET /api/health/weight`` видит импортированный ряд.

База — во временном каталоге; рабочая ``data/`` не затрагивается.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.server.db import connect, init_db
from app.server.main import create_app
from app.server.seeder import seed
from app.server.settings import load_settings

TODAY = date(2026, 11, 18)


@pytest.fixture()
def api(tmp_path):
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


# --------------------------------------------------------------------------
# Помощники
# --------------------------------------------------------------------------


def _sql(settings, query: str, params: tuple = ()) -> Any:
    conn = connect(settings.db_path)
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def _rows(settings, query: str, params: tuple = ()) -> list[Any]:
    conn = connect(settings.db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _point(days_ago: int, weight: float, today: date = TODAY) -> dict[str, Any]:
    return {
        "date": (today - timedelta(days=days_ago)).isoformat(),
        "weight": weight,
    }


def _import(
    client: TestClient,
    points: list[dict[str, Any]],
    source: str = "scale",
) -> Any:
    return client.post(
        "/api/health/weight/import",
        json={"source": source, "points": points},
    )


def _import_csv(client: TestClient, body: str, source: str = "scale") -> Any:
    return client.post(
        "/api/health/weight/import/csv?source=%s" % source,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/csv"},
    )


def _weight_count(settings, source: str | None = None) -> int:
    if source is None:
        row = _sql(settings, "SELECT COUNT(*) FROM metrics WHERE kind = 'weight'")
    else:
        row = _sql(
            settings,
            "SELECT COUNT(*) FROM metrics WHERE kind = 'weight' AND source = ?",
            (source,),
        )
    return int(row[0])


# --------------------------------------------------------------------------
# Схема и список интеграций
# --------------------------------------------------------------------------


def test_integrations_table_and_seed(settings) -> None:
    """Миграция создаёт таблицу, а схема хранит три честных источника."""
    columns = {
        str(row["name"])
        for row in _rows(settings, "PRAGMA table_info(integrations)")
    }
    assert {"id", "key", "title", "kind", "enabled", "last_sync", "notes"} <= columns

    rows = _rows(
        settings,
        "SELECT key, title, kind, enabled, last_sync FROM integrations ORDER BY id",
    )
    assert [str(row["key"]) for row in rows] == ["scale", "apple-health", "manual"]
    assert [str(row["kind"]) for row in rows] == ["import", "import", "manual"]
    assert all(int(row["enabled"]) == 1 for row in rows)
    assert all(row["last_sync"] is None for row in rows)


def test_seed_adds_three_integrations(tmp_path) -> None:
    """Сид гарантирует три источника и не плодит их при повторном прогоне."""
    settings = load_settings(env={}, root=tmp_path)
    conn = init_db(settings.db_path)
    try:
        first = seed(conn, days=2, today=TODAY)
        assert first.integrations == 3
        rows = conn.execute("SELECT key FROM integrations ORDER BY id").fetchall()
        assert [str(row["key"]) for row in rows] == ["scale", "apple-health", "manual"]

        second = seed(conn, days=2, today=TODAY)
        assert second.integrations == 3
        assert conn.execute("SELECT COUNT(*) FROM integrations").fetchone()[0] == 3
    finally:
        conn.close()


def test_integrations_list_contract(client: TestClient) -> None:
    """GET /api/integrations: ключи, подписи, kind, enabled, lastSync, notes."""
    response = client.get("/api/integrations")
    assert response.status_code == 200
    payload = response.json()

    assert [item["key"] for item in payload] == ["scale", "apple-health", "manual"]
    for item in payload:
        assert set(item) == {"key", "title", "kind", "enabled", "lastSync", "notes"}
        assert item["title"]
        assert item["kind"] in {"import", "manual"}
        assert item["enabled"] is True
        assert item["lastSync"] is None
        assert item["notes"]

    by_key = {item["key"]: item for item in payload}
    # Честно про отсутствие OAuth: это импорт файла, а не подключение сервиса.
    assert "OAuth" in by_key["scale"]["notes"]
    assert "OAuth" in by_key["apple-health"]["notes"]
    assert "ручн" in by_key["manual"]["title"].lower()


# --------------------------------------------------------------------------
# JSON-импорт
# --------------------------------------------------------------------------


def test_import_ten_points(client: TestClient, settings) -> None:
    points = [_point(9 - index, 72.0 + index / 10.0) for index in range(10)]
    response = _import(client, points)
    assert response.status_code == 200, response.text

    result = response.json()
    assert result["imported"] == 10
    assert result["skipped"] == 0
    assert result["firstDate"] == points[0]["date"]
    assert result["lastDate"] == points[-1]["date"]

    rows = _rows(
        settings,
        "SELECT date, value, unit, source FROM metrics "
        "WHERE kind = 'weight' ORDER BY date",
    )
    assert len(rows) == 10
    assert [(str(row["date"]), str(row["unit"]), str(row["source"])) for row in rows] == [
        (point["date"], "кг", "scale") for point in points
    ]
    assert [float(row["value"]) for row in rows] == [point["weight"] for point in points]

    # lastSync появился после импорта.
    listed = {item["key"]: item for item in client.get("/api/integrations").json()}
    assert listed["scale"]["lastSync"] is not None


def test_repeat_import_is_idempotent(client: TestClient, settings) -> None:
    points = [_point(9 - index, 72.0 + index / 10.0) for index in range(10)]
    first = _import(client, points)
    assert first.status_code == 200 and first.json()["imported"] == 10

    second = _import(client, points)
    assert second.status_code == 200, second.text
    assert second.json()["imported"] == 0
    assert second.json()["skipped"] == 10
    assert second.json()["firstDate"] is None
    assert second.json()["lastDate"] is None
    assert _weight_count(settings) == 10


def test_duplicate_dates_inside_request(client: TestClient, settings) -> None:
    """Две точки на одну дату: первая пишется, вторая считается пропуском."""
    point = _point(1, 71.5)
    response = _import(client, [point, dict(point, weight=71.6)])
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 1
    assert response.json()["skipped"] == 1
    assert _weight_count(settings) == 1


def test_dedup_is_per_source(client: TestClient, settings) -> None:
    """Одна дата из весов и из Apple Health — две честные записи."""
    point = _point(3, 72.2)
    assert _import(client, [point], source="scale").status_code == 200
    assert _import(client, [point], source="apple-health").status_code == 200
    rows = _rows(
        settings,
        "SELECT source FROM metrics WHERE kind = 'weight' AND date = ? ORDER BY source",
        (point["date"],),
    )
    assert [str(row["source"]) for row in rows] == ["apple-health", "scale"]


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "scale", "points": [{"date": "17.11.2026", "weight": 72.0}]},
        {"source": "scale", "points": [{"date": "2026-02-30", "weight": 72.0}]},
        {"source": "scale", "points": [{"date": "2026-11-01", "weight": 29.9}]},
        {"source": "scale", "points": [{"date": "2026-11-01", "weight": 350.1}]},
        {"source": "scale", "points": [{"date": "2026-11-01", "weight": "abc"}]},
        {"source": "scale", "points": []},
        {"source": "scale", "points": [{"date": "2026-11-01", "weight": 72, "extra": 1}]},
        {"source": "file", "points": [{"date": "2026-11-01", "weight": 72}]},
        {"source": "", "points": [{"date": "2026-11-01", "weight": 72}]},
    ],
)
def test_invalid_json_import_returns_422(
    client: TestClient, settings, payload: dict[str, Any]
) -> None:
    response = client.post("/api/health/weight/import", json=payload)
    assert response.status_code == 422, response.text
    assert _weight_count(settings) == 0


def test_too_many_points_returns_422(client: TestClient, settings) -> None:
    points = [_point(index, 72.0) for index in range(2001)]
    response = _import(client, points)
    assert response.status_code == 422
    assert _weight_count(settings) == 0


# --------------------------------------------------------------------------
# CSV-импорт
# --------------------------------------------------------------------------


def test_csv_semicolon_with_header_and_decimal_comma(client: TestClient, settings) -> None:
    body = "Дата;Вес\n%s;72,4\n%s;71,8\n" % (
        (TODAY - timedelta(days=2)).isoformat(),
        (TODAY - timedelta(days=1)).isoformat(),
    )
    response = _import_csv(client, body)
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 2

    rows = _rows(
        settings,
        "SELECT value FROM metrics WHERE kind = 'weight' ORDER BY date",
    )
    assert [float(row["value"]) for row in rows] == [72.4, 71.8]


def test_csv_comma_without_header(client: TestClient, settings) -> None:
    body = "%s,73.1\n%s,73.2\n" % (
        (TODAY - timedelta(days=2)).isoformat(),
        (TODAY - timedelta(days=1)).isoformat(),
    )
    response = _import_csv(client, body)
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 2
    assert response.json()["firstDate"] == (TODAY - timedelta(days=2)).isoformat()
    assert response.json()["lastDate"] == (TODAY - timedelta(days=1)).isoformat()


@pytest.mark.parametrize(
    "body",
    [
        "",
        "Дата;Вес\n",
        "2026-11-01\n",
        "17.11.2026;72\n",
        "2026-11-01;29\n",
        "2026-11-01;350.1\n",
        "2026-11-01;abc\n",
        "2026-11-01;72;1\n",
    ],
)
def test_csv_bad_rows_return_422(client: TestClient, settings, body: str) -> None:
    response = _import_csv(client, body)
    assert response.status_code == 422, response.text
    assert _weight_count(settings) == 0


def test_csv_import_is_idempotent(client: TestClient, settings) -> None:
    body = "%s,72.0\n" % (TODAY - timedelta(days=1)).isoformat()
    assert _import_csv(client, body).status_code == 200
    second = _import_csv(client, body)
    assert second.status_code == 200
    assert second.json()["imported"] == 0
    assert second.json()["skipped"] == 1
    assert _weight_count(settings) == 1


def test_csv_respects_source_and_disabled_integration(client: TestClient, settings) -> None:
    """CSV-источник задаётся query; выключенный Apple Health не принимает файл."""
    client.patch("/api/integrations/apple-health", json={"enabled": False})
    response = _import_csv(
        client,
        "%s;72.0\n" % (TODAY - timedelta(days=1)).isoformat(),
        source="apple-health",
    )
    assert response.status_code == 409
    assert _weight_count(settings) == 0

    client.patch("/api/integrations/apple-health", json={"enabled": True})
    response = _import_csv(
        client,
        "%s;72.0\n" % (TODAY - timedelta(days=1)).isoformat(),
        source="apple-health",
    )
    assert response.status_code == 200
    assert _weight_count(settings, "apple-health") == 1


# --------------------------------------------------------------------------
# PATCH enabled и ряд веса
# --------------------------------------------------------------------------


def test_patch_integration_toggle(client: TestClient, settings) -> None:
    response = client.patch("/api/integrations/scale", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    assert response.json()["key"] == "scale"

    listed = {item["key"]: item for item in client.get("/api/integrations").json()}
    assert listed["scale"]["enabled"] is False

    blocked = _import(client, [_point(1, 72.0)])
    assert blocked.status_code == 409
    assert _weight_count(settings) == 0

    response = client.patch("/api/integrations/scale", json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert _import(client, [_point(1, 72.0)]).status_code == 200


@pytest.mark.parametrize(
    "key, payload, status",
    [
        ("nope", {"enabled": False}, 404),
        ("scale", {}, 422),
        ("scale", {"enabled": "no"}, 422),
        ("scale", {"enabled": False, "extra": 1}, 422),
    ],
)
def test_patch_integration_validation(
    client: TestClient, key: str, payload: dict[str, Any], status: int
) -> None:
    assert client.patch("/api/integrations/%s" % key, json=payload).status_code == status


def test_weight_series_sees_imported_points(client: TestClient, settings) -> None:
    """Существующий ``GET /api/health/weight`` не сломан и видит импорт."""
    points = [_point(9 - index, 72.0 + index / 10.0) for index in range(10)]
    assert _import(client, points).status_code == 200

    response = client.get("/api/health/weight?days=90")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["days"] == 90
    assert len(payload["points"]) == 10
    assert payload["points"][0]["date"] == points[0]["date"]
    assert payload["points"][-1]["date"] == points[-1]["date"]
    assert payload["current"] == pytest.approx(points[-1]["weight"], abs=0.01)
