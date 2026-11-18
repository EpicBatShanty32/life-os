"""Питание: дневник КБЖУ, справочник продуктов и нормы.

Эндпоинты (камелКейс-JSON, ошибки — ``{"detail": "..."}`` с кодом 4xx):

* ``GET  /api/nutrition/day?date=YYYY-MM-DD`` — четыре приёма пищи с записями,
  итоги дня, нормы, остаток и вода;
* ``POST /api/nutrition/entries`` — запись дневника: из продукта (макросы
  считаются как ``продукт × граммы / 100``) или свободным текстом (тогда
  калории, белки, жиры и углеводы нужно передать явно);
* ``PATCH/DELETE /api/nutrition/entries/{id}`` — правка граммовки/приёма пищи
  (макросы пересчитываются) и удаление записи;
* ``GET  /api/nutrition/products?q=&limit=`` — регистронезависимый поиск
  по названию и бренду (кириллица — через Python ``casefold``);
* ``POST /api/nutrition/products``, ``PATCH /api/nutrition/products/{id}`` —
  справочник продуктов (КБЖУ на 100 г, ``baseGrams``, архив);
* ``GET  /api/nutrition/summary?days=30`` — дневные суммы, средние и покрытие
  норм (0..1);
* ``GET/PUT /api/nutrition/targets`` — нормы КБЖУ и воды (единственная строка).

Решения, которые видно снаружи:

* **Дата записи** — ``date`` (``YYYY-MM-DD``), время — ``ts`` (ISO-8601 с
  часовым поясом). Записи без продукта (свободный текст) при ``PATCH grams``
  масштабируются пропорционально: макросы на грамм сохраняются.
* **Продукт удаляется только в архив** (``archived = 1``): старые записи
  дневника сохраняют ссылку и КБЖУ. Поиск по умолчанию архивные не показывает.
* **Нормы** всегда существуют: миграция 4 вставляет строку с дефолтами
  (2300/150/75/260/30/50 ккал-белки-жиры-углеводы-клетчатка-сахар и 2200 мл
  воды), а ``GET`` возвращает дефолты даже на базе, где строку удалили.
* **Вода** берётся из метрики дня (``metrics.kind = 'water'``, мл); если
  метрики нет — из старых записей ``entries.kind = 'water'`` (``meta.ml``).
* **Покрытие норм** в ``summary`` — отношение среднего к норме, обрезанное
  сверху единицей (перебор не маскирует недобор по другому макросу).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.server.api.deps import ConnectionDep, SettingsDep
from app.server.db import DEFAULT_NUTRITION_TARGETS
from app.server.settings import Settings

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["nutrition"])

Meal = Literal["breakfast", "lunch", "dinner", "snack"]

#: Приёмы пищи в порядке вывода дневника.
MEALS: tuple[str, ...] = ("breakfast", "lunch", "dinner", "snack")

#: Подписи приёмов пищи для карточки дня.
MEAL_LABELS: dict[str, str] = {
    "breakfast": "Завтрак",
    "lunch": "Обед",
    "dinner": "Ужин",
    "snack": "Перекус",
}

#: Макросы, которые считаются и суммируются одинаково.
MACROS: tuple[str, ...] = ("kcal", "protein", "fat", "carbs", "fiber", "sugar")

_MAX_LIMIT = 100


# --------------------------------------------------------------------------
# Помощники: JSON, даты, суммы
# --------------------------------------------------------------------------


def _round(value: Any, digits: int = 1) -> float:
    try:
        return round(float(value or 0), digits)
    except (TypeError, ValueError):
        return 0.0


def _entry_json(row: Any) -> dict[str, Any]:
    """Запись дневника в camelCase."""
    return {
        "id": int(row["id"]),
        "date": str(row["date"]),
        "ts": str(row["ts"]),
        "meal": str(row["meal"]),
        "productId": None if row["product_id"] is None else int(row["product_id"]),
        "text": str(row["text"] or ""),
        "grams": _round(row["grams"]),
        "kcal": _round(row["kcal"]),
        "protein": _round(row["protein"]),
        "fat": _round(row["fat"]),
        "carbs": _round(row["carbs"]),
        "fiber": _round(row["fiber"]),
        "sugar": _round(row["sugar"]),
    }


def _product_json(row: Any) -> dict[str, Any]:
    """Продукт справочника: КБЖУ на 100 г."""
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "brand": str(row["brand"] or ""),
        "kcal": _round(row["kcal"]),
        "protein": _round(row["protein"]),
        "fat": _round(row["fat"]),
        "carbs": _round(row["carbs"]),
        "fiber": _round(row["fiber"]),
        "sugar": _round(row["sugar"]),
        "baseGrams": _round(row["base_grams"]),
        "archived": bool(row["archived"]),
    }


def _totals_json(rows: list[Any]) -> dict[str, float]:
    """Сумма макросов по списку записей."""
    return {macro: _round(sum(float(row[macro] or 0) for row in rows)) for macro in MACROS}


def _targets_json(row: Any | None) -> dict[str, Any]:
    """Нормы дня; без строки в БД — дефолты миграции 4."""
    if row is None:
        return {
            "kcal": DEFAULT_NUTRITION_TARGETS["kcal"],
            "protein": DEFAULT_NUTRITION_TARGETS["protein"],
            "fat": DEFAULT_NUTRITION_TARGETS["fat"],
            "carbs": DEFAULT_NUTRITION_TARGETS["carbs"],
            "fiber": DEFAULT_NUTRITION_TARGETS["fiber"],
            "sugar": DEFAULT_NUTRITION_TARGETS["sugar"],
            "waterMl": DEFAULT_NUTRITION_TARGETS["water_ml"],
            "updated": None,
        }
    return {
        "kcal": _round(row["kcal"]),
        "protein": _round(row["protein"]),
        "fat": _round(row["fat"]),
        "carbs": _round(row["carbs"]),
        "fiber": _round(row["fiber"]),
        "sugar": _round(row["sugar"]),
        "waterMl": _round(row["water_ml"]),
        "updated": None if row["updated"] is None else str(row["updated"]),
    }


def _parse_day(value: str | None, settings: Settings) -> date:
    """``YYYY-MM-DD``; пусто — сегодня; иначе 422."""
    text = (value or "").strip()
    if not text:
        return settings.now().date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Дата в формате YYYY-MM-DD") from exc


def _load_targets_row(conn: Any) -> Any:
    return conn.execute("SELECT * FROM nutrition_targets WHERE id = 1").fetchone()


def _water_ml(conn: Any, day: str) -> float:
    """Вода за день: метрика ``water`` (мл), иначе старые записи ``entries``."""
    row = conn.execute(
        "SELECT COALESCE(SUM(value), 0) FROM metrics "
        "WHERE date = ? AND kind = 'water' AND unit IN ('мл', 'ml')",
        (day,),
    ).fetchone()
    millilitres = float(row[0] or 0) if row else 0.0
    if millilitres > 0:
        return _round(millilitres)
    legacy = conn.execute(
        "SELECT COALESCE(SUM(CAST(json_extract(meta_json, '$.ml') AS REAL)), 0) "
        "FROM entries WHERE date = ? AND kind = 'water'",
        (day,),
    ).fetchone()
    return _round(float(legacy[0] or 0) if legacy else 0.0)


def _entry_row(conn: Any, entry_id: int) -> Any:
    row = conn.execute("SELECT * FROM food_log WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Запись дневника не найдена")
    return row


def _product_row(conn: Any, product_id: int) -> Any:
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Продукт не найден")
    return row


def _macros_from_product(product: Any, grams: float) -> dict[str, float]:
    factor = grams / 100.0
    return {macro: _round(float(product[macro] or 0) * factor) for macro in MACROS}


def _scaled_macros(stored: Any, old_grams: float, new_grams: float) -> dict[str, float]:
    """Пропорциональный пересчёт свободной записи (без продукта)."""
    if old_grams <= 0:
        return {macro: _round(stored[macro]) for macro in MACROS}
    factor = new_grams / old_grams
    return {macro: _round(float(stored[macro] or 0) * factor) for macro in MACROS}


def _now_ts(settings: Settings) -> str:
    """Метка времени записи в ISO-8601 с часовым поясом настроек."""
    return settings.now().isoformat(timespec="seconds")


def _non_empty(value: str, message: str = "Текст не может быть пустым") -> str:
    text = value.strip()
    if not text:
        raise ValueError(message)
    return text


# --------------------------------------------------------------------------
# Входные модели
# --------------------------------------------------------------------------


class EntryCreate(BaseModel):
    """Тело ``POST /api/nutrition/entries``.

    Либо ``productId`` (макросы считаются из продукта), либо свободный текст
    с обязательными ``kcal``/``protein``/``fat``/``carbs``.
    """

    model_config = ConfigDict(extra="ignore")

    date: str | None = None
    meal: Meal
    productId: int | None = Field(None, ge=1)  # noqa: N815 - camelCase JSON
    text: str = Field("", max_length=400)
    grams: float = Field(..., gt=0, le=5000)
    kcal: float | None = Field(None, ge=0, le=20000)
    protein: float | None = Field(None, ge=0, le=2000)
    fat: float | None = Field(None, ge=0, le=2000)
    carbs: float | None = Field(None, ge=0, le=2000)
    fiber: float | None = Field(None, ge=0, le=2000)
    sugar: float | None = Field(None, ge=0, le=2000)


class EntryPatch(BaseModel):
    """Тело ``PATCH /api/nutrition/entries/{id}``: граммовка и/или приём пищи."""

    model_config = ConfigDict(extra="ignore")

    grams: float | None = Field(None, gt=0, le=5000)
    meal: Meal | None = None


class ProductCreate(BaseModel):
    """Тело ``POST /api/nutrition/products`` (КБЖУ на 100 г)."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    brand: str = Field("", max_length=120)
    kcal: float = Field(..., ge=0, le=20000)
    protein: float = Field(0.0, ge=0, le=2000)
    fat: float = Field(0.0, ge=0, le=2000)
    carbs: float = Field(0.0, ge=0, le=2000)
    fiber: float = Field(0.0, ge=0, le=2000)
    sugar: float = Field(0.0, ge=0, le=2000)
    baseGrams: float = Field(100.0, gt=0, le=5000)  # noqa: N815

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        return _non_empty(value, "Название продукта не может быть пустым")

    @field_validator("brand")
    @classmethod
    def _brand_strip(cls, value: str) -> str:
        return value.strip()


