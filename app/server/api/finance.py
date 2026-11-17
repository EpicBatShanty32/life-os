"""Финансы: счета, категории, транзакции, бюджеты и сводка месяца.

Контракт v4 (``studio/HANDOFF-OPUS-5.5.md`` §12.4). Деньги в JSON — **рубли**
(``number`` с точностью до копеек), в SQLite — только целые **копейки**
(``amount_kopecks``/``limit_kopecks``/``balance_start_kopecks``). На входе
``amount``/``limit``/``balanceStart`` — рубли, сервер округляет half-up до
копеек (``1250.5`` → ``125050`` копеек); в ответах транзакций и бюджетов
рядом с рублями отдаются копейки (``amountKopecks``, ``limitKopecks``,
``spentKopecks``). В сводке месяца — рубли: ``income``/``expense``/``balance``,
``byCategory[].sum``, ``budgets[].limit``/``spent``, ``daily[].income``/``expense``;
``byCategory[].share`` — доля 0..1 внутри своего вида (доход/расход),
``budgets[].pct`` — проценты 0..N, ``budgets[].over`` — факт превышения лимита.

Месяц по умолчанию — текущий по часовому поясу приложения (Europe/Moscow),
формат ``YYYY-MM``. Пустой месяц — не ошибка: нули и пустые массивы.
Сводка не падает на базе без счетов/категорий/бюджетов.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.server.api.deps import ConnectionDep, SettingsDep
from app.server.settings import Settings

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["finance"])

Kind = Literal["income", "expense"]
AccountKind = Literal["cash", "card", "savings", "broker"]

DEFAULT_COLOR = "#A18BB7"
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CENT = Decimal("1")

_TRANSACTION_SELECT = (
    "SELECT t.*, c.name AS category_name, a.name AS account_name "
    "FROM transactions AS t "
    "LEFT JOIN finance_categories AS c ON c.id = t.category_id "
    "LEFT JOIN finance_accounts AS a ON a.id = t.account_id"
)

_BUDGET_SELECT = (
    "SELECT b.id AS id, b.category_id AS category_id, b.month AS month, "
    " b.limit_kopecks AS limit_kopecks, c.name AS name, c.color AS color, "
    " COALESCE((SELECT SUM(t.amount_kopecks) FROM transactions AS t "
    "   WHERE t.kind = 'expense' AND t.category_id = b.category_id "
    "     AND substr(t.ts, 1, 7) = b.month), 0) AS spent_kopecks "
    "FROM budgets AS b JOIN finance_categories AS c ON c.id = b.category_id"
)


# --------------------------------------------------------------------------
# Деньги, даты, месяц
# --------------------------------------------------------------------------


def _rub(kopecks: int | None) -> float:
    """Копейки → рубли для JSON (float с точностью до копеек)."""
    return round(int(kopecks or 0) / 100, 2)


def _to_kopecks(amount: Decimal, *, field: str = "amount") -> int:
    """Рубли → копейки, округление half-up; ноль и меньше — 422."""
    try:
        kopecks = (Decimal(amount) * 100).quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - Decimal уже провалидирован
        raise HTTPException(status_code=422, detail="%s: ожидается число" % field) from exc
    if kopecks <= 0:
        raise HTTPException(
            status_code=422, detail="%s: сумма должна быть не меньше копейки" % field
        )
    return int(kopecks)


def _signed_kopecks(amount: Decimal) -> int:
    """Рубли → копейки без требования положительности (стартовый баланс счёта)."""
    return int((Decimal(amount) * 100).quantize(_CENT, rounding=ROUND_HALF_UP))


def _resolve_month(value: str | None, settings: Settings) -> str:
    """``YYYY-MM``; пусто — текущий месяц приложения, иначе 422."""
    text = (value or "").strip()
    if not text:
        return settings.today()[:7]
    if not _MONTH_RE.match(text):
        raise HTTPException(status_code=422, detail="Месяц в формате YYYY-MM")
    return text


def _parse_day(value: str | None, settings: Settings) -> str:
    """``YYYY-MM-DD`` (допускается полный ISO); пусто — сегодня; иначе 422."""
    text = (value or "").strip()
    if not text or text.lower() in {"null", "none"}:
        return settings.today()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Дата в формате YYYY-MM-DD"
        ) from exc


def _day_ts(day: str, settings: Settings) -> str:
    """Момент транзакции: для сегодняшней даты — «сейчас», иначе полдень дня.

    Хранится ISO 8601 с часовым поясом приложения, как в остальных ``ts``;
    фильтр месяца идёт по ``substr(ts, 1, 7)``, поэтому время роли не играет.
    """
    if day == settings.today():
        return settings.now().replace(microsecond=0).isoformat()
    noon = datetime.fromisoformat(day + "T12:00:00").replace(tzinfo=settings.tz)
    return noon.replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# Входные модели
# --------------------------------------------------------------------------


def _non_empty(value: str, message: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(message)
    return text


class TransactionCreate(BaseModel):
    """Тело ``POST /api/finance/transactions``: ``amount`` — рубли."""

    model_config = ConfigDict(extra="ignore")

    date: str | None = None
    amount: Decimal = Field(gt=0, le=Decimal("1000000000"))
    kind: Kind
    categoryId: int | None = Field(None, ge=1)  # noqa: N815 - camelCase JSON
    accountId: int | None = Field(None, ge=1)  # noqa: N815
    note: str = Field("", max_length=500)


class TransactionPatch(BaseModel):
    """Тело ``PATCH /api/finance/transactions/{id}``."""

    model_config = ConfigDict(extra="ignore")

    date: str | None = None
    amount: Decimal | None = Field(None, gt=0, le=Decimal("1000000000"))
    kind: Kind | None = None
    categoryId: int | None = Field(None, ge=1)  # noqa: N815
    accountId: int | None = Field(None, ge=1)  # noqa: N815
    note: str | None = Field(None, max_length=500)


class AccountCreate(BaseModel):
    """Тело ``POST /api/finance/accounts``: ``balanceStart`` — рубли."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    kind: AccountKind = "card"
    currency: str = Field("RUB", min_length=1, max_length=8)
    balanceStart: Decimal = Field(Decimal(0), ge=Decimal("-1000000000"), le=Decimal("1000000000"))  # noqa: N815
    sort: int = Field(0, ge=0, le=10000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Название счёта не может быть пустым")


class AccountPatch(BaseModel):
    """Тело ``PATCH /api/finance/accounts/{id}``."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(None, min_length=1, max_length=200)
    kind: AccountKind | None = None
    currency: str | None = Field(None, min_length=1, max_length=8)
    balanceStart: Decimal | None = Field(None, ge=Decimal("-1000000000"), le=Decimal("1000000000"))  # noqa: N815
    archived: bool | None = None
    sort: int | None = Field(None, ge=0, le=10000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Название счёта не может быть пустым")


class CategoryCreate(BaseModel):
    """Тело ``POST /api/finance/categories``."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    kind: Kind
    color: str = Field(DEFAULT_COLOR, max_length=20)
    sort: int = Field(0, ge=0, le=10000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Название категории не может быть пустым")


class CategoryPatch(BaseModel):
    """Тело ``PATCH /api/finance/categories/{id}``."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(None, min_length=1, max_length=200)
    kind: Kind | None = None
    color: str | None = Field(None, max_length=20)
    sort: int | None = Field(None, ge=0, le=10000)
    archived: bool | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Название категории не может быть пустым")


class BudgetPut(BaseModel):
    """Тело ``PUT /api/finance/budgets`` (upsert по ``categoryId`` + ``month``)."""

    model_config = ConfigDict(extra="ignore")

    categoryId: int = Field(ge=1)  # noqa: N815
    month: str | None = None
    limit: Decimal = Field(gt=0, le=Decimal("1000000000"))


# --------------------------------------------------------------------------
# Строки БД → JSON (camelCase)
# --------------------------------------------------------------------------


def _transaction_dict(row: Any) -> dict[str, Any]:
    kopecks = int(row["amount_kopecks"])
    return {
        "id": int(row["id"]),
        "date": str(row["ts"])[:10],
        "ts": str(row["ts"]),
        "amount": _rub(kopecks),
        "amountKopecks": kopecks,
        "kind": str(row["kind"]),
        "categoryId": row["category_id"],
        "categoryName": row["category_name"],
        "accountId": row["account_id"],
        "accountName": row["account_name"],
        "note": str(row["note"] or ""),
        "source": str(row["source"] or "manual"),
        "created": str(row["created"] or ""),
    }


def _account_dict(row: Any) -> dict[str, Any]:
    kopecks = int(row["balance_start_kopecks"] or 0)
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "kind": str(row["kind"]),
        "currency": str(row["currency"] or "RUB"),
        "balanceStart": _rub(kopecks),
        "balanceStartKopecks": kopecks,
        "archived": bool(row["archived"]),
        "sort": int(row["sort"] or 0),
    }


def _category_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "kind": str(row["kind"]),
        "color": str(row["color"] or DEFAULT_COLOR),
        "sort": int(row["sort"] or 0),
        "archived": bool(row["archived"]),
    }


def _budget_dict(row: Any) -> dict[str, Any]:
    limit = int(row["limit_kopecks"] or 0)
    spent = int(row["spent_kopecks"] or 0)
    pct = round(spent / limit * 100, 1) if limit > 0 else None
    over = spent > limit
    return {
        "id": int(row["id"]),
        "categoryId": int(row["category_id"]),
        "name": str(row["name"]),
        "color": str(row["color"] or DEFAULT_COLOR),
        "month": str(row["month"]),
        "limit": _rub(limit),
        "limitKopecks": limit,
        "spent": _rub(spent),
        "spentKopecks": spent,
        "pct": pct,
        "over": over,
        "overAmount": _rub(max(0, spent - limit)),
    }


# --------------------------------------------------------------------------
# Общие проверки ссылок
# --------------------------------------------------------------------------


def _require_category(conn: Any, category_id: int) -> Any:
    row = conn.execute(
        "SELECT * FROM finance_categories WHERE id = ?", (category_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=422, detail="Категория не найдена")
    return row


def _require_account(conn: Any, account_id: int) -> Any:
    row = conn.execute("SELECT * FROM finance_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=422, detail="Счёт не найден")
    return row


def _check_transaction_refs(
    conn: Any, kind: str, category_id: int | None, account_id: int | None
) -> None:
    """Категория должна существовать и совпадать по виду; счёт — существовать."""
    if category_id is not None:
        category = _require_category(conn, category_id)
        if str(category["kind"]) != str(kind):
            raise HTTPException(
                status_code=422,
                detail="Категория относится к другому типу операции (доход/расход)",
            )
    if account_id is not None:
        _require_account(conn, account_id)


def _fetch_transaction(conn: Any, transaction_id: int) -> Any:
    row = conn.execute(_TRANSACTION_SELECT + " WHERE t.id = ?", (transaction_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Транзакция не найдена")
    return row


# --------------------------------------------------------------------------
# Сводка месяца
# --------------------------------------------------------------------------


@router.get("/finance/summary")
def finance_summary(
    conn: ConnectionDep,
    settings: SettingsDep,
    month: str | None = Query(None, max_length=7, description="YYYY-MM; пусто — текущий"),
) -> dict[str, Any]:
    """Доход, расход, баланс, доли категорий, бюджеты и дневная динамика."""
    month = _resolve_month(month, settings)

    totals = {"income": 0, "expense": 0}
    for row in conn.execute(
        "SELECT kind, COALESCE(SUM(amount_kopecks), 0) AS total FROM transactions "
        "WHERE substr(ts, 1, 7) = ? GROUP BY kind",
        (month,),
    ):
        totals[str(row["kind"])] = int(row["total"])
    income = totals["income"]
    expense = totals["expense"]

    by_category: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT t.kind AS kind, "
        " COALESCE(c.id, 0) AS category_id, "
        " COALESCE(c.name, 'Без категории') AS name, "
        " COALESCE(c.color, ?) AS color, "
        " SUM(t.amount_kopecks) AS total "
        "FROM transactions AS t LEFT JOIN finance_categories AS c ON c.id = t.category_id "
        "WHERE substr(t.ts, 1, 7) = ? "
        "GROUP BY t.kind, c.id "
        "ORDER BY CASE t.kind WHEN 'expense' THEN 0 ELSE 1 END, total DESC, name",
        (DEFAULT_COLOR, month),
    ):
        kind = str(row["kind"])
        category_id = int(row["category_id"])
        total = int(row["total"] or 0)
        kind_total = income if kind == "income" else expense
        by_category.append(
            {
                "id": category_id if category_id > 0 else None,
                "name": str(row["name"]),
                "color": str(row["color"]),
                "kind": kind,
                "sum": _rub(total),
                "sumKopecks": total,
                "share": round(total / kind_total, 4) if kind_total > 0 else 0.0,
            }
        )

    budgets = [
        _budget_dict(row)
        for row in conn.execute(_BUDGET_SELECT + " WHERE b.month = ? ORDER BY c.sort, c.name", (month,))
    ]

    daily: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT substr(ts, 1, 10) AS day, kind, SUM(amount_kopecks) AS total "
        "FROM transactions WHERE substr(ts, 1, 7) = ? "
        "GROUP BY day, kind ORDER BY day",
        (month,),
    ):
        day = str(row["day"])
        bucket = daily.setdefault(day, {"date": day, "income": 0.0, "expense": 0.0})
        bucket[str(row["kind"])] = _rub(int(row["total"] or 0))

    return {
        "month": month,
        "income": _rub(income),
        "incomeKopecks": income,
        "expense": _rub(expense),
        "expenseKopecks": expense,
        "balance": _rub(income - expense),
        "balanceKopecks": income - expense,
        "byCategory": by_category,
        "budgets": budgets,
        "daily": [daily[day] for day in sorted(daily)],
    }


# --------------------------------------------------------------------------
# Транзакции
# --------------------------------------------------------------------------


@router.get("/finance/transactions")
def list_transactions(
    conn: ConnectionDep,
    settings: SettingsDep,
    month: str | None = Query(None, max_length=7, description="YYYY-MM; пусто — текущий"),
    kind: Kind | None = Query(None, description="income или expense"),
    categoryId: int | None = Query(None, ge=1),  # noqa: N815
    q: str | None = Query(None, max_length=200, description="Поиск по заметке"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Транзакции месяца: свежие сверху, с фильтрами и поиском по заметке."""
    month = _resolve_month(month, settings)

    sql = _TRANSACTION_SELECT + " WHERE substr(t.ts, 1, 7) = ?"
    params: list[Any] = [month]
    if kind is not None:
        sql += " AND t.kind = ?"
        params.append(kind)
    if categoryId is not None:
        sql += " AND t.category_id = ?"
        params.append(categoryId)
    sql += " ORDER BY t.ts DESC, t.id DESC"
    rows = conn.execute(sql, params).fetchall()

    items = [_transaction_dict(row) for row in rows]
    needle = (q or "").strip().casefold()
    if needle:
        items = [item for item in items if needle in item["note"].casefold()]

    total = len(items)
    return {
        "items": items[:limit],
        "total": total,
        "limit": limit,
        "month": month,
        "kind": kind,
        "categoryId": categoryId,
        "q": needle or None,
    }


@router.post("/finance/transactions", status_code=201)
def create_transaction(
    payload: TransactionCreate, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Добавляет транзакцию; ``amount`` — рубли, в БД пишутся копейки."""
    day = _parse_day(payload.date, settings)
    kopecks = _to_kopecks(payload.amount)
    _check_transaction_refs(conn, payload.kind, payload.categoryId, payload.accountId)
    with conn:
        cursor = conn.execute(
            "INSERT INTO transactions "
            "(ts, amount_kopecks, kind, category_id, account_id, note, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'manual')",
            (
                _day_ts(day, settings),
                kopecks,
                payload.kind,
                payload.categoryId,
                payload.accountId,
                payload.note,
            ),
        )
        transaction_id = int(cursor.lastrowid or 0)
    return _transaction_dict(_fetch_transaction(conn, transaction_id))


@router.patch("/finance/transactions/{transaction_id}")
def patch_transaction(
    transaction_id: int,
    payload: TransactionPatch,
    conn: ConnectionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Правит транзакцию; вид операции и категория проверяются вместе."""
    row = _fetch_transaction(conn, transaction_id)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    for field, value in data.items():
        if value is None and field not in {"categoryId", "accountId"}:
            raise HTTPException(status_code=422, detail="Поле %s не может быть null" % field)

    updates: dict[str, Any] = {}
    if "date" in data:
        updates["ts"] = _day_ts(_parse_day(data["date"], settings), settings)
    if "amount" in data:
        updates["amount_kopecks"] = _to_kopecks(data["amount"])
    kind = str(data["kind"]) if "kind" in data else str(row["kind"])
    category_id = data["categoryId"] if "categoryId" in data else row["category_id"]
    account_id = data["accountId"] if "accountId" in data else row["account_id"]
    if "kind" in data:
        updates["kind"] = kind
    if "categoryId" in data:
        updates["category_id"] = category_id
    if "accountId" in data:
        updates["account_id"] = account_id
    if "note" in data:
        updates["note"] = data["note"]
    _check_transaction_refs(conn, kind, category_id, account_id)

    assignments = ", ".join("%s = ?" % column for column in updates)
    with conn:
        conn.execute(
            "UPDATE transactions SET " + assignments + " WHERE id = ?",
            (*updates.values(), transaction_id),
        )
    return _transaction_dict(_fetch_transaction(conn, transaction_id))


@router.delete("/finance/transactions/{transaction_id}")
def delete_transaction(transaction_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет транзакцию."""
    _fetch_transaction(conn, transaction_id)
    with conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
    return {"ok": True, "id": transaction_id}


# --------------------------------------------------------------------------
# Счета
# --------------------------------------------------------------------------


@router.get("/finance/accounts")
def list_accounts(conn: ConnectionDep) -> dict[str, Any]:
    """Все счета: активные по ``sort``, архивные — в конце."""
    rows = conn.execute(
        "SELECT * FROM finance_accounts ORDER BY archived, sort, id"
    ).fetchall()
    return {"items": [_account_dict(row) for row in rows]}


@router.post("/finance/accounts", status_code=201)
def create_account(payload: AccountCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Добавляет счёт; ``balanceStart`` — рубли (может быть отрицательным)."""
    kopecks = _signed_kopecks(payload.balanceStart)
    with conn:
        cursor = conn.execute(
            "INSERT INTO finance_accounts "
            "(name, kind, currency, balance_start_kopecks, archived, sort) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (payload.name, payload.kind, payload.currency, kopecks, payload.sort),
        )
        account_id = int(cursor.lastrowid or 0)
    row = conn.execute("SELECT * FROM finance_accounts WHERE id = ?", (account_id,)).fetchone()
    return _account_dict(row)


@router.patch("/finance/accounts/{account_id}")
def patch_account(
    account_id: int, payload: AccountPatch, conn: ConnectionDep
) -> dict[str, Any]:
    """Правит счёт: название, вид, валюту, стартовый баланс, архив, порядок."""
    row = conn.execute(
        "SELECT * FROM finance_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    for field, value in data.items():
        if value is None:
            raise HTTPException(status_code=422, detail="Поле %s не может быть null" % field)

    updates: dict[str, Any] = {}
    for field in ("name", "kind", "currency", "sort"):
        if field in data:
            updates[field] = data[field]
    if "balanceStart" in data:
        updates["balance_start_kopecks"] = _signed_kopecks(data["balanceStart"])
    if "archived" in data:
        updates["archived"] = 1 if data["archived"] else 0

    assignments = ", ".join("%s = ?" % column for column in updates)
    with conn:
        conn.execute(
            "UPDATE finance_accounts SET " + assignments + " WHERE id = ?",
            (*updates.values(), account_id),
        )
    return _account_dict(
        conn.execute("SELECT * FROM finance_accounts WHERE id = ?", (account_id,)).fetchone()
    )


# --------------------------------------------------------------------------
# Категории
# --------------------------------------------------------------------------


def _category_taken(conn: Any, name: str, kind: str, *, exclude_id: int | None = None) -> bool:
    row = conn.execute(
        "SELECT id FROM finance_categories WHERE name = ? AND kind = ?", (name, kind)
    ).fetchone()
    return row is not None and (exclude_id is None or int(row["id"]) != exclude_id)


@router.get("/finance/categories")
def list_categories(conn: ConnectionDep) -> dict[str, Any]:
    """Все категории: сначала активные, затем по ``sort`` и названию."""
    rows = conn.execute(
        "SELECT * FROM finance_categories ORDER BY archived, sort, name"
    ).fetchall()
    return {"items": [_category_dict(row) for row in rows]}


@router.post("/finance/categories", status_code=201)
def create_category(payload: CategoryCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Добавляет категорию; пара (название, вид) уникальна — иначе 409."""
    if _category_taken(conn, payload.name, payload.kind):
        raise HTTPException(
            status_code=409, detail="Категория с таким названием и видом уже есть"
        )
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO finance_categories (name, kind, color, sort, archived) "
                "VALUES (?, ?, ?, ?, 0)",
                (payload.name, payload.kind, payload.color, payload.sort),
            )
            category_id = int(cursor.lastrowid or 0)
    except sqlite3.IntegrityError as exc:  # гонка двух одинаковых POST
        raise HTTPException(
            status_code=409, detail="Категория с таким названием и видом уже есть"
        ) from exc
    row = conn.execute(
        "SELECT * FROM finance_categories WHERE id = ?", (category_id,)
    ).fetchone()
    return _category_dict(row)


@router.patch("/finance/categories/{category_id}")
def patch_category(
    category_id: int, payload: CategoryPatch, conn: ConnectionDep
) -> dict[str, Any]:
    """Правит категорию; вид нельзя менять, если к категории привязаны операции."""
    row = conn.execute(
        "SELECT * FROM finance_categories WHERE id = ?", (category_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    for field, value in data.items():
        if value is None:
            raise HTTPException(status_code=422, detail="Поле %s не может быть null" % field)

    new_kind = str(data["kind"]) if "kind" in data else str(row["kind"])
    if new_kind != str(row["kind"]):
        used = conn.execute(
            "SELECT 1 FROM transactions WHERE category_id = ? LIMIT 1", (category_id,)
        ).fetchone()
        if used is not None:
            raise HTTPException(
                status_code=409,
                detail="Нельзя изменить вид категории: к ней привязаны транзакции",
            )
    new_name = str(data["name"]) if "name" in data else str(row["name"])
    if _category_taken(conn, new_name, new_kind, exclude_id=category_id):
        raise HTTPException(
            status_code=409, detail="Категория с таким названием и видом уже есть"
        )

    updates: dict[str, Any] = {}
    for field in ("name", "kind", "color", "sort"):
        if field in data:
            updates[field] = data[field]
    if "archived" in data:
        updates["archived"] = 1 if data["archived"] else 0

    assignments = ", ".join("%s = ?" % column for column in updates)
    try:
        with conn:
            conn.execute(
                "UPDATE finance_categories SET " + assignments + " WHERE id = ?",
                (*updates.values(), category_id),
            )
    except sqlite3.IntegrityError as exc:  # гонка двух одинаковых PATCH
        raise HTTPException(
            status_code=409, detail="Категория с таким названием и видом уже есть"
        ) from exc
    return _category_dict(
        conn.execute("SELECT * FROM finance_categories WHERE id = ?", (category_id,)).fetchone()
    )


# --------------------------------------------------------------------------
# Бюджеты
# --------------------------------------------------------------------------


@router.get("/finance/budgets")
def list_budgets(
    conn: ConnectionDep,
    settings: SettingsDep,
    month: str | None = Query(None, max_length=7, description="YYYY-MM; пусто — текущий"),
) -> dict[str, Any]:
    """Бюджеты месяца с фактом, процентом и признаком превышения."""
    month = _resolve_month(month, settings)
    rows = conn.execute(
        _BUDGET_SELECT + " WHERE b.month = ? ORDER BY c.sort, c.name", (month,)
    ).fetchall()
    return {"month": month, "items": [_budget_dict(row) for row in rows]}


@router.put("/finance/budgets")
def put_budget(payload: BudgetPut, conn: ConnectionDep, settings: SettingsDep) -> dict[str, Any]:
    """Ставит лимит на категорию месяца (upsert по ``categoryId`` + ``month``)."""
    month = _resolve_month(payload.month, settings)
    category = _require_category(conn, payload.categoryId)
    if str(category["kind"]) != "expense":
        raise HTTPException(
            status_code=422, detail="Бюджет задаётся только для категории расходов"
        )
    limit = _to_kopecks(payload.limit, field="limit")
    with conn:
        conn.execute(
            "INSERT INTO budgets (category_id, month, limit_kopecks) VALUES (?, ?, ?) "
            "ON CONFLICT(category_id, month) DO UPDATE SET limit_kopecks = excluded.limit_kopecks",
            (payload.categoryId, month, limit),
        )
    row = conn.execute(
        _BUDGET_SELECT + " WHERE b.category_id = ? AND b.month = ?",
        (payload.categoryId, month),
    ).fetchone()
    return _budget_dict(row)
