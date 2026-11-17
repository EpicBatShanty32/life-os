"""``GET /api/journal`` — журнал с пагинацией, фильтром по типу и FTS5-поиском.

Без ``q`` — обычная выборка по убыванию времени. С ``q`` — поиск по
полнотекстовому индексу ``entries_fts`` (см. :func:`app.server.db.search_entries`):
операторы FTS5 в запрос пользователя не попадают, поэтому строка вида
``«вес OR сон»`` ищется как два слова и не может сломать SQL.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.server.api.deps import ConnectionDep
from app.server.db import fts_query, search_entries
from app.server.models import Entry

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["journal"])

_MAX_FTS_RESULTS = 200


@router.get("/journal")
def list_journal(
    conn: ConnectionDep,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    kind: str | None = Query(None, max_length=40, description="Тип записи (food, training...)"),
    q: str | None = Query(None, max_length=200, description="Поиск по тексту через FTS5"),
) -> dict[str, Any]:
    """Список записей: свежие сверху, всего не больше 100 за страницу."""
    query_text = (q or "").strip()
    if query_text:
        return _search(conn, query_text, limit=limit, offset=offset, kind=kind)
    return _plain(conn, limit=limit, offset=offset, kind=kind)


def _plain(conn, *, limit: int, offset: int, kind: str | None) -> dict[str, Any]:
    where = ""
    params: list[Any] = []
    if kind:
        where = " WHERE kind = ?"
        params.append(kind)

    total_row = conn.execute("SELECT count(*) FROM entries" + where, params).fetchone()
    total = int(total_row[0]) if total_row is not None else 0
    rows = conn.execute(
        "SELECT * FROM entries" + where + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {
        "items": [Entry.from_row(row).model_dump() for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "kind": kind,
        "q": None,
    }


def _search(
    conn,
    query_text: str,
    *,
    limit: int,
    offset: int,
    kind: str | None,
) -> dict[str, Any]:
    match = fts_query(query_text)
    if not match:
        return {
            "items": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
            "kind": kind,
            "q": query_text,
        }

    count_sql = (
        "SELECT count(*) FROM entries_fts "
        "JOIN entries AS e ON e.id = entries_fts.rowid "
        "WHERE entries_fts MATCH ?"
    )
    count_params: list[Any] = [match]
    if kind:
        count_sql += " AND e.kind = ?"
        count_params.append(kind)
    total_row = conn.execute(count_sql, count_params).fetchone()
    total = int(total_row[0]) if total_row is not None else 0

    # search_entries ограничивает выборку 200 строками, поэтому глубокая
    # пагинация по поиску не нужна и не поддерживается.
    fetch_limit = min(_MAX_FTS_RESULTS, max(limit + offset, limit))
    rows = search_entries(conn, query_text, limit=fetch_limit, kind=kind)
    window = rows[offset : offset + limit]
    return {
        "items": [Entry.from_row(row).model_dump() for row in window],
        "total": total,
        "limit": limit,
        "offset": offset,
        "kind": kind,
        "q": query_text,
    }
