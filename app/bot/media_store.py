"""Медиахранилище Life OS v3: раскладка, лимиты, тип по сигнатуре, атомарная запись.

Модуль принимает байты из Telegram (или локальный файл), проверяет размер,
определяет настоящий тип по сигнатуре (расширение из имени файла не считается
доказательством) и кладёт оригинал в::

    <media_root>/<YYYY>/<MM>/<media_id>.<ext>

Запись атомарная: данные сначала пишутся во временный файл в том же каталоге,
затем ``os.replace`` подменяет целевой файл. Читатель видит либо старую
версию, либо новую, но не половину файла; при ошибке временный файл
удаляется, а прежний оригинал остаётся нетронутым.

Ограничения:

* Bot API отдаёт боту файлы не больше :data:`BOT_API_MAX_BYTES` (20 МБ);
* локальный сервер Life OS принимает файлы до :data:`LOCAL_MAX_BYTES` (2 ГБ).

Модуль ничего не знает про aiogram, сеть и БД. Только stdlib и файловая
система, поэтому тесты работают на временных каталогах.
"""

import hashlib
import io
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Dict, NamedTuple, Optional, Union

__all__ = [
    "BOT_API_MAX_BYTES",
    "LOCAL_MAX_BYTES",
    "KIND_AUDIO",
    "KIND_DOCUMENT",
    "KIND_PHOTO",
    "KIND_VIDEO",
    "MediaError",
    "Signature",
    "build_relative_path",
    "check_size",
    "detect_kind",
    "fallback_extension",
    "fits_bot_api",
    "format_size",
    "normalize_kind_hint",
    "read_signature",
    "save_bytes",
    "save_file",
    "save_stream",
    "sanitize_extension",
    "sanitize_media_id",
    "sanitize_relative_folder",
    "sniff",
]

#: Лимит скачивания Bot API: файлы больше 20 МБ бот получить не может.
BOT_API_MAX_BYTES = 20 * 1024 * 1024
#: Лимит локального сервера Life OS для загруженных вручную файлов.
LOCAL_MAX_BYTES = 2 * 1024 * 1024 * 1024

#: Сколько первых байт достаточно, чтобы уверенно определить формат.
SIGNATURE_HEAD_BYTES = 64
#: Размер порции при потоковой записи (не держим файл в памяти целиком).
DEFAULT_CHUNK_SIZE = 64 * 1024

KIND_PHOTO = "photo"
KIND_AUDIO = "audio"
KIND_VIDEO = "video"
KIND_DOCUMENT = "document"

_KIND_TITLES = {
    KIND_PHOTO: "изображение",
    KIND_AUDIO: "аудио",
    KIND_VIDEO: "видео",
    KIND_DOCUMENT: "документ",
}

# Идентификатор попадает в имя файла, поэтому пропускаем только безопасный
# минимум: латиница, цифры, точка, подчёркивание, дефис. Первый символ —
# буква или цифра, чтобы имя не начиналось с точки (скрытые файлы) и не было
# похоже на «.»/«..».
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_EXT_RE = re.compile(r"^[a-z0-9]{1,10}$")
_MAX_ID_LENGTH = 120

# Расширение нужно только для имени файла; настоящий тип всегда берём из
# сигнатуры. Если сигнатура неизвестна, сохраняем документ с расширением из
# исходного имени (тоже безопасным), иначе — .bin.
_HINT_ALIASES = {
    "photo": KIND_PHOTO,
    "image": KIND_PHOTO,
    "picture": KIND_PHOTO,
    "audio": KIND_AUDIO,
    "voice": KIND_AUDIO,
    "music": KIND_AUDIO,
    "video": KIND_VIDEO,
    "video_note": KIND_VIDEO,
    "document": KIND_DOCUMENT,
    "file": KIND_DOCUMENT,
}

PathLike = Union[str, os.PathLike]


