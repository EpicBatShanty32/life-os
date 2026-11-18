"""Запись сущностей v4 из Telegram-бота в общую SQLite-базу.

Один разобранный интент (:mod:`app.bot.intent_v4`) превращается ровно в одну
запись — дело, транзакцию, книгу/цитату, мысль, привычку, чек-ин, прогресс
цели или правку последней записи. Всё пишется в те же таблицы, что читают API
и веб (``tasks``, ``transactions``, ``books``, ``entries``, ``checkins``,
``habit_marks``, ``goals``), поэтому запись сразу видна на экранах.

Два служебных ключа живут в ``settings_kv``:

* ``bot:last_record`` — последняя запись бота: описание и список операций для
  отмены. По нему работают кнопки «Изменить/Удалить» и фраза «удали последнее»;
* ``bot:pending_delete`` — снимок последней записи на время подтверждения.

Ничего не теряется: если бот не смог отнести фразу к сущности, она уходит в
``inbox_items`` с исходным текстом (см. :func:`save_inbox`).

Все функции пишут через переданное соединение и **не коммитят**: вызывающий
код оборачивает вызовы в ``with conn:``. Возвращают :class:`Outcome` —
готовую строку подтверждения и операции для отмены.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.bot import intent_v4, points_v4
from app.server import db, practices

logger = logging.getLogger(__name__)

__all__ = [
    "LAST_KEY",
    "Outcome",
    "PENDING_KEY",
    "apply_intent",
    "clear_pending",
    "delete_record",
    "finance_summary",
    "last_record",
    "load_pending",
    "remember_last",
    "save_inbox",
    "save_pending",
    "today_summary",
    "week_summary",
]

LAST_KEY = "bot:last_record"
PENDING_KEY = "bot:pending_delete"

#: Статьи на случай, когда словарь не угадал.
FALLBACK_EXPENSE_CATEGORY = "Разное"
FALLBACK_INCOME_CATEGORY = "Прочие доходы"
DEFAULT_CATEGORY_COLOR = "#A18BB7"

RESOURCE_LABELS = {"must": "обязательно", "important": "важно", "low": "низкий приоритет"}
PERIOD_LABELS = {"day": "за сегодня", "week": "за неделю", "month": "за месяц"}


@dataclass
class Outcome:
    """Результат записи: строка подтверждения и операции для отмены."""

    text: str
    operations: List[Dict[str, Any]] = field(default_factory=list)
    label: str = ""
    edit_hint: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.text


#: Последний вопрос и режим ответа — для кнопок «Ещё»/«Кратко».
ASK_KEY = "bot:last_ask"


@dataclass
class AskAnswer:
    """Ответ на вопрос о данных: короткий текст, экран веба и расшифровка."""

    text: str
    web_route: str = "/"
    detail: str = ""
    label: str = "ответ"


# --------------------------------------------------------------------------
# Время и мелочи
# --------------------------------------------------------------------------


def _now(settings: Any) -> datetime:
    try:
        return settings.now()
    except Exception:  # pragma: no cover - настройки всегда есть у бота
        return datetime.now().astimezone()


def _ts(settings: Any) -> str:
    return _now(settings).isoformat(timespec="seconds")


def _today(settings: Any) -> str:
    try:
        return str(settings.today())
    except Exception:  # pragma: no cover
        return _now(settings).date().isoformat()


def _stamp() -> str:
    """UTC-метка ``created`` как у сидера и API."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tz(settings: Any) -> Any:
    try:
        return settings.tz
    except Exception:  # pragma: no cover - настройки всегда есть у бота
        return timezone.utc


def _short(text: Any, limit: int = 120) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: max(1, limit - 1)].rstrip() + "…"


def _fmt_number(value: Any, digits: int = 1) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = "%.*f" % (digits, number)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def _fmt_date(iso: Optional[str], settings: Any) -> str:
    try:
        today = date.fromisoformat(_today(settings))
    except ValueError:  # pragma: no cover
        today = date.today()
    return intent_v4.format_date_short(iso, today)


def _rub(kopecks: Any) -> str:
    return intent_v4.format_money(int(kopecks or 0))


def _join(parts: Sequence[str]) -> str:
    return " · ".join(part for part in parts if part)


# --------------------------------------------------------------------------
# Справочники: сферы, проекты, категории, счета, книги, привычки
# --------------------------------------------------------------------------


def _sphere_rows(conn: Any) -> List[Any]:
    return list(
        conn.execute(
            "SELECT id, name FROM spheres WHERE archived = 0 ORDER BY sort, id"
        ).fetchall()
    )


def resolve_sphere(conn: Any, text: str) -> Optional[int]:
    """Сфера по словам; недостающую каноническую сферу создаёт."""
    rows = _sphere_rows(conn)
    lowered = str(text or "").casefold()
    for row in rows:
        name = str(row["name"])
        if name.casefold() in lowered:
            return int(row["id"])
    canonical = intent_v4.infer_sphere(text)
    if canonical:
        keywords = intent_v4.SPHERE_KEYWORDS.get(canonical, ())
        for row in rows:
            name = str(row["name"]).casefold()
            if canonical.casefold() in name or any(word in name for word in keywords):
                return int(row["id"])
        if not rows:
            return _create_sphere(conn, canonical)
        # Пользовательские сферы не совпали по словам — создаём каноническую,
        # чтобы у дела была сфера, а не пустое поле.
        return _create_sphere(conn, canonical)
    return int(rows[0]["id"]) if rows and not rows else None


def _create_sphere(conn: Any, name: str) -> int:
    color, icon = "#A18BB7", ""
    for canonical, canonical_color, canonical_icon in intent_v4.DEFAULT_SPHERES:
        if canonical == name:
            color, icon = canonical_color, canonical_icon
            break
    sort_row = conn.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM spheres").fetchone()
    cursor = conn.execute(
        "INSERT INTO spheres (name, color, icon, sort, archived, created) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (name[:80], color, icon, int(sort_row[0] if sort_row else 0), _stamp()),
    )
    return int(cursor.lastrowid or 0)


def _sphere_name(conn: Any, sphere_id: Optional[int]) -> str:
    if not sphere_id:
        return ""
    row = conn.execute("SELECT name FROM spheres WHERE id = ?", (int(sphere_id),)).fetchone()
    return str(row[0]) if row is not None else ""


def resolve_project(conn: Any, text: str, query: str = "") -> Optional[int]:
    """Проект по имени в тексте или по подсказке; не создаёт новые."""
    rows = conn.execute(
        "SELECT id, name FROM projects WHERE status <> 'done' ORDER BY id DESC"
    ).fetchall()
    body = "%s %s" % (text or "", query or "")
    lowered = body.casefold()
    needle = str(query or "").casefold().strip()
    for row in rows:
        name = str(row["name"])
        folded = name.casefold()
        if needle and (needle in folded or folded in needle):
            return int(row["id"])
        if folded and folded in lowered:
            return int(row["id"])
    return None


def _project_name(conn: Any, project_id: Optional[int]) -> str:
    if not project_id:
        return ""
    row = conn.execute("SELECT name FROM projects WHERE id = ?", (int(project_id),)).fetchone()
    return str(row[0]) if row is not None else ""


def _category_rows(conn: Any, kind: str) -> List[Any]:
    return list(
        conn.execute(
            "SELECT id, name FROM finance_categories "
            "WHERE archived = 0 AND kind = ? ORDER BY sort, id",
            (kind,),
        ).fetchall()
    )


def resolve_category(conn: Any, kind: str, wanted: str = "", text: str = "") -> Tuple[int, str]:
    """Статья по имени/словам; недостающую создаёт. Возвращает ``(id, имя)``."""
    rows = _category_rows(conn, kind)
    candidates = [str(wanted or "").strip(), intent_v4.infer_category(text or wanted, kind)]
    for candidate in candidates:
        if not candidate:
            continue
        folded = candidate.casefold()
        for row in rows:
            name = str(row["name"]).casefold()
            if folded == name or folded in name or name in folded:
                return int(row["id"]), str(row["name"])
    fallback = wanted.strip() or (
        FALLBACK_INCOME_CATEGORY if kind == "income" else FALLBACK_EXPENSE_CATEGORY
    )
    name = fallback[:80]
    sort_row = conn.execute(
        "SELECT COALESCE(MAX(sort), -1) + 1 FROM finance_categories WHERE kind = ?", (kind,)
    ).fetchone()
    cursor = conn.execute(
        "INSERT INTO finance_categories (name, kind, color, sort, archived) "
        "VALUES (?, ?, ?, ?, 0)",
        (name, kind, DEFAULT_CATEGORY_COLOR, int(sort_row[0] if sort_row else 0)),
    )
    return int(cursor.lastrowid or 0), name


def resolve_account(conn: Any, hint: str = "") -> Tuple[int, str]:
    """Счёт по подсказке (cash/card/savings/broker); недостающий создаёт."""
    rows = conn.execute(
        "SELECT id, name, kind FROM finance_accounts WHERE archived = 0 ORDER BY sort, id"
    ).fetchall()
    if hint:
        for row in rows:
            if str(row["kind"]) == hint:
                return int(row["id"]), str(row["name"])
    for row in rows:
        if str(row["kind"]) == "card":
            return int(row["id"]), str(row["name"])
    if rows:
        return int(rows[0]["id"]), str(rows[0]["name"])
    name, kind = ("Наличные", "cash") if hint == "cash" else ("Карта", "card")
    cursor = conn.execute(
        "INSERT INTO finance_accounts (name, kind, currency, balance_start_kopecks, "
        "archived, sort) VALUES (?, ?, 'RUB', 0, 0, 0)",
        (name, kind),
    )
    return int(cursor.lastrowid or 0), name


def resolve_book(
    conn: Any, query: str = "", *, create: bool = False, pages_total: int = 0
) -> Tuple[Optional[Any], bool]:
    """Книга по названию; при ``create`` создаёт. Возвращает ``(row, created)``."""
    rows = conn.execute(
        "SELECT id, title, pages_total, pages_read, shelf, finished_at "
        "FROM books ORDER BY COALESCE(updated, created) DESC, id DESC"
    ).fetchall()
    needle = str(query or "").casefold().strip()
    if needle:
        for row in rows:
            title = str(row["title"]).casefold()
            if needle == title or needle in title or title in needle:
                return row, False
    if not create:
        return None, False
    title = (str(query or "").strip() or "Без книги")[:300]
    cursor = conn.execute(
        "INSERT INTO books (title, category, pages_total, pages_read, shelf, created, updated) "
        "VALUES (?, 'nonfiction', ?, 0, 'reading', ?, ?)",
        (title, max(0, int(pages_total or 0)), _stamp(), _stamp()),
    )
    row = conn.execute(
        "SELECT id, title, pages_total, pages_read, shelf, finished_at FROM books WHERE id = ?",
        (int(cursor.lastrowid or 0),),
    ).fetchone()
    return row, True


def _last_book(conn: Any) -> Optional[Any]:
    row = conn.execute(
        "SELECT id, title, pages_total, pages_read, shelf, finished_at FROM books "
        "WHERE shelf = 'reading' ORDER BY COALESCE(updated, created) DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id, title, pages_total, pages_read, shelf, finished_at FROM books "
            "ORDER BY COALESCE(updated, created) DESC, id DESC LIMIT 1"
        ).fetchone()
    return row


def resolve_habit(conn: Any, data: Mapping[str, Any], text: str) -> Tuple[int, str, bool]:
    """Привычка по названию/словам; недостающую создаёт. ``(id, name, created)``."""
    wanted = str(data.get("habit") or "").strip()
    rows = conn.execute(
        "SELECT id, name FROM habits WHERE archived = 0 ORDER BY id"
    ).fetchall()
    candidates = [wanted.casefold()]
    for keyword_map in _HABIT_ALIASES:
        if any(word in str(text or "").casefold() for word in keyword_map[1]):
            candidates.append(keyword_map[0].casefold())
    for candidate in candidates:
        if not candidate:
            continue
        for row in rows:
            name = str(row["name"]).casefold()
            if candidate == name or candidate in name or name in candidate:
                return int(row["id"]), str(row["name"]), False
    name = (wanted or _habit_name_from_text(text) or "Привычка")[:80]
    cursor = conn.execute(
        "INSERT INTO habits (name, emoji, target_per_week, created) VALUES (?, ?, 7, ?)",
        (name, str(data.get("emoji") or "")[:8], _stamp()),
    )
    return int(cursor.lastrowid or 0), name, True


#: (имя привычки, слова-маркеры) — порядок важен.
_HABIT_ALIASES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Вода 2 литра", ("выпил", "вода", "воды", "литр")),
    ("Бег", ("пробеж", "бег", "кросс")),
    ("Прогулка 8 тысяч шагов", ("прогул", "шаг", "прошёл", "прошел")),
    ("Чтение 20 минут", ("читал", "чтение", "почитал")),
    ("Растяжка перед сном", ("растяжк", "потянулся", "потянулась")),
    ("Не курить", ("курил", "сигарет")),
    ("Без алкоголя", ("алкогол", "пил")),
)


def _habit_name_from_text(text: str) -> str:
    body = " ".join(str(text or "").split()).strip(" .,!?:;")
    return body[:60]


# --------------------------------------------------------------------------
# Дела
# --------------------------------------------------------------------------