class ProductPatch(BaseModel):
    """Тело ``PATCH /api/nutrition/products/{id}``: любое подмножество полей."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(None, min_length=1, max_length=200)
    brand: str | None = Field(None, max_length=120)
    kcal: float | None = Field(None, ge=0, le=20000)
    protein: float | None = Field(None, ge=0, le=2000)
    fat: float | None = Field(None, ge=0, le=2000)
    carbs: float | None = Field(None, ge=0, le=2000)
    fiber: float | None = Field(None, ge=0, le=2000)
    sugar: float | None = Field(None, ge=0, le=2000)
    baseGrams: float | None = Field(None, gt=0, le=5000)  # noqa: N815
    archived: bool | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "Название продукта не может быть пустым")


class TargetsUpdate(BaseModel):
    """Тело ``PUT /api/nutrition/targets``: любое подмножество норм."""

    model_config = ConfigDict(extra="ignore")

    kcal: float | None = Field(None, gt=0, le=20000)
    protein: float | None = Field(None, ge=0, le=2000)
    fat: float | None = Field(None, ge=0, le=2000)
    carbs: float | None = Field(None, ge=0, le=2000)
    fiber: float | None = Field(None, ge=0, le=2000)
    sugar: float | None = Field(None, ge=0, le=2000)
    waterMl: float | None = Field(None, ge=0, le=20000)  # noqa: N815


# --------------------------------------------------------------------------
# День
# --------------------------------------------------------------------------


@router.get("/nutrition/day")
def nutrition_day(
    conn: ConnectionDep,
    settings: SettingsDep,
    date_value: str | None = Query(None, alias="date", max_length=10),
) -> dict[str, Any]:
    """Дневник за день: приёмы пищи, итоги, нормы, остаток и вода."""
    day = _parse_day(date_value, settings)
    day_iso = day.isoformat()

    rows = conn.execute(
        "SELECT * FROM food_log WHERE date = ? ORDER BY ts, id", (day_iso,)
    ).fetchall()
    by_meal: dict[str, list[Any]] = {meal: [] for meal in MEALS}
    for row in rows:
        by_meal.setdefault(str(row["meal"]), []).append(row)

    meals: list[dict[str, Any]] = []
    for meal in MEALS:
        entries = by_meal.get(meal, [])
        meals.append(
            {
                "meal": meal,
                "label": MEAL_LABELS[meal],
                "entries": [_entry_json(row) for row in entries],
                "totals": _totals_json(entries),
            }
        )

    totals = _totals_json(rows)
    targets = _targets_json(_load_targets_row(conn))
    remaining = {macro: _round(float(targets[macro]) - totals[macro]) for macro in MACROS}
    water_ml = _water_ml(conn, day_iso)
    return {
        "date": day_iso,
        "meals": meals,
        "totals": totals,
        "targets": targets,
        "remaining": remaining,
        "water": {"ml": water_ml, "target": targets["waterMl"]},
    }


# --------------------------------------------------------------------------
# Записи дневника
# --------------------------------------------------------------------------


@router.post("/nutrition/entries", status_code=201)
def create_entry(
    payload: EntryCreate, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Добавляет запись дневника: из продукта или свободным текстом."""
    day = _parse_day(payload.date, settings)
    grams = _round(payload.grams)

    if payload.productId is not None:
        product = _product_row(conn, payload.productId)
        macros = _macros_from_product(product, payload.grams)
        text = payload.text.strip() or str(product["name"])
    else:
        text = payload.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="Нужен текст записи или productId")
        missing = [
            name
            for name, value in (
                ("kcal", payload.kcal),
                ("protein", payload.protein),
                ("fat", payload.fat),
                ("carbs", payload.carbs),
            )
            if value is None
        ]
        if missing:
            raise HTTPException(
                status_code=422,
                detail="Для записи без продукта нужны kcal, protein, fat и carbs",
            )
        macros = {
            "kcal": _round(payload.kcal),
            "protein": _round(payload.protein),
            "fat": _round(payload.fat),
            "carbs": _round(payload.carbs),
            "fiber": _round(payload.fiber),
            "sugar": _round(payload.sugar),
        }

    with conn:
        cursor = conn.execute(
            "INSERT INTO food_log (date, ts, meal, product_id, text, grams, kcal, "
            "protein, fat, carbs, fiber, sugar, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')",
            (
                day.isoformat(),
                _entry_ts(payload.date, settings),
                payload.meal,
                payload.productId,
                text,
                grams,
                macros["kcal"],
                macros["protein"],
                macros["fat"],
                macros["carbs"],
                macros["fiber"],
                macros["sugar"],
            ),
        )
        entry_id = int(cursor.lastrowid or 0)
    return _entry_json(_entry_row(conn, entry_id))