class MediaError(Exception):
    """Ошибка приёма файла с машинным кодом и текстом, понятным человеку.

    ``code`` нужен боту и API (например, ``too_large`` или ``unknown_type``),
    ``reason`` можно показать владельцу в Telegram как есть.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason

    def as_dict(self) -> Dict[str, Any]:
        """Представление для JSON-ответа API/бота."""
        return {"ok": False, "code": self.code, "reason": self.reason}

    def __repr__(self) -> str:  # без секретов и путей в отладочном выводе
        return "MediaError(code=%r)" % self.code


class Signature(NamedTuple):
    """Настоящий тип файла, определённый по первым байтам."""

    kind: str
    ext: str
    mime: str


def sniff(head: Union[bytes, bytearray]) -> Optional[Signature]:
    """Определяет формат по первым байтам; ``None`` — сигнатура неизвестна."""
    data = bytes(head or b"")[:SIGNATURE_HEAD_BYTES]
    if len(data) < 4:
        return None

    # Изображения Telegram (фото, стикеры, аватарки).
    if data.startswith(b"\xff\xd8\xff"):
        return Signature(KIND_PHOTO, "jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return Signature(KIND_PHOTO, "png", "image/png")
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return Signature(KIND_PHOTO, "gif", "image/gif")
    if data.startswith(b"BM") and len(data) >= 14:
        return Signature(KIND_PHOTO, "bmp", "image/bmp")

    # Контейнеры RIFF: WEBP (фото), WAVE (аудио), AVI (видео).
    if len(data) >= 12 and data[:4] == b"RIFF":
        form = data[8:12]
        if form == b"WEBP":
            return Signature(KIND_PHOTO, "webp", "image/webp")
        if form == b"WAVE":
            return Signature(KIND_AUDIO, "wav", "audio/wav")
        if form == b"AVI ":
            return Signature(KIND_VIDEO, "avi", "video/x-msvideo")

    # Аудио: голосовые Telegram — OGG/Opus, музыка — MP3/M4A/FLAC.
    if data.startswith(b"OggS"):
        return Signature(KIND_AUDIO, "ogg", "audio/ogg")
    if data.startswith(b"fLaC"):
        return Signature(KIND_AUDIO, "flac", "audio/flac")
    if data.startswith(b"ID3"):
        return Signature(KIND_AUDIO, "mp3", "audio/mpeg")
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # 11 бит синхронизации: MPEG-аудио (MP3) или ADTS AAC.
        # У ADTS поле layer = 00, у MP3 — 01/10/11.
        if (data[1] & 0x06) == 0:
            return Signature(KIND_AUDIO, "aac", "audio/aac")
        return Signature(KIND_AUDIO, "mp3", "audio/mpeg")

    # ISO BMFF: MP4/MOV (видео), M4A (аудио), HEIC (фото).
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P ", b"F4A "):
            return Signature(KIND_AUDIO, "m4a", "audio/mp4")
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return Signature(KIND_PHOTO, "heic", "image/heic")
        if brand == b"qt  ":
            return Signature(KIND_VIDEO, "mov", "video/quicktime")
        return Signature(KIND_VIDEO, "mp4", "video/mp4")

    # Matroska/WebM: один EBML-заголовок, различаем по DocType в первых байтах.
    if data.startswith(b"\x1aE\xdf\xa3"):
        if b"webm" in data:
            return Signature(KIND_VIDEO, "webm", "video/webm")
        return Signature(KIND_VIDEO, "mkv", "video/x-matroska")

    # Документы.
    if data.startswith(b"%PDF-"):
        return Signature(KIND_DOCUMENT, "pdf", "application/pdf")
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        return Signature(KIND_DOCUMENT, "zip", "application/zip")
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # Старые DOC/XLS/PPT — общий OLE-контейнер, точный формат не угадать.
        return Signature(KIND_DOCUMENT, "ole", "application/x-ole-storage")
    if data.startswith(b"Rar!\x1a\x07"):
        return Signature(KIND_DOCUMENT, "rar", "application/vnd.rar")
    if data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return Signature(KIND_DOCUMENT, "7z", "application/x-7z-compressed")
    if data.startswith(b"\x1f\x8b"):
        return Signature(KIND_DOCUMENT, "gz", "application/gzip")
    if data.startswith(b"{\\rtf"):
        return Signature(KIND_DOCUMENT, "rtf", "application/rtf")

    return None


def detect_kind(head: Union[bytes, bytearray]) -> Optional[str]:
    """Вид медиа по сигнатуре: photo/audio/video/document или ``None``."""
    signature = sniff(head)
    return signature.kind if signature is not None else None


def read_signature(path: PathLike) -> Optional[Signature]:
    """Читает начало файла и возвращает его сигнатуру (без доверия расширению)."""
    source = Path(path).expanduser()
    try:
        with source.open("rb") as handle:
            head = handle.read(SIGNATURE_HEAD_BYTES)
    except OSError as exc:
        raise MediaError(
            "read_failed",
            "Не удалось прочитать файл %s: %s." % (source.name, exc),
        ) from exc
    return sniff(head)


def sanitize_media_id(media_id: str) -> str:
    """Проверяет идентификатор файла и возвращает его без изменений.

    Идентификатор — часть пути, поэтому ``..``, слэши и служебные символы
    отклоняются, а не «чинятся» молча: после тихой замены два разных файла
    могли бы получить одно имя.
    """
    if not isinstance(media_id, str):
        raise MediaError("bad_media_id", "Идентификатор файла должен быть строкой.")
    text = media_id.strip()
    if not text:
        raise MediaError("bad_media_id", "Пустой идентификатор файла.")
    if len(text) > _MAX_ID_LENGTH:
        raise MediaError(
            "bad_media_id",
            "Слишком длинный идентификатор файла (максимум %d символов)." % _MAX_ID_LENGTH,
        )
    if ".." in text or not _SAFE_ID_RE.match(text):
        raise MediaError(
            "bad_media_id",
            "Недопустимый идентификатор файла: разрешены латиница, цифры, "
            "«.», «_», «-»; имя не может начинаться с точки или содержать «..».",
        )
    return text


def sanitize_extension(extension: str) -> str:
    """Безопасное расширение для имени файла; мусор превращается в ``bin``."""
    text = str(extension or "").strip().lower().lstrip(".")
    return text if _SAFE_EXT_RE.match(text) else "bin"


def sanitize_relative_folder(folder: PathLike) -> Path:
    """Проверяет относительный каталог медиа (например, ``course/lesson``).

    Разрешены вложенные каталоги с буквами (включая кириллицу), цифрами,
    пробелами, точками, дефисами и подчёркиваниями. Абсолютные пути, ``..``
    и скрытые каталоги отклоняются: путь всегда остаётся внутри media_root.
    """
    if folder is None:
        raise MediaError("bad_folder", "Не задан каталог для медиа.")
    try:
        path = Path(str(folder))
    except (TypeError, ValueError):
        raise MediaError("bad_folder", "Некорректный каталог для медиа.") from None
    if path.is_absolute() or getattr(path, "drive", ""):
        raise MediaError("bad_folder", "Каталог медиа должен быть относительным.")
    parts = []
    for part in path.parts:
        if part in ("", ".", ".."):
            raise MediaError(
                "bad_folder",
                "Недопустимый каталог медиа: «..» и пустые части пути запрещены.",
            )
        if part.startswith(".") or "/" in part or "\\" in part or "\x00" in part:
            raise MediaError("bad_folder", "Недопустимый каталог медиа: %r." % part)
        if len(part) > 80:
            raise MediaError(
                "bad_folder",
                "Слишком длинное имя каталога медиа (максимум 80 символов).",
            )
        parts.append(part)
    if not parts:
        raise MediaError("bad_folder", "Пустой каталог медиа.")
    return Path(*parts)


def fallback_extension(filename: Optional[str]) -> str:
    """Расширение из исходного имени — только для документов с неизвестной сигнатурой."""
    if not filename:
        return "bin"
    name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        return "bin"
    return sanitize_extension(name.rsplit(".", 1)[-1])


def format_size(size: int) -> str:
    """Размер по-русски: ``25,4 МБ`` (десятичная запятая)."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return str(size)
    if value < 1024:
        return "%d Б" % int(value)
    for unit in ("КБ", "МБ", "ГБ", "ТБ"):
        value /= 1024.0
        if value < 1024 or unit == "ТБ":
            return ("%.1f %s" % (value, unit)).replace(".", ",")
    return "%d Б" % int(size)


