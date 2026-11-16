"""Life OS — данные дашборда и сборка self-contained HTML-страницы.

Публичный контракт (INTERFACES.md §9)::

    collect_data(cfg, storage, now=None) -> dict
    build_dashboard(cfg, storage) -> str | None

``collect_data`` аккуратно вытаскивает статистику из SQLite через интерфейс
Storage (INTERFACES §3) и собирает словарь по схеме §9: today, kpis, series
(14 дней), goals, habits, feed (5 последних записей), heatmap (8 недель).

``build_dashboard`` вставляет этот словарь в ``dashboard/template.html`` как
``window.LIFEOS_DATA``, пишет ``paths.dashboard_out`` (по умолчанию
``dashboard/index.html``) и рядом ``data.json``, возвращает путь к HTML.

Принципы:
* никаких исключений наружу — всё пишется в лог и превращается в безопасное
  значение (None / 0 / пустой список);
* устойчивость к «странным» данным: None, строки вместо чисел, metrics как
  JSON-строка или dict, отсутствующие ключи, пустая БД;
* в JSON не попадают NaN/Infinity (только null) — страница не ломается.
"""

from __future__ import annotations

import html as _html
import json
import logging
import math
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

try:  # zoneinfo есть в Python 3.9+, но на всякий случай не падаем без него
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - экзотическое окружение
    ZoneInfo = None  # type: ignore[assignment]

__all__ = ["build_dashboard", "collect_data"]

log = logging.getLogger(__name__)

# --- константы схемы -------------------------------------------------------

WEEK_SLOTS = ("morning", "midday", "evening")
SERIES_DAYS = 14
HEATMAP_WEEKS = 8
HISTORY_DAYS = 366  # глубина выборки для стрика/хитмапа
DEFAULT_TZ = "Europe/Moscow"
DEFAULT_OUT = "dashboard/index.html"
DEFAULT_NAME = "Life OS"

MONTHS_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

# Слова, которыми пользователь говорит «сегодня не тренировался» / «отдых».
_NEGATIVE_TRAINING = (
    "не трениров", "без трениров", "не занимал", "отдых", "отдыхал",
    "пропустил", "rest day", "no training", "не было трениров",
)
# Для фолбэка подсчёта задач, если metrics нет.
_NEGATIVE_TASK = ("не сделал", "не выполнил", "не успел", "перенёс", "не доделал")
_POSITIVE_TASK = ("сделал", "сделала", "выполнил", "выполнила", "готово",
                  "закрыл", "закончил", "done", "завершил")

# Цели, у которых рост значения — это плохо (вес, похудение). Нужно для
# direction-логики прогресса: current/target для них даёт ложные 100%.
_DOWN_GOAL_RE = re.compile(r"вес|похуд|скинуть|сбросить", re.IGNORECASE)

# Атомарная запись: уникальный temp-файл + несколько попыток os.replace.
_WRITE_ATTEMPTS = 3
_WRITE_RETRY_DELAY = 0.03
_TMP_PREFIX = ".lifeos-dashboard-"

_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TIME_RE = re.compile(r"[T\s](\d{2}):(\d{2})")
_PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%")
_SPACES_RE = re.compile(r"[\s\u00a0\u202f]+")


# ==========================================================================
# Публичные функции
# ==========================================================================

def collect_data(cfg: dict, storage, now: datetime | None = None) -> dict:
    """Собрать данные для дашборда строго по схеме INTERFACES §9.

    Никогда не бросает исключений: при фатальной ошибке вернёт пустой скелет
    (все карточки отрисуются как «пока нет данных»).
    """
    try:
        return _collect(cfg, storage, now)
    except Exception:  # pragma: no cover - страховка от любых сюрпризов
        log.exception("dashboard: не удалось собрать данные")
        try:
            return _empty_payload(cfg, now)
        except Exception:
            log.exception("dashboard: не удалось собрать даже пустой скелет")
            return {}


