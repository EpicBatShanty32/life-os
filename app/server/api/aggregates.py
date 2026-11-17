"""Агрегаты для API: ряды метрик, EMA, корреляции, предпочтения пользователя.

Модуль не знает про HTTP: функции принимают ``sqlite3.Connection`` и даты,
поэтому их можно проверять юнит-тестами и переиспользовать в overview,
``/api/health/weight`` и ``/api/analytics/correlations``.

Данные в БД:

* ``metrics`` — измерения (``weight``, ``sleep``, ``mood``, ``water``...);
  за день может быть несколько строк (вода), поэтому ряды агрегируются
  средней (вес, сон, настроение) или суммой (вода);
* ``entries`` — журнал; тренировки и учёба выводятся из ``kind`` и
  ``meta_json`` (минуты, если они есть в записи).
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from app.server.db import get_setting, set_setting
from app.server.settings import Settings

__all__ = [
    "COMPOSITION_KINDS",
    "DEFAULT_PREFERENCES",
    "DELTA_WINDOWS",
    "MIN_CORRELATION_DAYS",
    "MIN_CORRELATION_R",
    "TRAINING_KINDS",
    "WATER_KINDS",
    "WEEKDAYS",
    "build_correlations",
    "composition_deltas",
    "ema_series",
    "linear_trend",
    "weight_forecast",
    "entry_daily_counts",
    "entry_daily_minutes",
    "latest_metric",
    "metric_daily_avg",
    "metric_daily_sum",
    "metric_unit",
    "parse_day",
    "pearson",
    "pref_float",
    "pref_int",
    "read_preferences",
    "shift_day",
    "training_days",
    "weekday_label",
    "write_preferences",
]

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

#: Какие ``entries.kind`` считаем тренировкой (сидер и бот могут писать по-разному).
TRAINING_KINDS = ("training", "workout", "workouts", "sport", "тренировка")

#: Какие ``metrics.kind`` складываем в воду за день.
WATER_KINDS = ("water", "water_ml")

#: Порог корреляции из MASTER_SPEC §2.5: меньше 14 дней — «мало данных».
MIN_CORRELATION_DAYS = 14
MIN_CORRELATION_R = 0.3

#: Предпочтения пользователя в ``settings_kv`` (ключи с префиксом ``pref.``).
DEFAULT_PREFERENCES: dict[str, Any] = {
    "theme": "system",
    "water_goal_ml": 2000,
    "weight_goal_kg": 78.0,
    "sleep_goal_h": 8.0,
    "workouts_per_week": 3,
}

_PREF_PREFIX = "pref."

# Сколько минут активности даёт запись, если в meta_json нет длительности.
_DEFAULT_MINUTES = {"training": 45.0, "study": 30.0, "learning": 30.0}
_DEFAULT_MINUTES_OTHER = 10.0


def parse_day(value: str | date | None) -> date | None:
    """Разбирает ``YYYY-MM-DD``; кривое значение — ``None``."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def shift_day(value: str, delta: int) -> str:
    """Сдвигает дату ``YYYY-MM-DD`` на ``delta`` дней."""
    day = parse_day(value)
    if day is None:
        raise ValueError("Ожидалась дата YYYY-MM-DD, получено %r" % (value,))
    return (day + timedelta(days=delta)).isoformat()


def weekday_label(value: str) -> str:
    """Короткое русское название дня недели для точки графика."""
    day = parse_day(value)
    return WEEKDAYS[day.weekday()] if day is not None else ""


def metric_daily_avg(
    conn,
    kind: str,
    *,
    since: str | None = None,
) -> dict[str, float]:
    """Среднее значение метрики по дням (за день может быть несколько строк)."""
    sql = "SELECT date, value FROM metrics WHERE kind = ?"
    params: list[Any] = [kind]
    if since:
        sql += " AND date >= ?"
        params.append(since)
    sql += " ORDER BY date, id"
    buckets: dict[str, list[float]] = {}
    for row in conn.execute(sql, params):
        buckets.setdefault(str(row["date"]), []).append(float(row["value"]))
    return {day: sum(values) / len(values) for day, values in buckets.items() if values}


def metric_daily_sum(
    conn,
    kinds: Sequence[str],
    *,
    since: str | None = None,
) -> dict[str, float]:
    """Сумма метрик по дням (вода: несколько стаканов за день)."""
    allowed = tuple(kinds)
    if not allowed:
        return {}
    placeholders = ", ".join("?" for _ in allowed)
    sql = "SELECT date, SUM(value) AS total FROM metrics WHERE kind IN (%s)" % placeholders
    params: list[Any] = list(allowed)
    if since:
        sql += " AND date >= ?"
        params.append(since)
    sql += " GROUP BY date ORDER BY date"
    return {str(row["date"]): float(row["total"] or 0.0) for row in conn.execute(sql, params)}