def check_size(size: int, max_bytes: int = LOCAL_MAX_BYTES) -> None:
    """Проверяет размер файла и бросает :class:`MediaError` с понятным текстом."""
    if isinstance(size, bool) or not isinstance(size, int):
        raise TypeError("size должен быть целым числом")
    if size < 0:
        raise ValueError("size не может быть отрицательным")
    if max_bytes <= 0:
        raise ValueError("max_bytes должен быть больше нуля")
    if size == 0:
        raise MediaError("empty", "Пустой файл: сохранять нечего. Пришлите файл с содержимым.")
    if size > max_bytes:
        raise MediaError("too_large", _too_large_reason(size, max_bytes))


def fits_bot_api(size: int) -> bool:
    """Поместится ли файл в лимит скачивания Bot API (20 МБ)."""
    return 0 < int(size) <= BOT_API_MAX_BYTES


def build_relative_path(
    media_id: str,
    extension: str,
    now: Optional[datetime] = None,
) -> Path:
    """Относительный путь ``<YYYY>/<MM>/<media_id>.<ext>`` для хранилища."""
    ident = sanitize_media_id(media_id)
    suffix = sanitize_extension(extension)
    moment = _local_moment(now)
    return (
        Path("%04d" % moment.year)
        / ("%02d" % moment.month)
        / ("%s.%s" % (ident, suffix))
    )