def build_dashboard(cfg: dict, storage) -> str | None:
    """Собрать dashboard/index.html (+ data.json рядом) и вернуть путь к HTML.

    Ошибки логируются, наружу отдаётся None. Никогда не бросает.
    """
    try:
        cfg = cfg if isinstance(cfg, dict) else {}
        data = collect_data(cfg, storage)

        root = _project_root(cfg)
        template_path = _template_path(cfg, root)
        try:
            with open(template_path, "r", encoding="utf-8") as fh:
                template = fh.read()
        except OSError as exc:
            log.error("dashboard: не читается шаблон %s: %s", template_path, exc)
            return None

        out_path = _output_path(cfg, root)
        if not out_path:
            log.error("dashboard: в конфиге нет пути вывода (paths.dashboard_out)")
            return None

        page = _render(template, data, cfg)
        _write_atomic(out_path, page)

        data_path = os.path.join(os.path.dirname(out_path), "data.json")
        try:
            # Тот же безопасный дамп, что и для страницы: NaN/Infinity → null,
            # а сбой сериализации не должен отменять уже собранную страницу.
            _write_atomic(data_path, _json_dumps(data, compact=False) + "\n")
        except Exception:
            log.exception(
                "dashboard: не удалось записать %s — страница уже собрана",
                data_path,
            )

        log.info("dashboard: собрано %s", out_path)
        return out_path
    except Exception:
        log.exception("dashboard: ошибка сборки")
        return None


# ==========================================================================
# Сбор данных
# ==========================================================================

def _collect(cfg: dict, storage, now: datetime | None) -> dict:
    tz = _timezone_of(cfg)
    now_local = _local_now(now, tz)
    today = now_local.date()
    today_iso = today.isoformat()

    history_from = today - timedelta(days=HISTORY_DAYS)
    entries = _fetch_entries(storage, history_from.isoformat(), today_iso)
    days = _aggregate(entries)

    week_start = today - timedelta(days=6)
    week_dates = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]
    last14_dates = [
        (today - timedelta(days=SERIES_DAYS - 1 - i)).isoformat()
        for i in range(SERIES_DAYS)
    ]

    # --- today -----------------------------------------------------------
    today_entries = days.get(today_iso, [])
    checkins = {
        slot: any(_slot_of(entry) == slot for entry in today_entries)
        for slot in WEEK_SLOTS
    }
    today_block = {
        "date": today_iso,
        "checkins": checkins,
        "entries": len(today_entries),
    }

    # --- series (14 дней) ------------------------------------------------
    series = {
        "dates": last14_dates,
        "sleep_h": [
            _round(_day_mean(days, d, "sleep"), 1) for d in last14_dates
        ],
        "mood": [
            _round(_day_mean(days, d, "mood"), 1) for d in last14_dates
        ],
        "energy": [
            _round(_day_mean(days, d, "energy"), 1) for d in last14_dates
        ],
        "water_ml": [
            _day_sum(days, d, "water") for d in last14_dates
        ],
        "calories": [
            _day_sum(days, d, "calories") for d in last14_dates
        ],
        "training": [
            1 if _day_flag(days, d, "training") else 0 for d in last14_dates
        ],
    }

    # --- kpis ------------------------------------------------------------
    kpis = _kpis(days, entries, today, week_dates, last14_dates)

    # --- остальные блоки -------------------------------------------------
    goals = _goals(cfg, storage)
    habits = _habits(storage)
    feed = _feed(entries)
    heatmap = _heatmap(entries, today)

    data = {
        "generated_at": now_local.isoformat(timespec="seconds"),
        "today": today_block,
        "kpis": kpis,
        "series": series,
        "goals": goals,
        "habits": habits,
        "feed": feed,
        "heatmap": heatmap,
    }

    # Последняя страховка: в JSON не должно быть NaN/Infinity.
    # Результат обязательно присваиваем — иначе вычищенные значения
    # остаются только в копии и попадают в data.json как NaN.
    return _json_safe(data)


