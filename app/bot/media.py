"""Медиа-пайплайн Telegram-бота: извлечение вложений, лимиты и скачивание.

Модуль отделяет «что пришло в сообщении» от aiogram-типов: :func:`extract_media`
работает с любым объектом, у которого есть поля ``photo``/``video``/``voice``
и т.п. (настоящий ``Message`` или фейк в тестах). Это позволяет проверять
раскладку и лимиты без сети.

Лимиты:

* обычный Bot API (``api.telegram.org``) отдаёт боту файлы до 20 МБ;
* локальный Bot API Server (``TELEGRAM_API_BASE`` / ``LIFEOS_TELEGRAM_API_BASE``)
  поднимает лимит до 2 ГБ — :func:`download_limit` выбирает по адресу API.

Файлы складываются через :mod:`app.bot.media_store` (сигнатура, атомарная
запись). Для принятых из Telegram сообщений по умолчанию используется
каталог ``inbox/<YYYY>/<MM>/``; для материалов курса предусмотрен
``<course>/<lesson>/``.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from app.bot import media_store

__all__ = [
    "MediaDescriptor",
    "course_lesson_folder",
    "download",
    "download_limit",
    "extract_media",
    "is_local_api_server",
    "inbox_folder",
    "label_for",
    "storage_id",
]

PUBLIC_API_HOST = "api.telegram.org"
PLAYABLE_KINDS = frozenset({"voice", "audio", "video", "video_note"})
_ENTRY_KIND = {
    "photo": "doc",
    "document": "doc",
    "video": "video",
    "video_note": "video",
    "voice": "voice",
    "audio": "voice",
}
_SLUG_RE = re.compile(r"[^\w.-]+", re.UNICODE)


@dataclass
class MediaDescriptor:
    """Вложение Telegram, приведённое к простому виду для пайплайна."""

    kind: str
    file_id: str
    file_unique_id: str = ""
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    duration: Optional[float] = None
    caption: str = ""
    from_channel: str = ""
    chat_id: Optional[int] = None
    message_id: Optional[int] = None

    @property
    def entry_kind(self) -> str:
        """Разрешённый вид записи журнала: note/food/workout/weight/voice/video/doc."""
        return _ENTRY_KIND.get(self.kind, "doc")

    @property
    def transcribable(self) -> bool:
        """Нужна ли расшифровка: голос/аудио/видео или документ с аудио-видео MIME."""
        if self.kind in PLAYABLE_KINDS:
            return True
        if self.kind == "document" and self.mime_type:
            mime = self.mime_type.lower()
            return mime.startswith("audio/") or mime.startswith("video/")
        return False

    def label(self) -> str:
        return label_for(self)


def extract_media(message: Any, chat_id: Optional[int] = None, message_id: Optional[int] = None) -> Optional[MediaDescriptor]:
    """Достаёт вложение из сообщения (aiogram Message или совместимый фейк).

    Возвращает ``None``, если вложений нет. Ничего не скачивает и не ходит
    в сеть; поля читаются через ``getattr`` с безопасными значениями.
    """
    if message is None:
        return None
    caption = _text(getattr(message, "caption", "") or getattr(message, "text", ""))
    from_channel = channel_title(message)
    resolved_chat = chat_id if chat_id is not None else _as_int(getattr(message, "chat", None))
    resolved_message = message_id if message_id is not None else _as_int(getattr(message, "message_id", None))

    photos = getattr(message, "photo", None)
    if isinstance(photos, (list, tuple)) and photos:
        photo = photos[-1]  # последний размер — самый большой
        descriptor = _descriptor(
            "photo", photo, caption, from_channel, resolved_chat, resolved_message
        )
        if descriptor is not None:
            return descriptor

    for attribute, kind in (
        ("video", "video"),
        ("video_note", "video_note"),
        ("voice", "voice"),
        ("audio", "audio"),
        ("document", "document"),
    ):
        attachment = getattr(message, attribute, None)
        if attachment is not None:
            descriptor = _descriptor(
                kind, attachment, caption, from_channel, resolved_chat, resolved_message
            )
            if descriptor is not None:
                return descriptor
    return None


def download_limit(api_base: Optional[str]) -> int:
    """Лимит скачивания: 20 МБ для публичного Bot API, 2 ГБ для локального."""
    return media_store.LOCAL_MAX_BYTES if is_local_api_server(api_base) else media_store.BOT_API_MAX_BYTES


def is_local_api_server(api_base: Optional[str]) -> bool:
    """Отличает локальный Bot API Server от api.telegram.org."""
    value = str(api_base or "").strip().lower()
    if not value:
        return False
    without_scheme = value.split("://", 1)[-1]
    host = without_scheme.split("/", 1)[0].split(":", 1)[0]
    return host not in ("", PUBLIC_API_HOST, "api.telegram.org")


def inbox_folder(now: Optional[datetime] = None) -> Path:
    """Каталог для неразобранных вложений: ``inbox/<YYYY>/<MM>``."""
    moment = now or datetime.now().astimezone()
    return Path("inbox") / ("%04d" % moment.year) / ("%02d" % moment.month)


def course_lesson_folder(course: Any, lesson: Any) -> Path:
    """Каталог материала курса: ``<course>/<lesson>`` (безопасные slug-имена)."""
    return Path(_slug(course, "course")) / _slug(lesson, "lesson")


def storage_id(descriptor: MediaDescriptor) -> str:
    """Безопасный идентификатор для имени файла (file_unique_id или ids сообщения)."""
    raw = re.sub(r"[^A-Za-z0-9._-]", "", str(descriptor.file_unique_id or ""))[:100]
    if raw and not raw.startswith(".") and ".." not in raw:
        return "tg-" + raw
    if descriptor.chat_id is not None and descriptor.message_id is not None:
        return "tg-%s-%s" % (descriptor.chat_id, descriptor.message_id)
    raw_file = re.sub(r"[^A-Za-z0-9._-]", "", str(descriptor.file_id or ""))[:60]
    if raw_file and not raw_file.startswith(".") and ".." not in raw_file:
        return "tg-file-" + raw_file
    return "tg-media"


async def download(bot: Any, descriptor: MediaDescriptor, limit: int) -> bytes:
    """Скачивает файл через Bot API с проверкой лимита; сеть — только здесь."""
    if descriptor.size is not None:
        media_store.check_size(int(descriptor.size), max_bytes=limit)
    try:
        file = await bot.get_file(descriptor.file_id)
        remote_size = getattr(file, "file_size", None)
        if remote_size is not None:
            media_store.check_size(int(remote_size), max_bytes=limit)
        buffer = await bot.download_file(file.file_path)
    except media_store.MediaError:
        raise
    except Exception as exc:
        raise media_store.MediaError(
            "download_failed",
            "Не удалось скачать файл из Telegram (%s). Попробуйте ещё раз."
            % type(exc).__name__,
        ) from exc
    data = buffer.read() if hasattr(buffer, "read") else buffer
    if not isinstance(data, (bytes, bytearray)):
        raise media_store.MediaError("read_failed", "Telegram вернул файл неизвестного типа.")
    payload = bytes(data)
    media_store.check_size(len(payload), max_bytes=limit)
    return payload


def label_for(descriptor: MediaDescriptor) -> str:
    """Человеческая подпись вложения для записи журнала и ответа бота."""
    if descriptor.kind == "photo":
        return "Фото"
    if descriptor.kind == "video":
        return "Видео"
    if descriptor.kind == "video_note":
        return "Видеосообщение"
    if descriptor.kind == "voice":
        return "Голосовое сообщение"
    if descriptor.kind == "audio":
        return "Аудио"
    name = (descriptor.file_name or "").strip()
    return "Документ: %s" % name if name else "Документ"


def _descriptor(
    kind: str,
    attachment: Any,
    caption: str,
    from_channel: str,
    chat_id: Optional[int],
    message_id: Optional[int],
) -> Optional[MediaDescriptor]:
    file_id = str(getattr(attachment, "file_id", "") or "").strip()
    if not file_id:
        return None
    duration = getattr(attachment, "duration", None)
    try:
        duration_value = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_value = None
    size = getattr(attachment, "file_size", None)
    try:
        size_value = int(size) if size is not None else None
    except (TypeError, ValueError):
        size_value = None
    return MediaDescriptor(
        kind=kind,
        file_id=file_id,
        file_unique_id=str(getattr(attachment, "file_unique_id", "") or ""),
        file_name=_optional_str(getattr(attachment, "file_name", None)),
        mime_type=_optional_str(getattr(attachment, "mime_type", None)),
        size=size_value,
        duration=duration_value,
        caption=caption,
        from_channel=from_channel,
        chat_id=chat_id,
        message_id=message_id,
    )


def channel_title(message: Any) -> str:
    """Название канала для пересланного сообщения (пусто, если это не канал)."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None)
        title = getattr(chat, "title", None) or getattr(chat, "username", None)
        if title:
            return str(title)[:120]
        sender = getattr(origin, "sender_chat", None)
        title = getattr(sender, "title", None) or getattr(sender, "username", None)
        if title:
            return str(title)[:120]
    chat = getattr(message, "forward_from_chat", None)
    if chat is not None:
        title = getattr(chat, "title", None) or getattr(chat, "username", None)
        if title:
            return str(title)[:120]
    return ""


def _slug(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    text = _SLUG_RE.sub("-", text)
    text = text.strip(".-_")
    text = re.sub(r"-{2,}", "-", text)
    return text[:60] or fallback


def _text(value: Any) -> str:
    return str(value or "").strip()[:4000]


def _optional_str(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(getattr(value, "id", value))
    except (TypeError, ValueError):
        return None
