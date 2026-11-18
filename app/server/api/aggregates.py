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
    "FDR_ALPHA",
    "MAX_FINDINGS",
    "MIN_CORRELATION_DAYS",
    "MIN_CORRELATION_R",
    "SERIES_LABELS",
    "SERIES_ORDER",
    "STRENGTH_STRONG",
    "STRENGTH_WEAK",
    "TRAINING_KINDS",
    "WATER_KINDS",
    "WEEKDAYS",
    "bh_adjust",
    "build_correlation_report",
    "build_correlations",
    "build_daily_series",
    "fisher_interval",
    "finding_title",
    "legacy_pairs_from_report",
    "spearman",
    "strength_label",
    "student_p_value",
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


#: Пары из MASTER_SPEC §2.5, которые ждёт старый экран ``/api/analytics/correlations``.
#: Своего расчёта у них больше нет: цифры и формулировки берутся из отчёта v2.
_CORRELATION_PAIRS: tuple[tuple[str, str], ...] = (
    ("sleep", "mood"),
    ("water", "mood"),
    ("training", "weight"),
)

# --------------------------------------------------------------------------
# Корреляции v2: лаги, ранги, FDR и человеческие находки
# --------------------------------------------------------------------------

#: Подписи дневных рядов для анализа связей.
SERIES_LABELS: dict[str, str] = {
    "sleep": "Сон",
    "energy": "Энергия",
    "mood": "Настроение",
    "water": "Вода",
    "weight": "Вес",
    "training": "Тренировки",
    "steps": "Шаги",
    "reading": "Чтение",
    "expenses": "Расходы",
    "income": "Доходы",
    "tasks": "Закрытые задачи",
    "habits": "Привычки",
}

#: Порядок рядов при переборе пар (все сочетания без повторов).
SERIES_ORDER: tuple[str, ...] = tuple(SERIES_LABELS)

#: Какие метрики считаем шагами и страницами чтения.
STEP_KINDS: tuple[str, ...] = ("steps", "steps_day", "шаги")
PAGES_KINDS: tuple[str, ...] = ("pages", "pages_read", "reading_pages")

#: Benjamini–Hochberg: допускаем не больше 10 % ложных находок в выдаче.
FDR_ALPHA = 0.1
#: Сколько находок показываем наверху.
MAX_FINDINGS = 5
#: Порог «силы» связи: слабая < 0.3, умеренная < 0.5, сильная ≥ 0.5.
STRENGTH_WEAK = 0.3
STRENGTH_STRONG = 0.5
#: Двусторонний z-квантиль для 95 % интервала Фишера.
Z_95 = 1.959963984540054


def _ranks(values: Sequence[float]) -> list[float]:
    """Ранги с усреднением совпадающих значений (для Спирмена)."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def spearman(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Спирмен: Пирсон на рангах; ``None`` при малом числе точек/без разброса."""
    points = [(float(left), float(right)) for left, right in pairs]
    if len(points) < 3:
        return None
    return pearson(
        list(zip(_ranks([left for left, _ in points]), _ranks([right for _, right in points])))
    )


def _betacf(a: float, b: float, x: float) -> float:
    """Непрерывная дробь для неполной бета-функции (Numerical Recipes)."""
    max_iterations = 200
    epsilon = 3e-12
    tiny = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for iteration in range(1, max_iterations + 1):
        even = 2 * iteration
        numerator = iteration * (b - iteration) * x / ((qam + even) * (a + even))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        numerator = -(a + iteration) * (qab + iteration) * x / (
            (a + even) * (qap + even)
        )
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return h


