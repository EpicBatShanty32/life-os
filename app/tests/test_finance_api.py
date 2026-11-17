"""Тесты API Финансов (``app/server/api/finance.py``).

Проверяется HTTP-слой: деньги в рублях на входе и копейках в БД (округление
half-up, ``1250.5`` → ``125050``), фильтры транзакций, суммы/доли категорий,
дневная динамика, бюджеты с ``pct``/``over`` и upsert по месяцу, а также
валидация (месяц/вид/сумма) и пустой месяц без ошибок.

База — во временном каталоге (``tmp_path``), рабочая ``data/`` не затрагивается.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.server.db import connect
from app.server.main import create_app
from app.server.settings import load_settings

MONTH = "2026-05"
DAY_1 = "2026-05-01"
DAY_2 = "2026-05-02"
DAY_5 = "2026-05-05"
NEXT_MONTH = "2026-06"
NEXT_DAY = "2026-06-03"


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


def _count(settings, table: str, where: str = "", params: tuple = ()) -> int:
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT count(*) FROM %s %s" % (table, where), params).fetchone()
        return int(row[0]) if row is not None else -1
    finally:
        conn.close()


def _account(client: TestClient, name: str = "Карта", **fields) -> dict:
    payload = {"name": name, "kind": "card"}
    payload.update(fields)
    response = client.post("/api/finance/accounts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _category(client: TestClient, name: str, kind: str = "expense", **fields) -> dict:
    payload = {"name": name, "kind": kind}
    payload.update(fields)
    response = client.post("/api/finance/categories", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _tx(client: TestClient, amount, kind: str = "expense", day: str = DAY_1, **fields) -> dict:
    payload = {"date": day, "amount": amount, "kind": kind}
    payload.update(fields)
    response = client.post("/api/finance/transactions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _summary(client: TestClient, month: str = MONTH) -> dict:
    response = client.get("/api/finance/summary", params={"month": month})
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# Пустой месяц и валидация месяца
# --------------------------------------------------------------------------


def test_summary_empty_month(client: TestClient) -> None:
    payload = _summary(client)
    assert payload["month"] == MONTH
    assert payload["income"] == 0.0
    assert payload["expense"] == 0.0
    assert payload["balance"] == 0.0
    assert payload["incomeKopecks"] == 0
    assert payload["expenseKopecks"] == 0
    assert payload["byCategory"] == []
    assert payload["budgets"] == []
    assert payload["daily"] == []


def test_summary_default_month_is_current(client: TestClient, settings) -> None:
    today = settings.today()
    response = client.get("/api/finance/summary")
    assert response.status_code == 200
    assert response.json()["month"] == today[:7]

    _tx(client, 100, day=today)
    current = _summary(client, today[:7])
    assert current["expense"] == 100.0


@pytest.mark.parametrize("bad", ["2026-13", "2026-5", "2026-00", "май", "2026/05"])
def test_month_validation(client: TestClient, bad: str) -> None:
    for url in (
        "/api/finance/summary",
        "/api/finance/transactions",
        "/api/finance/budgets",
    ):
        response = client.get(url, params={"month": bad})
        assert response.status_code == 422, (url, response.text)
    assert client.put(
        "/api/finance/budgets", json={"categoryId": 1, "month": bad, "limit": 100}
    ).status_code == 422


def test_empty_month_filter_ignores_other_months(client: TestClient) -> None:
    _tx(client, 500, day=NEXT_DAY)
    payload = _summary(client)
    assert payload["expense"] == 0.0
    assert payload["daily"] == []
    assert _summary(client, NEXT_MONTH)["expense"] == 500.0


# --------------------------------------------------------------------------
# Счета
# --------------------------------------------------------------------------


def test_account_create_and_rounding(client: TestClient, settings) -> None:
    account = _account(client, "Карта зарплатная", balanceStart=1250.5)
    assert account["name"] == "Карта зарплатная"
    assert account["kind"] == "card"
    assert account["currency"] == "RUB"
    assert account["balanceStart"] == 1250.5
    assert account["balanceStartKopecks"] == 125050
    assert account["archived"] is False
    assert account["sort"] == 0

    kopecks = _count(settings, "finance_accounts", "WHERE id = ?", (account["id"],))
    assert kopecks == 1
    conn = connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT balance_start_kopecks FROM finance_accounts WHERE id = ?",
            (account["id"],),
        ).fetchone()
        assert int(row["balance_start_kopecks"]) == 125050
    finally:
        conn.close()

    # Полкопейки вверх: 10.005 ₽ → 1001 копейка.
    rounded = _account(client, "Копейки", balanceStart=10.005)
    assert rounded["balanceStartKopecks"] == 1001
    assert rounded["balanceStart"] == 10.01


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "Счёт", "kind": "card2"},
        {"name": "Счёт", "currency": ""},
        {"name": "Счёт", "balanceStart": "много"},
    ],
)
def test_account_create_validation(client: TestClient, payload: dict) -> None:
    assert client.post("/api/finance/accounts", json=payload).status_code == 422


def test_account_patch_and_list(client: TestClient) -> None:
    first = _account(client, "Наличные", kind="cash", sort=1, balanceStart=-500)
    second = _account(client, "Вклад", kind="savings", sort=0, balanceStart=1000)
    assert first["balanceStartKopecks"] == -50000

    listed = client.get("/api/finance/accounts").json()["items"]
    assert [item["id"] for item in listed] == [second["id"], first["id"]]

    patched = client.patch(
        "/api/finance/accounts/%d" % first["id"],
        json={"name": "Карман", "archived": True, "balanceStart": 250.25},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Карман"
    assert patched.json()["archived"] is True
    assert patched.json()["balanceStartKopecks"] == 25025

    # Архивные — в конце списка.
    listed = client.get("/api/finance/accounts").json()["items"]
    assert listed[-1]["id"] == first["id"]

    assert client.patch("/api/finance/accounts/9999", json={"name": "Нет"}).status_code == 404
    assert client.patch("/api/finance/accounts/%d" % first["id"], json={}).status_code == 400
    assert (
        client.patch("/api/finance/accounts/%d" % first["id"], json={"name": None}).status_code
        == 422
    )
    assert (
        client.patch("/api/finance/accounts/%d" % first["id"], json={"archived": None}).status_code
        == 422
    )


# --------------------------------------------------------------------------
# Категории
# --------------------------------------------------------------------------


def test_category_create_and_duplicates(client: TestClient) -> None:
    food = _category(client, "Еда и продукты", color="#7fa890")
    assert food["kind"] == "expense"
    assert food["color"] == "#7fa890"
    assert food["archived"] is False

    duplicate = client.post(
        "/api/finance/categories", json={"name": "Еда и продукты", "kind": "expense"}
    )
    assert duplicate.status_code == 409
    # То же имя для другого вида — можно.
    income_same = client.post(
        "/api/finance/categories", json={"name": "Еда и продукты", "kind": "income"}
    )
    assert income_same.status_code == 201
    assert client.post(
        "/api/finance/categories", json={"name": "Еда", "kind": "transfer"}
    ).status_code == 422
    assert client.post("/api/finance/categories", json={"name": "  ", "kind": "expense"}).status_code == 422


def test_category_patch_rules(client: TestClient) -> None:
    food = _category(client, "Еда и продукты")
    taxi = _category(client, "Транспорт")
    home = _category(client, "Дом и быт")

    renamed = client.patch(
        "/api/finance/categories/%d" % taxi["id"],
        json={"name": "Транспорт и такси", "color": "#7e98bc", "sort": 3, "archived": True},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Транспорт и такси"
    assert renamed.json()["archived"] is True
    assert renamed.json()["sort"] == 3

    # Вид свободной категории менять можно.
    kind_changed = client.patch(
        "/api/finance/categories/%d" % taxi["id"], json={"kind": "income"}
    )
    assert kind_changed.status_code == 200
    assert kind_changed.json()["kind"] == "income"

    # Дубликат названия внутри того же вида — 409.
    duplicate = client.patch(
        "/api/finance/categories/%d" % food["id"], json={"name": "Дом и быт"}
    )
    assert duplicate.status_code == 409
    assert home["kind"] == "expense"

    # У категории с транзакциями вид менять нельзя.
    _tx(client, 10, categoryId=food["id"])
    used = client.patch("/api/finance/categories/%d" % food["id"], json={"kind": "income"})
    assert used.status_code == 409

    assert client.patch("/api/finance/categories/9999", json={"name": "X"}).status_code == 404
    assert client.patch("/api/finance/categories/%d" % food["id"], json={}).status_code == 400
    assert (
        client.patch("/api/finance/categories/%d" % food["id"], json={"color": None}).status_code
        == 422
    )

    listed = client.get("/api/finance/categories").json()["items"]
    assert any(item["id"] == food["id"] for item in listed)


# --------------------------------------------------------------------------
# Транзакции: округление и валидация
# --------------------------------------------------------------------------


def test_transaction_rounding(client: TestClient, settings) -> None:
    account = _account(client, "Карта")
    food = _category(client, "Еда и продукты")

    transaction = _tx(
        client, 1250.5, categoryId=food["id"], accountId=account["id"], note="Пятёрочка"
    )
    assert transaction["amount"] == 1250.5
    assert transaction["amountKopecks"] == 125050
    assert transaction["kind"] == "expense"
    assert transaction["date"] == DAY_1
    assert transaction["categoryId"] == food["id"]
    assert transaction["categoryName"] == "Еда и продукты"
    assert transaction["accountName"] == "Карта"
    assert transaction["note"] == "Пятёрочка"
    assert transaction["source"] == "manual"

    conn = connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT amount_kopecks FROM transactions WHERE id = ?", (transaction["id"],)
        ).fetchone()
        assert int(row["amount_kopecks"]) == 125050
    finally:
        conn.close()

    # 10.005 ₽ — half-up до копейки (1001), а не усечение.
    rounded = _tx(client, 10.005)
    assert rounded["amountKopecks"] == 1001
    assert rounded["amount"] == 10.01

    small = _tx(client, 0.01)
    assert small["amountKopecks"] == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"amount": 0},
        {"amount": -10},
        {"amount": 0.001},
        {"amount": 1000000001},
        {"amount": "много"},
        {"amount": 100, "kind": "transfer"},
        {"amount": 100, "date": "31.05.2026"},
        {"amount": 100, "categoryId": 9999},
        {"amount": 100, "accountId": 9999},
        {"amount": 100, "kind": "income", "categoryId": 1},
    ],
)
def test_transaction_validation(client: TestClient, fields: dict) -> None:
    # Категория id=1 — «Еда и продукты» (расход), чтобы проверить несовпадение вида.
    _category(client, "Еда и продукты")
    payload = {"date": DAY_1, "amount": 100, "kind": "expense"}
    payload.update(fields)
    assert client.post("/api/finance/transactions", json=payload).status_code == 422


def test_transaction_date_defaults_to_today(client: TestClient, settings) -> None:
    transaction = client.post(
        "/api/finance/transactions", json={"amount": 100, "kind": "expense"}
    ).json()
    assert transaction["date"] == settings.today()


def test_transaction_patch_and_delete(client: TestClient) -> None:
    food = _category(client, "Еда")
    salary = _category(client, "Зарплата", kind="income")
    transaction = _tx(client, 100, categoryId=food["id"])

    patched = client.patch(
        "/api/finance/transactions/%d" % transaction["id"],
        json={"amount": 99.99, "note": "Пересчитали", "date": DAY_2},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["amountKopecks"] == 9999
    assert patched.json()["amount"] == 99.99
    assert patched.json()["note"] == "Пересчитали"
    assert patched.json()["date"] == DAY_2

    # Смена вида без смены категории — 422.
    mismatch = client.patch(
        "/api/finance/transactions/%d" % transaction["id"], json={"kind": "income"}
    )
    assert mismatch.status_code == 422

    # Смена вида вместе с категорией нужного вида — ок.
    switched = client.patch(
        "/api/finance/transactions/%d" % transaction["id"],
        json={"kind": "income", "categoryId": salary["id"]},
    )
    assert switched.status_code == 200
    assert switched.json()["kind"] == "income"
    assert switched.json()["categoryName"] == "Зарплата"

    # Категорию можно снять.
    cleared = client.patch(
        "/api/finance/transactions/%d" % transaction["id"], json={"categoryId": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["categoryId"] is None
    assert cleared.json()["categoryName"] is None

    assert (
        client.patch("/api/finance/transactions/%d" % transaction["id"], json={}).status_code
        == 400
    )
    assert (
        client.patch(
            "/api/finance/transactions/%d" % transaction["id"], json={"amount": None}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/api/finance/transactions/9999", json={"amount": 10}
        ).status_code
        == 404
    )

    deleted = client.delete("/api/finance/transactions/%d" % transaction["id"])
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "id": transaction["id"]}
    assert client.delete("/api/finance/transactions/%d" % transaction["id"]).status_code == 404


# --------------------------------------------------------------------------
# Транзакции: фильтры и список
# --------------------------------------------------------------------------


def test_transactions_filters(client: TestClient, settings) -> None:
    food = _category(client, "Еда")
    taxi = _category(client, "Такси")
    salary = _category(client, "Зарплата", kind="income")

    first = _tx(client, 300, day=DAY_1, categoryId=food["id"], note="Пятёрочка")
    second = _tx(client, 100, day=DAY_2, categoryId=taxi["id"], note="Такси до дома")
    third = _tx(client, 1000, kind="income", day=DAY_1, categoryId=salary["id"], note="Зарплата")
    _tx(client, 50, day=NEXT_DAY, note="Июньская")

    everything = client.get("/api/finance/transactions", params={"month": MONTH}).json()
    assert everything["total"] == 3
    assert everything["limit"] == 100
    assert everything["month"] == MONTH
    # Свежие сверху: 2 мая, затем две транзакции 1 мая.
    assert [item["id"] for item in everything["items"]][0] == second["id"]
    assert {item["id"] for item in everything["items"]} == {first["id"], second["id"], third["id"]}

    expenses = client.get(
        "/api/finance/transactions", params={"month": MONTH, "kind": "expense"}
    ).json()
    assert {item["id"] for item in expenses["items"]} == {first["id"], second["id"]}
    assert expenses["kind"] == "expense"

    by_category = client.get(
        "/api/finance/transactions", params={"month": MONTH, "categoryId": food["id"]}
    ).json()
    assert [item["id"] for item in by_category["items"]] == [first["id"]]

    by_note = client.get(
        "/api/finance/transactions", params={"month": MONTH, "q": "пятёр"}
    ).json()
    assert [item["id"] for item in by_note["items"]] == [first["id"]]
    uppercase = client.get(
        "/api/finance/transactions", params={"month": MONTH, "q": "ТАКСИ"}
    ).json()
    assert [item["id"] for item in uppercase["items"]] == [second["id"]]
    nothing = client.get(
        "/api/finance/transactions", params={"month": MONTH, "q": "нет такого"}
    ).json()
    assert nothing["items"] == []
    assert nothing["total"] == 0

    limited = client.get(
        "/api/finance/transactions", params={"month": MONTH, "limit": 2}
    ).json()
    assert len(limited["items"]) == 2
    assert limited["total"] == 3

    june = client.get("/api/finance/transactions", params={"month": NEXT_MONTH}).json()
    assert june["total"] == 1

    assert (
        client.get("/api/finance/transactions", params={"month": MONTH, "limit": 0}).status_code
        == 422
    )
    assert (
        client.get("/api/finance/transactions", params={"month": MONTH, "limit": 501}).status_code
        == 422
    )
    assert (
        client.get(
            "/api/finance/transactions", params={"month": MONTH, "kind": "transfer"}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/finance/transactions", params={"month": MONTH, "categoryId": 0}
        ).status_code
        == 422
    )

    # Месяц по умолчанию — текущий.
    today = settings.today()
    _tx(client, 77, day=today, note="Сегодняшняя")
    default = client.get("/api/finance/transactions").json()
    assert default["month"] == today[:7]
    assert default["items"][0]["note"] == "Сегодняшняя"


# --------------------------------------------------------------------------
# Сводка: суммы, доли, дневная динамика
# --------------------------------------------------------------------------


def test_summary_totals_shares_and_daily(client: TestClient) -> None:
    food = _category(client, "Еда и продукты")
    taxi = _category(client, "Транспорт")
    salary = _category(client, "Зарплата", kind="income")

    _tx(client, 1000, kind="income", day=DAY_5, categoryId=salary["id"])
    _tx(client, 300, day=DAY_1, categoryId=food["id"])
    _tx(client, 200, day=DAY_2, categoryId=food["id"])
    _tx(client, 100, day=DAY_2, categoryId=taxi["id"])

    payload = _summary(client)
    assert payload["income"] == 1000.0
    assert payload["expense"] == 600.0
    assert payload["balance"] == 400.0
    assert payload["incomeKopecks"] == 100000
    assert payload["expenseKopecks"] == 60000
    assert payload["balanceKopecks"] == 40000

    rows = {(item["kind"], item["id"]): item for item in payload["byCategory"]}
    food_row = rows[("expense", food["id"])]
    taxi_row = rows[("expense", taxi["id"])]
    salary_row = rows[("income", salary["id"])]
    assert food_row["name"] == "Еда и продукты"
    assert food_row["sum"] == 500.0
    assert food_row["sumKopecks"] == 50000
    assert food_row["share"] == round(500 / 600, 4)
    assert taxi_row["sum"] == 100.0
    assert taxi_row["share"] == round(100 / 600, 4)
    assert salary_row["sum"] == 1000.0
    assert salary_row["share"] == 1.0
    # Сумма долей внутри вида = 1.
    assert round(food_row["share"] + taxi_row["share"], 6) == 1.0
    # Суммы по категориям сходятся с итогами.
    assert sum(row["sumKopecks"] for row in payload["byCategory"] if row["kind"] == "expense") == 60000
    assert sum(row["sumKopecks"] for row in payload["byCategory"] if row["kind"] == "income") == 100000
    # Расходы в byCategory идут раньше доходов.
    kinds = [item["kind"] for item in payload["byCategory"]]
    assert kinds == sorted(kinds, key=lambda kind: 0 if kind == "expense" else 1)

    daily = {item["date"]: item for item in payload["daily"]}
    assert sorted(daily) == [DAY_1, DAY_2, DAY_5]
    assert daily[DAY_1] == {"date": DAY_1, "income": 0.0, "expense": 300.0}
    assert daily[DAY_2] == {"date": DAY_2, "income": 0.0, "expense": 300.0}
    assert daily[DAY_5] == {"date": DAY_5, "income": 1000.0, "expense": 0.0}


def test_summary_uncategorized_expense(client: TestClient) -> None:
    _tx(client, 50)
    payload = _summary(client)
    assert payload["expense"] == 50.0
    assert len(payload["byCategory"]) == 1
    row = payload["byCategory"][0]
    assert row["id"] is None
    assert row["name"] == "Без категории"
    assert row["sum"] == 50.0
    assert row["share"] == 1.0


def test_summary_other_months_do_not_leak(client: TestClient) -> None:
    _tx(client, 100, day=DAY_1)
    _tx(client, 999, day=NEXT_DAY)
    assert _summary(client)["expense"] == 100.0
    assert _summary(client, NEXT_MONTH)["expense"] == 999.0


# --------------------------------------------------------------------------
# Бюджеты
# --------------------------------------------------------------------------


def test_budgets_over_and_upsert(client: TestClient, settings) -> None:
    food = _category(client, "Еда и продукты")
    taxi = _category(client, "Транспорт")
    salary = _category(client, "Зарплата", kind="income")
    _tx(client, 300, day=DAY_1, categoryId=food["id"])
    _tx(client, 100, day=DAY_2, categoryId=taxi["id"])

    food_budget = client.put(
        "/api/finance/budgets",
        json={"categoryId": food["id"], "month": MONTH, "limit": 100},
    )
    assert food_budget.status_code == 200, food_budget.text
    food_row = food_budget.json()
    assert food_row["limit"] == 100.0
    assert food_row["limitKopecks"] == 10000
    assert food_row["spent"] == 300.0
    assert food_row["spentKopecks"] == 30000
    assert food_row["pct"] == 300.0
    assert food_row["over"] is True
    assert food_row["overAmount"] == 200.0

    taxi_row = client.put(
        "/api/finance/budgets",
        json={"categoryId": taxi["id"], "month": MONTH, "limit": 1000},
    ).json()
    assert taxi_row["pct"] == 10.0
    assert taxi_row["over"] is False
    assert taxi_row["overAmount"] == 0.0

    # Upsert, а не вторая строка.
    updated = client.put(
        "/api/finance/budgets",
        json={"categoryId": food["id"], "month": MONTH, "limit": 400},
    ).json()
    assert updated["pct"] == 75.0
    assert updated["over"] is False
    assert _count(settings, "budgets") == 2

    budgets = client.get("/api/finance/budgets", params={"month": MONTH}).json()
    assert budgets["month"] == MONTH
    assert {item["categoryId"] for item in budgets["items"]} == {food["id"], taxi["id"]}
    by_id = {item["categoryId"]: item for item in budgets["items"]}
    assert by_id[food["id"]]["limitKopecks"] == 40000
    assert by_id[taxi["id"]]["limitKopecks"] == 100000

    # Бюджеты видны в сводке месяца.
    summary = _summary(client)
    summary_budgets = {item["categoryId"]: item for item in summary["budgets"]}
    assert summary_budgets[food["id"]]["spent"] == 300.0
    assert summary_budgets[food["id"]]["pct"] == 75.0
    assert summary_budgets[food["id"]]["over"] is False

    # Только категории расходов, только положительный лимит.
    assert (
        client.put(
            "/api/finance/budgets",
            json={"categoryId": salary["id"], "month": MONTH, "limit": 1000},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/finance/budgets",
            json={"categoryId": food["id"], "month": MONTH, "limit": 0},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/finance/budgets",
            json={"categoryId": food["id"], "month": MONTH, "limit": -100},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/finance/budgets",
            json={"categoryId": 9999, "month": MONTH, "limit": 100},
        ).status_code
        == 422
    )
    assert (
        client.put("/api/finance/budgets", json={"categoryId": 0, "month": MONTH, "limit": 100}).status_code
        == 422
    )


def test_budgets_default_month_and_spent_scope(client: TestClient, settings) -> None:
    food = _category(client, "Еда и продукты")
    client.put(
        "/api/finance/budgets",
        json={"categoryId": food["id"], "month": MONTH, "limit": 500},
    )
    # Расход в другом месяце не должен попадать в факт мая.
    _tx(client, 200, day=NEXT_DAY, categoryId=food["id"])

    may = client.get("/api/finance/budgets", params={"month": MONTH}).json()["items"]
    assert len(may) == 1
    assert may[0]["spent"] == 0.0
    assert may[0]["pct"] == 0.0
    assert may[0]["over"] is False

    june = client.get("/api/finance/budgets", params={"month": NEXT_MONTH}).json()
    assert june["items"] == []

    # Месяц по умолчанию — текущий.
    default = client.get("/api/finance/budgets").json()
    assert default["month"] == settings.today()[:7]
    assert default["items"] == []


def test_budget_rounding_of_limit(client: TestClient) -> None:
    food = _category(client, "Еда")
    row = client.put(
        "/api/finance/budgets",
        json={"categoryId": food["id"], "month": MONTH, "limit": 10.005},
    ).json()
    assert row["limitKopecks"] == 1001
    assert row["limit"] == 10.01


# --------------------------------------------------------------------------
# Регистрация маршрутов
# --------------------------------------------------------------------------


def test_finance_routes_registered(client: TestClient) -> None:
    paths = set(client.get("/openapi.json").json()["paths"])
    expected = {
        "/api/finance/summary",
        "/api/finance/transactions",
        "/api/finance/transactions/{transaction_id}",
        "/api/finance/accounts",
        "/api/finance/accounts/{account_id}",
        "/api/finance/categories",
        "/api/finance/categories/{category_id}",
        "/api/finance/budgets",
    }
    missing = expected - paths
    assert not missing, "не зарегистрированы: %s" % sorted(missing)
