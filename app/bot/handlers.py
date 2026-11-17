"""Обработчики Telegram-бота Life OS v3.

Логика отделена от aiogram: :class:`BotService` принимает обычные значения
(текст, ``MediaDescriptor``, id чата) и возвращает :class:`Reply` с текстом и
кнопками. Роутер внизу — тонкая обвязка aiogram: whitelist, скачивание файла,
отправка ответа. Тесты вызывают ``BotService`` напрямую, без сети и Telegram.

Что происходит с сообщением:

* текст разбирает :mod:`app.bot.intent` (LLM при наличии ключа, иначе правила);
* запись попадает в ``entries``, найденные числа — в ``metrics``;
* вложение сохраняется через :mod:`app.bot.media_store` (сигнатура, атомарная
  запись), для голоса/аудио/видео ставится задача ``transcribe`` в ``jobs``;
* ответ — короткое подтверждение с кнопками «Удалить» и «Категория».

Все записи журнала проверяются на допустимые виды:
``note|food|workout|weight|voice|video|doc`` (photo хранится как doc).
"""

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot import intent, media, media_store
from app.server import db, jobs
from app.server import transcribe as transcribe_module

logger = logging.getLogger("lifeos.bot.handlers")

__all__ = ["BotService", "Reply", "build_router", "entry_kind_label", "format_metrics"]

ENTRY_KINDS = frozenset({"note", "food", "workout", "weight", "voice", "video", "doc"})
CATEGORY_BY_KIND = {
    "note": "Заметка",
    "food": "Питание",
    "workout": "Тренировка",
    "weight": "Тело",
    "voice": "Голос",
    "video": "Видео",
    "doc": "Документ",
}
KIND_BY_CATEGORY_BUTTON = (
    ("note", "Заметка"),
    ("food", "Питание"),
    ("workout", "Тренировка"),
    ("weight", "Тело"),
)
#: Числовые метрики интента -> (kind в таблице metrics, единица).
METRIC_COLUMNS = {
    "weight": ("weight", "кг"),
    "fat": ("fat", "%"),
    "muscle": ("muscle", "кг"),
    "water": ("water", "мл"),
    "sleep": ("sleep", "ч"),
    "calories": ("calories", "ккал"),
    "protein": ("protein", "г"),
    "duration_min": ("workout", "мин"),
    "mood": ("mood", "из 10"),
}
MAX_TEXT_CHARS = 4000


@dataclass
class Reply:
    """Ответ бота: текст, inline-кнопки и служебные флаги."""

    text: str
    buttons: List[List[Tuple[str, str]]] = field(default_factory=list)
    notification: str = ""
    hide_keyboard: bool = False
    ignore: bool = False


