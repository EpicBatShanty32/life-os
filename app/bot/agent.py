"""Автономный агент Life OS: инструменты, память и журнал действий.

Смысл модуля — убрать «угадывание фразы по шаблонам». Модель получает
инструменты над той же базой, что и сайт, и сама решает: что прочитать,
что записать и что ответить человеку. Здесь три части:

* реестр инструментов (:data:`TOOLS`) — тонкие обёртки над ``store_v4``;
* цикл ReAct (:meth:`Agent.respond`) — модель зовёт инструменты, пока не
  скажет что-то человеку;
* следы и память: ``agent_messages`` (короткая история), ``agent_journal``
  (что сделано — по нему откат), ``agent_memory`` (досье), ``agent_thoughts``
  (отложенные мысли).

Модуль ничего не ломает, пока его не подключили: ``BotService`` берёт агента
только если он доступен (есть ключ LLM), иначе работает прежний разбор
правилами. Любой сбой модели на любом шаге — ``None``, и вызывающий код
уходит на запасной путь, а не показывает человеку ошибку.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from app.bot import intent_v4, store_v4

logger = logging.getLogger(__name__)

#: Сколько шагов «думай → зови инструмент → смотри результат» разрешаем.
MAX_STEPS = 5
#: Сколько последних реплик диалога подмешивать в контекст.
HISTORY_LIMIT = 10
#: Сколько сообщений хранить в истории (остальное — мусор для модели).
HISTORY_KEEP = 60
#: Максимальная длина ответа в Telegram.
REPLY_LIMIT = 3500

WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

PERSONA = """Ты — личный ассистент Life OS в Telegram. Не бот с командами, а живой \
собеседник: сам раскладываешь записи по базе и говоришь по-человечески.

Правила:
1. Сначала инструменты, потом слова. Если человек рассказал про дела, деньги, \
привычки, состояние или идею — запиши это инструментами, не спрашивая разрешения.
2. Из одной фразы может выйти несколько записей — сделай все, что слышишь.
3. Отвечай коротко и живо: одно-три предложения. Никаких анкет и перечислений полей.
4. Запрещены штампы: «Задача успешно добавлена», «Ваш запрос обработан», \
«Чем я ещё могу помочь», «Всё записано в базу данных». Пиши как человек человеку.
5. Спрашивай, только если без ответа можно записать неправильно: две «Ани», \
5000 — доход или возврат долга, непонятная дата. Один вопрос, не допрос.
6. Если человек устал, злится или болеет — сначала признай состояние, потом дело. \
В таком состоянии не задавай больше одного вопроса.
7. После записи коротко скажи, что именно записал (сумма, дело, дата). \
Не перечисляй служебные поля.
8. Не выдумывай числа. Вопрос «сколько я потратил» — сначала инструмент, потом ответ.
9. Ночь и отдых: если человек пишет ночью или просит не трогать — просто зафиксируй \
без встречных вопросов.
10. «Отмени последнее» → инструмент undo_last. «Я в отпуске / не пиши первым» — \
запомни это через remember.
11. Не пиши markdown, звёздочки и списки, если не просили. Обычный текст.

Сегодня: {today}.
Что в базе сейчас:
{snapshot}