def _empty_payload(cfg: dict, now: datetime | None) -> dict:
    """Пустой, но валидный по схеме словарь (аварийный вариант)."""
    tz = _timezone_of(cfg)
    now_local = _local_now(now, tz)
    today = now_local.date()
    dates = [
        (today - timedelta(days=SERIES_DAYS - 1 - i)).isoformat()
        for i in range(SERIES_DAYS)
    ]
    return {
        "generated_at": now_local.isoformat(timespec="seconds"),
        "today": {
            "date": today.isoformat(),
            "checkins": {slot: False for slot in WEEK_SLOTS},
            "entries": 0,
        },
        "kpis": {
            "streak_days": 0,
            "checkin_rate_week": 0.0,
            "training_days_week": 0,
            "avg_sleep_h": None,
            "mood_avg": None,
            "tasks_done_week": 0,
            "tasks_planned_week": 0,
            "water_avg_ml": None,
        },
        "series": {
            "dates": dates,
            "sleep_h": [None] * SERIES_DAYS,
            "mood": [None] * SERIES_DAYS,
            "energy": [None] * SERIES_DAYS,
            "water_ml": [None] * SERIES_DAYS,
            "calories": [None] * SERIES_DAYS,
            "training": [0] * SERIES_DAYS,
        },
        "goals": [],
        "habits": [],
        "feed": [],
        "heatmap": _heatmap([], today),
    }


def _kpis(days, entries, today, week_dates, last14_dates) -> dict:
    """Ключевые показатели: неделя = последние 7 дней включая сегодня."""
    week_entries_count = 0
    week_training = 0
    filled_slots = 0
    for iso in week_dates:
        day_entries = days.get(iso, [])
        week_entries_count += len(day_entries)
        if _day_flag(days, iso, "training"):
            week_training += 1
        filled_slots += len({s for s in (_slot_of(e) for e in day_entries)
                             if s in WEEK_SLOTS})

    # Проверка «чек-инов»: сколько из 21 слота (7 дней × 3) заполнено.
    checkin_rate = round(min(1.0, max(0.0, filled_slots / 21.0)), 2)

    # Средние за неделю; если за неделю пусто — смотрим на 14 дней,
    # чтобы карточка не пустовала при данных «недельной давности».
    sleep_avg = _average_over(days, week_dates, "sleep")
    if sleep_avg is None:
        sleep_avg = _average_over(days, last14_dates, "sleep")
    mood_avg = _average_over(days, week_dates, "mood")
    if mood_avg is None:
        mood_avg = _average_over(days, last14_dates, "mood")

    water_avg = _average_of_sums(days, week_dates, "water")
    if water_avg is None:
        water_avg = _average_of_sums(days, last14_dates, "water")

    # Задачи: сначала metrics (tasks_done/tasks_planned), иначе эвристика.
    done = 0
    planned = 0
    fallback_done = 0
    for iso in week_dates:
        for entry in days.get(iso, []):
            metrics = _metrics(entry)
            done += _count(metrics.get("tasks_done"))
            planned += _count(metrics.get("tasks_planned"))
            if _looks_like_done_task(entry):
                fallback_done += 1
    if done == 0 and planned == 0:
        done = fallback_done

    return {
        "streak_days": _streak(days, today),
        "checkin_rate_week": checkin_rate,
        "training_days_week": week_training,
        "avg_sleep_h": _round(sleep_avg, 1),
        "mood_avg": _round(mood_avg, 1),
        "tasks_done_week": int(done),
        "tasks_planned_week": int(max(planned, done)),
        "water_avg_ml": _round_int(water_avg),
    }


