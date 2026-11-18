"""Обработчики Telegram-бота Life OS v4.

Бот — главный ввод системы (``studio/BOT-MAIN.md``): одно сообщение — одна
запись, ничего не теряется, подтверждение одной строкой, постоянная
клавиатура «Дело · Расход · Еда · Привычка · Мысль · Сводка».

Логика отделена от aiogram: :class:`BotService` принимает обычные значения
(текст, ``MediaDescriptor``, id чата) и возвращает :class:`Reply` с текстом и
кнопками. Роутер внизу — тонкая обвязка aiogram: whitelist, скачивание файла,
отправка ответа, reply-клавиатура. Тесты вызывают ``BotService`` напрямую, без
сети и Telegram.

Что происходит с сообщением:

* текст разбирает :mod:`app.bot.intent_v4` (LLM при наличии ключа, иначе
  правила): дело, деньги, книга, мысль, привычка, чек-ин, правка/удаление;
* старые фразы v3 (вес, вода, сон, еда, тренировка) разбирает
  :mod:`app.bot.intent` и записывает в те же таблицы;
* запись идёт в общую БД v4 (:mod:`app.bot.store_v4`), поэтому сразу видна в
  API и на экранах веба;
* вложение сохраняется через :mod:`app.bot.media_store` (сигнатура, атомарная
  запись), для голоса/аудио/видео ставится задача ``transcribe`` в ``jobs``;
* ответ — короткая строка «что записано» и кнопки «Изменить/Удалить».
"""

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.bot import agent as agent_module
from app.bot import intent, intent_v4, media, media_store, nutrition_v4, points_v4, reminders, store_v4
from app.server import db, jobs, usage
from app.server import transcribe as transcribe_module

logger = logging.getLogger("lifeos.bot.handlers")

__all__ = [
    "BotService",
    "MAIN_KEYBOARD_ROWS",
    "Reply",
    "build_router",
    "entry_kind_label",
    "format_metrics",
]

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
#: Ключ в ``settings_kv``: владелец, если ``allowed_chat_id`` не задан в конфиге.
OWNER_KEY = "telegram.owner_chat_id"
#: Ключ в settings_kv: вопрос модели с кнопками «как сохранить?».
CLARIFY_KEY = "bot:clarify"

#: Сколько ждём расшифровку и как часто смотрим очередь задач.
TRANSCRIPT_TIMEOUT_S = 60.0
TRANSCRIPT_POLL_S = 0.5
#: Статус задачи, когда расшифровка отложена до включения движка.
PENDING_TOOLCHAIN = "pending_toolchain"

#: Постоянная reply-клавиатура (§3 BOT-MAIN): два ряда по три кнопки.
MAIN_KEYBOARD_ROWS: Tuple[Tuple[str, ...], ...] = (
    ("Дело", "Расход", "Еда"),
    ("Привычка", "Мысль", "Сводка"),
)

MEAL_LABELS = {
    "breakfast": "завтрак",
    "lunch": "обед",
    "dinner": "ужин",
    "snack": "перекус",
}


@dataclass
class Reply:
    """Ответ бота: текст, inline-кнопки, reply-клавиатура и служебные флаги."""

    text: str
    buttons: List[List[Tuple[str, str]]] = field(default_factory=list)
    notification: str = ""
    hide_keyboard: bool = False
    ignore: bool = False
    keyboard: List[List[str]] = field(
        default_factory=lambda: [list(row) for row in MAIN_KEYBOARD_ROWS]
    )
    #: Задача расшифровки, которую надо дождаться (голос/аудио/видео).
    job_id: int = 0
    entry_id: int = 0
    wait_transcript: bool = False


@dataclass
class TranscriptResult:
    """Итог ожидания расшифровки: статус, текст или честная причина."""

    status: str
    text: str = ""
    reason: str = ""


