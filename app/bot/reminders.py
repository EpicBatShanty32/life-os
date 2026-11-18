"""Напоминания и ритуалы Life OS — бот сам пишет владельцу по расписанию.

Решение владельца: бот — главный узел, вебом не пользуются. Значит бот должен
сам начинать разговор: утренний ритуал, вечерний итог, напоминание о деле за
30 минут, привычки к вечеру. Модуль отвечает за всё это.

Что здесь есть:

* **расписание** (``ReminderSettings``): утро 09:00, итог дня 22:00, привычки
  20:00, напоминание о деле за ``lead_min`` минут. Меняется командами бота
  (``/reminders morning 08:30``) и лежит в ``settings_kv`` — поэтому переживает
  перезапуск службы и видно вебу;
* **тихие часы** 23:00–08:00: ночью молчим, кроме жёстких событий
  (``tasks.resource = 'must'``) — их владелец сам поставил на это время;
* **окно актуальности**: у каждого напоминания есть срок, до которого оно
  уместно. Если мак был выключен, бот не присылает пачку пропущенного —
  просроченное помечается ``missed`` и не отправляется;
* **состояние в БД** (``reminder_events``): одна строка на событие дня, поэтому
  перезапуск бота не повторяет уже отправленное;
* **кнопки**: «Сделал», «Отложить 1 ч», «На завтра», «Пропустить», отметка
  привычки, энергия 1–5 — каждое нажатие пишет в БД, а не только в чат;
* **антиспам**: не больше ``max_per_task_day`` напоминаний в день на одно дело.

Время считается по часовому поясу приложения (``Settings.tz``). Модуль не
зависит от aiogram: ``tick()`` возвращает :class:`Outgoing`, а отправляет их
цикл в :mod:`app.bot.main` — так тесты проверяют расписание без сети.

Расписание живёт в ``settings_kv`` — эти же ключи читает и может писать веб
(``/api/settings``):

======================  ==========================================
``reminders.enabled``   ``"1"`` / ``"0"`` (``/mute``, ``/unmute``)
``reminders.morning``   время утреннего ритуала, ``"09:00"``
``reminders.evening``   время вечернего итога, ``"22:00"``
``reminders.habits``    время напоминания о привычках, ``"20:00"``
``reminders.lead_min``  за сколько минут до дела, ``"30"``
``reminders.max_per_task_day``  напоминаний в день на дело, ``"2"``
``reminders.quiet``     тихие часы, ``"23:00-08:00"``
======================  ==========================================
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.bot import store_v4

logger = logging.getLogger("lifeos.bot.reminders")

__all__ = [
    "ActionOutcome",
    "DEFAULTS",
    "Outgoing",
    "ReminderEngine",
    "ReminderSettings",
]

#: Префикс ключей в ``settings_kv``: настройки напоминаний.
SETTINGS_PREFIX = "reminders."
#: Значения по умолчанию. Время — местное (``Settings.tz``).
DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "morning": "09:00",
    "evening": "22:00",
    "habits": "20:00",
    "lead_min": 30,
    "max_per_task_day": 2,
    "quiet": ("23:00", "08:00"),
}
#: Сколько минут ритуал ещё уместен после назначенного времени.
MORNING_GRACE_MIN = 90
EVENING_GRACE_MIN = 90
#: После начала дела напоминание уместно ещё столько минут (опоздал — не спамим).
TASK_LATE_GRACE_MIN = 10
#: Отложенное владельцем напоминание живёт дольше окна: он сам попросил.
POSTPONE_MIN = 60
#: Пауза перед повтором, если Telegram не принял сообщение.
RETRY_DELAY_S = 120
#: Сколько раз пробуем отправить одно событие, прежде чем сдаться.
MAX_ATTEMPTS = 3
#: Ресурс дела, которое считается жёстким событием (его не глушат тихие часы).
HARD_RESOURCES = frozenset({"must"})
#: Статусы событий, после которых кнопки под сообщением уже не действуют.
FINAL_STATES = frozenset({"done", "moved", "skipped", "missed", "failed"})

_TABLE = "reminder_events"
_DDL = (
    """
    CREATE TABLE IF NOT EXISTS reminder_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        ref_id INTEGER NOT NULL DEFAULT 0,
        day TEXT NOT NULL,
        due_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'due',
        sends INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_at TEXT,
        message_id INTEGER,
        sent_at TEXT,
        updated TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reminder_events_day "
    "ON reminder_events(day, kind)",
    "CREATE INDEX IF NOT EXISTS idx_reminder_events_state "
    "ON reminder_events(state, next_at)",
)

_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


@dataclass
class Outgoing:
    """Готовое к отправке напоминание: текст, кнопки и id события."""

    event_id: int
    kind: str
    chat_id: int
    text: str
    buttons: List[List[Tuple[str, str]]] = field(default_factory=list)
    ref_id: int = 0


@dataclass
class ActionOutcome:
    """Результат нажатия кнопки: новый текст сообщения или только уведомление."""

    text: str = ""
    buttons: List[List[Tuple[str, str]]] = field(default_factory=list)
    notification: str = ""


@dataclass
class ReminderSettings:
    """Действующее расписание напоминаний."""

    enabled: bool = True
    morning: time = time(9, 0)
    evening: time = time(22, 0)
    habits: time = time(20, 0)
    lead_min: int = 30
    max_per_task_day: int = 2
    quiet: Tuple[time, time] = (time(23, 0), time(8, 0))

    def quiet_label(self) -> str:
        return "%s–%s" % (_hm_text(self.quiet[0]), _hm_text(self.quiet[1]))