def _streak(days: dict, today: date) -> int:
    """Сколько дней подряд есть хотя бы одна запись (сегодня ещё можно успеть)."""
    cursor = today
    if not days.get(cursor.isoformat()):
        cursor -= timedelta(days=1)
    count = 0
    while count < HISTORY_DAYS and days.get(cursor.isoformat()):
        count += 1
        cursor -= timedelta(days=1)
    return count


def _aggregate(entries: list) -> dict:
    """Сгруппировать записи по дате и извлечь метрики.

    Возвращает {YYYY-MM-DD: [entry, ...]} — дальше статистика считается
    по этому словарю и по «метрическим» аккумуляторам ниже.
    """
    by_date: dict = {}
    for entry in entries:
        iso = entry["date"]
        by_date.setdefault(iso, []).append(entry)
    return by_date


def _day_mean(by_date: dict, iso: str, key: str):
    """Среднее по дню для sleep/mood/energy (None — если данных нет)."""
    values = []
    for entry in by_date.get(iso, []):
        value = _num(_metrics(entry).get({"sleep": "sleep_h",
                                          "mood": "mood",
                                          "energy": "energy"}[key]))
        if value is None:
            continue
        if key == "sleep":
            if 0 < value <= 24:
                values.append(value)
        elif 1 <= value <= 5:
            values.append(value)
    if not values:
        return None
    return sum(values) / len(values)


def _day_sum(by_date: dict, iso: str, key: str):
    """Сумма за день для water_ml / calories; None, если данных не было."""
    total = 0.0
    seen = False
    for entry in by_date.get(iso, []):
        value = _num(_metrics(entry).get(
            {"water": "water_ml", "calories": "calories"}[key]))
        if value is None or value < 0:
            continue
        if value > 50000:  # мусорные значения не должны ломать график
            continue
        total += value
        seen = True
    if not seen:
        return None
    return _round_int(total)


def _day_flag(by_date: dict, iso: str, key: str) -> bool:
    if key != "training":
        return False
    return any(_is_training(entry) for entry in by_date.get(iso, []))


def _average_over(by_date: dict, dates: list, key: str):
    values = [_day_mean(by_date, iso, key) for iso in dates]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _average_of_sums(by_date: dict, dates: list, key: str):
    values = [_day_sum(by_date, iso, key) for iso in dates]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


# ==========================================================================
# Цели, привычки, лента, хитмап
# ==========================================================================

def _goals(cfg: dict, storage) -> list:
    raw = _safe_call(lambda: storage.goals(), [], "goals")
    if not isinstance(raw, (list, tuple)):
        return []
    goals = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"), 120) or "Без названия"
        status = _clean_text(item.get("status"), 40) or "active"
        progress = _goal_progress(item)
        deadline = _norm_date(item.get("deadline")) or _clean_text(
            item.get("deadline"), 40)
        goals.append({
            "title": title,
            "progress": progress,
            "deadline": deadline or "",
            "status": status,
            "link": _goal_link(cfg, title),
        })
    # активные — сверху, дальше по сроку (без срока — в конце)
    goals.sort(key=lambda g: (
        0 if g["status"].lower() in ("active", "активна", "в работе") else 1,
        g["deadline"] or "9999-99-99",
        g["title"].lower(),
    ))
    return goals


def _goal_direction(goal: dict) -> str:
    """Направление цели: ``down`` (чем меньше, тем лучше) или ``up``.

    Схему Storage не меняем: если хранилище отдало ``direction`` — уважаем
    его (``up``/``down`` и русские синонимы). Иначе весовые цели определяем
    по названию («вес», «похудеть», «скинуть», «сбросить») — для них рост
    значения означает движение назад, а не 100%.
    """
    raw = goal.get("direction") if isinstance(goal, dict) else None
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("down", "вниз", "снижение", "уменьшение", "меньше",
                     "похудение", "сбросить"):
            return "down"
        if value in ("up", "вверх", "рост", "увеличение", "больше"):
            return "up"
    title = str(goal.get("title") or "") if isinstance(goal, dict) else ""
    return "down" if _DOWN_GOAL_RE.search(title) else "up"