def _regularized_beta(a: float, b: float, x: float) -> float:
    """Регуляризованная неполная бета I_x(a, b) для p-value Стьюдента."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(log_beta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_p_value(r: float | None, n: int) -> float | None:
    """Двусторонний p-value для r по t = r·sqrt((n−2)/(1−r²)), df = n−2.

    Для t-распределения двусторонний p равен I_x(df/2, 1/2), где
    x = df/(df + t²); неполная бета считается непрерывной дробью, без scipy.
    """
    if r is None or n < 3:
        return None
    if abs(r) >= 1.0:
        return 0.0
    t = abs(float(r)) * math.sqrt((n - 2.0) / (1.0 - float(r) * float(r)))
    df = float(n - 2)
    return _regularized_beta(df / 2.0, 0.5, df / (df + t * t))


def fisher_interval(r: float | None, n: int) -> list[float] | None:
    """95 % доверительный интервал r через z-преобразование Фишера."""
    if r is None or n <= 3 or abs(float(r)) >= 1.0:
        return None
    value = float(r)
    z = 0.5 * math.log((1.0 + value) / (1.0 - value))
    error = 1.0 / math.sqrt(n - 3.0)
    return [
        round(math.tanh(z - Z_95 * error), 3),
        round(math.tanh(z + Z_95 * error), 3),
    ]


def bh_adjust(p_values: Sequence[float]) -> list[float]:
    """Benjamini–Hochberg: q-значения (FDR) для набора p-value."""
    count = len(p_values)
    if count == 0:
        return []
    order = sorted(range(count), key=lambda index: p_values[index])
    adjusted = [0.0] * count
    running = 1.0
    for rank in range(count, 0, -1):
        index = order[rank - 1]
        value = float(p_values[index]) * count / rank
        running = min(running, value)
        adjusted[index] = min(1.0, running)
    return adjusted


def strength_label(r: float | None) -> str | None:
    """Сила связи по |r|: weak < 0.3, moderate < 0.5, strong ≥ 0.5."""
    if r is None:
        return None
    value = abs(float(r))
    if value < STRENGTH_WEAK:
        return "weak"
    if value < STRENGTH_STRONG:
        return "moderate"
    return "strong"


def _shift_series(series: dict[str, float], lag: int) -> dict[str, float]:
    """Сдвиг правого ряда: значению дня ``d + lag`` соответствует день ``d``."""
    if lag <= 0:
        return dict(series)
    shifted: dict[str, float] = {}
    for day, value in series.items():
        try:
            base = shift_day(day, -lag)
        except ValueError:  # pragma: no cover - даты уже нормализованы
            continue
        shifted[base] = value
    return shifted


def build_daily_series(conn, today: date, days: int = 90) -> dict[str, dict[str, float]]:
    """Дневные ряды для анализа связей; только реальные дни.

    Тренировки и привычки — плотные ряды: отсутствие отметки означает 0, иначе
    двоичный ряд всегда был бы «без разброса» и связь физически не считалась.
    Остальные ряды — дни, когда показатель действительно измерен/случился.
    """
    days = max(1, int(days))
    start = today - timedelta(days=days - 1)
    since = start.isoformat()
    until = today.isoformat()
    window = [(start + timedelta(days=offset)).isoformat() for offset in range(days)]

    series: dict[str, dict[str, float]] = {}
    series["sleep"] = _sleep_daily(conn, since)
    series["mood"] = metric_daily_avg(conn, "mood", since=since)
    series["water"] = metric_daily_sum(conn, WATER_KINDS, since=since)
    series["weight"] = metric_daily_avg(conn, "weight", since=since)
    series["steps"] = metric_daily_sum(conn, STEP_KINDS, since=since)

    energy: dict[str, float] = {}
    for row in conn.execute(
        "SELECT date, energy FROM checkins WHERE date BETWEEN ? AND ? ORDER BY date",
        (since, until),
    ):
        energy[str(row["date"])] = float(row["energy"])
    series["energy"] = energy

    trainings = training_days(conn, since=since)
    # Ноль — это «не тренировался», но только с того дня, когда тренировки
    # вообще начали отмечать: иначе длинные нули до старта данных дают ложную
    # связь с другими плотными рядами (привычки).
    placeholders = ", ".join("?" for _ in TRAINING_KINDS)
    first_training = conn.execute(
        "SELECT MIN(substr(ts, 1, 10)) AS first FROM entries WHERE kind IN (%s)"
        % placeholders,
        tuple(TRAINING_KINDS),
    ).fetchone()[0]
    if first_training:
        training_start = max(since, str(first_training)[:10])
        series["training"] = {
            day: 1.0 if day in trainings else 0.0
            for day in window
            if day >= training_start
        }
    else:
        series["training"] = {}

    reading = metric_daily_sum(conn, PAGES_KINDS, since=since)
    if not reading:
        # Журнала чтения в схеме нет: если нет метрики страниц, честно берём
        # страницы книг, законченных в окне (по дате завершения).
        for row in conn.execute(
            "SELECT finished_at, pages_read FROM books "
            "WHERE shelf = 'done' AND finished_at IS NOT NULL "
            "AND finished_at BETWEEN ? AND ?",
            (since, until),
        ):
            day = str(row["finished_at"])[:10]
            reading[day] = reading.get(day, 0.0) + float(row["pages_read"] or 0)
    series["reading"] = reading

    for key, kind in (("expenses", "expense"), ("income", "income")):
        daily: dict[str, float] = {}
        for row in conn.execute(
            "SELECT substr(ts, 1, 10) AS day, SUM(amount_kopecks) AS total "
            "FROM transactions WHERE kind = ? AND substr(ts, 1, 10) BETWEEN ? AND ? "
            "GROUP BY day",
            (kind, since, until),
        ):
            daily[str(row["day"])] = float(row["total"] or 0) / 100.0
        series[key] = daily

    done_tasks: dict[str, float] = {}
    for row in conn.execute(
        "SELECT substr(done_at, 1, 10) AS day, COUNT(*) AS total FROM tasks "
        "WHERE status = 'done' AND done_at IS NOT NULL "
        "AND substr(done_at, 1, 10) BETWEEN ? AND ? GROUP BY day",
        (since, until),
    ):
        done_tasks[str(row["day"])] = float(row["total"])
    series["tasks"] = done_tasks

    active_habits = int(
        conn.execute("SELECT COUNT(*) FROM habits WHERE archived = 0").fetchone()[0] or 0
    )
    first_mark = conn.execute(
        "SELECT MIN(date) AS first FROM habit_marks WHERE value > 0"
    ).fetchone()[0]
    if active_habits > 0 and first_mark:
        habits_start = max(since, str(first_mark)[:10])
        marks: dict[str, int] = {}
        for row in conn.execute(
            "SELECT date, COUNT(*) AS total FROM habit_marks "
            "WHERE value > 0 AND date BETWEEN ? AND ? GROUP BY date",
            (since, until),
        ):
            marks[str(row["date"])] = int(row["total"])
        series["habits"] = {
            day: marks.get(day, 0) / float(active_habits)
            for day in window
            if day >= habits_start
        }
    else:
        series["habits"] = {}
    return series


def _lower_first(label: str) -> str:
    """Строчная первая буква для середины фразы; аббревиатуры не трогаем."""
    if not label:
        return label
    if label.isupper() or (len(label) > 1 and label[1].isupper()):
        return label
    return label[0].lower() + label[1:]


def _days_before(lag: int) -> str:
    """«вчера» / «2 дня назад» / «5 дней назад» — сторона опережающего ряда."""
    if lag <= 1:
        return "вчера"
    tail = "дня" if lag in (2, 3, 4) else "дней"
    return "%d %s назад" % (lag, tail)


def _days_after(lag: int) -> str:
    """«на следующий день» / «через 2 дня» / «через 5 дней»."""
    if lag <= 1:
        return "на следующий день"
    tail = "дня" if lag in (2, 3, 4) else "дней"
    return "через %d %s" % (lag, tail)


def finding_title(
    left_label: str, right_label: str, lag: int, r: float | None = None
) -> str:
    """Короткая нейтральная формулировка пары — для карточек Обзора и находок.

    Стрелки-причины не используем вовсе: при ``lag=0`` пишем «идут вместе»
    (при ``r < 0`` — «движутся в разные стороны»), при ``lag > 0`` называем
    порядок во времени: «Сон вчера, энергия сегодня». Так фраза не врёт про
    направление, даже если связь работает в обратную сторону.
    """
    right = _lower_first(right_label)
    if lag <= 0:
        if r is not None and float(r) < 0:
            return "%s и %s движутся в разные стороны" % (left_label, right)
        return "%s и %s идут вместе" % (left_label, right)
    return "%s %s, %s сегодня" % (left_label, _days_before(int(lag)), right)


def _finding_text(left_label: str, right_label: str, lag: int, r: float) -> str:
    """Человеческая формулировка «что это значит» (без канцелярита и эмодзи)."""
    higher = "выше" if r > 0 else "ниже"
    if lag == 0:
        return (
            "В дни, когда «%s» выше обычного, «%s» %s. "
            "Связь синхронная: это ещё не причина." % (left_label, right_label, higher)
        )
    return (
        "Когда «%s» выше обычного, %s «%s» %s. "
        "Это порядок во времени, а не причина."
        % (left_label, _days_after(int(lag)), right_label, higher)
    )


def _finding_advice(left_label: str, right_label: str, lag: int) -> str:
    if lag == 0:
        return (
            "Выберите одну привычку, которая может влиять сразу на оба показателя, "
            "и подержите её 2–3 недели."
        )
    return (
        "Попробуйте 2–3 недели держать «%s» в норме и смотреть, как меняется «%s»: "
        "если сдвиг повторится, связь рабочая." % (left_label, right_label)
    )


def _decorate_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Добавляет округления, статус, силу и человеческий текст к результату."""
    r = candidate["r"]
    n = int(candidate["n"])
    q = candidate["q"]
    if r is None:
        status = "insufficient_data" if n < MIN_CORRELATION_DAYS else "no_variance"
        text = (
            "Мало реальных дней: %d из %d" % (n, MIN_CORRELATION_DAYS)
            if status == "insufficient_data"
            else "Значения не меняются — корреляцию не посчитать"
        )
        advice = (
            "Продолжайте записывать оба показателя: нужно ещё %d дн."
            % (MIN_CORRELATION_DAYS - n)
            if status == "insufficient_data"
            else "Показатель всё время одинаков — сравнивать нечего."
        )
    else:
        status = "ok" if (q is not None and q <= FDR_ALPHA) else "weak"
        text = _finding_text(
            candidate["leftLabel"], candidate["rightLabel"], int(candidate["lag"]), float(r)
        )
        advice = _finding_advice(
            candidate["leftLabel"], candidate["rightLabel"], int(candidate["lag"])
        )
    candidate["status"] = status
    candidate["effectPct"] = round(float(r) ** 2 * 100, 1) if r is not None else None
    candidate["strength"] = strength_label(r)
    candidate["text"] = text
    candidate["advice"] = advice
    candidate["sentence"] = (
        "Чем выше «%s», тем %s «%s»"
        % (candidate["leftLabel"], "выше" if float(r) > 0 else "ниже", candidate["rightLabel"])
        if r is not None
        else ""
    )
    candidate["title"] = (
        finding_title(
            candidate["leftLabel"], candidate["rightLabel"], int(candidate["lag"]), float(r)
        )
        if r is not None
        else ""
    )
    # Какой ряд идёт первым: при lag=0 порядок не определён, при lag>0 левый
    # ряд относится ко дню d, правый — ко дню d+lag, то есть водит левый.
    candidate["leads"] = "left" if int(candidate["lag"]) > 0 else None
    candidate["follows"] = "right" if int(candidate["lag"]) > 0 else None
    candidate["enough_data"] = status == "ok"
    candidate["r"] = round(float(r), 3) if r is not None else None
    candidate["rho"] = round(float(candidate["rho"]), 3) if candidate["rho"] is not None else None
    # p и q оставляем сырыми: при сильной связи они порядка 1e-19, и
    # округление до шести знаков превращало бы их в «0.0».
    candidate["p"] = float(candidate["p"]) if candidate["p"] is not None else None
    candidate["q"] = float(q) if q is not None else None
    return candidate