# --------------------------------------------------------------------------
# Время и текст
# --------------------------------------------------------------------------


def _hm_text(value: time) -> str:
    return "%02d:%02d" % (value.hour, value.minute)


def parse_hm(raw: Any) -> Optional[time]:
    """``"9:00"``/``"09:00"``/``"9"`` → ``time``; иначе ``None``."""
    text = str(raw or "").strip().replace(".", ":")
    if not text:
        return None
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _at(day: Any, moment: time, tz: Any) -> datetime:
    return datetime.combine(day, moment, tzinfo=tz)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _aware(raw: Any, tz: Any) -> Optional[datetime]:
    """ISO-время из БД как aware-datetime в часовом поясе приложения."""
    parsed = _parse_iso(raw)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def in_quiet_hours(now: datetime, quiet: Tuple[time, time]) -> bool:
    """Попадает ли момент в тихие часы (окно может переходить через полночь)."""
    start, end = _minutes(quiet[0]), _minutes(quiet[1])
    current = now.hour * 60 + now.minute
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: Any, limit: int = 60) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: max(1, limit - 1)].rstrip() + "…"


def _fmt_number(value: Any, digits: int = 1) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = ("%." + str(digits) + "f") % number
    return text.replace(".", ",")


def _plural(count: int, one: str, few: str, many: str) -> str:
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def _human_day(day: Any) -> str:
    return "%d %s" % (day.day, _MONTHS_GENITIVE[day.month - 1])


# --------------------------------------------------------------------------
# Движок
# --------------------------------------------------------------------------


