"""Практики и очки: каталог, сессии, журнал начислений, уровни и серии.

Дизайн — ``studio/PRACTICES.md``, постановка — задача t33. Модуль общий для
трёх потребителей, поэтому логика живёт здесь, а не в роутере:

* **веб-плеер** (``POST /api/practice-sessions``) сохраняет сессию и получает
  очки, серию и уровень одним ответом;
* **бот** (``app/bot/store_v4.py``) зовёт :func:`save_practice_session` и
  :func:`points_summary` в процессе, без HTTP;
* **сидер** заводит каталог (:func:`ensure_practice_catalog`) и демо-историю
  теми же функциями начисления, что и живая работа, — поэтому демо-очки
  подчиняются тем же капам и не расходятся с API.

Решения, которые видно снаружи:

* **Сумма очков всегда вычисляется из ``points_ledger``** (:func:`total_points`).
  Счётчика-строки «всего очков» нет: она неизбежно разъезжается с историей,
  когда запись удаляют или начисление не прошло.
* **Идемпотентность — уникальным индексом, а не проверкой.** У
  ``points_ledger`` есть ``UNIQUE(kind, ref_id)``, начисление идёт одной
  инструкцией ``INSERT ... ON CONFLICT DO NOTHING``: повтор с тем же
  ``ref_id`` не даёт очков второй раз и не оставляет гонки
  «прочитал-проверил-записал».
* **Одна сессия на практику в день** (``UNIQUE(practice_id, date)``).
  Повторное сохранение (двойной тап, ретрай запроса) обновляет
  ``rounds_done``/``best_hold_s`` по максимуму и не плодит строк. Первая
  завершённая сессия дня даёт фиксированные 10 очков. Повторные круги,
  добавленные к уже записанной сессии (``rounds_increment``), дают очки по
  ставке практики (10 за круг, 5 для 4-7-8): у каждого круга свой ``ref_id``,
  поэтому один и тот же круг не оплачивается дважды, а новая работа — да.
* **Начисления**: практика 10, новый личный рекорд задержки +5, день серии
  +2 (не больше 20 за день), привычка 2, дело 1, чек-ин 3, мысль 1. Кап дня —
  60 очков: одним нажатием не нафармить.
* **Уровни** — Новичок / Устойчивый / В форме / Мастер; пороги лежат в
  ``settings_kv['points.levels']`` (по умолчанию 0 / 1000 / 5000 / 15000) и
  правятся :func:`save_levels`.
* **Безопасность**: у дыхательных практик ``safety_note`` не пустой — это
  требование постановки, его проверяет и тест, и сидер.

Даты — ISO ``YYYY-MM-DD``, времена — ISO-8601 с часовым поясом. Время
передаётся снаружи (``now``), чтобы API брало его из настроек, а сидер мог
писать историю за прошлые дни.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from app.server.db import get_setting, set_setting

__all__ = [
    "CATALOG_SLUGS",
    "DAY_CAP",
    "DEFAULT_LEVELS",
    "FALLBACK_SAFETY_NOTE",
    "KIND_LABELS",
    "LEDGER_KINDS",
    "LEVELS_KEY",
    "POINTS",
    "POINTS_PER_ROUND",
    "PRACTICE_CATALOG",
    "SESSION_SOURCES",
    "STREAK_BONUS_CAP",
    "STREAK_POINTS",
    "award",
    "day_points",
    "ensure_practice_catalog",
    "format_hold",
    "get_practice",
    "level_for",
    "levels_with_default",
    "list_practices",
    "load_levels",
    "points_per_round",
    "practice_id_for",
    "practice_streak",
    "points_summary",
    "save_levels",
    "save_practice_session",
    "total_points",
]

# --------------------------------------------------------------------------
# Константы начисления
# --------------------------------------------------------------------------

#: Сколько очков даёт одно событие. ``record`` и ``streak`` — бонусы практики.
POINTS: Mapping[str, int] = {
    "practice": 10,
    "record": 5,
    "habit": 2,
    "task": 1,
    "checkin": 3,
    "journal": 1,
}

#: Очки за повторный круг в тот же день: у каждой практики своя ставка
#: (10 за круг Вим Хофа, 5 за круг 4-7-8), по умолчанию — как за сессию.
POINTS_PER_ROUND: Mapping[str, int] = {
    "wim-hof": 10,
    "breath-478": 5,
}


def points_per_round(slug: str) -> int:
    """Ставка за дополнительный круг: своя для практики или 10 по умолчанию."""
    return int(POINTS_PER_ROUND.get(str(slug or "").strip().lower(), POINTS["practice"]))

#: Виды начислений в ``points_ledger.kind`` (полный список для CHECK схемы).
LEDGER_KINDS: tuple[str, ...] = (
    "practice",
    "record",
    "streak",
    "habit",
    "task",
    "checkin",
    "journal",
)

#: Откуда пришла сессия. ``demo`` — метка строк сидера, по ней ``clear_demo``
#: отличает демонстрационную историю от живой.
SESSION_SOURCES: tuple[str, ...] = ("web", "telegram", "demo")

#: Бонус за день серии: 2 очка за каждый день, но не больше 20 в день.
STREAK_POINTS = 2
STREAK_BONUS_CAP = 20

#: Кап очков в день: даже серия практик и все привычки не дают больше.
DAY_CAP = 60

#: Ограничения на вход: защита от мусора и от «нафармить» одним запросом.
MAX_ROUNDS = 100
MAX_HOLD_S = 3600

#: Ключ порогов уровней в ``settings_kv``.
LEVELS_KEY = "points.levels"

#: Пороги по умолчанию: Новичок → Устойчивый → В форме → Мастер.
DEFAULT_LEVELS: tuple[dict[str, Any], ...] = (
    {"key": "novice", "title": "Новичок", "min": 0},
    {"key": "steady", "title": "Устойчивый", "min": 1000},
    {"key": "fit", "title": "В форме", "min": 5000},
    {"key": "master", "title": "Мастер", "min": 15000},
)

FALLBACK_SAFETY_NOTE = (
    "Не в воде и не за рулём. При эпилепсии, беременности, проблемах с давлением "
    "и сердцем — только после консультации с врачом. Закружилась голова — "
    "остановись и сядь."
)


# --------------------------------------------------------------------------
# Каталог практик (стартовый набор)
# --------------------------------------------------------------------------


def _practice(
    slug: str,
    title: str,
    kind: str,
    description: str,
    steps: Sequence[Mapping[str, Any]],
    levels: Sequence[Mapping[str, Any]],
    safety_note: str,
) -> dict[str, Any]:
    """Одна практика каталога: шаги, уровни и посчитанная длительность."""
    duration = 0
    for step in steps:
        rounds = int(step.get("rounds") or 1)
        duration += int(step.get("seconds") or 0) * rounds
    return {
        "slug": slug,
        "title": title,
        "kind": kind,
        "description": description,
        "steps": [dict(step) for step in steps],
        "levels": [dict(level) for level in levels],
        "safetyNote": safety_note,
        "durationS": int(duration),
    }


#: Шесть стартовых практик. Тексты и тайминги свои, никаких чужих аудио.
#: Уровни — раунды и задержки; ровно один уровень помечен ``default``.
PRACTICE_CATALOG: tuple[dict[str, Any], ...] = (
    _practice(
        "wim-hof",
        "Дыхание Вим Хофа",
        "breathing",
        "Три раунда: 30 быстрых вдохов, задержка на выдохе, спокойный "
        "восстановительный вдох на 15 секунд. Продвинутый режим — задержки "
        "1:30 / 2:00 / 2:30.",
        (
            {"phase": "breath", "label": "Быстрые вдохи", "seconds": 2, "rounds": 30},
            {"phase": "hold", "label": "Задержка на выдохе", "seconds": 90},
            {"phase": "recover", "label": "Восстановительный вдох", "seconds": 15},
        ),
        (
            {
                "key": "basic",
                "title": "Базовый",
                "rounds": 3,
                "holds": [60, 75, 90],
                "roundSeconds": [60, 15],
            },
            {
                "key": "advanced",
                "title": "Продвинутый",
                "rounds": 3,
                "holds": [90, 120, 150],
                "roundSeconds": [60, 15],
                "default": True,
            },
        ),
        FALLBACK_SAFETY_NOTE,
    ),
    _practice(
        "breath-478",
        "Дыхание 4-7-8",
        "breathing",
        "Вдох 4 секунды, задержка 7, выдох 8. Классическая схема перед сном: "
        "выключает «разгон» мыслей.",
        (
            {"phase": "inhale", "label": "Вдох", "seconds": 4},
            {"phase": "hold", "label": "Задержка", "seconds": 7},
            {"phase": "exhale", "label": "Выдох", "seconds": 8},
        ),
        (
            {
                "key": "basic",
                "title": "Базовый",
                "rounds": 4,
                "holds": [],
                "cycleSeconds": 19,
                "default": True,
            },
            {"key": "deep", "title": "Глубже", "rounds": 6, "holds": [], "cycleSeconds": 19},
        ),
        FALLBACK_SAFETY_NOTE,
    ),
    _practice(
        "box-breathing",
        "Боксовое дыхание",
        "breathing",
        "Вдох 4 — задержка 4 — выдох 4 — задержка 4. Ровный счёт перед "
        "сложным разговором или выступлением.",
        (
            {"phase": "inhale", "label": "Вдох", "seconds": 4},
            {"phase": "hold", "label": "Задержка после вдоха", "seconds": 4},
            {"phase": "exhale", "label": "Выдох", "seconds": 4},
            {"phase": "hold", "label": "Задержка после выдоха", "seconds": 4},
        ),
        (
            {"key": "basic", "title": "Базовый", "rounds": 6, "holds": [], "cycleSeconds": 16},
            {
                "key": "long",
                "title": "Дольше",
                "rounds": 10,
                "holds": [],
                "cycleSeconds": 16,
                "default": True,
            },
        ),
        FALLBACK_SAFETY_NOTE,
    ),
    _practice(
        "breath-55",
        "Дыхание 5-5",
        "breathing",
        "Вдох 5 секунд, выдох 5 секунд, без задержек. Самое мягкое "
        "успокоение: можно делать при бессоннице и в дороге.",
        (
            {"phase": "inhale", "label": "Вдох", "seconds": 5},
            {"phase": "exhale", "label": "Выдох", "seconds": 5},
        ),
        (
            {
                "key": "basic",
                "title": "Базовый",
                "rounds": 10,
                "holds": [],
                "cycleSeconds": 10,
                "default": True,
            },
            {"key": "long", "title": "Дольше", "rounds": 18, "holds": [], "cycleSeconds": 10},
        ),
        FALLBACK_SAFETY_NOTE,
    ),
    _practice(
        "stretch-7",
        "Растяжка 7 минут",
        "stretch",
        "Семь спокойных движений по минуте: шея, плечи, спина, таз, "
        "бёдра, икры, дыхание. Без рывков и без боли — только до натяжения.",
        (
            {"phase": "move", "label": "Шея и плечи", "seconds": 60},
            {"phase": "move", "label": "Наклон вперёд", "seconds": 60},
            {"phase": "move", "label": "Скрутка лёжа", "seconds": 60},
            {"phase": "move", "label": "Таз и сгибатели бедра", "seconds": 60},
            {"phase": "move", "label": "Бёдра и внутренняя поверхность", "seconds": 60},
            {"phase": "move", "label": "Икры и стопы", "seconds": 60},
            {"phase": "rest", "label": "Дыхание лёжа", "seconds": 60},
        ),
        (
            {
                "key": "basic",
                "title": "Базовый",
                "rounds": 1,
                "holds": [],
                "default": True,
            },
        ),
        "Растягивайся до натяжения, а не до боли. Травма, грыжа или "
        "недавняя операция — сначала врач; болят суставы — прекрати.",
    ),
    _practice(
        "cold-shower-60",
        "Холодный душ 60 секунд",
        "other",
        "Минута под холодной водой после обычного душа. Дыхание ровное, "
        "не задерживать: тело согревается само через минуту.",
        (
            {"phase": "cold", "label": "Холодная вода", "seconds": 60},
        ),
        (
            {
                "key": "basic",
                "title": "60 секунд",
                "rounds": 1,
                "holds": [],
                "default": True,
            },
            {"key": "long", "title": "90 секунд", "rounds": 1, "holds": []},
        ),
        "Не начинай с ледяной воды в одиночку. Сердце, давление, "
        "эпилепсия, беременность — сначала врач. Появился озноб или "
        "онемение — сразу выходи.",
    ),
)

#: Слаги каталога: по ним сидер и бот отличают стартовый набор.
CATALOG_SLUGS: tuple[str, ...] = tuple(item["slug"] for item in PRACTICE_CATALOG)


# --------------------------------------------------------------------------
# Помощники: JSON, даты, форматирование
# --------------------------------------------------------------------------


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":"))


def _loads(text: Any, fallback: Any) -> Any:
    """Разбор JSON из БД: битая строка не должна ронять ответ API."""
    if isinstance(text, (list, dict)):
        return text
    if not text:
        return fallback
    try:
        value = json.loads(str(text))
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _now(now: datetime | date_cls | None) -> datetime:
    """Момент времени: переданный, иначе текущий в локальном поясе."""
    if isinstance(now, datetime):
        return now
    if isinstance(now, date_cls):
        return datetime(now.year, now.month, now.day, 12, 0, 0).astimezone()
    return datetime.now().astimezone()


def _day(now: datetime | date_cls | None) -> str:
    return _now(now).date().isoformat()


def format_hold(seconds: Any) -> str:
    """Задержка по-человечески: ``150`` → ``«2:30»``, ``45`` → ``«45 с»``."""
    try:
        total = int(round(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return "0 с"
    if total < 60:
        return "%d с" % total
    return "%d:%02d" % (total // 60, total % 60)


def _plural_days(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "%d день" % count
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "%d дня" % count
    return "%d дней" % count


# --------------------------------------------------------------------------
# Каталог: чтение и идемпотентное заполнение
# --------------------------------------------------------------------------


def _practice_row(row: sqlite3.Row) -> dict[str, Any]:
    """Строка ``practices`` → камелКейс-JSON (без личных цифр)."""
    steps = _loads(row["steps_json"], [])
    levels = _loads(row["levels_json"], [])
    return {
        "id": int(row["id"]),
        "slug": str(row["slug"]),
        "title": str(row["title"]),
        "kind": str(row["kind"]),
        "description": str(row["description"] or ""),
        "durationS": int(row["duration_s"] or 0),
        "steps": [step for step in steps if isinstance(step, Mapping)],
        "levels": [level for level in levels if isinstance(level, Mapping)],
        "defaultLevel": _default_level(levels),
        "safetyNote": str(row["safety_note"] or ""),
        "archived": bool(row["archived"]),
    }


def _default_level(levels: Any) -> str:
    """Ключ уровня по умолчанию: помеченный ``default``, иначе первый."""
    if not isinstance(levels, list) or not levels:
        return ""
    for level in levels:
        if isinstance(level, Mapping) and level.get("default"):
            return str(level.get("key") or "")
    first = levels[0]
    return str(first.get("key") or "") if isinstance(first, Mapping) else ""


def levels_with_default(levels: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Уровни для API: один и только один помечен ``default``."""
    items = [dict(level) for level in (levels or []) if isinstance(level, Mapping)]
    if not items:
        return items
    marker = _default_level(items)
    for level in items:
        level["default"] = str(level.get("key") or "") == marker
    return items