def save_bytes(
    media_root: PathLike,
    media_id: str,
    data: bytes,
    now: Optional[datetime] = None,
    filename: Optional[str] = None,
    kind_hint: Optional[str] = None,
    folder: Optional[PathLike] = None,
    max_bytes: int = LOCAL_MAX_BYTES,
) -> Dict[str, Any]:
    """Сохраняет готовые байты. Возвращает описание файла, ошибки — :class:`MediaError`."""
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise TypeError("data должен быть bytes или bytearray")
    check_size(len(data), max_bytes)
    return save_stream(
        media_root,
        media_id,
        io.BytesIO(data),
        now=now,
        filename=filename,
        kind_hint=kind_hint,
        folder=folder,
        max_bytes=max_bytes,
    )


def save_file(
    media_root: PathLike,
    media_id: str,
    source: PathLike,
    now: Optional[datetime] = None,
    filename: Optional[str] = None,
    kind_hint: Optional[str] = None,
    folder: Optional[PathLike] = None,
    max_bytes: int = LOCAL_MAX_BYTES,
) -> Dict[str, Any]:
    """Сохраняет локальный файл потоком, не загружая его в память целиком."""
    origin = Path(source).expanduser()
    try:
        stat = origin.stat()
    except OSError as exc:
        raise MediaError(
            "not_found",
            "Исходный файл не найден или недоступен: %s." % origin.name,
        ) from exc
    if not origin.is_file():
        raise MediaError("not_found", "Источник не является файлом: %s." % origin.name)
    check_size(stat.st_size, max_bytes)
    try:
        handle = origin.open("rb")
    except OSError as exc:
        raise MediaError(
            "read_failed",
            "Не удалось открыть файл %s: %s." % (origin.name, exc),
        ) from exc
    with handle:
        return save_stream(
            media_root,
            media_id,
            handle,
            now=now,
            filename=filename or origin.name,
            kind_hint=kind_hint,
            folder=folder,
            max_bytes=max_bytes,
        )


