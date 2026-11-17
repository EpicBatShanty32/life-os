"""Тесты app/bot/intent.py: правила, LLM-роутер и разбор ответа модели.

Сеть не используется: вместо шлюза — фейковый объект с ``available`` и
``json_chat``. Проверяются шесть типовых фраз из ТЗ, приоритеты метрик,
терпимость к мусору и fallback при любом сбое LLM.
"""

import json

import pytest

from app.bot import intent


class FakeGateway:
    """Фейковый LLM-шлюз: отдаёт заранее заданный ответ или падает."""

    def __init__(self, data=None, error=None, available=True):
        self.data = data
        self.error = error
        self._available = available
        self.calls = []

    def available(self):
        return self._available

    def json_chat(self, messages, max_tokens=700):
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return self.data


@pytest.mark.parametrize(
    "text, kind, metrics, category",
    [
        ("вес 81.2, жир 18.5", "weight", {"weight": 81.2, "fat": 18.5}, "Тело"),
        ("вода 300", "note", {"water": 300.0}, "Вода"),
        ("спал 7.5", "note", {"sleep": 7.5}, "Сон"),
        ("жим 80х5", "workout", {"load_kg": 80.0, "reps": 5.0}, "Тренировка"),
        ("съел 600 ккал и 40 г белка", "food", {"calories": 600.0, "protein": 40.0}, "Питание"),
        ("тренировка 45 минут", "workout", {"duration_min": 45.0}, "Тренировка"),
    ],
)
def test_scripted_fallback_six_phrases(text, kind, metrics, category):
    parsed = intent.ScriptedFallback().parse(text)
    assert parsed["kind"] == kind
    assert parsed["category"] == category
    for key, value in metrics.items():
        assert parsed["metrics"][key] == pytest.approx(value)
    assert parsed["text"] == text
    assert parsed["title"]
    assert parsed["engine"] == "rules"


def test_scripted_fallback_commas_and_liters():
    parsed = intent.ScriptedFallback().parse("вес 81,2 кг, выпил 1.5 л воды")
    assert parsed["metrics"]["weight"] == pytest.approx(81.2)
    assert parsed["metrics"]["water"] == pytest.approx(1500.0)


def test_scripted_fallback_lift_and_mood():
    lift = intent.ScriptedFallback().parse("присед 100 на 3")
    assert lift["kind"] == "workout"
    assert lift["metrics"]["load_kg"] == pytest.approx(100.0)
    assert lift["metrics"]["reps"] == pytest.approx(3.0)
    assert "присед" in lift["title"].lower()

    mood = intent.ScriptedFallback().parse("настроение 8")
    assert mood["metrics"]["mood"] == pytest.approx(8.0)
    assert mood["category"] == "Настроение"


def test_scripted_fallback_never_raises_on_empty_or_musor():
    for text in ("", "   ", "просто мысль без чисел", "!!! ???", "0", None):
        parsed = intent.ScriptedFallback().parse(text)
        assert parsed["kind"] == "note"
        assert isinstance(parsed["metrics"], dict)
        assert isinstance(parsed["title"], str)


def test_llm_router_uses_valid_llm_answer():
    gateway = FakeGateway(
        {
            "kind": "food",
            "title": "Обед",
            "text": "съел суп и котлету",
            "category": "Питание",
            "metrics": {"calories": 550},
        }
    )
    router = intent.LLMRouter(gateway=gateway)
    parsed = router.parse("съел суп и котлету")
    assert parsed["engine"] == "llm"
    assert parsed["kind"] == "food"
    assert parsed["metrics"]["calories"] == pytest.approx(550.0)
    assert gateway.calls and gateway.calls[0][0]["role"] == "system"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "не JSON",
        {"kind": "hologram", "metrics": {}},
        {"kind": "voice", "metrics": {}},
        {"title": "без kind"},
    ],
)
def test_llm_router_falls_back_on_bad_answer(payload):
    router = intent.LLMRouter(gateway=FakeGateway(payload))
    parsed = router.parse("вес 81.2")
    assert parsed["engine"] == "rules"
    assert parsed["metrics"]["weight"] == pytest.approx(81.2)


def test_llm_router_falls_back_when_gateway_raises():
    router = intent.LLMRouter(gateway=FakeGateway(error=RuntimeError("сеть")))
    parsed = router.parse("вода 300")
    assert parsed["engine"] == "rules"
    assert parsed["metrics"]["water"] == pytest.approx(300.0)


def test_llm_router_skips_unavailable_gateway():
    gateway = FakeGateway(data={"kind": "note"}, available=False)
    router = intent.LLMRouter(gateway=gateway)
    parsed = router.parse("спал 7.5")
    assert parsed["engine"] == "rules"
    assert gateway.calls == []


def test_gateway_without_key_is_offline():
    gateway = intent.OpenAICompatibleGateway(api_key="", base_url="http://127.0.0.1:1")
    assert gateway.available() is False
    assert gateway.json_chat([{"role": "user", "content": "привет"}]) is None
    assert "key" not in repr(gateway).lower()


def test_validate_intent_drops_out_of_range_metrics():
    parsed = intent.validate_intent(
        {
            "kind": "weight",
            "title": "Вес",
            "text": "вес",
            "metrics": {"weight": 1000, "fat": 18.5, "unknown": 3, "sleep": -1},
        },
        "вес",
    )
    assert parsed is not None
    assert parsed["metrics"] == {"fat": pytest.approx(18.5)}
    assert parsed["kind"] == "weight"


def test_validate_intent_rejects_media_kinds_in_text():
    assert intent.validate_intent({"kind": "video", "text": "x"}, "x") is None


def test_extract_json_object_handles_markdown_wrapper():
    text = 'Вот результат:\n```json\n{"kind": "note", "metrics": {}}\n```\nконец'
    parsed = intent.extract_json_object(text)
    assert parsed == {"kind": "note", "metrics": {}}
    assert intent.extract_json_object("без json") is None
    assert intent.extract_json_object(None) is None


def test_build_router_without_settings_and_parse_text_helper():
    router = intent.build_router(None)
    assert isinstance(router, intent.LLMRouter)
    parsed = intent.parse_text("вода 300", settings=None)
    assert parsed["metrics"]["water"] == pytest.approx(300.0)


def test_system_prompt_is_russian_and_strict_json():
    assert "JSON" in intent.SYSTEM_PROMPT
    assert "metrics" in intent.SYSTEM_PROMPT
    assert "Life OS" in intent.SYSTEM_PROMPT
    json.dumps({"prompt": intent.SYSTEM_PROMPT}, ensure_ascii=False)
