"""Глобальный поиск: ``GET /api/search?q=&limit=20`` (§12.6, §10.7).

Ищет по задачам, проектам, книгам, цитатам, целям, инбоксу и заметкам
проектов. Ответ — ``{items:[{kind,id,title,snippet,route}], total, limit, q}``;
``kind`` из фиксированного набора §12.6, ``route`` — прямой deep-link на
страницу SPA с якорем объекта.

Регистр сравнивается через ``casefold`` в Python: SQLite ``LIKE``/``lower()``
не меняет регистр кириллицы, а база локальная (тысячи строк), поэтому фильтр
по всем совпадениям дешевле, чем потерянные результаты. Пустой ``q`` — не
ошибка: возвращается пустой список.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.server.api.deps import ConnectionDep

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["search"])

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
TITLE_WIDTH = 140
SNIPPET_WIDTH = 120

#: Порядок типов в выдаче — от «действия» к «справочному».
KIND_ORDER: tuple[str, ...] = ("task", "project", "book", "quote", "goal", "inbox", "note")


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def _snippet(text: Any, needle: str, width: int = SNIPPET_WIDTH) -> str:
    """Контекст вокруг первого совпадения; без совпадения — начало текста."""
    body = _clean(text)
    if not body:
        return ""
    index = body.casefold().find(needle.casefold())
    if index < 0:
        return _truncate(body, width)
    start = max(0, index - 40)
    end = min(len(body), index + len(needle) + 60)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return prefix + body[start:end] + suffix


def _matches(needle: str, *fields: Any) -> bool:
    lowered = needle.casefold()
    return any(lowered in _clean(field).casefold() for field in fields)


def _items(conn: Any, needle: str) -> list[dict[str, Any]]:
    """Все совпадения по типам; сортировка внутри типа — свежее сверху."""
    found: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KIND_ORDER}

    for row in conn.execute(
        "SELECT id, title, notes, tags, status FROM tasks ORDER BY id DESC"
    ).fetchall():
        if _matches(needle, row["title"], row["notes"], row["tags"]):
            title = _truncate(_clean(row["title"]), TITLE_WIDTH)
            found["task"].append(
                {
                    "kind": "task",
                    "id": int(row["id"]),
                    "title": title,
                    "snippet": _snippet(row["notes"] or row["title"], needle),
                    "route": "/today?task=%d" % int(row["id"]),
                }
            )

    for row in conn.execute(
        "SELECT id, name, description, outcome, status FROM projects ORDER BY id DESC"
    ).fetchall():
        if _matches(needle, row["name"], row["description"], row["outcome"]):
            found["project"].append(
                {
                    "kind": "project",
                    "id": int(row["id"]),
                    "title": _truncate(_clean(row["name"]), TITLE_WIDTH),
                    "snippet": _snippet(
                        row["description"] or row["outcome"] or row["name"], needle
                    ),
                    "route": "/projects?project=%d" % int(row["id"]),
                }
            )

    for row in conn.execute(
        "SELECT id, title, author, why, shelf FROM books ORDER BY id DESC"
    ).fetchall():
        if _matches(needle, row["title"], row["author"], row["why"]):
            author = _clean(row["author"])
            found["book"].append(
                {
                    "kind": "book",
                    "id": int(row["id"]),
                    "title": _truncate(_clean(row["title"]), TITLE_WIDTH),
                    "snippet": _snippet(row["why"] or author or row["title"], needle),
                    "route": "/library?book=%d" % int(row["id"]),
                }
            )

    for row in conn.execute(
        "SELECT q.id AS id, q.text AS text, q.page AS page, "
        " b.id AS book_id, b.title AS book_title "
        "FROM book_quotes AS q JOIN books AS b ON b.id = q.book_id "
        "ORDER BY q.id DESC"
    ).fetchall():
        if _matches(needle, row["text"], row["book_title"]):
            page = "стр. %d" % int(row["page"]) if row["page"] is not None else None
            book = _clean(row["book_title"])
            detail = " · ".join(part for part in (book, page) if part)
            found["quote"].append(
                {
                    "kind": "quote",
                    "id": int(row["id"]),
                    "title": _truncate(_clean(row["text"]), TITLE_WIDTH),
                    "snippet": _snippet(detail or row["text"], needle),
                    "route": "/library?book=%d&quote=%d"
                    % (int(row["book_id"]), int(row["id"])),
                }
            )

    for row in conn.execute(
        "SELECT id, title, why, status FROM goals ORDER BY id DESC"
    ).fetchall():
        if _matches(needle, row["title"], row["why"]):
            found["goal"].append(
                {
                    "kind": "goal",
                    "id": int(row["id"]),
                    "title": _truncate(_clean(row["title"]), TITLE_WIDTH),
                    "snippet": _snippet(row["why"] or row["title"], needle),
                    "route": "/compass?goal=%d" % int(row["id"]),
                }
            )

    for row in conn.execute(
        "SELECT id, text FROM inbox_items ORDER BY id DESC"
    ).fetchall():
        if _matches(needle, row["text"]):
            found["inbox"].append(
                {
                    "kind": "inbox",
                    "id": int(row["id"]),
                    "title": _truncate(_clean(row["text"]), TITLE_WIDTH),
                    "snippet": _snippet(row["text"], needle),
                    "route": "/inbox?item=%d" % int(row["id"]),
                }
            )

    for row in conn.execute(
        "SELECT id, project_id, kind, title, body, url FROM project_notes ORDER BY id DESC"
    ).fetchall():
        if _matches(needle, row["title"], row["body"], row["url"]):
            title = _clean(row["title"]) or _clean(row["body"]) or _clean(row["url"])
            found["note"].append(
                {
                    "kind": "note",
                    "id": int(row["id"]),
                    "title": _truncate(title, TITLE_WIDTH),
                    "snippet": _snippet(row["body"] or row["url"] or title, needle),
                    "route": "/projects?project=%d&note=%d"
                    % (int(row["project_id"]), int(row["id"])),
                }
            )

    ordered: list[dict[str, Any]] = []
    for kind in KIND_ORDER:
        ordered.extend(found[kind])
    return ordered


@router.get("/search")
def search(
    conn: ConnectionDep,
    q: str | None = Query(None, max_length=200, description="Строка поиска"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    """Поиск по всем сущностям v4; пустой запрос — пустая выдача без ошибки."""
    needle = _clean(q)
    if not needle:
        return {"q": "", "items": [], "total": 0, "limit": limit, "kinds": {}}
    items = _items(conn, needle)
    kinds: dict[str, int] = {}
    for item in items:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    return {
        "q": needle,
        "items": items[:limit],
        "total": len(items),
        "limit": limit,
        "kinds": kinds,
    }