class BotService:
    """Сервис обработки сообщений: БД v4, медиа и очередь задач.

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
        v4_router: Optional[Any] = None,
        agent: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.intent_router = intent_router or intent.build_router(settings)
        self.v4_router = v4_router or intent_v4.build_router(settings)
        #: Автономный агент (модель с инструментами). ``None`` — работаем
        #: прежним разбором: бот не должен зависеть от наличия ключа.
        self.agent = agent
        #: Шлюз для оценки КБЖУ продуктов, которых нет в справочнике.
        self.food_gateway = getattr(self.v4_router, "gateway", None)
        #: Модель-формулировщик ответов по готовым числам (может отсутствовать).
        self.answer_writer = getattr(self.v4_router, "answer_writer", None)
        self.transcriber = transcriber
        self.media_root = Path(media_root or getattr(settings, "media_dir"))
        self.download_limit = media.download_limit(
            getattr(settings, "telegram_api_base", "https://api.telegram.org")
        )
        self.toolchain = transcribe_module.toolchain_status(
            engine=getattr(settings, "transcribe_engine", None)
        )
        #: Ожидание расшифровки: тесты уменьшают, чтобы не спать секундами.
        self.transcript_timeout = TRANSCRIPT_TIMEOUT_S
        self.transcript_poll = TRANSCRIPT_POLL_S
        self._db_lock = threading.Lock()
        #: Расписание напоминаний и ритуалов: состояние в общей БД, тот же
        #: замок, что у обработчиков (цикл напоминаний живёт в другом потоке).
        self.reminders = reminders.ReminderEngine(settings, conn, self._db_lock)

    # -- расшифровка голоса ------------------------------------------------

    def _transcribe_off(self) -> bool:
        """Расшифровка выключена на устройстве, а не «не хватает компонентов»."""
        return not self.toolchain.get("enabled", True)

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

    # -- клавиатура -------------------------------------------------------

    @staticmethod
    def keyboard_rows() -> List[List[str]]:
        """Постоянная клавиатура: кнопки шлют текст, слэш не нужен."""
        return [list(row) for row in MAIN_KEYBOARD_ROWS]

    # -- статистика использования -----------------------------------------

    def record_usage(
        self,
        screen: str,
        action: str = "message",
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Событие использования бота — в ту же таблицу, что и веб-экраны.

        ``screen`` — имя интента/команды (``task``, ``journal``, ``/today``),
        канал всегда ``bot``. Ошибка статистики не влияет на ответ.
        """
        try:
            with self._db_lock:
                usage.record_event(
                    self.conn,
                    source="bot",
                    screen=screen,
                    action=action,
                    ts=self._now().isoformat(timespec="seconds"),
                    meta=meta,
                )
        except Exception:  # pragma: no cover - статистика не ломает бота
            logger.exception("не удалось записать событие использования бота")

    def record_command(self, command: str) -> None:
        """Команда бота (``/today``, ``/week``, ``/stats``…) как отдельный экран."""
        self.record_usage(command, "command")

    # -- команды ----------------------------------------------------------

    def handle_start(self) -> Reply:
        lines = [
            "Life OS на связи. Пиши обычным текстом — разложу по местам.",
            "",
            "Дело: «завтра позвонить в банк», «в пятницу стоматолог в 15».",
            "Деньги: «такси 450», «зарплата 120000».",
            "Питание и тело: «600 ккал и 40 г белка», «вес 81.2, жир 18.5».",
            "Привычка: «выпил 2 л», «пробежал 5 км», «не курил».",
            "Мысль: просто напиши, что думаешь.",
            "",
            "Команды: /today — день, /week — итоги, /morning и /evening — ритуалы, "
            "/reminders — расписание, /mute — тишина, /help — подсказка.",
            "Утром и вечером я сам напишу: напоминать буду по расписанию.",
        ]
        if self._transcribe_off():
            lines.append("")
            lines.append(
                "Расшифровка голоса выключена на этом устройстве: "
                "файл сохраню, текста не будет."
            )
        elif not self.toolchain.get("ready"):
            missing = ", ".join(self.toolchain.get("missing") or [])
            lines.append("")
            lines.append(
                "Расшифровка голоса пока недоступна: не хватает %s. "
                "Файл сохраню, текст появится после установки." % (missing or "ffmpeg/whisper")
            )
        return Reply("\n".join(lines))

    def handle_help(self) -> Reply:
        return Reply(
            "Что я умею:\n"
            "• текст → дело, расход/доход, книга, мысль, привычка, чек-ин;\n"
            "• «готово» / «сделал X» → закрыть дело;\n"
            "• «перенеси на завтра», «это в работу», «удали последнее»;\n"
            "• «подышал 3 раунда», «задержка 2 минуты» → практика и очки;\n"
            "• /points — баланс очков, уровень и серия практик;\n"
            "• /today — сводка дня, /week — итоги недели, /weight — вес и тренд, "
            "/backup — копия базы файлом;\n"
            "• /morning и /evening — ритуалы сейчас, /reminders — расписание, /mute — тишина;\n"
            "• голос/аудио/видео → файл и расшифровка, фото и документы → файл;\n"
            "• всё непонятное уйдёт в инбокс, текст не потеряется.\n\n"
            "Кнопки внизу: Дело · Расход · Еда · Привычка · Мысль · Сводка.\n"
            "Команды: /start, /today, /week, /points, /weight, /morning, /evening, "
            "/reminders, /mute, /unmute, /stats, /backup, /help."
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
            tasks_today = self._scalar(
                "SELECT COUNT(*) FROM tasks WHERE due_date = ? "
                "AND status <> 'cancelled'",
                (today,),
            )
            money_today = self._scalar(
                "SELECT COUNT(*) FROM transactions WHERE substr(ts, 1, 10) = ?",
                (today,),
            )
            jobs_rows = self.conn.execute(
                "SELECT status, COUNT(*) AS amount FROM jobs GROUP BY status ORDER BY status"
            ).fetchall()
            entries_total = self._scalar("SELECT COUNT(*) FROM entries", ())
        jobs_text = ", ".join(
            "%s: %d" % (str(row[0]), int(row[1])) for row in jobs_rows
        ) or "нет"
        media_count, media_bytes = self._media_stats()
        if self._transcribe_off():
            transcription = "выключена на этом устройстве"
        elif self.toolchain.get("ready"):
            transcription = "готова (ffmpeg + whisper)"
        else:
            transcription = "нет: %s" % (", ".join(self.toolchain.get("missing") or ["ffmpeg/whisper"]))
        lines = [
            "Статистика Life OS",
            "Сегодня (%s): записей %d, метрик %d, дел %d, транзакций %d."
            % (today, entries_today, metrics_today, tasks_today, money_today),
            "Всего записей журнала: %d." % entries_total,
            "Очередь задач: %s." % jobs_text,
            "Медиа: %d файлов, %s." % (media_count, media_store.format_size(media_bytes)),
            "Транскрибация: %s." % transcription,
        ]
        return Reply("\n".join(lines))

    def handle_today(self) -> Reply:
        """``/today``: дела, деньги, еда/вода/сон, привычки, чек-ин."""
        try:
            with self._db_lock:
                text = store_v4.today_summary(self.conn, self.settings)
        except Exception as exc:
            logger.exception("не удалось собрать сводку дня")
            return Reply("Не смог собрать сводку: %s." % _short_error(exc))
        return Reply(text)

    def handle_week(self) -> Reply:
        """``/week``: итоги недели цифрами."""
        try:
            with self._db_lock:
                text = store_v4.week_summary(self.conn, self.settings)
        except Exception as exc:
            logger.exception("не удалось собрать итоги недели")
            return Reply("Не смог собрать итоги: %s." % _short_error(exc))
        return Reply(text)

    def handle_weight(self) -> Reply:
        """``/weight``: последние 10 замеров веса и тренд (две-три строки)."""
        try:
            with self._db_lock:
                text = store_v4.weight_summary(self.conn, self.settings)
        except Exception as exc:
            logger.exception("не удалось собрать замеры веса")
            return Reply("Не смог посмотреть вес: %s." % _short_error(exc))
        self.record_usage("/weight", "command")
        return Reply(text)

    def handle_points(self) -> Reply:
        """``/points``: баланс, уровень, серия и что дало очки сегодня."""
        try:
            with self._db_lock:
                text = points_v4.points_report(self.conn, self.settings)
        except Exception as exc:
            logger.exception("не удалось посчитать очки")
            return Reply("Не смог посчитать очки: %s." % _short_error(exc))
        self.record_usage("/points", "command")
        return Reply(text)

    # -- напоминания и ритуалы ---------------------------------------------

    def handle_mute(self) -> Reply:
        """``/mute``: выключить напоминания до ``/unmute``."""
        try:
            text = self.reminders.mute()
        except Exception as exc:
            logger.exception("не удалось выключить напоминания")
            return Reply("Не смог выключить напоминания: %s." % _short_error(exc))
        self.record_usage("reminders", "mute")
        return Reply(text)

    def handle_unmute(self) -> Reply:
        """``/unmute``: включить напоминания обратно."""
        try:
            text = self.reminders.unmute()
        except Exception as exc:
            logger.exception("не удалось включить напоминания")
            return Reply("Не смог включить напоминания: %s." % _short_error(exc))
        self.record_usage("reminders", "unmute")
        return Reply(text)

    def handle_undo(self) -> Reply:
        """``/undo``: откат последнего действия агента, затем старой записи."""
        try:
            with self._db_lock:
                removed, label = agent_module.undo_last(self.conn)
        except Exception:
            logger.exception("не удалось откатить последнее действие")
            removed, label = False, ""
        if removed:
            self.record_usage("undo", "agent")
            return Reply("Отменил: %s." % label)
        try:
            with self._db_lock:
                record = store_v4.last_record(self.conn)
                if record:
                    removed, label = store_v4.delete_record(self.conn, record)
                    if removed:
                        store_v4.clear_last(self.conn)
        except Exception:
            logger.exception("не удалось откатить последнюю запись")
            removed, label = False, ""
        if removed:
            self.record_usage("undo", "record")
            return Reply("Отменил: %s." % label)
        return Reply("Отменять нечего — журнал пуст.")

    def handle_reminders(self, text: str = "") -> Reply:
        """``/reminders`` — расписание; ``/reminders morning 08:30`` — правка."""
        argument = ""
        body = str(text or "").strip()
        if body.startswith("/"):
            chunks = body.split(maxsplit=1)
            argument = chunks[1].strip() if len(chunks) > 1 else ""
        try:
            answer = self.reminders.configure(argument) if argument else self.reminders.describe()
        except Exception as exc:
            logger.exception("не удалось показать расписание напоминаний")
            return Reply("Не смог показать расписание: %s." % _short_error(exc))
        self.record_usage("reminders", "settings" if argument else "describe")
        return Reply(answer)

    def handle_morning(self) -> Reply:
        """``/morning``: утренний ритуал сейчас, по просьбе владельца."""
        return self._ritual_reply("morning")

    def handle_evening(self) -> Reply:
        """``/evening``: вечерний итог сейчас, по просьбе владельца."""
        return self._ritual_reply("evening")

    def _ritual_reply(self, kind: str) -> Reply:
        try:
            outgoing = self.reminders.manual(kind)
        except Exception as exc:
            logger.exception("не удалось собрать ритуал %s", kind)
            return Reply("Не смог собрать ритуал: %s." % _short_error(exc))
        if outgoing is None:
            return Reply(
                "Писать некому: в config/config.json не задан telegram.allowed_chat_id."
            )
        self.record_usage("ritual", kind)
        return Reply(outgoing.text, buttons=outgoing.buttons)

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

        return self._understand(clean, chat_id, message_id)

    def _agent_reply(self, clean: str) -> Optional[Reply]:
        """Первая попытка — агент с инструментами; ``None`` — решают правила.

        Любой сбой (нет ключа, сеть, пустой ответ модели) тихо возвращает
        ``None``: человек получит ответ старого разбора, а не ошибку.
        """
        agent = getattr(self, "agent", None)
        if agent is None:
            return None
        try:
            if not agent.available():
                return None
            result = agent.respond(self.conn, clean, lock=self._db_lock)
        except Exception:
            logger.exception("агент упал — разбираю фразу правилами")
            return None
        if result is None:
            logger.warning("агент не ответил — разбираю фразу правилами")
            return None
        logger.info(
            "агент ответил за %.1f c, инструменты: %s",
            result.latency_ms / 1000.0,
            ", ".join(result.tools_used()) or "без инструментов",
        )
        self.record_usage(
            "agent",
            "message",
            {
                "engine": result.engine,
                "tools": result.tools_used(),
                "latency_ms": result.latency_ms,
            },
        )
        return Reply(result.text)

    def _understand(
        self,
        clean: str,
        chat_id: Optional[int],
        message_id: Optional[int] = None,
    ) -> Reply:
        """Разбор фразы и учёт использования — общий вход для текста и голоса."""
        agent_reply = self._agent_reply(clean)
        if agent_reply is not None:
            return agent_reply
        started = time.perf_counter()
        try:
            parsed = self.v4_router.parse(clean)
        except Exception:
            logger.exception("v4-роутер упал — кладу сообщение в инбокс")
            parsed = {"intent": "inbox", "text": clean, "engine": "rules"}
        if not isinstance(parsed, Mapping):
            parsed = {"intent": "inbox", "text": clean, "engine": "rules"}
        latency_ms = int((time.perf_counter() - started) * 1000)
        intent_name = str(parsed.get("intent") or "inbox")
        screen = intent_name
        meta: dict[str, Any] = {
            "intent": intent_name,
            "engine": str(parsed.get("engine") or ""),
            "latency_ms": latency_ms,
            "memo": clean[:60],
        }
        if intent_name == "legacy":
            legacy = parsed.get("legacy")
            if isinstance(legacy, Mapping):
                legacy_kind = str(legacy.get("kind") or "").strip()
                if legacy_kind:
                    screen = legacy_kind
                meta["legacy"] = legacy_kind
        self.record_usage(screen, "message", meta)
        return self._dispatch(parsed, clean, chat_id, message_id)

    def _dispatch(
        self,
        parsed: Mapping[str, Any],
        clean: str,
        chat_id: Optional[int],
        message_id: Optional[int] = None,
    ) -> Reply:
        """Раскладывает разобранное намерение в запись, ответ или инбокс.

        Общий путь для текста и расшифровки голоса: сказал «на обед гречка
        200» — получил ту же запись еды, что и напечатал бы.
        """
        intent_name = str(parsed.get("intent") or "inbox")
        # Ответ моделью формулируем только если разбор был на правилах:
        # один вызов модели на сообщение (парс ИЛИ ответ, не оба).
        allow_llm_answer = str(parsed.get("engine") or "") != "llm"
        if intent_name == "today":
            return self._ask_reply(
                {"intent": "query", "topic": "day", "query_kind": "today", "date": self._today()},
                allow_llm=allow_llm_answer,
            )
        if intent_name == "week":
            return self._ask_reply(
                {"intent": "query", "topic": "week", "query_kind": "week"},
                allow_llm=allow_llm_answer,
            )
        if intent_name == "greeting":
            return Reply(
                "Пиши что угодно — разложу. Кнопки внизу помогают не вспоминать команды."
            )
        if intent_name == "mode_hint":
            return Reply(str(parsed.get("hint") or "Напиши детали."))
        if intent_name == "finance_summary":
            return self._ask_reply(
                {
                    "intent": "query",
                    "topic": "money",
                    "query_kind": "spent",
                    "period": str(parsed.get("period") or "week"),
                },
                allow_llm=allow_llm_answer,
            )
        if intent_name == "query":
            return self._ask_reply(parsed, allow_llm=allow_llm_answer)
        if intent_name == "clarify":
            question = str(parsed.get("question") or "Уточни, пожалуйста.").strip()
            return self._clarify_reply(parsed, clean, question)
        if intent_name == "delete_last":
            return self._ask_delete_last()
        if intent_name == "legacy":
            legacy = parsed.get("legacy")
            if isinstance(legacy, Mapping):
                return self._handle_legacy(legacy, clean, chat_id, message_id)
        if intent_name == "food":
            return self._handle_food(parsed, clean)
        if intent_name in {"journal", "inbox"}:
            # Страховка: модель могла назвать еду мыслью. Если в тексте есть
            # «продукт + граммы», еда важнее — иначе владелец теряет запись.
            food = intent_v4.parse_food(clean)
            if food and food.get("food_items"):
                return self._handle_food(food, clean)

        outcome: Optional[store_v4.Outcome] = None
        try:
            with self._db_lock, self.conn:
                outcome = store_v4.apply_intent(self.conn, self.settings, parsed)
                if outcome is not None and outcome.operations:
                    store_v4.remember_last(self.conn, outcome)
        except Exception as exc:
            logger.exception("не удалось записать сообщение в БД")
            return Reply("Не смог сохранить: %s." % _short_error(exc))

        if outcome is None:
            # Намерение не про запись (неизвестный/пустой) — в инбокс.
            return self._save_inbox(clean)
        return self._outcome_reply(outcome)

    # -- еда --------------------------------------------------------------

    def _handle_food(self, parsed: Mapping[str, Any], clean: str) -> Reply:
        """Еда по граммам: справочник, КБЖУ, строки дневника и итог дня.

        Одна фраза может дать несколько строк ``food_log`` — по строке на
        продукт; все они попадают в «последнюю запись», поэтому «Удалить»
        убирает всю фразу целиком.
        """
        try:
            moment = self._now()
            meal, _label = _meal_from_text(
                clean, moment.hour, hint=str(parsed.get("meal") or "")
            )
            payload = dict(parsed)
            payload["meal"] = meal
            payload["text"] = clean
            with self._db_lock, self.conn:
                result = nutrition_v4.log_food(
                    self.conn,
                    self.settings,
                    payload,
                    now=moment,
                    gateway=self.food_gateway,
                )
                if result.operations:
                    store_v4.remember_last(
                        self.conn,
                        store_v4.Outcome(
                            text=result.text,
                            operations=result.operations,
                            label=result.label,
                            edit_hint=result.edit_hint,
                        ),
                    )
        except Exception as exc:
            logger.exception("не удалось записать еду")
            return Reply("Не смог сохранить еду: %s." % _short_error(exc))
        buttons: List[List[Tuple[str, str]]] = [list(row) for row in result.buttons]
        if result.operations:
            buttons.append([("Изменить", "v4:edit:last"), ("Удалить", "v4:del:ask")])
        return Reply(text=result.text, buttons=buttons)

    # -- старые фразы v3 --------------------------------------------------

    def _handle_legacy(
        self,
        parsed: Mapping[str, Any],
        clean: str,
        chat_id: Optional[int],
        message_id: Optional[int],
    ) -> Reply:
        """Замеры/тренировки v3 — в общую БД, с отменой и кнопками."""
        kind = str(parsed.get("kind") or "note")
        if kind not in ENTRY_KINDS:
            kind = "note"
        metrics = parsed.get("metrics") if isinstance(parsed.get("metrics"), Mapping) else {}
        title = str(parsed.get("title") or clean[:80])
        category = str(parsed.get("category") or CATEGORY_BY_KIND.get(kind, "Заметка"))
        operations: List[Dict[str, Any]] = []
        reply_text = "Записал: %s" % title
        if kind == "food":
            # Еда v3 и еда v4 — один путь: строки food_log с граммами и КБЖУ.
            return self._handle_food(_food_payload(parsed), clean)
        try:
            with self._db_lock, self.conn:
                meta = {
                    "title": title,
                    "category": category,
                    "intent": str(parsed.get("engine") or "rules"),
                    "metrics": _json_safe(metrics),
                    "chat_id": chat_id,
                    "message_id": message_id,
                }
                entry_id = db.add_entry(
                    self.conn, kind, clean, ts=self._now(), source="telegram", meta=meta
                )
                operations.append({"type": "entry", "id": int(entry_id)})
                for name, value in metrics.items():
                    column = METRIC_COLUMNS.get(str(name))
                    if column is None:
                        continue
                    number = _as_float(value)
                    if number is None:
                        continue
                    if column[0] == "water":
                        # «вода 100» и «выпил 2 л» — один смысл, одно хранилище:
                        # metrics kind='water' (мл) + отметка привычки, если она
                        # есть. Ответ говорит итог дня, а не тип записи.
                        water_text, water_ops = store_v4.log_water(
                            self.conn, self.settings, number
                        )
                        operations.extend(water_ops)
                        reply_text = water_text
                        continue
                    metric_id = db.add_metric(
                        self.conn,
                        column[0],
                        number,
                        date=self._today(),
                        unit=column[1],
                        source="telegram",
                    )
                    operations.append({"type": "metric", "id": int(metric_id)})
                outcome = store_v4.Outcome(
                    text=reply_text,
                    operations=operations,
                    label="запись «%s»" % title,
                    edit_hint="Пришли новую запись — старую удали кнопкой.",
                )
                store_v4.remember_last(self.conn, outcome)
        except Exception as exc:
            logger.exception("не удалось записать текст в БД")
            return Reply("Не смог сохранить: %s." % _short_error(exc))
        return self._outcome_reply(outcome)

    # -- ответы и отмена --------------------------------------------------

    def _outcome_reply(self, outcome: store_v4.Outcome) -> Reply:
        buttons: List[List[Tuple[str, str]]] = []
        if outcome.operations:
            buttons = [
                [("Изменить", "v4:edit:last"), ("Удалить", "v4:del:ask")],
            ]
        return Reply(text=outcome.text, buttons=buttons)

    def _ask_reply(
        self,
        query: Mapping[str, Any],
        detail: bool = False,
        allow_llm: bool = True,
    ) -> Reply:
        """Короткий ответ на вопрос о данных + кнопки «Подробнее» и «Ещё».

        Код всегда считает ответ по БД; модель может только переписать готовые
        числа человеческим языком (и не имеет права добавлять свои). Модель
        зовём один раз и только если разбор фразы был на правилах.
        """
        try:
            with self._db_lock:
                answer = store_v4.answer_question(self.conn, self.settings, query, detail=detail)
        except Exception as exc:
            logger.exception("не удалось ответить на вопрос")
            return Reply("Не смог посмотреть: %s." % _short_error(exc))
        try:
            with self._db_lock, self.conn:
                store_v4.save_last_ask(self.conn, query, detail=detail)
        except Exception:
            logger.exception("не удалось запомнить последний вопрос")
        page = _web_url(self.settings, answer.web_route)
        body = answer.detail if detail and answer.detail else answer.text
        writer = self.answer_writer
        if allow_llm and not detail and writer is not None:
            started = time.perf_counter()
            rewritten = None
            try:
                rewritten = writer.rewrite(
                    answer.text,
                    answer.detail,
                    topic=str(query.get("topic") or query.get("query_kind") or ""),
                )
            except Exception:
                logger.exception("LLM-ответ упал — оставляю ответ кода")
            latency_ms = int((time.perf_counter() - started) * 1000)
            engine = "llm" if rewritten else "rules"
            if rewritten and rewritten.get("text"):
                body = str(rewritten["text"])
            self.record_usage(
                str(query.get("intent") or "query"),
                "answer",
                {
                    "stage": "answer",
                    "engine": engine,
                    "latency_ms": latency_ms,
                    "error": str(getattr(writer, "last_error", "") or ""),
                },
            )
        return Reply(body, buttons=_ask_buttons(page, detail))

    def _clarify_reply(
        self,
        parsed: Mapping[str, Any],
        clean: str,
        question: str,
    ) -> Reply:
        """Один вопрос с кнопками: варианты модели применяются по клику.

        Ответ модели с низкой уверенностью не пишет запись сразу и не уходит
        в Инбокс: человек выбирает «это дело / это мысль / в инбокс».
        """
        options = parsed.get("options")
        rows: List[List[Tuple[str, str]]] = []
        candidates: List[Dict[str, Any]] = []
        if isinstance(options, (list, tuple)):
            for entry in options:
                if not isinstance(entry, Mapping):
                    continue
                label = str(entry.get("label") or "").strip()
                candidate = entry.get("parsed")
                if not label or not isinstance(candidate, Mapping):
                    continue
                candidates.append(dict(candidate))
                rows.append([(label[:40], "v4:pick:%d" % (len(candidates) - 1))])
        if candidates:
            try:
                with self._db_lock, self.conn:
                    db.set_setting(
                        self.conn,
                        CLARIFY_KEY,
                        json.dumps({"text": clean, "options": candidates}, ensure_ascii=False),
                    )
            except Exception:
                logger.exception("не удалось сохранить варианты уточнения")
                rows = []
        if not rows:
            return Reply(question, buttons=_pair_buttons(options))
        return Reply(question, buttons=rows)

    def _handle_clarify_pick(self, action: str) -> Reply:
        """Клик по варианту уточнения: применяем выбранный разбор."""
        try:
            index = int(action.split(":", 2)[1])
        except (IndexError, ValueError):
            return Reply("", notification="Кнопка устарела.")
        try:
            with self._db_lock:
                raw = db.get_setting(self.conn, CLARIFY_KEY)
                db.set_setting(self.conn, CLARIFY_KEY, "")
            payload = json.loads(raw) if raw else {}
        except Exception:
            logger.exception("не удалось прочитать варианты уточнения")
            payload = {}
        options = payload.get("options") if isinstance(payload, Mapping) else None
        if not isinstance(options, list) or not 0 <= index < len(options):
            return Reply("", notification="Вопрос устарел — напиши фразу заново.")
        chosen = options[index]
        if not isinstance(chosen, Mapping):
            return Reply("", notification="Кнопка устарела.")
        text = str(payload.get("text") or chosen.get("text") or "")
        return self._dispatch(dict(chosen), text, None)

    def _save_inbox(self, text: str) -> Reply:
        try:
            with self._db_lock, self.conn:
                outcome = store_v4.save_inbox(self.conn, self.settings, text)
                store_v4.remember_last(self.conn, outcome)
        except Exception as exc:
            logger.exception("не удалось положить сообщение в инбокс")
            return Reply("Не смог сохранить: %s." % _short_error(exc))
        return self._outcome_reply(outcome)

    def _ask_delete_last(self) -> Reply:
        try:
            with self._db_lock, self.conn:
                record = store_v4.last_record(self.conn)
                if not record:
                    return Reply("Пока нечего удалять.")
                store_v4.save_pending(self.conn, record)
        except Exception as exc:
            logger.exception("не удалось подготовить удаление")
            return Reply("Не смог подготовить удаление: %s." % _short_error(exc))
        label = str(record.get("label") or "последнюю запись")
        return Reply(
            "Удалить %s?" % label,
            buttons=[[("Удалить", "v4:del:confirm"), ("Отмена", "v4:del:cancel")]],
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
                return Reply(exc.reason)
        try:
            media_store.check_size(len(content), max_bytes=limit)
        except media_store.MediaError as exc:
            return Reply(exc.reason)

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
            return Reply(exc.reason)
        except Exception as exc:
            logger.exception("не удалось сохранить медиа")
            return Reply("Не смог сохранить файл: %s." % _short_error(exc))

        entry_kind = descriptor.entry_kind
        text = (descriptor.caption or media.label_for(descriptor)).strip()[:MAX_TEXT_CHARS]
        # Голос/аудио/видео без подписи: в журнале честно видно, что текст ещё
        # в очереди, — потом его заменит расшифровка (``wait_for_transcript``).
        if descriptor.transcribable and not descriptor.caption and self.toolchain.get("ready"):
            text = (text + " · расшифровка в очереди")[:MAX_TEXT_CHARS]
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
            return Reply("Файл сохранён, но запись в журнал не удалась: %s." % _short_error(exc))

        # Фото еды с подписью: ту же подпись разбираем как обычную фразу и
        # пишем приём пищи в food_log, а файл привязываем к нему в meta.
        caption = (descriptor.caption or "").strip()
        meal_food = None
        meal = ""
        if descriptor.kind == "photo" and caption:
            meal_food, meal = self._photo_meal(caption, moment)
        if meal_food is not None and meal_food.operations:
            self._link_meal(
                entry_id,
                meal,
                [
                    int(op["id"])
                    for op in meal_food.operations
                    if str(op.get("type")) == "food"
                ],
            )

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

        media_kind = str(descriptor.kind)
        voice_event = media_kind in ("voice", "audio")
        self.record_usage(
            "voice" if voice_event else "media",
            "voice" if voice_event else "media",
            {
                "kind": media_kind,
                "transcribable": bool(descriptor.transcribable),
                "size": stored["size"],
                "memo": text[:60],
            },
        )

        lines = [
            "Сохранил: %s — %s." % (media.label_for(descriptor), media_store.format_size(stored["size"])),
            "data/media/%s" % stored["rel_path"],
        ]
        if meal_food is not None and meal_food.operations:
            lines.append("Привязал к приёму пищи: %s." % MEAL_LABELS.get(meal, "еда"))
            lines.append("")
            lines.append(meal_food.text)
        elif descriptor.kind == "photo" and not caption:
            lines.append(
                "Что на фото? Напиши «на обед гречка 200 г» — запишу еду с КБЖУ. "
                "Содержимое фото не распознаю."
            )
        if descriptor.transcribable:
            if job_id is None:
                lines.append("Расшифровку поставить в очередь не удалось.")
            elif self._transcribe_off():
                lines.append(
                    "Расшифровка выключена на этом устройстве (задача №%d ждёт "
                    "движка) — текста не будет, файл сохранён." % job_id
                )
            elif self.toolchain.get("ready"):
                lines.append("Расшифровка в очереди (задача №%d)." % job_id)
            else:
                missing = ", ".join(self.toolchain.get("missing") or ["ffmpeg/whisper"])
                lines.append(
                    "Расшифровка в очереди (задача №%d), но пока недоступна: "
                    "нет %s. Файл сохранён — текст появится после установки."
                    % (job_id, missing)
                )
        # Ждём результат только когда расшифровка действительно возможна:
        # при выключенном движке ждать нечего (задача останется в очереди).
        wait = bool(
            descriptor.transcribable
            and job_id
            and not self._transcribe_off()
            and self.toolchain.get("ready")
        )
        buttons = self._entry_buttons(entry_id)
        if meal_food is not None and meal_food.buttons:
            buttons = [list(row) for row in meal_food.buttons] + buttons
        return Reply(
            "\n".join(lines),
            buttons=buttons,
            job_id=int(job_id or 0),
            entry_id=int(entry_id),
            wait_transcript=wait,
        )

    # -- расшифровка голоса ------------------------------------------------

    # -- фото еды ----------------------------------------------------------

    def _photo_meal(self, caption: str, moment: Any) -> Tuple[Optional[Any], str]:
        """Фото еды с подписью: пишет приём пищи тем же путём, что текст.

        Возвращает ``(FoodResult, meal)``; ``(None, meal)`` — подпись не про еду.
        """
        try:
            parsed = intent_v4.parse_food(caption)
        except Exception:
            logger.exception("не удалось разобрать подпись фото как еду")
            return None, ""
        if not parsed or not parsed.get("food_items"):
            return None, ""
        meal, _label = _meal_from_text(
            caption, moment.hour, hint=str(parsed.get("meal") or "")
        )
        payload = dict(parsed)
        payload["meal"] = meal
        payload["text"] = caption
        try:
            with self._db_lock, self.conn:
                result = nutrition_v4.log_food(
                    self.conn,
                    self.settings,
                    payload,
                    now=moment,
                    gateway=self.food_gateway,
                )
                if result.operations:
                    store_v4.remember_last(
                        self.conn,
                        store_v4.Outcome(
                            text=result.text,
                            operations=result.operations,
                            label=result.label,
                            edit_hint=result.edit_hint,
                        ),
                    )
        except Exception:
            logger.exception("не удалось записать еду с фото")
            return None, meal
        if not result.operations:
            return None, meal
        return result, meal

    def _link_meal(self, entry_id: int, meal: str, food_ids: Sequence[int]) -> None:
        """Привязывает фото к приёму пищи: ``meta.meal`` и ``meta.food_log_ids``."""
        if not entry_id:
            return
        try:
            with self._db_lock:
                row = self.conn.execute(
                    "SELECT meta_json FROM entries WHERE id = ?", (int(entry_id),)
                ).fetchone()
                if row is None:
                    return
                try:
                    meta = json.loads(row["meta_json"] or "{}")
                except (TypeError, ValueError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                meta["meal"] = str(meal)
                meta["food_log_ids"] = [int(item) for item in food_ids]
                with self.conn:
                    self.conn.execute(
                        "UPDATE entries SET meta_json = ? WHERE id = ?",
                        (
                            json.dumps(
                                meta,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            int(entry_id),
                        ),
                    )
        except Exception:  # pragma: no cover - привязка не должна ломать ответ
            logger.exception("не удалось привязать фото к приёму пищи")

    def _transcript_state(self, job_id: int) -> TranscriptResult:
        """Смотрит задачу один раз: done → текст, failed → причина."""
        try:
            with self._db_lock:
                row = self.conn.execute(
                    "SELECT status, error, payload_json, meta_json FROM jobs WHERE id = ?",
                    (int(job_id),),
                ).fetchone()
        except Exception as exc:  # БД не должна ронять ожидание
            logger.exception("не удалось прочитать задачу %s", job_id)
            return TranscriptResult("error", reason=_short_error(exc))
        if row is None:
            return TranscriptResult("missing", reason="задача не найдена")
        status = str(row["status"] or "")
        if status == jobs.JOB_DONE:
            return TranscriptResult("done", text=self._transcript_text(row))
        if status == PENDING_TOOLCHAIN and not str(row["error"] or "").strip():
            return TranscriptResult("pending_toolchain", reason="движок выключен")
        if status == jobs.JOB_FAILED:
            return TranscriptResult("failed", reason=str(row["error"] or "").strip())
        if status == PENDING_TOOLCHAIN:
            return TranscriptResult(
                "pending_toolchain", reason=str(row["error"] or "").strip()
            )
        return TranscriptResult("running", reason=status)

    def _transcript_text(self, job_row: Any) -> str:
        """Текст расшифровки: воркер пишет его в ``entries.meta_json``.

        Читаем и ``meta.transcript``, и ``entries.text`` — воркер кладёт текст
        в meta, а сам ``text`` записи заменяет бот, поэтому источники
        равноправны (что найдено первым, то и правда).
        """
        try:
            payload = json.loads(job_row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        entry_id = 0
        if isinstance(payload, Mapping):
            try:
                entry_id = int(payload.get("entry_id") or 0)
            except (TypeError, ValueError):
                entry_id = 0
        if entry_id <= 0:
            return ""
        with self._db_lock:
            entry = self.conn.execute(
                "SELECT text, meta_json FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        if entry is None:
            return ""
        try:
            meta = json.loads(entry["meta_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if isinstance(meta, Mapping):
            transcript = str(meta.get("transcript") or "").strip()
            if transcript:
                return transcript
        text = str(entry["text"] or "").strip()
        # Заглушку «Голосовое сообщение» за расшифровку не считаем.
        if text and "расшифровка в очереди" not in text.casefold():
            if text.casefold() not in {"голосовое сообщение", "аудио", "видео", "документ"}:
                return text
        return ""

    def _remember_transcript_text(self, job_id: int, text: str) -> None:
        """Кладёт расшифровку в запись сразу, не дожидаясь ответа бота.

        Иначе запись в журнале (и в вебе) оставалась бы с заглушкой
        «расшифровка в очереди», пока бот не пришлёт «Услышал: …».
        """
        entry_id = self._entry_id_for_job(job_id)
        if not entry_id:
            return
        transcript = " ".join(str(text or "").split())[:MAX_TEXT_CHARS]
        if not transcript:
            return
        try:
            with self._db_lock:
                row = self.conn.execute(
                    "SELECT text FROM entries WHERE id = ?", (entry_id,)
                ).fetchone()
                if row is None:
                    return
                current = str(row["text"] or "")
                if current and current != transcript and "расшифровка в очереди" not in current.casefold():
                    return  # у записи есть своя подпись — не затираем
                with self.conn:
                    self.conn.execute(
                        "UPDATE entries SET text = ? WHERE id = ?",
                        (transcript, entry_id),
                    )
        except Exception:  # pragma: no cover - запись уже удалили
            logger.exception("не удалось сохранить текст расшифровки в запись")

    def _entry_id_for_job(self, job_id: int) -> int:
        try:
            with self._db_lock:
                row = self.conn.execute(
                    "SELECT payload_json FROM jobs WHERE id = ?", (int(job_id),)
                ).fetchone()
        except Exception:  # pragma: no cover - БД недоступна
            return 0
        if row is None:
            return 0
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            return 0
        if not isinstance(payload, Mapping):
            return 0
        try:
            return int(payload.get("entry_id") or 0)
        except (TypeError, ValueError):
            return 0

    def wait_for_transcript(
        self,
        job_id: int,
        timeout: Optional[float] = None,
        interval: Optional[float] = None,
    ) -> TranscriptResult:
        """Ждёт задачу расшифровки, не блокируя event loop (вызов из потока).

        Возвращает ``done`` с текстом, ``failed``/``pending_toolchain`` с
        причиной или ``timeout`` — тогда бот честно скажет, что не дождался.
        """
        limit = float(self.transcript_timeout if timeout is None else timeout)
        step = max(0.01, float(self.transcript_poll if interval is None else interval))
        deadline = time.monotonic() + max(0.0, limit)
        while True:
            state = self._transcript_state(job_id)
            if state.status in {"done", "failed", "pending_toolchain", "missing"}:
                if state.status == "done" and state.text.strip():
                    self._remember_transcript_text(job_id, state.text)
                return state
            left = deadline - time.monotonic()
            if left <= 0:
                return TranscriptResult("timeout", reason="")
            time.sleep(min(step, left))

    def handle_transcript(
        self,
        entry_id: Optional[int],
        job_id: Optional[int],
        result: Optional[TranscriptResult] = None,
        chat_id: Optional[int] = None,
    ) -> Reply:
        """Принимает расшифровку: текст в запись и тот же разбор, что для текста.

        Голос не становится отдельной сущностью: запись медиа получает текст
        расшифровки, а смысл («на обед гречка 200») уходит в обычный разбор —
        еда, дело, расход и т.д. Если текста нет, бот честно говорит почему.
        """
        state = result if result is not None else (
            self._transcript_state(int(job_id)) if job_id else TranscriptResult("missing")
        )
        if state.status != "done" or not state.text.strip():
            return Reply(self._transcript_failure(state))
        transcript = " ".join(state.text.split())[:MAX_TEXT_CHARS]
        if entry_id:
            try:
                with self._db_lock, self.conn:
                    self.conn.execute(
                        "UPDATE entries SET text = ? WHERE id = ?",
                        (transcript, int(entry_id)),
                    )
            except Exception:
                logger.exception("не удалось записать расшифровку в запись")
        self.record_usage(
            "voice",
            "transcript",
            {"job_id": int(job_id or 0), "entry_id": int(entry_id or 0), "chars": len(transcript)},
        )
        reply = self._understand(transcript, chat_id)
        if reply.ignore:
            return Reply('Услышал: «%s»' % transcript)
        text = "Услышал: «%s»" % transcript
        if reply.text:
            text = "%s\n%s" % (text, reply.text)
        return Reply(text=text, buttons=reply.buttons, notification=reply.notification)

    def _transcript_failure(self, state: TranscriptResult) -> str:
        """Честное объяснение, почему текста нет (без «не хватает ffmpeg»)."""
        reason = (state.reason or "").strip().rstrip(".")
        if state.status == "pending_toolchain":
            if self._transcribe_off():
                return (
                    "Расшифровка выключена на этом устройстве — файл сохранён. "
                    "Напиши текстом, что записать."
                )
            return (
                "Расшифровка пока недоступна%s — файл сохранён. "
                "Напиши текстом, что записать."
                % ((": " + reason) if reason else "")
            )
        if state.status == "failed":
            return (
                "Не расслышал%s — файл сохранён. Напиши текстом, что записать."
                % ((": " + reason) if reason else "")
            )
        if state.status == "timeout":
            return (
                "Расшифровка не пришла за %d с — файл сохранён. "
                "Напиши текстом, что записать." % int(self.transcript_timeout)
            )
        return "Расшифровка не получилась — файл сохранён. Напиши текстом, что записать."

    # -- inline-кнопки ----------------------------------------------------

    def handle_callback(
        self,
        data: str,
        chat_id: Optional[int],
        user_id: Optional[int] = None,
    ) -> Reply:
        if not self.is_allowed(chat_id, user_id):
            return Reply("", ignore=True)
        raw = str(data or "")
        if raw.startswith("r:"):
            return self._handle_reminder_callback(raw)
        if raw.startswith("v4:"):
            return self._handle_v4_callback(raw[3:])
        parts = raw.split(":")
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
                    record = store_v4.last_record(self.conn)
                    if record and any(
                        op.get("type") == "entry" and int(op.get("id") or 0) == entry_id
                        for op in record.get("operations") or []
                        if isinstance(op, Mapping)
                    ):
                        store_v4.clear_last(self.conn)
            except Exception as exc:
                logger.exception("не удалось удалить запись")
                return Reply("", notification="Не удалось удалить: %s" % _short_error(exc))
            return Reply("Запись удалена.", hide_keyboard=True, notification="Удалено")

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
                "Категория записи #%d: %s." % (entry_id, entry_kind_label(kind)),
                buttons=self._entry_buttons(entry_id),
                notification="Сохранено",
            )

        if action == "noop":
            return Reply("", notification="Ок")
        return Reply("", notification="Кнопка устарела.")

    def _handle_reminder_callback(self, data: str) -> Reply:
        """Кнопки напоминаний: «Сделал», «Отложить 1 ч», «На завтра», привычки."""
        try:
            outcome = self.reminders.handle_action(data)
        except Exception as exc:
            logger.exception("не удалось обработать кнопку напоминания")
            return Reply("", notification="Не получилось: %s" % _short_error(exc))
        action = data.split(":")[1] if ":" in data else "?"
        self.record_usage("reminders", "click:" + action)
        return Reply(
            outcome.text,
            buttons=outcome.buttons,
            notification=outcome.notification,
        )

    def _handle_v4_callback(self, action: str) -> Reply:
        """Кнопки v4: Изменить/Удалить, быстрые значения и удаление."""
        if action.startswith("pick:"):
            return self._handle_clarify_pick(action)
        if action.startswith("quick:"):
            return self._handle_quick(action)
        if action in {"food:save", "food:kcal"}:
            return self._handle_food_estimate(action)
        if action in {"ask:more", "ask:brief"}:
            try:
                with self._db_lock:
                    payload = store_v4.load_last_ask(self.conn)
            except Exception:
                logger.exception("не удалось прочитать последний вопрос")
                payload = None
            if not payload:
                return Reply("", notification="Вопрос устарел — спроси заново.")
            return self._ask_reply(payload["query"], detail=(action == "ask:more"))
        if action == "edit:last":
            with self._db_lock:
                record = store_v4.last_record(self.conn)
            if not record:
                return Reply("", notification="Запись уже удалена.", hide_keyboard=True)
            hint = str(record.get("edit_hint") or "").strip()
            if not hint:
                hint = "Пришли новую версию записи, а старую удали кнопкой."
            return Reply("Правка: %s" % hint, notification="Правка")
        if action == "del:ask":
            return self._ask_delete_last()
        if action in {"del:confirm", "del:cancel"}:
            if action == "del:cancel":
                try:
                    with self._db_lock, self.conn:
                        store_v4.clear_pending(self.conn)
                except Exception:
                    logger.exception("не удалось отменить удаление")
                return Reply("Отмена.", hide_keyboard=True, notification="Отменено")
            try:
                with self._db_lock, self.conn:
                    pending = store_v4.load_pending(self.conn)
                    store_v4.clear_pending(self.conn)
                    if not pending:
                        return Reply("Нечего удалять.", hide_keyboard=True, notification="Нечего удалять")
                    removed, label = store_v4.delete_record(self.conn, pending)
                    if removed:
                        store_v4.clear_last(self.conn)
            except Exception as exc:
                logger.exception("не удалось удалить запись")
                return Reply("Не удалось удалить: %s." % _short_error(exc))
            if not removed:
                return Reply("Запись уже удалена.", hide_keyboard=True, notification="Уже удалено")
            return Reply("Удалено: %s." % label, hide_keyboard=True, notification="Удалено")
        return Reply("", notification="Кнопка устарела.")

    def _handle_food_estimate(self, action: str) -> Reply:
        """«Добавить в справочник» / «Только калории».

        Оценённые позиции применяются сразу. Если КБЖУ нет (модель недоступна
        или продукт неизвестен) — бот спрашивает числа у владельца: КБЖУ на
        100 г для справочника или калории готовой порции.
        """
        try:
            with self._db_lock:
                payload = nutrition_v4.load_pending_estimate(self.conn)
        except Exception as exc:
            logger.exception("не удалось прочитать оценку еды")
            return Reply("", notification="Не удалось: %s" % _short_error(exc))
        if not payload or not payload.get("items"):
            return Reply(
                "", notification="Оценка устарела — запиши еду заново."
            )
        mode = "catalog" if action == "food:save" else "kcal"
        items = [item for item in payload.get("items") or [] if isinstance(item, Mapping)]
        known = [item for item in items if item.get("per100")]
        unknown = [item for item in items if not item.get("per100")]
        parts: List[str] = []
        try:
            with self._db_lock, self.conn:
                if known:
                    if mode == "catalog":
                        _added, text = nutrition_v4.add_products_to_catalog(
                            self.conn, {"items": known}
                        )
                    else:
                        count = nutrition_v4.keep_kcal_only(self.conn, {"items": known})
                        text = (
                            "Оставил только калории: %d %s."
                            % (count, "запись" if count == 1 else "записи")
                            if count
                            else "Запись уже удалена."
                        )
                    parts.append(text)
                nutrition_v4.clear_pending_estimate(self.conn)
                if unknown:
                    # Спрашиваем числа владельца: ответ перехватит _pending_food_input.
                    nutrition_v4.save_pending_input(self.conn, mode, unknown)
        except Exception as exc:
            logger.exception("не удалось применить оценку еды")
            return Reply("", notification="Не удалось: %s" % _short_error(exc))
        if unknown:
            ask = (
                nutrition_v4.ask_catalog_input(unknown)
                if mode == "catalog"
                else nutrition_v4.ask_kcal_input(unknown)
            )
            parts.append(ask)
        text = "\n".join(part for part in parts if part)
        return Reply(text, notification="Сохранено")

    def _pending_food_input(self, clean: str) -> Optional[Reply]:
        """Ждём КБЖУ на 100 г или калории порции — принимаем только числа."""
        try:
            with self._db_lock:
                payload = nutrition_v4.load_pending_input(self.conn)
        except Exception:
            logger.exception("не удалось прочитать ожидание КБЖУ")
            return None
        if not payload:
            return None
        numbers = nutrition_v4.parse_pending_numbers(clean)
        if numbers is None:
            # Не числа: ожидание устарело, обычная фраза идёт своим путём.
            try:
                with self._db_lock, self.conn:
                    nutrition_v4.clear_pending_input(self.conn)
            except Exception:
                logger.exception("не удалось сбросить ожидание КБЖУ")
            return None
        mode = str(payload.get("mode") or "catalog")
        try:
            with self._db_lock, self.conn:
                if mode == "kcal":
                    text, _waiting = nutrition_v4.apply_kcal_input(self.conn, payload, numbers)
                else:
                    text, _waiting = nutrition_v4.apply_catalog_input(
                        self.conn, payload, numbers
                    )
        except Exception as exc:
            logger.exception("не удалось применить КБЖУ владельца")
            return Reply("Не смог записать КБЖУ: %s." % _short_error(exc))
        self.record_usage("food", "input:" + mode)
        return Reply(text, notification="Питание")

    def _handle_quick(self, action: str) -> Reply:
        """Быстрые кнопки уточнения: вода в мл и энергия 1–5."""
        parts = action.split(":")
        if len(parts) != 3:
            return Reply("", notification="Кнопка устарела.")
        kind, raw = parts[1], parts[2]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return Reply("", notification="Кнопка устарела.")
        try:
            if kind == "water" and 50 <= value <= 5000:
                data = {
                    "intent": "habit",
                    "habit": "Вода 2 литра",
                    "emoji": "💧",
                    "amount": value // 1000 if value % 1000 == 0 else value,
                    "unit": "л" if value % 1000 == 0 else "мл",
                    "water_ml": value,
                }
                with self._db_lock, self.conn:
                    outcome = store_v4.mark_habit(self.conn, self.settings, data)
                    if outcome.operations:
                        store_v4.remember_last(self.conn, outcome)
                return self._outcome_reply(outcome)
            if kind == "energy" and 1 <= value <= 5:
                data = {"intent": "checkin", "energy": value, "mood": "", "note": ""}
                with self._db_lock, self.conn:
                    outcome = store_v4.save_checkin(self.conn, self.settings, data)
                    if outcome.operations:
                        store_v4.remember_last(self.conn, outcome)
                return self._outcome_reply(outcome)
        except Exception as exc:
            logger.exception("не удалось сохранить быстрый ответ")
            return Reply("Не смог сохранить: %s." % _short_error(exc))
        return Reply("", notification="Кнопка устарела.")

    # -- помощники --------------------------------------------------------

    def _entry_buttons(self, entry_id: int) -> List[List[Tuple[str, str]]]:
        return [
            [
                ("Удалить", "e:del:%d" % entry_id),
                ("Категория", "e:cat:%d" % entry_id),
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


def _meal_from_text(text: str, hour: int, hint: str = "") -> Tuple[str, str]:
    """Приём пищи: подсказка парсера, слово в сообщении, иначе по часу."""
    lowered = str(text or "").casefold()
    if hint in MEAL_LABELS:
        for meal, label in (
            ("breakfast", "завтрак"),
            ("lunch", "обед"),
            ("dinner", "ужин"),
            ("snack", "перекус"),
        ):
            if meal == hint:
                return meal, label
    for meal, label in (("breakfast", "завтрак"), ("lunch", "обед"), ("dinner", "ужин"), ("snack", "перекус")):
        if label in lowered:
            return meal, label
    if hour < 11:
        return "breakfast", MEAL_LABELS["breakfast"]
    if hour < 16:
        return "lunch", MEAL_LABELS["lunch"]
    if hour < 21:
        return "dinner", MEAL_LABELS["dinner"]
    return "snack", MEAL_LABELS["snack"]


def _food_payload(parsed: Mapping[str, Any]) -> Dict[str, Any]:
    """Еда из разбора v3/v4 -> общий формат :func:`nutrition_v4.log_food`."""
    metrics = parsed.get("metrics") if isinstance(parsed.get("metrics"), Mapping) else {}
    items: List[Dict[str, Any]] = []
    raw_items = parsed.get("food_items")
    if isinstance(raw_items, list):
        for entry in raw_items:
            if isinstance(entry, Mapping):
                items.append(dict(entry))
    if not items:
        grams_total = _as_float(parsed.get("grams_total")) or 0.0
        names = parsed.get("items") if isinstance(parsed.get("items"), list) else []
        for name in names:
            items.append({"name": str(name), "grams": grams_total})
    return {
        "meal": str(parsed.get("meal") or ""),
        "items": items,
        "kcal": _as_float(metrics.get("calories")),
        "protein": _as_float(metrics.get("protein")),
        "fat": _as_float(metrics.get("fat")),
        "carbs": _as_float(metrics.get("carbs")),
        "text": str(parsed.get("text") or ""),
    }


# --------------------------------------------------------------------------
# aiogram-роутер: тонкая обвязка над BotService
# --------------------------------------------------------------------------


def build_router(service: BotService) -> Router:
    """Собирает Router с командами, текстом, медиа и кнопками."""
    router = Router(name="lifeos-bot")
    #: Ссылки на фоновые ожидания расшифровки: без них asyncio их соберёт.
    transcript_tasks: set = set()

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
        if markup is None:
            markup = _reply_keyboard(reply)
        try:
            await message.answer(reply.text, reply_markup=markup)
        except Exception:
            logger.exception("не удалось отправить ответ в чат")

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/start")
        await _send(message, await asyncio.to_thread(service.handle_start))

    @router.message(Command("help"))
    async def on_help(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/help")
        await _send(message, await asyncio.to_thread(service.handle_help))

    @router.message(Command("stats"))
    async def on_stats(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/stats")
        await _send(message, await asyncio.to_thread(service.handle_stats))

    @router.message(Command("today"))
    async def on_today(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/today")
        await _send(message, await asyncio.to_thread(service.handle_today))

    @router.message(Command("week"))
    async def on_week(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/week")
        await _send(message, await asyncio.to_thread(service.handle_week))

    @router.message(Command("weight"))
    async def on_weight(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/weight")
        await _send(message, await asyncio.to_thread(service.handle_weight))

    @router.message(Command("points"))
    async def on_points(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/points")
        await _send(message, await asyncio.to_thread(service.handle_points))

    @router.message(Command("morning"))
    async def on_morning(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/morning")
        await _send(message, await asyncio.to_thread(service.handle_morning))

    @router.message(Command("evening"))
    async def on_evening(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/evening")
        await _send(message, await asyncio.to_thread(service.handle_evening))

    @router.message(Command("undo"))
    async def on_undo(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/undo")
        await _send(message, await asyncio.to_thread(service.handle_undo))

    @router.message(Command("reminders"))
    async def on_reminders(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/reminders")
        text = str(getattr(message, "text", "") or "")
        await _send(
            message,
            await asyncio.to_thread(service.handle_reminders, text),
        )

    @router.message(Command("mute"))
    async def on_mute(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/mute")
        await _send(message, await asyncio.to_thread(service.handle_mute))

    @router.message(Command("unmute"))
    async def on_unmute(message: Message) -> None:
        if not _allowed(message):
            return
        service.record_command("/unmute")
        await _send(message, await asyncio.to_thread(service.handle_unmute))

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
            await _send(message, Reply(exc.reason))
            return
        except Exception as exc:
            logger.exception("не удалось скачать медиа из Telegram")
            await _send(message, Reply("Не смог скачать файл: %s." % _short_error(exc)))
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
        if reply.wait_transcript and reply.job_id:
            task = asyncio.create_task(_deliver_transcript(message, reply))
            transcript_tasks.add(task)
            task.add_done_callback(transcript_tasks.discard)

    async def _deliver_transcript(message: Message, reply: Reply) -> None:
        """Дожидается расшифровки в фоне и присылает её отдельным сообщением.

        Ключевое: ожидание идёт в потоке (``asyncio.to_thread``), поэтому
        поллинг бота не блокируется, а человек получает ответ, как только
        задача ``transcribe`` стала ``done``.
        """
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        try:
            result = await asyncio.to_thread(
                service.wait_for_transcript, reply.job_id
            )
            transcript_reply = await asyncio.to_thread(
                service.handle_transcript,
                reply.entry_id,
                reply.job_id,
                result,
                chat_id,
            )
        except Exception:
            logger.exception("не удалось дождаться расшифровки")
            return
        if not transcript_reply.text:
            return
        markup = _markup(transcript_reply)
        if markup is None:
            markup = _reply_keyboard(transcript_reply)
        try:
            await message.answer(transcript_reply.text, reply_markup=markup)
        except Exception:
            logger.exception("не удалось отправить расшифровку в чат")

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

    @router.callback_query(
        F.data.startswith("e:")
        | F.data.startswith("v4:")
        | F.data.startswith("r:")
    )
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
                await message.answer(
                    reply.text,
                    reply_markup=markup if markup is not None else _reply_keyboard(reply),
                )
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


def _web_url(settings: Any, route: str) -> str:
    """Ссылка на экран веба: env → config app.public_url → host:port."""
    base = os.environ.get("LIFEOS_WEB_BASE_URL", "").strip()
    if not base:
        config = getattr(settings, "config", {}) or {}
        app = config.get("app") if isinstance(config, Mapping) else None
        if isinstance(app, Mapping):
            base = str(app.get("public_url") or "").strip()
    if not base:
        host = str(getattr(settings, "host", "127.0.0.1") or "127.0.0.1")
        port = getattr(settings, "port", 8787)
        base = "http://%s:%s" % (host, port)
    return base.rstrip("/") + "/#/" + str(route or "/").lstrip("/")


def _ask_buttons(page: str, detail: bool) -> List[List[Tuple[str, str]]]:
    """Кнопки ответа: ссылка на экран и переключение кратко/подробно."""
    more = ("Кратко", "v4:ask:brief") if detail else ("Ещё", "v4:ask:more")
    rows: List[List[Tuple[str, str]]] = []
    if page:
        rows.append([("Подробнее", page), more])
    else:
        rows.append([more])
    return rows


def _markup(reply: Reply) -> Optional[InlineKeyboardMarkup]:
    if reply.hide_keyboard:
        # Пустой reply_markup при edit_text убирает кнопки под сообщением.
        return None
    rows = []
    for row in reply.buttons:
        buttons = []
        for label, callback_data in row:
            data = str(callback_data)
            if data.startswith(("http://", "https://")):
                buttons.append(InlineKeyboardButton(text=str(label), url=data))
                continue
            if len(data.encode("utf-8")) > 64:
                logger.warning("callback_data длиннее 64 байт — кнопка пропущена")
                continue
            buttons.append(InlineKeyboardButton(text=str(label), callback_data=data))
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _reply_keyboard(reply: Reply) -> Optional[ReplyKeyboardMarkup]:
    """Постоянная клавиатура: кнопки шлют текст, а не callback."""
    rows = [
        [KeyboardButton(text=str(label)) for label in row if str(label).strip()]
        for row in reply.keyboard
        if row
    ]
    rows = [row for row in rows if row]
    if not rows:
        return None
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напиши или выбери кнопку",
    )


def _pair_buttons(options: Any) -> List[List[Tuple[str, str]]]:
    """Варианты уточнения (label, callback) -> инлайн-кнопки по одной в ряд."""
    rows: List[List[Tuple[str, str]]] = []
    if not isinstance(options, (list, tuple)):
        return rows
    for option in options:
        if isinstance(option, (list, tuple)) and len(option) >= 2:
            label, callback = str(option[0]), str(option[1])
        elif isinstance(option, Mapping):
            label = str(option.get("label") or option.get("text") or "").strip()
            callback = str(option.get("callback") or option.get("value") or "")
        else:
            continue
        if label and callback:
            rows.append([(label, callback)])
    return rows


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