def ensure_practice_catalog(conn: sqlite3.Connection, *, stamp: str | None = None) -> int:
    """Заводит стартовый набор из шести практик (идемпотентно).

    ``INSERT OR IGNORE`` по уникальному ``slug``: повторный вызов ничего не
    меняет, а правки пользователя (переименовал, поправил описание, ушёл в
    архив) не перетираются — каталог это стартовая точка, а не константа.

    Две оговорки для баз, которые успели пожить на ленивой схеме бота:

    * строка-стаб (шаги или уровни пустые) доливается содержимым каталога —
      иначе плеер открывает практику и не находит фаз; свои непустые шаги
      пользователя не трогаем;
    * стабы **не из каталога** уходят в архив: у них нет шагов, запустить их
      нельзя, но история сессий и очки остаются на месте.

    Возвращает число реально вставленных строк.
    """
    created = stamp or datetime.now().astimezone().isoformat(timespec="seconds")
    inserted = 0
    for index, item in enumerate(PRACTICE_CATALOG):
        cursor = conn.execute(
            "INSERT OR IGNORE INTO practices"
            "(slug, title, kind, description, steps_json, levels_json, safety_note, "
            "duration_s, sort, created, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                item["slug"],
                item["title"],
                item["kind"],
                item["description"],
                _dumps(item["steps"]),
                _dumps(levels_with_default(item["levels"])),
                item["safetyNote"],
                item["durationS"],
                index,
                created,
            ),
        )
        inserted += int(cursor.rowcount or 0)
        if not int(cursor.rowcount or 0):
            # Строка уже есть: запись старой схемы (шаги пустые) получает
            # содержимое каталога. Непустые шаги — правка пользователя.
            conn.execute(
                "UPDATE practices SET title = ?, kind = ?, description = ?, "
                "steps_json = ?, levels_json = ?, safety_note = ?, duration_s = ?, "
                "sort = ? "
                "WHERE slug = ? AND (steps_json IN ('', '[]') OR levels_json IN ('', '[]'))",
                (
                    item["title"],
                    item["kind"],
                    item["description"],
                    _dumps(item["steps"]),
                    _dumps(levels_with_default(item["levels"])),
                    item["safetyNote"],
                    item["durationS"],
                    index,
                    item["slug"],
                ),
            )
    slugs = tuple(item["slug"] for item in PRACTICE_CATALOG)
    conn.execute(
        "UPDATE practices SET archived = 1 "
        "WHERE archived = 0 AND slug NOT IN (%s) "
        "AND (steps_json IN ('', '[]') OR levels_json IN ('', '[]'))" % (",".join("?" * len(slugs))),
        slugs,
    )
    return inserted


