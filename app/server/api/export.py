"""Экспорт данных: ``GET /api/export?format=json|md`` (§12.6).

Отдаёт файл с ``Content-Disposition: attachment``. JSON — полный снимок
пользовательских сущностей v4 (сферы, цели, проекты, задачи, инбокс,
библиотека, финансы, привычки, чек-ины, итоги, журнал, метрики, курсы) плюс
счётчики и безопасные предпочтения. Markdown — читаемая выгрузка для глаза:
те же сущности, но текстом и без служебных полей.

Секреты не выгружаются: таблицы ``settings_kv``/``jobs`` и расшифровки уроков
(``transcripts``/``segments``/``lesson_progress``) в экспорт не попадают,
а из предпочтений берётся только белый список (тема, цели по воде/весу/сну,
тренировки). Токены бота и LLM живут в конфиге/окружении, а не в БД.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Query, Response

from app import __version__
from app.server.api.aggregates import read_preferences
from app.server.api.deps import ConnectionDep, SettingsDep
from app.server.settings import Settings
from app.server.db import SCHEMA_VERSION

__all__ = ["EXPORT_TABLES", "router"]

router = APIRouter(prefix="/api", tags=["export"])

#: Что попадает в выгрузку. Порядок — смысловой, он же порядок секций MD.
EXPORT_TABLES: tuple[str, ...] = (
    "spheres",
    "goals",
    "goal_steps",
    "anti_goals",
    "projects",
    "project_steps",
    "project_notes",
    "tasks",
    "inbox_items",
    "books",
    "book_insights",
    "book_quotes",
    "book_actions",
    "finance_accounts",
    "finance_categories",
    "transactions",
    "budgets",
    "habits",
    "habit_marks",
    "checkins",
    "reviews",
    "entries",
    "metrics",
    "courses",
    "lessons",
)

MD_JOURNAL_LIMIT = 30
MD_METRICS_LIMIT = 30
MD_REVIEWS_LIMIT = 5
MD_CHECKINS_LIMIT = 14
MD_OPEN_TASKS_LIMIT = 100
MD_DONE_TASKS_LIMIT = 30
MD_REVIEW_DAYS = 14


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _rub(kopecks: int | float | None) -> float:
    return round(int(kopecks or 0) / 100, 2)


def _table_rows(conn: Any, table: str) -> list[dict[str, Any]]:
    # ``table`` — константа модуля, пользовательские данные в SQL не попадают.
    rows = conn.execute("SELECT * FROM %s ORDER BY id" % table).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _payload(conn: Any, settings: Settings) -> dict[str, Any]:
    """Полный JSON-снимок: данные по таблицам + счётчики + безопасные префы."""
    data: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for table in EXPORT_TABLES:
        rows = _table_rows(conn, table)
        data[table] = rows
        counts[table] = len(rows)
    return {
        "app": {
            "name": "Life OS",
            "version": __version__,
            "schemaVersion": SCHEMA_VERSION,
        },
        "exportedAt": settings.now().isoformat(),
        "timezone": settings.timezone,
        "counts": counts,
        "preferences": read_preferences(conn),
        "data": data,
    }


def _md_heading(text: str) -> str:
    return "\n## " + text + "\n"


def _md_list(lines: list[str]) -> str:
    return "\n".join("- " + line for line in lines) if lines else "_нет данных_"


def _markdown(conn: Any, settings: Settings) -> str:
    """Читаемая Markdown-выгрузка без секретов и служебных таблиц."""
    today = settings.now().date()
    today_s = today.isoformat()
    counts = {
        table: int(
            conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        )
        for table in EXPORT_TABLES
    }

    parts: list[str] = [
        "# Life OS — выгрузка",
        "",
        "- Дата: %s" % settings.now().strftime("%Y-%m-%d %H:%M %Z"),
        "- Версия: %s (схема %d)" % (__version__, SCHEMA_VERSION),
        "- Часовой пояс: %s" % settings.timezone,
        "",
        "| Сущность | Записей |",
        "| --- | --- |",
    ]
    labels = {
        "spheres": "Сферы",
        "goals": "Цели",
        "goal_steps": "Вехи целей",
        "anti_goals": "Анти-цели",
        "projects": "Проекты",
        "project_steps": "Шаги проектов",
        "project_notes": "Заметки проектов",
        "tasks": "Задачи",
        "inbox_items": "Инбокс",
        "books": "Книги",
        "book_insights": "Мысли о книгах",
        "book_quotes": "Цитаты",
        "book_actions": "Действия по книгам",
        "finance_accounts": "Счета",
        "finance_categories": "Категории финансов",
        "transactions": "Транзакции",
        "budgets": "Бюджеты",
        "habits": "Привычки",
        "habit_marks": "Отметки привычек",
        "checkins": "Чек-ины",
        "reviews": "Итоги недель/месяцев",
        "entries": "Записи журнала",
        "metrics": "Измерения",
        "courses": "Курсы",
        "lessons": "Уроки",
    }
    for table in EXPORT_TABLES:
        parts.append("| %s | %d |" % (labels.get(table, table), counts[table]))

    # Сферы
    parts.append(_md_heading("Сферы"))
    spheres = conn.execute(
        "SELECT name, color, archived FROM spheres ORDER BY sort, id"
    ).fetchall()
    parts.append(
        _md_list(
            [
                "%s%s" % (str(row["name"]), " (архив)" if row["archived"] else "")
                for row in spheres
            ]
        )
    )

    # Цели
    parts.append(_md_heading("Цели"))
    goals = conn.execute(
        "SELECT title, target, current, unit, deadline, status FROM goals "
        "WHERE status IN ('active', 'done') ORDER BY status, id"
    ).fetchall()
    goal_lines: list[str] = []
    for row in goals:
        target = row["target"]
        progress = ""
        if target:
            progress = " — %s из %s %s" % (
                row["current"],
                target,
                str(row["unit"] or "").strip(),
            )
        deadline = " до %s" % row["deadline"] if row["deadline"] else ""
        goal_lines.append(
            "%s%s%s (%s)"
            % (_clean(row["title"]), progress, deadline, "закрыта" if row["status"] == "done" else "в работе")
        )
    parts.append(_md_list(goal_lines))

    # Проекты
    parts.append(_md_heading("Проекты"))
    projects = conn.execute(
        "SELECT name, status, deadline, outcome FROM projects ORDER BY id"
    ).fetchall()
    parts.append(
        _md_list(
            [
                "%s — %s%s"
                % (
                    _clean(row["name"]),
                    str(row["status"]),
                    " до %s" % row["deadline"] if row["deadline"] else "",
                )
                for row in projects
            ]
        )
    )

    # Задачи
    parts.append(_md_heading("Открытые задачи"))
    open_tasks = conn.execute(
        "SELECT title, due_date, resource, list_kind FROM tasks "
        "WHERE status = 'todo' ORDER BY due_date IS NULL, due_date, id LIMIT ?",
        (MD_OPEN_TASKS_LIMIT,),
    ).fetchall()
    parts.append(
        _md_list(
            [
                "%s — %s, %s%s"
                % (
                    _clean(row["title"]),
                    str(row["resource"]),
                    "работа" if row["list_kind"] == "work" else "личное",
                    " · до %s" % row["due_date"] if row["due_date"] else "",
                )
                for row in open_tasks
            ]
        )
    )

    parts.append(_md_heading("Закрытые задачи (14 дней)"))
    since = (today - timedelta(days=MD_REVIEW_DAYS)).isoformat()
    done_tasks = conn.execute(
        "SELECT title, done_at FROM tasks WHERE status = 'done' "
        "AND (done_at IS NULL OR substr(done_at, 1, 10) >= ?) "
        "ORDER BY done_at DESC, id DESC LIMIT ?",
        (since, MD_DONE_TASKS_LIMIT),
    ).fetchall()
    parts.append(_md_list([_clean(row["title"]) for row in done_tasks]))

    # Библиотека
    parts.append(_md_heading("Библиотека"))
    books = conn.execute(
        "SELECT title, author, shelf, pages_total, pages_read, rating FROM books "
        "ORDER BY shelf, priority DESC, id"
    ).fetchall()
    shelf_titles = {
        "reading": "Читаю",
        "queue": "В очереди",
        "done": "Прочитано",
        "dropped": "Отложено",
    }
    book_lines: list[str] = []
    for row in books:
        pages = ""
        if row["pages_total"]:
            pages = " — %d/%d стр." % (int(row["pages_read"] or 0), int(row["pages_total"]))
        rating = " · оценка %d/5" % int(row["rating"]) if row["rating"] is not None else ""
        author = " — %s" % _clean(row["author"]) if row["author"] else ""
        book_lines.append(
            "%s%s [%s]%s%s"
            % (
                _clean(row["title"]),
                author,
                shelf_titles.get(str(row["shelf"]), str(row["shelf"])),
                pages,
                rating,
            )
        )
    parts.append(_md_list(book_lines))

    # Финансы: текущий месяц
    month = today_s[:7]
    totals = {"income": 0, "expense": 0}
    for row in conn.execute(
        "SELECT kind, COALESCE(SUM(amount_kopecks), 0) AS total FROM transactions "
        "WHERE substr(ts, 1, 7) = ? GROUP BY kind",
        (month,),
    ):
        totals[str(row["kind"])] = int(row["total"] or 0)
    parts.append(_md_heading("Финансы (%s)" % month))
    parts.append(
        "Доход: **%s ₽**, расход: **%s ₽**, баланс: **%s ₽**"
        % (
            _rub(totals["income"]),
            _rub(totals["expense"]),
            _rub(totals["income"] - totals["expense"]),
        )
    )
    expense_rows = conn.execute(
        "SELECT c.name AS name, SUM(t.amount_kopecks) AS total "
        "FROM transactions AS t JOIN finance_categories AS c ON c.id = t.category_id "
        "WHERE t.kind = 'expense' AND substr(t.ts, 1, 7) = ? "
        "GROUP BY c.id ORDER BY total DESC LIMIT 8",
        (month,),
    ).fetchall()
    parts.append("")
    parts.append(_md_list(["%s — %s ₽" % (row["name"], _rub(row["total"])) for row in expense_rows]))
    budget_rows = conn.execute(
        "SELECT c.name AS name, b.limit_kopecks AS limit_kopecks, "
        " COALESCE((SELECT SUM(t.amount_kopecks) FROM transactions AS t "
        "   WHERE t.category_id = b.category_id AND t.kind = 'expense' "
        "     AND substr(t.ts, 1, 7) = b.month), 0) AS spent "
        "FROM budgets AS b JOIN finance_categories AS c ON c.id = b.category_id "
        "WHERE b.month = ? ORDER BY c.sort, c.name",
        (month,),
    ).fetchall()
    parts.append("")
    parts.append(
        _md_list(
            [
                "%s: %s из %s ₽ (%d%%)"
                % (
                    row["name"],
                    _rub(row["spent"]),
                    _rub(row["limit_kopecks"]),
                    round(int(row["spent"] or 0) / int(row["limit_kopecks"]) * 100)
                    if int(row["limit_kopecks"] or 0) > 0
                    else 0,
                )
                for row in budget_rows
            ]
        )
    )

    # Привычки
    parts.append(_md_heading("Привычки"))
    week = [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    habit_rows = conn.execute(
        "SELECT id, name, target_per_week FROM habits WHERE archived = 0 ORDER BY id"
    ).fetchall()
    marks_by_habit: dict[int, set[str]] = {}
    for row in conn.execute(
        "SELECT habit_id, date FROM habit_marks WHERE date >= ? AND value > 0",
        (week[0],),
    ).fetchall():
        marks_by_habit.setdefault(int(row["habit_id"]), set()).add(str(row["date"]))
    parts.append(
        _md_list(
            [
                "%s — %d из %d за неделю (норма %d)"
                % (
                    _clean(row["name"]),
                    len(marks_by_habit.get(int(row["id"]), set())),
                    len(week),
                    int(row["target_per_week"] or 0),
                )
                for row in habit_rows
            ]
        )
    )

    # Чек-ины
    parts.append(_md_heading("Чек-ины энергии"))
    checkins = conn.execute(
        "SELECT date, energy, mood, note FROM checkins ORDER BY date DESC LIMIT ?",
        (MD_CHECKINS_LIMIT,),
    ).fetchall()
    parts.append(
        _md_list(
            [
                "%s — энергия %d%s%s"
                % (
                    row["date"],
                    int(row["energy"]),
                    " · %s" % _clean(row["mood"]) if row["mood"] else "",
                    " · %s" % _clean(row["note"]) if row["note"] else "",
                )
                for row in checkins
            ]
        )
    )

    # Итоги
    parts.append(_md_heading("Итоги периодов"))
    reviews = conn.execute(
        "SELECT period, start_date, end_date, wins, misses, focus, care FROM reviews "
        "ORDER BY start_date DESC LIMIT ?",
        (MD_REVIEWS_LIMIT,),
    ).fetchall()
    review_lines: list[str] = []
    for row in reviews:
        summary = _clean(row["wins"] or row["focus"] or row["care"] or "")
        review_lines.append(
            "%s %s–%s%s"
            % (
                "неделя" if row["period"] == "week" else "месяц",
                row["start_date"],
                row["end_date"],
                " — %s" % summary if summary else "",
            )
        )
    parts.append(_md_list(review_lines))

    # Журнал
    parts.append(_md_heading("Журнал"))
    journal = conn.execute(
        "SELECT substr(ts, 1, 16) AS ts, kind, text FROM entries "
        "ORDER BY ts DESC LIMIT ?",
        (MD_JOURNAL_LIMIT,),
    ).fetchall()
    parts.append(
        _md_list(
            [
                "%s [%s] %s"
                % (str(row["ts"]).replace("T", " "), str(row["kind"]), _clean(row["text"]))
                for row in journal
            ]
        )
    )

    # Измерения
    parts.append(_md_heading("Последние измерения"))
    metrics = conn.execute(
        "SELECT date, kind, value, unit FROM metrics ORDER BY date DESC, id DESC LIMIT ?",
        (MD_METRICS_LIMIT,),
    ).fetchall()
    parts.append(
        _md_list(
            [
                "%s — %s: %s %s"
                % (row["date"], row["kind"], row["value"], _clean(row["unit"]))
                for row in metrics
            ]
        )
    )

    parts.append("")
    parts.append("_Выгружено из Life OS %s, схема %d._" % (__version__, SCHEMA_VERSION))
    parts.append("")
    return "\n".join(parts)


def _attachment(content: str, *, filename: str, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": 'attachment; filename="%s"' % filename},
    )


@router.get("/export")
def export_data(
    conn: ConnectionDep,
    settings: SettingsDep,
    format: Literal["json", "md"] = Query("json", description="json или md"),
) -> Response:
    """Файл выгрузки: ``json`` — полный снимок, ``md`` — читаемая версия."""
    day = settings.today()
    if format == "md":
        return _attachment(
            _markdown(conn, settings),
            filename="life-os-export-%s.md" % day,
            media_type="text/markdown; charset=utf-8",
        )
    body = json.dumps(
        _payload(conn, settings),
        ensure_ascii=False,
        indent=2,
        default=str,
        sort_keys=False,
    )
    return _attachment(
        body,
        filename="life-os-export-%s.json" % day,
        media_type="application/json; charset=utf-8",
    )
