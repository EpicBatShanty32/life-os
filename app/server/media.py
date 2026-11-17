"""Раздача медиа уроков с поддержкой HTTP Range (``GET /media/{lesson_id}``).

Почему Range важен: браузерный плеер перематывает видео отдельными запросами
``Range: bytes=...`` и ждёт ответ ``206 Partial Content`` с точным
``Content-Range``; без этого перемотка либо не работает, либо качает файл
целиком.

Безопасность:

* путь берётся из ``lessons.media_path`` и всегда проверяется на принадлежность
  каталогу ``settings.media_dir`` после ``resolve()`` — ``..``, абсолютные пути
  и симлинки наружу дают 404, а не чтение чужого файла;
* поддержан только один диапазон за запрос (множественные диапазоны → 416):
  multipart/byteranges браузерному плееру не нужен;
* файл читается кусками по 64 КБ, поэтому гигабайтное видео не попадает в
  память целиком.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.server.api.deps import ConnectionDep, SettingsDep

__all__ = [
    "CHUNK_SIZE",
    "RangeNotSatisfiable",
    "build_media_response",
    "parse_range",
    "resolve_media_path",
    "router",
]

CHUNK_SIZE = 64 * 1024

router = APIRouter(tags=["media"])


class RangeNotSatisfiable(ValueError):
    """Заголовок Range корректен синтаксически, но диапазон вне файла."""

    def __init__(self, size: int) -> None:
        super().__init__("Диапазон вне файла")
        self.size = size


def resolve_media_path(
    media_root: Path,
    media_path: str,
    *,
    project_root: Path | None = None,
) -> Path | None:
    """Возвращает реальный файл внутри ``media_root`` или ``None``.

    Поддерживает оба формата ``media_path`` из БД: относительный к каталогу
    медиа (``2026/11/abc.mp4``, так пишет bot/media_store) и относительный к
    корню проекта (``data/media/2026/11/abc.mp4``). Абсолютный путь допустим,
    только если после ``resolve()`` он лежит внутри ``media_root``.
    """
    raw = (media_path or "").strip()
    if not raw or "\x00" in raw:
        return None
    try:
        root = media_root.expanduser().resolve()
    except OSError:
        return None

    candidate = Path(raw).expanduser()
    candidates: list[Path] = []
    if candidate.is_absolute():
        candidates.append(candidate)
    else:
        candidates.append(root / candidate)
        if project_root is not None:
            candidates.append(project_root.expanduser() / candidate)

    for item in candidates:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if resolved == root or not resolved.is_relative_to(root):
            continue
        if resolved.is_file():
            return resolved
    return None


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Разбирает ``Range: bytes=...`` в инклюзивный ``(start, end)``.

    ``None`` — заголовка нет (отдаём файл целиком). Неизвестные единицы
    измерения игнорируются как того требует RFC 9110. Некорректный или
    выходящий за файл диапазон — :class:`RangeNotSatisfiable`.
    """
    if not header or not header.strip():
        return None
    value = header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[len("bytes=") :].strip()
    if not spec or "," in spec:
        raise RangeNotSatisfiable(size)

    start_text, separator, end_text = spec.partition("-")
    if not separator:
        raise RangeNotSatisfiable(size)
    start_text = start_text.strip()
    end_text = end_text.strip()

    if start_text == "":
        # Суффиксный диапазон: bytes=-500 — последние 500 байт.
        if not end_text.isdigit():
            raise RangeNotSatisfiable(size)
        suffix = int(end_text)
        if suffix <= 0 or size <= 0:
            raise RangeNotSatisfiable(size)
        start = max(0, size - suffix)
        return start, size - 1

    if not start_text.isdigit():
        raise RangeNotSatisfiable(size)
    start = int(start_text)
    if start >= size or size <= 0:
        raise RangeNotSatisfiable(size)

    if end_text == "":
        return start, size - 1
    if not end_text.isdigit():
        raise RangeNotSatisfiable(size)
    end = int(end_text)
    if end < start:
        raise RangeNotSatisfiable(size)
    return start, min(end, size - 1)


def build_media_response(
    path: Path,
    range_header: str | None,
    *,
    chunk_size: int = CHUNK_SIZE,
) -> Response:
    """Собирает 200/206/416-ответ для файла и заголовка Range."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Медиафайл недоступен") from exc

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    base_headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=0"}
    try:
        byte_range = parse_range(range_header, size)
    except RangeNotSatisfiable:
        return Response(
            status_code=416,
            headers={**base_headers, "Content-Range": "bytes */%d" % size},
            media_type="application/json",
            content='{"detail":"Запрошенный диапазон вне файла"}'.encode("utf-8"),
        )

    if byte_range is None:
        headers = {**base_headers, "Content-Length": str(size)}
        return StreamingResponse(
            _iter_file(path, 0, size, chunk_size),
            media_type=media_type,
            headers=headers,
        )

    start, end = byte_range
    length = end - start + 1
    headers = {
        **base_headers,
        "Content-Range": "bytes %d-%d/%d" % (start, end, size),
        "Content-Length": str(length),
    }
    return StreamingResponse(
        _iter_file(path, start, length, chunk_size),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


def _iter_file(path: Path, start: int, length: int, chunk_size: int) -> Iterator[bytes]:
    remaining = length
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    except OSError:
        # Файл исчез между stat и чтением: клиент увидит оборванный ответ,
        # сервер не падает.
        return


@router.get("/media/{lesson_id}")
def serve_lesson_media(
    lesson_id: int,
    request: Request,
    conn: ConnectionDep,
    settings: SettingsDep,
) -> Response:
    """Отдаёт файл урока с Range; 404 — нет урока/файла, 416 — плохой диапазон."""
    row = conn.execute(
        "SELECT media_path FROM lessons WHERE id = ?", (lesson_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Урок не найден")
    media_path = str(row["media_path"] or "")
    if not media_path:
        raise HTTPException(status_code=404, detail="У урока нет медиафайла")

    resolved = resolve_media_path(
        settings.media_dir,
        media_path,
        project_root=settings.root,
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="Медиафайл не найден")

    return build_media_response(resolved, request.headers.get("range"))