def _goal_progress(goal: dict) -> float:
    """Прогресс 0..1: current/target, статус done, или процент из текста.

    Для убывающих целей (вес и т.п.) при ``current > target`` формула
    ``current/target`` давала ложные 100%; считаем оставшийся путь
    относительно текущего значения:
    ``1 - (current - target) / max(current, 1)``. Если цель уже достигнута
    (``current <= target``), работает обычная формула роста.
    """
    status = str(goal.get("status") or "").strip().lower()
    if status in ("done", "completed", "complete", "выполнена", "выполнено",
                  "готово", "завершена", "закрыта"):
        return 1.0

    direct = goal.get("progress")
    direct_num = _num(direct)
    if direct_num is not None and 0 <= direct_num <= 1:
        return round(float(direct_num), 3)

    target = _num(goal.get("target"))
    current = _num(goal.get("current"))
    progress = None
    if target is not None and target > 0 and current is not None:
        if current > target and _goal_direction(goal) == "down":
            progress = 1.0 - (current - target) / max(current, 1.0)
        else:
            progress = current / target
    if progress is None:
        # Иногда прогресс записан как «60%» в current/note.
        for field in ("current", "note", "target"):
            match = _PERCENT_RE.search(str(goal.get(field) or ""))
            if match:
                progress = _num(match.group(1))
                if progress is not None:
                    progress = progress / 100.0
                break
    if progress is None:
        return 0.0
    return round(min(1.0, max(0.0, progress)), 3)


def _goal_link(cfg: dict, title: str) -> str:
    """Ссылка obsidian:// на страницу целей (если Obsidian включён)."""
    try:
        obsidian = cfg.get("obsidian") if isinstance(cfg, dict) else None
        if not isinstance(obsidian, dict) or not obsidian.get("enabled", True):
            return ""
        vault = str(obsidian.get("vault_path") or "").rstrip("/")
        rel = str(obsidian.get("goals_path") or "").strip()
        if not vault or not rel:
            return ""
        vault_name = os.path.basename(vault)
        if not vault_name:
            return ""
        rel_file = rel[:-3] if rel.lower().endswith(".md") else rel
        from urllib.parse import quote
        return "obsidian://open?vault=%s&file=%s" % (
            quote(vault_name), quote(rel_file))
    except Exception:
        return ""


def _habits(storage) -> list:
    raw = _safe_call(lambda: storage.habits(), [], "habits")
    if not isinstance(raw, (list, tuple)):
        return []
    habits = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"), 80)
        if not name:
            continue
        habits.append({
            "name": name,
            "streak": int(_num(item.get("streak")) or 0),
            "last_done": _norm_date(item.get("last_done")) or "",
        })
    return habits


def _feed(entries: list) -> list:
    """5 последних записей, новые сверху."""
    def key(entry):
        return (entry.get("date") or "", str(entry.get("ts") or ""),
                entry.get("id") or 0)

    ordered = sorted(entries, key=key, reverse=True)
    feed = []
    for entry in ordered[:5]:
        text = (_clean_text(entry.get("text"), 320)
                or _clean_text(entry.get("title"), 320)
                or _clean_text(entry.get("raw"), 320))
        category = _clean_text(entry.get("category"), 24) or "other"
        feed.append({
            "date": entry.get("date") or "",
            "time": _time_of(entry.get("ts")),
            "category": category,
            "text": text,
        })
    return feed