def save_stream(
    media_root: PathLike,
    media_id: str,
    stream: BinaryIO,
    now: Optional[datetime] = None,
    filename: Optional[str] = None,
    kind_hint: Optional[str] = None,
    folder: Optional[PathLike] = None,
    max_bytes: int = LOCAL_MAX_BYTES,
) -> Dict[str, Any]:
    """Пишет поток в хранилище атомарно и возвращает описание сохранённого файла.

    Тип определяется по первым байтам, расширение берётся из сигнатуры (для
    документа с неизвестной сигнатурой — из ``filename``). Если Telegram
    заявляет фото/аудио/видео, а сигнатура этого не подтверждает, файл не
    сохраняется: это защита от подмены типа.

    Повторное сохранение с тем же ``media_id`` в том же каталоге заменяет файл
    атомарно (идемпотентная повторная доставка апдейта).
    """
    ident = sanitize_media_id(media_id)
    hint = normalize_kind_hint(kind_hint)
    if max_bytes <= 0:
        raise ValueError("max_bytes должен быть больше нуля")

    root = Path(media_root).expanduser().resolve()
    moment = _local_moment(now)
    if folder is None:
        dest_dir = root / ("%04d" % moment.year) / ("%02d" % moment.month)
    else:
        dest_dir = root / sanitize_relative_folder(folder)
    # Защита от симлинка каталога года/месяца наружу: проверяем до mkdir,
    # чтобы ничего не создавать за пределами хранилища.
    if not _is_inside(dest_dir, root):
        raise MediaError(
            "unsafe_path",
            "Каталог медиа указывает за пределы хранилища — запись отменена.",
        )
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MediaError(
            "write_failed",
            "Не удалось создать каталог хранилища %s: %s. Проверьте права на data/media." %
            (dest_dir, exc),
        ) from exc
    if not _is_inside(dest_dir, root):
        raise MediaError(
            "unsafe_path",
            "Каталог медиа указывает за пределы хранилища — запись отменена.",
        )

    hasher = hashlib.sha256()
    head = bytearray()
    size = 0
    fd = None
    tmp_path = None
    committed = False
    try:
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=".tmp-%s-" % ident, dir=str(dest_dir))
            tmp_path = Path(tmp_name)
        except OSError as exc:
            raise MediaError(
                "write_failed",
                "Не удалось создать временный файл в %s: %s." % (dest_dir, exc),
            ) from exc

        try:
            output = os.fdopen(fd, "wb")
            fd = None
            with output:
                while True:
                    try:
                        chunk = stream.read(DEFAULT_CHUNK_SIZE)
                    except OSError as exc:
                        raise MediaError(
                            "read_failed",
                            "Не удалось прочитать файл из источника: %s." % exc,
                        ) from exc
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise MediaError(
                            "read_failed",
                            "Источник вернул данные неизвестного типа (ожидались bytes).",
                        )
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaError("too_large", _too_large_reason(size, max_bytes))
                    if len(head) < SIGNATURE_HEAD_BYTES:
                        head.extend(chunk[: SIGNATURE_HEAD_BYTES - len(head)])
                    hasher.update(chunk)
                    output.write(chunk)
                if size == 0:
                    raise MediaError(
                        "empty",
                        "Пустой файл: сохранять нечего. Пришлите файл с содержимым.",
                    )
                output.flush()
                os.fsync(output.fileno())
        except OSError as exc:
            raise MediaError(
                "write_failed",
                "Не удалось записать файл в хранилище: %s." % exc,
            ) from exc

        signature = sniff(head)
        if signature is None:
            if hint in (KIND_PHOTO, KIND_AUDIO, KIND_VIDEO):
                raise MediaError(
                    "unknown_type",
                    "Не удалось определить тип файла по содержимому: похоже, файл "
                    "повреждён или это не %s. Файл не сохранён." % _KIND_TITLES[hint],
                )
            kind = KIND_DOCUMENT
            extension = fallback_extension(filename)
            mime = "application/octet-stream"
        else:
            kind = signature.kind
            extension = sanitize_extension(signature.ext)
            mime = signature.mime

        dest = dest_dir / ("%s.%s" % (ident, extension))
        if not _is_inside(dest, root):
            raise MediaError(
                "unsafe_path",
                "Итоговый путь указывает за пределы хранилища — запись отменена.",
            )
        if dest.is_dir():
            raise MediaError(
                "path_conflict",
                "На месте файла уже есть каталог — запись отменена.",
            )
        try:
            os.replace(str(tmp_path), str(dest))
        except OSError as exc:
            raise MediaError(
                "write_failed",
                "Не удалось атомарно заменить файл %s: %s." % (dest.name, exc),
            ) from exc
        committed = True
        _fsync_dir(dest_dir)

        return {
            "ok": True,
            "media_id": ident,
            "kind": kind,
            "mime": mime,
            "ext": extension,
            "size": size,
            "sha256": hasher.hexdigest(),
            "path": str(dest),
            "rel_path": dest.relative_to(root).as_posix(),
            "year": "%04d" % moment.year,
            "month": "%02d" % moment.month,
            "created_at": moment.isoformat(timespec="seconds"),
            "declared_kind": hint,
            "signature_mismatch": bool(hint is not None and hint != kind),
        }
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if not committed and tmp_path is not None:
            try:
                os.unlink(str(tmp_path))
            except OSError:
                pass


def normalize_kind_hint(kind_hint: Optional[str]) -> Optional[str]:
    """Приводит подсказку Telegram (voice/photo/...) к виду photo/audio/video/document."""
    if kind_hint is None:
        return None
    text = str(kind_hint).strip().lower()
    if text not in _HINT_ALIASES:
        raise MediaError("bad_kind", "Неизвестный тип медиа: %r." % kind_hint)
    return _HINT_ALIASES[text]


def _too_large_reason(size: int, max_bytes: int) -> str:
    if max_bytes == BOT_API_MAX_BYTES:
        return (
            "Файл %s — больше лимита Bot API 20 МБ: Telegram не отдаёт такие "
            "файлы боту. Пришлите файл до 20 МБ или загрузите его через "
            "локальный сервер." % format_size(size)
        )
    if max_bytes == LOCAL_MAX_BYTES:
        return (
            "Файл %s — больше лимита локального сервера 2 ГБ. "
            "Пришлите файл меньшего размера." % format_size(size)
        )
    return (
        "Файл %s — больше допустимого размера %s. Пришлите файл меньшего размера."
        % (format_size(size), format_size(max_bytes))
    )


def _local_moment(now: Optional[datetime]) -> datetime:
    """Локальное время для каталога года/месяца (naive — уже локальное)."""
    if now is None:
        return datetime.now().astimezone()
    if not isinstance(now, datetime):
        raise TypeError("now должен быть datetime или None")
    if now.tzinfo is None:
        return now
    return now.astimezone()


def _is_inside(path: Path, root: Path) -> bool:
    """Лежит ли путь внутри корня после раскрытия симлинков."""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _fsync_dir(directory: Path) -> None:
    """Сбрасывает каталог на диск, чтобы переименование пережило сбой.

    На некоторых файловых системах fsync каталога возвращает ошибку; это не
    повод считать сохранение неуспешным.
    """
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