class ReminderEngine:
    """Расписание и состояние напоминаний поверх общей SQLite-базы.

    Один экземпляр на процесс бота. Записи в БД защищены тем же
    ``threading.Lock``, что и обработчики сообщений: цикл напоминаний живёт в
    отдельном потоке (``asyncio.to_thread``), и без общей блокировки SQLite
    получал бы «database is locked».
    """

    def __init__(self, settings: Any, conn: Any, lock: Optional[Any] = None) -> None:
        self.settings = settings
        self.conn = conn
        #: RLock: ensure_schema может вызываться изнутри уже взятого замка.
        self._lock = lock or threading.RLock()
        self._schema_ready = False
        try:
            self.ensure_schema()
        except Exception:  # pragma: no cover - БД может быть занята чужим процессом
            logger.exception("не удалось создать таблицу напоминаний при старте")

    # -- схема и настройки -------------------------------------------------

    def ensure_schema(self) -> None:
        """Создаёт таблицу событий (идемпотентно, на каждом старте бота)."""
        if self._schema_ready:
            return
        with self._lock, self.conn:
            for statement in _DDL:
                self.conn.execute(statement)
        self._schema_ready = True

    def load_settings(self) -> ReminderSettings:
        """Читает настройки из ``settings_kv`` с проверкой значений."""
        try:
            self.ensure_schema()
            raw = self._kv_all()
        except Exception:  # pragma: no cover - БД недоступна: работаем на дефолтах
            logger.exception("не удалось прочитать настройки напоминаний")
            raw = {}
        # По умолчанию — ``app.quiet_hours`` из config/config.json, затем код.
        default_quiet = tuple(DEFAULTS["quiet"])
        config_quiet = getattr(self.settings, "quiet_hours", None)
        if isinstance(config_quiet, (list, tuple)) and len(config_quiet) >= 2:
            default_quiet = (str(config_quiet[0]), str(config_quiet[1]))
        quiet = _split_quiet(raw.get("quiet"), default_quiet)
        return ReminderSettings(
            enabled=_as_bool(raw.get("enabled"), True),
            morning=parse_hm(raw.get("morning")) or parse_hm(DEFAULTS["morning"]),
            evening=parse_hm(raw.get("evening")) or parse_hm(DEFAULTS["evening"]),
            habits=parse_hm(raw.get("habits")) or parse_hm(DEFAULTS["habits"]),
            lead_min=_as_int(raw.get("lead_min"), int(DEFAULTS["lead_min"]), 5, 180),
            max_per_task_day=_as_int(
                raw.get("max_per_task_day"), int(DEFAULTS["max_per_task_day"]), 1, 10
            ),
            quiet=quiet,
        )

    def _kv_all(self) -> Dict[str, str]:
        rows = self.conn.execute(
            "SELECT key, value FROM settings_kv WHERE key LIKE ?",
            (SETTINGS_PREFIX + "%",),
        ).fetchall()
        result: Dict[str, str] = {}
        for row in rows:
            result[str(row[0])[len(SETTINGS_PREFIX):]] = str(row[1] or "")
        return result

    def save_setting(self, name: str, value: Any) -> None:
        """Пишет значение в ``settings_kv`` (переживает перезапуск)."""
        self.ensure_schema()
        text = value if isinstance(value, str) else str(value)
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO settings_kv(key, value, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated = excluded.updated",
                (SETTINGS_PREFIX + name, text, _stamp()),
            )

    def chat_id(self) -> int:
        """Кому писать: ``allowed_chat_id`` из конфига, иначе владелец из kv."""
        try:
            allowed = int(getattr(self.settings, "allowed_chat_id", 0) or 0)
        except (TypeError, ValueError):
            allowed = 0
        if allowed:
            return allowed
        try:
            row = self.conn.execute(
                "SELECT value FROM settings_kv WHERE key = 'telegram.owner_chat_id'"
            ).fetchone()
        except Exception:  # pragma: no cover
            return 0
        try:
            return int(str(row[0])) if row is not None else 0
        except (TypeError, ValueError):
            return 0

    def now(self) -> datetime:
        try:
            return self.settings.now()
        except Exception:  # pragma: no cover - настройки всегда есть
            return datetime.now().astimezone()

    @property
    def tz(self) -> Any:
        try:
            return self.settings.tz
        except Exception:  # pragma: no cover
            return timezone.utc

    # -- команды настроек --------------------------------------------------

    def mute(self) -> str:
        """``/mute``: выключить напоминания до ``/unmute``."""
        self.save_setting("enabled", "0")
        return (
            "Напоминания выключены. Ритуалы и напоминания о делах приходить не будут; "
            "включить — /unmute."
        )

    def unmute(self) -> str:
        """``/unmute``: снова включить напоминания."""
        self.save_setting("enabled", "1")
        schedule = self.load_settings()
        return "Напоминания включены: утро %s, итог дня %s, привычки %s." % (
            _hm_text(schedule.morning),
            _hm_text(schedule.evening),
            _hm_text(schedule.habits),
        )

    def describe(self) -> str:
        """Текст ``/reminders``: что и когда приходит."""
        schedule = self.load_settings()
        status = "включены" if schedule.enabled else "выключены (включить — /unmute)"
        return "\n".join(
            [
                "Напоминания: %s." % status,
                "Утро %s · Итог дня %s · Привычки %s."
                % (
                    _hm_text(schedule.morning),
                    _hm_text(schedule.evening),
                    _hm_text(schedule.habits),
                ),
                "Дела со временем: за %d мин, не больше %d %s в день на дело."
                % (
                    schedule.lead_min,
                    schedule.max_per_task_day,
                    _plural(schedule.max_per_task_day, "раз", "раза", "раз"),
                ),
                "Тихие часы %s: молчу, кроме жёстких событий." % schedule.quiet_label(),
                "Изменить: /reminders morning 08:30, evening 21:30, habits 19:30, "
                "lead 15, max 3, quiet 23:00-08:00.",
                "Выключить: /mute. Ритуал сейчас: /morning и /evening.",
            ]
        )

    def configure(self, argument: str) -> str:
        """``/reminders <что> <значение>``: меняет расписание и отвечает строкой."""
        tokens = str(argument or "").split()
        if not tokens:
            return self.describe()
        name = tokens[0].casefold()
        value = " ".join(tokens[1:]).strip()
        if name in {"on", "вкл", "включить"}:
            return self.unmute()
        if name in {"off", "выкл", "выключить", "mute"}:
            return self.mute()
        if name in {"morning", "утро", "утром"}:
            return self._set_time("morning", value, "Утренний ритуал")
        if name in {"evening", "вечер", "итог", "вечером"}:
            return self._set_time("evening", value, "Вечерний итог")
        if name in {"habits", "привычки"}:
            return self._set_time("habits", value, "Напоминание о привычках")
        if name in {"lead", "за", "минуты"}:
            number = _as_int(value, 0, 5, 180)
            if not number:
                return "Сколько минут до дела? Например: /reminders lead 30"
            self.save_setting("lead_min", str(number))
            return "Напоминание о деле — за %d мин до начала." % number
        if name in {"max", "лимит"}:
            number = _as_int(value, 0, 1, 10)
            if not number:
                return "Сколько напоминаний в день на дело? Например: /reminders max 2"
            self.save_setting("max_per_task_day", str(number))
            return "Не больше %d %s в день на одно дело." % (
                number,
                _plural(number, "напоминание", "напоминания", "напоминаний"),
            )
        if name in {"quiet", "тихие"}:
            quiet = _split_quiet(value, tuple(DEFAULTS["quiet"]))
            start, end = quiet
            self.save_setting("quiet", "%s-%s" % (_hm_text(start), _hm_text(end)))
            return "Тихие часы %s–%s: ночью молчу, кроме жёстких событий." % (
                _hm_text(start),
                _hm_text(end),
            )
        return (
            "Не понял. Что можно менять: morning 09:00, evening 22:00, habits 20:00, "
            "lead 30, max 2, quiet 23:00-08:00, on, off."
        )

    def _set_time(self, name: str, value: str, label: str) -> str:
        moment = parse_hm(value)
        if moment is None:
            return "Время в формате ЧЧ:ММ, например: /reminders %s 09:00" % name
        self.save_setting(name, _hm_text(moment))
        return "%s — в %s (%s)." % (label, _hm_text(moment), getattr(self.settings, "timezone", ""))

    # -- отправка по расписанию -------------------------------------------

    def tick(self, now: Optional[datetime] = None) -> List[Outgoing]:
        """Проверяет расписание и возвращает готовые к отправке напоминания.

        Побочные эффекты (запись в БД) — часть работы: событие помечается
        ``sending`` до отправки, поэтому даже падение процесса между тиком и
        отправкой не приведёт к бесконечным повторам (не больше
        ``MAX_ATTEMPTS``), а уже отправленное не уйдёт второй раз.
        """
        self.ensure_schema()
        moment = now or self.now()
        schedule = self.load_settings()
        if not schedule.enabled:
            return []
        chat_id = self.chat_id()
        if not chat_id:
            logger.warning("напоминания: некому писать (allowed_chat_id = 0)")
            return []
        with self._lock:
            outgoing: List[Outgoing] = []
            outgoing.extend(self._rituals(moment, schedule, chat_id, "morning"))
            outgoing.extend(self._tasks(moment, schedule, chat_id))
            outgoing.extend(self._habits(moment, schedule, chat_id))
            outgoing.extend(self._rituals(moment, schedule, chat_id, "evening"))
        return outgoing

    def _rituals(
        self,
        now: datetime,
        schedule: ReminderSettings,
        chat_id: int,
        kind: str,
    ) -> List[Outgoing]:
        """Утренний ритуал или вечерний итог: окно, тихие часы, состояние."""
        moment = schedule.morning if kind == "morning" else schedule.evening
        grace = MORNING_GRACE_MIN if kind == "morning" else EVENING_GRACE_MIN
        day = now.date()
        due_at = _at(day, moment, now.tzinfo)
        if now < due_at:
            return []
        key = "%s:%s" % (kind, day.isoformat())
        if self._is_final(key):
            return []
        if in_quiet_hours(now, schedule.quiet):
            self._mark(key, kind, 0, day, due_at, "missed")
            return []
        if now - due_at > timedelta(minutes=grace):
            # Мак спал: вчерашний ритуал уже неактуален — не догоняем.
            self._mark(key, kind, 0, day, due_at, "missed")
            return []
        event_id = self._claim(key, kind, 0, day, due_at, now)
        if not event_id:
            return []
        text, buttons = self.build(kind, event_id, now, schedule)
        return [
            Outgoing(
                event_id=event_id,
                kind=kind,
                chat_id=chat_id,
                text=text,
                buttons=buttons,
            )
        ]

    def _tasks(
        self,
        now: datetime,
        schedule: ReminderSettings,
        chat_id: int,
    ) -> List[Outgoing]:
        """Дела со временем: за ``lead_min`` минут до начала."""
        day = now.date()
        rows = self.conn.execute(
            "SELECT id, title, start_at, resource FROM tasks "
            "WHERE status = 'todo' AND start_at IS NOT NULL "
            "AND substr(start_at, 1, 10) = ? ORDER BY start_at, id",
            (day.isoformat(),),
        ).fetchall()
        if not rows:
            return []
        sent = self._task_sends(day)
        outgoing: List[Outgoing] = []
        lead = timedelta(minutes=schedule.lead_min)
        for row in rows:
            start = _aware(row["start_at"], now.tzinfo)
            if start is None:
                continue
            task_id = int(row["id"])
            key = "task:%d:%s" % (task_id, str(row["start_at"])[:19])
            if self._is_final(key):
                continue
            due_at = start - lead
            if now < due_at:
                continue
            # Просроченное без явного «отложить» не догоняем: это и есть пачка
            # пропущенного после выключенного мака.
            postponed = self._state(key) == "postponed"
            if not postponed and now > start + timedelta(minutes=TASK_LATE_GRACE_MIN):
                self._mark(key, "task", task_id, day, due_at, "missed")
                continue
            hard = str(row["resource"] or "") in HARD_RESOURCES
            if in_quiet_hours(now, schedule.quiet) and not hard:
                self._mark(key, "task", task_id, day, due_at, "missed")
                continue
            if sent.get(task_id, 0) >= schedule.max_per_task_day:
                self._close(key, "task", task_id, day, due_at, "skipped", now)
                continue
            event_id = self._claim(key, "task", task_id, day, due_at, now)
            if not event_id:
                continue
            text, buttons = self.build("task", event_id, now, schedule, task_id=task_id)
            outgoing.append(
                Outgoing(
                    event_id=event_id,
                    kind="task",
                    chat_id=chat_id,
                    text=text,
                    buttons=buttons,
                    ref_id=task_id,
                )
            )
        return outgoing

    def _habits(
        self,
        now: datetime,
        schedule: ReminderSettings,
        chat_id: int,
    ) -> List[Outgoing]:
        """Привычки: если к назначенному времени они не отмечены."""
        day = now.date()
        due_at = _at(day, schedule.habits, now.tzinfo)
        if now < due_at:
            return []
        window_end = _at(day, schedule.evening, now.tzinfo)
        if window_end <= due_at:
            window_end = due_at + timedelta(hours=3)
        key = "habits:%s" % day.isoformat()
        if self._is_final(key):
            return []
        if in_quiet_hours(now, schedule.quiet):
            self._mark(key, "habits", 0, day, due_at, "missed")
            return []
        if now >= window_end:
            self._mark(key, "habits", 0, day, due_at, "missed")
            return []
        undone = self._undone_habits(day)
        if not undone:
            self._mark(key, "habits", 0, day, due_at, "done")
            return []
        event_id = self._claim(key, "habits", 0, day, due_at, now)
        if not event_id:
            return []
        text, buttons = self.build("habits", event_id, now, schedule)
        return [
            Outgoing(
                event_id=event_id,
                kind="habits",
                chat_id=chat_id,
                text=text,
                buttons=buttons,
            )
        ]

    def mark_sent(self, event_id: int, message_id: int = 0, now: Optional[datetime] = None) -> None:
        """Telegram принял сообщение: фиксируем факт и id сообщения."""
        self.ensure_schema()
        moment = now or self.now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE reminder_events SET state = 'sent', sends = sends + 1, "
                "sent_at = ?, message_id = ?, next_at = NULL, updated = ? WHERE id = ?",
                (_iso(moment), int(message_id or 0), _iso(moment), int(event_id)),
            )

    def mark_failed(self, event_id: int, now: Optional[datetime] = None) -> None:
        """Telegram не принял сообщение: повторяем позже или сдаёмся."""
        self.ensure_schema()
        moment = now or self.now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT attempts FROM reminder_events WHERE id = ?", (int(event_id),)
            ).fetchone()
            attempts = int(row["attempts"]) if row is not None else MAX_ATTEMPTS
            if attempts >= MAX_ATTEMPTS:
                self.conn.execute(
                    "UPDATE reminder_events SET state = 'failed', updated = ? WHERE id = ?",
                    (_iso(moment), int(event_id)),
                )
                return
            retry_at = moment + timedelta(seconds=RETRY_DELAY_S)
            self.conn.execute(
                "UPDATE reminder_events SET state = 'postponed', next_at = ?, updated = ? "
                "WHERE id = ?",
                (_iso(retry_at), _iso(moment), int(event_id)),
            )

    # -- ручной запуск ритуалов -------------------------------------------

    def manual(self, kind: str, now: Optional[datetime] = None) -> Optional[Outgoing]:
        """``/morning`` и ``/evening``: ритуал сейчас, по просьбе владельца.

        Возвращает ``None``, если писать некому. Событие дня при этом считается
        отправленным: планировщик не пришлёт его второй раз.
        """
        self.ensure_schema()
        if kind not in {"morning", "evening"}:
            return None
        moment = now or self.now()
        schedule = self.load_settings()
        chat_id = self.chat_id()
        if not chat_id:
            return None
        day = moment.date()
        due_at = _at(day, schedule.morning if kind == "morning" else schedule.evening, moment.tzinfo)
        key = "%s:%s" % (kind, day.isoformat())
        with self._lock:
            event_id = self._force_claim(key, kind, 0, day, due_at, moment)
            text, buttons = self.build(kind, event_id, moment, schedule)
            self.conn.execute(
                "UPDATE reminder_events SET state = 'sent', sends = sends + 1, "
                "sent_at = ?, updated = ? WHERE id = ?",
                (_iso(moment), _iso(moment), int(event_id)),
            )
            self.conn.commit()
        return Outgoing(
            event_id=event_id,
            kind=kind,
            chat_id=chat_id,
            text=text,
            buttons=buttons,
        )

    # -- нажатия кнопок ----------------------------------------------------

    def handle_action(self, data: str, now: Optional[datetime] = None) -> ActionOutcome:
        """Разбирает ``r:…`` из кнопки напоминания и пишет результат в БД."""
        self.ensure_schema()
        moment = now or self.now()
        parts = str(data or "").split(":")
        if len(parts) < 3 or parts[0] != "r":
            return ActionOutcome(notification="Кнопка устарела.")
        action = parts[1]
        try:
            event_id = int(parts[-1])
        except (TypeError, ValueError):
            return ActionOutcome(notification="Кнопка устарела.")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM reminder_events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return ActionOutcome(notification="Кнопка устарела.")
            state = str(row["state"] or "")
            kind = str(row["kind"] or "")
            ref_id = int(row["ref_id"] or 0)
            if action in {"done", "tomorrow", "skip", "postpone"} and state in FINAL_STATES:
                return ActionOutcome(notification="Уже обработано.")
            if action == "done":
                return self._action_done(row, moment)
            if action == "postpone":
                return self._action_postpone(row, moment)
            if action == "tomorrow":
                return self._action_tomorrow(row, moment)
            if action == "skip":
                return self._action_skip(row, moment)
            if action == "habit":
                try:
                    habit_id = int(parts[2])
                except (IndexError, TypeError, ValueError):
                    return ActionOutcome(notification="Кнопка устарела.")
                return self._action_habit(row, moment, habit_id)
            if action == "energy":
                try:
                    level = int(parts[2])
                except (IndexError, TypeError, ValueError):
                    return ActionOutcome(notification="Кнопка устарела.")
                return self._action_energy(row, moment, level)
        return ActionOutcome(notification="Кнопка устарела.")

    def _action_done(self, row: Any, now: datetime) -> ActionOutcome:
        """«Сделал»: закрывает дело, событие становится ``done``."""
        kind = str(row["kind"] or "")
        if kind != "task":
            return ActionOutcome(notification="Здесь закрывать нечего.")
        task_id = int(row["ref_id"] or 0)
        task = self.conn.execute(
            "SELECT id, title, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            return ActionOutcome(notification="Дело уже удалено.")
        title = str(task["title"] or "")
        if str(task["status"]) != "todo":
            self._set_event(row, "done", now)
            return ActionOutcome(
                notification="Уже закрыто.",
                text="Дело «%s» уже закрыто." % _clip(title),
            )
        self.conn.execute(
            "UPDATE tasks SET status = 'done', done_at = ? WHERE id = ?",
            (_stamp(), task_id),
        )
        self._set_event(row, "done", now)
        self.conn.commit()
        return ActionOutcome(
            notification="Закрыто",
            text="Закрыл дело: «%s»." % _clip(title),
        )

    def _action_postpone(self, row: Any, now: datetime) -> ActionOutcome:
        """«Отложить 1 ч»: напомнить позже, но не бесконечно."""
        kind = str(row["kind"] or "")
        if kind == "task":
            sends = int(row["sends"] or 0)
            limit = self.load_settings().max_per_task_day
            if sends >= limit:
                self._set_event(row, "skipped", now)
                return ActionOutcome(notification="Больше не напомню про это сегодня.")
        next_at = now + timedelta(minutes=POSTPONE_MIN)
        with self.conn:
            self.conn.execute(
                "UPDATE reminder_events SET state = 'postponed', next_at = ?, updated = ? "
                "WHERE id = ?",
                (_iso(next_at), _iso(now), int(row["id"])),
            )
        return ActionOutcome(
            notification="Отложено на час",
            text="",
        )

    def _action_tomorrow(self, row: Any, now: datetime) -> ActionOutcome:
        """«На завтра»: дело переезжает на завтра, напоминание снимается."""
        if str(row["kind"] or "") != "task":
            return ActionOutcome(notification="Так можно только с делом.")
        task_id = int(row["ref_id"] or 0)
        task = self.conn.execute(
            "SELECT id, title, due_date, start_at FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            return ActionOutcome(notification="Дело уже удалено.")
        due = _shift_date(task["due_date"], 1) or (now.date() + timedelta(days=1)).isoformat()
        start = _shift_date(str(task["start_at"])[:10], 1) if task["start_at"] else None
        start_at = None
        if start and task["start_at"]:
            start_at = start + str(task["start_at"])[10:19]
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET due_date = ?, start_at = ? WHERE id = ?",
                (due, start_at, task_id),
            )
        self._set_event(row, "moved", now)
        self.conn.commit()
        return ActionOutcome(
            notification="Перенёс на завтра",
            text="Перенёс на завтра: «%s» — %s." % (_clip(task["title"]), _human_day(now.date() + timedelta(days=1))),
        )

    def _action_skip(self, row: Any, now: datetime) -> ActionOutcome:
        """«Пропустить»: сегодня про это больше не напоминаем."""
        self._set_event(row, "skipped", now)
        self.conn.commit()
        return ActionOutcome(notification="Пропущено")

    def _action_habit(self, row: Any, now: datetime, habit_id: int) -> ActionOutcome:
        """Отметка привычки прямо из напоминания."""
        habit = self.conn.execute(
            "SELECT id, name FROM habits WHERE id = ?", (habit_id,)
        ).fetchone()
        if habit is None:
            return ActionOutcome(notification="Привычка не найдена.")
        day = now.date().isoformat()
        name = str(habit["name"] or "")
        with self.conn:
            self.conn.execute(
                "INSERT INTO habit_marks (habit_id, date, value) VALUES (?, ?, 1) "
                "ON CONFLICT(habit_id, date) DO UPDATE SET value = excluded.value",
                (habit_id, day),
            )
        undone = self._undone_habits(now.date())
        if not undone:
            return ActionOutcome(
                notification="Отмечено",
                text="Привычка «%s» — отмечена. Все привычки на сегодня закрыты." % _clip(name),
            )
        names = ", ".join(_clip(item["name"], 40) for item in undone[:5])
        buttons = self._habit_buttons(undone, int(row["id"]))
        return ActionOutcome(
            notification="Отмечено",
            text="Привычка «%s» — отмечена. Осталось: %s." % (_clip(name), names),
            buttons=buttons,
        )

    def _action_energy(self, row: Any, now: datetime, level: int) -> ActionOutcome:
        """Энергия 1–5 кнопкой из вечернего итога."""
        if not 1 <= level <= 5:
            return ActionOutcome(notification="Кнопка устарела.")
        try:
            store_v4.save_checkin(self.conn, self.settings, {"energy": level, "text": ""})
            self.conn.commit()
        except Exception:  # pragma: no cover - БД недоступна
            logger.exception("не удалось записать энергию из напоминания")
            return ActionOutcome(notification="Не смог записать энергию.")
        return ActionOutcome(notification="Энергия %d/5 — записал" % level)

    # -- тексты напоминаний ------------------------------------------------

    def build(
        self,
        kind: str,
        event_id: int,
        now: datetime,
        schedule: Optional[ReminderSettings] = None,
        task_id: int = 0,
    ) -> Tuple[str, List[List[Tuple[str, str]]]]:
        """Текст и кнопки напоминания (без отправки — это делает вызывающий)."""
        schedule = schedule or self.load_settings()
        if kind == "morning":
            return self._build_morning(now, event_id)
        if kind == "evening":
            return self._build_evening(now, event_id)
        if kind == "habits":
            return self._build_habits(now, event_id)
        return self._build_task(now, event_id, task_id, schedule)

    def _build_morning(self, now: datetime, event_id: int) -> Tuple[str, Any]:
        day = now.date().isoformat()
        weight = self._metric(day, "weight")
        sleep = self._metric(day, "sleep")
        tasks = self._tasks_today(now.date())
        lines = ["Утро, %s. Ритуал: вес, сон, план дня." % _human_day(now.date())]
        lines.append(
            "Вес: %s." % ("%s кг" % _fmt_number(weight) if weight else "нет записи — «вес 81,2»")
        )
        lines.append(
            "Сон: %s." % ("%s ч" % _fmt_number(sleep) if sleep else "нет записи — «спал 7,5»")
        )
        if tasks:
            titles = ", ".join("«%s»" % _clip(item["title"], 32) for item in tasks[:3])
            lines.append(
                "План: %d %s — %s."
                % (len(tasks), _plural(len(tasks), "дело", "дела", "дел"), titles)
            )
        else:
            lines.append("План: дел на сегодня нет — просто скажи, что важно.")
        return "\n".join(lines), self._postpone_buttons(event_id)

    def _build_evening(self, now: datetime, event_id: int) -> Tuple[str, Any]:
        try:
            summary = store_v4.today_summary(self.conn, self.settings)
        except Exception:  # pragma: no cover - сводка не должна глушить ритуал
            logger.exception("не удалось собрать итог дня")
            summary = "Сегодня"
        body = summary.splitlines()
        lines = ["Вечер. Итог дня."]
        lines.extend(body[1:] if len(body) > 1 else [])
        lines.append("Вопросы: что закрыл, что заметил, что завтра? Энергия — кнопкой.")
        buttons: List[List[Tuple[str, str]]] = [
            [
                ("1", "r:energy:1:%d" % event_id),
                ("2", "r:energy:2:%d" % event_id),
                ("3", "r:energy:3:%d" % event_id),
                ("4", "r:energy:4:%d" % event_id),
                ("5", "r:energy:5:%d" % event_id),
            ]
        ]
        buttons.extend(self._postpone_buttons(event_id))
        return "\n".join(lines), buttons

    def _build_habits(self, now: datetime, event_id: int) -> Tuple[str, Any]:
        undone = self._undone_habits(now.date())
        names = ", ".join(_clip(item["name"], 40) for item in undone[:6])
        if len(undone) > 6:
            names += " и ещё %d" % (len(undone) - 6)
        text = "Привычки до вечера не отмечены: %s." % names
        return text, self._habit_buttons(undone, event_id)

    def _build_task(
        self,
        now: datetime,
        event_id: int,
        task_id: int,
        schedule: ReminderSettings,
    ) -> Tuple[str, Any]:
        task = self.conn.execute(
            "SELECT id, title, start_at FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            return "", []
        title = _clip(task["title"], 80)
        start = _aware(task["start_at"], now.tzinfo)
        clock = "%02d:%02d" % (start.hour, start.minute) if start else ""
        if start is None:
            text = "Дело: «%s»." % title
        elif now < start:
            minutes = max(1, int(round((start - now).total_seconds() / 60.0)))
            text = "Через %d %s: «%s» — %s." % (
                minutes,
                _plural(minutes, "минуту", "минуты", "минут"),
                title,
                clock,
            )
        else:
            text = "Сейчас по плану: «%s» — %s." % (title, clock)
        buttons = [
            [
                ("Сделал", "r:done:%d" % event_id),
                ("Отложить 1 ч", "r:postpone:%d" % event_id),
            ],
            [
                ("На завтра", "r:tomorrow:%d" % event_id),
                ("Пропустить", "r:skip:%d" % event_id),
            ],
        ]
        return text, buttons

    def _postpone_buttons(self, event_id: int) -> List[List[Tuple[str, str]]]:
        return [
            [
                ("Отложить 1 ч", "r:postpone:%d" % event_id),
                ("Пропустить", "r:skip:%d" % event_id),
            ]
        ]

    def _habit_buttons(
        self, habits: Sequence[Any], event_id: int
    ) -> List[List[Tuple[str, str]]]:
        rows: List[List[Tuple[str, str]]] = []
        for habit in habits:
            rows.append(
                [
                    (
                        "Отметить: %s" % _clip(habit["name"], 40),
                        "r:habit:%d:%d" % (int(habit["id"]), event_id),
                    )
                ]
            )
        rows.append([("Пропустить", "r:skip:%d" % event_id)])
        return rows

    # -- состояние в БД ----------------------------------------------------

    def _event(self, key: str) -> Optional[Any]:
        return self.conn.execute(
            "SELECT * FROM reminder_events WHERE key = ?", (key,)
        ).fetchone()

    def _state(self, key: str) -> str:
        row = self._event(key)
        return str(row["state"] or "") if row is not None else ""

    def _is_final(self, key: str) -> bool:
        return self._state(key) in FINAL_STATES

    def _claim(
        self,
        key: str,
        kind: str,
        ref_id: int,
        day: Any,
        due_at: datetime,
        now: datetime,
    ) -> Optional[int]:
        """Помечает событие ``sending`` и отдаёт его id (или ``None``).

        ``None`` — событие уже обработано или ждёт повтора. Так два тика
        подряд (или перезапуск посреди отправки) не отправят дубль.
        """
        row = self._event(key)
        # Оптимистичная метка: если процесс упадёт между тиком и подтверждением
        # отправки, повтор случится не раньше, чем через RETRY_DELAY_S.
        retry_at = _iso(now + timedelta(seconds=RETRY_DELAY_S))
        if row is None:
            cursor = self.conn.execute(
                "INSERT INTO reminder_events "
                "(key, kind, ref_id, day, due_at, state, sends, attempts, next_at, updated) "
                "VALUES (?, ?, ?, ?, ?, 'sending', 0, 1, ?, ?)",
                (key, kind, int(ref_id), str(day), _iso(due_at), retry_at, _iso(now)),
            )
            self.conn.commit()
            return int(cursor.lastrowid or 0)
        state = str(row["state"] or "")
        attempts = int(row["attempts"] or 0)
        next_at = _aware(row["next_at"], now.tzinfo)
        if state == "due":
            allowed = True
        elif state == "postponed":
            allowed = next_at is None or next_at <= now
        elif state == "sending":
            # Прошлый тик не подтвердил отправку (упал процесс) — повторяем
            # ограниченное число раз, чтобы не зациклиться.
            allowed = attempts < MAX_ATTEMPTS and (next_at is None or next_at <= now)
        else:
            allowed = False
        if not allowed:
            return None
        with self.conn:
            self.conn.execute(
                "UPDATE reminder_events SET state = 'sending', attempts = attempts + 1, "
                "next_at = ?, updated = ? WHERE id = ?",
                (retry_at, _iso(now), int(row["id"])),
            )
        return int(row["id"])

    def _force_claim(
        self,
        key: str,
        kind: str,
        ref_id: int,
        day: Any,
        due_at: datetime,
        now: datetime,
    ) -> int:
        """Ручной запуск ритуала: событие дня переиспользуется, а не дублируется."""
        row = self._event(key)
        if row is None:
            cursor = self.conn.execute(
                "INSERT INTO reminder_events "
                "(key, kind, ref_id, day, due_at, state, sends, attempts, updated) "
                "VALUES (?, ?, ?, ?, ?, 'sending', 0, 1, ?)",
                (key, kind, int(ref_id), str(day), _iso(due_at), _iso(now)),
            )
            self.conn.commit()
            return int(cursor.lastrowid or 0)
        with self.conn:
            self.conn.execute(
                "UPDATE reminder_events SET state = 'sending', "
                "attempts = attempts + 1, next_at = NULL, updated = ? WHERE id = ?",
                (_iso(now), int(row["id"])),
            )
        return int(row["id"])

    def _mark(
        self,
        key: str,
        kind: str,
        ref_id: int,
        day: Any,
        due_at: datetime,
        state: str,
    ) -> None:
        """Фиксирует факт «это событие мы не отправляли» (``missed`` и др.)."""
        if self._event(key) is not None:
            return
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO reminder_events "
                "(key, kind, ref_id, day, due_at, state, sends, attempts, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)",
                (key, kind, int(ref_id), str(day), _iso(due_at), state, _iso(due_at)),
            )

    def _close(
        self,
        key: str,
        kind: str,
        ref_id: int,
        day: Any,
        due_at: datetime,
        state: str,
        now: datetime,
    ) -> None:
        """Закрывает событие: обновляет существующее, иначе создаёт ``final``."""
        row = self._event(key)
        if row is not None:
            if str(row["state"] or "") not in FINAL_STATES:
                self._set_event(row, state, now)
            return
        self._mark(key, kind, ref_id, day, due_at, state)

    def _set_event(self, row: Any, state: str, now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE reminder_events SET state = ?, next_at = NULL, updated = ? "
                "WHERE id = ?",
                (state, _iso(now), int(row["id"])),
            )

    def _task_sends(self, day: Any) -> Dict[int, int]:
        rows = self.conn.execute(
            "SELECT ref_id, COALESCE(SUM(sends), 0) AS total FROM reminder_events "
            "WHERE kind = 'task' AND day = ? GROUP BY ref_id",
            (str(day),),
        ).fetchall()
        return {int(row["ref_id"]): int(row["total"]) for row in rows}

    # -- данные для текстов ------------------------------------------------

    def _metric(self, day: str, kind: str) -> Optional[float]:
        row = self.conn.execute(
            "SELECT value FROM metrics WHERE kind = ? AND date = ? ORDER BY id DESC LIMIT 1",
            (kind, day),
        ).fetchone()
        if row is None:
            return None
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None

    def _tasks_today(self, day: Any) -> List[Any]:
        return list(
            self.conn.execute(
                "SELECT id, title FROM tasks WHERE status = 'todo' "
                "AND (due_date = ? OR substr(COALESCE(start_at, ''), 1, 10) = ?) "
                "ORDER BY CASE resource WHEN 'must' THEN 0 WHEN 'important' THEN 1 "
                "ELSE 2 END, start_at, id LIMIT 10",
                (str(day), str(day)),
            ).fetchall()
        )

    def _undone_habits(self, day: Any) -> List[Any]:
        return list(
            self.conn.execute(
                "SELECT h.id, h.name FROM habits h WHERE h.archived = 0 "
                "AND NOT EXISTS (SELECT 1 FROM habit_marks m WHERE m.habit_id = h.id "
                "AND m.date = ? AND m.value > 0) ORDER BY h.id",
                (str(day),),
            ).fetchall()
        )


# --------------------------------------------------------------------------
# Разбор значений настроек
# --------------------------------------------------------------------------


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().casefold() in {"1", "true", "on", "yes", "да", "вкл"}


def _as_int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _split_quiet(raw: Any, default: Tuple[str, str]) -> Tuple[time, time]:
    """``"23:00-08:00"`` (или пара) → окно тихих часов; иначе значение по умолчанию."""
    start_raw: Any = default[0]
    end_raw: Any = default[1]
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        start_raw, end_raw = raw[0], raw[1]
    elif isinstance(raw, str) and raw.strip():
        normalized = raw.replace("–", "-").replace("—", "-")
        parts = [part for part in re.split(r"[-\s]+", normalized) if part.strip()]
        times = re.findall(r"\d{1,2}:\d{2}", normalized)
        if len(times) >= 2:
            start_raw, end_raw = times[0], times[1]
        elif len(parts) >= 2:
            start_raw, end_raw = parts[0], parts[1]
    start = parse_hm(start_raw) or parse_hm(default[0])
    end = parse_hm(end_raw) or parse_hm(default[1])
    return start, end


def _shift_date(raw: Any, days: int) -> Optional[str]:
    """Сдвигает ISO-дату на ``days`` дней; ``None``, если дата битая."""
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (day + timedelta(days=days)).isoformat()
