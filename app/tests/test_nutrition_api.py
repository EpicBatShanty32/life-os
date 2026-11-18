"""Тесты API Питания (``app/server/api/nutrition.py``).

Проверяется HTTP-слой целиком: день с приёмами пищи и итогами, запись из
продукта и свободным текстом, пересчёт макросов при ``PATCH grams``, поиск
по справочнику, нормы, сводка за период, валидация 4xx и идемпотентность
демо-сида (второй прогон даёт те же суммы дневника).

База — во временном каталоге (``tmp_path``), рабочая ``data/`` не затрагивается.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.server.db import connect, migrate
from app.server.main import create_app
from app.server.seeder import seed
from app.server.settings import load_settings

TODAY = date(2026, 11, 17)
OTHER = date(2026, 11, 16)


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


def _sql(settings, query: str, params: tuple = ()):
    conn = connect(settings.db_path)
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def _create_product(client: TestClient, **fields) -> dict:
    payload = {"name": "Гречка ядрица", "kcal": 343, "protein": 12.6, "fat": 3.3, "carbs": 57.1}
    payload.update(fields)
    response = client.post("/api/nutrition/products", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_entry(client: TestClient, **fields) -> dict:
    payload = {"date": TODAY.isoformat(), "meal": "breakfast", "grams": 150}
    payload.update(fields)
    response = client.post("/api/nutrition/entries", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _day(client: TestClient, day: date = TODAY) -> dict:
    response = client.get("/api/nutrition/day", params={"date": day.isoformat()})
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# День
# --------------------------------------------------------------------------


def test_day_empty(client: TestClient) -> None:
    """Пустой день: четыре приёма пищи, нули, дефолтные нормы и вода."""
    payload = _day(client)

    assert payload["date"] == TODAY.isoformat()
    assert [meal["meal"] for meal in payload["meals"]] == [
        "breakfast",
        "lunch",
        "dinner",
        "snack",
    ]
    assert [meal["label"] for meal in payload["meals"]] == [
        "Завтрак",
        "Обед",
        "Ужин",
        "Перекус",
    ]
    for meal in payload["meals"]:
        assert meal["entries"] == []
        assert meal["totals"]["kcal"] == 0.0

    assert payload["totals"] == {
        "kcal": 0.0,
        "protein": 0.0,
        "fat": 0.0,
        "carbs": 0.0,
        "fiber": 0.0,
        "sugar": 0.0,
    }
    assert payload["targets"]["kcal"] == 2300.0
    assert payload["targets"]["protein"] == 150.0
    assert payload["targets"]["waterMl"] == 2200.0
    assert payload["remaining"]["kcal"] == 2300.0
    assert payload["water"] == {"ml": 0.0, "target": 2200.0}


def test_day_with_entries_and_totals(client: TestClient) -> None:
    """Записи попадают в свой приём пищи, итоги и остаток считаются."""
    product = _create_product(client)
    _create_entry(client, productId=product["id"], grams=150)
    _create_entry(
        client,
        meal="lunch",
        grams=300,
        text="Суп куриный",
        kcal=165,
        protein=13.5,
        fat=7.5,
        carbs=10.5,
    )

    payload = _day(client)
    breakfast, lunch = payload["meals"][0], payload["meals"][1]
    assert len(breakfast["entries"]) == 1
    assert breakfast["entries"][0]["text"] == "Гречка ядрица"
    assert breakfast["entries"][0]["grams"] == 150.0
    assert breakfast["entries"][0]["kcal"] == 514.5  # 343 × 1,5
    assert breakfast["entries"][0]["protein"] == 18.9
    assert breakfast["entries"][0]["productId"] == product["id"]

    assert lunch["totals"]["kcal"] == 165.0
    assert lunch["totals"]["protein"] == 13.5

    assert payload["totals"]["kcal"] == 679.5
    assert payload["remaining"]["kcal"] == 1620.5
    assert payload["remaining"]["fat"] == 62.6


def test_day_water_from_metrics(client: TestClient, settings) -> None:
    """Вода дня берётся из метрики дня, а не из дневника."""
    conn = connect(settings.db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO metrics(date, kind, value, unit) VALUES (?, 'water', 1800, 'мл')",
                (TODAY.isoformat(),),
            )
    finally:
        conn.close()
    assert _day(client)["water"] == {"ml": 1800.0, "target": 2200.0}


# --------------------------------------------------------------------------
# Записи: создание, пересчёт, удаление
# --------------------------------------------------------------------------


def test_create_entry_from_product(client: TestClient, settings) -> None:
    """Макросы считаются из продукта × граммы / 100 и пишутся в БД."""
    product = _create_product(client, name="Овсяные хлопья", kcal=352, protein=12.3, fat=6.1, carbs=59.5)
    entry = _create_entry(client, productId=product["id"], grams=80)

    assert entry["kcal"] == 281.6  # 352 × 0,8
    assert entry["protein"] == 9.8
    assert entry["fat"] == 4.9
    assert entry["carbs"] == 47.6
    assert entry["grams"] == 80.0
    assert entry["meal"] == "breakfast"
    assert entry["date"] == TODAY.isoformat()
    assert entry["text"] == "Овсяные хлопья"

    row = _sql(settings, "SELECT kcal, protein, product_id, source FROM food_log")
    assert row["kcal"] == 281.6
    assert row["protein"] == 9.8
    assert row["product_id"] == product["id"]
    assert row["source"] == ""


def test_create_entry_freetext(client: TestClient, settings) -> None:
    """Свободная запись: макросы из запроса, граммовка сохраняется."""
    entry = _create_entry(
        client,
        meal="snack",
        grams=45,
        text="Батончик протеиновый",
        kcal=144,
        protein=13.5,
        fat=4.5,
        carbs=12.6,
        fiber=2.7,
        sugar=1.8,
    )
    assert entry["productId"] is None
    assert entry["kcal"] == 144.0
    assert entry["fiber"] == 2.7
    assert entry["sugar"] == 1.8
    assert entry["meal"] == "snack"

    row = _sql(settings, "SELECT product_id, text FROM food_log")
    assert row["product_id"] is None
    assert row["text"] == "Батончик протеиновый"


def test_patch_entry_grams_recalculates_product(client: TestClient) -> None:
    """PATCH grams для записи из продукта пересчитывает макросы от продукта."""
    product = _create_product(client, name="Куриная грудка", kcal=113, protein=23.6, fat=1.9, carbs=0.4)
    entry = _create_entry(client, productId=product["id"], grams=100)
    assert entry["kcal"] == 113.0

    response = client.patch("/api/nutrition/entries/%d" % entry["id"], json={"grams": 250})
    assert response.status_code == 200, response.text
    patched = response.json()
    assert patched["grams"] == 250.0
    assert patched["kcal"] == 282.5  # 113 × 2,5
    assert patched["protein"] == 59.0
    assert patched["fat"] == 4.8

    # Повторный патч возвращает то же число (идемпотентность).
    again = client.patch("/api/nutrition/entries/%d" % entry["id"], json={"grams": 250})
    assert again.json() == patched


def test_patch_entry_grams_scales_freetext(client: TestClient) -> None:
    """Свободная запись масштабируется пропорционально граммовке."""
    entry = _create_entry(
        client, grams=200, text="Плов", kcal=370, protein=16, fat=12, carbs=48
    )
    response = client.patch("/api/nutrition/entries/%d" % entry["id"], json={"grams": 100})
    assert response.status_code == 200, response.text
    patched = response.json()
    assert patched["kcal"] == 185.0
    assert patched["protein"] == 8.0
    assert patched["fat"] == 6.0
    assert patched["carbs"] == 24.0


def test_patch_entry_meal_and_delete(client: TestClient) -> None:
    """PATCH meal переносит запись, DELETE убирает её из дня."""
    entry = _create_entry(
        client, grams=100, text="Яблоко", kcal=52, protein=0.3, fat=0.2, carbs=14
    )
    moved = client.patch("/api/nutrition/entries/%d" % entry["id"], json={"meal": "dinner"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["meal"] == "dinner"
    assert moved.json()["kcal"] == 52.0

    payload = _day(client)
    assert payload["meals"][0]["entries"] == []
    assert len(payload["meals"][2]["entries"]) == 1

    removed = client.delete("/api/nutrition/entries/%d" % entry["id"])
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"ok": True, "id": entry["id"]}
    assert _day(client)["totals"]["kcal"] == 0.0

    gone = client.delete("/api/nutrition/entries/%d" % entry["id"])
    assert gone.status_code == 404
    assert gone.json()["detail"] == "Запись дневника не найдена"


# --------------------------------------------------------------------------
# Справочник продуктов
# --------------------------------------------------------------------------


def test_products_search_and_archive(client: TestClient) -> None:
    """Поиск по названию и бренду, лимит, архивные не находятся."""
    _create_product(client, name="Рис басмати", brand="Мистраль", kcal=349)
    _create_product(client, name="Треска филе", brand="Русское море", kcal=82)
    archived = _create_product(client, name="Старый продукт", kcal=100)

    by_name = client.get("/api/nutrition/products", params={"q": "рис"}).json()
    assert [item["name"] for item in by_name["items"]] == ["Рис басмати"]

    by_brand = client.get("/api/nutrition/products", params={"q": "море"}).json()
    assert [item["name"] for item in by_brand["items"]] == ["Треска филе"]

    limited = client.get("/api/nutrition/products", params={"limit": 1}).json()
    assert len(limited["items"]) == 1

    response = client.patch(
        "/api/nutrition/products/%d" % archived["id"], json={"archived": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["archived"] is True
    assert all(
        item["id"] != archived["id"] for item in client.get("/api/nutrition/products").json()["items"]
    )

    empty = client.get("/api/nutrition/products", params={"q": "нет-такого"}).json()
    assert empty == {"items": [], "total": 0}


def test_patch_product_fields(client: TestClient) -> None:
    """PATCH правит КБЖУ и бренд, не затирая остальные поля."""
    product = _create_product(client, name="Творог", brand="", kcal=121, protein=17)
    response = client.patch(
        "/api/nutrition/products/%d" % product["id"],
        json={"protein": 18.0, "brand": "Простоквашино"},
    )
    assert response.status_code == 200, response.text
    patched = response.json()
    assert patched["protein"] == 18.0
    assert patched["brand"] == "Простоквашино"
    assert patched["kcal"] == 121.0
    assert patched["baseGrams"] == 100.0


# --------------------------------------------------------------------------
# Нормы и сводка
# --------------------------------------------------------------------------


def test_targets_get_and_put(client: TestClient, settings) -> None:
    """PUT меняет переданные нормы, остальные остаются; GET отдаёт их же."""
    defaults = client.get("/api/nutrition/targets").json()
    assert defaults["kcal"] == 2300.0
    assert defaults["updated"] is not None

    response = client.put(
        "/api/nutrition/targets", json={"kcal": 2100, "protein": 160, "waterMl": 2500}
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["kcal"] == 2100.0
    assert updated["protein"] == 160.0
    assert updated["waterMl"] == 2500.0
    assert updated["fat"] == 75.0

    assert client.get("/api/nutrition/targets").json() == updated
    assert _sql(settings, "SELECT COUNT(*) AS n FROM nutrition_targets")["n"] == 1
    assert _day(client)["water"]["target"] == 2500.0
    assert _day(client)["remaining"]["kcal"] == 2100.0


def test_targets_validation(client: TestClient) -> None:
    """Нулевые калории и отрицательная вода — 422."""
    assert client.put("/api/nutrition/targets", json={"kcal": 0}).status_code == 422
    assert client.put("/api/nutrition/targets", json={"waterMl": -1}).status_code == 422


def test_summary(client: TestClient, settings) -> None:
    """Сводка: дневные суммы, средние по дням с записями и покрытие 0..1."""
    today = date.fromisoformat(settings.today())
    yesterday = today - timedelta(days=1)
    _create_entry(
        client,
        date=yesterday.isoformat(),
        grams=100,
        text="Овсянка",
        kcal=200,
        protein=10,
        fat=5,
        carbs=30,
    )
    _create_entry(
        client,
        date=today.isoformat(),
        grams=100,
        text="Курица",
        kcal=300,
        protein=50,
        fat=10,
        carbs=0,
    )

    payload = client.get(
        "/api/nutrition/summary", params={"days": 7}
    ).json()
    assert payload["days"] == 7
    assert payload["daysWithEntries"] == 2
    assert [item["date"] for item in payload["daily"]] == [
        yesterday.isoformat(),
        today.isoformat(),
    ]
    assert payload["daily"][1]["kcal"] == 300.0
    assert payload["average"]["kcal"] == 250.0
    assert payload["average"]["protein"] == 30.0
    assert payload["coverage"]["kcal"] == round(250 / 2300, 3)
    assert payload["coverage"]["protein"] == round(30 / 150, 3)
    assert 0.0 <= payload["coverage"]["kcal"] <= 1.0


def test_summary_without_entries(client: TestClient, settings) -> None:
    """Пустая сводка: ни одного дня, нулевые средние и покрытие."""
    empty = client.get("/api/nutrition/summary").json()
    assert empty["days"] == 30
    assert empty["endDate"] == settings.today()
    assert empty["daily"] == []
    assert empty["daysWithEntries"] == 0
    assert empty["average"]["kcal"] == 0.0
    assert empty["coverage"]["kcal"] == 0.0


def test_summary_coverage_capped_at_one(client: TestClient, settings) -> None:
    """Перебор по макросу не даёт покрытие больше единицы."""
    _create_entry(
        client,
        date=settings.today(),
        grams=100,
        text="Протеин",
        kcal=1000,
        protein=400,
        fat=10,
        carbs=10,
    )
    payload = client.get("/api/nutrition/summary", params={"days": 1}).json()
    assert payload["coverage"]["protein"] == 1.0


# --------------------------------------------------------------------------
# Валидация
# --------------------------------------------------------------------------


def test_validation_errors(client: TestClient) -> None:
    """4xx на плохие данные: дата, приём пищи, граммовка, продукт, продукты."""
    assert client.get("/api/nutrition/day", params={"date": "17.11.2026"}).status_code == 422
    assert client.get("/api/nutrition/summary", params={"days": 0}).status_code == 422
    assert (
        client.post(
            "/api/nutrition/entries",
            json={"date": TODAY.isoformat(), "meal": "brunch", "grams": 100},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/nutrition/entries",
            json={"date": TODAY.isoformat(), "meal": "lunch", "grams": 0, "text": "x", "kcal": 1},
        ).status_code
        == 422
    )
    missing_macros = client.post(
        "/api/nutrition/entries",
        json={"date": TODAY.isoformat(), "meal": "lunch", "grams": 100, "text": "Суп"},
    )
    assert missing_macros.status_code == 422
    assert "kcal" in missing_macros.json()["detail"]

    no_text = client.post(
        "/api/nutrition/entries",
        json={"date": TODAY.isoformat(), "meal": "lunch", "grams": 100},
    )
    assert no_text.status_code == 422

    unknown_product = client.post(
        "/api/nutrition/entries",
        json={"meal": "lunch", "grams": 100, "productId": 99999},
    )
    assert unknown_product.status_code == 404
    assert unknown_product.json()["detail"] == "Продукт не найден"

    assert client.patch("/api/nutrition/entries/99999", json={"grams": 10}).status_code == 404
    assert client.patch("/api/nutrition/products/99999", json={"kcal": 10}).status_code == 404
    assert (
        client.post("/api/nutrition/products", json={"name": " ", "kcal": 10}).status_code == 422
    )


# --------------------------------------------------------------------------
# Демо-сид: дневник и идемпотентность
# --------------------------------------------------------------------------


def test_seed_fills_diary_and_is_idempotent(tmp_path: Path) -> None:
    """Сид: ~120 продуктов и 60–90 дней дневника; второй прогон — те же суммы."""
    settings = load_settings(env={}, root=tmp_path)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        first = seed(conn, days=120, today=TODAY)

        products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        entries = conn.execute("SELECT COUNT(*) FROM food_log").fetchone()[0]
        days = conn.execute("SELECT COUNT(DISTINCT date) FROM food_log").fetchone()[0]
        per_day = conn.execute(
            "SELECT date, COUNT(DISTINCT meal), ROUND(SUM(kcal)) FROM food_log GROUP BY date"
        ).fetchall()

        assert products >= 120
        assert first.products == products
        assert first.food_log == entries
        assert 60 <= days <= 90
        for _, meals, kcal in per_day:
            assert 3 <= int(meals) <= 4
            assert 1800 <= float(kcal) <= 2400
        # Продукты связаны с записями, а «пищевых» записей в журнале не осталось.
        assert conn.execute(
            "SELECT COUNT(*) FROM food_log WHERE product_id IS NULL"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE kind = 'food'"
        ).fetchone()[0] == 0

        before = conn.execute(
            "SELECT COUNT(*), ROUND(SUM(kcal), 1), ROUND(SUM(protein), 1) FROM food_log"
        ).fetchone()[:]
        second = seed(conn, days=120, today=TODAY)
        after = conn.execute(
            "SELECT COUNT(*), ROUND(SUM(kcal), 1), ROUND(SUM(protein), 1) FROM food_log"
        ).fetchone()[:]
        assert before == after
        assert second.food_log == first.food_log
        assert second.products == first.products
    finally:
        conn.close()


def test_seeded_day_api(tmp_path: Path) -> None:
    """После сида день отдаёт заполненные приёмы пищи и ненулевые итоги."""
    settings = load_settings(env={}, root=tmp_path)
    app = create_app(settings, start_worker=False, api_token="")

    with TestClient(app) as client:
        # Миграции выполняются на старте приложения (lifespan), сид — после.
        conn = connect(settings.db_path)
        try:
            seed(conn, days=30, today=date.fromisoformat(settings.today()))
        finally:
            conn.close()

        payload = client.get("/api/nutrition/day").json()
        assert payload["date"] == settings.today()
        assert payload["totals"]["kcal"] > 0
        assert payload["totals"]["protein"] > 0
        filled = [meal for meal in payload["meals"] if meal["entries"]]
        assert len(filled) >= 3
        assert payload["water"]["ml"] > 0

        summary = client.get("/api/nutrition/summary", params={"days": 7}).json()
        assert summary["daysWithEntries"] >= 7
        assert summary["average"]["kcal"] > 0
