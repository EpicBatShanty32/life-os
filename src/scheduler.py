"""Планировщик Life OS — сердце бота.

Отвечает за:
* слоты опроса (morning / midday / evening / weekly) по Europe/Moscow;
* диалоговые сессии: вопрос → ответ → уточнение (follow-up через LLM) →
  следующий вопрос; состояние сессии лежит в kv-ключе ``session`` (INTERFACES §8);
* команды Telegram и свободный чат;
* сохранение каждого ответа в SQLite (``storage.add_entry``), запись в Obsidian
  (``vault.write_entry``) и пересборку дашборда (``dash_build``);
* информативные русские тексты: status / today / report / goals.

Модуль не бросает исключения в цикл main: все ошибки LLM/vault/dashboard
ловятся и логируются, чат продолжает работать.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import parsing, prompts

__all__ = ["Scheduler"]

_SLOT_ORDER = ("morning", "midday", "evening", "weekly")

# Подписи на случай, если prompts.SLOT_LABELS недоступны/изменены.
_FALLBACK_SLOT_LABELS = {
    "morning": "утро",
    "midday": "день",
    "evening": "вечер",
    "weekly": "неделя",
}
_SLOT_TITLES = {
    "morning": "Утро",
    "midday": "День",
    "evening": "Вечер",
    "weekly": "Неделя",
    "free": "Свободный чат",
}
_CATEGORY_LABELS = {
    "tasks": "задачи",
    "food": "еда",
    "training": "тренировка",
    "goals": "цели",
    "mood": "настроение",
    "journal": "мысли",
    "sleep": "сон",
    "water": "вода",
    "other": "прочее",
}
_STATUS_LABELS = {
    "active": "активна",
    "done": "достигнута",
    "paused": "на паузе",
    "dropped": "снята",
}
_WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# Пропуск текущего вопроса / прерывание сессии.
_SKIP_TOKENS = {"-", "—", "–", "пропустить", "пропусти", "скип", "skip"}
_STOP_TOKENS = {"стоп", "stop", "хватит", "прекрати", "отмена"}

# Если prompts.scripted_questions сломается, бот всё равно должен спрашивать.
_FALLBACK_QUESTIONS = {
    "morning": [
        "Какой у тебя план на сегодня?",
        "Какие три главные задачи?",
        "Как дела с целями — что продвинет их сегодня?",
    ],
    "midday": [
        "Что уже ел сегодня?",
        "Сколько воды выпил?",
        "Как энергия и настроение?",
    ],
    "evening": [
        "Как прошла тренировка (или движение/прогулка)?",
        "Что сделал по задачам, что перенёс?",
        "Что по еде за день и как самочувствие?",
        "Как настроение, энергия и фокус?",
        "Что важного произошло, о чём стоит помнить?",
    ],
    "weekly": [
        "Что было главным на этой неделе?",
        "Что получилось, а что нет?",
        "Что хочется изменить на следующей неделе?",
    ],
}
_FALLBACK_GREETINGS = {
    "morning": "Доброе утро! Давай сверимся с планами на день.",
    "midday": "Привет! Как дела? Давай коротко про еду, воду и состояние.",
    "evening": "Добрый вечер! Как прошёл день? Давай сохраним итоги.",
    "weekly": "Привет! Давай подведём итоги недели и заглянем в следующую.",
}
_CLOSING_TEXTS = {
    "morning": "Спасибо! План сохранил, дашборд обновил. Хорошего дня — писать можно в любой момент.",
    "midday": "Спасибо, всё записал. Если что-то изменится — просто напиши.",
    "evening": "Спасибо за день и за искренность. Всё сохранил, дашборд обновил. Отдыхай.",
    "weekly": "Спасибо! Итоги недели сохранил. Дальше — по плану новой недели.",
}

# Алиасы команд: латиница со слэшем и русские слова без слэша.
_COMMAND_ALIASES = {
    "start": "start",
    "старт": "start",
    "pause": "pause",
    "пауза": "pause",
    "resume": "resume",
    "продолжить": "resume",
    "возобновить": "resume",
    "status": "status",
    "статус": "status",
    "today": "today",
    "сегодня": "today",
    "report": "report",
    "отчёт": "report",
    "отчет": "report",
    "goals": "goals",
    "цели": "goals",
    "add_goal": "add_goal",
    "добавить_цель": "add_goal",
    "новая_цель": "add_goal",
    "skip": "skip",
    "пропустить": "skip",
    "пропусти": "skip",
    "help": "help",
    "помощь": "help",
    "дизайн": "design",
    "design": "design",
    "дашборд": "design",
    "dashboard": "design",
    "stop": "stop",
    "стоп": "stop",
}

_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _to_float(value) -> float | None:
    """Достаёт число из метрики: 7, "7.5", "7,5 ч", "2000 мл" → 7.0/7.5/2000."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUM_RE.search(_text(value).replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _fmt_num(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return ("%.1f" % value).rstrip("0").rstrip(".")