def _heatmap(entries: list, today: date) -> dict:
    """8 недель × 7 дней, столбцы — недели с понедельника.

    Хитмап по ТЗ §3.6 — «чек-инов», поэтому считаем только записи слот-сессий
    (morning/midday/evening), а не любые записи (вода, мысли и т.п.). Схема
    ``cells[{date,count}]`` не меняется.
    """
    weeks = HEATMAP_WEEKS
    monday = today - timedelta(days=today.weekday())
    start = monday - timedelta(weeks=weeks - 1)
    counts: dict = {}
    for entry in entries:
        if not isinstance(entry, dict) or _slot_of(entry) not in WEEK_SLOTS:
            continue
        day = entry.get("date")
        if not isinstance(day, str):
            continue
        counts[day] = counts.get(day, 0) + 1
    cells = []
    for index in range(weeks * 7):
        day_iso = (start + timedelta(days=index)).isoformat()
        cells.append({"date": day_iso, "count": counts.get(day_iso, 0)})
    return {"start": start.isoformat(), "weeks": weeks, "cells": cells}


# ==========================================================================
# Чтение Storage (мягко, без падений)
# ==========================================================================

def _fetch_entries(storage, date_from: str, date_to: str) -> list:
    if storage is None:
        return []
    raw = _safe_call(lambda: storage.entries(date_from, date_to), [],
                     "entries")
    if not isinstance(raw, (list, tuple)):
        return []
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        iso = _norm_date(item.get("date"))
        if not iso:
            continue
        entry = dict(item)
        entry["date"] = iso
        entries.append(entry)
    return entries


def _safe_call(call, default, what: str):
    try:
        result = call()
    except Exception as exc:
        log.warning("dashboard: storage.%s недоступен: %s", what, exc)
        return default
    return default if result is None else result


# ==========================================================================
# Рендер и запись файлов
# ==========================================================================

def _render(template: str, data: dict, cfg: dict) -> str:
    """Подставить в шаблон JSON, дату генерации и название приложения."""
    # _json_dumps экранирует все «<» как \u003c: ни «</script>», ни
    # «<!--<script>» в тексте пользователя не могут закрыть/открыть тег.
    payload = _json_dumps(data, compact=True)

    generated = data.get("generated_at") or ""
    generated_human = _human_datetime(generated)
    app_name = DEFAULT_NAME
    try:
        app = cfg.get("app")
        if isinstance(app, dict):
            candidate = _clean_text(app.get("name"), 60)
            if candidate:
                app_name = candidate
    except Exception:
        pass

    replacements = {
        "{{LIFEOS_DATA}}": payload,
        "{{GENERATED_AT}}": _html.escape(str(generated)),
        "{{GENERATED_HUMAN}}": _html.escape(generated_human),
        "{{GENERATED_YEAR}}": _html.escape(generated[:4] or "2026"),
        "{{APP_NAME}}": _html.escape(app_name),
    }
    page = template
    for token, value in replacements.items():
        page = page.replace(token, value)

    if "{{LIFEOS_DATA}}" in template:  # уже заменено
        return page

    # Шаблон без плейсхолдера данных — не оставляем страницу пустой.
    log.warning("dashboard: в шаблоне нет {{LIFEOS_DATA}}, вставляю в конец")
    script = "<script>window.LIFEOS_DATA = %s;</script>\n" % payload
    if "</body>" in page:
        return page.replace("</body>", script + "</body>", 1)
    return page + script