class BotService:
    """Сервис обработки сообщений: БД, медиа и очередь задач.

    Один экземпляр живёт на процесс; ``threading.Lock`` защищает SQLite от
    одновременных записей из потока LLM-разбора и event loop aiogram.
    """

    def __init__(
        self,
        settings: Any,
        conn: Any,
        intent_router: Optional[Any] = None,
        transcriber: Optional[Any] = None,
        media_root: Optional[Path] = None,
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.intent_router = intent_router or intent.build_router(settings)
        self.transcriber = transcriber
        self.media_root = Path(media_root or getattr(settings, "media_dir"))
        self.download_limit = media.download_limit(
            getattr(settings, "telegram_api_base", "https://api.telegram.org")
        )
        self.toolchain = transcribe_module.toolchain_status()
        self._db_lock = threading.Lock()

    # -- доступ -----------------------------------------------------------

    def is_allowed(self, chat_id: Optional[int], user_id: Optional[int] = None) -> bool:
        """Whitelist ADMIN_TELEGRAM_ID: пустой id означает «никому нельзя»."""
        try:
            allowed = int(getattr(self.settings, "allowed_chat_id", 0) or 0)
        except (TypeError, ValueError):
            return False
        if allowed == 0:
            return False
        return chat_id == allowed or (user_id is not None and user_id == allowed)

    # -- команды ----------------------------------------------------------

    def handle_start(self) -> Reply:
        lines = [
            "Привет! Я Life OS — личный дневник в Telegram.",
            "",
            "Пиши обычным текстом: «вес 81.2, жир 18.5», «вода 300», «спал 7.5», "
            "«жим 80х5», «съел 600 ккал и 40 г белка».",
            "Присылай голосовые, видео и документы — сохраню оригинал и поставлю "
            "расшифровку в очередь.",
            "",
            "Команды: /help — подсказка, /stats — статистика.",
        ]
        if not self.toolchain.get("ready"):
            missing = ", ".join(self.toolchain.get("missing") or [])
            lines.append("")
            lines.append(
                "Расшифровка пока недоступна: не хватает %s. Файлы сохраняются, "
                "текст появится после установки инструментов." % (missing or "ffmpeg/whisper")
            )
        return Reply("\n".join(lines))

    def handle_help(self) -> Reply:
        return Reply(
            "Что я умею:\n"
            "• текст → запись в журнал и метрики (вес, жир, вода, сон, еда, тренировка);\n"
            "• голос/аудио/видео → файл в data/media + задача на расшифровку;\n"
            "• документы и фото → файл в data/media, запись в журнал;\n"
            "• пересланные из канала сообщения принимаю как обычные.\n\n"
            "Команды:\n"
            "/start — знакомство\n"
            "/stats — что записано и состояние очереди\n"
            "/help — эта подсказка\n\n"
            "Кнопки под подтверждением: «Удалить» и «Категория»."
        )

    def handle_stats(self) -> Reply:
        today = self._today()
        with self._db_lock:
            entries_today = self._scalar(
                "SELECT COUNT(*) FROM entries WHERE date = ?", (today,)
            )
            metrics_today = self._scalar(
                "SELECT COUNT(*) FROM metrics WHERE date = ?", (today,)
            )
            jobs_rows = self.conn.execute(
                "SELECT status, COUNT(*) AS amount FROM jobs GROUP BY status ORDER BY status"
            ).fetchall()
            entries_total = self._scalar("SELECT COUNT(*) FROM entries", ())
        jobs_text = ", ".join(
            "%s: %d" % (str(row[0]), int(row[1])) for row in jobs_rows
        ) or "нет"
        media_count, media_bytes = self._media_stats()
        if self.toolchain.get("ready"):
            transcription = "готова (ffmpeg + whisper)"
        else:
            transcription = "нет: %s" % (", ".join(self.toolchain.get("missing") or ["ffmpeg/whisper"]))
        lines = [
            "📊 Статистика Life OS",
            "Сегодня (%s): записей %d, метрик %d." % (today, entries_today, metrics_today),
            "Всего записей: %d." % entries_total,
            "Очередь задач: %s." % jobs_text,
            "Медиа: %d файлов, %s." % (media_count, media_store.format_size(media_bytes)),
            "Транскрибация: %s." % transcription,
        ]
        return Reply("\n".join(lines))

    # -- текст ------------------------------------------------------------

    def handle_text(
        self,
        text: str,
        chat_id: Optional[int],
        message_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> Reply:
        if not self.is_allowed(chat_id, user_id):
            return Reply("", ignore=True)
        clean = (text or "").strip()
        if not clean:
            return Reply("Пустое сообщение — нечего записывать.")
        if len(clean) > MAX_TEXT_CHARS:
            return Reply("Слишком длинное сообщение (больше %d символов)." % MAX_TEXT_CHARS)

        parsed = self.intent_router.parse(clean)
        kind = str(parsed.get("kind") or "note")
        if kind not in ENTRY_KINDS:
            kind = "note"
        metrics = parsed.get("metrics") if isinstance(parsed.get("metrics"), Mapping) else {}
        title = str(parsed.get("title") or clean[:80])
        category = str(parsed.get("category") or CATEGORY_BY_KIND.get(kind, "Заметка"))

        meta = {
            "title": title,
            "category": category,
            "intent": str(parsed.get("engine") or "rules"),
            "metrics": _json_safe(metrics),
            "chat_id": chat_id,
            "message_id": message_id,
        }
        try:
            with self._db_lock, self.conn:
                entry_id = db.add_entry(
                    self.conn, kind, clean, ts=self._now(), source="telegram", meta=meta
                )
                for name, value in metrics.items():
                    column = METRIC_COLUMNS.get(str(name))
                    if column is None:
                        continue
                    number = _as_float(value)
                    if number is None:
                        continue
                    db.add_metric(
                        self.conn,
                        column[0],
                        number,
                        date=self._today(),
                        unit=column[1],
                        source="telegram",
                    )
        except Exception as exc:
            logger.exception("не удалось записать текст в БД")
            return Reply("⚠️ Не смог сохранить запись: %s." % _short_error(exc))

        return Reply(
            "✅ Записал: %s\n%s" % (title, format_metrics(metrics)),
            buttons=self._entry_buttons(entry_id),
        )

    # -- медиа ------------------------------------------------------------

    def handle_media(
        self,
        descriptor: media.MediaDescriptor,
        content: bytes,
        chat_id: Optional[int],
        message_id: Optional[int] = None,
        user_id: Optional[int] = None,
        now: Optional[Any] = None,
    ) -> Reply:
        if not self.is_allowed(chat_id, user_id):
            return Reply("", ignore=True)
        moment = now or self._now()
        limit = self.download_limit
        if descriptor.size is not None:
            try:
                media_store.check_size(int(descriptor.size), max_bytes=limit)
            except media_store.MediaError as exc:
                return Reply("⚠️ " + exc.reason)
        try:
            media_store.check_size(len(content), max_bytes=limit)
        except media_store.MediaError as exc:
            return Reply("⚠️ " + exc.reason)

        try:
            stored = media_store.save_bytes(
                self.media_root,
                media.storage_id(descriptor),
                content,
                now=moment,
                filename=descriptor.file_name,
                kind_hint=descriptor.kind,
                folder=media.inbox_folder(moment),
                max_bytes=limit,
            )
        except media_store.MediaError as exc:
            return Reply("⚠️ " + exc.reason)
        except Exception as exc:
            logger.exception("не удалось сохранить медиа")
            return Reply("⚠️ Не смог сохранить файл: %s." % _short_error(exc))

        entry_kind = descriptor.entry_kind
        text = (descriptor.caption or media.label_for(descriptor)).strip()[:MAX_TEXT_CHARS]
        meta = {
            "title": media.label_for(descriptor),
            "category": CATEGORY_BY_KIND.get(entry_kind, "Документ"),
            "intent": "media",
            "chat_id": chat_id,
            "message_id": message_id,
            "media": {
                "rel_path": stored["rel_path"],
                "kind": descriptor.kind,
                "mime": stored["mime"],
                "size": stored["size"],
                "sha256": stored["sha256"],
                "file_name": descriptor.file_name,
                "duration": descriptor.duration,
                "from_channel": descriptor.from_channel,
            },
        }
        try:
            with self._db_lock, self.conn:
                entry_id = db.add_entry(
                    self.conn, entry_kind, text, ts=moment, source="telegram", meta=meta
                )
        except Exception as exc:
            logger.exception("не удалось записать медиа в БД")
            return Reply("⚠️ Файл сохранён, но запись в журнал не удалась: %s." % _short_error(exc))

        job_id = None
        if descriptor.transcribable:
            payload = {
                "entry_id": entry_id,
                "media_path": stored["rel_path"],
                "media_id": stored["media_id"],
                "media_kind": descriptor.kind,
                "chat_id": chat_id,
                "message_id": message_id,
                "language": "ru",
                "duration": descriptor.duration,
                "from_channel": descriptor.from_channel,
            }
            try:
                job_id = jobs.enqueue(
                    self.conn,
                    "transcribe",
                    payload,
                    meta={"progress": 0.0, "message": "в очереди"},
                )
            except Exception:
                logger.exception("не удалось поставить задачу transcribe")

        lines = [
            "📥 Сохранил: %s — %s." % (media.label_for(descriptor), media_store.format_size(stored["size"])),
            "📁 data/media/%s" % stored["rel_path"],
        ]
        if descriptor.transcribable:
            if job_id is None:
                lines.append("⚠️ Расшифровку поставить в очередь не удалось.")
            elif self.toolchain.get("ready"):
                lines.append("🎙 Расшифровка в очереди (задача №%d)." % job_id)
            else:
                missing = ", ".join(self.toolchain.get("missing") or ["ffmpeg/whisper"])
                lines.append(
                    "🎙 Расшифровка в очереди (задача №%d), но пока недоступна: "
                    "нет %s. Файл сохранён — текст появится после установки."
                    % (job_id, missing)
                )
        return Reply("\n".join(lines), buttons=self._entry_buttons(entry_id))

    # -- inline-кнопки ----------------------------------------------------

    def handle_callback(
        self,
        data: str,
        chat_id: Optional[int],
        user_id: Optional[int] = None,
    ) -> Reply:
        if not self.is_allowed(chat_id, user_id):
            return Reply("", ignore=True)
        parts = str(data or "").split(":")
        if len(parts) < 3 or parts[0] != "e":
            return Reply("", notification="Кнопка устарела.")
        action = parts[1]
        try:
            entry_id = int(parts[2])
        except (TypeError, ValueError):
            return Reply("", notification="Кнопка устарела.")

        with self._db_lock:
            row = self.conn.execute(
                "SELECT id, kind, text FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        if row is None:
            return Reply("", notification="Запись уже удалена.", hide_keyboard=True)

        if action == "del":
            try:
                with self._db_lock, self.conn:
                    self.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            except Exception as exc:
                logger.exception("не удалось удалить запись")
                return Reply("", notification="Не удалось удалить: %s" % _short_error(exc))
            return Reply("🗑 Запись удалена.", hide_keyboard=True, notification="Удалено")

        if action == "cat":
            buttons = []
            current = str(row[1])
            for kind, label in KIND_BY_CATEGORY_BUTTON:
                mark = "• " if kind == current else ""
                buttons.append([(mark + label, "e:set:%d:%s" % (entry_id, kind))])
            buttons.append([("Отмена", "e:noop:%d" % entry_id)])
            return Reply("Выбери категорию записи #%d:" % entry_id, buttons=buttons)

        if action == "set" and len(parts) >= 4:
            kind = parts[3]
            if kind not in ENTRY_KINDS:
                return Reply("", notification="Неизвестная категория.")
            try:
                with self._db_lock, self.conn:
                    self.conn.execute(
                        "UPDATE entries SET kind = ? WHERE id = ?", (kind, entry_id)
                    )
            except Exception as exc:
                logger.exception("не удалось изменить категорию")
                return Reply("", notification="Не удалось изменить: %s" % _short_error(exc))
            return Reply(
                "🏷 Категория записи #%d: %s." % (entry_id, entry_kind_label(kind)),
                buttons=self._entry_buttons(entry_id),
                notification="Сохранено",
            )

        if action == "noop":
            return Reply("", notification="Ок")
        return Reply("", notification="Кнопка устарела.")

    # -- помощники --------------------------------------------------------

    def _entry_buttons(self, entry_id: int) -> List[List[Tuple[str, str]]]:
        return [
            [
                ("🗑 Удалить", "e:del:%d" % entry_id),
                ("🏷 Категория", "e:cat:%d" % entry_id),
            ]
        ]

    def _today(self) -> str:
        try:
            return str(self.settings.today())
        except Exception:
            return str(self._now().date().isoformat())

    def _now(self) -> Any:
        try:
            return self.settings.now()
        except Exception:
            from datetime import datetime

            return datetime.now().astimezone()

    def _scalar(self, sql: str, params: Sequence[Any]) -> int:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row is not None else 0

    def _media_stats(self) -> Tuple[int, int]:
        count = 0
        total = 0
        root = self.media_root
        try:
            for item in root.rglob("*"):
                if item.is_file():
                    count += 1
                    total += item.stat().st_size
        except OSError:
            pass
        return count, total


def format_metrics(metrics: Mapping[str, Any]) -> str:
    """Человеческая строка метрик: «вес 81,2 кг · жир 18,5 %»."""
    labels = {
        "weight": ("вес", "кг"),
        "fat": ("жир", "%"),
        "muscle": ("мышцы", "кг"),
        "water": ("вода", "мл"),
        "sleep": ("сон", "ч"),
        "calories": ("калории", "ккал"),
        "protein": ("белок", "г"),
        "duration_min": ("тренировка", "мин"),
        "mood": ("настроение", "/10"),
        "exercise": ("упражнение", ""),
        "load_kg": ("вес снаряда", "кг"),
        "reps": ("повторения", ""),
    }
    parts = []
    for key, value in metrics.items():
        label = labels.get(str(key))
        if label is None:
            continue
        name, unit = label
        if key in ("exercise",):
            parts.append("%s %s" % (name, value))
        else:
            number = _as_float(value)
            if number is None:
                continue
            text = ("%.2f" % number).rstrip("0").rstrip(".").replace(".", ",")
            parts.append(("%s %s %s" % (name, text, unit)).strip())
    return " · ".join(parts) if parts else ""


def entry_kind_label(kind: str) -> str:
    return CATEGORY_BY_KIND.get(str(kind), "Заметка")


# --------------------------------------------------------------------------
# aiogram-роутер: тонкая обвязка над BotService
# --------------------------------------------------------------------------


def build_router(service: BotService) -> Router:
    """Собирает Router с обработчиками команд, текста, медиа и кнопок."""
    router = Router(name="lifeos-bot")

    def _allowed(message: Any) -> bool:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        user = getattr(message, "from_user", None)
        user_id = getattr(user, "id", None)
        if service.is_allowed(chat_id, user_id):
            return True
        logger.warning("чужой chat_id=%r — сообщение игнорирую", chat_id)
        return False

    async def _send(message: Message, reply: Reply) -> None:
        if reply.ignore or not reply.text:
            return
        markup = _markup(reply)
        try:
            await message.answer(reply.text, reply_markup=markup)
        except Exception:
            logger.exception("не удалось отправить ответ в чат")

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        if not _allowed(message):
            return
        await _send(message, await asyncio.to_thread(service.handle_start))

    @router.message(Command("help"))
    async def on_help(message: Message) -> None:
        if not _allowed(message):
            return
        await _send(message, await asyncio.to_thread(service.handle_help))

    @router.message(Command("stats"))
    async def on_stats(message: Message) -> None:
        if not _allowed(message):
            return
        await _send(message, await asyncio.to_thread(service.handle_stats))

    @router.message(F.photo | F.video | F.video_note | F.voice | F.audio | F.document)
    async def on_media(message: Message) -> None:
        if not _allowed(message):
            return
        descriptor = media.extract_media(
            message,
            chat_id=getattr(getattr(message, "chat", None), "id", None),
            message_id=getattr(message, "message_id", None),
        )
        if descriptor is None:
            await _send(message, Reply("Не вижу вложения — пришлите файл ещё раз."))
            return
        try:
            content = await media.download(message.bot, descriptor, service.download_limit)
        except media_store.MediaError as exc:
            await _send(message, Reply("⚠️ " + exc.reason))
            return
        except Exception as exc:
            logger.exception("не удалось скачать медиа из Telegram")
            await _send(message, Reply("⚠️ Не смог скачать файл: %s." % _short_error(exc)))
            return
        reply = await asyncio.to_thread(
            service.handle_media,
            descriptor,
            content,
            getattr(getattr(message, "chat", None), "id", None),
            getattr(message, "message_id", None),
            getattr(getattr(message, "from_user", None), "id", None),
        )
        await _send(message, reply)

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        if not _allowed(message):
            return
        text = str(getattr(message, "text", "") or "")
        if text.startswith("/"):
            await _send(message, await asyncio.to_thread(service.handle_help))
            return
        reply = await asyncio.to_thread(
            service.handle_text,
            text,
            getattr(getattr(message, "chat", None), "id", None),
            getattr(message, "message_id", None),
            getattr(getattr(message, "from_user", None), "id", None),
        )
        await _send(message, reply)

    @router.callback_query(F.data.startswith("e:"))
    async def on_callback(callback: CallbackQuery) -> None:
        chat = getattr(getattr(callback, "message", None), "chat", None)
        chat_id = getattr(chat, "id", None)
        user = getattr(callback, "from_user", None)
        user_id = getattr(user, "id", None)
        if not service.is_allowed(chat_id, user_id):
            logger.warning("чужой callback chat_id=%r — игнорирую", chat_id)
            await callback.answer("Нет доступа.", show_alert=False)
            return
        reply = await asyncio.to_thread(
            service.handle_callback,
            str(getattr(callback, "data", "") or ""),
            chat_id,
            user_id,
        )
        try:
            await callback.answer(reply.notification or "", show_alert=False)
        except Exception:
            pass
        if not reply.text:
            return
        message = getattr(callback, "message", None)
        if message is None:
            return
        markup = _markup(reply)
        try:
            await message.edit_text(reply.text, reply_markup=markup)
        except Exception:
            try:
                await message.answer(reply.text, reply_markup=markup)
            except Exception:
                logger.exception("не удалось обновить сообщение с кнопками")

    @router.message()
    async def on_other(message: Message) -> None:
        if not _allowed(message):
            return
        await _send(
            message,
            Reply(
                "Пока понимаю текст, фото, видео, документы и голосовые. "
                "Стикеры и геолокацию сохранять не умею."
            ),
        )

    return router


def _markup(reply: Reply) -> Optional[InlineKeyboardMarkup]:
    if reply.hide_keyboard:
        # Пустой reply_markup при edit_text убирает кнопки под сообщением.
        return None
    rows = []
    for row in reply.buttons:
        buttons = []
        for label, callback_data in row:
            data = str(callback_data)
            if len(data.encode("utf-8")) > 64:
                logger.warning("callback_data длиннее 64 байт — кнопка пропущена")
                continue
            buttons.append(InlineKeyboardButton(text=str(label), callback_data=data))
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _json_safe(value: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        json.dumps(value, ensure_ascii=False)
        return dict(value)
    except (TypeError, ValueError):
        return {str(key): str(item) for key, item in value.items()}


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip().replace("\n", " ")[:160]
    return text or exc.__class__.__name__