class Scheduler:
    """Планировщик слотов, диалогов и команд.

    Args:
        cfg: весь конфиг (см. config.load_config).
        storage: Storage — база.
        tg: Telegram-клиент (нужен метод ``send(chat_id, text)``). В dry-run
            подменяется заглушкой, которая пишет в лог.
        llm: LLM-клиент (``available()``, ``chat(messages)``).
        vault: Vault или None (``write_entry``, ``sync_pending``, ``rewrite_goals``).
        dash_build: callable(cfg, storage) -> str | None или None.
        logger: logging.Logger или None.
    """

    SESSION_KEY = "session"

    def __init__(self, cfg: dict, storage, tg, llm, vault, dash_build, logger) -> None:
        self.cfg = cfg or {}
        self.storage = storage
        self.tg = tg
        self.llm = llm
        self.vault = vault
        self.dash_build = dash_build
        self.log = logger or logging.getLogger("lifeos.scheduler")

        tz_name = _text((self.cfg.get("app") or {}).get("timezone")) or "Europe/Moscow"
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 — кривую таймзону уже отловил config
            self.tz = ZoneInfo("Europe/Moscow")

        telegram_cfg = self.cfg.get("telegram") or {}
        try:
            self.chat_id = int(telegram_cfg.get("allowed_chat_id") or 0)
        except (TypeError, ValueError):
            self.chat_id = 0
        llm_cfg = self.cfg.get("llm") or {}
        try:
            self.max_followups = int(llm_cfg.get("max_followups", 2) or 0)
        except (TypeError, ValueError):
            self.max_followups = 2
        self.schedule = self.cfg.get("schedule") or {}
        self.app_cfg = self.cfg.get("app") or {}
        # Итог последней сборки дашборда: нужен ролловеру, чтобы не отмечать
        # день успешным при упавшем dash_build (REVIEW P2.5).
        self._dashboard_ok = True

    # ================================================================ ВРЕМЯ
    def _local(self, now: datetime | None = None) -> datetime:
        """Приводит время к таймзоне приложения (naive считаем локальным)."""
        if now is None:
            return datetime.now(self.tz)
        if now.tzinfo is None:
            return now.replace(tzinfo=self.tz)
        return now.astimezone(self.tz)

    def _slot_label(self, slot: str) -> str:
        labels = getattr(prompts, "SLOT_LABELS", None)
        if isinstance(labels, dict) and labels.get(slot):
            return _text(labels[slot])
        return _FALLBACK_SLOT_LABELS.get(slot, slot)

    def _category_label(self, category: str) -> str:
        return _CATEGORY_LABELS.get(category or "other", category or "прочее")

    def _today_human(self, day) -> str:
        return "%s (%s)" % (day.strftime("%d.%m.%Y"), _WEEKDAY_SHORT[day.weekday()])

    def _short_date(self, day) -> str:
        return day.strftime("%d.%m")

    def _tz_label(self) -> str:
        key = _text(getattr(self.tz, "key", "")) or _text(self.app_cfg.get("timezone"))
        return "МСК" if key == "Europe/Moscow" else key

    # ============================================================== ОТПРАВКА
    def _send(self, chat_id: int, text: str, slot: str | None = None) -> None:
        """Отправляет сообщение и пишет историю. Ошибки Telegram не роняют бота."""
        if text is None:
            return
        text = _text(text)
        if not text.strip():
            return
        try:
            self.tg.send(chat_id, text)
        except Exception as exc:  # noqa: BLE001 — сеть/Telegram/мок
            status = getattr(exc, "status", None)
            retry_after = getattr(exc, "retry_after", None)
            # 429: один короткий повтор — дальше пусть main делает backoff.
            if status == 429 and retry_after and float(retry_after) <= 10:
                try:
                    time.sleep(float(retry_after))
                    self.tg.send(chat_id, text)
                except Exception:  # noqa: BLE001
                    self.log.warning("Telegram: повторная отправка не удалась (%s)", exc)
                    return
            else:
                self.log.warning("Telegram: отправка не удалась (%s)", exc)
                return
        if self.storage is not None:
            try:
                self.storage.record_message(chat_id, "out", text, slot=slot)
            except Exception:  # noqa: BLE001
                self.log.exception("Не удалось записать исходящее сообщение в БД")

    # ================================================================ СЕССИЯ
    def _load_session(self) -> dict | None:
        raw = self.storage.kv_get(self.SESSION_KEY)
        if not raw:
            return None
        try:
            session = json.loads(raw)
        except (ValueError, TypeError):
            self.log.warning("Битый JSON сессии в kv — сбрасываю")
            self.storage.kv_del(self.SESSION_KEY)
            return None
        if not isinstance(session, dict) or not session.get("slot"):
            self.storage.kv_del(self.SESSION_KEY)
            return None
        return session

    def _save_session(self, session: dict) -> None:
        if self.storage is not None:
            self.storage.kv_set(self.SESSION_KEY, json.dumps(session, ensure_ascii=False))

    def _clear_session(self) -> None:
        if self.storage is not None:
            self.storage.kv_del(self.SESSION_KEY)

    def _is_paused(self) -> bool:
        if self.storage is None:
            return False
        return _text(self.storage.kv_get("paused")).strip() in {"1", "true", "yes", "on"}

    # ------------------------------------------------------------ онбординг
    def _onboarding_required(self) -> bool:
        """Знакомство перед первым вопросом включается флагом app.onboarding."""
        return bool(self.app_cfg.get("onboarding"))

    def _is_onboarded(self) -> bool:
        if self.storage is None:
            return True
        return _text(self.storage.kv_get("onboarded")).strip() == "1"

    def _mark_onboarded(self) -> None:
        if self.storage is None:
            return
        try:
            self.storage.kv_set("onboarded", "1")
            self.storage.kv_set("onboarded_at", datetime.now().astimezone().isoformat(timespec="seconds"))
            self.storage.kv_del("onboarding_choice")
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось сохранить флаг онбординга")

    def _quiet_window(self) -> tuple[int, int] | None:
        spec = self.app_cfg.get("quiet_hours")
        if not isinstance(spec, (list, tuple)) or len(spec) != 2:
            return None
        start = self._parse_time(spec[0])
        end = self._parse_time(spec[1])
        if start is None or end is None:
            return None
        return (start[0] * 60 + start[1], end[0] * 60 + end[1])

    def _in_quiet_hours(self, now: datetime) -> bool:
        window = self._quiet_window()
        if window is None:
            return False
        start, end = window
        if start == end:
            return False
        current = now.hour * 60 + now.minute
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _onboarding_text(self) -> str:
        name = _text(self.app_cfg.get("name")) or "Life OS"
        return (
            "Привет! Я %s — дневник-собеседник.\n\n"
            "Несколько раз в день спрашиваю про дела, тело и голову и складываю "
            "ответы в твой Obsidian на этом Mac.\n\n"
            "Дальше я напишу сам: утром — про план, днём — про еду и силы, "
            "вечером — про итоги. Отвечать можно свободным текстом.\n\n"
            "Начать сейчас? Напиши «да» или «вечером».\n\n"
            "Приватность: записи хранятся локально. Если включены «умные» "
            "уточнения (llm.enabled), текст ответов уходит на api.samp.team — "
            "их можно выключить в config.json, и тогда всё останется только у тебя."
            % name
        )

    def _quiet_text(self) -> str:
        return (
            "Поздно уже, не буду грузить вопросами. Напиши, если хочешь что-то "
            "записать, — сохраню. Или продолжим утром."
        )

    def _handle_onboarding_choice(self, chat_id: int, text: str, now: datetime) -> bool:
        """Первое сообщение после знакомства: «да» — начать, «вечером» — ждать.

        Возвращает True, если сообщение обработано как выбор онбординга.
        """
        low = text.strip().lower().strip("!.,… ")
        start_words = {"да", "начать", "сейчас", "давай", "давай сейчас", "поехали", "ок", "окей"}
        wait_words = {"вечером", "потом", "нет", "позже", "не сейчас", "в следующий раз"}
        if low in start_words:
            self._mark_onboarded()
            if self._in_quiet_hours(now):
                self._send(chat_id, self._quiet_text())
                return True
            hour = now.hour
            slot = "morning" if hour < 12 else ("midday" if hour < 18 else "evening")
            self._start_session(slot, now)
            return True
        if low in wait_words:
            self._mark_onboarded()
            self._send(
                chat_id,
                "Договорились. Напишу в ближайший слот — а если что-то захочется "
                "записать раньше, просто пиши сюда.",
            )
            return True
        self._send(chat_id, "Напиши «да», чтобы начать сейчас, или «вечером» — подожду до вечера.")
        return True

    def _questions(self, slot: str) -> list[str]:
        """Скриптованные вопросы слота из prompts с fallback-набором."""
        questions: list[str] = []
        try:
            raw = prompts.scripted_questions(slot)
            if isinstance(raw, (list, tuple)):
                questions = [_text(item).strip() for item in raw if _text(item).strip()]
        except Exception:  # noqa: BLE001
            self.log.exception("prompts.scripted_questions упал для слота %s", slot)
        if not questions:
            questions = list(_FALLBACK_QUESTIONS.get(slot, _FALLBACK_QUESTIONS["morning"]))
        return questions

    def _greeting(self, slot: str) -> str:
        name = _text(self.app_cfg.get("user_name")).strip()
        try:
            text = prompts.greeting(slot, name)
            if _text(text).strip():
                return _text(text).strip()
        except Exception:  # noqa: BLE001
            self.log.exception("prompts.greeting упал для слота %s", slot)
        return _FALLBACK_GREETINGS.get(slot, "Привет! Давай сверимся с днём.")

    def _closing_text(self, slot: str) -> str:
        return _CLOSING_TEXTS.get(slot, "Спасибо, всё сохранил. Писать можно в любой момент.")

    # ============================================================ TICK / СЛОТЫ
    def tick(self, now: datetime) -> None:
        """Периодический вызов из main: просроченные сессии, слоты, ролловер.

        Сначала закрываем/напоминаем про зависшую сессию, и только потом
        проверяем слоты: иначе сессия вчерашнего дня блокирует старт
        сегодняшнего слота до следующего тика (REVIEW P2.4).
        """
        now = self._local(now)
        try:
            self._tick_overdue(now)
        except Exception:  # noqa: BLE001
            self.log.exception("tick: ошибка при проверке просроченных сессий")
        try:
            self._tick_slots(now)
        except Exception:  # noqa: BLE001
            self.log.exception("tick: ошибка при запуске слота")
        try:
            self._tick_rollover(now)
        except Exception:  # noqa: BLE001
            self.log.exception("tick: ошибка ежедневного ролловера")

    def _parse_time(self, value) -> tuple[int, int] | None:
        match = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", _text(value))
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    def _catchup_minutes(self) -> float:
        """Сколько минут опоздания ещё можно начать слот (иначе — мягкий пинг)."""
        raw = self.schedule.get("catchup_minutes", 180)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 180.0

    def _tick_slots(self, now: datetime) -> None:
        """1) Если не пауза и время слота пришло — начать сессию."""
        if self._is_paused():
            return
        if self._onboarding_required() and not self._is_onboarded():
            return  # сначала знакомство (/start), потом вопросы
        if self._in_quiet_hours(now):
            return  # тихие часы: не будим вопросами
        if self._load_session() is not None:
            return  # одна сессия за раз

        today = now.date().isoformat()
        due: list[tuple[float, str]] = []
        for slot in _SLOT_ORDER:
            spec = self.schedule.get(slot)
            if not isinstance(spec, dict) or not spec.get("enabled"):
                continue
            hm = self._parse_time(spec.get("time"))
            if hm is None:
                continue
            if slot == "weekly":
                weekday = spec.get("weekday", 6)
                try:
                    weekday = int(weekday)
                except (TypeError, ValueError):
                    weekday = 6
                if now.weekday() != weekday:
                    continue
            slot_dt = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if now < slot_dt:
                continue
            if _text(self.storage.kv_get("last_slot:%s" % slot)) == today:
                continue
            late_minutes = (now - slot_dt).total_seconds() / 60.0
            due.append((late_minutes, slot))

        if not due:
            return

        catchup = self._catchup_minutes()
        ready = sorted(item for item in due if item[0] <= catchup)
        if ready:
            # Самый «свежий» из наступивших слотов (на случай долгого простоя).
            self._start_session(ready[0][1], now)
            return

        # Все слоты сильно опоздали: мягко напомним про вечер, без сессии.
        if {slot for _, slot in due} & {"evening"} and self._schedule_flag("soft_ping", True):
            self._soft_ping_evening(now)

    def _schedule_flag(self, key: str, default: bool) -> bool:
        value = self.schedule.get(key, default)
        if isinstance(value, bool):
            return value
        return bool(value)

    def _start_session(self, slot: str, now: datetime) -> None:
        chat_id = self.chat_id
        if not chat_id:
            self._warn_no_chat(now)
            return
        questions = self._questions(slot)
        first = questions[0] if questions else ""
        greeting = self._greeting(slot)
        session = {
            "slot": slot,
            "idx": 0,
            "step": "question",
            "followups": 0,
            "question": first,
            "asked": first,
            "started_at": now.isoformat(timespec="seconds"),
            "last_activity_at": now.isoformat(timespec="seconds"),
            "date": now.date().isoformat(),
            "answered": [],
        }
        # Сначала фиксируем состояние, потом отправляем: крах после отправки
        # не должен приводить к повторному старту в тот же день.
        self._save_session(session)
        try:
            self.storage.kv_set("last_slot:%s" % slot, now.date().isoformat())
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось отметить слот %s", slot)
        text = greeting + ("\n\n" + first if first else "")
        self.log.info("Старт сессии «%s» (%d вопросов)", slot, len(questions))
        self._send(chat_id, text, slot=slot)

    def _warn_no_chat(self, now: datetime) -> None:
        key = "warn_no_chat:%s" % now.date().isoformat()
        if self.storage.kv_get(key):
            return
        try:
            self.storage.kv_set(key, "1")
        except Exception:  # noqa: BLE001
            pass
        self.log.warning(
            "Не задан telegram.allowed_chat_id — плановые сессии не отправляются. "
            "Укажите id своего чата в config/config.json (подсказка: @userinfobot)."
        )

    def _soft_ping_evening(self, now: datetime) -> None:
        if not self.chat_id:
            self._warn_no_chat(now)
            return
        key = "evening_ping:%s" % now.date().isoformat()
        if self.storage.kv_get(key):
            return
        try:
            self.storage.kv_set(key, "1")
            self._send(
                self.chat_id,
                "Привет! День почти прошёл. Если хочешь, расскажи в двух словах, "
                "как он прошёл, — я аккуратно запишу в дневник. "
                "Можно и просто написать что угодно, свободным текстом.",
                slot="evening",
            )
            self.log.info("Отправлен мягкий вечерний пинг (вечерний слот пропущен)")
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось отправить мягкий пинг")

    def _tick_overdue(self, now: datetime) -> None:
        """2) Закрыть/напомнить про зависшую сессию."""
        session = self._load_session()
        if session is None:
            return
        today = now.date().isoformat()
        if session.get("date") != today:
            self.log.info("Закрываю сессию прошлого дня (%s)", session.get("date"))
            self._clear_session()
            return
        started_at = self._parse_iso(session.get("last_activity_at")
                                     or session.get("started_at"))
        if started_at is None:
            return
        idle_hours = (now - started_at).total_seconds() / 3600.0
        if idle_hours >= self._session_ttl_hours():
            self.log.info("Сессия «%s» висит %.1f ч — закрываю", session.get("slot"), idle_hours)
            self._clear_session()
            return
        if idle_hours >= self._session_reminder_hours() and not session.get("reminded"):
            session["reminded"] = True
            self._save_session(session)
            if self.chat_id:
                self._send(
                    self.chat_id,
                    "Если ещё актуально — продолжим с того же места: просто ответь "
                    "на вопрос выше. Если нет — напиши /skip или /stop.",
                    slot=session.get("slot"),
                )
                self.log.info("Отправлено напоминание о незавершённой сессии")

    def _session_reminder_hours(self) -> float:
        return self._cfg_hours("session_reminder_hours", 3.0)

    def _session_ttl_hours(self) -> float:
        return self._cfg_hours("session_ttl_hours", 12.0)

    def _cfg_hours(self, key: str, default: float) -> float:
        try:
            return max(0.0, float(self.app_cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _parse_iso(self, value) -> datetime | None:
        text = _text(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return self._local(parsed) if parsed.tzinfo else parsed.replace(tzinfo=self.tz)

    def _tick_rollover(self, now: datetime) -> None:
        """3) Раз в день: досинхронизировать Obsidian и пересобрать дашборд.

        Маркер ``last_rollover`` пишется только после успешной работы: если
        vault/дашборд упали, следующий тик повторит ролловер в этот же день
        (REVIEW P2.5), а не отложит его до завтра.
        """
        today = now.date().isoformat()
        try:
            if _text(self.storage.kv_get("last_rollover")) == today:
                return
        except Exception:  # noqa: BLE001
            self.log.exception("Ролловер: не удалось прочитать kv")
            return
        self.log.info("Ролловер дня %s", today)

        sync_ok = self._sync_vault() >= 0
        goals_ok = self._rewrite_goals()
        mirror_ok = self._write_mirror_safe()
        self._rebuild_dashboard()
        dash_ok = self._dashboard_ok
        if not (sync_ok and goals_ok and mirror_ok and dash_ok):
            self.log.warning(
                "Ролловер дня %s завершился с ошибками — повторю на следующем тике",
                today,
            )
            return
        try:
            self.storage.kv_set("last_rollover", today)
        except Exception:  # noqa: BLE001
            self.log.exception(
                "Ролловер: не удалось сохранить last_rollover — повторю на следующем тике"
            )

    def _sync_vault(self) -> int:
        """Досинхронизация Obsidian; -1 — ошибка (для повтора ролловера)."""
        if self.vault is None or not hasattr(self.vault, "sync_pending"):
            return 0
        try:
            written = self.vault.sync_pending()
            written = int(written or 0)
        except Exception:  # noqa: BLE001
            self.log.exception("Vault.sync_pending упал")
            return -1
        if written:
            self.log.info("Досинхронизировано записей в Obsidian: %d", written)
        return written

    def _rewrite_goals(self) -> bool:
        """True — готово (в т.ч. если vault нет), False — ошибка."""
        if self.vault is None or not hasattr(self.vault, "rewrite_goals"):
            return True
        try:
            self.vault.rewrite_goals()
        except Exception:  # noqa: BLE001
            self.log.exception("Vault.rewrite_goals упал")
            return False
        return True

    def _rebuild_dashboard(self) -> str | None:
        if self.dash_build is None:
            self._dashboard_ok = True
            return None
        try:
            path = self.dash_build(self.cfg, self.storage)
        except Exception:  # noqa: BLE001
            self.log.exception("Сборка дашборда упала")
            self._dashboard_ok = False
            return None
        self._dashboard_ok = bool(path)
        return path if isinstance(path, str) else None

    # ============================================================== ВХОДЯЩИЕ
    def on_message(self, chat_id: int, text: str, now: datetime) -> None:
        """Обрабатывает любое входящее сообщение: команда, ответ, свободный чат."""
        now = self._local(now)
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            self.log.warning("Некорректный chat_id: %r — игнорирую", chat_id)
            return
        if not self._is_allowed(chat_id):
            return

        text = _text(text)
        session = self._load_session()
        try:
            self.storage.record_message(
                chat_id, "in", text, slot=(session or {}).get("slot")
            )
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось записать входящее сообщение в БД")

        stripped = text.strip()
        if not stripped:
            self._send(chat_id, "Пустое сообщение. Напиши /help, если нужны подсказки.")
            return

        try:
            command = self._parse_command(stripped)
            if command is not None:
                self._handle_command(command[0], command[1], chat_id, now)
                return

            # Первый выбор после знакомства («да» / «вечером») — до сессий.
            if (
                self._onboarding_required()
                and not self._is_onboarded()
                and _text(self.storage.kv_get("onboarding_choice")).strip() == "1"
            ):
                if self._handle_onboarding_choice(chat_id, stripped, now):
                    return

            session = self._load_session()
            if session is not None:
                self._handle_session_reply(chat_id, stripped, now, session)
            else:
                self._handle_free_text(chat_id, stripped, now)
        except Exception:  # noqa: BLE001 — чат не должен падать из-за одной ошибки
            self.log.exception("Ошибка обработки сообщения")
            try:
                self._send(
                    chat_id,
                    "Что-то пошло не так на моей стороне. Попробуй ещё раз "
                    "или напиши /status.",
                )
            except Exception:  # noqa: BLE001
                self.log.exception("Не удалось отправить сообщение об ошибке")

    def _is_allowed(self, chat_id: int) -> bool:
        if not self.chat_id:
            return True  # chat_id не настроен (dev/dry-run) — не блокируем
        if chat_id != self.chat_id:
            self.log.warning("Сообщение от чужого chat_id=%s проигнорировано", chat_id)
            return False
        return True

    # ============================================================== КОМАНДЫ
    def _parse_command(self, text: str) -> tuple[str, str] | None:
        """Возвращает (команда, аргументы) или None, если это не команда."""
        stripped = text.strip()
        if stripped.startswith("/"):
            head, _, rest = stripped.partition(" ")
            name = head[1:].split("@", 1)[0].strip().lower()
            if not name:
                return None
            return (_COMMAND_ALIASES.get(name, "unknown"), rest.strip())
        bare = stripped.rstrip("!.").strip().lower()
        command = _COMMAND_ALIASES.get(bare)
        if command:
            return command, ""
        return None

    def _handle_command(self, command: str, args: str, chat_id: int, now: datetime) -> None:
        if command == "start":
            if self._onboarding_required() and not self._is_onboarded():
                self.storage.kv_set("onboarding_choice", "1")
                self._send(chat_id, self._onboarding_text())
            elif self._load_session() is not None:
                self._send(
                    chat_id,
                    "Мы на середине вопросов. Продолжить — просто ответь на последний "
                    "вопрос, или /stop, чтобы остановить сессию.",
                )
            else:
                self._send(chat_id, self._start_text())
        elif command == "pause":
            self.storage.kv_set("paused", "1")
            self._send(
                chat_id,
                "⏸ Пауза включена: плановые вопросы приходить не будут. "
                "Ты можешь писать мне сам, а вернуть опросы — /resume (или «продолжить»).",
            )
            self.log.info("Пользователь поставил бота на паузу")
        elif command == "resume":
            was_paused = self._is_paused()
            self.storage.kv_set("paused", "0")
            self._send(
                chat_id,
                "▶️ Пауза снята, работаю по расписанию." if was_paused
                else "▶️ Я и так работаю по расписанию — паузы не было.",
            )
        elif command == "status":
            self._send(chat_id, self.status_text())
        elif command == "today":
            self._send(chat_id, self.today_text())
        elif command == "report":
            self._send(chat_id, self.report_text())
        elif command == "goals":
            self._send(chat_id, self.goals_text())
        elif command == "add_goal":
            self._handle_add_goal(chat_id, args)
        elif command == "skip":
            session = self._load_session()
            if session is None:
                self._send(chat_id, "Сейчас нет активной сессии — пропускать нечего. /status покажет состояние.")
            else:
                self._advance(chat_id, now, session, skipped=True)
        elif command == "stop":
            session = self._load_session()
            if session is None:
                self._send(chat_id, "Сейчас нет активной сессии. Писать свободным текстом всё равно можно.")
            else:
                self._clear_session()
                self._send(chat_id, "Ок, сессию остановил. Ответы, которые ты уже дал, сохранены. Вернёмся в следующий слот или пиши что хочешь.")
                self.log.info("Сессия остановлена пользователем")
        elif command == "help":
            self._send(chat_id, self.help_text())
        elif command == "design":
            path = self._rebuild_dashboard()
            if path:
                self._send(chat_id, "🎨 Дашборд обновлён.\nФайл: %s" % path)
            elif self.dash_build is None:
                self._send(chat_id, "Дашборд пока не подключён (нет модуля dashboard_data).")
            else:
                self._send(chat_id, "Не удалось собрать дашборд — смотри лог агента, данные не потеряны.")
        else:
            self._send(
                chat_id,
                "Не знаю такую команду. Список всех команд — /help. "
                "А свободный текст я всегда понимаю.",
            )

    def _handle_add_goal(self, chat_id: int, args: str) -> None:
        raw = _text(args).strip()
        if not raw:
            self._send(
                chat_id,
                "Чтобы добавить цель, напиши в формате:\n"
                "/add_goal Название | прогресс | срок\n"
                "Например: /add_goal Пробежать 10 км | 3/10 | 2027-06-01\n"
                "Срок и текущий прогресс можно не указывать.",
            )
            return
        parts = [part.strip() for part in raw.split("|")]
        title = parts[0] if parts else ""
        if not title:
            self._send(chat_id, "Не понял название цели. Напиши: /add_goal Название | прогресс | срок")
            return
        # Прогресс можно задать как «3/10» или «3 из 10»; иначе это просто цель.
        progress_raw = parts[1] if len(parts) > 1 else ""
        current = ""
        target = progress_raw
        match = re.match(r"^\s*([\d.,]+)\s*(?:/|из)\s*([\d.,]+)\s*$", progress_raw)
        if match:
            current, target = match.group(1), match.group(2)
        goal = {
            "title": title,
            "target": target,
            "current": current,
            "deadline": parts[2] if len(parts) > 2 else "",
            "status": "active",
        }
        try:
            goal_id = self.storage.upsert_goal(goal)
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось сохранить цель")
            self._send(chat_id, "Не получилось сохранить цель — попробуй ещё раз.")
            return
        self._rewrite_goals()
        self._write_mirror_safe()
        details = []
        if current and target:
            details.append("прогресс %s из %s" % (current, target))
        elif goal["target"]:
            details.append("цель: %s" % goal["target"])
        if goal["deadline"]:
            details.append("срок: %s" % goal["deadline"])
        suffix = (" (%s)" % ", ".join(details)) if details else ""
        self._send(chat_id, "🎯 Цель добавлена: %s%s.\nВсе цели — /goals." % (title, suffix))
        self.log.info("Добавлена цель id=%s: %s", goal_id, title)

    def _start_text(self) -> str:
        name = _text(self.app_cfg.get("name")) or "Life OS"
        return (
            "Привет! Я %s — твой персональный дневник-собеседник.\n\n"
            "Я сам пишу несколько раз в день: утром — про план, днём — про еду и "
            "состояние, вечером — про итоги дня. Отвечать можно в любой момент, "
            "в том числе свободным текстом — всё аккуратно запишу в дневник и "
            "отражу на дашборде.\n\n"
            "Полезное:\n"
            "• /today — записи за сегодня\n"
            "• /report — отчёт за 7 дней\n"
            "• /goals — цели и прогресс\n"
            "• /status — что сейчас происходит\n"
            "• /help — все команды\n\n"
            "Приватность: записи хранятся в твоём Obsidian на этом Mac. Если включены "
            "«умные» уточнения (llm.enabled), текст ответов уходит на api.samp.team — "
            "их можно отключить в config.json, тогда всё останется только у тебя.\n\n"
            "Если сейчас неудобно — /pause, вернуться — /resume."
            % name
        )

    def help_text(self) -> str:
        return (
            "Команды Life OS:\n"
            "/start — приветствие\n"
            "/status — состояние: пауза, слоты, активная сессия\n"
            "/today — записи за сегодня\n"
            "/report — итоги за 7 дней\n"
            "/goals — цели и прогресс\n"
            "/add_goal Название | 3/10 | срок — добавить цель\n"
            "/skip — пропустить текущий вопрос\n"
            "/pause — пауза (плановые вопросы не приходят)\n"
            "/resume — продолжить\n"
            "/дизайн — пересобрать дашборд (то же: «дашборд»)\n"
            "/stop — прервать сессию\n"
            "/help — эта справка\n\n"
            "Свободный текст можно писать в любой момент: отвечу и сохраню в дневник.\n\n"
            "Приватность: записи лежат локально в Obsidian. «Умные» уточнения "
            "(llm.enabled) отправляют текст ответов на api.samp.team; выключи "
            "llm.enabled в config.json — и данные останутся только у тебя."
        )

    # ======================================================= ДИАЛОГ: СЕССИЯ
    def _handle_session_reply(self, chat_id: int, text: str, now: datetime,
                              session: dict) -> None:
        low = text.strip().lower()
        if low in _STOP_TOKENS:
            self._clear_session()
            self._send(chat_id, "Ок, сессию прервал. Уже сказанное сохранено — продолжим в следующий слот.")
            return
        if low in _SKIP_TOKENS:
            self._advance(chat_id, now, session, skipped=True)
            return

        slot = _text(session.get("slot")) or "morning"
        question = _text(session.get("asked")) or _text(session.get("question"))
        self._save_answer(chat_id, slot, question, text, now)

        session["last_activity_at"] = now.isoformat(timespec="seconds")
        answered = session.get("answered")
        if not isinstance(answered, list):
            answered = []
        if len(answered) < 40:
            answered.append({"question": question[:160], "answer": text[:200]})
        session["answered"] = answered

        followup = self._maybe_followup(slot, _text(session.get("question")), text, session)
        if followup:
            try:
                followups = int(session.get("followups") or 0)
            except (TypeError, ValueError):
                followups = 0
            session["followups"] = followups + 1
            session["step"] = "followup"
            session["asked"] = followup
            self._save_session(session)
            self._send(chat_id, followup, slot=slot)
            return

        self._advance(chat_id, now, session)

    def _maybe_followup(self, slot: str, question: str, answer: str,
                        session: dict) -> str | None:
        """Решает, нужен ли уточняющий вопрос (LLM). None — не нужен/не можем."""
        if not self._llm_available():
            return None
        try:
            followups = int(session.get("followups") or 0)
        except (TypeError, ValueError):
            followups = 0
        if followups >= self.max_followups:
            return None
        # ТЗ §3.2: уточняем только короткий ответ, иначе идём дальше.
        if len(answer.strip()) >= 30:
            return None
        try:
            messages = prompts.followup_messages(slot, question, answer, self._llm_history())
            reply = self.llm.chat(messages)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Follow-up через LLM не получился: %s", exc)
            return None
        cleaned = _text(reply).strip()
        if not cleaned or self._followup_is_skip(cleaned):
            return None
        return cleaned

    def _followup_is_skip(self, reply: str) -> bool:
        """LLM ответил «OK/ОК» — уточнение не нужно (используем prompts, если есть).

        Если ``prompts.followup_is_skip`` вернул False (или упал), пробуем
        запасной ``_is_ok``: модели иногда отвечают «Хорошо»/«Готово» вместо
        строгого «OK» (REVIEW P2.7).
        """
        helper = getattr(prompts, "followup_is_skip", None)
        if callable(helper):
            try:
                if bool(helper(reply)):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return _is_ok(reply)

    def _advance(self, chat_id: int, now: datetime, session: dict,
                 skipped: bool = False) -> None:
        """Перейти к следующему вопросу или завершить сессию."""
        slot = _text(session.get("slot")) or "morning"
        questions = self._questions(slot)
        try:
            idx = int(session.get("idx") or 0) + 1
        except (TypeError, ValueError):
            idx = 1
        session["followups"] = 0
        session["step"] = "question"
        session["last_activity_at"] = now.isoformat(timespec="seconds")
        if idx < len(questions):
            session["idx"] = idx
            session["question"] = questions[idx]
            session["asked"] = questions[idx]
            self._save_session(session)
            prefix = "Хорошо, пропустил.\n\n" if skipped else ""
            self._send(chat_id, prefix + questions[idx], slot=slot)
            return

        self._clear_session()
        self.log.info("Сессия «%s» завершена", slot)
        self._write_mirror_safe()
        self._send(chat_id, self._closing_text(slot), slot=slot)

    # ==================================================== ДИАЛОГ: СВОБОДНЫЙ
    def _handle_free_text(self, chat_id: int, text: str, now: datetime) -> None:
        reply: str | None = None
        if self._llm_available():
            try:
                messages = [{"role": "system", "content": prompts.system_prompt()}]
                messages.extend(self._llm_history(limit=12))
                reply = self.llm.chat(messages)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("LLM не ответил в свободном чате: %s", exc)
        if not _text(reply).strip():
            reply = self._free_fallback_reply(text)
        self._send(chat_id, _text(reply).strip(), slot="free")
        self._save_answer(chat_id, "free", "", text, now)

    def _free_fallback_reply(self, text: str) -> str:
        snippet = text.strip()
        if len(snippet) > 300:
            snippet = snippet[:300].rstrip() + "…"
        return (
            "Записал: «%s». Если хочешь, расскажи подробнее — сохраню всё в дневник."
            % snippet
        )

    # ========================================================= СОХРАНЕНИЕ
    def _save_answer(self, chat_id: int, slot: str, question: str,
                     answer: str, now: datetime) -> int | None:
        """Ответ → entry (parsing) → SQLite → Obsidian → дашборд."""
        now_iso = now.isoformat(timespec="seconds")
        entry: dict | None = None
        try:
            entry = parsing.parse_answer(self.llm, slot, question, answer, now_iso)
        except Exception:  # noqa: BLE001
            self.log.exception("parsing.parse_answer упал — сохраняю ответ как есть")
        if not isinstance(entry, dict):
            entry = {}

        entry["date"] = _text(entry.get("date")) or now.date().isoformat()
        entry["ts"] = _text(entry.get("ts")) or now_iso
        entry["slot"] = _text(entry.get("slot")) or slot
        entry["category"] = _text(entry.get("category")) or "other"
        entry["title"] = _text(entry.get("title")) or self._short_title(answer)
        entry["text"] = _text(entry.get("text")) or answer
        entry["raw"] = _text(entry.get("raw")) or answer
        if not isinstance(entry.get("metrics"), dict):
            entry["metrics"] = {}

        try:
            entry_id = self.storage.add_entry(entry)
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось сохранить запись в SQLite")
            return None
        entry["id"] = int(entry_id)

        if self._write_to_vault(entry):
            try:
                self.storage.set_entry_written(int(entry_id), True)
            except Exception:  # noqa: BLE001
                self.log.exception("Не удалось отметить запись written=1")
        self._write_mirror_safe()
        self._rebuild_dashboard()
        return int(entry_id)

    def _write_to_vault(self, entry: dict) -> bool:
        if self.vault is None:
            return False
        obsidian_cfg = self.cfg.get("obsidian") or {}
        if obsidian_cfg.get("enabled") is False:
            return False
        try:
            return bool(self.vault.write_entry(entry))
        except Exception:  # noqa: BLE001
            self.log.exception("Vault.write_entry упал — запись останется в БД")
            return False

    def _write_mirror_safe(self) -> bool:
        """Обновляет machine-readable зеркало Obsidian (entries.jsonl/goals.json).

        Vault.write_mirror() ждёт полный список записей; контракт Storage не
        меняем — берём все записи через entries() с широким диапазоном дат.
        Любые ошибки только логируются: зеркало вторично по отношению к БД/дневнику.
        Возвращает False при ошибке — ролловер тогда повторится (REVIEW P2.5).
        """
        if self.vault is None or not hasattr(self.vault, "write_mirror"):
            return True
        obsidian_cfg = self.cfg.get("obsidian") or {}
        if obsidian_cfg.get("enabled") is False:
            return True
        try:
            all_entries = self.storage.entries("0001-01-01", "9999-12-31")
        except Exception:  # noqa: BLE001
            self.log.exception("Не удалось прочитать записи для зеркала Obsidian")
            return False
        try:
            self.vault.write_mirror(all_entries)
        except Exception:  # noqa: BLE001
            self.log.exception(
                "Vault.write_mirror упал — данные в БД и дневнике не потеряны"
            )
            return False
        return True

    def _short_title(self, text: str, limit: int = 80) -> str:
        for line in _text(text).splitlines():
            line = line.strip()
            if line:
                return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
        return "запись"

    # ========================================================== LLM-ПОМОЩЬ
    def _llm_available(self) -> bool:
        try:
            return bool(self.llm is not None and self.llm.available())
        except Exception:  # noqa: BLE001
            return False

    def _llm_history(self, limit: int = 10) -> list[dict]:
        """История диалога в формате OpenAI chat messages."""
        try:
            rows = self.storage.chat_history(limit)
        except Exception:  # noqa: BLE001
            return []
        messages: list[dict] = []
        for row in rows:
            content = _text(row.get("text")).strip()
            if not content:
                continue
            role = "user" if _text(row.get("direction")) == "in" else "assistant"
            messages.append({"role": role, "content": content})
        return messages

    # ================================================================ ТЕКСТЫ
    def status_text(self) -> str:
        now = datetime.now(self.tz)
        today = now.date().isoformat()
        paused = self._is_paused()
        session = self._load_session()

        lines = ["📊 Life OS — статус", ""]
        lines.append("Состояние: %s" % ("⏸ на паузе (вернуться: /resume)" if paused else "✅ работает"))
        lines.append("Время: %s, %s" % (self._today_human(now.date()), now.strftime("%H:%M")))

        slot_parts = []
        for slot in ("morning", "midday", "evening"):
            spec = self.schedule.get(slot) or {}
            label = self._slot_label(slot)
            if not isinstance(spec, dict) or not spec.get("enabled"):
                slot_parts.append("%s — выкл" % label)
                continue
            hm = self._parse_time(spec.get("time"))
            sent_today = _text(self.storage.kv_get("last_slot:%s" % slot)) == today
            if sent_today:
                slot_parts.append("%s ✅" % label)
            elif hm is not None and (now.hour, now.minute) >= hm:
                slot_parts.append("%s ⏳ ждёт" % label)
            else:
                slot_parts.append("%s 🕓 %s" % (label, _text(spec.get("time"))))
        weekly = self.schedule.get("weekly") or {}
        if isinstance(weekly, dict) and weekly.get("enabled"):
            try:
                weekday = int(weekly.get("weekday", 6))
            except (TypeError, ValueError):
                weekday = 6
            if weekday == now.weekday():
                done = _text(self.storage.kv_get("last_slot:weekly")) == today
                slot_parts.append("неделя %s" % ("✅" if done else "🕓 %s" % _text(weekly.get("time"))))
            else:
                slot_parts.append("неделя — %s" % _WEEKDAY_SHORT[weekday % 7])
        else:
            slot_parts.append("неделя — выкл")
        lines.append("Слоты: " + " · ".join(slot_parts))

        if session is not None:
            slot = _text(session.get("slot")) or "morning"
            try:
                idx = int(session.get("idx") or 0) + 1
            except (TypeError, ValueError):
                idx = 1
            total = len(self._questions(slot))
            step = "уточняющий вопрос" if session.get("step") == "followup" else "вопрос"
            lines.append(
                "Сессия: %s — %s %d из %d" % (self._slot_label(slot), step, idx, total)
            )
        else:
            lines.append("Сессия: сейчас нет")

        entries: list[dict] = []
        try:
            entries = self.storage.entries(today, today)
        except Exception:  # noqa: BLE001
            self.log.exception("status: не удалось прочитать записи")
        lines.append("Записей сегодня: %d" % len(entries))
        if entries:
            counts: dict[str, int] = {}
            for entry in entries:
                category = _text(entry.get("category")) or "other"
                counts[category] = counts.get(category, 0) + 1
            summary = ", ".join(
                "%s %d" % (self._category_label(cat), counts[cat])
                for cat in sorted(counts, key=lambda c: (-counts[c], c))
            )
            lines.append("По категориям: %s" % summary)
            last = entries[-1]
            lines.append(
                "Последняя запись: %s — %s"
                % (self._entry_time(last), _text(last.get("title")) or "без названия")
            )
        try:
            goals = self.storage.goals()
            active = [g for g in goals if _text(g.get("status")) in ("", "active")]
            if goals:
                lines.append("Цели: %d, активных %d (/goals)" % (len(goals), len(active)))
        except Exception:  # noqa: BLE001
            self.log.exception("status: не удалось прочитать цели")
        return "\n".join(lines)

    def today_text(self) -> str:
        now = datetime.now(self.tz)
        today = now.date().isoformat()
        header = "📅 Сегодня, %s" % self._today_human(now.date())
        try:
            entries = self.storage.entries(today, today)
        except Exception:  # noqa: BLE001
            self.log.exception("today: не удалось прочитать записи")
            entries = []
        if not entries:
            return (
                header
                + "\n\nЗаписей пока нет. Бот спросит сам в ближайший слот — "
                "или просто напиши что-нибудь свободным текстом."
            )

        grouped: dict[str, list[dict]] = {}
        for entry in entries:
            slot = _text(entry.get("slot")) or "free"
            grouped.setdefault(slot, []).append(entry)
        order = [slot for slot in _SLOT_ORDER + ("free",) if slot in grouped]
        unknown = sorted(set(grouped) - set(order))
        order.extend(unknown)

        lines = [header, ""]
        for slot in order:
            lines.append(self._slot_title(slot))
            for entry in grouped[slot]:
                title = _text(entry.get("title")) or self._short_title(_text(entry.get("text")))
                category = _text(entry.get("category"))
                suffix = " · %s" % self._category_label(category) if category else ""
                lines.append("• %s — %s%s" % (self._entry_time(entry), title, suffix))
            lines.append("")

        totals = self._today_totals(entries)
        if totals:
            lines.append(totals)
        lines.append("Итого: %d записей." % len(entries))
        return "\n".join(lines).strip()

    def _slot_title(self, slot: str) -> str:
        if slot in _SLOT_TITLES:
            return _SLOT_TITLES[slot]
        return _text(slot)

    def _today_totals(self, entries: list[dict]) -> str:
        parts = []
        water = sum(v for v in (self._metric(entry, "water_ml") for entry in entries) if v)
        if water:
            parts.append("вода %s мл" % _fmt_num(water))
        sleep = self._metric_values(entries, "sleep_h")
        if sleep:
            parts.append("сон %s ч" % _fmt_num(sum(sleep) / len(sleep)))
        moods = self._metric_values(entries, "mood")
        if moods:
            parts.append("настроение %s/5" % _fmt_num(sum(moods) / len(moods)))
        return ("За день: " + ", ".join(parts) + ".") if parts else ""

    def report_text(self) -> str:
        now = datetime.now(self.tz)
        date_to = now.date()
        date_from = date_to - timedelta(days=6)
        header = "📈 Отчёт за 7 дней: %s — %s" % (
            self._short_date(date_from), self._short_date(date_to)
        )
        try:
            entries = self.storage.entries(date_from.isoformat(), date_to.isoformat())
        except Exception:  # noqa: BLE001
            self.log.exception("report: не удалось прочитать записи")
            entries = []
        if not entries:
            return header + "\n\nЗаписей за неделю нет. Начни с ответа на ближайший вопрос или напиши что-нибудь свободным текстом."

        days = {_text(entry.get("date")) for entry in entries}
        lines = [header, ""]
        lines.append("Записей: %d · дней с записями: %d из 7" % (len(entries), len(days)))
        checkin_days = {
            _text(entry.get("date"))
            for entry in entries
            if _text(entry.get("slot")) in _SLOT_ORDER
        }
        lines.append("Чек-инов слотов: %d из 7 дней" % len(checkin_days))

        sleep = self._metric_values(entries, "sleep_h")
        if sleep:
            lines.append("Сон: в среднем %s ч (%d записей)" % (_fmt_num(sum(sleep) / len(sleep)), len(sleep)))
        waters = self._metric_values(entries, "water_ml")
        if waters:
            lines.append("Вода: в среднем %s мл на запись (%d записей)" % (_fmt_num(sum(waters) / len(waters)), len(waters)))
        moods = self._metric_values(entries, "mood")
        if moods:
            lines.append("Настроение: %s из 5" % _fmt_num(sum(moods) / len(moods)))
        energies = self._metric_values(entries, "energy")
        if energies:
            lines.append("Энергия: %s из 5" % _fmt_num(sum(energies) / len(energies)))

        training_days = {
            _text(entry.get("date"))
            for entry in entries
            if _text(entry.get("category")) == "training"
        }
        if training_days:
            lines.append("Тренировки: %d дн." % len(training_days))

        done_tasks = 0
        planned_tasks = 0
        for entry in entries:
            done = entry.get("metrics", {}).get("tasks_done")
            planned = entry.get("metrics", {}).get("tasks_planned")
            if isinstance(done, list):
                done_tasks += len(done)
            if isinstance(planned, list):
                planned_tasks += len(planned)
        if done_tasks or planned_tasks:
            lines.append("Задачи: выполнено %d, запланировано %d" % (done_tasks, planned_tasks))

        counts: dict[str, int] = {}
        for entry in entries:
            category = _text(entry.get("category")) or "other"
            counts[category] = counts.get(category, 0) + 1
        top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        if top:
            lines.append("Категории: " + ", ".join(
                "%s %d" % (self._category_label(cat), num) for cat, num in top
            ))

        try:
            goals = self.storage.goals()
        except Exception:  # noqa: BLE001
            goals = []
        if goals:
            active = [g for g in goals if _text(g.get("status")) in ("", "active")]
            lines.append("Цели: %d, активных %d — /goals" % (len(goals), len(active)))
        return "\n".join(lines)

    def goals_text(self) -> str:
        try:
            goals = self.storage.goals()
        except Exception:  # noqa: BLE001
            self.log.exception("goals: не удалось прочитать цели")
            goals = []
        if not goals:
            return (
                "🎯 Цели\n\nЦелей пока нет. Добавить можно так:\n"
                "/add_goal Читать 12 книг | 4 | 2027-01-01\n"
                "Формат: Название | сколько уже | срок (всё, кроме названия, опционально)."
            )
        lines = ["🎯 Цели (%d)" % len(goals), ""]
        for number, goal in enumerate(goals, 1):
            title = _text(goal.get("title")) or "без названия"
            bits = []
            progress = self._goal_progress(goal)
            if progress is not None:
                bits.append("%d%%" % progress)
                target = _to_float(goal.get("target"))
                current = _to_float(goal.get("current"))
                if target is not None and current is not None and target > 0:
                    bits.append("%s из %s" % (_fmt_num(current), _fmt_num(target)))
            else:
                target_text = _text(goal.get("target")).strip()
                if target_text:
                    bits.append("цель %s" % target_text)
            deadline = _text(goal.get("deadline")).strip()
            if deadline:
                bits.append("срок %s" % deadline)
            status = _text(goal.get("status")).strip() or "active"
            bits.append(_STATUS_LABELS.get(status, status))
            lines.append("%d. %s — %s" % (number, title, " · ".join(bits)))
        lines.append("")
        lines.append("Добавить: /add_goal Название | прогресс | срок")
        return "\n".join(lines)

    # -------------------------------------------------------- метрики/цели
    def _metric(self, entry: dict, key: str) -> float | None:
        metrics = entry.get("metrics")
        if not isinstance(metrics, dict):
            return None
        return _to_float(metrics.get(key))

    def _metric_values(self, entries: list[dict], key: str) -> list[float]:
        values = []
        for entry in entries:
            value = self._metric(entry, key)
            if value is not None:
                values.append(value)
        return values

    def _goal_progress(self, goal: dict) -> int | None:
        target = _to_float(goal.get("target"))
        current = _to_float(goal.get("current"))
        if target is not None and target > 0 and current is not None:
            percent = int(round(current / target * 100.0))
            return max(0, min(100, percent))
        # Иногда прогресс пишут прямо в note/current: "60%".
        for field in ("current", "note", "target"):
            match = re.search(r"(\d{1,3})\s*%", _text(goal.get(field)))
            if match:
                return max(0, min(100, int(match.group(1))))
        return None

    def _entry_time(self, entry: dict) -> str:
        ts = _text(entry.get("ts"))
        if ts:
            try:
                parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return parsed.strftime("%H:%M")
            except ValueError:
                if len(ts) >= 16:
                    return ts[11:16]
        return "--:--"


def _is_ok(text: str) -> bool:
    """True, если ответ LLM — сигнал «уточнение не нужно» (OK/ОК)."""
    return text.strip().strip(".!").upper() in {"OK", "ОК", "ОКЕЙ", "ХОРОШО"}