def create_task(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """Новое дело: дата/время, сфера, проект, ресурс, оценка минут."""
    title = _short(data.get("title") or data.get("text") or "Дело", 200)
    text = str(data.get("text") or title)
    sphere_id = resolve_sphere(conn, "%s %s" % (title, text))
    project_id = resolve_project(conn, "%s %s" % (title, text), str(data.get("project") or ""))
    due = str(data.get("due_date") or _today(settings))
    start_time = str(data.get("start_time") or "")
    start_at = "%sT%s:00" % (due, start_time) if start_time else None
    resource = str(data.get("resource") or "important")
    if resource not in RESOURCE_LABELS:
        resource = "important"
    sphere_name = _sphere_name(conn, sphere_id)
    list_kind = str(data.get("list_kind") or ("personal" if sphere_name == "Личное и семья" else "work"))
    minutes = int(data.get("minutes") or 0)

    notes_parts: List[str] = []
    if text.casefold().strip() != title.casefold().strip():
        notes_parts.append("Из бота: " + text[:400])
    if minutes > 0:
        notes_parts.append("Оценка: %d мин" % minutes)
    cursor = conn.execute(
        "INSERT INTO tasks (title, sphere_id, project_id, list_kind, tags, due_date, "
        "start_at, end_at, resource, status, recurrence, notes, created) "
        "VALUES (?, ?, ?, ?, '', ?, ?, NULL, ?, 'todo', '', ?, ?)",
        (
            title,
            sphere_id,
            project_id,
            list_kind,
            due if due else None,
            start_at,
            resource,
            "; ".join(notes_parts),
            _stamp(),
        ),
    )
    task_id = int(cursor.lastrowid or 0)

    when = _fmt_date(due, settings)
    if start_time:
        when = "%s, %s" % (when, start_time)
    details = [when]
    if sphere_name:
        details.append(sphere_name)
    project_name = _project_name(conn, project_id)
    if project_name:
        details.append("проект «%s»" % project_name)
    details.append(RESOURCE_LABELS[resource])
    if minutes:
        details.append("~%d мин" % minutes)
    reply = "Записал дело: «%s» — %s" % (title, ", ".join(details))
    return Outcome(
        text=reply,
        operations=[{"type": "task", "id": task_id}],
        label="дело «%s»" % title,
        edit_hint="Перенести: «перенеси на завтра». В работу: «это в работу».",
    )


def _title_matches(rows: Sequence[Any], query: str) -> Optional[Any]:
    needle = str(query or "").casefold().strip()
    if not needle:
        return None
    best = None
    best_score = 0
    needle_tokens = set(re.findall(r"\w+", needle))
    for row in rows:
        title = str(row["title"]).casefold()
        if needle == title:
            return row
        if needle in title or title in needle:
            score = 2
        else:
            title_tokens = set(re.findall(r"\w+", title))
            score = len(needle_tokens & title_tokens)
        if score > best_score:
            best, best_score = row, score
    return best if best_score else None


def _open_tasks(conn: Any, limit: int = 100) -> List[Any]:
    return list(
        conn.execute(
            "SELECT id, title, due_date FROM tasks WHERE status = 'todo' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    )


def complete_task(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """Закрывает последнее подходящее открытое дело."""
    rows = _open_tasks(conn)
    if not rows:
        return Outcome("Открытых дел нет — закрывать нечего.")
    target = _title_matches(rows, str(data.get("title") or "")) or rows[0]
    task_id = int(target["id"])
    title = str(target["title"])
    with_status = conn.execute(
        "UPDATE tasks SET status = 'done', done_at = ? WHERE id = ?", (_ts(settings), task_id)
    )
    if with_status.rowcount == 0:  # pragma: no cover - гонка маловероятна
        return Outcome("Дело уже изменилось — пришли фразу ещё раз.")
    earned = points_v4.award(
        conn, "task", "task:%d" % task_id, "Дело «%s»" % title, now=_now(settings)
    )
    return Outcome(
        text="Закрыл дело: «%s»%s" % (title, _points_suffix(earned)),
        operations=[{"type": "task_status", "id": task_id, "prev_status": "todo", "prev_done_at": None}],
        label="дело «%s»" % title,
        edit_hint="Вернуть в работу — кнопкой «Удалить» (отменит закрытие).",
    )


def _last_task_row(conn: Any) -> Optional[Any]:
    record = last_record(conn)
    if record:
        for op in record.get("operations") or []:
            if op.get("type") in {"task", "task_status", "task_due", "task_list"}:
                row = conn.execute(
                    "SELECT id, title, due_date, status FROM tasks WHERE id = ?",
                    (int(op.get("id") or 0),),
                ).fetchone()
                if row is not None:
                    return row
    return conn.execute(
        "SELECT id, title, due_date, status FROM tasks WHERE status = 'todo' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


def reschedule_last(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """«перенеси на завтра» — сдвигает последнее дело."""
    task = _last_task_row(conn)
    if task is None:
        return Outcome("Не нашёл открытых дел — скажи, что перенести.")
    due = str(data.get("due_date") or "")
    if not due:
        due = (date.fromisoformat(_today(settings)) + timedelta(days=1)).isoformat()
    prev = task["due_date"]
    conn.execute("UPDATE tasks SET due_date = ? WHERE id = ?", (due, int(task["id"])))
    title = str(task["title"])
    return Outcome(
        text="«%s» — на %s" % (title, _fmt_date(due, settings)),
        operations=[{"type": "task_due", "id": int(task["id"]), "prev": prev}],
        label="перенос дела «%s»" % title,
        edit_hint="Скажи «перенеси на послезавтра», если дата другая.",
    )


def move_task_list(conn: Any, data: Mapping[str, Any], list_kind: str) -> Outcome:
    """«это в работу» / «это в личное» — меняет список последнего дела."""
    task = _last_task_row(conn)
    if task is None:
        return Outcome("Не нашёл открытых дел — скажи, что перенести в список.")
    prev_row = conn.execute(
        "SELECT list_kind FROM tasks WHERE id = ?", (int(task["id"]),)
    ).fetchone()
    prev = str(prev_row[0]) if prev_row is not None else "work"
    conn.execute(
        "UPDATE tasks SET list_kind = ? WHERE id = ?", (list_kind, int(task["id"]))
    )
    title = str(task["title"])
    where = "в работе" if list_kind == "work" else "в личном"
    return Outcome(
        text="«%s» — %s" % (title, where),
        operations=[{"type": "task_list", "id": int(task["id"]), "prev": prev}],
        label="список дела «%s»" % title,
    )


# --------------------------------------------------------------------------
# Цели
# --------------------------------------------------------------------------


def _goal_rows(conn: Any) -> List[Any]:
    return list(
        conn.execute(
            "SELECT g.id, g.title, g.target, g.current, g.unit, g.progress, "
            " g.sphere_id, s.name AS sphere_name "
            "FROM goals AS g LEFT JOIN spheres AS s ON s.id = g.sphere_id "
            "WHERE g.status = 'active' ORDER BY g.id DESC"
        ).fetchall()
    )


def _find_goal(conn: Any, query: str = "") -> Optional[Any]:
    rows = _goal_rows(conn)
    needle = str(query or "").casefold().strip()
    if not needle:
        return rows[0] if rows else None
    for row in rows:
        if str(row["title"]).casefold() == needle:
            return row
    for row in rows:
        title = str(row["title"]).casefold()
        if needle in title or title in needle:
            return row
    for row in rows:
        sphere = str(row["sphere_name"] or "")
        if sphere and needle in sphere.casefold():
            return row
    canonical = intent_v4.infer_sphere(needle)
    if canonical:
        for row in rows:
            sphere = str(row["sphere_name"] or "")
            if sphere and canonical.casefold() in sphere.casefold():
                return row
    return None


def add_goal_progress(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """«+10% к цели Здоровье» — прогресс цели (при отсутствии — создаёт её)."""
    query = str(data.get("goal") or "").strip()
    delta = int(data.get("delta") or 0)
    mode = str(data.get("mode") or "delta")
    if not delta:
        return Outcome("Не понял, на сколько двигать цель. Напиши «+10% к цели Здоровье».")
    goal = _find_goal(conn, query)
    if goal is None:
        title = query or "Цель"
        progress = max(0, min(100, delta)) if mode == "delta" else max(0, min(100, delta))
        cursor = conn.execute(
            "INSERT INTO goals (title, target, current, unit, horizon, progress, status, created) "
            "VALUES (?, 100, ?, '%', 'year', ?, 'active', ?)",
            (title[:200], progress, progress, _stamp()),
        )
        return Outcome(
            text="Цель «%s»: %d%%" % (title, progress),
            operations=[{"type": "goal", "id": int(cursor.lastrowid or 0), "created": True}],
            label="цель «%s»" % title,
        )

    goal_id = int(goal["id"])
    title = str(goal["title"])
    prev_progress = int(goal["progress"] or 0)
    prev_current = float(goal["current"] or 0)
    target = float(goal["target"] or 0)
    unit = str(goal["unit"] or "")
    if mode == "set":
        progress = max(0, min(100, delta))
    else:
        progress = max(0, min(100, prev_progress + delta))

    current = prev_current
    if unit == "%" and target > 0:
        value = progress if mode == "set" else prev_current + delta
        current = max(0.0, min(target, value))
    conn.execute(
        "UPDATE goals SET progress = ?, current = ? WHERE id = ?",
        (progress, current, goal_id),
    )
    sign = "%+d" % delta if mode == "delta" else "%d" % delta
    return Outcome(
        text="Цель «%s»: %d%% (%s)" % (title, progress, sign),
        operations=[
            {
                "type": "goal",
                "id": goal_id,
                "prev_progress": prev_progress,
                "prev_current": prev_current,
            }
        ],
        label="прогресс цели «%s»" % title,
    )


def complete_goal_step(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """«веха … выполнена» — закрывает веху цели."""
    goal_query = str(data.get("goal") or "").strip()
    step_query = str(data.get("step") or "").strip()
    goal = _find_goal(conn, goal_query) if goal_query else _find_goal(conn, "")
    rows = conn.execute(
        "SELECT st.id, st.title, st.done, st.goal_id, g.title AS goal_title "
        "FROM goal_steps AS st JOIN goals AS g ON g.id = st.goal_id "
        "WHERE st.done = 0 ORDER BY st.sort, st.id"
    ).fetchall()
    target = None
    if step_query:
        needle = step_query.casefold()
        for row in rows:
            if goal is not None and int(row["goal_id"]) != int(goal["id"]):
                continue
            title = str(row["title"]).casefold()
            if needle == title or needle in title or title in needle:
                target = row
                break
    if target is None and goal is not None:
        for row in rows:
            if int(row["goal_id"]) == int(goal["id"]):
                target = row
                break
    if target is None and not goal_query and step_query:
        for row in rows:  # веха названа, цель не названа — ищем по всем целям
            title = str(row["title"]).casefold()
            if step_query.casefold() in title or title in step_query.casefold():
                target = row
                break

    if target is not None:
        conn.execute("UPDATE goal_steps SET done = 1 WHERE id = ?", (int(target["id"]),))
        return Outcome(
            text="Веха «%s» закрыта (цель «%s»)" % (target["title"], target["goal_title"]),
            operations=[{"type": "goal_step", "id": int(target["id"]), "created": False}],
            label="веха «%s»" % str(target["title"]),
        )

    if goal is None:
        return Outcome("Не нашёл цель — назови её: «веха к цели Здоровье выполнена».")
    if step_query:
        cursor = conn.execute(
            "INSERT INTO goal_steps (goal_id, title, done, sort, created) VALUES (?, ?, 1, 0, ?)",
            (int(goal["id"]), step_query[:200], _stamp()),
        )
        return Outcome(
            text="Веха «%s» закрыта (цель «%s»)" % (step_query, goal["title"]),
            operations=[{"type": "goal_step", "id": int(cursor.lastrowid or 0), "created": True}],
            label="веха «%s»" % step_query,
        )
    return Outcome(
        "У цели «%s» нет вех — добавь их в Компасе." % goal["title"]
    )


# --------------------------------------------------------------------------
# Деньги
# --------------------------------------------------------------------------


def add_transaction(conn: Any, settings: Any, data: Mapping[str, Any], kind: str) -> Outcome:
    """Транзакция: сумма, статья, счёт и тип."""
    amount = int(data.get("amount_kopecks") or 0)
    if amount <= 0:
        return Outcome("Не понял сумму — напиши, например «такси 450».")
    text = str(data.get("note") or data.get("text") or "")
    wanted = str(data.get("category") or "")
    account_hint = str(data.get("account") or intent_v4.infer_account(text))
    category_id, category_name = resolve_category(conn, kind, wanted, text)
    account_id, account_name = resolve_account(conn, account_hint)
    day = str(data.get("date") or "").strip()
    ts = _ts(settings)
    if day:
        try:
            moment = datetime.fromisoformat(day[:10] + "T12:00:00").replace(
                tzinfo=_tz(settings)
            )
            ts = moment.isoformat(timespec="seconds")
        except ValueError:  # pragma: no cover - дата уже провалидирована
            ts = _ts(settings)
    cursor = conn.execute(
        "INSERT INTO transactions (ts, amount_kopecks, kind, category_id, account_id, "
        "note, source, created) VALUES (?, ?, ?, ?, ?, ?, 'telegram', ?)",
        (
            ts,
            amount,
            kind,
            category_id,
            account_id,
            _short(text, 300),
            _stamp(),
        ),
    )
    action = "доход" if kind == "income" else "расход"
    return Outcome(
        text="Записал %s: %s — %s, %s" % (action, _rub(amount), category_name, account_name),
        operations=[{"type": "transaction", "id": int(cursor.lastrowid or 0)}],
        label="%s %s" % (action, _rub(amount)),
        edit_hint="Исправить: пришли запись заново, старую удали кнопкой.",
    )


# --------------------------------------------------------------------------
# Книги
# --------------------------------------------------------------------------


def save_book_progress(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """«читаю Sapiens стр 120» — страница книги и полка."""
    book, created = resolve_book(
        conn, str(data.get("book") or ""), create=True, pages_total=int(data.get("pages_total") or 0)
    )
    if book is None:  # pragma: no cover - create=True всегда даёт строку
        return Outcome("Не понял книгу — напиши «читаю Sapiens стр 120».")
    book_id = int(book["id"])
    title = str(book["title"])
    prev = {
        "pages_total": int(book["pages_total"] or 0),
        "pages_read": int(book["pages_read"] or 0),
        "shelf": str(book["shelf"] or "queue"),
        "finished_at": book["finished_at"],
    }
    page = int(data.get("page") or 0)
    total = int(data.get("pages_total") or book["pages_total"] or 0)
    pages_read = max(prev["pages_read"], page) if page > 0 else prev["pages_read"]
    shelf = prev["shelf"]
    finished_at = prev["finished_at"]
    status = str(data.get("book_status") or data.get("shelf") or "").strip()
    rating = data.get("rating")
    if status in {"done", "dropped"}:
        shelf = status
        if status == "done":
            if total > 0:
                pages_read = total
            finished_at = _today(settings)
        else:
            finished_at = prev["finished_at"]
    elif total > 0 and pages_read >= total:
        shelf = "done"
        pages_read = total
        finished_at = _today(settings)
    elif page > 0 and shelf != "done":
        shelf = "reading"
    elif shelf == "queue":
        shelf = "reading"
    updates = "pages_total = ?, pages_read = ?, shelf = ?, finished_at = ?, updated = ?"
    params: List[Any] = [total, pages_read, shelf, finished_at, _stamp(), book_id]
    if isinstance(rating, int) and 1 <= rating <= 5:
        updates += ", rating = ?"
        params.insert(-1, rating)
    conn.execute("UPDATE books SET " + updates + " WHERE id = ?", tuple(params))
    percent = int(round(pages_read * 100 / total)) if total > 0 else 0
    if shelf == "done":
        text = "«%s» — прочитана" % title
        if isinstance(rating, int) and 1 <= rating <= 5:
            text += ", оценка %d/5" % rating
    elif shelf == "dropped":
        text = "«%s» — отложена" % title
    elif total > 0:
        text = "«%s»: стр. %d из %d (%d%%)" % (title, pages_read, total, percent)
    elif pages_read > 0:
        text = "«%s»: стр. %d" % (title, pages_read)
    else:
        text = "«%s» — на полке «Читаю»" % title
    return Outcome(
        text=text,
        operations=[{"type": "book", "id": book_id, "prev": prev, "created": created}],
        label="книга «%s»" % title,
    )


def create_book_intent(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """«купил книгу …» / «взял в работу …» — книга на нужной полке."""
    query = str(data.get("book") or data.get("title") or data.get("text") or "").strip()
    if not query:
        return Outcome("Не понял книгу — напиши «купил книгу Думай медленно».")
    shelf = str(data.get("shelf") or data.get("book_status") or "queue").strip()
    if shelf not in {"reading", "queue", "done", "dropped"}:
        shelf = "queue"
    book, created = resolve_book(conn, query, create=True)
    if book is None:  # pragma: no cover - create=True всегда даёт строку
        return Outcome("Не смог добавить книгу.")
    book_id = int(book["id"])
    title = str(book["title"])
    prev = {
        "pages_total": int(book["pages_total"] or 0),
        "pages_read": int(book["pages_read"] or 0),
        "shelf": str(book["shelf"] or "queue"),
        "finished_at": book["finished_at"],
    }
    conn.execute(
        "UPDATE books SET shelf = ?, updated = ? WHERE id = ?",
        (shelf, _stamp(), book_id),
    )
    titles = {
        "reading": "Читаю",
        "queue": "В очереди",
        "done": "Прочитано",
        "dropped": "Отложено",
    }
    return Outcome(
        text="Книга «%s» — на полке «%s»" % (title, titles[shelf]),
        operations=[{"type": "book", "id": book_id, "prev": prev, "created": created}],
        label="книга «%s»" % title,
    )


def create_goal_intent(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """«цель: прочитать 12 книг за год» — новая цель Компаса."""
    title = _short(data.get("title") or data.get("text") or "Цель", 200)
    if not title:
        return Outcome("Не понял цель — напиши «цель: прочитать 12 книг за год».")
    why = _short(str(data.get("text") or ""), 400)
    cursor = conn.execute(
        "INSERT INTO goals (title, target, current, unit, horizon, why, progress, "
        "status, created) VALUES (?, 0, 0, '', ?, ?, 0, 'active', ?)",
        (title, str(data.get("horizon") or "year"), why, _stamp()),
    )
    return Outcome(
        text="Цель «%s» записана" % title,
        operations=[{"type": "goal", "id": int(cursor.lastrowid or 0), "created": True}],
        label="цель «%s»" % title,
    )


def add_book_quote(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """«цитата: …» — в копилку цитат книги."""
    text = str(data.get("text") or "").strip()
    if not text:
        return Outcome("Пустая цитата — пришли текст после «цитата:».")
    book, created = resolve_book(conn, str(data.get("book") or ""), create=False)
    if book is None:
        book = _last_book(conn)
    if book is None:
        book, created = resolve_book(conn, "Без книги", create=True)
    page = data.get("page")
    cursor = conn.execute(
        "INSERT INTO book_quotes (book_id, text, page, created) VALUES (?, ?, ?, ?)",
        (int(book["id"]), text[:4000], int(page) if page else None, _stamp()),
    )
    conn.execute("UPDATE books SET updated = ? WHERE id = ?", (_stamp(), int(book["id"])))
    title = str(book["title"])
    page_note = ", стр. %d" % int(page) if page else ""
    return Outcome(
        text="Цитата в «%s»%s: «%s»" % (title, page_note, _short(text, 80)),
        operations=[
            {
                "type": "book_quote",
                "id": int(cursor.lastrowid or 0),
                "book_id": int(book["id"]),
                "book_created": bool(created),
            }
        ],
        label="цитата в «%s»" % title,
    )


def add_book_insight(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """«мысль из книги: …» / «инсайт: …» — в главные мысли книги."""
    text = str(data.get("text") or "").strip()
    if not text:
        return Outcome("Пустая мысль — пришли текст после «мысль из книги:».")
    book, created = resolve_book(conn, str(data.get("book") or ""), create=False)
    if book is None:
        book = _last_book(conn)
    if book is None:
        book, created = resolve_book(conn, "Без книги", create=True)
    sort_row = conn.execute(
        "SELECT COALESCE(MAX(sort), -1) + 1 FROM book_insights WHERE book_id = ?",
        (int(book["id"]),),
    ).fetchone()
    cursor = conn.execute(
        "INSERT INTO book_insights (book_id, text, sort) VALUES (?, ?, ?)",
        (int(book["id"]), text[:4000], int(sort_row[0] if sort_row else 0)),
    )
    conn.execute("UPDATE books SET updated = ? WHERE id = ?", (_stamp(), int(book["id"])))
    title = str(book["title"])
    return Outcome(
        text="Мысль в «%s»: «%s»" % (title, _short(text, 80)),
        operations=[
            {
                "type": "book_insight",
                "id": int(cursor.lastrowid or 0),
                "book_id": int(book["id"]),
                "book_created": bool(created),
            }
        ],
        label="мысль из «%s»" % title,
    )


# --------------------------------------------------------------------------
# Журнал, привычки, чек-ин
# --------------------------------------------------------------------------


def add_journal(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """Свободная мысль — запись журнала с темами и настроением."""
    text = str(data.get("text") or "").strip()
    if not text:
        return Outcome("Пустая мысль — напиши текст.")
    topics = data.get("topics") if isinstance(data.get("topics"), list) else intent_v4.infer_topics(text)
    mood = str(data.get("mood") or intent_v4.infer_mood(text))
    meta = {
        "topics": [str(topic)[:40] for topic in topics][:8],
        "mood": mood[:60],
        "intent": str(data.get("engine") or "rules"),
    }
    entry_id = db.add_entry(
        conn, "journal", text[:4000], ts=_ts(settings), source="telegram", meta=meta
    )
    earned = points_v4.award(
        conn, "journal", "journal:%d" % entry_id, "Мысль", now=_now(settings)
    )
    details: List[str] = []
    if topics:
        details.append("темы: " + ", ".join(str(topic) for topic in topics[:4]))
    if mood:
        details.append("настроение: " + mood)
    suffix = (" — " + "; ".join(details)) if details else ""
    journal_kind = str(data.get("journal_kind") or "")
    if journal_kind in ("activity", "state"):
        head = "Записал в журнал: «%s»" % _short(text, 80)
    else:
        head = "Мысль записана"
    return Outcome(
        text=head + suffix + _points_suffix(earned),
        operations=[{"type": "entry", "id": entry_id}],
        label="мысль",
        edit_hint="Новую версию пришли текстом, старую удали кнопкой.",
    )


def mark_habit(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """«выпил 2 л», «пробежал 5 км», «не курил» — отметка привычки за сегодня."""
    text = str(data.get("text") or "")
    habit_id, name, created = resolve_habit(conn, data, text)
    day = _today(settings)
    water_ml = int(data.get("water_ml") or 0)
    operations: List[Dict[str, Any]] = [
        {"type": "habit_mark", "habit_id": habit_id, "date": day}
    ]
    if created:
        operations.append({"type": "habit", "id": habit_id, "created": True})
    if water_ml > 0:
        # Вода — всегда метрика kind='water' (мл), как и «вода 100» из v3.
        # Отметка привычки — дополнительная: без неё вода из «выпил 2 л» жила
        # бы только в habit_marks и не попадала в дневную норму на экране.
        metric_id = db.add_metric(
            conn, "water", float(water_ml), date=day, unit="мл", source="telegram"
        )
        operations.append({"type": "metric", "id": metric_id})
        total_ml = _water_ml(conn, day)
        value = (total_ml or float(water_ml)) / 1000.0
    else:
        value = float(data.get("amount") or 0) or 1.0
    conn.execute(
        "INSERT INTO habit_marks (habit_id, date, value) VALUES (?, ?, ?) "
        "ON CONFLICT(habit_id, date) DO UPDATE SET value = excluded.value",
        (habit_id, day, value),
    )
    earned = points_v4.award(
        conn, "habit", "habit:%d:%s" % (habit_id, day), "Привычка «%s»" % name,
        now=_now(settings),
    )
    suffix = ""
    amount = data.get("amount")
    unit = str(data.get("unit") or "")
    if amount and unit:
        suffix = " · %s %s" % (_fmt_number(amount, 1), unit)
    if water_ml > 0:
        reply = water_line(conn, settings, water_ml) + " Привычка «%s» отмечена." % name
    else:
        reply = "Привычка «%s» — сегодня%s%s" % (name, suffix, _points_suffix(earned))
    return Outcome(
        text=reply,
        operations=operations,
        label="привычка «%s»" % name,
    )


def save_checkin(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """Чек-ин ресурса: энергия 1–5, тег настроения, заметка."""
    energy = int(data.get("energy") or 0)
    if not 1 <= energy <= 5:
        return Outcome("Напиши энергию от 1 до 5: «энергия 4, устал после созвонов».")
    day = _today(settings)
    note = str(data.get("note") or "")
    mood = str(data.get("mood") or intent_v4.infer_mood(note or str(data.get("text") or "")))
    prev = conn.execute("SELECT * FROM checkins WHERE date = ?", (day,)).fetchone()
    conn.execute(
        "INSERT INTO checkins (date, energy, mood, note, created) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET energy = excluded.energy, "
        "mood = excluded.mood, note = excluded.note",
        (day, energy, mood[:60], note[:2000], _ts(settings)),
    )
    earned = points_v4.award(
        conn, "checkin", "checkin:%s" % day, "Чек-ин", now=_now(settings)
    )
    details = ["энергия %d/5" % energy]
    if mood:
        details.append(mood)
    text = "Чек-ин: " + ", ".join(details)
    if note:
        text += " — «%s»" % _short(note, 60)
    text += _points_suffix(earned)
    operation: Dict[str, Any] = {"type": "checkin", "date": day}
    if prev is not None:
        operation["prev"] = {
            "energy": int(prev["energy"]),
            "mood": str(prev["mood"] or ""),
            "note": str(prev["note"] or ""),
        }
    return Outcome(text=text, operations=[operation], label="чек-ин за сегодня")


def save_inbox(conn: Any, settings: Any, text: str) -> Outcome:
    """Нераспознанное не теряется: исходный текст — в ``inbox_items``."""
    cleaned = " ".join(str(text or "").split())[:4000]
    if not cleaned:
        return Outcome("Пустое сообщение — нечего записывать.")
    cursor = conn.execute(
        "INSERT INTO inbox_items (text, status, created) VALUES (?, 'new', ?)",
        (cleaned, _stamp()),
    )
    return Outcome(
        text="Положил в Инбокс, разбери вечером: «%s»" % _short(cleaned, 80),
        operations=[{"type": "inbox", "id": int(cursor.lastrowid or 0)}],
        label="инбокс",
    )


# --------------------------------------------------------------------------
# Диспетчер записи
# --------------------------------------------------------------------------

_WRITERS = {
    "task": lambda conn, settings, data: create_task(conn, settings, data),
    "task_done": lambda conn, settings, data: complete_task(conn, settings, data),
    "goal_progress": lambda conn, settings, data: add_goal_progress(conn, settings, data),
    "goal_step_done": lambda conn, settings, data: complete_goal_step(conn, settings, data),
    "expense": lambda conn, settings, data: add_transaction(conn, settings, data, "expense"),
    "income": lambda conn, settings, data: add_transaction(conn, settings, data, "income"),
    "book_progress": lambda conn, settings, data: save_book_progress(conn, settings, data),
    "create_book": lambda conn, settings, data: create_book_intent(conn, settings, data),
    "create_goal": lambda conn, settings, data: create_goal_intent(conn, settings, data),
    "book_quote": lambda conn, settings, data: add_book_quote(conn, settings, data),
    "book_insight": lambda conn, settings, data: add_book_insight(conn, settings, data),
    "journal": lambda conn, settings, data: add_journal(conn, settings, data),
    "habit": lambda conn, settings, data: mark_habit(conn, settings, data),
    "practice": lambda conn, settings, data: log_practice(conn, settings, data),
    "checkin": lambda conn, settings, data: save_checkin(conn, settings, data),
    "reschedule": lambda conn, settings, data: reschedule_last(conn, settings, data),
    "move_work": lambda conn, settings, data: move_task_list(conn, data, "work"),
    "move_personal": lambda conn, settings, data: move_task_list(conn, data, "personal"),
}


def apply_intent(conn: Any, settings: Any, data: Mapping[str, Any]) -> Optional[Outcome]:
    """Пишет одну сущность по разобранному намерению.

    ``None`` — намерение не про запись (сводки, подсказки): их обрабатывает
    ``BotService``. Исключения БД не глотает — вызывающий код отвечает человеку.
    """
    intent = str(data.get("intent") or "")
    writer = _WRITERS.get(intent)
    if writer is None:
        return None
    return writer(conn, settings, data)


# --------------------------------------------------------------------------
# Последняя запись и отмена
# --------------------------------------------------------------------------


def remember_last(conn: Any, outcome: Outcome) -> None:
    payload = {
        "label": outcome.label or outcome.text,
        "edit_hint": outcome.edit_hint,
        "operations": outcome.operations,
    }
    db.set_setting(conn, LAST_KEY, json.dumps(payload, ensure_ascii=False))


def last_record(conn: Any) -> Optional[Dict[str, Any]]:
    raw = db.get_setting(conn, LAST_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def clear_last(conn: Any) -> None:
    """Забывает последнюю запись (после удаления по кнопке)."""
    db.set_setting(conn, LAST_KEY, "")


def save_pending(conn: Any, payload: Mapping[str, Any]) -> None:
    db.set_setting(conn, PENDING_KEY, json.dumps(dict(payload), ensure_ascii=False))


def load_pending(conn: Any) -> Optional[Dict[str, Any]]:
    raw = db.get_setting(conn, PENDING_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def clear_pending(conn: Any) -> None:
    db.set_setting(conn, PENDING_KEY, "")


def _delete_op(conn: Any, op: Mapping[str, Any]) -> bool:
    kind = str(op.get("type") or "")
    try:
        if kind in {"entry", "inbox"}:
            table = "entries" if kind == "entry" else "inbox_items"
            conn.execute("DELETE FROM %s WHERE id = ?" % table, (int(op.get("id") or 0),))
        elif kind == "metric":
            conn.execute("DELETE FROM metrics WHERE id = ?", (int(op.get("id") or 0),))
        elif kind == "food":
            conn.execute("DELETE FROM food_log WHERE id = ?", (int(op.get("id") or 0),))
        elif kind == "task":
            conn.execute("DELETE FROM tasks WHERE id = ?", (int(op.get("id") or 0),))
        elif kind == "task_status":
            conn.execute(
                "UPDATE tasks SET status = ?, done_at = ? WHERE id = ?",
                (
                    str(op.get("prev_status") or "todo"),
                    op.get("prev_done_at"),
                    int(op.get("id") or 0),
                ),
            )
        elif kind == "task_due":
            conn.execute(
                "UPDATE tasks SET due_date = ? WHERE id = ?",
                (op.get("prev"), int(op.get("id") or 0)),
            )
        elif kind == "task_list":
            conn.execute(
                "UPDATE tasks SET list_kind = ? WHERE id = ?",
                (str(op.get("prev") or "work"), int(op.get("id") or 0)),
            )
        elif kind == "transaction":
            conn.execute("DELETE FROM transactions WHERE id = ?", (int(op.get("id") or 0),))
        elif kind == "book":
            if op.get("created"):
                conn.execute("DELETE FROM books WHERE id = ?", (int(op.get("id") or 0),))
            else:
                prev = op.get("prev") or {}
                conn.execute(
                    "UPDATE books SET pages_total = ?, pages_read = ?, shelf = ?, "
                    "finished_at = ?, updated = ? WHERE id = ?",
                    (
                        int(prev.get("pages_total") or 0),
                        int(prev.get("pages_read") or 0),
                        str(prev.get("shelf") or "queue"),
                        prev.get("finished_at"),
                        _stamp(),
                        int(op.get("id") or 0),
                    ),
                )
        elif kind == "book_quote":
            conn.execute(
                "DELETE FROM book_quotes WHERE id = ?", (int(op.get("id") or 0),)
            )
            if op.get("book_created"):
                conn.execute(
                    "DELETE FROM books WHERE id = ?", (int(op.get("book_id") or 0),)
                )
        elif kind == "book_insight":
            conn.execute(
                "DELETE FROM book_insights WHERE id = ?", (int(op.get("id") or 0),)
            )
            if op.get("book_created"):
                conn.execute(
                    "DELETE FROM books WHERE id = ?", (int(op.get("book_id") or 0),)
                )
        elif kind == "practice":
            session = conn.execute(
                "SELECT practice_id, date FROM practice_sessions WHERE id = ?",
                (int(op.get("id") or 0),),
            ).fetchone()
            if session is not None:
                practice_id = int(session["practice_id"])
                day = str(session["date"])
                conn.execute(
                    "DELETE FROM points_ledger WHERE ref_id IN (?, ?)",
                    (
                        "practice:%d:%s" % (practice_id, day),
                        "record:%d:%s" % (practice_id, day),
                    ),
                )
                conn.execute(
                    "DELETE FROM practice_sessions WHERE id = ?",
                    (int(op.get("id") or 0),),
                )
        elif kind == "habit_mark":
            conn.execute(
                "DELETE FROM habit_marks WHERE habit_id = ? AND date = ?",
                (int(op.get("habit_id") or 0), str(op.get("date") or "")),
            )
        elif kind == "habit":
            if op.get("created"):
                conn.execute("DELETE FROM habits WHERE id = ?", (int(op.get("id") or 0),))
        elif kind == "checkin":
            prev = op.get("prev")
            if prev:
                conn.execute(
                    "UPDATE checkins SET energy = ?, mood = ?, note = ? WHERE date = ?",
                    (
                        int(prev.get("energy") or 1),
                        str(prev.get("mood") or ""),
                        str(prev.get("note") or ""),
                        str(op.get("date") or ""),
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM checkins WHERE date = ?", (str(op.get("date") or ""),)
                )
        elif kind == "goal":
            if op.get("created"):
                conn.execute("DELETE FROM goals WHERE id = ?", (int(op.get("id") or 0),))
            else:
                conn.execute(
                    "UPDATE goals SET progress = ?, current = ? WHERE id = ?",
                    (
                        int(op.get("prev_progress") or 0),
                        float(op.get("prev_current") or 0),
                        int(op.get("id") or 0),
                    ),
                )
        elif kind == "goal_step":
            if op.get("created"):
                conn.execute(
                    "DELETE FROM goal_steps WHERE id = ?", (int(op.get("id") or 0),)
                )
            else:
                conn.execute(
                    "UPDATE goal_steps SET done = 0 WHERE id = ?", (int(op.get("id") or 0),)
                )
        else:
            return False
    except Exception as exc:  # pragma: no cover - защита от гонок и битых ссылок
        logger.warning("не удалось отменить операцию %s: %s", kind, type(exc).__name__)
        return False
    return True


def delete_record(conn: Any, payload: Mapping[str, Any]) -> Tuple[bool, str]:
    """Отменяет запись по снимку ``bot:last_record``. ``(успех, что отменили)``."""
    operations = payload.get("operations")
    if not isinstance(operations, list) or not operations:
        return False, ""
    removed = 0
    # Сначала зависимые строки (метрика/цитата), затем созданные сущности.
    for op in reversed([op for op in operations if isinstance(op, Mapping)]):
        if _delete_op(conn, op):
            removed += 1
    label = str(payload.get("label") or "запись")
    return removed > 0, label


# --------------------------------------------------------------------------
# Практики и очки (t33): каталог и начисления — в app.server.practices
# --------------------------------------------------------------------------


def _practice_fallback(conn: Any, settings: Any, data: Mapping[str, Any], title: str) -> Outcome:
    """Если модуль практик недоступен — не теряем запись, кладём в журнал."""
    meta = {
        "practice": title,
        "slug": str(data.get("slug") or ""),
        "rounds": int(data.get("rounds") or 0) or 1,
        "text": "практика",
    }
    entry_id = db.add_entry(
        conn,
        "note",
        str(data.get("text") or title),
        ts=_now(settings),
        source="telegram",
        meta=meta,
    )
    return Outcome(
        text="Не смог записать практику — сохранил в дневник.",
        operations=[{"type": "entry", "id": int(entry_id)}],
        label="практика «%s»" % title,
    )


def log_practice(conn: Any, settings: Any, data: Mapping[str, Any]) -> Outcome:
    """«делаю практику вима хоффа» — сессия, очки и серия через t33.

    Тонкая обёртка над :mod:`app.server.practices`: каталог и начисления живут
    там, бот только передаёт слаг, круги и источник ``telegram``.

    Каждая фраза — это круги, сделанные сейчас: ``rounds_increment=True``
    прибавляет их к сегодняшней сессии. Повтор в тот же день не падает и не
    молчит «очки потом»: добавленные круги дают очки по ставке практики, а
    ответ честно называет, какой это круг за день.
    """
    title = _short(data.get("practice") or data.get("title") or "Практика", 120)
    slug = str(data.get("slug") or "wim-hof").strip() or "wim-hof"
    rounds = max(1, min(int(data.get("rounds") or 0) or 1, 100))
    try:
        db.ensure_practices(conn)
        practices.ensure_practice_catalog(conn)
        repeat = _practice_already_today(conn, settings, slug)
        if repeat is not None:
            # Одна практика в день даёт очки один раз: повтор только уточняет
            # число кругов (MAX), а ответ честно говорит «уже учтено».
            rounds_today, streak_days, actual_title = repeat
            if rounds > rounds_today:
                # Уточняем число кругов напрямую: очки за день уже начислены,
                # второй сессии и повторных начислений быть не должно.
                conn.execute(
                    "UPDATE practice_sessions SET rounds_done = ? "
                    "WHERE date = ? AND practice_id = "
                    "(SELECT id FROM practices WHERE slug = ?)",
                    (rounds, _now(settings).date().isoformat(), slug),
                )
                conn.commit()
                rounds_today = rounds
            return Outcome(
                text="%s — %d %s сегодня, очки уже учтено; серия %d %s."
                % (
                    actual_title,
                    rounds_today,
                    _plural(rounds_today, "круг", "круга", "кругов"),
                    streak_days,
                    _plural(streak_days, "день", "дня", "дней"),
                ),
                operations=[{"type": "practice", "id": 0}],
                label="практика «%s»" % actual_title,
                edit_hint="Пришли практику заново с числом кругов, старую удали кнопкой.",
            )
        result = practices.save_practice_session(
            conn,
            slug=slug,
            rounds_done=rounds,
            rounds_increment=True,
            source="telegram",
            now=_now(settings),
        )
    except KeyError:
        logger.warning("практика %r не найдена в каталоге — в журнал", slug)
        return _practice_fallback(conn, settings, data, title)
    except Exception:
        logger.exception("не удалось записать практику — сохраняю в журнал")
        return _practice_fallback(conn, settings, data, title)

    actual_title = str(result.get("title") or title)
    points = int(result.get("pointsEarned") or 0)
    streak = int((result.get("streak") or {}).get("days") or 0)
    rounds_done = int(result.get("roundsDone") or rounds)
    rounds_today = int(result.get("roundsToday") or rounds_done)
    added = int(result.get("roundsAdded") or 0)
    session = result.get("session") or {}
    session_id = int(session.get("id") or 0)
    streak_tail = "серия %d %s." % (streak, _plural(streak, "день", "дня", "дней"))
    if added <= 0:
        text = "%s — сегодня уже %d %s, очки не начисляю; %s" % (
            actual_title,
            rounds_today,
            _plural(rounds_today, "круг", "круга", "кругов"),
            streak_tail,
        )
    elif points > 0:
        if added == 1:
            text = "%s — %d-й круг сегодня, +%d %s, %s" % (
                actual_title,
                rounds_today,
                points,
                _plural(points, "очко", "очка", "очков"),
                streak_tail,
            )
        else:
            text = "%s — ещё %d %s, всего %d %s сегодня, +%d %s, %s" % (
                actual_title,
                added,
                _plural(added, "круг", "круга", "кругов"),
                rounds_today,
                _plural(rounds_today, "круг", "круга", "кругов"),
                points,
                _plural(points, "очко", "очка", "очков"),
                streak_tail,
            )
    else:
        text = "%s — %d-й круг сегодня, +0 очков: дневной лимит; %s" % (
            actual_title,
            rounds_today,
            streak_tail,
        )
    return Outcome(
        text=text,
        operations=[{"type": "practice", "id": session_id}],
        label="практика «%s»" % actual_title,
        edit_hint="Пришли практику заново с числом кругов, старую удали кнопкой.",
    )


def _practice_already_today(
    conn: Any, settings: Any, slug: str
) -> Optional[Tuple[int, int, str]]:
    """Сессия этой практики за сегодня с кругами: (круги, серия, название)."""
    day = _now(settings).date().isoformat()
    row = conn.execute(
        "SELECT ps.rounds_done, p.title, p.id FROM practice_sessions ps "
        "JOIN practices p ON p.id = ps.practice_id "
        "WHERE p.slug = ? AND ps.date = ?",
        (slug, day),
    ).fetchone()
    if row is None or int(row[0] or 0) <= 0:
        return None
    practice = practices.get_practice(conn, slug, today=_now(settings))
    streak = int(((practice or {}).get("stats") or {}).get("streakDays") or 0)
    return int(row[0] or 0), streak, str(row[1] or slug)


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def _points_suffix(points: int) -> str:
    """Хвост подтверждения «(+2 очка)»; ноль очков ничего не добавляет."""
    if points <= 0:
        return ""
    return " (+%d %s)" % (points, _plural(points, "очко", "очка", "очков"))


# --------------------------------------------------------------------------
# Сводки: день, неделя, деньги
# --------------------------------------------------------------------------


def _water_ml(conn: Any, day: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(value), 0) FROM metrics "
        "WHERE kind IN ('water', 'water_ml') AND date = ?",
        (day,),
    ).fetchone()
    return float(row[0] or 0) if row is not None else 0.0


def _water_target_ml(conn: Any) -> float:
    """Дневная норма воды из ``nutrition_targets``; 2200 мл — дефолт миграции 4."""
    try:
        row = conn.execute("SELECT water_ml FROM nutrition_targets WHERE id = 1").fetchone()
    except Exception:  # pragma: no cover - очень старая база без норм
        row = None
    try:
        value = float(row["water_ml"] or 0) if row is not None else 0.0
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else 2200.0


def _water_habit(conn: Any) -> Optional[Any]:
    """Существующая привычка про воду; новую ради отметки не создаём."""
    rows = conn.execute(
        "SELECT id, name FROM habits WHERE archived = 0 ORDER BY id"
    ).fetchall()
    for row in rows:
        name = str(row["name"] or "").casefold()
        if "вод" in name and "алко" not in name:
            return row
    return None


def water_line(conn: Any, settings: Any, added_ml: float) -> str:
    """«Вода: +2 л, итого 2 л из 2,2 л (91%)» — одна строка об одном смысле."""
    day = _today(settings)
    total = _water_ml(conn, day)
    target = _water_target_ml(conn)
    percent = int(round(total / target * 100.0)) if target > 0 else 0
    if added_ml >= 1000:
        added = "%s л" % _fmt_number(added_ml / 1000.0, 1)
    else:
        added = "%d мл" % int(round(added_ml))
    return "Вода: +%s, итого %s л из %s л (%d%%)." % (
        added,
        _fmt_number(total / 1000.0, 1),
        _fmt_number(target / 1000.0, 1),
        percent,
    )


def log_water(
    conn: Any, settings: Any, ml: float, *, day: Optional[str] = None
) -> Tuple[str, List[Dict[str, Any]]]:
    """Вода — всегда в ``metrics kind='water'`` (мл), привычка — дополнительно.

    Возвращает ``(текст ответа, операции для отмены)``. Привычку не заводит:
    «вода 100» не должна создавать новую привычку, если её нет; у «выпил 2 л»
    привычка уже есть и просто отмечается вместе с метрикой.
    """
    day = day or _today(settings)
    added = float(ml or 0)
    metric_id = db.add_metric(
        conn, "water", added, date=day, unit="мл", source="telegram"
    )
    operations: List[Dict[str, Any]] = [{"type": "metric", "id": int(metric_id)}]
    text = water_line(conn, settings, added)
    habit = _water_habit(conn)
    if habit is not None:
        total = _water_ml(conn, day)
        conn.execute(
            "INSERT INTO habit_marks (habit_id, date, value) VALUES (?, ?, ?) "
            "ON CONFLICT(habit_id, date) DO UPDATE SET value = excluded.value",
            (int(habit["id"]), day, total / 1000.0),
        )
        operations.append(
            {"type": "habit_mark", "habit_id": int(habit["id"]), "date": day}
        )
        text += " Привычка «%s» отмечена." % str(habit["name"])
    return text, operations


def _kcal(conn: Any, day: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(kcal), 0) FROM food_log WHERE date = ?", (day,)
    ).fetchone()
    return float(row[0] or 0) if row is not None else 0.0


def _sleep_hours(conn: Any, day: str) -> Optional[float]:
    row = conn.execute(
        "SELECT value FROM metrics WHERE kind = 'sleep' AND date = ? ORDER BY id DESC LIMIT 1",
        (day,),
    ).fetchone()
    return float(row[0]) if row is not None else None


def _habit_progress(conn: Any, day: str) -> Tuple[List[str], List[str]]:
    habits = conn.execute(
        "SELECT id, name FROM habits WHERE archived = 0 ORDER BY id"
    ).fetchall()
    marked = {
        int(row[0])
        for row in conn.execute(
            "SELECT habit_id FROM habit_marks WHERE date = ? AND value > 0", (day,)
        ).fetchall()
    }
    undone = [str(row["name"]) for row in habits if int(row["id"]) not in marked]
    done = [str(row["name"]) for row in habits if int(row["id"]) in marked]
    return done, undone


def _money_by_period(conn: Any, start: str, end: str) -> Dict[str, Any]:
    totals = {"expense": 0, "income": 0}
    for row in conn.execute(
        "SELECT kind, COALESCE(SUM(amount_kopecks), 0) AS total FROM transactions "
        "WHERE substr(ts, 1, 10) BETWEEN ? AND ? GROUP BY kind",
        (start, end),
    ).fetchall():
        if str(row["kind"]) in totals:
            totals[str(row["kind"])] = int(row["total"] or 0)
    categories = list(
        conn.execute(
            "SELECT COALESCE(c.name, 'Без статьи') AS name, "
            "       COALESCE(SUM(t.amount_kopecks), 0) AS total "
            "FROM transactions AS t "
            "LEFT JOIN finance_categories AS c ON c.id = t.category_id "
            "WHERE t.kind = 'expense' AND substr(t.ts, 1, 10) BETWEEN ? AND ? "
            "GROUP BY c.name ORDER BY total DESC LIMIT 4",
            (start, end),
        ).fetchall()
    )
    return {
        "expense": totals["expense"],
        "income": totals["income"],
        "categories": [(str(row["name"]), int(row["total"] or 0)) for row in categories],
    }


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _summary_period(period: str, settings: Any) -> Tuple[str, str, str]:
    """``(start, end, подпись)`` для сводки по деньгам."""
    try:
        today = date.fromisoformat(_today(settings))
    except ValueError:  # pragma: no cover
        today = date.today()
    if period == "day":
        return today.isoformat(), today.isoformat(), PERIOD_LABELS["day"]
    if period == "month":
        return today.replace(day=1).isoformat(), today.isoformat(), PERIOD_LABELS["month"]
    start = _week_start(today)
    return start.isoformat(), today.isoformat(), PERIOD_LABELS["week"]


def finance_summary(conn: Any, settings: Any, period: str = "week") -> str:
    """«сколько потратил» — цифры за день/неделю/месяц, без отговорок."""
    period = period if period in PERIOD_LABELS else "week"
    start, end, label = _summary_period(period, settings)
    data = _money_by_period(conn, start, end)
    lines: List[str] = []
    if data["expense"]:
        lines.append("Расходы %s: %s" % (label, _rub(data["expense"])))
        if data["categories"]:
            parts = ["%s %s" % (name, _rub(total)) for name, total in data["categories"][:4]]
            lines.append(" · ".join(parts))
    else:
        lines.append("Расходов %s нет." % label)
    if data["income"]:
        lines.append("Доходы %s: %s" % (label, _rub(data["income"])))
    return "\n".join(lines)


def today_summary(conn: Any, settings: Any) -> str:
    """``/today``: дела, деньги, еда/вода/сон, привычки и энергия за день."""
    day = _today(settings)
    rows = conn.execute(
        "SELECT title, status FROM tasks WHERE status <> 'cancelled' "
        "AND (due_date = ? OR substr(COALESCE(start_at, ''), 1, 10) = ?) "
        "ORDER BY CASE resource WHEN 'must' THEN 0 WHEN 'important' THEN 1 ELSE 2 END, id",
        (day, day),
    ).fetchall()
    open_titles = [str(row["title"]) for row in rows if str(row["status"]) == "todo"]
    done_count = sum(1 for row in rows if str(row["status"]) == "done")
    lines = ["Сегодня, %s" % intent_v4.format_human_day(day)]
    if open_titles:
        visible = "; ".join(open_titles[:4])
        extra = " (ещё %d)" % (len(open_titles) - 4) if len(open_titles) > 4 else ""
        tail = ", закрыто %d" % done_count if done_count else ""
        lines.append("Дела: %d открыто — %s%s%s" % (len(open_titles), visible, extra, tail))
    elif done_count:
        lines.append("Дела: всё закрыто — %d" % done_count)
    else:
        lines.append("Дел на сегодня нет.")

    money = _money_by_period(conn, day, day)
    if money["expense"]:
        parts = ["%s %s" % (name, _rub(total)) for name, total in money["categories"][:2]]
        lines.append("Расходы: %s%s" % (_rub(money["expense"]), (" — " + ", ".join(parts)) if parts else ""))
    else:
        lines.append("Расходов сегодня нет.")

    kcal = _kcal(conn, day)
    water = _water_ml(conn, day)
    sleep = _sleep_hours(conn, day)
    body: List[str] = []
    if kcal:
        body.append("%s ккал" % _fmt_number(kcal, 0))
    if water:
        body.append("вода %s л" % _fmt_number(water / 1000.0, 1))
    if sleep is not None:
        body.append("сон %s ч" % _fmt_number(sleep, 1))
    if body:
        lines.append("Еда и тело: " + " · ".join(body))

    done_habits, undone_habits = _habit_progress(conn, day)
    if undone_habits:
        lines.append("Привычки не отмечены: " + ", ".join(undone_habits[:5]))
    elif done_habits:
        lines.append("Привычки: отмечены все (%d)" % len(done_habits))

    practice_line = _practice_today_line(conn, day)
    if practice_line:
        lines.append(practice_line)

    checkin = conn.execute(
        "SELECT energy, mood, note FROM checkins WHERE date = ?", (day,)
    ).fetchone()
    if checkin is not None:
        details = ["энергия %d/5" % int(checkin["energy"])]
        if checkin["mood"]:
            details.append(str(checkin["mood"]))
        if checkin["note"]:
            details.append("«%s»" % _short(checkin["note"], 40))
        lines.append("Чек-ин: " + " · ".join(details))
    else:
        lines.append("Чек-ина нет: «энергия 4, устал после созвонов».")
    return "\n".join(lines)


def _practice_today_line(conn: Any, day: str) -> str:
    """Строка о практике за день: сделана или напоминание, что можно подышать."""
    try:
        row = conn.execute(
            "SELECT p.title, ps.rounds_done FROM practice_sessions ps "
            "JOIN practices p ON p.id = ps.practice_id "
            "WHERE ps.date = ? AND ps.rounds_done > 0 ORDER BY ps.id DESC LIMIT 1",
            (day,),
        ).fetchone()
    except Exception:  # pragma: no cover - таблиц практик ещё нет
        return ""
    if row is None:
        return "Практика: не было — «подышал 3 раунда»."
    rounds = int(row["rounds_done"] or 0)
    return "Практика: %s — %d %s." % (
        str(row["title"]),
        rounds,
        _plural(rounds, "круг", "круга", "кругов"),
    )


def week_summary(conn: Any, settings: Any) -> str:
    """``/week``: дела, деньги, привычки, чтение и энергия за неделю."""
    try:
        today = date.fromisoformat(_today(settings))
    except ValueError:  # pragma: no cover
        today = date.today()
    start = _week_start(today)
    week_end = start + timedelta(days=6)
    start_s, end_s = start.isoformat(), today.isoformat()
    week_end_s = week_end.isoformat()
    lines = [
        "Итоги недели: %s-%s"
        % (intent_v4.format_human_day(start_s), intent_v4.format_human_day(week_end_s))
    ]

    total_row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status <> 'cancelled' "
        "AND due_date IS NOT NULL AND due_date BETWEEN ? AND ?",
        (start_s, week_end_s),
    ).fetchone()
    done_row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'done' "
        "AND due_date IS NOT NULL AND due_date BETWEEN ? AND ?",
        (start_s, week_end_s),
    ).fetchone()
    tasks_total = int(total_row[0] or 0) if total_row is not None else 0
    tasks_done = int(done_row[0] or 0) if done_row is not None else 0
    lines.append("Дела: закрыто %d из %d" % (tasks_done, tasks_total) if tasks_total else "Дел с датой на неделе нет.")

    money = _money_by_period(conn, start_s, end_s)
    if money["expense"] or money["income"]:
        lines.append(
            "Деньги: расход %s · доход %s" % (_rub(money["expense"]), _rub(money["income"]))
        )
    else:
        lines.append("Денег на неделе не записано.")

    habits = conn.execute("SELECT COUNT(*) FROM habits WHERE archived = 0").fetchone()
    habit_count = int(habits[0] or 0) if habits is not None else 0
    marks = conn.execute(
        "SELECT COUNT(*) FROM habit_marks WHERE date BETWEEN ? AND ? AND value > 0",
        (start_s, end_s),
    ).fetchone()
    mark_count = int(marks[0] or 0) if marks is not None else 0
    if habit_count:
        days = max(1, (today - start).days + 1)
        pct = min(100, int(round(100.0 * mark_count / (habit_count * days))))
        lines.append("Привычки: %d%%" % pct)

    pages = conn.execute(
        "SELECT COALESCE(SUM(pages_read), 0) FROM books "
        "WHERE shelf = 'done' AND finished_at BETWEEN ? AND ?",
        (start_s, end_s),
    ).fetchone()
    pages_read = int(pages[0] or 0) if pages is not None else 0
    if pages_read:
        lines.append("Прочитано: %d страниц" % pages_read)

    energy = conn.execute(
        "SELECT AVG(energy) FROM checkins WHERE date BETWEEN ? AND ?", (start_s, end_s)
    ).fetchone()
    if energy is not None and energy[0] is not None:
        lines.append("Энергия: %s/5" % _fmt_number(energy[0], 1))
    return "\n".join(lines)


def _latest_metric(conn: Any, kind: str) -> Optional[Tuple[float, str]]:
    """Последнее значение замера: ``(value, дата)`` или ``None``."""
    row = conn.execute(
        "SELECT value, date FROM metrics WHERE kind = ? ORDER BY date DESC, id DESC LIMIT 1",
        (kind,),
    ).fetchone()
    if row is None:
        return None
    try:
        return float(row["value"]), str(row["date"])[:10]
    except (TypeError, ValueError):
        return None


def _weight_target(conn: Any) -> Optional[float]:
    """Цель по весу из настроек веба (``pref.weight_goal_kg``), если задана."""
    raw = db.get_setting(conn, "pref.weight_goal_kg")
    if not raw:
        return None
    try:
        return float(json.loads(raw))
    except (TypeError, ValueError):
        return None


def weight_summary(conn: Any, settings: Any) -> str:
    """``/weight``: последние 10 замеров веса и тренд, без простыни.

    Строка 1 — сами значения (по одному на день, новые слева), строка 2 —
    тренд и средний за неделю, строка 3 (если есть) — жир/мышцы и цель.
    """
    rows = conn.execute(
        "SELECT value, date FROM metrics WHERE kind = 'weight' "
        "ORDER BY date DESC, id DESC LIMIT 60"
    ).fetchall()
    points: List[Tuple[str, float]] = []
    seen: set = set()
    for row in rows:
        day = str(row["date"] or "")[:10]
        if not day or day in seen:
            continue
        try:
            value = float(row["value"])
        except (TypeError, ValueError):
            continue
        seen.add(day)
        points.append((day, value))
        if len(points) >= 10:
            break

    if not points:
        return (
            "Замеров веса ещё нет. Напиши «вес 81,2» — запишу и посчитаю тренд; "
            "всё вместе: «вес 81,2, жир 18,5, мышцы 35»."
        )

    values = " · ".join(_fmt_number(value, 1) for _day, value in points)
    lines = ["Вес, последние %d: %s кг." % (len(points), values)]
    latest_day, latest = points[0]
    if len(points) == 1:
        lines.append(
            "Один замер — %s (%s). Тренд появится со второго."
            % (_fmt_number(latest, 1), _fmt_date(latest_day, settings))
        )
        return "\n".join(lines)

    oldest_day, oldest = points[-1]
    try:
        days = (date.fromisoformat(latest_day) - date.fromisoformat(oldest_day)).days
    except ValueError:  # pragma: no cover - даты в БД уже ISO
        days = 0
    delta = latest - oldest
    trend = "Тренд: %s%s кг" % ("+" if delta > 0 else "", _fmt_number(delta, 1))
    if days > 0:
        trend += " за %d %s (%s → %s), %s%s кг в день" % (
            days,
            _days_word(days),
            _fmt_number(oldest, 1),
            _fmt_number(latest, 1),
            "+" if delta > 0 else "",
            _fmt_number(delta / days, 2),
        )
    else:
        trend += " (%s → %s, замеры за один день)" % (
            _fmt_number(oldest, 1),
            _fmt_number(latest, 1),
        )
    week_start = (_now(settings).date() - timedelta(days=6)).isoformat()
    avg_row = conn.execute(
        "SELECT AVG(value) AS avg, COUNT(*) AS n FROM metrics "
        "WHERE kind = 'weight' AND date >= ?",
        (week_start,),
    ).fetchone()
    if avg_row is not None and avg_row["avg"] is not None:
        trend += ", средний за 7 дней %s кг" % _fmt_number(float(avg_row["avg"]), 1)
    lines.append(trend + ".")

    sentences: List[str] = []
    measurements: List[str] = []
    fat = _latest_metric(conn, "fat")
    muscle = _latest_metric(conn, "muscle")
    if fat is not None:
        measurements.append("жир %s %%" % _fmt_number(fat[0], 1))
    if muscle is not None:
        measurements.append("мышцы %s кг" % _fmt_number(muscle[0], 1))
    if measurements:
        sentences.append("Замеры: " + " · ".join(measurements))
    target = _weight_target(conn)
    if target:
        left = latest - target
        sign = "осталось" if left > 0 else ("набрано" if left < 0 else "ровно")
        sentences.append(
            "цель %s кг — %s %s кг"
            % (_fmt_number(target, 1), sign, _fmt_number(abs(left), 1))
        )
    if sentences:
        lines.append(". ".join(sentences) + ".")
    return "\n".join(lines)


def save_last_ask(conn: Any, parsed: Mapping[str, Any], detail: bool = False) -> None:
    """Запоминает последний вопрос: по нему работают «Ещё» и «Кратко»."""
    payload = {"query": dict(parsed), "detail": bool(detail)}
    db.set_setting(conn, ASK_KEY, json.dumps(payload, ensure_ascii=False))


def load_last_ask(conn: Any) -> Optional[Dict[str, Any]]:
    raw = db.get_setting(conn, ASK_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("query"), Mapping):
        return None
    return payload


def clear_last_ask(conn: Any) -> None:
    db.set_setting(conn, ASK_KEY, "")


def answer_query(conn: Any, settings: Any, data: Mapping[str, Any]) -> str:
    """Совместимая обёртка: текст ответа без кнопок."""
    return answer_question(conn, settings, data).text


def answer_question(
    conn: Any,
    settings: Any,
    data: Mapping[str, Any],
    detail: bool = False,
) -> AskAnswer:
    """Ответ на вопрос о данных: короткий (3–6 строк) или подробный.

    Никогда не бросает наружу: неизвестный/сломанный вопрос получает честное
    «не знаю» с подсказкой, а не 500 и не запись в журнал.
    """
    topic = str(data.get("topic") or "").strip()
    kind = str(data.get("query_kind") or "").strip()
    try:
        if topic == "money" or kind == "spent":
            return _answer_money(conn, settings, data, detail)
        if topic == "overdue" or kind == "overdue":
            return _answer_overdue(conn, settings, data, detail)
        if topic in {"tasks", "day"} or kind in {"today", "day"}:
            return _answer_day(conn, settings, data, detail)
        if topic == "goal" or kind == "goal":
            return _answer_goal(conn, settings, data, detail)
        if topic == "weight" or kind == "weight":
            return _answer_weight(conn, settings, data, detail)
        if topic == "nutrition" or kind == "nutrition":
            return _answer_nutrition(conn, settings, data, detail)
        if topic == "sleep" or kind == "sleep":
            return _answer_sleep(conn, settings, data, detail)
        if topic == "energy" or kind == "energy":
            return _answer_energy(conn, settings, data, detail)
        if topic == "read" or kind == "read":
            return _answer_read(conn, settings, data, detail)
        if topic == "books" or kind == "books":
            return _answer_books(conn, settings, data, detail)
        if topic == "journal" or kind == "journal":
            return _answer_journal(conn, settings, data, detail)
        if topic == "workout" or kind == "workout":
            return _answer_workout(conn, settings, data, detail)
        if topic == "habits" or kind == "habits":
            return _answer_habits(conn, settings, data, detail)
        if topic == "week" or kind == "week":
            return _answer_week(conn, settings, data, detail)
        if topic == "points" or kind in {"points", "points_rules", "points_level"}:
            return _answer_points(conn, settings, data, detail)
        if topic == "unknown" or kind == "unknown":
            return _answer_unknown(data)
        return _answer_day(conn, settings, {"date": _today(settings)}, detail)
    except Exception:
        logger.exception("не удалось ответить на вопрос %r", {"topic": topic, "kind": kind})
        return AskAnswer(
            "Не смог посчитать ответ — попробую ещё раз по другому вопросу. "
            "Спроси про деньги, дела, сон, еду, книги, цели или привычки.",
            "/",
            "Примеры: «сколько потратил за неделю», «что на завтра», "
            "«как дела с целью Здоровье», «сколько спал на этой неделе».",
            "ошибка",
        )


def _ask_lines(lines: Sequence[str], limit: int = 6) -> str:
    return "\n".join([line for line in lines if line][:limit])


def _period_start_end(period: str, settings: Any) -> Tuple[date, date, str]:
    """Границы периода для вопросов: день/неделя/месяц и подпись."""
    try:
        today = date.fromisoformat(_today(settings))
    except ValueError:  # pragma: no cover
        today = date.today()
    if period == "day":
        return today, today, "за сегодня"
    if period == "month":
        return today.replace(day=1), today, "за месяц"
    start = _week_start(today)
    return start, today, "за неделю"


def _share(part: int, total: int) -> int:
    return int(round(part * 100.0 / total)) if total else 0


def _answer_money(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    period = str(data.get("period") or "week")
    if period not in PERIOD_LABELS:
        period = "week"
    start, end, label = _summary_period(period, settings)
    money = _money_by_period(conn, start, end)
    expense = int(money["expense"])
    income = int(money["income"])
    categories: List[Tuple[str, int]] = list(money["categories"])
    focus = str(data.get("focus") or "")
    lines: List[str] = []
    if focus == "top" and categories:
        name, total = categories[0]
        lines.append(
            "Больше всего %s: %s — %s (%d%%)."
            % (label, name, _rub(total), _share(total, expense))
        )
        rest = categories[1:3]
        if rest:
            lines.append("Дальше: " + ", ".join("%s %s" % (n, _rub(t)) for n, t in rest) + ".")
    else:
        if expense:
            lines.append("Расходы %s: %s." % (label, _rub(expense)))
        else:
            lines.append("Расходов %s нет." % label)
        if categories:
            name, total = categories[0]
            lines.append("Больше всего: %s — %s (%d%%)." % (name, _rub(total), _share(total, expense)))
    if income:
        lines.append("Доходы %s: %s, баланс %s." % (label, _rub(income), _rub(income - expense)))
    else:
        lines.append("Доходов %s не было." % label)
    days = conn.execute(
        "SELECT COUNT(DISTINCT substr(ts, 1, 10)) FROM transactions "
        "WHERE kind = 'expense' AND substr(ts, 1, 10) BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    spent_days = int(days[0] or 0) if days is not None else 0
    detail_lines = ["%s: %s" % (name, _rub(total)) for name, total in categories[:6]]
    detail_lines.append("Дней с тратами: %d." % spent_days)
    if income:
        detail_lines.append("Доходы: %s." % _rub(income))
    return AskAnswer(
        _ask_lines(lines, 4),
        "/finance",
        _ask_lines(detail_lines, 8),
        "деньги",
    )


def _day_header(day: date, settings: Any) -> str:
    today = _now(settings).date()
    if day == today:
        return "Сегодня, %s" % intent_v4.format_human_day(day.isoformat())
    if day == today + timedelta(days=1):
        return "Завтра, %s" % intent_v4.format_human_day(day.isoformat())
    return intent_v4.format_human_day(day.isoformat())


def _task_resource_label(resource: str) -> str:
    return {"must": "важное", "important": "обычное", "low": "мелкое"}.get(resource, resource)


def _answer_day(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    raw_day = str(data.get("date") or _today(settings))[:10]
    try:
        day = date.fromisoformat(raw_day)
    except ValueError:
        day = _now(settings).date()
    day_s = day.isoformat()
    rows = conn.execute(
        "SELECT title, status, resource FROM tasks WHERE status <> 'cancelled' "
        "AND (due_date = ? OR substr(COALESCE(start_at, ''), 1, 10) = ?) "
        "ORDER BY CASE resource WHEN 'must' THEN 0 WHEN 'important' THEN 1 ELSE 2 END, id",
        (day_s, day_s),
    ).fetchall()
    opened = [row for row in rows if str(row["status"]) == "todo"]
    done = sum(1 for row in rows if str(row["status"]) == "done")
    header = _day_header(day, settings)
    if opened:
        tail = ", %d закрыто" % done if done else ""
        lines = ["%s: %d открыто%s." % (header, len(opened), tail)]
    elif done:
        lines = ["%s: всё закрыто, %d." % (header, done)]
    else:
        lines = ["%s: открытых дел нет." % header]
    if opened:
        lines.append(
            " · ".join(
                "%s (%s)" % (str(row["title"]), _task_resource_label(str(row["resource"])))
                for row in opened[:3]
            )
            + (" …" if len(opened) > 3 else "")
        )
    today = _now(settings).date()
    overdue_row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'todo' AND due_date IS NOT NULL "
        "AND due_date < ?",
        (today.isoformat(),),
    ).fetchone()
    overdue = int(overdue_row[0] or 0) if overdue_row is not None else 0
    if overdue and day != today:
        lines.append("Просрочено сейчас: %d." % overdue)
    detail_lines = ["Открытые:"]
    for row in opened[:8]:
        detail_lines.append("— %s (%s)" % (str(row["title"]), _task_resource_label(str(row["resource"]))))
    if not opened:
        detail_lines = ["Открытых дел нет."]
    done_rows = [row for row in rows if str(row["status"]) == "done"]
    if done_rows:
        detail_lines.append("Закрыто: " + ", ".join(str(row["title"]) for row in done_rows[:5]) + ".")
    if overdue:
        detail_lines.append("Просрочено всего: %d." % overdue)
    return AskAnswer(_ask_lines(lines, 4), "/today", _ask_lines(detail_lines, 8), "дела")


def _answer_overdue(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    today = _now(settings).date()
    rows = conn.execute(
        "SELECT title, due_date FROM tasks WHERE status = 'todo' AND due_date IS NOT NULL "
        "AND due_date < ? ORDER BY due_date, id",
        (today.isoformat(),),
    ).fetchall()
    if not rows:
        return AskAnswer(
            "Просрочено: 0 — все дела в порядке.",
            "/today",
            "Просроченных дел нет. Если появятся, покажу их здесь.",
            "дела",
        )
    lines = ["Просрочено: %d." % len(rows)]
    for row in rows[:3]:
        try:
            due = date.fromisoformat(str(row["due_date"]))
            age = (today - due).days
            lines.append("— %s (с %02d.%02d, %d %s)." % (
                str(row["title"]), due.day, due.month, age, _days_word(age)
            ))
        except ValueError:
            lines.append("— %s." % str(row["title"]))
    detail_lines = ["Все просроченные:"]
    for row in rows[:8]:
        detail_lines.append("— %s (с %s)" % (str(row["title"]), str(row["due_date"])))
    return AskAnswer(_ask_lines(lines, 5), "/today", _ask_lines(detail_lines, 8), "просрочено")


def _answer_goal(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    query = str(data.get("goal") or "").strip()
    rows = []
    if query:
        needle = query.casefold()
        for candidate in conn.execute(
            "SELECT * FROM goals WHERE status = 'active' ORDER BY id"
        ).fetchall():
            if needle in str(candidate["title"]).casefold():
                rows.append(candidate)
                if len(rows) >= 2:
                    break
    if not rows:
        return AskAnswer(
            "Пока не знаю цели «%s» — в Компасе такой нет. "
            "Назови точнее или поставь цель: «цель: прочитать 12 книг за год»." % query,
            "/compass",
            "Активные цели: " + _goal_titles(conn) + ".",
            "цель",
        )
    row = rows[0]
    goal_id = int(row["id"])
    title = str(row["title"])
    steps = int(conn.execute("SELECT COUNT(*) FROM goal_steps WHERE goal_id = ?", (goal_id,)).fetchone()[0])
    steps_done = int(conn.execute(
        "SELECT COUNT(*) FROM goal_steps WHERE goal_id = ? AND done = 1", (goal_id,)
    ).fetchone()[0])
    tasks_total = int(conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE goal_id = ? AND status <> 'cancelled'", (goal_id,)
    ).fetchone()[0])
    tasks_done = int(conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE goal_id = ? AND status = 'done'", (goal_id,)
    ).fetchone()[0])
    manual = int(row["progress"] or 0)
    percent = _goal_progress_pct(manual, steps_done, steps, tasks_done, tasks_total)
    horizon = {"year": "год", "quarter": "квартал", "month": "месяц"}.get(str(row["horizon"] or ""), str(row["horizon"] or "—"))
    lines = ["Цель «%s»: %d%%." % (title, percent)]
    if steps:
        lines.append("Вехи: %d из %d." % (steps_done, steps))
    else:
        lines.append("Вех пока нет.")
    if row["target"]:
        unit = str(row["unit"] or "")
        if unit == "%":
            lines.append("Прогресс: %s из %s%%." % (
                _fmt_number(row["current"] or 0, 1), _fmt_number(row["target"], 1)
            ))
        else:
            lines.append("Прогресс: %s из %s %s." % (
                _fmt_number(row["current"] or 0, 1), _fmt_number(row["target"], 1), unit
            ))
    deadline = row["deadline"]
    lines.append("Горизонт: %s%s." % (horizon, (", до " + str(deadline)) if deadline else ""))
    detail_lines = ["Шаги цели:"]
    step_rows = conn.execute(
        "SELECT title, done FROM goal_steps WHERE goal_id = ? ORDER BY sort, id LIMIT 8", (goal_id,)
    ).fetchall()
    if step_rows:
        for step in step_rows:
            detail_lines.append(("+ " if int(step["done"]) else "· ") + str(step["title"]))
    else:
        detail_lines.append("Вех нет — добавь в Компасе.")
    if row["why"]:
        detail_lines.append("Зачем: " + _short(row["why"], 120))
    return AskAnswer(_ask_lines(lines, 4), "/compass", _ask_lines(detail_lines, 9), "цель")


def _goal_titles(conn: Any, limit: int = 5) -> str:
    rows = conn.execute(
        "SELECT title FROM goals WHERE status = 'active' ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    return ", ".join(str(row["title"]) for row in rows) if rows else "нет активных целей"


def _goal_progress_pct(manual: int, steps_done: int, steps: int, tasks_done: int, tasks: int) -> int:
    """Та же формула, что в API Компаса: ручной оверрайд → вехи → задачи."""
    if manual > 0:
        return min(int(manual), 100)
    if steps > 0:
        return int(round(steps_done * 100 / steps))
    if tasks > 0:
        return int(round(tasks_done * 100 / tasks))
    return 0


def _answer_weight(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    period = str(data.get("period") or "month")
    days = 7 if period == "week" else 30
    today = _now(settings).date()
    since = (today - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT date, value FROM metrics WHERE kind = 'weight' AND date >= ? "
        "AND date <= ? ORDER BY date, id",
        (since, today.isoformat()),
    ).fetchall()
    if not rows:
        return AskAnswer(
            "Пока не знаю: вес за %s не записан. Напиши «вес 81,2», и я посчитаю средний."
            % ("неделю" if days == 7 else "месяц"),
            "/body",
            "Замеры веса приходят из бота («вес 81,2») или из импорта весов в Настройках.",
            "вес",
        )
    by_day: Dict[str, List[float]] = {}
    for row in rows:
        by_day.setdefault(str(row["date"]), []).append(float(row["value"]))
    daily = [sum(values) / len(values) for values in by_day.values()]
    average = sum(daily) / len(daily)
    last = daily[-1]
    low, high = min(daily), max(daily)
    label = "за неделю" if days == 7 else "за месяц"
    lines = [
        "Средний вес %s: %s кг (%d %s)." % (label, _fmt_number(average, 1), len(daily), _measure_word(len(daily))),
        "Последний: %s кг." % _fmt_number(last, 1),
        "Диапазон: %s–%s кг." % (_fmt_number(low, 1), _fmt_number(high, 1)),
    ]
    detail_lines = ["Замеры:"]
    for day, values in list(by_day.items())[-10:]:
        detail_lines.append("%s — %s кг" % (day, _fmt_number(sum(values) / len(values), 1)))
    return AskAnswer(_ask_lines(lines, 4), "/body", _ask_lines(detail_lines, 10), "вес")


def _answer_nutrition(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    day = str(data.get("date") or _today(settings))[:10]
    macro = str(data.get("macro") or "kcal")
    row = conn.execute(
        "SELECT COALESCE(SUM(kcal), 0) AS kcal, COALESCE(SUM(protein), 0) AS protein, "
        "COALESCE(SUM(fat), 0) AS fat, COALESCE(SUM(carbs), 0) AS carbs, "
        "COALESCE(SUM(fiber), 0) AS fiber, COALESCE(SUM(sugar), 0) AS sugar "
        "FROM food_log WHERE date = ?",
        (day,),
    ).fetchone()
    totals = {key: float(row[key] or 0) for key in ("kcal", "protein", "fat", "carbs", "fiber", "sugar")} if row else {}
    target_row = conn.execute("SELECT * FROM nutrition_targets WHERE id = 1").fetchone()
    targets = {
        "kcal": float(target_row["kcal"]) if target_row else 2300.0,
        "protein": float(target_row["protein"]) if target_row else 150.0,
        "fat": float(target_row["fat"]) if target_row else 75.0,
        "carbs": float(target_row["carbs"]) if target_row else 260.0,
    }
    if not totals or totals["kcal"] <= 0:
        return AskAnswer(
            "Пока не знаю: за %s еда не записана. Напиши «на обед гречка 200 г и курица 150»."
            % _day_word(day, settings),
            "/nutrition",
            "Дневник еды: «съел 600 ккал и 40 белка», «банан 100 г».",
            "еда",
        )
    labels = {"kcal": "Калории", "protein": "Белок", "fat": "Жир", "carbs": "Углеводы"}
    unit = "ккал" if macro == "kcal" else "г"
    value = totals.get(macro, 0.0)
    target = targets.get(macro, 0.0)
    label = _day_word(day, settings)
    lines = [
        "%s %s: %s %s из %s (%d%%)."
        % (labels.get(macro, macro), label, _fmt_number(value, 0), unit, _fmt_number(target, 0), _share(int(value), int(target))),
        "Белок %s/%s г, жир %s/%s г, углеводы %s/%s г."
        % (
            _fmt_number(totals["protein"], 0), _fmt_number(targets["protein"], 0),
            _fmt_number(totals["fat"], 0), _fmt_number(targets["fat"], 0),
            _fmt_number(totals["carbs"], 0), _fmt_number(targets["carbs"], 0),
        ),
    ]
    remaining = target - value
    if remaining >= 0:
        lines.append("Остаток по норме: %s %s." % (_fmt_number(remaining, 0), unit))
    else:
        lines.append("Выше нормы на %s %s." % (_fmt_number(-remaining, 0), unit))
    meals = conn.execute(
        "SELECT meal, COALESCE(SUM(kcal), 0) AS kcal FROM food_log WHERE date = ? "
        "GROUP BY meal ORDER BY MIN(ts)",
        (day,),
    ).fetchall()
    meal_labels = {"breakfast": "завтрак", "lunch": "обед", "dinner": "ужин", "snack": "перекус"}
    detail_lines = ["%s: %s ккал" % (meal_labels.get(str(item["meal"]), str(item["meal"])), _fmt_number(item["kcal"], 0)) for item in meals]
    return AskAnswer(_ask_lines(lines, 4), "/nutrition", _ask_lines(detail_lines, 8), "еда")


def _day_word(day: str, settings: Any) -> str:
    label = intent_v4.format_date_short(day, _now(settings).date())
    return label if label in {"сегодня", "вчера", "завтра", "послезавтра"} else "за " + label


def _measure_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "замер"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "замера"
    return "замеров"


def _answer_sleep(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    period = str(data.get("period") or "week")
    if period == "month":
        start, today, label = _period_start_end("month", settings)
    else:
        start, today, label = _period_start_end("week", settings)
    rows = conn.execute(
        "SELECT date, value, unit FROM metrics WHERE kind IN ('sleep', 'sleep_h') "
        "AND date BETWEEN ? AND ? ORDER BY date, id",
        (start.isoformat(), today.isoformat()),
    ).fetchall()
    values: List[float] = []
    detail_lines = ["Ночи:"]
    for row in rows:
        value = float(row["value"] or 0)
        if "мин" in str(row["unit"] or "").casefold():
            value = value / 60.0
        elif value > 24:  # старые записи в минутах без единицы
            value = value / 60.0
        values.append(value)
        detail_lines.append("%s — %s ч" % (str(row["date"]), _fmt_number(value, 1)))
    if not values:
        return AskAnswer(
            "Пока не знаю: сон %s не записан. Напиши «спал 7,5», и я посчитаю." % label,
            "/sleep",
            "Сон пишется фразой «спал 7,5» или из импорта.",
            "сон",
        )
    average = sum(values) / len(values)
    lines = [
        "Сон %s: %s ч в среднем, %d %s." % (label, _fmt_number(average, 1), len(values), _nights_word(len(values))),
        "Последняя ночь: %s ч." % _fmt_number(values[-1], 1),
        "Дней с записью: %d." % len(values),
    ]
    return AskAnswer(_ask_lines(lines, 4), "/sleep", _ask_lines(detail_lines, 9), "сон")


def _answer_energy(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    day = str(data.get("date") or _today(settings))[:10]
    row = conn.execute(
        "SELECT energy, mood, note FROM checkins WHERE date = ? ORDER BY id DESC LIMIT 1",
        (day,),
    ).fetchone()
    label = _day_word(day, settings)
    if row is None:
        return AskAnswer(
            "Чек-ина %s нет. Напиши «энергия 4» или «устал после созвонов»." % label,
            "/personal",
            "Чек-ин энергии — 1 раз в день, вечером.",
            "чек-ин",
        )
    parts = ["энергия %d/5" % int(row["energy"] or 0)]
    if row["mood"]:
        parts.append(str(row["mood"]))
    if row["note"]:
        parts.append("«%s»" % _short(row["note"], 60))
    lines = ["Энергия %s: %s." % (label, " · ".join(parts))]
    detail_lines = ["Энергия %s: %d/5" % (label, int(row["energy"] or 0))]
    if row["mood"]:
        detail_lines.append("Состояние: " + str(row["mood"]))
    if row["note"]:
        detail_lines.append("Заметка: " + str(row["note"]))
    return AskAnswer(_ask_lines(lines, 3), "/personal", _ask_lines(detail_lines, 6), "энергия")


def _answer_read(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    term = str(data.get("term") or "").strip().casefold()
    snippets: List[str] = []
    sources: List[str] = []
    quotes = conn.execute(
        "SELECT q.text AS text, b.title AS book FROM book_quotes AS q "
        "LEFT JOIN books AS b ON b.id = q.book_id ORDER BY q.id DESC LIMIT 300"
    ).fetchall()
    for row in quotes:
        text = str(row["text"] or "")
        if not term or term in text.casefold():
            snippets.append("«%s»" % _short(text, 100))
            if row["book"]:
                sources.append(str(row["book"]))
        if len(snippets) >= 3:
            break
    if not snippets:
        for row in conn.execute(
            "SELECT text FROM book_insights ORDER BY id DESC LIMIT 200"
        ).fetchall():
            text = str(row["text"] or "")
            if term and term in text.casefold():
                snippets.append("«%s»" % _short(text, 100))
            if len(snippets) >= 3:
                break
    if not snippets:
        return AskAnswer(
            "Пока не знаю: про «%s» в книгах и мыслях ничего не записано. "
            "Пришли цитату: «цитата: …»." % term if term else "Цитат и мыслей о книгах пока нет.",
            "/library",
            "Бот сохраняет цитаты и инсайты из книг — они ищутся по тексту.",
            "чтение",
        )
    prefix = "Про «%s»" % term if term else "Из прочитанного"
    lines = ["%s: %s" % (prefix, snippets[0])]
    if len(snippets) > 1:
        lines.append("Ещё: " + " ".join(snippets[1:3]))
    if sources:
        lines.append("Книга: " + ", ".join(dict.fromkeys(sources[:3])) + ".")
    return AskAnswer(_ask_lines(lines, 4), "/library", _ask_lines(["Найдено:"] + snippets, 6), "чтение")


def _answer_books(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    rows = conn.execute(
        "SELECT title, shelf, pages_read, pages_total FROM books "
        "ORDER BY updated DESC, id DESC LIMIT 10"
    ).fetchall()
    if not rows:
        return AskAnswer(
            "На полке пока нет книг. Напиши «взял в работу Sapiens» или «купил книгу 1984».",
            "/library",
            "Полки: читаю, в очереди, прочитано, отложено.",
            "книги",
        )
    labels = {"reading": "читаю", "queue": "в очереди", "done": "прочитано", "dropped": "отложено"}
    reading = [row for row in rows if str(row["shelf"]) == "reading"]
    lines = ["Книг на полке: %d, читаю: %d." % (len(rows), len(reading))]
    if reading:
        lines.append("Сейчас: " + ", ".join(str(row["title"]) for row in reading[:3]) + ".")
    parts = [
        "%s (%s)" % (str(row["title"]), labels.get(str(row["shelf"]), str(row["shelf"])))
        for row in rows[:3]
    ]
    lines.append(" · ".join(parts) + ".")
    detail_lines = []
    for row in rows[:8]:
        progress = ""
        if int(row["pages_total"] or 0) > 0:
            progress = " — %d/%d стр." % (int(row["pages_read"] or 0), int(row["pages_total"]))
        detail_lines.append("%s (%s)%s" % (str(row["title"]), labels.get(str(row["shelf"]), str(row["shelf"])), progress))
    return AskAnswer(_ask_lines(lines, 4), "/library", _ask_lines(detail_lines, 9), "книги")


def _answer_journal(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    period = str(data.get("period") or "week")
    start, today, label = _period_start_end(period, settings)
    rows = conn.execute(
        "SELECT text, kind, meta_json, date FROM entries WHERE date BETWEEN ? AND ? "
        "AND kind NOT IN ('food', 'workout', 'weight') ORDER BY ts DESC, id DESC",
        (start.isoformat(), today.isoformat()),
    ).fetchall()
    if not rows:
        return AskAnswer(
            "Пока не знаю: %s записей нет. Пришли мысль — сохраню в журнал." % label,
            "/journal",
            "Журнал принимает свободный текст, «мысль: …», голос с расшифровкой.",
            "журнал",
        )
    topics: Dict[str, int] = {}
    for row in rows:
        try:
            meta = json.loads(str(row["meta_json"] or "{}"))
        except (TypeError, ValueError):
            meta = {}
        for topic in meta.get("topics") or []:
            topics[str(topic)] = topics.get(str(topic), 0) + 1
    lines = ["%s: %d %s." % (label.capitalize(), len(rows), _records_word(len(rows)))]
    if topics:
        top = sorted(topics.items(), key=lambda item: (-item[1], item[0]))[:3]
        lines.append("Темы: " + ", ".join("%s (%d)" % (name, count) for name, count in top) + ".")
    lines.append("Последняя: «%s»." % _short(str(rows[0]["text"] or ""), 100))
    detail_lines = ["Последние записи:"]
    for row in rows[:6]:
        detail_lines.append("%s — %s" % (str(row["date"]), _short(str(row["text"] or ""), 100)))
    return AskAnswer(_ask_lines(lines, 4), "/journal", _ask_lines(detail_lines, 9), "журнал")


def _answer_workout(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    rows = conn.execute(
        "SELECT text, ts, date FROM entries WHERE kind IN ('workout', 'training') "
        "ORDER BY ts DESC, id DESC LIMIT 8"
    ).fetchall()
    if not rows:
        return AskAnswer(
            "Пока не знаю: тренировок не записано. Напиши «тренировка 45 минут» или «жим 80х5».",
            "/workouts",
            "Тренировки пишутся фразой или из раздела «Тело».",
            "тренировки",
        )
    last = rows[0]
    try:
        last_day = date.fromisoformat(str(last["date"])[:10])
        ago = (_now(settings).date() - last_day).days
        ago_text = "сегодня" if ago == 0 else ("вчера" if ago == 1 else "%d %s назад" % (ago, _days_word(ago)))
    except ValueError:
        last_day = None
        ago_text = "недавно"
    lines = ["Последняя тренировка: %s — %s." % (ago_text, _short(str(last["text"] or "тренировка"), 80))]
    if last_day is not None:
        lines.append("Дата: %s." % intent_v4.format_human_day(last_day.isoformat()))
    month_start = _now(settings).date().replace(day=1)
    count_row = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE kind IN ('workout', 'training') AND date >= ?",
        (month_start.isoformat(),),
    ).fetchone()
    lines.append("В этом месяце: %d." % int(count_row[0] or 0) if count_row is not None else "В этом месяце: 0.")
    detail_lines = ["Последние тренировки:"]
    for row in rows[:6]:
        detail_lines.append("%s — %s" % (str(row["date"]), _short(str(row["text"] or ""), 80)))
    return AskAnswer(_ask_lines(lines, 4), "/workouts", _ask_lines(detail_lines, 8), "тренировки")


def _answer_points(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    """Вопросы об очках: баланс, что даёт очки, сколько до уровня.

    Цифры те же, что у ``/api/points`` и ``/points``: считает
    :mod:`app.bot.points_v4` поверх общего журнала начислений.
    """
    text = points_v4.answer(conn, settings, data, detail)
    detail_lines = [text]
    try:
        summary = practices.points_summary(conn, date.fromisoformat(_today(settings)), days=30)
    except Exception:  # pragma: no cover - сводка не должна глушить ответ
        summary = {}
    recent = summary.get("recent") if isinstance(summary.get("recent"), list) else []
    if recent:
        detail_lines.append("")
        detail_lines.append("Последние начисления:")
        for row in recent[:6]:
            if not isinstance(row, Mapping):
                continue
            detail_lines.append(
                "• %s: %s +%d"
                % (
                    str(row.get("date") or ""),
                    _short(str(row.get("reason") or row.get("kind") or "очки"), 60),
                    int(row.get("points") or 0),
                )
            )
    return AskAnswer(
        _ask_lines([text], 6),
        "/points",
        "\n".join(detail_lines),
        "очки",
    )


def _answer_habits(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    rows = conn.execute(
        "SELECT id, name FROM habits WHERE archived = 0 ORDER BY id"
    ).fetchall()
    if not rows:
        return AskAnswer(
            "Привычек пока нет. Напиши «не курил» или «выпил 2 л» — заведу привычку.",
            "/habits",
            "Привычки отмечаются фразами: «сделал зарядку», «без сахара».",
            "привычки",
        )
    day = _now(settings).date()
    today_s = day.isoformat()
    marks = {
        int(row["habit_id"])
        for row in conn.execute(
            "SELECT habit_id FROM habit_marks WHERE date = ? AND value > 0", (today_s,)
        ).fetchall()
    }
    week_marks = int(conn.execute(
        "SELECT COUNT(*) FROM habit_marks WHERE date BETWEEN ? AND ? AND value > 0",
        (_week_start(day).isoformat(), today_s),
    ).fetchone()[0] or 0)
    lines = ["Привычек: %d активных." % len(rows)]
    lines.append("Сегодня отмечено %d из %d (%d%%)." % (len(marks), len(rows), _share(len(marks), len(rows))))
    lines.append("За неделю: %d отметок." % week_marks)
    detail_lines = ["Привычки:"]
    for row in rows[:8]:
        mark = "✓" if int(row["id"]) in marks else "·"
        detail_lines.append("%s %s" % (mark, str(row["name"])))
    return AskAnswer(_ask_lines(lines, 4), "/habits", _ask_lines(detail_lines, 8), "привычки")


def _answer_week(conn: Any, settings: Any, data: Mapping[str, Any], detail: bool) -> AskAnswer:
    text = week_summary(conn, settings)
    start, end, _label = _period_start_end("week", settings)
    money = _money_by_period(conn, start.isoformat(), end.isoformat())
    detail_lines = ["Деньги за неделю: расход %s · доход %s." % (_rub(money["expense"]), _rub(money["income"]))]
    if money["categories"]:
        detail_lines.append("Статьи: " + ", ".join("%s %s" % (name, _rub(total)) for name, total in money["categories"][:4]) + ".")
    rows = conn.execute(
        "SELECT title FROM tasks WHERE status = 'todo' AND due_date BETWEEN ? AND ? "
        "ORDER BY due_date, id LIMIT 8",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if rows:
        detail_lines.append("Не закрыто: " + ", ".join(str(row["title"]) for row in rows) + ".")
    return AskAnswer(_ask_lines(text.splitlines(), 6), "/review", _ask_lines(detail_lines, 8), "итоги")


def _answer_unknown(data: Mapping[str, Any]) -> AskAnswer:
    question = str(data.get("question") or data.get("text") or "").strip()
    excerpt = _short(question, 100)
    text = (
        "Не знаю, как ответить на «%s»." % excerpt
        if excerpt
        else "Не знаю, как ответить на этот вопрос."
    )
    text += (
        " Я считаю только по твоим данным: деньги, дела, цели, сон, еда, книги, привычки."
    )
    examples = [
        "Примеры: «сколько потратил за неделю», «что на завтра», "
        "«как дела с целью Здоровье», «сколько спал на этой неделе»."
    ]
    return AskAnswer(text, "/", _ask_lines(examples, 3), "не знаю")


def _records_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "запись"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "записи"
    return "записей"


def _days_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "день"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "дня"
    return "дней"


def _when_label(day: str, settings: Any) -> str:
    label = intent_v4.format_date_short(day, _now(settings).date())
    if label in {"сегодня", "вчера", "завтра", "послезавтра"}:
        return label
    return "за " + label


def _nights_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "ночь"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "ночи"
    return "ночей"
