"""Еда по граммам в боте: справочник, КБЖУ, дневник и остаток нормы (t25/t32).

Проверяется то, что владелец просил словами: «пусть считает всё, я пишу
граммы — пусть ищет и считает в еде».

* фраза «на обед гречка 200 г и курица 150» → две строки ``food_log`` с
  ``product_id``, граммами и КБЖУ ``справочник × граммы / base_grams``;
* «гречка 200 и курица 150» (без «г») — тоже еда, а не мысль;
* продукта нет в справочнике → оценка LLM на 100 г (xhigh), пометка
  «примерно» и кнопки «Добавить в справочник» / «Только калории»;
* ерунда не пишется: КБЖУ вне пределов, граммы вне 1–3000, абракадабра;
* модель недоступна → работают правила, граммы всё равно сохраняются;
* модель назвала еду мыслью → еда важнее (страховка в обработчике);
* запись сразу видна через API ``/api/nutrition/day`` (общая база);
* «Удалить» убирает всю фразу целиком, если продуктов было два.

Сеть не используется: LLM подменяется фейковым шлюзом, база — временная.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping

import pytest
from fastapi.testclient import TestClient

from app.bot import handlers, intent_v4, media, nutrition_v4
from app.server import db
from app.server.main import create_app
from app.server.settings import load_settings

CHAT_ID = 4242

#: Мини-справочник с заранее известными цифрами (как реальные продукты сида).
CATALOG = (
    ("Гречка ядрица", 343.0, 12.6, 3.3, 57.1, 10.0, 0.9),
    ("Куриная грудка", 113.0, 23.6, 1.9, 0.4, 0.0, 0.0),
    ("Банан", 96.0, 1.5, 0.3, 22.0, 2.6, 12.0),
    ("Творог 5%", 121.0, 17.0, 5.0, 1.8, 0.0, 1.8),
    ("Овсянка на воде", 88.0, 3.0, 1.5, 15.0, 1.8, 0.4),
    ("Треска филе", 82.0, 17.8, 0.7, 0.0, 0.0, 0.0),
    ("Яйцо куриное", 143.0, 12.6, 9.5, 0.7, 0.0, 0.7),
    ("Кофе чёрный", 2.0, 0.2, 0.0, 0.0, 0.0, 0.0),
)


# --------------------------------------------------------------------------
# Окружение: временная база, бот и API на одной базе
# --------------------------------------------------------------------------


def _seed_catalog(conn: Any) -> Dict[str, int]:
    """Кладёт мини-справочник и возвращает id по названию."""
    ids: Dict[str, int] = {}
    for name, kcal, protein, fat, carbs, fiber, sugar in CATALOG:
        cursor = conn.execute(
            "INSERT INTO products (name, brand, kcal, protein, fat, carbs, fiber, "
            "sugar, base_grams) VALUES (?, '', ?, ?, ?, ?, ?, ?, 100)",
            (name, kcal, protein, fat, carbs, fiber, sugar),
        )
        ids[name] = int(cursor.lastrowid or 0)
    conn.commit()
    return ids


def _make_env(tmp_path: Path, gateway: Any = None) -> SimpleNamespace:
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
    ids = _seed_catalog(conn)
    router = intent_v4.V4Router(gateway=gateway)
    service = handlers.BotService(settings, conn, v4_router=router)
    app = create_app(settings, start_worker=False, api_token="")
    return SimpleNamespace(
        settings=settings,
        conn=conn,
        service=service,
        ids=ids,
        app=app,
        tmp=tmp_path,
    )


@pytest.fixture()
def env(tmp_path: Path):
    """Бот без LLM: только правила и справочник."""
    space = _make_env(tmp_path, gateway=None)
    with TestClient(space.app) as client:
        space.client = client
        yield space
    space.conn.close()


def send(env: Any, text: str) -> handlers.Reply:
    return env.service.handle_text(text, CHAT_ID)


def food_rows(env: Any) -> List[Any]:
    return env.conn.execute(
        "SELECT id, date, meal, product_id, text, grams, kcal, protein, fat, carbs, source "
        "FROM food_log ORDER BY id"
    ).fetchall()


def row_dict(row: Any) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class FakeEstimateGateway:
    """Шлюз только для оценки КБЖУ: разбор отдаём правилам, оценку — таблицей."""

    def __init__(self, answers: Mapping[str, Any], *, fail: bool = False) -> None:
        self.answers = dict(answers)
        self.fail = fail
        self.calls: List[str] = []
        self.efforts: List[Any] = []

    def available(self) -> bool:
        return True

    def json_chat(self, messages, max_tokens=700, reasoning_effort=None):
        user = str((messages or [{}])[-1].get("content") or "")
        self.calls.append(user)
        self.efforts.append(reasoning_effort)
        if self.fail:
            raise RuntimeError("network down")
        return self.answers.get(user)


class FakeParseGateway:
    """Шлюз разбора фразы: отдаёт заданный JSON (для страховки «еда vs мысль»)."""

    def __init__(self, data: Any) -> None:
        self.data = data
        self.calls: List[Any] = []

    def available(self) -> bool:
        return True

    def json_chat(self, messages, max_tokens=700, reasoning_effort=None):
        self.calls.append(messages)
        return self.data


# --------------------------------------------------------------------------
# 1. Живые фразы корпуса: продукт + граммы — это еда
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "на обед гречка 200 г и курица 150",
        "гречка 200 и курица 150",
        "гречка 200 г",
        "курица 150",
        "банан 100 г",
        "творог 200 г",
        "завтрак овсянка 250 и кофе",
        "ужин: рыба 150 и овощи",
        "съел 600 ккал и 40 белка",
        "перекус 200 ккал",
    ],
)
def test_corpus_food_phrases_are_food(phrase: str) -> None:
    """Фразы корпуса (группа food) разбираются как еда, а не журнал."""
    parsed = intent_v4.RulesV4().parse(phrase)
    assert parsed["intent"] == "legacy", parsed
    assert parsed["legacy"]["kind"] == "food", parsed["legacy"]


def test_food_group_of_corpus_keeps_meaning() -> None:
    """Все фразы группы food, которые должны логировать еду, идут в еду."""
    from app.tests.bot_scenarios import corpus

    bad = []
    for phrase in corpus.PHRASES:
        if phrase["group"] != "food":
            continue
        if phrase["expect"].get("action") != "log_food":
            continue
        parsed = intent_v4.RulesV4().parse(phrase["phrase"])
        legacy = parsed.get("legacy") or {}
        if parsed.get("intent") == "legacy" and legacy.get("kind") == "food":
            continue
        if parsed.get("intent") == "food":
            continue
        bad.append((phrase["id"], phrase["phrase"], parsed.get("intent")))
    assert not bad, "еда ушла не туда: %s" % bad


# --------------------------------------------------------------------------
# 2. Граммы, продукт и КБЖУ в базе
# --------------------------------------------------------------------------


def test_two_products_become_two_rows_with_macros(env) -> None:
    reply = send(env, "на обед гречка 200 г и курица 150")
    rows = [row_dict(row) for row in food_rows(env)]
    assert len(rows) == 2, reply.text
    buckwheat, chicken = rows
    assert buckwheat["grams"] == 200
    assert buckwheat["product_id"] == env.ids["Гречка ядрица"]
    assert buckwheat["kcal"] == pytest.approx(686.0)
    assert buckwheat["protein"] == pytest.approx(25.2)
    assert buckwheat["fat"] == pytest.approx(6.6)
    assert buckwheat["carbs"] == pytest.approx(114.2)
    assert buckwheat["meal"] == "lunch"
    assert chicken["grams"] == 150
    assert chicken["product_id"] == env.ids["Куриная грудка"]
    assert chicken["kcal"] == pytest.approx(169.5)
    assert chicken["protein"] == pytest.approx(35.4)
    assert chicken["source"] == "telegram"


def test_reply_has_item_macros_day_total_and_remaining(env) -> None:
    reply = send(env, "на обед гречка 200 г и курица 150")
    lines = [line for line in reply.text.splitlines() if line.strip()]
    assert 3 <= len(lines) <= 4, reply.text
    assert "Гречка ядрица 200 г" in lines[0]
    assert "686 ккал" in lines[0]
    assert "855" in lines[1] or "856" in lines[1], lines[1]  # 686 + 169.5
    assert "2300" in lines[1], "в итоге дня должна быть норма"
    assert lines[2].startswith("Осталось:"), lines[2]


def test_grams_without_unit_are_food(env) -> None:
    """«курица 150» — еда, даже если слово «съел» не написано."""
    reply = send(env, "курица 150")
    rows = [row_dict(row) for row in food_rows(env)]
    assert len(rows) == 1, reply.text
    assert rows[0]["grams"] == 150
    assert rows[0]["product_id"] == env.ids["Куриная грудка"]


def test_kilograms_are_converted_to_grams(env) -> None:
    send(env, "курица 0,5 кг")
    row = row_dict(food_rows(env)[-1])
    assert row["grams"] == 500
    assert row["kcal"] == pytest.approx(565.0)


def test_targets_from_database_are_used_in_reply(env) -> None:
    with env.conn:
        env.conn.execute("UPDATE nutrition_targets SET kcal = 2000, protein = 120 WHERE id = 1")
    reply = send(env, "гречка 200 г")
    assert "2000" in reply.text, reply.text
    assert "120" in reply.text, reply.text


def test_food_is_visible_through_api(env) -> None:
    send(env, "на обед гречка 200 г и курица 150")
    day = env.client.get("/api/nutrition/day", params={"date": env.settings.today()}).json()
    titles = [
        str(entry.get("title") or entry.get("text") or "")
        for meal in day["meals"]
        for entry in meal["entries"]
    ]
    assert any("Гречка" in title for title in titles), titles
    assert any("Куриная" in title for title in titles), titles
    assert day["totals"]["kcal"] >= 855


def test_meal_from_word_and_from_clock(env) -> None:
    send(env, "на завтрак овсянка 200 г")
    assert row_dict(food_rows(env)[-1])["meal"] == "breakfast"
    send(env, "перекус банан 100 г")
    assert row_dict(food_rows(env)[-1])["meal"] == "snack"


def test_kcal_only_phrase_writes_one_row(env) -> None:
    reply = send(env, "съел 600 ккал и 40 белка")
    rows = [row_dict(row) for row in food_rows(env)]
    assert len(rows) == 1, reply.text
    assert rows[0]["kcal"] == 600
    assert rows[0]["protein"] == 40
    assert rows[0]["grams"] == 0
    assert rows[0]["product_id"] is None


def test_item_without_grams_is_asked_not_invented(env) -> None:
    reply = send(env, "завтрак овсянка 250 и кофе")
    rows = [row_dict(row) for row in food_rows(env)]
    assert len(rows) == 1, "овсянка записана, кофе — нет"
    assert rows[0]["grams"] == 250
    assert "кофе" in reply.text.lower()
    assert "граммов" in reply.text.lower() or "граммы" in reply.text.lower()


# --------------------------------------------------------------------------
# 3. Оценка LLM: пометка «примерно» и кнопки
# --------------------------------------------------------------------------


def test_unknown_product_is_estimated_and_marked(tmp_path: Path) -> None:
    gateway = FakeEstimateGateway(
        {"хумус": {"kcal": 166, "protein": 7.9, "fat": 9.6, "carbs": 14.3}}
    )
    space = _make_env(tmp_path, gateway=None)
    space.service.food_gateway = gateway
    with TestClient(space.app) as client:
        space.client = client
        reply = space.service.handle_text("на ужин хумус 80 г", CHAT_ID)
    rows = [row_dict(row) for row in food_rows(space)]
    assert len(rows) == 1, reply.text
    row = rows[0]
    assert row["product_id"] is None
    assert row["grams"] == 80
    assert row["kcal"] == pytest.approx(132.8)
    assert row["source"] == "telegram:llm"
    assert "примерно" in reply.text.lower(), reply.text
    assert gateway.calls == ["хумус"]
    assert gateway.efforts == ["xhigh"], "оценка КБЖУ — «думающий» вызов"
    labels = [label for row_buttons in reply.buttons for label, _data in row_buttons]
    assert "Добавить в справочник" in labels
    assert "Только калории" in labels
    space.conn.close()


def test_estimate_can_be_saved_to_catalog(tmp_path: Path) -> None:
    gateway = FakeEstimateGateway(
        {"хумус": {"kcal": 166, "protein": 7.9, "fat": 9.6, "carbs": 14.3}}
    )
    space = _make_env(tmp_path, gateway=None)
    space.service.food_gateway = gateway
    with TestClient(space.app) as client:
        space.client = client
        space.service.handle_text("хумус 80 г", CHAT_ID)
        reply = space.service.handle_callback("v4:food:save", CHAT_ID, CHAT_ID)
    assert "справочник" in reply.text.lower(), reply.text
    product = space.conn.execute(
        "SELECT id, name, kcal, protein FROM products WHERE name = 'хумус'"
    ).fetchone()
    assert product is not None, "продукт не добавлен в справочник"
    assert float(product["kcal"]) == pytest.approx(166.0)
    row = row_dict(food_rows(space)[-1])
    assert row["product_id"] == int(product["id"])
    assert row["source"] == "telegram:catalog"
    space.conn.close()


def test_estimate_can_be_kept_as_kcal_only(tmp_path: Path) -> None:
    gateway = FakeEstimateGateway(
        {"хумус": {"kcal": 166, "protein": 7.9, "fat": 9.6, "carbs": 14.3}}
    )
    space = _make_env(tmp_path, gateway=None)
    space.service.food_gateway = gateway
    with TestClient(space.app) as client:
        space.client = client
        space.service.handle_text("хумус 80 г", CHAT_ID)
        reply = space.service.handle_callback("v4:food:kcal", CHAT_ID, CHAT_ID)
    assert "калории" in reply.text.lower(), reply.text
    row = row_dict(food_rows(space)[-1])
    assert row["kcal"] > 0
    assert row["protein"] == 0 and row["fat"] == 0 and row["carbs"] == 0
    assert row["source"] == "telegram:kcal"
    space.conn.close()


def test_absurd_estimate_is_not_written(tmp_path: Path) -> None:
    gateway = FakeEstimateGateway(
        {"хумус": {"kcal": 5000, "protein": 400, "fat": 400, "carbs": 400}}
    )
    space = _make_env(tmp_path, gateway=None)
    space.service.food_gateway = gateway
    with TestClient(space.app) as client:
        space.client = client
        reply = space.service.handle_text("хумус 80 г", CHAT_ID)
    row = row_dict(food_rows(space)[-1])
    assert row["grams"] == 80, "граммы сохраняем"
    assert row["kcal"] == 0, "выдуманные 5000 ккал не пишем"
    assert row["source"] == "telegram:grams"
    assert "кбжу не посчитал" in reply.text.lower(), reply.text
    space.conn.close()


def test_estimate_failure_does_not_lose_grams(tmp_path: Path) -> None:
    gateway = FakeEstimateGateway({}, fail=True)
    space = _make_env(tmp_path, gateway=None)
    space.service.food_gateway = gateway
    with TestClient(space.app) as client:
        space.client = client
        reply = space.service.handle_text("хумус 80 г", CHAT_ID)
    row = row_dict(food_rows(space)[-1])
    assert row["grams"] == 80
    assert row["kcal"] == 0
    assert "кбжу не посчитал" in reply.text.lower(), reply.text
    space.conn.close()


# --------------------------------------------------------------------------
# 4. Санити: ерунда не попадает в дневник
# --------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", ["гречка 5000 г", "гречка 0 г"])
def test_wrong_grams_are_not_written(env, phrase: str) -> None:
    reply = send(env, phrase)
    assert food_rows(env) == []
    assert "грамм" in reply.text.lower(), reply.text


def test_nonsense_is_not_food(env) -> None:
    send(env, "абракадабра блабла")
    assert food_rows(env) == []


def test_money_phrase_with_food_word_stays_money(env) -> None:
    reply = send(env, "кофе 320 наличными")
    assert food_rows(env) == []
    row = env.conn.execute("SELECT amount_kopecks, kind FROM transactions").fetchone()
    assert row is not None, reply.text
    assert int(row["amount_kopecks"]) == 32000
    assert str(row["kind"]) == "expense"


# --------------------------------------------------------------------------
# 5. Откат на правила и страховка «еда важнее мысли»
# --------------------------------------------------------------------------


def test_rules_work_when_llm_is_unavailable(env) -> None:
    """Модель недоступна — фраза всё равно еда, граммы на месте."""
    reply = send(env, "на обед гречка 200 г и курица 150")
    assert len(food_rows(env)) == 2, reply.text


def test_llm_unavailable_gateway_falls_back(env) -> None:
    class Down:
        def available(self) -> bool:
            return True

        def json_chat(self, *args, **kwargs):
            raise RuntimeError("network down")

    service = handlers.BotService(
        env.settings, env.conn, v4_router=intent_v4.V4Router(gateway=Down())
    )
    reply = service.handle_text("на обед гречка 200 г и курица 150", CHAT_ID)
    assert len(food_rows(env)) == 2, reply.text


def test_llm_journal_does_not_swallow_food(env) -> None:
    """Модель назвала еду мыслью — страховка обработчика пишет еду.

    С t39 явная еда с граммами остаётся на правилах (модель не вызывается),
    поэтому страховку проверяем на разборе модели напрямую: обработчик обязан
    увидеть «продукт + граммы» и записать еду, а не журнал.
    """
    phrase = "на обед гречка 200 г и курица 150"
    service = handlers.BotService(env.settings, env.conn)
    reply = service._dispatch(
        {"intent": "journal", "text": phrase, "topics": ["работа"], "engine": "llm"},
        phrase,
        CHAT_ID,
    )
    assert len(food_rows(env)) == 2, reply.text
    journals = env.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    assert journals == 0, "еда не должна попадать в журнал"


def test_llm_food_intent_is_written(env) -> None:
    gateway = FakeParseGateway(
        {
            "intent": "food",
            "meal": "lunch",
            "items": [{"name": "гречка", "grams": 200}, {"name": "курица", "grams": 150}],
        }
    )
    service = handlers.BotService(
        env.settings, env.conn, v4_router=intent_v4.V4Router(gateway=gateway)
    )
    reply = service.handle_text("на обед гречка 200 г и курица 150", CHAT_ID)
    rows = [row_dict(row) for row in food_rows(env)]
    assert len(rows) == 2, reply.text
    assert {row["meal"] for row in rows} == {"lunch"}
    assert rows[0]["product_id"] == env.ids["Гречка ядрица"]


# --------------------------------------------------------------------------
# 6. Валидация ответа модели и подсказка
# --------------------------------------------------------------------------


def test_system_prompt_names_food_and_forbids_journal() -> None:
    prompt = intent_v4.SYSTEM_PROMPT
    assert '"intent":"food"' in prompt
    assert "не journal" in prompt.lower() or "а не journal" in prompt.lower()
    assert "items" in prompt


def test_validate_accepts_food_items() -> None:
    parsed = intent_v4.validate(
        {
            "intent": "food",
            "meal": "lunch",
            "items": [{"name": "гречка", "grams": 200}, {"product": "курица", "grams": "150"}],
        },
        "на обед гречка 200 г и курица 150",
        date(2026, 11, 18),
    )
    assert parsed is not None
    assert parsed["intent"] == "food"
    assert parsed["meal"] == "lunch"
    assert parsed["items"] == [
        {"name": "гречка", "grams": 200.0},
        {"name": "курица", "grams": 150.0},
    ]


def test_validate_rejects_empty_food() -> None:
    assert intent_v4.validate({"intent": "food"}, "поел", date(2026, 11, 18)) is None


def test_validate_rejects_absurd_grams() -> None:
    parsed = intent_v4.validate(
        {"intent": "food", "items": [{"name": "гречка", "grams": 9000}]},
        "гречка 9000",
        date(2026, 11, 18),
    )
    assert parsed is None


def test_validate_kcal_only_food() -> None:
    parsed = intent_v4.validate(
        {"intent": "food", "kcal": 600, "protein": 40},
        "съел 600 ккал и 40 белка",
        date(2026, 11, 18),
    )
    assert parsed is not None
    assert parsed["kcal"] == 600
    assert parsed["protein"] == 40


# --------------------------------------------------------------------------
# 7. Удаление и отмена
# --------------------------------------------------------------------------


def test_delete_last_removes_whole_phrase(env) -> None:
    send(env, "на обед гречка 200 г и курица 150")
    assert len(food_rows(env)) == 2
    ask = send(env, "удали последнее")
    assert "удал" in ask.text.lower(), ask.text
    done = env.service.handle_callback("v4:del:confirm", CHAT_ID, CHAT_ID)
    assert "удалено" in done.text.lower(), done.text
    assert food_rows(env) == []


# --------------------------------------------------------------------------
# 8. Модуль еды: матчинг продуктов и арифметика
# --------------------------------------------------------------------------


def test_product_matching_for_live_words(env) -> None:
    catalog = nutrition_v4.load_catalog(env.conn)
    cases = {
        "гречка": "Гречка ядрица",
        "курица": "Куриная грудка",
        "куриное филе": "Куриная грудка",
        "банан": "Банан",
        "творог": "Творог 5%",
        "рыба": "Треска филе",
        "овсянка": "Овсянка на воде",
        "яйца": "Яйцо куриное",
        "кофе": "Кофе чёрный",
        "масло": None,
        "абракадабра": None,
    }
    for query, expected in cases.items():
        hit = nutrition_v4.match_product(query, catalog)
        assert (hit["name"] if hit else None) == expected, query


def test_macros_use_base_grams(env) -> None:
    product = {"kcal": 200, "protein": 10, "fat": 5, "carbs": 20, "fiber": 1,
               "sugar": 2, "base_grams": 250}
    macros = nutrition_v4.macros_for(product, 500)
    assert macros["kcal"] == pytest.approx(400.0)
    assert macros["protein"] == pytest.approx(20.0)


def test_sanity_functions() -> None:
    assert nutrition_v4.sanity_grams(200) == 200
    assert nutrition_v4.sanity_grams(0) is None
    assert nutrition_v4.sanity_grams(5000) is None
    assert nutrition_v4.sanity_macros({"kcal": 100, "protein": 10}) is not None
    assert nutrition_v4.sanity_macros({"kcal": 1200, "protein": 10}) is None
    assert nutrition_v4.sanity_macros({"kcal": 100, "protein": 150}) is None
    assert nutrition_v4.sanity_macros({"kcal": 0}) is None


def test_grams_before_product_is_food(env) -> None:
    """Живая фраза владельца «150 г творога» — еда, а не мысль."""
    reply = send(env, "150 г творога")
    rows = [row_dict(row) for row in food_rows(env)]
    assert len(rows) == 1, reply.text
    assert rows[0]["product_id"] == env.ids["Творог 5%"]
    assert rows[0]["grams"] == 150
    assert rows[0]["kcal"] > 0 and rows[0]["protein"] > 0
    assert "творог" in reply.text.lower()


def test_omelette_from_eggs_counts_as_eggs(env) -> None:
    """«омлет из 3 яиц» — три яйца по 55 г, КБЖУ из справочника, не вопрос."""
    reply = send(env, "омлет из 3 яиц")
    rows = [row_dict(row) for row in food_rows(env)]
    assert len(rows) == 1, reply.text
    assert rows[0]["product_id"] == env.ids["Яйцо куриное"]
    assert rows[0]["grams"] == pytest.approx(165.0)
    assert rows[0]["kcal"] == pytest.approx(143 * 1.65, abs=1.5)
    assert "яйц" in reply.text.lower() and "ккал" in reply.text.lower()


def test_kcal_only_reply_names_type(env) -> None:
    """«съел 600 ккал» — в ответе видно, что это еда, а не что-то ещё."""
    reply = send(env, "съел 600 ккал и 40 белка")
    assert "ед" in reply.text.lower(), reply.text
    assert "600" in reply.text


# --------------------------------------------------------------------------
# 12. Замеры тела: вес, жир, мышцы и /weight
# --------------------------------------------------------------------------


def test_measurements_in_one_phrase(env) -> None:
    """«вес 81.2, жир 18.5, мышцы 35» — три замера и один ответ."""
    reply = send(env, "вес 81.2, жир 18.5, мышцы 35")
    rows = env.conn.execute("SELECT kind, value, unit FROM metrics").fetchall()
    got = {row["kind"]: (float(row["value"]), str(row["unit"])) for row in rows}
    assert got["weight"] == (pytest.approx(81.2), "кг")
    assert got["fat"] == (pytest.approx(18.5), "%")
    assert got["muscle"] == (pytest.approx(35.0), "кг")
    assert "81,2" in reply.text and "18,5" in reply.text and "35" in reply.text


@pytest.mark.parametrize(
    "phrase,kind,unit",
    [
        ("вес 81,2", "weight", "кг"),
        ("жир 18.5", "fat", "%"),
        ("мышцы 35", "muscle", "кг"),
        ("вес 82.4", "weight", "кг"),
    ],
)
def test_single_measurement_is_written(env, phrase, kind, unit) -> None:
    """Каждый замер по отдельности — тоже запись, а не мысль."""
    send(env, phrase)
    row = env.conn.execute(
        "SELECT kind, value, unit FROM metrics WHERE kind = ?", (kind,)
    ).fetchone()
    assert row is not None, "замер не записан: %s" % phrase
    assert str(row["unit"]) == unit


def _add_metric(env: Any, kind: str, value: float, day: str, unit: str) -> None:
    db.add_metric(env.conn, kind, value, date=day, unit=unit, source="telegram")
    env.conn.commit()


def test_weight_command_lists_ten_and_trend(env) -> None:
    """/weight: десять последних значений, тренд, средний за неделю, цель."""
    today = date.fromisoformat(env.settings.today())
    values = [79.4, 79.6, 80.0, 80.2, 80.5, 80.8, 81.0, 81.2, 81.4, 81.6, 81.8, 82.0]
    for index, value in enumerate(values):
        _add_metric(env, "weight", value, (today - timedelta(days=index * 2)).isoformat(), "кг")
    _add_metric(env, "fat", 18.5, env.settings.today(), "%")
    _add_metric(env, "muscle", 35.0, env.settings.today(), "кг")
    db.set_setting(env.conn, "pref.weight_goal_kg", json.dumps(78.0))
    env.conn.commit()

    reply = env.service.handle_weight()
    text = reply.text
    assert "последние 10" in text, text
    shown = text.splitlines()[0].split(":", 1)[1]
    assert len([part for part in shown.replace(" кг.", "").split("·") if part.strip()]) == 10
    assert "Тренд" in text and "кг за" in text
    assert "средний за 7 дней" in text
    assert "жир 18,5 %" in text and "мышцы 35 кг" in text
    assert "цель 78 кг" in text and "осталось 1,4 кг" in text


def test_weight_command_empty(env) -> None:
    reply = env.service.handle_weight()
    assert "Замеров веса ещё нет" in reply.text
    assert "вес 81,2" in reply.text


def test_weight_command_single_measurement(env) -> None:
    _add_metric(env, "weight", 79.4, env.settings.today(), "кг")
    reply = env.service.handle_weight()
    assert "79,4" in reply.text
    assert "Один замер" in reply.text


def test_weight_is_visible_through_api(env) -> None:
    """/weight читает ту же таблицу, что API веса — замеры видны в вебе."""
    _add_metric(env, "weight", 79.4, env.settings.today(), "кг")
    response = env.client.get("/api/health/weight", params={"days": 30})
    assert response.status_code == 200
    payload = response.json()
    assert payload["points"], payload
    assert payload["points"][-1]["value"] == pytest.approx(79.4)
    assert payload["current"] == pytest.approx(79.4)


# --------------------------------------------------------------------------
# 13. Фото еды: файл в медиа и привязка к приёму пищи
# --------------------------------------------------------------------------


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _photo(env: Any, caption: str, file_id: str = "photo-1") -> handlers.Reply:
    descriptor = media.MediaDescriptor(
        kind="photo",
        file_id=file_id,
        file_unique_id=file_id + "-u",
        mime_type="image/png",
        size=len(PNG),
        caption=caption,
        chat_id=CHAT_ID,
    )
    return env.service.handle_media(descriptor, PNG, CHAT_ID)


def test_photo_with_food_caption_writes_meal_and_links(env) -> None:
    """Подпись «на обед гречка 200 г» — это еда; фото привязано к обеду."""
    reply = _photo(env, "на обед гречка 200 г")
    rows = [row_dict(row) for row in food_rows(env)]
    assert len(rows) == 1, reply.text
    row = rows[0]
    assert row["meal"] == "lunch"
    assert row["product_id"] == env.ids["Гречка ядрица"]
    assert row["grams"] == 200
    assert round(float(row["kcal"])) == 686
    assert "Привязал к приёму пищи" in reply.text
    assert "осталось" in reply.text.lower(), "нет остатка нормы: %s" % reply.text

    entry = env.conn.execute(
        "SELECT id, kind, text, meta_json FROM entries ORDER BY id DESC LIMIT 1"
    ).fetchone()
    meta = json.loads(entry["meta_json"])
    assert meta["meal"] == "lunch"
    assert meta["food_log_ids"] == [row["id"]]
    assert entry["text"] == "на обед гречка 200 г"
    assert meta["media"]["rel_path"]


def test_photo_without_caption_saves_and_asks(env) -> None:
    """Без подписи фото просто сохраняется: распознавать содержимое не обещаем."""
    reply = _photo(env, "")
    assert "Что на фото" in reply.text
    assert food_rows(env) == []
    entry = env.conn.execute(
        "SELECT kind, text FROM entries ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert entry is not None and str(entry["kind"]) == "doc"


def test_photo_non_food_caption_stays_journal_only(env) -> None:
    """Подпись не про еду — запись журнала, food_log не трогаем."""
    reply = _photo(env, "вид из окна")
    assert food_rows(env) == []
    assert "Привязал" not in reply.text
    assert food_rows(env) == []


def test_photo_food_is_visible_in_nutrition_api(env) -> None:
    """Приём пищи с фото виден в API питания — то есть на экране «Питание»."""
    _photo(env, "на завтрак овсянка 250 г", file_id="photo-2")
    day = env.client.get(
        "/api/nutrition/day", params={"date": env.settings.today()}
    ).json()
    titles = [
        str(entry.get("title") or entry.get("text") or "")
        for meal in day["meals"]
        for entry in meal["entries"]
    ]
    assert any("Овсянка" in title for title in titles), titles
    assert day["totals"]["kcal"] >= 220


def test_photo_phrase_is_not_double_counted(env) -> None:
    """Фото с едой не пишет еду дважды (подпись не проходит общий разбор)."""
    _photo(env, "на обед гречка 200 г", file_id="photo-3")
    assert len(food_rows(env)) == 1