def latest_metric(conn, kind: str) -> tuple[float, str] | None:
    """Последнее значение метрики и её единица измерения."""
    row = conn.execute(
        "SELECT value, unit FROM metrics WHERE kind = ? ORDER BY date DESC, id DESC LIMIT 1",
        (kind,),
    ).fetchone()
    if row is None:
        return None
    return float(row["value"]), str(row["unit"] or "")


def metric_unit(conn, kind: str) -> str:
    row = conn.execute(
        "SELECT unit FROM metrics WHERE kind = ? ORDER BY date DESC, id DESC LIMIT 1",
        (kind,),
    ).fetchone()
    return str(row["unit"] or "") if row is not None else ""


def sleep_hours(value: float, unit: str) -> float:
    """Приводит сон к часам: ``min``/``мин`` делим на 60."""
    if unit.strip().lower() in {"min", "mins", "minute", "minutes", "мин", "минуты"}:
        return float(value) / 60.0
    return float(value)


def entry_daily_counts(conn, *, since: str | None = None) -> dict[str, int]:
    """Сколько записей журнала было в каждый день."""
    sql = "SELECT date, COUNT(*) AS total FROM entries"
    params: list[Any] = []
    if since:
        sql += " WHERE date >= ?"
        params.append(since)
    sql += " GROUP BY date ORDER BY date"
    return {str(row["date"]): int(row["total"]) for row in conn.execute(sql, params)}


def entry_daily_minutes(conn, *, since: str | None = None) -> dict[str, float]:
    """Минуты активности по дням.

    Если в ``meta_json`` записи есть ``minutes``/``duration_min`` — берём их,
    иначе оценка по типу записи (тренировка 45 минут, учёба 30, прочее 10).
    Это честная эвристика для графика активности, а не выдуманные данные:
    основа — реальные записи журнала.
    """
    sql = "SELECT date, kind, meta_json FROM entries"
    params: list[Any] = []
    if since:
        sql += " WHERE date >= ?"
        params.append(since)
    totals: dict[str, float] = {}
    for row in conn.execute(sql, params):
        day = str(row["date"])
        kind = str(row["kind"] or "")
        minutes = _minutes_from_meta(row["meta_json"], kind)
        totals[day] = totals.get(day, 0.0) + minutes
    return totals


def training_days(conn, *, since: str | None = None) -> set[str]:
    """Даты, в которые была тренировка."""
    placeholders = ", ".join("?" for _ in TRAINING_KINDS)
    sql = "SELECT DISTINCT date FROM entries WHERE kind IN (%s)" % placeholders
    params: list[Any] = list(TRAINING_KINDS)
    if since:
        sql += " AND date >= ?"
        params.append(since)
    return {str(row["date"]) for row in conn.execute(sql, params)}