def _json_dumps(value, compact: bool) -> str:
    """JSON без NaN/Infinity и без литеральных «<»/«>» внутри строк.

    Экранирование «<» в ``\\u003c`` обязательно: данные вставляются внутрь
    ``<script>``, и текст вида ``<!--<script>`` ломает HTML-парсер (браузер
    видит два тега вместо одного). ``\\u003c`` — валидный JSON/JS-escape,
    поэтому ``json.loads`` и ``JSON.parse`` возвращают исходный текст.
    """
    kwargs = {"ensure_ascii": False, "allow_nan": False}
    if not compact:
        kwargs["indent"] = 2
    try:
        text = json.dumps(value, **kwargs)
    except (TypeError, ValueError):
        log.exception("dashboard: данные не сериализуются в JSON")
        try:
            # NaN/Infinity → null; нестандартные объекты — через str().
            text = json.dumps(_json_safe(value), default=str, **kwargs)
        except (TypeError, ValueError):
            log.exception("dashboard: безопасный дамп тоже не удался")
            return "{}"

    # «<» и «>» вне строк в JSON не встречаются, поэтому замена безопасна.
    text = text.replace("<", "\\u003c").replace(">", "\\u003e")
    # U+2028/U+2029 — исторически переводы строк для JS-парсеров.
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _replace_with_retry(source: str, target: str) -> None:
    """``os.replace`` с несколькими попытками (кратковременный сбой ФС)."""
    last_error = None
    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            last_error = exc
            if attempt < _WRITE_ATTEMPTS:
                log.warning(
                    "dashboard: не удалось заменить %s (попытка %d/%d): %s",
                    target, attempt, _WRITE_ATTEMPTS, exc,
                )
                time.sleep(_WRITE_RETRY_DELAY * attempt)
    if last_error is not None:
        raise last_error


def _write_atomic(path: str, text: str) -> None:
    """Запись через уникальный временный файл + ``os.replace``.

    Уникальный temp (``mkstemp``) обязателен: при параллельных сборках общий
    ``index.html.tmp`` приводил к тому, что 3 из 4 потоков теряли результат.
    """
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=_TMP_PREFIX, suffix=".tmp", dir=folder
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp создаёт 0600; для локальной страницы это нормально,
        # но права существующего файла сохраняем.
        try:
            if os.path.exists(path):
                os.chmod(tmp_name, os.stat(path).st_mode & 0o7777)
        except OSError:  # pragma: no cover - права не критичны
            pass
        _replace_with_retry(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


def _project_root(cfg: dict) -> str:
    try:
        root = cfg.get("_root") if isinstance(cfg, dict) else None
        if isinstance(root, str) and os.path.isdir(root):
            return os.path.abspath(root)
    except Exception:
        pass
    # src/dashboard_data.py → родитель src = project_root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _template_path(cfg: dict, root: str) -> str:
    custom = None
    try:
        paths = cfg.get("paths")
        if isinstance(paths, dict):
            custom = paths.get("dashboard_template")
    except Exception:
        custom = None
    if isinstance(custom, str) and custom.strip():
        candidate = custom if os.path.isabs(custom) else os.path.join(root, custom)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(root, "dashboard", "template.html")


def _output_path(cfg: dict, root: str) -> str | None:
    raw = None
    try:
        paths = cfg.get("paths")
        if isinstance(paths, dict):
            raw = paths.get("dashboard_out")
    except Exception:
        raw = None
    if not isinstance(raw, str) or not raw.strip():
        raw = DEFAULT_OUT
    raw = raw.strip()
    return raw if os.path.isabs(raw) else os.path.join(root, raw)


def _human_datetime(value: str) -> str:
    """ISO-время → «16 ноября 2026, 16:42» (русская локаль без locale)."""
    if not isinstance(value, str) or len(value) < 16:
        return ""
    try:
        dt_value = datetime.fromisoformat(value)
    except ValueError:
        return value
    return "%d %s %d, %02d:%02d" % (
        dt_value.day, MONTHS_RU[dt_value.month - 1], dt_value.year,
        dt_value.hour, dt_value.minute)


# ==========================================================================
# Мелкие утилиты: конфиг, даты, числа, текст
# ==========================================================================

def _timezone_of(cfg: dict):
    name = DEFAULT_TZ
    try:
        app = cfg.get("app") if isinstance(cfg, dict) else None
        if isinstance(app, dict):
            candidate = app.get("timezone")
            if isinstance(candidate, str) and candidate.strip():
                name = candidate.strip()
    except Exception:
        pass
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            log.warning("dashboard: неизвестная таймзона %r, беру Москву", name)
            try:
                return ZoneInfo(DEFAULT_TZ)
            except Exception:
                pass
    return timezone(timedelta(hours=3))


def _local_now(now, tz) -> datetime:
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=tz)
        return now.astimezone(tz)
    if isinstance(now, date):
        return datetime(now.year, now.month, now.day, tzinfo=tz)
    return datetime.now(tz)


