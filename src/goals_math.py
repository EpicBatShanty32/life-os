"""Единый расчёт прогресса целей Life OS.

Один и тот же расчёт используют все три места, где показывается прогресс:

* дашборд (``dashboard_data._goal_progress``) — доля 0..1 для полосы;
* команды бота (``scheduler._goal_progress``) — целые проценты 0..100;
* страница Obsidian ``Цели.md`` (``obsidian._goal_progress``) — строка
  ``0%`` / ``50%`` / ``100%``.

Раньше у каждого места была своя формула, и они расходились: дашборд показывал
0% для цели «вес 92 → 78» с неизвестной стартовой точкой, а бот — 94,7% только
потому, что текущее значение близко к цели (UX_REVIEW, P0-1). Теперь формула
одна.

Формулы (``start`` → ``target``):

* рост:  ``(current - start) / (target - start)``;
* снижение: ``(start - current) / (start - target)``.

Так получается 0% в начале и 100% при достижении цели; результат обрезается
до 0..1. Если ``start`` нет (старые цели, созданные до появления колонки),
для роста работает ``current / target``, а для снижения — 0%, пока
``current > target``: базовая точка неизвестна, и рисовать «почти 100%»
только потому, что значение близко к цели, было бы выдумкой. При
``current <= target`` цель считается достигнутой (100%).

Крайние случаи не делят на ноль: ``start == target`` откатывается на формулу
без старта; ``target == 0`` даёт 100% только если значение уже по нужную
сторону нуля; ``current``/``target`` не числа → 0.
"""

from __future__ import annotations

import math
import re

__all__ = ["goal_direction", "goal_start", "goal_fraction", "goal_percent"]

#: Статусы «цель достигнута»: прогресс 100% независимо от чисел.
_DONE_STATUSES = frozenset({
    "done", "completed", "complete", "выполнена", "выполнено", "готово",
    "завершена", "закрыта",
})

#: Направление цели: ``up`` — чем больше, тем лучше; ``down`` — наоборот.
_DOWN_DIRECTIONS = frozenset({
    "down", "вниз", "снижение", "уменьшение", "меньше", "похудение",
    "сбросить",
})
_UP_DIRECTIONS = frozenset({"up", "вверх", "рост", "увеличение", "больше"})

#: Цели, у которых рост значения — это плохо (вес, похудение). Нужно для
#: direction-логики прогресса: current/target для них даёт ложные 100%.
_DOWN_GOAL_RE = re.compile(r"вес|похуд|скинуть|сбросить", re.IGNORECASE)
#: Исключение: «набрать вес» — это рост, а не снижение.
_UP_GOAL_RE = re.compile(
    r"набрать\s+вес|набор\s+веса|прибавить\s+вес|поправиться", re.IGNORECASE
)

_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%")
_SPACES_RE = re.compile(r"[\s\u00a0\u202f]+")


def _num(value):
    """Число из значения цели: 7.5, "7,5 ч", "1 800", "$6,000", "60%" → float.

    Возвращает ``float`` или ``None``; bool, NaN и Infinity отбрасываются.
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
    if isinstance(value, (list, tuple, dict)):
        return float(len(value)) if value else None
    return None


def goal_direction(goal) -> str:
    """Направление цели: ``down`` (чем меньше, тем лучше) или ``up``.

    Если Storage отдал ``direction`` — уважаем его (``up``/``down`` и русские
    синонимы). Иначе весовые цели определяем по названию («вес», «похудеть»,
    «скинуть», «сбросить»): для них рост значения означает движение назад.
    """
    raw = goal.get("direction") if isinstance(goal, dict) else None
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in _DOWN_DIRECTIONS:
            return "down"
        if value in _UP_DIRECTIONS:
            return "up"
    title = str(goal.get("title") or "") if isinstance(goal, dict) else ""
    if _UP_GOAL_RE.search(title):
        return "up"
    return "down" if _DOWN_GOAL_RE.search(title) else "up"


def goal_start(goal):
    """Стартовое значение цели: ``start``/``start_value`` (или синонимы)."""
    if not isinstance(goal, dict):
        return None
    for field in ("start", "start_value", "initial", "baseline"):
        value = _num(goal.get(field))
        if value is not None:
            return value
    return None


def _done(goal) -> bool:
    status = str(goal.get("status") or "").strip().lower()
    return status in _DONE_STATUSES


def _direct_fraction(goal):
    """Прогресс, записанный прямо в поле ``progress`` (доля или проценты)."""
    direct = goal.get("progress")
    direct_num = _num(direct)
    if direct_num is None:
        return None
    if 0 <= direct_num <= 1:
        return round(float(direct_num), 3)
    if isinstance(direct, str) and "%" in direct and 0 <= direct_num <= 100:
        return round(direct_num / 100.0, 3)
    # progress=60 без знака «%» — это 60%, а не 60 раз.
    if 1 < direct_num <= 100:
        return round(direct_num / 100.0, 3)
    return None


def _percent_fraction(goal):
    """Прогресс, записанный как «60%» в current/note/target/progress."""
    if not isinstance(goal, dict):
        return None
    for field in ("current", "note", "target", "progress"):
        match = _PERCENT_RE.search(str(goal.get(field) or ""))
        if match:
            value = _num(match.group(1))
            if value is not None:
                return value / 100.0
    return None


def _has_progress(goal) -> bool:
    """Есть ли из чего считать прогресс (для текстового вывода процентов)."""
    if not isinstance(goal, dict):
        return False
    if _done(goal):
        return True
    if _direct_fraction(goal) is not None:
        return True
    target = _num(goal.get("target"))
    current = _num(goal.get("current"))
    if target is not None or current is not None:
        return True
    return _percent_fraction(goal) is not None


def goal_fraction(goal) -> float:
    """Прогресс цели 0..1 с учётом направления и стартовой точки.

    Возвращает округлённое до тысячных число в диапазоне 0..1; любые
    «странные» данные дают 0.0, а не исключение.
    """
    if not isinstance(goal, dict):
        return 0.0
    if _done(goal):
        return 1.0

    direct = _direct_fraction(goal)
    if direct is not None:
        return direct

    target = _num(goal.get("target"))
    current = _num(goal.get("current"))

    progress = None
    if target is not None and current is not None:
        direction = goal_direction(goal)
        start = goal_start(goal)
        if direction == "down":
            if start is not None and start > target:
                progress = (start - current) / (start - target)
            elif current <= target:
                progress = 1.0
            elif target > 0:
                # Без start базовая точка неизвестна: 0%, пока цель не
                # достигнута, вместо выдуманных «почти 100%».
                progress = 0.0
            elif target == 0:
                progress = 1.0 if current <= 0 else 0.0
            else:
                progress = 0.0
        else:
            if start is not None and target > start:
                progress = (current - start) / (target - start)
            elif target > 0:
                progress = current / target
            elif target == 0:
                # Нулевая цель: 100% только когда значение действительно 0;
                # иначе current/target не существует и 100% было бы ложью.
                progress = 1.0 if current == 0 else 0.0
            else:
                progress = 0.0

    if progress is None:
        progress = _percent_fraction(goal)
    if progress is None:
        return 0.0
    try:
        progress = float(progress)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(progress):
        return 0.0
    return round(min(1.0, max(0.0, progress)), 3)


def goal_percent(goal):
    """Целые проценты 0..100 или ``None``, если прогресса ещё нет.

    ``None`` — только когда у цели вообще нет ни чисел, ни процентов;
    цель с одним ``target`` показывает 0% (как и полоса на дашборде).
    """
    if not _has_progress(goal):
        return None
    return int(round(goal_fraction(goal) * 100))