def build_correlation_report(
    conn, today: date, days: int = 90, lags: int = 2
) -> dict[str, Any]:
    """Полный анализ связей: пары × лаги, p, FDR, находки и честные отказы.

    Отбираются только реальные дни; тест проходит при ``n ≥ 14`` и наличии
    разброса. p-value — двусторонний Стьюдент по t = r·sqrt((n−2)/(1−r²)),
    FDR — Benjamini–Hochberg при ``q ≤ 0.1`` по всем протестированным
    парам×лагам; находки ранжируются по |r|, показываются первые 5.
    """
    days = max(1, int(days))
    lags = max(0, int(lags))
    start = today - timedelta(days=days - 1)
    series = build_daily_series(conn, today, days)

    candidates: list[dict[str, Any]] = []
    for index, left in enumerate(SERIES_ORDER):
        for right in SERIES_ORDER[index + 1 :]:
            left_series = series[left]
            right_series = series[right]
            for lag in range(lags + 1):
                shifted = _shift_series(right_series, lag)
                common = sorted(set(left_series) & set(shifted))
                count = len(common)
                points = [(left_series[day], shifted[day]) for day in common]
                coefficient = (
                    pearson(points) if count >= MIN_CORRELATION_DAYS else None
                )
                candidates.append(
                    {
                        "left": left,
                        "right": right,
                        "leftLabel": SERIES_LABELS[left],
                        "rightLabel": SERIES_LABELS[right],
                        "label": "%s ↔ %s" % (SERIES_LABELS[left], SERIES_LABELS[right]),
                        "left_label": SERIES_LABELS[left],
                        "right_label": SERIES_LABELS[right],
                        "lag": lag,
                        "n": count,
                        "r": coefficient,
                        "rho": spearman(points) if coefficient is not None else None,
                        "p": student_p_value(coefficient, count),
                        "q": None,
                        "ci": fisher_interval(coefficient, count),
                    }
                )

    tested = [item for item in candidates if item["r"] is not None]
    for item, q_value in zip(tested, bh_adjust([float(item["p"]) for item in tested])):
        item["q"] = q_value
    for item in candidates:
        _decorate_candidate(item)

    passed = [item for item in tested if item["q"] is not None and item["q"] <= FDR_ALPHA]
    passed.sort(key=lambda item: (-abs(float(item["r"])), float(item["p"]), int(item["lag"])))
    found = [
        {
            "left": item["left"],
            "right": item["right"],
            "leftLabel": item["leftLabel"],
            "rightLabel": item["rightLabel"],
            "lag": int(item["lag"]),
            "r": item["r"],
            "rho": item["rho"],
            "n": int(item["n"]),
            "p": item["p"],
            "q": item["q"],
            "effectPct": item["effectPct"],
            "strength": item["strength"],
            "title": item["title"],
            "leads": item["leads"],
            "text": item["text"],
            "advice": item["advice"],
            "ci": item["ci"],
        }
        for item in passed[:MAX_FINDINGS]
    ]

    passed_pairs = {(item["left"], item["right"]) for item in passed}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in candidates:
        grouped.setdefault((item["left"], item["right"]), []).append(item)
    not_ready: list[dict[str, Any]] = []
    for (left, right), group in grouped.items():
        if (left, right) in passed_pairs:
            continue
        computed = [item for item in group if item["r"] is not None]
        if computed:
            reason = "fdr"
            days_have = max(int(item["n"]) for item in computed)
        elif any(int(item["n"]) >= MIN_CORRELATION_DAYS for item in group):
            reason = "no_variance"
            days_have = max(int(item["n"]) for item in group)
        else:
            reason = "insufficient_data"
            days_have = max(int(item["n"]) for item in group)
        not_ready.append(
            {
                "left": left,
                "right": right,
                "reason": reason,
                "daysHave": days_have,
                "needDays": MIN_CORRELATION_DAYS,
            }
        )
    not_ready.sort(key=lambda item: (-item["daysHave"], item["left"], item["right"]))

    ordered_pairs = sorted(
        candidates,
        key=lambda item: (item["r"] is None, -abs(float(item["r"] or 0.0)), int(item["lag"])),
    )
    return {
        "window": {"from": start.isoformat(), "to": today.isoformat(), "days": days},
        "lags": lags,
        "tested": len(tested),
        "found": found,
        "notReady": not_ready,
        "pairs": [_report_pair(item) for item in ordered_pairs],
    }


