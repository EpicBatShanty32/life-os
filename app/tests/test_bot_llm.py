"""t39: LLM вторым слоем — свобода понимания, откат на правила, деньги и кэш.

Проверяем без сети: шлюз подменяется моком, поэтому тесты быстрые и
детерминированные. Настоящий api.samp.team проверяется отдельным прогоном
(studio/BOT-LLM-REPORT.md), в pytest он не ходит.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from app.bot import handlers, intent, intent_v4
from app.server import db
from app.server.settings import ConfigError, load_settings

CHAT_ID = 777_001
TODAY = date(2026, 11, 18)


# --------------------------------------------------------------------------
# Моки шлюза
# --------------------------------------------------------------------------


class PhraseGateway:
    """Отвечает по фразе человека; запоминает аргументы вызова."""

    def __init__(
        self,
        answers: Mapping[str, Mapping[str, Any]],
        *,
        error: Optional[BaseException] = None,
    ) -> None:
        self.answers = dict(answers)
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def available(self) -> bool:
        return True

    def json_chat(self, messages, max_tokens=700, reasoning_effort=None, timeout=None):
        user = ""
        for message in reversed(list(messages or [])):
            if isinstance(message, Mapping) and message.get("role") == "user":
                user = str(message.get("content") or "")
                break
        self.calls.append(
            {
                "messages": list(messages or []),
                "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort,
                "timeout": timeout,
                "user": user,
            }
        )
        if self.error is not None:
            raise self.error
        for phrase, answer in self.answers.items():
            if phrase in user:
                return dict(answer)
        return dict(next(iter(self.answers.values()))) if self.answers else {}


class ScriptedGateway:
    """Отдаёт ответы по очереди: сначала разбор, потом ответ о данных."""

    def __init__(self, replies: List[Mapping[str, Any]]) -> None:
        self.replies = [dict(item) for item in replies]
        self.calls: List[Dict[str, Any]] = []

    def available(self) -> bool:
        return True

    def json_chat(self, messages, max_tokens=700, reasoning_effort=None, timeout=None):
        system = str((messages or [{}])[0].get("content") or "")
        self.calls.append(
            {
                "system": system,
                "reasoning_effort": reasoning_effort,
                "timeout": timeout,
                "messages": list(messages or []),
            }
        )
        if not self.replies:
            return {}
        return self.replies.pop(0)


def make_env(tmp_path: Path):
    """Настройки и БД без ключа LLM (ключ подменяем моком в роутере)."""
    settings = load_settings(
        env={
            "LIFEOS_ADMIN_TELEGRAM_ID": str(CHAT_ID),
            "LIFEOS_DB_PATH": str(tmp_path / "lifeos.db"),
            "LIFEOS_LLM_ENABLED": "0",
            "LIFEOS_LLM_API_KEY": "",
        },
        root=tmp_path,
    )
    settings.ensure_dirs()
    conn = db.init_db(settings.db_path)
    return settings, conn


def service_with(settings: Any, conn: Any, gateway: Any) -> handlers.BotService:
    router = intent_v4.V4Router(gateway=gateway)
    return handlers.BotService(settings, conn, v4_router=router)


def usage_rows(conn: Any) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT screen, action, meta_json FROM usage_events ORDER BY id"
    ).fetchall()
    return [
        {
            "screen": row["screen"],
            "action": row["action"],
            "meta": json.loads(row["meta_json"] or "{}"),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------
# 1. Правила первыми: явные фразы модель не трогают
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "такси 450",
        "на обед гречка 200 г и курица 150",
        "выпил 2 л",
        "завтра позвонить в банк",
        "вес 81,2",
    ],
)
def test_confident_rules_do_not_call_model(phrase: str) -> None:
    gateway = PhraseGateway({"такси": {"intent": "expense", "amount": 450}})
    router = intent_v4.V4Router(gateway=gateway)
    parsed = router.parse(phrase, today=TODAY)
    assert parsed["engine"] == "rules", parsed
    assert gateway.calls == [], "на явной фразе модель вызываться не должна"


# --------------------------------------------------------------------------
# 2. Двадцать свободных фраз: правила пасуют, модель понимает
# --------------------------------------------------------------------------


FREE_PHRASES: List[tuple] = [
    ("поздно не сплю", "checkin", {"energy": 2, "mood": "усталость"}),
    ("дела life off", "journal", {"topics": ["работа"]}),
    ("я сегодня как выжатый лимон после планёрки", "checkin", {"energy": 1, "note": "выжат"}),
    ("вчера лёг в час ночи", "journal", {"mood": "усталость"}),
    ("созвоны доконали, сил ноль", "checkin", {"energy": 1, "note": "созвоны"}),
    ("хочу в отпуск", "journal", {"topics": ["отдых"]}),
    ("помирился с Леной", "journal", {"topics": ["семья"]}),
    ("много ходил сегодня", "journal", {}),
    ("сегодня без сладкого", "habit", {"habit": "Без сладкого"}),
    ("сходил в зал, тягал ноги", "journal", {}),
    ("кофе с коллегами за счёт компании", "journal", {}),
    (
        "надо бы разобраться с налогами до конца месяца",
        "task",
        {"title": "Разобраться с налогами", "due_date": "2026-11-30"},
    ),
    (
        "записался к стоматологу на четверг в 15:30",
        "task",
        {"title": "Стоматолог", "due_date": "2026-11-19", "start_time": "15:30"},
    ),
    ("обед был плотный, взял добавку", "journal", {}),
    ("утром бегал вдоль реки", "journal", {}),
    ("вечером посмотрю сериал", "journal", {}),
    ("чувствую тревогу перед встречей", "journal", {"mood": "тревога"}),
    ("надо позвонить маме", "task", {"title": "Позвонить маме"}),
    ("жизнь налаживается", "journal", {}),
    ("планёрка была бесполезной", "journal", {}),
]


def test_twenty_free_phrases_are_understood_by_model() -> None:
    assert len(FREE_PHRASES) == 20
    for phrase, expected, fields in FREE_PHRASES:
        rules_parsed = intent_v4.RulesV4(TODAY).parse(phrase)
        confidence = intent_v4.rules_confidence(rules_parsed, phrase, TODAY)
        assert confidence < intent_v4.RULES_CONFIDENCE_MIN, (
            "%r: правила уверены (%.2f) — это не свободная фраза" % (phrase, confidence)
        )
        payload = {"intent": expected, "confidence": 0.9, **fields}
        gateway = PhraseGateway({phrase: payload})
        router = intent_v4.V4Router(gateway=gateway)
        parsed = router.parse(phrase, today=TODAY)
        assert parsed["intent"] == expected, (phrase, parsed)
        assert parsed.get("engine") == "llm", (phrase, parsed)
        assert parsed.get("text") == phrase, "исходный текст должен сохраниться"
        assert gateway.calls, phrase
        for key, value in fields.items():
            assert parsed.get(key) == value, (phrase, key, parsed)


def test_parse_uses_medium_effort_and_eight_second_timeout() -> None:
    phrase = "поздно не сплю"
    gateway = PhraseGateway({phrase: {"intent": "journal", "confidence": 0.9}})
    router = intent_v4.V4Router(gateway=gateway, parse_effort="medium", answer_effort="xhigh")
    router.parse(phrase, today=TODAY)
    call = gateway.calls[0]
    assert call["reasoning_effort"] == "medium"
    assert call["timeout"] == intent_v4.PARSE_TIMEOUT_S == 8.0
    system = str(call["messages"][0]["content"])
    for action in (
        "task",
        "expense",
        "food",
        "habit",
        "checkin",
        "practice",
        "legacy",
        "book_quote",
        "query",
    ):
        assert action in system, "в подсказке нет действия %s" % action


# --------------------------------------------------------------------------
# 3. Низкая уверенность — один вопрос с кнопками, не Инбокс
# --------------------------------------------------------------------------


def test_low_confidence_asks_one_question_with_buttons(tmp_path: Path) -> None:
    settings, conn = make_env(tmp_path)
    phrase = "помирился с Леной"
    gateway = PhraseGateway(
        {
            phrase: {
                "intent": "clarify",
                "confidence": 0.3,
                "question": "Это мысль или встреча?",
                "options": [
                    {"label": "Это мысль", "intent": "journal", "text": phrase},
                    {"label": "Это встреча", "intent": "task", "title": "Встреча с Леной"},
                ],
            }
        }
    )
    service = service_with(settings, conn, gateway)
    reply = service.handle_text(phrase, CHAT_ID)

    assert reply.text == "Это мысль или встреча?"
    callbacks = [data for row in reply.buttons for _label, data in row]
    assert callbacks == ["v4:pick:0", "v4:pick:1"]
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0, "не Инбокс"

    picked = service.handle_callback("v4:pick:0", CHAT_ID)
    assert "запис" in picked.text.lower() or "мысл" in picked.text.lower()
    entry = conn.execute("SELECT * FROM entries ORDER BY id DESC LIMIT 1").fetchone()
    assert entry is not None
    assert phrase in str(entry["text"])


def test_model_top_level_options_become_buttons(tmp_path: Path) -> None:
    """Варианты модели на верхнем уровне (как в подсказке) — это кнопки."""
    settings, conn = make_env(tmp_path)
    phrase = "поздно не сплю"
    gateway = PhraseGateway(
        {
            phrase: {
                "intent": "clarify",
                "confidence": 0.35,
                "question": "Это привычка или мысль?",
                "options": [
                    {"label": "Привычка", "intent": "habit", "habit": "Не спать поздно"},
                    {"label": "Это мысль", "intent": "journal", "text": phrase},
                ],
            }
        }
    )
    service = service_with(settings, conn, gateway)
    reply = service.handle_text(phrase, CHAT_ID)
    labels = [label for row in reply.buttons for label, _data in row]
    assert labels == ["Привычка", "Это мысль"], reply.text


def test_low_confidence_without_ask_still_asks_not_inbox() -> None:
    phrase = "жизнь налаживается"
    gateway = PhraseGateway(
        {phrase: {"intent": "inbox", "confidence": 0.2}}
    )
    router = intent_v4.V4Router(gateway=gateway)
    parsed = router.parse(phrase, today=TODAY)
    assert parsed["intent"] == "clarify", parsed
    assert parsed.get("question")
    assert len(parsed.get("options") or []) >= 2


# --------------------------------------------------------------------------
# 4. Отказы модели: тихий откат на правила без технотекста
# --------------------------------------------------------------------------


def test_model_error_falls_back_to_rules(tmp_path: Path) -> None:
    settings, conn = make_env(tmp_path)
    gateway = PhraseGateway({}, error=RuntimeError("network down"))
    service = service_with(settings, conn, gateway)
    reply = service.handle_text("поздно не сплю", CHAT_ID)

    assert gateway.calls, "модель должна была попробовать"
    lowered = reply.text.lower()
    for tech in ("llm", "json", "traceback", "timeout", "таймаут", "ошибка сети"):
        assert tech not in lowered, "технотекст в ответе: %r" % reply.text
    entry = conn.execute("SELECT text FROM entries ORDER BY id DESC LIMIT 1").fetchone()
    assert entry is not None and "не сплю" in str(entry["text"])


def test_model_timeout_on_doubtful_task_asks_not_records(tmp_path: Path) -> None:
    """Модель не ответила, а правила сделали «дело» из фразы-состояния.

    Записывать «Я как выжатый лимон после планёрки» как задачу нельзя:
    лучше один вопрос с кнопками (не Инбокс).
    """
    settings, conn = make_env(tmp_path)
    gateway = PhraseGateway({}, error=TimeoutError("slow"))
    service = service_with(settings, conn, gateway)
    phrase = "я сегодня как выжатый лимон после планёрки"
    reply = service.handle_text(phrase, CHAT_ID)
    assert reply.text == "Не уверен, что это. Как сохранить?"
    assert [data for row in reply.buttons for _label, data in row] == ["v4:pick:0", "v4:pick:1"]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_invalid_model_fields_on_doubtful_task_asks(tmp_path: Path) -> None:
    """Модель вернула checkin без энергии — не пишем сомнительное дело."""
    settings, conn = make_env(tmp_path)
    phrase = "я сегодня как выжатый лимон после планёрки"
    gateway = PhraseGateway(
        {phrase: {"intent": "checkin", "confidence": 0.65, "note": "выжат"}}
    )
    service = service_with(settings, conn, gateway)
    reply = service.handle_text(phrase, CHAT_ID)
    assert reply.text == "Не уверен, что это. Как сохранить?"
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_silent_gateway_timeout_asks_not_records(tmp_path: Path) -> None:
    """Шлюз вернул None с last_error=TimeoutError — как таймаут, не как молчание."""

    class SilentTimeout:
        last_error = "TimeoutError"

        def __init__(self) -> None:
            self.calls = 0

        def available(self) -> bool:
            return True

        def json_chat(self, messages, **kwargs):
            self.calls += 1
            return None

    settings, conn = make_env(tmp_path)
    gateway = SilentTimeout()
    service = service_with(settings, conn, gateway)
    phrase = "я сегодня как выжатый лимон после планёрки"
    reply = service.handle_text(phrase, CHAT_ID)
    assert reply.text == "Не уверен, что это. Как сохранить?"
    assert gateway.calls == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_unavailable_gateway_keeps_rules() -> None:
    phrase = "поздно не сплю"
    gateway = PhraseGateway({phrase: {"intent": "checkin", "energy": 2}})
    gateway.available = lambda: False  # type: ignore[assignment]
    router = intent_v4.V4Router(gateway=gateway)
    parsed = router.parse(phrase, today=TODAY)
    assert parsed["engine"] == "rules"
    assert parsed["intent"] == "journal"
    assert gateway.calls == []


def test_garbage_model_answer_falls_back_to_rules() -> None:
    phrase = "жизнь налаживается"
    gateway = PhraseGateway({phrase: {"foo": "bar"}})
    router = intent_v4.V4Router(gateway=gateway)
    parsed = router.parse(phrase, today=TODAY)
    assert parsed["engine"] == "rules"
    assert parsed["intent"] == "inbox"


# --------------------------------------------------------------------------
# 5. Кэш одинаковых фраз и один вызов на сообщение
# --------------------------------------------------------------------------


def test_identical_phrases_cached_for_ten_minutes() -> None:
    phrase = "поздно не сплю"
    gateway = PhraseGateway({phrase: {"intent": "journal", "confidence": 0.9}})
    router = intent_v4.V4Router(gateway=gateway)
    first = router.parse(phrase, today=TODAY)
    second = router.parse(phrase, today=TODAY)
    assert first == second
    assert len(gateway.calls) == 1, "одинаковая фраза не должна звать модель дважды"


def test_cache_expires() -> None:
    phrase = "поздно не сплю"
    gateway = PhraseGateway({phrase: {"intent": "journal", "confidence": 0.9}})
    router = intent_v4.V4Router(gateway=gateway, cache_ttl=0.01)
    router.parse(phrase, today=TODAY)
    time.sleep(0.02)
    router.parse(phrase, today=TODAY)
    assert len(gateway.calls) == 2


# --------------------------------------------------------------------------
# 6. Ответ о данных: модель формулирует, но не считает
# --------------------------------------------------------------------------


def test_answer_writer_rephrases_from_aggregates() -> None:
    gateway = PhraseGateway(
        {"": {"answer": "За неделю ты потратил 400 ₽."}}
    )
    writer = intent_v4.AnswerWriter(gateway, effort="xhigh")
    result = writer.rewrite("Расходы за неделю: 400 ₽.", "Дней с тратами: 2.", topic="money")
    assert result is not None
    assert "400" in result["text"]
    call = gateway.calls[0]
    assert call["reasoning_effort"] == "xhigh"
    assert call["timeout"] == intent_v4.ANSWER_TIMEOUT_S == 25.0


def test_answer_writer_rejects_invented_numbers() -> None:
    gateway = PhraseGateway(
        {"": {"answer": "Ты потратил 999 ₽ за 3 дня."}}
    )
    writer = intent_v4.AnswerWriter(gateway)
    result = writer.rewrite("Расходы за неделю: 400 ₽.", "Дней с тратами: 2.")
    assert result is None
    assert writer.last_error == "invented_number"


def test_numbers_from_source_normalizes_groups_and_commas() -> None:
    assert intent_v4.numbers_from_source("Потратил 8 900 ₽, это 2,5% дохода", "8 900 ₽; 2,5 %")
    assert not intent_v4.numbers_from_source("Потратил 8 901 ₽", "8 900 ₽")


def test_model_answer_only_when_parse_was_rules(tmp_path: Path) -> None:
    settings, conn = make_env(tmp_path)
    gateway = ScriptedGateway(
        [
            {
                "intent": "query",
                "query_kind": "spent",
                "topic": "money",
                "period": "week",
                "confidence": 0.9,
            }
        ]
    )
    service = service_with(settings, conn, gateway)
    reply = service.handle_text("сколько ушло на еду", CHAT_ID)
    assert reply.text.strip()
    assert len(gateway.calls) == 1, "после разбора моделью второй вызов запрещён"


def test_answer_writer_used_for_rules_query(tmp_path: Path) -> None:
    settings, conn = make_env(tmp_path)
    gateway = ScriptedGateway([{"answer": "За неделю расходов нет."}])
    service = service_with(settings, conn, gateway)
    reply = service.handle_text("сколько потратил за неделю", CHAT_ID)

    assert reply.text == "За неделю расходов нет."
    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    assert "ДАННЫЕ" in str(call["messages"][0]["content"] + call["messages"][1]["content"])
    assert call["reasoning_effort"] == "xhigh"
    assert call["timeout"] == 25.0
    answer_events = [row for row in usage_rows(conn) if row["action"] == "answer"]
    assert answer_events and answer_events[-1]["meta"]["engine"] == "llm"
    assert isinstance(answer_events[-1]["meta"]["latency_ms"], int)


# --------------------------------------------------------------------------
# 7. usage_events: engine=llm только там, где правила не справились
# --------------------------------------------------------------------------


def test_usage_engine_marks_only_low_confidence(tmp_path: Path) -> None:
    settings, conn = make_env(tmp_path)
    gateway = PhraseGateway(
        {"поздно не сплю": {"intent": "journal", "confidence": 0.9}}
    )
    service = service_with(settings, conn, gateway)
    service.handle_text("такси 450", CHAT_ID)
    service.handle_text("поздно не сплю", CHAT_ID)

    events = {row["meta"].get("memo", ""): row for row in usage_rows(conn) if row["action"] == "message"}
    assert events["такси 450"]["meta"]["engine"] == "rules"
    assert events["поздно не сплю"]["meta"]["engine"] == "llm"
    assert isinstance(events["такси 450"]["meta"]["latency_ms"], int)
    assert isinstance(events["поздно не сплю"]["meta"]["latency_ms"], int)


# --------------------------------------------------------------------------
# 8. Шлюз: reasoning в payload, повтор без поля на 400, ключ не в логах
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


def test_gateway_sends_reasoning_and_retries_without_on_400(monkeypatch, caplog) -> None:
    calls: List[tuple] = []
    ok = json.dumps({"choices": [{"message": {"content": "{\"answer\": \"ok\"}"}}]}).encode()

    def fake_urlopen(request, timeout=None):
        payload = json.loads(request.data.decode("utf-8"))
        calls.append((payload, timeout))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"reasoning"}')
            )
        return _FakeResponse(ok)

    monkeypatch.setattr(intent.urllib.request, "urlopen", fake_urlopen)
    gateway = intent.OpenAICompatibleGateway(api_key="sk-secret-test", reasoning_effort="xhigh")
    data = gateway.json_chat([{"role": "user", "content": "привет"}], timeout=8.0)

    assert data == {"answer": "ok"}
    assert calls[0][0]["reasoning"] == {"effort": "xhigh"}, calls[0][0]
    assert "reasoning" not in calls[1][0], "повтор должен быть без поля reasoning"
    assert calls[0][1] == 8.0
    assert gateway.reasoning_supported is False
    assert "sk-secret-test" not in caplog.text


def test_effort_xhigh_reaches_payload_and_key_never_in_repr() -> None:
    gateway = intent.OpenAICompatibleGateway(api_key="sk-secret-test", reasoning_effort="xhigh")
    assert gateway.reasoning_effort == "xhigh"
    assert "sk-secret-test" not in repr(gateway)
    assert intent.OpenAICompatibleGateway(api_key="k", reasoning_effort="huge").reasoning_effort == ""


# --------------------------------------------------------------------------
# 9. Настройки: три уровня усилия, env, валидация
# --------------------------------------------------------------------------


def test_settings_expose_efforts(tmp_path: Path) -> None:
    settings = load_settings(env={}, root=tmp_path)
    assert settings.llm_reasoning_effort == "xhigh"
    assert settings.llm_parse_effort == "medium"
    assert settings.llm_answer_effort == "xhigh"
    assert settings.safe_dict()["llm"]["parse_effort"] == "medium"
    assert settings.llm_parse_timeout == 8.0
    assert settings.llm_answer_timeout == 25.0


def test_settings_efforts_env_overrides(tmp_path: Path) -> None:
    settings = load_settings(
        env={
            "LIFEOS_LLM_PARSE_EFFORT": "low",
            "LIFEOS_LLM_ANSWER_EFFORT": "high",
            "LIFEOS_LLM_REASONING_EFFORT": "medium",
        },
        root=tmp_path,
    )
    assert (settings.llm_parse_effort, settings.llm_answer_effort, settings.llm_reasoning_effort) == (
        "low",
        "high",
        "medium",
    )


def test_settings_timeout_env_overrides(tmp_path: Path) -> None:
    settings = load_settings(
        env={
            "LIFEOS_LLM_PARSE_TIMEOUT": "12",
            "LIFEOS_LLM_ANSWER_TIMEOUT": "40",
        },
        root=tmp_path,
    )
    assert settings.llm_parse_timeout == 12.0
    assert settings.llm_answer_timeout == 40.0
    router = intent_v4.build_router(settings)
    assert router.parse_timeout == 12.0
    assert router.answer_timeout == 40.0


def test_settings_reject_unknown_effort(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(env={"LIFEOS_LLM_PARSE_EFFORT": "turbo"}, root=tmp_path)


def test_build_router_takes_efforts_from_settings(tmp_path: Path) -> None:
    settings = load_settings(
        env={
            "LIFEOS_LLM_ENABLED": "0",
            "LIFEOS_LLM_API_KEY": "",
            "LIFEOS_LLM_PARSE_EFFORT": "high",
            "LIFEOS_LLM_ANSWER_EFFORT": "medium",
        },
        root=tmp_path,
    )
    router = intent_v4.build_router(settings)
    assert router.parse_effort == "high"
    assert router.answer_effort == "medium"