def ema_series(values: Sequence[float], window: int = 7) -> list[float]:
    """Экспоненциальное сглаживание: alpha = 2 / (window + 1)."""
    if not values:
        return []
    span = max(1, int(window))
    alpha = 2.0 / (span + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


#: Показатели состава тела для дельт за 7/30/90 дней (MASTER_SPEC §2.5).
COMPOSITION_KINDS: tuple[tuple[str, str], ...] = (
    ("fat", "Жир"),
    ("muscle", "Мышцы"),
    ("body_water", "Вода в теле"),
)

#: Окна дельт состава тела, дней.
DELTA_WINDOWS: tuple[int, ...] = (7, 30, 90)


def linear_trend(points: Sequence[tuple[str, float]]) -> tuple[float, float] | None:
    """Прямая по МНК через точки ``(дата, значение)``.

    Возвращает ``(наклон_за_день, свободный_член)`` или ``None``, если точек
    меньше трёх или все даты совпадают. Наклон отрицательный — показатель
    падает (вес снижается), положительный — растёт.
    """
    if len(points) < 3:
        return None
    xs = [date.fromisoformat(str(day)).toordinal() for day, _ in points]
    ys = [float(value) for _, value in points]
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        return None
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = numerator / denominator
    return slope, mean_y - slope * mean_x


def weight_forecast(
    points: Sequence[tuple[str, float]],
    target_kg: float | None,
    today: date,
    *,
    window_days: int = 30,
    min_points: int = 5,
) -> dict[str, Any]:
    """Прогноз даты цели по тренду последних ``window_days`` дней.

    ``points`` — дневные средние веса ``(YYYY-MM-DD, кг)``. Никаких выдуманных
    дат: если тренда нет (``flat``), данных мало (``insufficient_data``) или
    движение идёт от цели (``away_from_target``), ``date`` остаётся ``None`` —
    UI покажет честный статус.
    """
    horizon = today.toordinal() - max(1, int(window_days))
    recent = [
        (str(day), float(value))
        for day, value in points
        if date.fromisoformat(str(day)).toordinal() >= horizon
    ][-60:]
    result: dict[str, Any] = {
        "status": "insufficient_data",
        "target_kg": None if target_kg is None else round(float(target_kg), 2),
        "rate_per_week": None,
        "date": None,
        "weeks": None,
        "current": round(recent[-1][1], 2) if recent else None,
    }
    if len(recent) < min_points:
        return result

    trend = linear_trend(recent)
    if trend is None:
        return result

    slope, _ = trend
    rate = round(slope * 7.0, 2)
    result["rate_per_week"] = rate
    if abs(rate) < 0.05:
        result["status"] = "flat"
        return result
    if target_kg is None:
        result["status"] = "no_target"
        return result

    current = recent[-1][1]
    remaining = float(target_kg) - current
    if abs(remaining) < 0.05:
        result.update(status="reached", date=today.isoformat(), weeks=0.0)
        return result
    if remaining * slope <= 0:
        result["status"] = "away_from_target"
        return result

    days_needed = remaining / slope
    due = today + timedelta(days=int(round(days_needed)))
    result.update(status="ok", date=due.isoformat(), weeks=round(days_needed / 7.0, 1))
    return result


def composition_deltas(
    conn,
    today: date,
    *,
    windows: Sequence[int] = DELTA_WINDOWS,
) -> list[dict[str, Any]]:
    """Дельты жира/мышц/воды за 7/30/90 дней по дневным средним.

    Для каждого окна берётся первое и последнее измерение внутри него; если
    точек меньше двух, дельта ``None`` — интерфейс пишет «мало данных», а не
    рисует ноль.
    """
    windows = tuple(int(window) for window in windows if int(window) >= 2)
    if not windows:
        return []
    lookback = max(90, max(windows))
    since = (today - timedelta(days=lookback - 1)).isoformat()
    items: list[dict[str, Any]] = []
    for kind, label in COMPOSITION_KINDS:
        daily = metric_daily_avg(conn, kind, since=since)
        days = sorted(daily)
        current = round(daily[days[-1]], 2) if days else None
        deltas: dict[str, float | None] = {}
        counts: dict[str, int] = {}
        for window in windows:
            start = today - timedelta(days=window - 1)
            subset = [daily[day] for day in days if date.fromisoformat(day) >= start]
            counts[str(window)] = len(subset)
            deltas[str(window)] = (
                round(subset[-1] - subset[0], 2) if len(subset) >= 2 else None
            )
        items.append(
            {
                "key": kind,
                "label": label,
                "unit": metric_unit(conn, kind),
                "current": current,
                "deltas": deltas,
                "counts": counts,
            }
        )
    return items


def pearson(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Коэффициент Пирсона; ``None``, если точек мало или нет разброса."""
    points = [(float(left), float(right)) for left, right in pairs]
    count = len(points)
    if count < 3:
        return None
    mean_left = sum(left for left, _ in points) / count
    mean_right = sum(right for _, right in points) / count
    covariance = sum((left - mean_left) * (right - mean_right) for left, right in points)
    variance_left = sum((left - mean_left) ** 2 for left, _ in points)
    variance_right = sum((right - mean_right) ** 2 for _, right in points)
    if variance_left <= 0 or variance_right <= 0:
        return None
    return covariance / math.sqrt(variance_left * variance_right)


_CORRELATION_PAIRS: tuple[tuple[str, str, str, str, str], ...] = (
    ("sleep", "mood", "Сон", "Настроение", "Когда сон длится дольше, настроение выше"),
    ("water", "mood", "Вода", "Настроение", "В дни с достаточной водой настроение выше"),
    ("training", "weight", "Тренировки", "Вес", "Тренировки связаны с изменением веса"),
)


def build_correlations(conn, today: date, days: int = 90) -> list[dict[str, Any]]:
    """Считает корреляции из MASTER_SPEC §2.5 и честно помечает нехватку данных."""
    since = (today - timedelta(days=max(1, int(days)) - 1)).isoformat()
    series = {
        "sleep": _sleep_daily(conn, since),
        "mood": metric_daily_avg(conn, "mood", since=since),
        "water": metric_daily_sum(conn, WATER_KINDS, since=since),
        "weight": metric_daily_avg(conn, "weight", since=since),
        "training": {day: 1.0 for day in training_days(conn, since=since)},
    }
    results: list[dict[str, Any]] = []
    for left, right, left_label, right_label, sentence in _CORRELATION_PAIRS:
        left_series = series[left]
        right_series = series[right]
        if left == "training":
            # Отсутствие тренировки — это ноль, а не «нет данных»: иначе
            # двоичный ряд всегда постоянный и корреляция физически невозможна.
            trainings = set(left_series)
            left_series = {
                day: 1.0 if day in trainings else 0.0 for day in right_series
            }
        common = sorted(set(left_series) & set(right_series))
        pairs = [(left_series[day], right_series[day]) for day in common]
        count = len(pairs)
        coefficient = pearson(pairs)
        if count < MIN_CORRELATION_DAYS:
            status = "insufficient_data"
            text = "Мало данных: %d из %d дней" % (count, MIN_CORRELATION_DAYS)
        elif coefficient is None:
            status = "no_variance"
            text = "Нет разброса значений — корреляцию не посчитать"
        elif abs(coefficient) < MIN_CORRELATION_R:
            status = "weak"
            text = "Слабая связь: r = %.2f" % coefficient
        else:
            status = "ok"
            text = "%s: r = %.2f, n = %d" % (sentence, coefficient, count)
        results.append(
            {
                "left": left,
                "right": right,
                "label": "%s ↔ %s" % (left_label, right_label),
                "left_label": left_label,
                "right_label": right_label,
                "n": count,
                "r": round(coefficient, 2) if coefficient is not None else None,
                "status": status,
                "text": text,
                "sentence": sentence,
                "enough_data": status == "ok",
            }
        )
    return results


def _sleep_daily(conn, since: str) -> dict[str, float]:
    sql = (
        "SELECT date, value, unit FROM metrics WHERE kind = 'sleep' AND date >= ? "
        "ORDER BY date, id"
    )
    buckets: dict[str, list[float]] = {}
    for row in conn.execute(sql, (since,)):
        buckets.setdefault(str(row["date"]), []).append(
            sleep_hours(float(row["value"]), str(row["unit"] or ""))
        )
    return {day: sum(values) / len(values) for day, values in buckets.items() if values}


def _minutes_from_meta(raw: Any, kind: str) -> float:
    meta = _load_json(raw)
    for key in ("minutes", "duration_min", "duration_minutes"):
        value = meta.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return _DEFAULT_MINUTES.get(kind.strip().lower(), _DEFAULT_MINUTES_OTHER)


def _load_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------
# Предпочтения пользователя (settings_kv)
# --------------------------------------------------------------------------


def read_preferences(conn) -> dict[str, Any]:
    """Читает предпочтения из ``settings_kv`` с валидацией и дефолтами."""
    preferences: dict[str, Any] = {}
    for key, default in DEFAULT_PREFERENCES.items():
        raw = get_setting(conn, _PREF_PREFIX + key)
        preferences[key] = _coerce_preference(key, raw, default)
    return preferences


def write_preferences(conn, values: Mapping[str, Any]) -> dict[str, Any]:
    """Пишет известные предпочтения (ключи вне ``DEFAULT_PREFERENCES`` игнорируются)."""
    for key, value in values.items():
        if key not in DEFAULT_PREFERENCES or value is None:
            continue
        validated = _coerce_preference(key, value, DEFAULT_PREFERENCES[key])
        if validated != DEFAULT_PREFERENCES[key] or value == DEFAULT_PREFERENCES[key]:
            set_setting(conn, _PREF_PREFIX + key, json.dumps(validated, ensure_ascii=False))
    return read_preferences(conn)


def pref_int(conn, key: str) -> int:
    value = read_preferences(conn).get(key, DEFAULT_PREFERENCES.get(key, 0))
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(DEFAULT_PREFERENCES.get(key, 0))


def pref_float(conn, key: str) -> float:
    value = read_preferences(conn).get(key, DEFAULT_PREFERENCES.get(key, 0.0))
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_PREFERENCES.get(key, 0.0))


def _coerce_preference(key: str, raw: Any, default: Any) -> Any:
    """Приводит значение из settings_kv к типу дефолта; мусор → дефолт."""
    if raw is None:
        return default
    value = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = raw
    if isinstance(default, bool):
        return bool(value) if isinstance(value, bool) else default
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        if isinstance(value, bool):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return value if isinstance(value, str) and value.strip() else default
    return value if value is not None else default