Что ты знаешь о человеке:
{memory}
"""


# --------------------------------------------------------------------------
# Мелкие помощники
# --------------------------------------------------------------------------


@dataclass
class ToolResult:
    """Что инструмент вернул: текст для модели и операции для отката."""

    text: str
    operations: List[Dict[str, Any]] = field(default_factory=list)
    label: str = ""


@dataclass
class Step:
    """Один вызов инструмента в рамках ответа."""

    tool: str
    args: Dict[str, Any]
    text: str
    ok: bool = True


@dataclass
class AgentReply:
    """Готовый ответ агента человеку."""

    text: str
    steps: List[Step] = field(default_factory=list)
    engine: str = "agent"
    latency_ms: int = 0

    def tools_used(self) -> List[str]:
        return [step.tool for step in self.steps]


@dataclass
class _Ctx:
    conn: Any
    settings: Any


@contextmanager
def _locked(lock: Optional[Any]) -> Iterator[None]:
    """Берёт замок БД, если он есть (в тестах и CLI может не быть)."""
    if lock is None:
        yield
        return
    with lock:
        yield


def _now(settings: Any) -> datetime:
    """Текущее время в часовом поясе приложения."""
    method = getattr(settings, "now", None)
    if callable(method):
        try:
            return method()
        except Exception:  # pragma: no cover - защита от мока
            pass
    name = str(getattr(settings, "timezone", "") or "Europe/Moscow")
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(name))
    except Exception:  # pragma: no cover - экзотические зоны
        return datetime.now()


def _today(settings: Any) -> str:
    method = getattr(settings, "today", None)
    if callable(method):
        try:
            return str(method())
        except Exception:  # pragma: no cover
            pass
    return _now(settings).date().isoformat()


def _stamp(settings: Any) -> str:
    return _now(settings).isoformat(timespec="seconds")


def _today_human(settings: Any) -> str:
    moment = _now(settings)
    return "%s, %s, %s" % (
        moment.date().isoformat(),
        WEEKDAYS[moment.weekday()],
        moment.strftime("%H:%M"),
    )


def _as_date(value: Any, settings: Any) -> str:
    """Дата из ISO или из русской фразы; пусто, если не понять."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        pass
    try:
        parsed, _phrase = intent_v4.parse_date_phrase(text, _now(settings).date())
    except Exception:
        return ""
    return parsed or ""


def _as_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return intent_v4.parse_time_phrase(text) or ""
    except Exception:
        return ""


def _kopecks(value: Any) -> int:
    try:
        amount = float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return 0
    if amount <= 0:
        return 0
    return int(round(amount * 100))


def _tags(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip()[:40] for item in value if str(item).strip()][:8]
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip()[:40] for part in text.split(",") if part.strip()][:8]


# --------------------------------------------------------------------------
# Инструменты: тонкие обёртки над store_v4
# --------------------------------------------------------------------------


