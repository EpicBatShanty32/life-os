"""Практики и очки в боте (задача t35).

Очки считает сервер (:mod:`app.server.practices`): журнал ``points_ledger``,
кап дня, серия и уровни. Бот не заводит своей арифметики — он показывает то
же, что ``/api/points``, и начисляет очки за привычку, закрытое дело, чек-ин и
мысль теми же функциями t33. Поэтому сумма в боте и в вебе всегда сходится:
источник правды один — общая база.

Что здесь есть:

* :func:`points_report` — текст ``/points``: баланс, уровень, серия, что дало
  очки сегодня (включая начисления из веба и напоминаний);
* :func:`answer` — короткие ответы на «что мне даёт очки» и «сколько до
  уровня»;
* :func:`award` — аккуратное начисление очков из бота: исключение не ломает
  ответ владельцу, начисление идемпотентно по ``ref_id``;
* :func:`record_practice` и :func:`practice_today` — практика по кнопке
  «Сделал» из напоминания и проверка «сегодня уже отмечено» (в вебе тоже).

Тон сухой: цифры и что с ними делать, без похвалы и соревнований на задержку.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, Mapping, Optional, Tuple

from app.server import db, practices

logger = logging.getLogger("lifeos.bot.points")

__all__ = [
    "answer",
    "award",
    "default_practice",
    "level_line",
    "points_report",
    "practice_reply",
    "practice_today",
    "record_practice",
]

#: Практика, которую предлагает напоминание по умолчанию.
REMINDER_SLUG = "wim-hof"
#: Сколько кругов записывает кнопка «Сделал», если у практики нет уровней.
DEFAULT_ROUNDS = 3
#: Виды начислений в человеческом порядке (подписи — из t33).
KIND_ORDER: Tuple[str, ...] = ("practice", "record", "streak", "habit", "task", "checkin", "journal")


# --------------------------------------------------------------------------
# Мелочи: время, склонения, подготовка схемы
# --------------------------------------------------------------------------


def _now(settings: Any) -> datetime:
    try:
        return settings.now()
    except Exception:  # pragma: no cover - настройки всегда есть у бота
        return datetime.now().astimezone()


def _today(settings: Any) -> date:
    try:
        return date.fromisoformat(str(settings.today()))
    except Exception:  # pragma: no cover
        return _now(settings).date()


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


def _prepare(conn: Any) -> None:
    """Каталог и журнал очков на месте — даже если база старая.

    ``ensure_practices`` идемпотентна: на актуальной базе это два дешёвых
    ``CREATE IF NOT EXISTS``. Ошибку не пробрасываем: показ очков не должен
    падать из-за занятой базы — вызывающий получит честный текст.
    """
    try:
        db.ensure_practices(conn)
        practices.ensure_practice_catalog(conn)
    except Exception:  # pragma: no cover - БД занята чужим процессом
        logger.exception("не удалось подготовить таблицы практик и очков")


def _summary(conn: Any, settings: Any) -> Dict[str, Any]:
    _prepare(conn)
    return practices.points_summary(conn, _today(settings), days=30, recent=8)


# --------------------------------------------------------------------------
# Начисления из бота: привычка, дело, чек-ин, мысль
# --------------------------------------------------------------------------


def award(
    conn: Any,
    kind: str,
    ref_id: str,
    reason: str = "",
    now: Optional[datetime] = None,
) -> int:
    """Начисляет очки по правилам t33 и возвращает сколько дали.

    Один ``ref_id`` — одно начисление (уникальный индекс), кап дня считает
    сервер. Любая ошибка только пишется в лог: запись владельца важнее очков.
    """
    try:
        _prepare(conn)
        result = practices.award(
            conn,
            kind=kind,
            ref_id=ref_id,
            reason=reason,
            now=now,
        )
        return int(result.get("awarded") or 0)
    except Exception:  # pragma: no cover - очки не должны ломать запись
        logger.exception("не удалось начислить очки %r (%s)", ref_id, kind)
        return 0


# --------------------------------------------------------------------------
# Тексты: /points и ответы на вопросы
# --------------------------------------------------------------------------


def level_line(level: Mapping[str, Any]) -> str:
    """«Уровень: Устойчивый» + сколько до следующего, одной строкой."""
    title = str(level.get("title") or "Новичок")
    following = level.get("next") if isinstance(level.get("next"), Mapping) else None
    if following is None:
        return "Уровень: %s (высший)." % title
    return "Уровень: %s, до «%s» — %d." % (
        title,
        str(following.get("title") or ""),
        int(level.get("toNext") or 0),
    )


def _today_parts(summary: Mapping[str, Any]) -> Tuple[int, str]:
    """Сколько очков за сегодня и из чего они сложились (веб тоже считается)."""
    history = summary.get("days") if isinstance(summary.get("days"), list) else []
    today_row = history[-1] if history else {}
    by_kind = today_row.get("byKind") if isinstance(today_row, Mapping) else {}
    by_kind = by_kind if isinstance(by_kind, Mapping) else {}
    labels = summary.get("labels") if isinstance(summary.get("labels"), Mapping) else {}
    parts = []
    for kind in KIND_ORDER:
        points = int(by_kind.get(kind) or 0)
        if not points:
            continue
        label = str(labels.get(kind) or kind).lower()
        parts.append("%s +%d" % (label, points))
    total = int(today_row.get("points") or 0) if isinstance(today_row, Mapping) else 0
    cap = summary.get("cap") if isinstance(summary.get("cap"), Mapping) else {}
    total = max(total, int(cap.get("today") or 0))
    return total, ", ".join(parts)


def points_report(conn: Any, settings: Any) -> str:
    """``/points``: баланс, уровень, серия и что дало очки сегодня."""
    try:
        summary = _summary(conn, settings)
    except Exception as exc:
        logger.exception("не удалось посчитать очки")
        return "Не смог посчитать очки: %s." % (str(exc)[:80] or "ошибка базы")
    balance = int(summary.get("balance") or 0)
    level = summary.get("level") if isinstance(summary.get("level"), Mapping) else {}
    streak = summary.get("streak") if isinstance(summary.get("streak"), Mapping) else {}
    days = int(streak.get("days") or 0)
    best = int(streak.get("best") or 0)
    lines = [
        "Очки: %d. %s" % (balance, level_line(level)),
        "Серия практик: %d %s%s."
        % (
            days,
            _plural(days, "день", "дня", "дней"),
            "" if not best or best <= days else " (лучшая %d)" % best,
        ),
    ]
    cap = summary.get("cap") if isinstance(summary.get("cap"), Mapping) else {}
    cap_day = int(cap.get("day") or practices.DAY_CAP)
    total_today, parts = _today_parts(summary)
    if total_today:
        lines.append(
            "Сегодня +%d из %d: %s."
            % (total_today, cap_day, parts or "начисления")
        )
    else:
        lines.append("Сегодня 0 из %d." % cap_day)
    return "\n".join(lines)


def _rules_text(summary: Mapping[str, Any]) -> str:
    """Таблица начислений одной строкой — из тех же констант, что и сервер."""
    rules = summary.get("points") if isinstance(summary.get("points"), Mapping) else {}
    values = {kind: int(rules.get(kind) or 0) for kind in KIND_ORDER}
    streak_cap = int(rules.get("streakCap") or practices.STREAK_BONUS_CAP)
    return (
        "Очки: практика +%d, рекорд задержки +%d, серия +%d за день (не больше +%d), "
        "привычка +%d, дело +%d, чек-ин +%d, мысль +%d. Кап дня — %d."
        % (
            values["practice"],
            values["record"],
            values["streak"],
            streak_cap,
            values["habit"],
            values["task"],
            values["checkin"],
            values["journal"],
            practices.DAY_CAP,
        )
    )


def answer(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool = False) -> str:
    """Короткий ответ на вопрос об очках: что даёт, сколько до уровня, итог."""
    kind = str(data.get("query_kind") or "").strip()
    try:
        summary = _summary(conn, settings)
    except Exception:
        logger.exception("не удалось посчитать очки для ответа")
        return "Не смог посчитать очки — попробуй ещё раз или /points."
    if kind == "points_rules":
        return _rules_text(summary)
    if kind == "points_level":
        level = summary.get("level") if isinstance(summary.get("level"), Mapping) else {}
        following = level.get("next") if isinstance(level.get("next"), Mapping) else None
        total = int(summary.get("balance") or 0)
        if following is None:
            return "«%s» — высший уровень, %d очков. Дальше только серия." % (
                str(level.get("title") or "Мастер"),
                total,
            )
        return "До «%s» — %d очков (сейчас %d)." % (
            str(following.get("title") or ""),
            int(level.get("toNext") or 0),
            total,
        )
    return points_report(conn, settings)


# --------------------------------------------------------------------------
# Практика: напоминание, кнопка «Сделал», уже отмечено сегодня
# --------------------------------------------------------------------------


def default_practice(conn: Any) -> Optional[Dict[str, Any]]:
    """Практика для напоминания и кнопки «Сделал»: Вим Хоф, иначе первая."""
    _prepare(conn)
    row = conn.execute(
        "SELECT slug, title FROM practices WHERE archived = 0 "
        "ORDER BY CASE WHEN slug = ? THEN 0 ELSE 1 END, sort, id LIMIT 1",
        (REMINDER_SLUG,),
    ).fetchone()
    if row is None:
        return None
    slug = str(row["slug"])
    rounds = DEFAULT_ROUNDS
    try:
        practice = practices.get_practice(conn, slug)
    except Exception:  # pragma: no cover
        practice = None
    if isinstance(practice, Mapping):
        levels = practice.get("levels") if isinstance(practice.get("levels"), list) else []
        default_key = str(practice.get("defaultLevel") or "")
        for level in levels:
            if not isinstance(level, Mapping):
                continue
            if default_key and str(level.get("key") or "") != default_key:
                continue
            try:
                rounds = int(level.get("rounds") or rounds)
            except (TypeError, ValueError):
                rounds = DEFAULT_ROUNDS
            break
    return {"slug": slug, "title": str(row["title"]), "rounds": max(1, rounds)}


def practice_today(conn: Any, settings: Any) -> Optional[Dict[str, Any]]:
    """Сессия любой практики за сегодня: бот не напоминает, если уже сделано."""
    _prepare(conn)
    day = _today(settings).isoformat()
    row = conn.execute(
        "SELECT ps.rounds_done, ps.source, ps.best_hold_s, p.title "
        "FROM practice_sessions ps JOIN practices p ON p.id = ps.practice_id "
        "WHERE ps.date = ? AND ps.rounds_done > 0 ORDER BY ps.id DESC LIMIT 1",
        (day,),
    ).fetchone()
    if row is None:
        return None
    return {
        "title": str(row["title"]),
        "rounds": int(row["rounds_done"] or 0),
        "bestHoldS": int(row["best_hold_s"] or 0),
        "source": str(row["source"] or ""),
    }


def practice_reply(result: Mapping[str, Any], title: str, rounds: int) -> str:
    """Одна строка про практику: очки, серия, всего.

    Повтор без новых кругов очков не даёт — так и говорим, без упрёка.
    """
    earned = int(result.get("pointsEarned") or 0)
    total = int(result.get("total") or 0)
    streak = result.get("streak") if isinstance(result.get("streak"), Mapping) else {}
    days = int(streak.get("days") or 0)
    session = result.get("session") if isinstance(result.get("session"), Mapping) else {}
    source = str(session.get("source") or "")
    done_rounds = int(result.get("roundsDone") or rounds or 0)
    rounds_text = "%d %s" % (done_rounds, _plural(done_rounds, "круг", "круга", "кругов"))
    streak_text = "%d %s" % (days, _plural(days, "день", "дня", "дней"))
    if earned > 0:
        return "Записал: %s, %s. +%d, серия %s, всего %d." % (
            title,
            rounds_text,
            earned,
            streak_text,
            total,
        )
    awards = result.get("awards") if isinstance(result.get("awards"), list) else []
    capped = any(
        isinstance(item, Mapping) and item.get("capped") and int(item.get("points") or 0) > 0
        for item in awards
    )
    if capped:
        reason = "дневной кап набран, очков не начисляю"
    elif source == "web":
        reason = "уже отмечено сегодня в вебе, второй раз не начисляю"
    else:
        reason = "уже отмечено сегодня, второй раз не начисляю"
    return "%s — %s, %s; серия %s, всего %d." % (
        title,
        rounds_text,
        reason,
        streak_text,
        total,
    )


def record_practice(
    conn: Any,
    settings: Any,
    data: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    """Кнопка «Сделал» в напоминании: сессия практики и ответ с очками."""
    data = data if isinstance(data, Mapping) else {}
    practice = default_practice(conn)
    if practice is None:
        return "Практик в каталоге нет — запиши фразой «подышал 3 раунда»."
    slug = str(data.get("slug") or practice["slug"])
    rounds = int(data.get("rounds") or practice["rounds"] or DEFAULT_ROUNDS)
    moment = now or _now(settings)
    try:
        result = practices.save_practice_session(
            conn,
            slug=slug,
            rounds_done=rounds,
            source="telegram",
            now=moment,
        )
    except KeyError:
        return "Практика «%s» не нашлась в каталоге — запиши фразой." % slug
    except Exception as exc:  # практика не должна ломать нажатие кнопки
        logger.exception("не удалось записать практику по кнопке")
        return "Не смог записать практику: %s." % (str(exc)[:80] or "ошибка базы")
    title = str(result.get("title") or practice["title"])
    return practice_reply(result, title, rounds)