def _report_pair(item: dict[str, Any]) -> dict[str, Any]:
    """Пара для списков: старые поля + поля находок (camelCase и snake_case)."""
    return {
        "left": item["left"],
        "right": item["right"],
        "label": item["label"],
        "leftLabel": item["leftLabel"],
        "rightLabel": item["rightLabel"],
        "left_label": item["left_label"],
        "right_label": item["right_label"],
        "lag": int(item["lag"]),
        "n": int(item["n"]),
        "r": item["r"],
        "rho": item["rho"],
        "p": item["p"],
        "q": item["q"],
        "ci": item["ci"],
        "effectPct": item["effectPct"],
        "strength": item["strength"],
        "status": item["status"],
        "title": item["title"],
        "leads": item["leads"],
        "text": item["text"],
        "sentence": item["sentence"],
        "advice": item["advice"],
        "enough_data": bool(item["enough_data"]),
        "enoughData": bool(item["enough_data"]),
    }


def legacy_pairs_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Старые три пары как срез отчёта v2 (lag=0), без своего порога.

    Цифры, статус, q и формулировки берутся из :func:`build_correlation_report`,
    поэтому ``/api/analytics/correlations`` не может разойтись с Аналитикой и
    Обзором. Ориентация пары сохраняется прежней (``training`` → ``weight``),
    даже если в отчёте она записана наоборот: корреляция lag=0 симметрична.
    """
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for item in report.get("pairs", []):
        if int(item.get("lag", 0)) != 0:
            continue
        by_pair[(str(item["left"]), str(item["right"]))] = item

    results: list[dict[str, Any]] = []
    for left, right in _CORRELATION_PAIRS:
        item = by_pair.get((left, right)) or by_pair.get((right, left))
        if item is None:  # pragma: no cover - все пары есть в SERIES_ORDER
            continue
        left_label = SERIES_LABELS[left]
        right_label = SERIES_LABELS[right]
        coefficient = item["r"]
        count = int(item["n"])
        status = str(item["status"])
        results.append(
            {
                "left": left,
                "right": right,
                "label": "%s ↔ %s" % (left_label, right_label),
                "left_label": left_label,
                "right_label": right_label,
                "n": count,
                "r": round(float(coefficient), 2) if coefficient is not None else None,
                "status": status,
                "text": (
                    _finding_text(left_label, right_label, 0, float(coefficient))
                    if coefficient is not None
                    else item["text"]
                ),
                "sentence": (
                    "Чем выше «%s», тем %s «%s»"
                    % (left_label, "выше" if float(coefficient) > 0 else "ниже", right_label)
                    if coefficient is not None
                    else ""
                ),
                "enough_data": status == "ok",
                "lag": 0,
                "rho": item["rho"],
                "p": item["p"],
                "q": item["q"],
                "ci": item["ci"],
                "effectPct": item["effectPct"],
                "strength": item["strength"],
                "title": (
                    finding_title(left_label, right_label, 0, float(coefficient))
                    if coefficient is not None
                    else ""
                ),
                "leads": None,
                "advice": _finding_advice(left_label, right_label, 0)
                if coefficient is not None
                else (
                    "Продолжайте записывать оба показателя: нужно ещё %d дн."
                    % (MIN_CORRELATION_DAYS - count)
                    if count < MIN_CORRELATION_DAYS
                    else "Показатель не меняется — сравнивать нечего."
                ),
                "enoughData": status == "ok",
            }
        )
    return results


def build_correlations(conn, today: date, days: int = 90) -> list[dict[str, Any]]:
    """LEGACY-пары §2.5 — тонкий срез отчёта v2 на lag=0.

    Отдельного порогового расчёта больше нет: единственный источник правды —
    :func:`build_correlation_report`. Старые поля (``left``/``right``/
    ``left_label``/``status``/``text``/``sentence``) сохранены, чтобы прежние
    экраны не сломались.
    """
    report = build_correlation_report(conn, today, days=days, lags=0)
    return legacy_pairs_from_report(report)


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