def _tool_create_task(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    title = str(args.get("title") or "").strip()
    if not title:
        return ToolResult("Не понял, какое дело создать: нужен короткий заголовок.")
    data: Dict[str, Any] = {"title": title[:200], "text": str(args.get("text") or title)[:800]}
    due = _as_date(args.get("due_date"), ctx.settings)
    if due:
        data["due_date"] = due
    start = _as_time(args.get("start_time"))
    if start:
        data["start_time"] = start
        data.setdefault("due_date", due or _today(ctx.settings))
    minutes = args.get("minutes")
    if minutes:
        try:
            data["minutes"] = max(0, min(int(float(minutes)), 600))
        except (TypeError, ValueError):
            pass
    if args.get("project"):
        data["project"] = str(args["project"])[:80]
    outcome = store_v4.create_task(ctx.conn, ctx.settings, data)
    return ToolResult(outcome.text, list(outcome.operations), outcome.label)


def _tool_complete_task(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    outcome = store_v4.complete_task(
        ctx.conn, ctx.settings, {"title": str(args.get("title") or "")[:200]}
    )
    return ToolResult(outcome.text, list(outcome.operations), outcome.label)


def _money(ctx: _Ctx, args: Mapping[str, Any], kind: str) -> ToolResult:
    amount = _kopecks(args.get("amount"))
    if amount <= 0:
        return ToolResult("Не разобрал сумму — назови число, например «2400».")
    data: Dict[str, Any] = {
        "amount_kopecks": amount,
        "category": str(args.get("category") or "")[:60],
        "note": str(args.get("note") or args.get("text") or "")[:300],
        "account": str(args.get("account") or "")[:40],
    }
    day = _as_date(args.get("date"), ctx.settings)
    if day:
        data["date"] = day
    outcome = store_v4.add_transaction(ctx.conn, ctx.settings, data, kind)
    return ToolResult(outcome.text, list(outcome.operations), outcome.label)


def _tool_add_expense(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    return _money(ctx, args, "expense")


def _tool_add_income(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    return _money(ctx, args, "income")


def _tool_add_journal(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolResult("Пустая мысль — напиши текст.")
    data: Dict[str, Any] = {"text": text[:4000], "engine": "agent"}
    mood = str(args.get("mood") or "").strip()
    if mood:
        data["mood"] = mood[:60]
    tags = _tags(args.get("topics") or args.get("tags"))
    if tags:
        data["topics"] = tags
    if str(args.get("journal_kind") or "") in {"activity", "state"}:
        data["journal_kind"] = str(args["journal_kind"])
    outcome = store_v4.add_journal(ctx.conn, ctx.settings, data)
    return ToolResult(outcome.text, list(outcome.operations), outcome.label)


def _tool_add_note(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolResult("Пустая заметка — напиши, что сохранить.")
    outcome = store_v4.save_inbox(ctx.conn, ctx.settings, text[:4000])
    return ToolResult(outcome.text, list(outcome.operations), outcome.label)


def _tool_log_habit(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    text = str(args.get("text") or args.get("habit") or "").strip()
    if not text:
        return ToolResult("Не понял, какую привычку отметить.")
    data = {"text": text[:400], "habit": str(args.get("habit") or "")[:80]}
    outcome = store_v4.mark_habit(ctx.conn, ctx.settings, data)
    return ToolResult(outcome.text, list(outcome.operations), outcome.label)


def _tool_checkin(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    try:
        energy = int(float(args.get("energy") or 0))
    except (TypeError, ValueError):
        energy = 0
    if not 1 <= energy <= 5:
        return ToolResult("Нужна энергия от 1 до 5 — например 3.")
    data = {
        "energy": energy,
        "note": str(args.get("note") or args.get("text") or "")[:2000],
        "mood": str(args.get("mood") or "")[:60],
    }
    outcome = store_v4.save_checkin(ctx.conn, ctx.settings, data)
    return ToolResult(outcome.text, list(outcome.operations), outcome.label)


def _tool_query_day(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    return ToolResult(store_v4.today_summary(ctx.conn, ctx.settings))


def _tool_query_week(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    return ToolResult(store_v4.week_summary(ctx.conn, ctx.settings))


def _tool_finance(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    period = str(args.get("period") or "week").strip()
    if period not in {"day", "week", "month"}:
        period = "week"
    return ToolResult(store_v4.finance_summary(ctx.conn, ctx.settings, period))


def _tool_search(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if len(query) < 2:
        return ToolResult("Слишком короткий запрос для поиска.")
    needle = "%" + query.replace("%", "").replace("_", "") + "%"
    found: List[str] = []
    try:
        rows = ctx.conn.execute(
            "SELECT text, ts FROM entries WHERE text LIKE ? ORDER BY id DESC LIMIT 5",
            (needle,),
        ).fetchall()
        for row in rows:
            found.append("запись %s: %s" % (str(row["ts"])[:10], str(row["text"])[:160]))
    except Exception:  # pragma: no cover - старые базы
        logger.debug("поиск по entries не удался", exc_info=True)
    try:
        rows = ctx.conn.execute(
            "SELECT title, due_date, status FROM tasks WHERE title LIKE ? "
            "ORDER BY id DESC LIMIT 5",
            (needle,),
        ).fetchall()
        for row in rows:
            found.append(
                "дело «%s» (%s, %s)"
                % (str(row["title"])[:120], str(row["due_date"] or "без даты"), str(row["status"]))
            )
    except Exception:  # pragma: no cover
        logger.debug("поиск по tasks не удался", exc_info=True)
    try:
        rows = ctx.conn.execute(
            "SELECT text FROM inbox_items WHERE text LIKE ? ORDER BY id DESC LIMIT 5",
            (needle,),
        ).fetchall()
        for row in rows:
            found.append("заметка: %s" % str(row["text"])[:160])
    except Exception:  # pragma: no cover
        logger.debug("поиск по inbox не удался", exc_info=True)
    if not found:
        return ToolResult("По запросу «%s» ничего не нашёл." % query[:60])
    return ToolResult("Нашёл:\n" + "\n".join(found[:8]))


def _tool_undo(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    removed, label = undo_last(ctx.conn)
    if removed:
        return ToolResult("Отменил последнее: %s." % label)
    return ToolResult("Отменять нечего — журнал пуст.")


def _tool_remember(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    key = str(args.get("key") or args.get("fact") or "").strip()
    value = str(args.get("value") or args.get("fact") or "").strip()
    if not key or not value:
        return ToolResult("Нужны и тема, и факт: например key='стиль', value='короткие ответы'.")
    remember(ctx.conn, key, value, ctx.settings)
    return ToolResult("Запомнил: %s — %s." % (key[:60], value[:160]))


def _tool_schedule_thought(ctx: _Ctx, args: Mapping[str, Any]) -> ToolResult:
    note = str(args.get("note") or "").strip()
    if not note:
        return ToolResult("Не понял, о чём напомнить — нужен текст.")
    due = _as_date(args.get("when"), ctx.settings)
    clock = _as_time(args.get("when"))
    if due:
        due_ts = "%sT%s:00" % (due, clock or "10:00")
    else:
        due_ts = ( _now(ctx.settings) + timedelta(hours=1) ).isoformat(timespec="seconds")
    thought_id = add_thought(ctx.conn, due_ts, note[:400], ctx.settings)
    return ToolResult(
        "Запомнил, что вернуться к этому %s (мысль #%d)." % (due_ts[:16].replace("T", " "), thought_id)
    )


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    properties: Dict[str, Any]
    required: Tuple[str, ...]
    handler: Any

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                },
            },
        }


_S = {"type": "string"}
_N = {"type": "number"}
_I = {"type": "integer"}

TOOLS: Dict[str, _Tool] = {
    tool.name: tool
    for tool in (
        _Tool(
            "create_task",
            "Создать дело в Life OS. Дату бери из фразы; если даты нет — не указывай (поставлю сегодня).",
            {
                "title": {**_S, "description": "Короткое название дела"},
                "due_date": {**_S, "description": "Дата в ISO (2026-11-20) или по-русски («завтра»)"},
                "start_time": {**_S, "description": "Время начала, HH:MM"},
                "minutes": {**_I, "description": "Оценка длительности в минутах"},
                "project": {**_S, "description": "Проект, если человек его назвал"},
                "text": {**_S, "description": "Исходная формулировка человека"},
            },
            ("title",),
            _tool_create_task,
        ),
        _Tool(
            "complete_task",
            "Закрыть дело. Укажи название так, как оно есть в базе, или как сказал человек.",
            {"title": {**_S, "description": "Название дела"}},
            ("title",),
            _tool_complete_task,
        ),
        _Tool(
            "add_expense",
            "Записать расход. Сумма в рублях (2400, а не 240000).",
            {
                "amount": {**_N, "description": "Сумма в рублях"},
                "category": {**_S, "description": "Категория: продукты, кафе, транспорт, шопинг…"},
                "note": {**_S, "description": "На что потрачено"},
                "account": {**_S, "description": "Счёт, если назван"},
                "date": {**_S, "description": "Дата траты, если не сегодня"},
            },
            ("amount",),
            _tool_add_expense,
        ),
        _Tool(
            "add_income",
            "Записать доход (зарплата, возврат долга, подработка). Сумма в рублях.",
            {
                "amount": {**_N, "description": "Сумма в рублях"},
                "category": {**_S, "description": "Категория дохода"},
                "note": {**_S, "description": "Откуда пришло"},
                "account": {**_S, "description": "Счёт"},
                "date": {**_S, "description": "Дата"},
            },
            ("amount",),
            _tool_add_income,
        ),
        _Tool(
            "add_journal",
            "Записать мысль, состояние, рефлексию или событие дня в дневник.",
            {
                "text": {**_S, "description": "Текст записи своими словами человека"},
                "mood": {**_S, "description": "Настроение одним словом"},
                "topics": {"type": "array", "items": {**_S}, "description": "Темы"},
                "journal_kind": {**_S, "description": "state (состояние) или activity (что делал)"},
            },
            ("text",),
            _tool_add_journal,
        ),
        _Tool(
            "add_note",
            "Сохранить идею, ссылку или заметку в инбокс — когда это не дело и не дневник.",
            {
                "text": {**_S, "description": "Текст заметки"},
                "tags": {"type": "array", "items": {**_S}, "description": "Теги"},
            },
            ("text",),
            _tool_add_note,
        ),
        _Tool(
            "log_habit",
            "Отметить привычку за сегодня: вода, бег, прогулка, чтение, не курить.",
            {
                "text": {**_S, "description": "Как человек сказал: «выпил 2 литра»"},
                "habit": {**_S, "description": "Название привычки, если понятно"},
            },
            ("text",),
            _tool_log_habit,
        ),
        _Tool(
            "checkin",
            "Чек-ин состояния: энергия 1–5 плюс заметка о самочувствии.",
            {
                "energy": {**_I, "description": "Энергия от 1 до 5"},
                "note": {**_S, "description": "Что происходит: устал, голова квадратная"},
                "mood": {**_S, "description": "Настроение"},
            },
            ("energy",),
            _tool_checkin,
        ),
        _Tool(
            "query_day",
            "Посмотреть, что сегодня: дела, деньги, еда, привычки, энергия.",
            {},
            (),
            _tool_query_day,
        ),
        _Tool(
            "query_week",
            "Сводка за неделю: дела, деньги, привычки, энергия.",
            {},
            (),
            _tool_query_week,
        ),
        _Tool(
            "finance_summary",
            "Сколько потрачено и получено за период.",
            {"period": {"type": "string", "enum": ["day", "week", "month"]}},
            (),
            _tool_finance,
        ),
        _Tool(
            "search",
            "Найти в базе: записи дневника, дела, заметки по словам.",
            {"query": {**_S, "description": "Слова для поиска"}},
            ("query",),
            _tool_search,
        ),
        _Tool(
            "undo_last",
            "Отменить последнее действие агента в базе (запись, задачу, трату).",
            {},
            (),
            _tool_undo,
        ),
        _Tool(
            "remember",
            "Запомнить устойчивый факт о человеке: стиль общения, ценности, контекст, просьбу.",
            {
                "key": {**_S, "description": "Короткая тема: стиль, работа, здоровье, просьба"},
                "value": {**_S, "description": "Сам факт"},
            },
            ("key", "value"),
            _tool_remember,
        ),
        _Tool(
            "schedule_thought",
            "Вернуться к теме позже: сам напиши человеку в нужный момент.",
            {
                "when": {**_S, "description": "Когда: ISO-время, дата или «завтра в 15»"},
                "note": {**_S, "description": "О чём напомнить"},
            },
            ("when", "note"),
            _tool_schedule_thought,
        ),
    )
}


def tool_schemas() -> List[Dict[str, Any]]:
    """Схемы инструментов в формате OpenAI."""
    return [tool.schema() for tool in TOOLS.values()]


# --------------------------------------------------------------------------
# Память, журнал и отложенные мысли
# --------------------------------------------------------------------------


def remember(conn: Any, key: str, value: str, settings: Any) -> None:
    """Кладёт факт в досье (перезаписывает тот же ключ)."""
    conn.execute(
        "INSERT INTO agent_memory(key, value, updated) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
        (str(key)[:60], str(value)[:600], _stamp(settings)),
    )


def memories(conn: Any, limit: int = 20) -> List[Tuple[str, str]]:
    """Досье для промпта: свежие факты первыми."""
    try:
        rows = conn.execute(
            "SELECT key, value FROM agent_memory ORDER BY updated DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    except Exception:  # pragma: no cover - база без миграции
        return []
    return [(str(row[0]), str(row[1])) for row in rows]


def journal_add(
    conn: Any,
    tool: str,
    args: Mapping[str, Any],
    result: str,
    operations: Sequence[Mapping[str, Any]],
    label: str,
    settings: Any,
) -> int:
    """Пишет действие агента в журнал; возвращает id записи."""
    cursor = conn.execute(
        "INSERT INTO agent_journal(ts, tool, args, result, operations, label, undone) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (
            _stamp(settings),
            str(tool)[:60],
            json.dumps(dict(args), ensure_ascii=False)[:2000],
            str(result)[:1000],
            json.dumps(list(operations), ensure_ascii=False) if operations else "",
            str(label)[:200],
        ),
    )
    return int(cursor.lastrowid or 0)


def journal_recent(conn: Any, limit: int = 10) -> List[Dict[str, Any]]:
    """Последние действия агента — для «что ты записал» и отладки."""
    try:
        rows = conn.execute(
            "SELECT id, ts, tool, result, label, undone FROM agent_journal "
            "ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    except Exception:  # pragma: no cover
        return []
    return [dict(row) for row in rows]


def undo_last(conn: Any) -> Tuple[bool, str]:
    """Откатывает последнее неотменённое действие. ``(сработало, подпись)``."""
    try:
        row = conn.execute(
            "SELECT id, operations, label FROM agent_journal "
            "WHERE undone = 0 AND operations <> '' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:  # pragma: no cover
        return False, ""
    if row is None:
        return False, ""
    try:
        operations = json.loads(str(row["operations"]))
    except (TypeError, ValueError):
        operations = []
    if not isinstance(operations, list) or not operations:
        return False, ""
    removed, label = store_v4.delete_record(
        conn, {"operations": operations, "label": str(row["label"] or "запись")}
    )
    if not removed:
        return False, ""
    conn.execute("UPDATE agent_journal SET undone = 1 WHERE id = ?", (int(row["id"]),))
    return True, label or str(row["label"] or "запись")


def add_thought(conn: Any, due_ts: str, note: str, settings: Any) -> int:
    """Отложенная мысль: агент сам напишет, когда придёт время."""
    cursor = conn.execute(
        "INSERT INTO agent_thoughts(due_ts, note, status, created) VALUES (?, ?, 'pending', ?)",
        (str(due_ts)[:19], str(note)[:400], _stamp(settings)),
    )
    return int(cursor.lastrowid or 0)


def due_thoughts(conn: Any, now_ts: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Мысли, которым пора сработать."""
    try:
        rows = conn.execute(
            "SELECT id, due_ts, note FROM agent_thoughts "
            "WHERE status = 'pending' AND due_ts <= ? ORDER BY due_ts LIMIT ?",
            (str(now_ts)[:19], max(1, int(limit))),
        ).fetchall()
    except Exception:  # pragma: no cover
        return []
    return [dict(row) for row in rows]


def mark_thought(conn: Any, thought_id: int, status: str = "sent") -> None:
    conn.execute(
        "UPDATE agent_thoughts SET status = ? WHERE id = ?", (str(status)[:20], int(thought_id))
    )


def history(conn: Any, limit: int = HISTORY_LIMIT) -> List[Dict[str, str]]:
    """Короткая история диалога для контекста модели."""
    try:
        rows = conn.execute(
            "SELECT role, content FROM agent_messages ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    except Exception:  # pragma: no cover
        return []
    return [{"role": str(row[0]), "content": str(row[1])} for row in reversed(rows)]


def save_turn(conn: Any, user_text: str, reply_text: str, settings: Any) -> None:
    """Запоминает реплику человека и ответ агента, подрезая историю."""
    ts = _stamp(settings)
    conn.execute(
        "INSERT INTO agent_messages(ts, role, content) VALUES (?, 'user', ?)",
        (ts, str(user_text)[:4000]),
    )
    conn.execute(
        "INSERT INTO agent_messages(ts, role, content) VALUES (?, 'assistant', ?)",
        (ts, str(reply_text)[:4000]),
    )
    conn.execute(
        "DELETE FROM agent_messages WHERE id NOT IN "
        "(SELECT id FROM agent_messages ORDER BY id DESC LIMIT ?)",
        (HISTORY_KEEP,),
    )


def snapshot(conn: Any, settings: Any) -> str:
    """Короткий слепок дня для системного промпта."""
    parts: List[str] = []
    try:
        summary = store_v4.today_summary(conn, settings).strip()
        if summary:
            parts.append(summary)
    except Exception:  # pragma: no cover - слепок не должен ломать ответ
        logger.debug("слепок дня не собрался", exc_info=True)
    try:
        today = _today(settings)
        overdue = conn.execute(
            "SELECT count(*) FROM tasks WHERE status = 'todo' "
            "AND due_date IS NOT NULL AND due_date < ?",
            (today,),
        ).fetchone()[0]
        if overdue:
            parts.append("Просрочено дел: %d." % int(overdue))
    except Exception:  # pragma: no cover
        pass
    return ("\n".join(parts) or "Пустая база — записывать пока нечего.")[:900]


def build_agent(settings: Any = None) -> "Agent":
    """Собирает агента из настроек; без ключа он просто неактивен."""
    return Agent(settings)


# --------------------------------------------------------------------------
# Сам агент
# --------------------------------------------------------------------------


class Agent:
    """Цикл «думай → зови инструменты → ответь» поверх модели с tool calling."""

    def __init__(
        self,
        settings: Any = None,
        gateway: Optional[Any] = None,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.settings = settings
        self.gateway = gateway if gateway is not None else self._build_gateway(settings)
        self.max_steps = max(1, int(max_steps))
        try:
            self.timeout = min(max(float(getattr(settings, "llm_timeout", 25.0) or 25.0), 5.0), 120.0)
        except (TypeError, ValueError):
            self.timeout = 25.0
        effort = str(getattr(settings, "llm_parse_effort", "") or "")
        self.effort = effort if effort in {"minimal", "low", "medium", "high", "xhigh"} else "medium"

    @staticmethod
    def _build_gateway(settings: Any) -> Optional[Any]:
        if settings is None:
            return None
        try:
            from app.bot.intent import OpenAICompatibleGateway

            return OpenAICompatibleGateway(
                base_url=getattr(settings, "llm_base_url", "") or "https://api.samp.team/v1",
                api_key=getattr(settings, "llm_api_key", ""),
                model=getattr(settings, "llm_model", "") or "deepseek-v4.1-flash-1017",
                timeout=float(getattr(settings, "llm_timeout", 25.0) or 25.0),
                enabled=bool(getattr(settings, "llm_enabled", True)),
                reasoning_effort="",
            )
        except Exception:  # pragma: no cover - неполные настройки
            logger.warning("агент: шлюз не собрался", exc_info=True)
            return None

    def available(self) -> bool:
        """Есть ли смысл звать агента (иначе работает разбор правилами)."""
        gateway = self.gateway
        try:
            return bool(gateway is not None and gateway.available())
        except Exception:  # pragma: no cover
            return False

    # -- публичное ---------------------------------------------------------

    def respond(
        self,
        conn: Any,
        text: str,
        *,
        lock: Optional[Any] = None,
        save: bool = True,
    ) -> Optional[AgentReply]:
        """Разбирает реплику человека. ``None`` — модель недоступна."""
        clean = str(text or "").strip()
        if not self.available() or not clean:
            return None
        started = time.perf_counter()
        messages = self._messages(conn, clean)
        steps: List[Step] = []
        answer = ""
        for _step in range(self.max_steps):
            message = self.gateway.chat_tools(
                messages,
                tools=tool_schemas(),
                max_tokens=1200,
                reasoning_effort=self.effort,
                timeout=self.timeout,
            )
            if message is None:
                return None
            calls = message.get("tool_calls") or []
            if calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": [
                            {
                                "id": call.get("id") or "",
                                "type": "function",
                                "function": {
                                    "name": call.get("name") or "",
                                    "arguments": call.get("arguments") or "{}",
                                },
                            }
                            for call in calls
                        ],
                    }
                )
                for call in calls:
                    step = self._run_tool(conn, call, lock)
                    steps.append(step)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id") or "",
                            "name": step.tool,
                            "content": step.text,
                        }
                    )
                continue
            answer = str(message.get("content") or "").strip()
            break
        if not answer:
            # Модель дошла до лимита шагов: короткая сводка по фактам, не пустота.
            answer = _steps_summary(steps)
        answer = answer[:REPLY_LIMIT]
        if not answer:
            return None
        latency = int((time.perf_counter() - started) * 1000)
        if save:
            try:
                with _locked(lock):
                    save_turn(conn, clean, answer, self.settings)
            except Exception:  # pragma: no cover - история не критична
                logger.warning("агент: историю не сохранил", exc_info=True)
        return AgentReply(text=answer, steps=steps, latency_ms=latency)

    # -- внутреннее --------------------------------------------------------

    def _messages(self, conn: Any, user_text: str) -> List[Dict[str, Any]]:
        try:
            remembered = "\n".join(
                "- %s: %s" % (key, value) for key, value in memories(conn)
            ) or "Пока ничего."
        except Exception:  # pragma: no cover
            remembered = "Пока ничего."
        try:
            day = snapshot(conn, self.settings)
        except Exception:  # pragma: no cover
            day = "Пустая база."
        system = PERSONA.format(
            today=_today_human(self.settings),
            snapshot=day,
            memory=remembered,
        )
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        try:
            for row in history(conn):
                if row["role"] in {"user", "assistant"} and row["content"]:
                    messages.append(row)
        except Exception:  # pragma: no cover
            pass
        messages.append({"role": "user", "content": user_text})
        return messages

    def _run_tool(self, conn: Any, call: Mapping[str, Any], lock: Optional[Any]) -> Step:
        name = str(call.get("name") or "")
        raw_args = call.get("arguments")
        try:
            args = json.loads(raw_args or "{}")
        except (TypeError, ValueError):
            return Step(name, {}, "Не разобрал аргументы инструмента.", ok=False)
        if not isinstance(args, dict):
            return Step(name, {}, "Аргументы инструмента — не объект.", ok=False)
        tool = TOOLS.get(name)
        if tool is None:
            return Step(name, args, "Инструмента «%s» нет." % name, ok=False)
        try:
            with _locked(lock):
                result = tool.handler(_Ctx(conn, self.settings), args)
                journal_add(
                    conn,
                    name,
                    args,
                    result.text,
                    result.operations,
                    result.label,
                    self.settings,
                )
        except Exception as exc:  # инструмент не должен ронять ответ
            logger.exception("агент: инструмент %s упал", name)
            return Step(name, args, "Ошибка инструмента: %s" % type(exc).__name__, ok=False)
        return Step(name, args, str(result.text or ""), ok=True)


def _steps_summary(steps: Sequence[Step]) -> str:
    """Что ответить, если модель не сформулировала финал."""
    done = [step for step in steps if step.ok]
    if not done:
        return "Не разобрал сообщение. Скажи иначе — например «расход 400 такси»."
    lines = [step.text for step in done if step.text][:3]
    return "\n".join(lines)[:REPLY_LIMIT]