def _practice_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM practices WHERE slug = ?", (str(slug or "").strip(),)
    ).fetchone()


def get_practice(conn: sqlite3.Connection, slug: str, *, today: Any = None) -> dict[str, Any] | None:
    """Одна практика с моими цифрами и последними сессиями (``None`` — нет такой)."""
    row = _practice_by_slug(conn, slug)
    if row is None:
        return None
    day = _now(today).date()
    stats = _practice_stats(conn, int(row["id"]), day)
    practice = _practice_row(row)
    practice["stats"] = stats
    practice["sessions"] = _recent_sessions(conn, int(row["id"]), limit=10)
    practice["recordUpdatedAt"] = _record_date(conn, int(row["id"]))
    return practice


def list_practices(
    conn: sqlite3.Connection,
    *,
    today: Any = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Каталог с личными цифрами: сколько раз, серия, рекорд, очки."""
    day = _now(today).date()
    where = "" if include_archived else " WHERE archived = 0"
    rows = conn.execute(
        "SELECT * FROM practices%s ORDER BY sort, id" % where
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        practice = _practice_row(row)
        practice["stats"] = _practice_stats(conn, int(row["id"]), day)
        practice["recordUpdatedAt"] = _record_date(conn, int(row["id"]))
        result.append(practice)
    return result


def practice_id_for(conn: sqlite3.Connection, slug: str) -> int | None:
    """id практики по слагу (``None``, если такой нет)."""
    row = _practice_by_slug(conn, slug)
    return int(row["id"]) if row is not None else None


# --------------------------------------------------------------------------
# Личные цифры: сессии, рекорд, серия, очки практики
# --------------------------------------------------------------------------


def _sessions_of(conn: sqlite3.Connection, practice_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM practice_sessions WHERE practice_id = ? "
        "ORDER BY date DESC, id DESC",
        (practice_id,),
    ).fetchall()


def _practice_stats(
    conn: sqlite3.Connection, practice_id: int, today: date_cls
) -> dict[str, Any]:
    """Сколько раз, когда последний раз, серия дней, рекорд, очки практики."""
    sessions = _sessions_of(conn, practice_id)
    dates = [str(row["date"]) for row in sessions]
    done_dates = [str(row["date"]) for row in sessions if int(row["rounds_done"] or 0) > 0]
    best_hold = max([int(row["best_hold_s"] or 0) for row in sessions] or [0])
    best_rounds = max([int(row["rounds_done"] or 0) for row in sessions] or [0])
    points = conn.execute(
        "SELECT COALESCE(SUM(points), 0) FROM points_ledger "
        "WHERE (kind = 'practice' AND ref_id LIKE ?) "
        "   OR (kind = 'record' AND ref_id LIKE ?)",
        ("practice:%d:%%" % practice_id, "record:%d:%%" % practice_id),
    ).fetchone()[0]
    return {
        "sessions": len(sessions),
        "days": len(set(dates)),
        "lastDate": dates[0] if dates else None,
        "streakDays": _streak_from_dates(done_dates, today)[0],
        "bestHoldS": best_hold,
        "bestHoldLabel": format_hold(best_hold),
        "bestRounds": best_rounds,
        "points": int(points or 0),
    }


def _recent_sessions(
    conn: sqlite3.Connection, practice_id: int, *, limit: int = 10
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM practice_sessions WHERE practice_id = ? "
        "ORDER BY date DESC, id DESC LIMIT ?",
        (practice_id, max(1, int(limit))),
    ).fetchall()
    return [_session_row(row) for row in rows]


def _session_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "date": str(row["date"]),
        "ts": str(row["ts"] or ""),
        "practiceId": int(row["practice_id"]),
        "startedAt": str(row["started_ts"] or ""),
        "finishedAt": str(row["finished_ts"] or ""),
        "roundsDone": int(row["rounds_done"] or 0),
        "bestHoldS": int(row["best_hold_s"] or 0),
        "bestHoldLabel": format_hold(row["best_hold_s"]),
        "source": str(row["source"] or ""),
        "note": str(row["note"] or ""),
    }


def _record_date(conn: sqlite3.Connection, practice_id: int) -> str | None:
    row = conn.execute(
        "SELECT date FROM practice_sessions WHERE practice_id = ? "
        "ORDER BY best_hold_s DESC, date ASC LIMIT 1",
        (practice_id,),
    ).fetchone()
    return str(row["date"]) if row is not None else None


def _streak_from_dates(dates: Iterable[str], today: date_cls) -> tuple[int, int]:
    """Текущая и лучшая серия по датам сессий.

    Текущая считается по дням подряд, заканчиваясь сегодня или вчера: пока
    сегодняшняя практика не сделана, серия ещё жива (не обрывается в полночь).
    """
    days = sorted({day for day in dates if day})
    if not days:
        return 0, 0
    parsed = {date_cls.fromisoformat(day) for day in days}
    best = 1
    run = 1
    ordered = sorted(parsed)
    for previous, current in zip(ordered, ordered[1:]):
        run = run + 1 if (current - previous) == timedelta(days=1) else 1
        best = max(best, run)
    anchor = today
    if anchor not in parsed:
        anchor = today - timedelta(days=1)
        if anchor not in parsed:
            return 0, best
    current_streak = 0
    cursor = anchor
    while cursor in parsed:
        current_streak += 1
        cursor -= timedelta(days=1)
    return current_streak, best


def practice_streak(conn: sqlite3.Connection, today: Any = None) -> dict[str, Any]:
    """Серия дней с практиками: текущая, лучшая и последняя дата."""
    day = _now(today).date()
    rows = conn.execute(
        "SELECT DISTINCT date FROM practice_sessions WHERE rounds_done > 0"
    ).fetchall()
    dates = [str(row[0]) for row in rows]
    current, best = _streak_from_dates(dates, day)
    last = max(dates) if dates else None
    return {"days": current, "best": best, "lastDate": last, "alive": current > 0}


# --------------------------------------------------------------------------
# Журнал начислений
# --------------------------------------------------------------------------


def total_points(conn: sqlite3.Connection) -> int:
    """Баланс: сумма журнала, а не сохранённый счётчик."""
    row = conn.execute("SELECT COALESCE(SUM(points), 0) FROM points_ledger").fetchone()
    return int(row[0] or 0)


def day_points(conn: sqlite3.Connection, day: Any) -> int:
    """Сколько очков начислено за конкретный день."""
    row = conn.execute(
        "SELECT COALESCE(SUM(points), 0) FROM points_ledger WHERE date = ?",
        (_day(day) if not isinstance(day, str) else str(day),),
    ).fetchone()
    return int(row[0] or 0)


def award(
    conn: sqlite3.Connection,
    *,
    kind: str,
    ref_id: str,
    points: int | None = None,
    reason: str = "",
    source: str = "app",
    now: Any = None,
) -> dict[str, Any]:
    """Начисляет очки один раз на ``ref_id`` и возвращает результат.

    Порядок: посчитать, сколько ещё влезает в кап дня, вставить строку
    журнала одной инструкцией с ``ON CONFLICT (kind, ref_id) DO NOTHING``
    и проверить, что день не вышел за кап (на случай двух одновременных
    запросов — тогда лишняя строка удаляется). Никакого
    «прочитал-проверил-записал» здесь нет.
    """
    ledger_kind = str(kind or "").strip()
    if ledger_kind not in LEDGER_KINDS:
        raise ValueError("Неизвестный вид начисления: %r" % (kind,))
    ref = str(ref_id or "").strip()
    if not ref:
        raise ValueError("ref_id начисления не может быть пустым")
    if source not in ("app", "demo"):
        raise ValueError("Источник начисления: app или demo")
    moment = _now(now)
    day = moment.date().isoformat()
    requested = POINTS.get(ledger_kind, 0) if points is None else int(points)
    if requested < 0:
        raise ValueError("Очки не могут быть отрицательными")

    used = day_points(conn, day)
    room = max(0, DAY_CAP - used)
    amount = min(requested, room)
    result: dict[str, Any] = {
        "kind": ledger_kind,
        "refId": ref,
        "reason": str(reason or ""),
        "requested": requested,
        "awarded": 0,
        "duplicate": False,
        "capped": amount < requested,
        "dayPoints": used,
        "remaining": room,
        "total": total_points(conn),
    }
    if amount <= 0:
        return result
    cursor = conn.execute(
        "INSERT INTO points_ledger(ts, date, kind, ref_id, points, reason, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(kind, ref_id) DO NOTHING",
        (
            moment.isoformat(timespec="seconds"),
            day,
            ledger_kind,
            ref,
            amount,
            str(reason or ""),
            source,
        ),
    )
    if int(cursor.rowcount or 0) == 0:
        result["duplicate"] = True
        result["capped"] = False
        return result
    if day_points(conn, day) > DAY_CAP:  # гонка двух запросов: откатываем лишнее
        conn.execute("DELETE FROM points_ledger WHERE id = ?", (cursor.lastrowid,))
        result["capped"] = True
        return result
    result.update(
        {
            "awarded": amount,
            "dayPoints": used + amount,
            "remaining": max(0, room - amount),
            "total": total_points(conn),
        }
    )
    return result


# --------------------------------------------------------------------------
# Сессия практики
# --------------------------------------------------------------------------


def save_practice_session(
    conn: sqlite3.Connection,
    *,
    slug: str,
    rounds_done: int = 0,
    rounds_increment: bool = False,
    best_hold_s: int = 0,
    source: str = "web",
    note: str = "",
    started_ts: str = "",
    finished_ts: str = "",
    now: Any = None,
    points_source: str = "app",
) -> dict[str, Any]:
    """Сохраняет сессию практики, начисляет очки и возвращает итог для UI.

    Одна сессия на практику в день: повторный вызов обновляет максимумы
    раундов и задержки, но не плодит строк. Первая завершённая сессия дня
    даёт ``POINTS["practice"]`` (10). Если к уже записанной сессии добавляются
    новые круги (``rounds_increment=True`` — «ещё один круг сегодня»), каждый
    добавленный круг приносит ``points_per_round(slug)``; у каждого круга свой
    ``ref_id``, поэтому один и тот же круг не оплачивается дважды. Повтор с
    тем же числом кругов по-прежнему даёт 0 и ``duplicate``.

    ``KeyError`` — если практики с таким слагом нет (API отвечает 404).
    """
    row = _practice_by_slug(conn, slug)
    if row is None:
        raise KeyError(str(slug))
    practice_id = int(row["id"])
    title = str(row["title"])

    rounds = max(0, min(MAX_ROUNDS, int(rounds_done or 0)))
    hold = max(0, min(MAX_HOLD_S, int(best_hold_s or 0)))
    if source not in SESSION_SOURCES:
        raise ValueError("Источник сессии: web, telegram или demo")

    moment = _now(now)
    day = moment.date().isoformat()
    ts = moment.isoformat(timespec="seconds")

    previous = conn.execute(
        "SELECT rounds_done FROM practice_sessions WHERE practice_id = ? AND date = ?",
        (practice_id, day),
    ).fetchone()
    previous_rounds = int(previous["rounds_done"] or 0) if previous is not None else 0
    if rounds_increment and rounds > 0:
        # «Ещё один круг сегодня»: прибавляем к уже записанному, а не заменяем.
        rounds = min(MAX_ROUNDS, previous_rounds + rounds)

    previous_record = conn.execute(
        "SELECT COALESCE(MAX(best_hold_s), 0) FROM practice_sessions WHERE practice_id = ?",
        (practice_id,),
    ).fetchone()[0]
    previous_record = int(previous_record or 0)

    cursor = conn.execute(
        "INSERT INTO practice_sessions"
        "(date, ts, practice_id, started_ts, finished_ts, rounds_done, best_hold_s, source, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(practice_id, date) DO UPDATE SET "
        "  ts = excluded.ts, "
        "  rounds_done = MAX(practice_sessions.rounds_done, excluded.rounds_done), "
        "  best_hold_s = MAX(practice_sessions.best_hold_s, excluded.best_hold_s), "
        "  finished_ts = excluded.finished_ts, "
        "  source = excluded.source, "
        "  note = CASE WHEN excluded.note <> '' THEN excluded.note "
        "              ELSE practice_sessions.note END",
        (
            day,
            ts,
            practice_id,
            str(started_ts or ""),
            str(finished_ts or "") or ts,
            rounds,
            hold,
            source,
            str(note or "")[:500],
        ),
    )
    session_id = int(cursor.lastrowid or 0)
    if not session_id:  # ветка UPDATE: id берём запросом
        found = conn.execute(
            "SELECT id FROM practice_sessions WHERE practice_id = ? AND date = ?",
            (practice_id, day),
        ).fetchone()
        session_id = int(found[0]) if found is not None else 0
    stored = conn.execute(
        "SELECT * FROM practice_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    stored_rounds = int(stored["rounds_done"] or 0) if stored is not None else rounds
    added_rounds = max(0, stored_rounds - previous_rounds)

    record_updated = rounds > 0 and hold > previous_record
    # «Закончить» сразу после «Старта» — это не завершённая практика: очков нет,
    # иначе кап дня набирается шестью нажатиями «старт → финиш». Сессия при этом
    # сохраняется (история видна), а повтор с кругами в тот же день уже начислит.
    completed = rounds > 0
    awards: list[dict[str, Any]] = []
    primary = award(
        conn,
        kind="practice",
        ref_id="practice:%d:%s" % (practice_id, day),
        points=POINTS["practice"] if completed else 0,
        reason=(
            "Практика «%s»" % title
            if completed
            else "Нет ни одного круга — практика не засчитана"
        ),
        source=points_source,
        now=moment,
    )
    awards.append(primary)
    if completed and primary["duplicate"] and added_rounds > 0:
        # Сессия дня уже была — оплачиваем только новые круги. Свой ref_id на
        # каждый круг: повтор «того же» круга очков не даст, новый — да.
        rate = points_per_round(str(row["slug"]))
        for offset in range(added_rounds):
            round_no = previous_rounds + offset + 1
            awards.append(
                award(
                    conn,
                    kind="practice",
                    ref_id="practice:%d:%s:r%d" % (practice_id, day, round_no),
                    points=rate,
                    reason="Практика «%s» — круг %d" % (title, round_no),
                    source=points_source,
                    now=moment,
                )
            )
    if record_updated:
        awards.append(
            award(
                conn,
                kind="record",
                ref_id="record:%d:%s" % (practice_id, day),
                reason="Новый рекорд задержки — %s" % format_hold(hold),
                source=points_source,
                now=moment,
            )
        )
    streak = practice_streak(conn, moment.date())
    if completed and streak["days"] >= 2:
        awards.append(
            award(
                conn,
                kind="streak",
                ref_id="streak:%s" % day,
                points=min(STREAK_POINTS * int(streak["days"]), STREAK_BONUS_CAP),
                reason="Серия %s" % _plural_days(int(streak["days"])),
                source=points_source,
                now=moment,
            )
        )

    earned = sum(int(item["awarded"]) for item in awards)
    levels = load_levels(conn)
    total = total_points(conn)
    used = day_points(conn, day)
    return {
        "session": _session_row(stored) if stored is not None else None,
        "practiceId": practice_id,
        "slug": str(row["slug"]),
        "title": title,
        "pointsEarned": earned,
        "awards": [
            {
                "kind": item["kind"],
                "points": item["awarded"],
                "reason": item["reason"],
                "duplicate": item["duplicate"],
                "capped": item["capped"],
            }
            for item in awards
        ],
        "recordUpdated": record_updated,
        "previousRecordS": previous_record,
        "bestHoldS": int(stored["best_hold_s"] or 0) if stored is not None else hold,
        "bestHoldLabel": format_hold(
            int(stored["best_hold_s"] or 0) if stored is not None else hold
        ),
        "roundsDone": int(stored["rounds_done"] or 0) if stored is not None else rounds,
        "roundsToday": stored_rounds,
        "roundsAdded": added_rounds,
        "streak": streak,
        "total": total,
        "level": level_for(total, levels),
        "cap": {"day": DAY_CAP, "today": used, "remaining": max(0, DAY_CAP - used)},
    }


# --------------------------------------------------------------------------
# Уровни
# --------------------------------------------------------------------------


def _normalise_levels(value: Any) -> list[dict[str, Any]]:
    """Проверяет и нормализует пороги уровней (``ValueError`` при ошибке)."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError("Нужно минимум два уровня")
    if len(value) > 8:
        raise ValueError("Слишком много уровней: максимум 8")
    levels: list[dict[str, Any]] = []
    keys: set[str] = set()
    previous = -1
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError("Уровень %d: ожидался объект" % (index + 1))
        key = str(raw.get("key") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not key or not title:
            raise ValueError("Уровень %d: нужны key и title" % (index + 1))
        if key in keys:
            raise ValueError("Уровень %r повторяется" % key)
        try:
            minimum = int(raw.get("min"))
        except (TypeError, ValueError):
            raise ValueError("Уровень %r: min должен быть целым" % key) from None
        if minimum < 0:
            raise ValueError("Уровень %r: min не может быть отрицательным" % key)
        if minimum <= previous:
            raise ValueError("Пороги уровней должны возрастать: %r" % key)
        if index == 0 and minimum != 0:
            raise ValueError("Первый уровень должен начинаться с 0")
        keys.add(key)
        previous = minimum
        levels.append({"key": key, "title": title, "min": minimum})
    return levels


def load_levels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Пороги уровней из ``settings_kv``; битая настройка → дефолты."""
    raw = get_setting(conn, LEVELS_KEY)
    if not raw:
        return [dict(level) for level in DEFAULT_LEVELS]
    try:
        return _normalise_levels(json.loads(raw))
    except (TypeError, ValueError):
        return [dict(level) for level in DEFAULT_LEVELS]


def save_levels(conn: sqlite3.Connection, levels: Any) -> list[dict[str, Any]]:
    """Сохраняет пороги уровней в ``settings_kv`` и возвращает их."""
    normalised = _normalise_levels(levels)
    set_setting(conn, LEVELS_KEY, _dumps(normalised))
    return normalised


def level_for(total: Any, levels: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Уровень по сумме очков: текущий, следующий и прогресс до него."""
    items = [dict(level) for level in (levels or DEFAULT_LEVELS)]
    if not items:
        items = [dict(level) for level in DEFAULT_LEVELS]
    score = int(total or 0)
    current = items[0]
    index = 0
    for position, level in enumerate(items):
        if score >= int(level["min"]):
            current, index = level, position
    following = items[index + 1] if index + 1 < len(items) else None
    if following is None:
        progress = 1.0
        to_next = 0
    else:
        span = max(1, int(following["min"]) - int(current["min"]))
        progress = round(min(1.0, max(0.0, (score - int(current["min"])) / span)), 3)
        to_next = max(0, int(following["min"]) - score)
    return {
        "key": str(current["key"]),
        "title": str(current["title"]),
        "min": int(current["min"]),
        "next": (
            {"key": str(following["key"]), "title": str(following["title"]), "min": int(following["min"])}
            if following is not None
            else None
        ),
        "toNext": to_next,
        "progress": progress,
        "total": score,
    }


# --------------------------------------------------------------------------
# Сводка для экрана и бота
# --------------------------------------------------------------------------


def _ledger_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "ts": str(row["ts"] or ""),
        "date": str(row["date"]),
        "kind": str(row["kind"]),
        "refId": str(row["ref_id"]),
        "points": int(row["points"] or 0),
        "reason": str(row["reason"] or ""),
        "source": str(row["source"] or ""),
    }


#: Подписи видов начислений для сводки и бота.
KIND_LABELS: Mapping[str, str] = {
    "practice": "Практики",
    "record": "Рекорды",
    "streak": "Серии",
    "habit": "Привычки",
    "task": "Дела",
    "checkin": "Чек-ины",
    "journal": "Мысли",
}


def points_summary(
    conn: sqlite3.Connection, today: Any = None, *, days: int = 30, recent: int = 10
) -> dict[str, Any]:
    """Баланс, уровень, серия, история по дням и последние начисления."""
    moment = _now(today)
    day = moment.date()
    window = max(1, min(365, int(days)))
    start = day - timedelta(days=window - 1)

    rows = conn.execute(
        "SELECT date, kind, SUM(points) AS points FROM points_ledger "
        "WHERE date >= ? AND date <= ? GROUP BY date, kind ORDER BY date",
        (start.isoformat(), day.isoformat()),
    ).fetchall()
    by_day: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_day.setdefault(str(row["date"]), {})
        bucket[str(row["kind"])] = int(row["points"] or 0)
    history: list[dict[str, Any]] = []
    for offset in range(window):
        current = start + timedelta(days=offset)
        key = current.isoformat()
        by_kind = by_day.get(key, {})
        history.append(
            {
                "date": key,
                "points": sum(by_kind.values()),
                "byKind": {kind: by_kind.get(kind, 0) for kind in LEDGER_KINDS},
            }
        )

    totals = {
        str(row["kind"]): int(row["points"] or 0)
        for row in conn.execute(
            "SELECT kind, SUM(points) AS points FROM points_ledger GROUP BY kind"
        ).fetchall()
    }
    last = conn.execute(
        "SELECT * FROM points_ledger ORDER BY ts DESC, id DESC LIMIT ?",
        (max(0, int(recent)),),
    ).fetchall()
    levels = load_levels(conn)
    total = total_points(conn)
    today_points = day_points(conn, day.isoformat())
    return {
        "date": day.isoformat(),
        "balance": total,
        "level": level_for(total, levels),
        "levels": levels,
        "streak": practice_streak(conn, day),
        "cap": {
            "day": DAY_CAP,
            "today": today_points,
            "remaining": max(0, DAY_CAP - today_points),
        },
        "points": {
            "practice": POINTS["practice"],
            "record": POINTS["record"],
            "streak": STREAK_POINTS,
            "streakCap": STREAK_BONUS_CAP,
            "habit": POINTS["habit"],
            "task": POINTS["task"],
            "checkin": POINTS["checkin"],
            "journal": POINTS["journal"],
        },
        "days": history,
        "totalsByKind": {
            kind: int(totals.get(kind, 0)) for kind in LEDGER_KINDS
        },
        "labels": dict(KIND_LABELS),
        "recent": [_ledger_row(row) for row in last],
    }