def _norm_date(value) -> str | None:
    """Привести дату к YYYY-MM-DD (или None, если не получилось)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = _DATE_RE.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)),
                        int(match.group(3))).isoformat()
        except ValueError:
            return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _time_of(ts) -> str:
    if not isinstance(ts, str):
        return ""
    match = _TIME_RE.search(ts) or re.match(r"^(\d{2}):(\d{2})", ts.strip())
    if not match:
        return ""
    return "%s:%s" % (match.group(1), match.group(2))


def _num(value):
    """Число из чего угодно: 7.5, "7,5 ч", "1 800 мл", "$6,000", "60%".

    Возвращает float или None; NaN/Infinity и bool отбрасываются.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        squeezed = _SPACES_RE.sub("", value)
        if not squeezed:
            return None
        match = _NUM_RE.search(squeezed)
        if not match:
            return None
        try:
            number = float(match.group(0).replace(",", "."))
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    if isinstance(value, (list, tuple)):
        return len(value) if value else None
    if isinstance(value, dict):
        return len(value) if value else None
    return None


def _count(value) -> int:
    """Количество элементов в tasks_done/tasks_planned (список/число/JSON)."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, (list, tuple, dict)):
                    return len(parsed)
            except (ValueError, TypeError):
                pass
        number = _num(text)
        return int(number) if number is not None and number > 0 else 0
    number = _num(value)
    return int(number) if number is not None and number > 0 else 0


def _round(value, digits: int):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _round_int(value):
    rounded = _round(value, 0)
    return None if rounded is None else int(rounded)


def _metrics(entry: dict) -> dict:
    """metrics может быть dict, JSON-строкой или None."""
    if not isinstance(entry, dict):
        return {}
    raw = entry.get("metrics")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _slot_of(entry: dict) -> str:
    slot = entry.get("slot") if isinstance(entry, dict) else None
    return slot.strip().lower() if isinstance(slot, str) else ""


def _text_of(entry: dict) -> str:
    parts = []
    for field in ("title", "text", "raw"):
        value = entry.get(field) if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " ".join(parts).lower()


def _is_training(entry: dict) -> bool:
    """Запись считается тренировкой (осторожно с «не тренировался»)."""
    metrics = _metrics(entry)
    text = _text_of(entry)
    negative = any(marker in text for marker in _NEGATIVE_TRAINING)

    duration = _num(metrics.get("duration_min"))
    if duration is not None and duration > 0:
        return True

    training_type = metrics.get("training_type")
    if isinstance(training_type, str) and training_type.strip():
        normalized = training_type.strip().lower()
        if normalized in ("нет", "none", "-", "rest", "отдых", "0", "не было"):
            return False
        return True
    if isinstance(metrics.get("training"), bool):
        return bool(metrics["training"])

    if entry.get("category") == "training":
        return not negative
    return False


def _looks_like_done_task(entry: dict) -> bool:
    """Фолбэк, когда в metrics нет tasks_done/tasks_planned."""
    if _metrics(entry).get("tasks_done") or _metrics(entry).get(
            "tasks_planned"):
        return False
    text = _text_of(entry)
    category = entry.get("category")
    if category != "tasks" and not any(word in text for word in ("задач", "сделал")):
        return False
    if any(marker in text for marker in _NEGATIVE_TASK):
        return False
    return any(marker in text for marker in _POSITIVE_TASK)


def _clean_text(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        if isinstance(value, (int, float)):
            value = str(value)
        else:
            return ""
    text = _SPACES_RE.sub(" ", value).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _json_safe(value):
    """Рекурсивно вычистить NaN/Infinity (JSON не допускает их)."""
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