def _entry_ts(day_value: str, settings: Settings) -> str:
    """Метка времени для записи с явной датой: сегодня — «сейчас», иначе полдень."""
    day = _parse_day(day_value, settings)
    now = settings.now()
    if day == now.date():
        return now.isoformat(timespec="seconds")
    return "%sT12:00:00%s" % (day.isoformat(), _tz_suffix(now))


def _tz_suffix(moment: Any) -> str:
    offset = moment.utcoffset()
    if offset is None:
        return ""
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "%s%02d:%02d" % (sign, total // 3600, (total % 3600) // 60)


@router.patch("/nutrition/entries/{entry_id}")
def patch_entry(entry_id: int, payload: EntryPatch, conn: ConnectionDep) -> dict[str, Any]:
    """Меняет граммовку и/или приём пищи, пересчитывая макросы."""
    row = _entry_row(conn, entry_id)
    updates: list[str] = []
    params: list[Any] = []

    new_grams = None if payload.grams is None else _round(payload.grams)
    if new_grams is not None and new_grams != _round(row["grams"]):
        if row["product_id"] is not None:
            product = conn.execute(
                "SELECT * FROM products WHERE id = ?", (row["product_id"],)
            ).fetchone()
            if product is None:
                macros = _scaled_macros(row, float(row["grams"] or 0), new_grams)
            else:
                macros = _macros_from_product(product, new_grams)
        else:
            macros = _scaled_macros(row, float(row["grams"] or 0), new_grams)
        updates.append("grams = ?")
        params.append(new_grams)
        for macro in MACROS:
            updates.append("%s = ?" % macro)
            params.append(macros[macro])

    if payload.meal is not None:
        updates.append("meal = ?")
        params.append(payload.meal)

    if updates:
        params.append(entry_id)
        with conn:
            conn.execute("UPDATE food_log SET %s WHERE id = ?" % ", ".join(updates), params)
    return _entry_json(_entry_row(conn, entry_id))


@router.delete("/nutrition/entries/{entry_id}")
def delete_entry(entry_id: int, conn: ConnectionDep) -> dict[str, Any]:
    """Удаляет запись дневника."""
    _entry_row(conn, entry_id)
    with conn:
        conn.execute("DELETE FROM food_log WHERE id = ?", (entry_id,))
    return {"ok": True, "id": entry_id}


# --------------------------------------------------------------------------
# Справочник продуктов
# --------------------------------------------------------------------------


@router.get("/nutrition/products")
def list_products(
    conn: ConnectionDep,
    q: str | None = Query(None, max_length=200),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT),
) -> dict[str, Any]:
    """Поиск продуктов по названию и бренду (архивные не показываются).

    Регистр сравнивается в Python: встроенный SQLite ``lower()`` не понимает
    кириллицу, поэтому «рис» находил бы «Рис басмати» через костыль, а не
    через ``LIKE``. Справочник локальный (сотни строк), фильтрация дешёвая.
    """
    rows = conn.execute(
        "SELECT * FROM products WHERE archived = 0 ORDER BY name COLLATE NOCASE, id"
    ).fetchall()
    text = (q or "").strip().casefold()
    if text:
        rows = [
            row
            for row in rows
            if text in ("%s %s" % (row["name"], row["brand"])).casefold()
        ]
    items = rows[:limit]
    return {"items": [_product_json(row) for row in items], "total": len(items)}


@router.post("/nutrition/products", status_code=201)
def create_product(payload: ProductCreate, conn: ConnectionDep) -> dict[str, Any]:
    """Добавляет продукт в справочник."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO products (name, brand, kcal, protein, fat, carbs, fiber, "
            "sugar, base_grams, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                payload.name.strip(),
                payload.brand.strip(),
                payload.kcal,
                payload.protein,
                payload.fat,
                payload.carbs,
                payload.fiber,
                payload.sugar,
                payload.baseGrams,
            ),
        )
        product_id = int(cursor.lastrowid or 0)
    return _product_json(_product_row(conn, product_id))


@router.patch("/nutrition/products/{product_id}")
def patch_product(
    product_id: int, payload: ProductPatch, conn: ConnectionDep
) -> dict[str, Any]:
    """Правка продукта; ``archived`` убирает его из поиска, не ломая записи."""
    _product_row(conn, product_id)
    fields: list[str] = []
    params: list[Any] = []
    mapping = {
        "name": ("name", lambda value: value.strip()),
        "brand": ("brand", lambda value: value.strip()),
        "kcal": ("kcal", float),
        "protein": ("protein", float),
        "fat": ("fat", float),
        "carbs": ("carbs", float),
        "fiber": ("fiber", float),
        "sugar": ("sugar", float),
        "baseGrams": ("base_grams", float),
        "archived": ("archived", lambda value: 1 if value else 0),
    }
    for field, (column, convert) in mapping.items():
        value = getattr(payload, field, None)
        if value is None:
            continue
        fields.append("%s = ?" % column)
        params.append(convert(value))
    if fields:
        params.append(product_id)
        with conn:
            conn.execute("UPDATE products SET %s WHERE id = ?" % ", ".join(fields), params)
    return _product_json(_product_row(conn, product_id))


# --------------------------------------------------------------------------
# Сводка и нормы
# --------------------------------------------------------------------------


@router.get("/nutrition/summary")
def nutrition_summary(
    conn: ConnectionDep,
    settings: SettingsDep,
    days: int = Query(30, ge=1, le=365),
) -> dict[str, Any]:
    """Дневные суммы КБЖУ, средние за дни с записями и покрытие норм 0..1."""
    last_day = settings.now().date()
    first_day = last_day - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT date, SUM(kcal) AS kcal, SUM(protein) AS protein, SUM(fat) AS fat, "
        "SUM(carbs) AS carbs, SUM(fiber) AS fiber, SUM(sugar) AS sugar "
        "FROM food_log WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date",
        (first_day.isoformat(), last_day.isoformat()),
    ).fetchall()
    daily = [
        {"date": str(row["date"]), **{macro: _round(row[macro]) for macro in MACROS}}
        for row in rows
    ]
    targets = _targets_json(_load_targets_row(conn))
    if daily:
        average = {
            macro: _round(sum(item[macro] for item in daily) / len(daily)) for macro in MACROS
        }
    else:
        average = {macro: 0.0 for macro in MACROS}
    coverage = {}
    for macro in MACROS:
        limit = float(targets[macro]) or 0.0
        coverage[macro] = (
            0.0 if limit <= 0 else round(min(1.0, average[macro] / limit), 3)
        )
    return {
        "days": days,
        "startDate": first_day.isoformat(),
        "endDate": last_day.isoformat(),
        "daysWithEntries": len(daily),
        "daily": daily,
        "average": average,
        "targets": targets,
        "coverage": coverage,
    }


@router.get("/nutrition/targets")
def get_targets(conn: ConnectionDep) -> dict[str, Any]:
    """Нормы КБЖУ и воды."""
    return _targets_json(_load_targets_row(conn))


@router.put("/nutrition/targets")
def put_targets(payload: TargetsUpdate, conn: ConnectionDep) -> dict[str, Any]:
    """Меняет нормы; не переданные поля остаются прежними."""
    row = _load_targets_row(conn)
    current = _targets_json(row)
    values = {
        "kcal": current["kcal"] if payload.kcal is None else payload.kcal,
        "protein": current["protein"] if payload.protein is None else payload.protein,
        "fat": current["fat"] if payload.fat is None else payload.fat,
        "carbs": current["carbs"] if payload.carbs is None else payload.carbs,
        "fiber": current["fiber"] if payload.fiber is None else payload.fiber,
        "sugar": current["sugar"] if payload.sugar is None else payload.sugar,
        "water_ml": current["waterMl"] if payload.waterMl is None else payload.waterMl,
    }
    with conn:
        conn.execute(
            "INSERT INTO nutrition_targets (id, kcal, protein, fat, carbs, fiber, sugar, "
            "water_ml, updated) VALUES (1, ?, ?, ?, ?, ?, ?, ?, "
            "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "ON CONFLICT(id) DO UPDATE SET kcal = excluded.kcal, "
            "protein = excluded.protein, fat = excluded.fat, carbs = excluded.carbs, "
            "fiber = excluded.fiber, sugar = excluded.sugar, "
            "water_ml = excluded.water_ml, updated = excluded.updated",
            (
                values["kcal"],
                values["protein"],
                values["fat"],
                values["carbs"],
                values["fiber"],
                values["sugar"],
                values["water_ml"],
            ),
        )
    return _targets_json(_load_targets_row(conn))
