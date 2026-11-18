"""Еда по граммам: справочник продуктов, КБЖУ и дневник из Telegram.

Владелец пишет «на обед гречка 200 г и курица 150», и это должно стать
строками ``food_log`` с граммами, ссылкой на продукт и КБЖУ на порцию —
а не заметкой в журнале. Модуль делает вторую половину работы: разобранные
позиции (:mod:`app.bot.intent_v4`) превращает в записи дневника и в короткий
ответ «что съедено + итог дня + остаток до нормы».

Как считаются цифры:

* продукт есть в справочнике ``products`` — КБЖУ берётся из него и
  пересчитывается как ``на 100 г × граммы / base_grams`` (``base_grams``
  обычно 100, но порция может быть другой);
* продукта нет — КБЖУ на 100 г оценивает LLM (``reasoning_effort=xhigh``),
  запись помечается в ответе словом «примерно», а кнопки предлагают
  «Добавить в справочник» или «Только калории»;
* продукт назван без граммов — строка не пишется, бот честно спрашивает
  граммовку, известные позиции при этом сохраняются.

Санити-проверки (ерунда не попадает в базу): калории 0–900 на 100 г,
белки/жиры/углеводы 0–100 на 100 г, граммы 1–3000, название 2–80 символов.
Оценка, не прошедшая проверку, отбрасывается: лучше спросить, чем соврать.

Все функции пишут через переданное соединение и **не коммитят** — вызывающий
код (``BotService``) оборачивает вызовы в ``with conn:``. Записи сразу видны
API и веб-экрану «Питание»: это та же таблица ``food_log``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.server import db

logger = logging.getLogger("lifeos.bot.food")

__all__ = [
    "ESTIMATE_KEY",
    "FoodResult",
    "MEAL_LABELS",
    "MEALS",
    "PENDING_INPUT_KEY",
    "add_products_to_catalog",
    "apply_catalog_input",
    "apply_kcal_input",
    "ask_catalog_input",
    "ask_kcal_input",
    "clear_pending_estimate",
    "clear_pending_input",
    "estimate_macros",
    "keep_kcal_only",
    "load_macros",
    "load_pending_estimate",
    "load_pending_input",
    "parse_pending_numbers",
    "save_pending_input",
    "water_progress",
    "water_target",
    "log_food",
    "match_product",
    "merge_extras",
    "save_pending_estimate",
]

#: Приёмы пищи в порядке дневника (совпадает с API ``/api/nutrition/day``).
MEALS: Tuple[str, ...] = ("breakfast", "lunch", "dinner", "snack")

#: Подписи приёмов пищи в ответе бота.
MEAL_LABELS: Dict[str, str] = {
    "breakfast": "Завтрак",
    "lunch": "Обед",
    "dinner": "Ужин",
    "snack": "Перекус",
}

#: Макросы дневника: порядок вывода и подписи.
MACROS: Tuple[str, ...] = ("kcal", "protein", "fat", "carbs", "fiber", "sugar")
SHORT_LABELS: Dict[str, str] = {"kcal": "ккал", "protein": "Б", "fat": "Ж", "carbs": "У"}

#: Нормы по умолчанию, если строки ``nutrition_targets`` нет (как в миграции 4).
DEFAULT_TARGETS: Dict[str, float] = {
    "kcal": 2300.0,
    "protein": 150.0,
    "fat": 75.0,
    "carbs": 260.0,
}

#: Санити-пределы: КБЖУ на 100 г и граммовка порции.
MAX_KCAL_PER_100 = 900.0
MAX_MACRO_PER_100 = 100.0
MIN_GRAMS = 1.0
MAX_GRAMS = 3000.0
MAX_NAME_CHARS = 80
MIN_NAME_CHARS = 2

#: Ключ в ``settings_kv``: оценка, которую можно сохранить в справочник.
ESTIMATE_KEY = "bot:food_estimate"

#: Слова-продукты для «это еда?»: курица/курин, гречка, творог и т.д.
#: Из них считается :data:`FOOD_STEMS` — уже основами (см. ``_stem``).
_FOOD_WORDS = tuple(
    {
        "авокад", "апельсин", "багет", "баклаж", "банан", "батон", "батончик",
        "бекон", "белок", "борщ", "брокколи", "булгур", "бургер", "бутерброд",
        "виноград", "говядин", "говяж", "горох", "горошек", "гранат", "гречк",
        "груш", "джем", "запеканк", "зефир", "изюм", "икра", "индейк", "йогурт",
        "кабач", "какао", "кальмар", "капуст", "картоф", "картош", "кефир",
        "кешью", "киви", "киноа", "клубник", "колбас", "кофе", "креветк",
        "кукуруз", "кунжут", "кураг", "куриц", "курин", "кускус", "лавш",
        "лапш", "лимон", "лосос", "лук", "макарон", "малин", "мандарин",
        "мед", "миндал", "минтай", "молок", "морков", "мороженое",
        "моцарелл", "мяс", "нут", "овощ", "овсян", "овсянк", "огурец", "омлет",
        "орех", "паста", "паштет", "перец", "перловк", "персик", "печен",
        "печень", "печенье", "пицц", "плов", "помидор", "простокваш", "пшен",
        "пшено", "ряженк", "фил", "рис", "ростбиф", "рыб", "салат", "свинин",
        "свёкл", "свекл", "сельд", "семен", "семечк", "сгущ", "скумбри",
        "сметан", "сок", "сосиск", "судак", "суп", "сыр", "сырник",
        "творог", "телятин", "тилапи", "тост", "треск", "тунец", "тыкв",
        "фарш", "фасол", "фундук", "хлеб", "хлебц", "хлопь", "хурм",
        "цветн", "чай", "чеснок", "черник", "чечев", "шампиньон", "шаурм",
        "шоколад", "шпинат", "яблок", "ягод", "яиц", "яйц",
    }
)


#: Как человек называет продукт -> как он записан в справочнике.
SYNONYMS: Dict[str, str] = {
    "курица": "Куриная грудка",
    "курицу": "Куриная грудка",
    "курицы": "Куриная грудка",
    "курятина": "Куриная грудка",
    "куриное мясо": "Куриная грудка",
    "грудка": "Куриная грудка",
    "мясо": "Говядина лопатка",
    "рыба": "Треска филе",
    "рыбу": "Треска филе",
    "рыбы": "Треска филе",
    "картошка": "Картофель",
    "картошку": "Картофель",
    "яйца": "Яйцо куриное",
    "яиц": "Яйцо куриное",
    "яйцо": "Яйцо куриное",
    "хлеб": "Хлеб цельнозерновой",
    "овсянка": "Овсянка на воде",
    "овсянку": "Овсянка на воде",
    "макароны": "Макароны из твёрдых сортов",
}

#: Когда подходящих продуктов несколько — что выбрать по умолчанию.
#: Выбор всегда виден в ответе («из справочника: …»), его можно удалить.
PREFERRED: Dict[str, str] = {
    "гречка": "Гречка ядрица",
    "гречк": "Гречка ядрица",
    "овсянка": "Овсянка на воде",
    "рис": "Рис басмати",
    "курица": "Куриная грудка",
    "куриц": "Куриная грудка",
    "курин": "Куриная грудка",
    "сыр": "Сыр российский",
    "кофе": "Кофе чёрный",
    "чай": "Чай зелёный",
    "хлеб": "Хлеб цельнозерновой",
    "творог": "Творог 5%",
    "молоко": "Молоко 2,5%",
    "фарш": "Говяжий фарш 10%",
    "печень": "Печень куриная",
    "рыба": "Треска филе",
    "мясо": "Говядина лопатка",
    "яйца": "Яйцо куриное",
    "яйцо": "Яйцо куриное",
    "салат": "Салат листовой",
    "йогурт": "Греческий йогурт",
    "сок": "Сок апельсиновый",
}

#: Окончания, которые отрезаются при сравнении слов (морфология «на глаз»).
_ENDINGS: Tuple[str, ...] = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ах", "ях",
    "ов", "ев", "ый", "ий", "ая", "ое", "ые", "ых", "ом", "ем", "ой", "ей",
    "ую", "юю", "а", "о", "ы", "и", "у", "ю", "е", "я", "ь",
)

_WORD_RE = re.compile(r"[a-zа-я0-9]+")

#: Предлоги и союзы в названиях блюд: «Гречка с маслом» — два слова, не три.
_STOP_TOKENS = frozenset({"с", "со", "на", "из", "и", "в", "во", "по", "для"})


# --------------------------------------------------------------------------
# Нормализация названий и слова
# --------------------------------------------------------------------------


def _norm(text: Any) -> str:
    """Название для сравнения: нижний регистр, ё→е, без пунктуации."""
    value = str(text or "").casefold().replace("ё", "е")
    return " ".join(_WORD_RE.findall(value))


def _tokens(text: Any) -> List[str]:
    """Слова названия без чисел: «2 яйца» — про яйца, а не про двойку."""
    return [
        word
        for word in _norm(text).split()
        if not word.isdigit() and word not in _STOP_TOKENS
    ]


def _stem(word: str) -> str:
    """Грубая основа слова: «курицей» -> «куриц», «яйца» -> «яйц»."""
    value = str(word or "").casefold().replace("ё", "е")
    for ending in _ENDINGS:
        if len(value) - len(ending) >= 3 and value.endswith(ending):
            return value[: -len(ending)]
    return value


def _stems(text: Any) -> List[str]:
    return [_stem(token) for token in _tokens(text)]


#: Основы слов-продуктов: «кофе» -> «коф», «мороженое» -> «морожен».
FOOD_STEMS = frozenset(_stem(word) for word in _FOOD_WORDS)


def _signal_stems(name: Any) -> List[str]:
    """Основы «главных» слов названия: у «Гречка с маслом» — только гречка.

    Правило: если первое слово само по себе еда, второстепенные слова
    (масло, сыр, ягоды) не считаются сигналом — иначе «масло» находило бы
    «Гречку с маслом». Если первое слово не еда («Греческий йогурт»),
    сигналом становится первое слово-еда.
    """
    stems = _stems(name)
    if not stems:
        return []
    if stems[0] in FOOD_STEMS:
        return [stems[0]]
    for stem in stems:
        if stem in FOOD_STEMS:
            return [stem]
    return [stems[0]]


def is_food_word(name: Any) -> bool:
    """Похоже ли слово на продукт: «курица», «гречка», «омлет» — да."""
    stems = _stems(name)
    return bool(stems) and stems[0] in FOOD_STEMS


# --------------------------------------------------------------------------
# Справочник: загрузка, поиск продукта
# --------------------------------------------------------------------------


def load_catalog(conn: Any) -> List[Dict[str, Any]]:
    """Все неархивные продукты справочника (123 строки — читаем целиком)."""
    rows = conn.execute(
        "SELECT id, name, brand, kcal, protein, fat, carbs, fiber, sugar, base_grams, "
        "archived FROM products WHERE archived = 0 ORDER BY name COLLATE NOCASE"
    ).fetchall()
    catalog: List[Dict[str, Any]] = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        item["_norm"] = _norm(item["name"])
        item["_first"] = (_tokens(item["name"]) or [""])[0]
        item["_signal"] = _signal_stems(item["name"])
        catalog.append(item)
    return catalog


def _score(query: str, product: Mapping[str, Any]) -> Tuple[float, str]:
    """Насколько название продукта подходит запросу: (0..1, почему)."""
    norm = _norm(query)
    if not norm:
        return 0.0, ""
    if product["_norm"] == norm:
        return 1.0, "точное название"
    if product["_first"] == norm:
        return 0.96, "первое слово"
    query_stems = _stems(norm)
    if not query_stems:
        return 0.0, ""
    signal = product.get("_signal") or []
    if signal and any(stem == signal[0] for stem in query_stems):
        return 0.92, "основа"
    if product["_norm"].startswith(norm) or norm.startswith(product["_norm"]):
        # «сыр» -> «Сырники» — тоже совпадение, но слабее точного слова.
        return 0.88, "начало названия"
    if signal:
        ratio = max(
            SequenceMatcher(None, stem, signal[0]).ratio() for stem in query_stems
        )
        if ratio >= 0.78:
            return round(ratio * 0.8, 4), "похожее слово"
    return 0.0, ""


def match_product(
    name: Any, catalog: Sequence[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Ищет продукт по названию; ``None`` — в справочнике нет.

    Порядок: синоним человека -> точное/начальное совпадение -> основа слова
    -> похожее слово. Если подходящих несколько, берётся продукт из
    ``PREFERRED`` (если он есть в справочнике), иначе — самое точное.
    """
    query = _norm(name)
    if not query:
        return None
    synonym = SYNONYMS.get(query)
    if synonym:
        target = _norm(synonym)
        for product in catalog:
            if product["_norm"] == target:
                return _hit(product, 1.0, "синоним")

    scored: List[Tuple[float, int, Dict[str, Any], str]] = []
    for product in catalog:
        score, reason = _score(query, product)
        if score > 0:
            scored.append((score, len(str(product["name"])), product, reason))
    if not scored:
        return None
    scored.sort(
        key=lambda item: (
            -item[0],
            -SequenceMatcher(None, query, item[2]["_norm"]).ratio(),
            item[1],
        )
    )

    preferred = PREFERRED.get(query)
    if preferred:
        target = _norm(preferred)
        for score, _, product, reason in scored:
            if product["_norm"] == target:
                return _hit(product, max(score, 0.9), reason + " + выбор по умолчанию")

    best_score = scored[0][0]
    best = [item for item in scored if best_score - item[0] < 1e-9]
    if len(best) > 1:
        logger.info(
            "продукт %r: несколько кандидатов (%s), беру %r",
            name,
            ", ".join(str(item[2]["name"]) for item in best[:4]),
            best[0][2]["name"],
        )
    score, _, product, reason = scored[0]
    return _hit(product, score, reason)


def _hit(product: Mapping[str, Any], score: float, reason: str) -> Dict[str, Any]:
    hit = dict(product)
    hit["match_score"] = score
    hit["match_reason"] = reason
    return hit


# --------------------------------------------------------------------------
# КБЖУ: из справочника и оценка LLM
# --------------------------------------------------------------------------


def _round(value: Any, digits: int = 1) -> float:
    try:
        return round(float(value or 0), digits)
    except (TypeError, ValueError):
        return 0.0


def macros_for(product: Mapping[str, Any], grams: float) -> Dict[str, float]:
    """КБЖУ порции: ``на единицу справочника × граммы / base_grams``."""
    base = _round(product.get("base_grams"), 1) or 100.0
    factor = float(grams) / base if base > 0 else 0.0
    return {macro: _round(product.get(macro) or 0, 2) * factor for macro in MACROS}


def sanity_macros(per100: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    """Проверяет КБЖУ на 100 г: ерунда -> ``None`` (лучше не писать вовсе)."""
    values: Dict[str, float] = {}
    for macro in MACROS:
        try:
            value = float(per100.get(macro) or 0)
        except (TypeError, ValueError):
            return None
        if value < 0:
            return None
        limit = MAX_KCAL_PER_100 if macro == "kcal" else MAX_MACRO_PER_100
        if value > limit:
            return None
        values[macro] = round(value, 1)
    if values["kcal"] <= 0:
        return None
    return values


def sanity_grams(grams: Any) -> Optional[float]:
    """Граммовка порции: 1–3000 г, иначе ``None``."""
    try:
        value = float(grams)
    except (TypeError, ValueError):
        return None
    if not (MIN_GRAMS <= value <= MAX_GRAMS):
        return None
    return round(value, 1)


def sanity_name(name: Any) -> Optional[str]:
    """Название продукта: 2–80 символов, без переводов строк."""
    value = " ".join(str(name or "").split())
    if not (MIN_NAME_CHARS <= len(value) <= MAX_NAME_CHARS):
        return None
    return value


_ESTIMATE_PROMPT = """Ты оцениваешь пищевую ценность продукта на 100 г.
Верни ТОЛЬКО один JSON-объект без пояснений и markdown:
{"kcal":0,"protein":0,"fat":0,"carbs":0,"fiber":0,"sugar":0}
Значения — обычные для этого продукта в готовом/сыром виде, как в справочниках
калорийности. Только числа, без единиц и диапазонов. Если это не продукт,
верни {"kcal":0}."""


def estimate_macros(gateway: Any, name: str) -> Optional[Dict[str, float]]:
    """КБЖУ на 100 г через LLM (xhigh). ``None`` — модель недоступна/ерунда.

    Оценка всегда проверяется ``sanity_macros``: правдоподобные пределы
    отсекают «1000 ккал на 100 г морковки» и опечатки модели.
    """
    if gateway is None:
        return None
    clean_name = sanity_name(name)
    if clean_name is None:
        return None
    try:
        available = getattr(gateway, "available", None)
        if callable(available) and not available():
            return None
    except Exception:  # pragma: no cover - шлюз не должен ломать запись
        return None
    messages = [
        {"role": "system", "content": _ESTIMATE_PROMPT},
        {"role": "user", "content": clean_name},
    ]
    try:
        try:
            data = gateway.json_chat(messages, max_tokens=900, reasoning_effort="xhigh")
        except TypeError:
            data = gateway.json_chat(messages, max_tokens=900)
    except Exception as exc:
        logger.warning("оценка КБЖУ (%s): сбой модели (%s)", clean_name, type(exc).__name__)
        return None
    if not isinstance(data, Mapping):
        logger.warning("оценка КБЖУ (%s): модель вернула не объект", clean_name)
        return None
    values = sanity_macros(data)
    if values is None:
        logger.warning("оценка КБЖУ (%s): значения вне пределов, не пишу", clean_name)
        return None
    return values


# --------------------------------------------------------------------------
# Ожидающая оценка (кнопки «в справочник» / «только калории»)
# --------------------------------------------------------------------------


def save_pending_estimate(conn: Any, payload: Mapping[str, Any]) -> None:
    db.set_setting(conn, ESTIMATE_KEY, json.dumps(dict(payload), ensure_ascii=False))


def load_pending_estimate(conn: Any) -> Optional[Dict[str, Any]]:
    raw = db.get_setting(conn, ESTIMATE_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def clear_pending_estimate(conn: Any) -> None:
    db.set_setting(conn, ESTIMATE_KEY, "")


def add_products_to_catalog(conn: Any, payload: Mapping[str, Any]) -> Tuple[int, str]:
    """Сохраняет оценённые продукты в справочник и привязывает записи.

    Возвращает ``(сколько продуктов добавлено, короткая строка для ответа)``.
    Повторное нажатие не плодит дубликаты: продукт ищется по названию.
    """
    catalog = load_catalog(conn)
    added: List[str] = []
    linked = 0
    for item in payload.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        name = sanity_name(item.get("name"))
        values = sanity_macros(item.get("per100") or {})
        if name is None or values is None:
            continue
        existing = match_product(name, catalog)
        if existing is not None and existing.get("_norm") == _norm(name):
            product_id = int(existing["id"])
        else:
            cursor = conn.execute(
                "INSERT INTO products (name, brand, kcal, protein, fat, carbs, fiber, "
                "sugar, base_grams) VALUES (?, '', ?, ?, ?, ?, ?, ?, 100)",
                (
                    name,
                    values["kcal"],
                    values["protein"],
                    values["fat"],
                    values["carbs"],
                    values["fiber"],
                    values["sugar"],
                ),
            )
            product_id = int(cursor.lastrowid or 0)
            added.append(name)
            catalog.append(
                {
                    "id": product_id,
                    "name": name,
                    "_norm": _norm(name),
                    "_first": (_tokens(name) or [""])[0],
                    "_signal": _signal_stems(name),
                }
            )
        ids = [int(value) for value in item.get("ids") or [] if str(value).isdigit()]
        for food_id in ids:
            conn.execute(
                "UPDATE food_log SET product_id = ?, source = 'telegram:catalog' WHERE id = ?",
                (product_id, food_id),
            )
            linked += 1
    if added:
        text = "Добавил в справочник: %s." % ", ".join(added[:3])
    elif linked:
        text = "Продукт уже был в справочнике — привязал запись."
    else:
        text = "Нечего добавлять: запись уже удалена."
    return len(added), text


def keep_kcal_only(conn: Any, payload: Mapping[str, Any]) -> int:
    """Оставляет у записей только калории (оценку Б/Ж/У владелец не принял)."""
    ids: List[int] = []
    for item in payload.get("items") or []:
        if isinstance(item, Mapping):
            ids.extend(int(value) for value in item.get("ids") or [] if str(value).isdigit())
    if not ids:
        return 0
    marks = ", ".join("?" for _ in ids)
    cursor = conn.execute(
        "UPDATE food_log SET protein = 0, fat = 0, carbs = 0, fiber = 0, sugar = 0, "
        "source = 'telegram:kcal' WHERE id IN (%s)" % marks,
        tuple(ids),
    )
    return int(cursor.rowcount or 0)


# --------------------------------------------------------------------------
# Ввод владельца: КБЖУ на 100 г или калории порции
# --------------------------------------------------------------------------

#: Ключ в ``settings_kv``: бот спросил КБЖУ и ждёт одну числовую строку.
PENDING_INPUT_KEY = "bot:food_pending_input"

#: Сколько живёт ожидание ответа: полчаса, потом кнопка считается устаревшей.
PENDING_INPUT_TTL = 30 * 60

#: Строка только из чисел: «240 4 1 60», «72».
_NUMBERS_ONLY_RE = re.compile(r"^[\d\s.,;]+$")


def parse_pending_numbers(text: Any) -> Optional[List[float]]:
    """«240 4 1 60» -> [240, 4, 1, 60]; всё, что не числа 1–4 штук, — ``None``.

    Это единственный вход, который перехватывает ожидание КБЖУ: обычная
    фраза («гречка 200 г») не должна превращаться в ответ на вопрос.
    """
    raw = str(text or "").strip()
    if not raw or len(raw) > 40 or not _NUMBERS_ONLY_RE.match(raw):
        return None
    values: List[float] = []
    for token in re.split(r"[\s;]+", raw):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(round(float(token.replace(",", ".")), 2))
        except ValueError:
            return None
    if not 1 <= len(values) <= 4:
        return None
    if any(value < 0 or value > MAX_KCAL_PER_100 * 5 for value in values):
        return None
    return values


def save_pending_input(conn: Any, mode: str, items: Sequence[Mapping[str, Any]]) -> None:
    """Запоминает, какой продукт ждёт КБЖУ: ``mode`` = catalog | kcal."""
    payload = {
        "mode": str(mode),
        "items": [dict(item) for item in items],
        "ts": int(time.time()),
    }
    db.set_setting(conn, PENDING_INPUT_KEY, json.dumps(payload, ensure_ascii=False))


def load_pending_input(conn: Any) -> Optional[Dict[str, Any]]:
    """Ожидание ввода; устаревшее (старше 30 минут) отбрасывается."""
    raw = db.get_setting(conn, PENDING_INPUT_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("items"):
        return None
    try:
        age = time.time() - float(payload.get("ts") or 0)
    except (TypeError, ValueError):
        age = PENDING_INPUT_TTL + 1
    if age > PENDING_INPUT_TTL:
        return None
    return payload


def clear_pending_input(conn: Any) -> None:
    db.set_setting(conn, PENDING_INPUT_KEY, "")


def ask_catalog_input(items: Sequence[Mapping[str, Any]]) -> str:
    """Вопрос про КБЖУ на 100 г: «курага» — ккал Б Ж У."""
    names = ", ".join("«%s»" % str(item.get("name") or "") for item in items[:2])
    return (
        "%s: напиши КБЖУ на 100 г — ккал, белки, жиры, углеводы. "
        "Например «240 4 1 60»." % names
    )


def ask_kcal_input(items: Sequence[Mapping[str, Any]]) -> str:
    """Вопрос про калории готовой порции: «курага 30 г» — сколько ккал."""
    head = items[0] if items else {}
    name = str(head.get("name") or "")
    grams = head.get("grams")
    portion = "%s %s г" % (name, _fmt(grams, 0)) if grams else name
    return "%s: сколько калорий в этой порции? Напиши число, например «72»." % (
        portion or "Порция"
    )


def _portion_macros(per100: Mapping[str, float], grams: Any) -> Dict[str, float]:
    """КБЖУ порции: значения на 100 г × граммы / 100."""
    try:
        grams_value = float(grams or 0)
    except (TypeError, ValueError):
        grams_value = 0.0
    if grams_value <= 0:
        return {macro: 0.0 for macro in MACROS}
    return {macro: _round(per100.get(macro) or 0, 2) * grams_value / 100.0 for macro in MACROS}


def _first_row(conn: Any, item: Mapping[str, Any]) -> Optional[Any]:
    for food_id in item.get("ids") or []:
        if not str(food_id).isdigit():
            continue
        row = conn.execute(
            "SELECT id, date, grams FROM food_log WHERE id = ?", (int(food_id),)
        ).fetchone()
        if row is not None:
            return row
    return None


def _drop_first(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return items[1:]


def _pending_tail(
    conn: Any, mode: str, items: Sequence[Mapping[str, Any]]
) -> Tuple[str, bool]:
    """Что делать после применения: спросить следующий продукт или закончить."""
    rest = [dict(item) for item in items]
    if not rest:
        clear_pending_input(conn)
        return "", False
    save_pending_input(conn, mode, rest)
    ask = ask_catalog_input if mode == "catalog" else ask_kcal_input
    return "\n" + ask(rest), True


def _day_tail(conn: Any, day: str) -> str:
    """Строки «Сегодня: … из … ккал» и «Осталось: …» после уточнения."""
    return "\n" + "\n".join(_day_line(day, day_totals(conn, day), day_targets(conn)))


def apply_catalog_input(
    conn: Any, payload: Mapping[str, Any], numbers: Sequence[float]
) -> Tuple[str, bool]:
    """«Добавить в справочник» без оценки: владелец назвал КБЖУ на 100 г.

    Пишет продукт в ``products``, привязывает к нему запись дневника и
    пересчитывает её КБЖУ по граммам. Возвращает ``(текст, ждёт ли ещё)``.
    """
    items = [dict(item) for item in payload.get("items") or [] if isinstance(item, Mapping)]
    if not items:
        clear_pending_input(conn)
        return "Позиция уже удалена — добавлять нечего.", False
    per100 = {
        "kcal": numbers[0] if len(numbers) > 0 else 0.0,
        "protein": numbers[1] if len(numbers) > 1 else 0.0,
        "fat": numbers[2] if len(numbers) > 2 else 0.0,
        "carbs": numbers[3] if len(numbers) > 3 else 0.0,
        "fiber": 0.0,
        "sugar": 0.0,
    }
    values = sanity_macros(per100)
    if values is None:
        return (
            "Не понял КБЖУ: нужны 1–4 числа на 100 г, ккал от 1 до %d, Б/Ж/У до %d. "
            "Например «240 4 1 60»." % (int(MAX_KCAL_PER_100), int(MAX_MACRO_PER_100)),
            True,
        )

    item = items[0]
    row = _first_row(conn, item)
    name = sanity_name(item.get("name"))
    if name is None or row is None:
        rest = _drop_first(items)
        tail, waiting = _pending_tail(conn, "catalog", rest)
        return "Позиция уже удалена — добавлять нечего." + tail, waiting

    catalog = load_catalog(conn)
    existing = match_product(name, catalog)
    if existing is not None and existing.get("_norm") == _norm(name):
        product_id = int(existing["id"])
        conn.execute(
            "UPDATE products SET kcal = ?, protein = ?, fat = ?, carbs = ?, fiber = ?, "
            "sugar = ?, archived = 0 WHERE id = ?",
            (
                values["kcal"],
                values["protein"],
                values["fat"],
                values["carbs"],
                values["fiber"],
                values["sugar"],
                product_id,
            ),
        )
        title = "Обновил «%s» в справочнике" % existing["name"]
    else:
        cursor = conn.execute(
            "INSERT INTO products (name, brand, kcal, protein, fat, carbs, fiber, sugar, "
            "base_grams) VALUES (?, '', ?, ?, ?, ?, ?, ?, 100)",
            (
                name,
                values["kcal"],
                values["protein"],
                values["fat"],
                values["carbs"],
                values["fiber"],
                values["sugar"],
            ),
        )
        product_id = int(cursor.lastrowid or 0)
        title = "Добавил «%s» в справочник" % name

    grams = float(row["grams"] or 0)
    macros = _portion_macros(values, grams)
    conn.execute(
        "UPDATE food_log SET product_id = ?, text = ?, kcal = ?, protein = ?, fat = ?, "
        "carbs = ?, fiber = ?, sugar = ?, source = 'telegram:catalog' WHERE id = ?",
        (
            product_id,
            ("%s %s г" % (name, _fmt(grams))) if grams else name,
            _round(macros["kcal"], 1),
            _round(macros["protein"], 1),
            _round(macros["fat"], 1),
            _round(macros["carbs"], 1),
            _round(macros["fiber"], 1),
            _round(macros["sugar"], 1),
            int(row["id"]),
        ),
    )
    text = "%s: %s ккал, Б %s · Ж %s · У %s на 100 г." % (
        title,
        _fmt(values["kcal"]),
        _fmt(values["protein"]),
        _fmt(values["fat"]),
        _fmt(values["carbs"]),
    )
    if grams:
        text += " %s г = %s ккал." % (_fmt(grams), _fmt(macros["kcal"]))
    rest = _drop_first(items)
    tail, waiting = _pending_tail(conn, "catalog", rest)
    return text + tail + _day_tail(conn, str(row["date"])), waiting


def apply_kcal_input(
    conn: Any, payload: Mapping[str, Any], numbers: Sequence[float]
) -> Tuple[str, bool]:
    """«Только калории»: владелец назвал калории порции — Б/Ж/У не выдумываем."""
    items = [dict(item) for item in payload.get("items") or [] if isinstance(item, Mapping)]
    if not items:
        clear_pending_input(conn)
        return "Позиция уже удалена — записывать нечего.", False
    kcal = float(numbers[0] or 0) if numbers else 0.0
    if not 0 < kcal <= MAX_KCAL_PER_100 * 5:
        return (
            "Нужно число калорий в порции, например «72» (не больше %d)."
            % int(MAX_KCAL_PER_100 * 5),
            True,
        )
    item = items[0]
    row = _first_row(conn, item)
    if row is None:
        rest = _drop_first(items)
        tail, waiting = _pending_tail(conn, "kcal", rest)
        return "Позиция уже удалена — записывать нечего." + tail, waiting
    conn.execute(
        "UPDATE food_log SET kcal = ?, protein = 0, fat = 0, carbs = 0, fiber = 0, "
        "sugar = 0, source = 'telegram:kcal' WHERE id = ?",
        (_round(kcal, 1), int(row["id"])),
    )
    grams = float(row["grams"] or 0)
    portion = "%s %s г" % (str(item.get("name") or ""), _fmt(grams)) if grams else str(
        item.get("name") or "порция"
    )
    text = "Записал %s: %s ккал (только калории)." % (portion, _fmt(kcal))
    rest = _drop_first(items)
    tail, waiting = _pending_tail(conn, "kcal", rest)
    return text + tail + _day_tail(conn, str(row["date"])), waiting


# --------------------------------------------------------------------------
# Вода
# --------------------------------------------------------------------------


def water_target(conn: Any) -> float:
    """Норма воды из ``nutrition_targets`` (мл); 2200 — дефолт миграции 4."""
    try:
        row = conn.execute("SELECT water_ml FROM nutrition_targets WHERE id = 1").fetchone()
    except Exception:  # pragma: no cover - старая база без норм
        row = None
    try:
        value = float(row["water_ml"] or 0) if row is not None else 0.0
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else 2200.0


def water_progress(conn: Any, day: str) -> str:
    """Строка «Вода: 0,5 из 2,2 л» — вода живёт в метрике ``water``, не в еде."""
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(value), 0) AS ml FROM metrics "
            "WHERE kind IN ('water', 'water_ml') AND date = ?",
            (day,),
        ).fetchone()
    except Exception:  # pragma: no cover - очень старая база
        row = None
    ml = float(row["ml"] or 0) if row is not None else 0.0
    target = water_target(conn)
    if ml <= 0:
        return "Вода: пока 0 из %s л." % _fmt(target / 1000.0, 1)
    left = target - ml
    if left > 0:
        tail = "осталось %s л" % _fmt(left / 1000.0, 1)
    else:
        tail = "норма закрыта"
    return "Вода: %s из %s л (%s)." % (_fmt(ml / 1000.0, 1), _fmt(target / 1000.0, 1), tail)


# --------------------------------------------------------------------------
# Запись дневника
# --------------------------------------------------------------------------


@dataclass
class FoodResult:
    """Итог записи еды: короткий ответ, кнопки и операции для отмены."""

    text: str
    operations: List[Dict[str, Any]] = field(default_factory=list)
    label: str = ""
    edit_hint: str = ""
    buttons: List[List[Tuple[str, str]]] = field(default_factory=list)
    estimated: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    kcal: float = 0.0
    macros: Dict[str, float] = field(default_factory=dict)


def _fmt(value: Any, digits: int = 0) -> str:
    """Число для человека: запятая, без хвостовых нулей."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    text = "%.*f" % (digits, number)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def _item_line(
    item: Mapping[str, Any], *, estimated: bool, unknown: bool = False
) -> str:
    """Строка позиции: «гречка 200 г — 686 ккал, Б 25 · Ж 7 · У 114»."""
    name = str(item.get("name") or "")
    grams = item.get("grams")
    head = "%s %s г" % (name, _fmt(grams, 0)) if grams else name
    if unknown:
        return "%s — КБЖУ нет" % head
    macros = item.get("macros") or {}
    kcal = _fmt(macros.get("kcal") or 0, 0)
    mark = "≈" if estimated else ""
    parts = [
        "%s%s ккал" % (mark, kcal),
        "Б " + _fmt(macros.get("protein") or 0, 0),
        "Ж " + _fmt(macros.get("fat") or 0, 0),
        "У " + _fmt(macros.get("carbs") or 0, 0),
    ]
    return "%s — %s" % (head, ", ".join(parts))


def day_totals(conn: Any, day: str) -> Dict[str, float]:
    """Сумма КБЖУ за день по ``food_log`` (та же таблица, что читает API)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(kcal), 0) AS kcal, COALESCE(SUM(protein), 0) AS protein, "
        "COALESCE(SUM(fat), 0) AS fat, COALESCE(SUM(carbs), 0) AS carbs, "
        "COALESCE(SUM(fiber), 0) AS fiber, COALESCE(SUM(sugar), 0) AS sugar "
        "FROM food_log WHERE date = ?",
        (day,),
    ).fetchone()
    if row is None:
        return {macro: 0.0 for macro in MACROS}
    return {macro: _round(row[macro], 1) for macro in MACROS}


def day_targets(conn: Any) -> Dict[str, float]:
    """Нормы из ``nutrition_targets``; на пустой базе — дефолты миграции 4."""
    try:
        row = conn.execute("SELECT * FROM nutrition_targets WHERE id = 1").fetchone()
    except Exception:  # pragma: no cover - старая база без таблицы норм
        row = None
    if row is None:
        return dict(DEFAULT_TARGETS)
    targets = dict(DEFAULT_TARGETS)
    for macro in DEFAULT_TARGETS:
        try:
            value = float(row[macro] or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            targets[macro] = value
    return targets


def _day_line(day: str, totals: Mapping[str, float], targets: Mapping[str, float]) -> List[str]:
    """Две строки: «сколько из нормы» и остаток (или перебор) до нормы."""
    lines = [
        "%s: %s из %s ккал, Б %s/%s, Ж %s/%s, У %s/%s."
        % (
            _day_label(day).capitalize(),
            _fmt(totals.get("kcal")),
            _fmt(targets.get("kcal")),
            _fmt(totals.get("protein")),
            _fmt(targets.get("protein")),
            _fmt(totals.get("fat")),
            _fmt(targets.get("fat")),
            _fmt(totals.get("carbs")),
            _fmt(targets.get("carbs")),
        )
    ]
    left = {macro: targets.get(macro, 0.0) - totals.get(macro, 0.0) for macro in DEFAULT_TARGETS}
    if all(value >= 0 for value in left.values()):
        lines.append(
            "Осталось: %s ккал, Б %s г, Ж %s г, У %s г."
            % (_fmt(left["kcal"]), _fmt(left["protein"]), _fmt(left["fat"]), _fmt(left["carbs"]))
        )
    else:
        over = [
            "%s %s" % (label, _fmt(-left[macro]))
            for macro, label in (("kcal", "ккал"), ("protein", "Б"), ("fat", "Ж"), ("carbs", "У"))
            if left[macro] < 0
        ]
        lines.append("Перебор: %s." % ", ".join(over))
    return lines


def log_food(
    conn: Any,
    settings: Any,
    parsed: Mapping[str, Any],
    *,
    now: Any = None,
    gateway: Any = None,
    today: Optional[str] = None,
) -> FoodResult:
    """Пишет еду в ``food_log`` и собирает короткий ответ.

    ``parsed`` — разобранная фраза::

        {"meal": "lunch", "items": [{"name": "гречка", "grams": 200}, ...],
         "kcal": 600, "protein": 40}          # kcal/белки — если продукта нет

    Что попадает в базу: по строке на каждую позицию с явными граммами
    (продукт из справочника или оценка), и одна строка на явные калории.
    Позиции без граммов не пишутся — о них бот спрашивает в ответе.
    """
    moment = now or _now(settings)
    day = str(today or parsed.get("date") or moment.date().isoformat())[:10]
    meal = str(parsed.get("meal") or "").strip()
    if meal not in MEALS:
        meal = _meal_by_hour(moment.hour)

    catalog = load_catalog(conn)
    written: List[Dict[str, Any]] = []
    operations: List[Dict[str, Any]] = []
    estimated_items: List[Dict[str, Any]] = []
    unknown_items: List[str] = []
    bad_grams: List[str] = []
    missing: List[str] = []
    labels: List[str] = []

    for raw in _items_of(parsed):
        name = sanity_name(raw.get("name"))
        grams = sanity_grams(raw.get("grams"))
        if name is None:
            continue
        if grams is None:
            raw_grams = raw.get("grams")
            try:
                too_much = float(raw_grams) > MAX_GRAMS
            except (TypeError, ValueError):
                too_much = False
            if too_much:
                bad_grams.append("%s %s г" % (name, _fmt(raw_grams, 0)))
            else:
                missing.append(name)
            continue
        product = match_product(name, catalog)
        if product is not None and sanity_macros(product) is None:
            # Справочник с ерундой (например, 2000 ккал на 100 г) не доверяем:
            # путь как у неизвестного продукта — оценка или честные граммы.
            logger.warning("продукт %r: КБЖУ вне пределов, не доверяю", product["name"])
            product = None
        is_estimated = False
        is_unknown = False
        estimated_per100: Optional[Dict[str, float]] = None
        if product is not None:
            macros = macros_for(product, grams)
            source = "telegram"
            title = str(product["name"])
            product_id: Optional[int] = int(product["id"])
        else:
            per100 = estimate_macros(gateway, name)
            source = "telegram:llm"
            title = name
            product_id = None
            if per100 is None:
                # Продукта нет и оценить некому: граммы всё равно записываем
                # (владелец продиктовал — не теряем), КБЖУ честно не считаем.
                macros = {macro: 0.0 for macro in MACROS}
                source = "telegram:grams"
                is_unknown = True
                unknown_items.append(title)
            else:
                macros = {
                    macro: _round(per100.get(macro) or 0, 2) * grams / 100.0
                    for macro in MACROS
                }
                is_estimated = True
                estimated_items.append(
                    {"name": title, "grams": grams, "per100": per100, "macros": macros}
                )
                estimated_per100 = per100
        food_id = conn.execute(
            "INSERT INTO food_log (date, ts, meal, product_id, text, grams, kcal, "
            "protein, fat, carbs, fiber, sugar, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                day,
                _iso(moment),
                meal,
                product_id,
                ("%s %s г" % (title, _fmt(grams))) if grams else title,
                grams,
                _round(macros["kcal"], 1),
                _round(macros["protein"], 1),
                _round(macros["fat"], 1),
                _round(macros["carbs"], 1),
                _round(macros["fiber"], 1),
                _round(macros["sugar"], 1),
                source,
            ),
        ).lastrowid
        operations.append({"type": "food", "id": int(food_id or 0)})
        written.append(
            {
                "name": title,
                "grams": grams,
                "macros": macros,
                "estimated": is_estimated,
                "unknown": is_unknown,
                "per100": estimated_per100,
                "food_id": int(food_id or 0),
            }
        )
        labels.append("%s %s г" % (title, _fmt(grams)))

    # Явные калории без продукта: «съел 600 ккал и 40 белка», «перекус 200 ккал».
    explicit = {macro: _round(parsed.get(macro), 1) for macro in ("kcal", "protein", "fat", "carbs")}
    explicit = {macro: value for macro, value in explicit.items() if value > 0}
    if explicit and not written:
        kcal = explicit.get("kcal") or 0.0
        if kcal <= MAX_KCAL_PER_100 * 4:
            food_id = conn.execute(
                "INSERT INTO food_log (date, ts, meal, product_id, text, grams, kcal, "
                "protein, fat, carbs, fiber, sugar, source) "
                "VALUES (?, ?, ?, NULL, ?, 0, ?, ?, ?, ?, 0, 0, 'telegram:free')",
                (
                    day,
                    _iso(moment),
                    meal,
                    str(parsed.get("text") or "")[:200],
                    kcal,
                    explicit.get("protein", 0.0),
                    explicit.get("fat", 0.0),
                    explicit.get("carbs", 0.0),
                ),
            ).lastrowid
            operations.append({"type": "food", "id": int(food_id or 0)})
            written.append(
                {
                    "name": "",
                    "grams": 0,
                    "macros": {
                        "kcal": kcal,
                        "protein": explicit.get("protein", 0.0),
                        "fat": explicit.get("fat", 0.0),
                        "carbs": explicit.get("carbs", 0.0),
                    },
                    "estimated": False,
                    "food_id": int(food_id or 0),
                }
            )
            labels.append("%s ккал" % _fmt(kcal))
        else:
            missing.append("калории")

    if not written:
        if bad_grams:
            return FoodResult(
                text="Не записал: %s — граммы должны быть от 1 до %d."
                % (", ".join(bad_grams[:3]), int(MAX_GRAMS)),
                missing=missing,
            )
        return FoodResult(
            text=_ask_grams(missing) if missing else "Что съел? Напиши: «гречка 200 г и курица 150».",
            missing=missing,
        )

    # Кнопки «в справочник / только калории» нужны и для оценённых позиций,
    # и для тех, что в справочнике не нашлись: владелец сам назовёт КБЖУ.
    pending = {
        "items": [
            {
                "name": item["name"],
                "grams": item["grams"],
                "per100": item["per100"],
                "ids": [item["food_id"]],
            }
            for item in written
            if item.get("name") and (item.get("estimated") or item.get("unknown"))
        ]
    }
    if pending["items"]:
        save_pending_estimate(conn, pending)

    totals = day_totals(conn, day)
    targets = day_targets(conn)
    label_map = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин", "snack": "Перекус"}

    pieces = []
    for item in written:
        if item["name"]:
            pieces.append(
                _item_line(
                    item,
                    estimated=bool(item.get("estimated")),
                    unknown=bool(item.get("unknown")),
                )
            )
        else:
            macros = item["macros"]
            pieces.append(
                "%s ккал, Б %s · Ж %s · У %s"
                % (
                    _fmt(macros.get("kcal")),
                    _fmt(macros.get("protein")),
                    _fmt(macros.get("fat")),
                    _fmt(macros.get("carbs")),
                )
            )
    if all(not item["name"] for item in written):
        # Только калории без продукта: тип записи должен быть виден в ответе.
        header = "Еда, %s: %s" % (label_map.get(meal, "приём").lower(), "; ".join(pieces))
    else:
        header = label_map.get(meal, "Еда") + ": " + "; ".join(pieces)
    lines = [header] + _day_line(day, totals, targets)

    notes: List[str] = []
    if estimated_items:
        names = ", ".join(item["name"] for item in estimated_items)
        notes.append("Примерно (не из справочника): %s." % names)
    if unknown_items:
        names = ", ".join(unknown_items)
        notes.append(
            "КБЖУ не посчитал: %s — нет в справочнике. Скажи КБЖУ на 100 г кнопкой "
            "«Добавить в справочник» или только калории порции." % names
        )
    if bad_grams:
        notes.append(
            "Проверь граммы: %s — записываю от 1 до %d г."
            % (", ".join(bad_grams[:3]), int(MAX_GRAMS))
        )
    if missing:
        notes.append(
            "Без граммов: %s — напиши, например «%s 150 г»." % (", ".join(missing), missing[0])
        )
    if notes:
        lines.extend(notes)

    buttons: List[List[Tuple[str, str]]] = []
    if pending["items"]:
        buttons.append(
            [
                ("Добавить в справочник", "v4:food:save"),
                ("Только калории", "v4:food:kcal"),
            ]
        )
    return FoodResult(
        text="\n".join(lines),
        operations=operations,
        label="еду «%s»" % ", ".join(labels[:3]),
        edit_hint="Пришли новую граммовку — старую запись удали кнопкой.",
        buttons=buttons,
        estimated=[item["name"] for item in estimated_items],
        missing=missing,
        kcal=_round(sum(item["macros"].get("kcal") or 0 for item in written), 1),
        macros={
            macro: _round(sum(item["macros"].get(macro) or 0 for item in written), 1)
            for macro in ("protein", "fat", "carbs")
        },
    )


def _items_of(parsed: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Позиции из разобранной фразы: ``items`` или ``food_items``."""
    raw = parsed.get("items")
    if raw is None:
        raw = parsed.get("food_items")
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, Mapping)):
        return []
    items: List[Mapping[str, Any]] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            items.append(entry)
        elif isinstance(entry, str):
            items.append({"name": entry, "grams": parsed.get("grams_total") or 0})
    return items[:12]


def _ask_grams(missing: Sequence[str]) -> str:
    if not missing:
        return "Что съел? Напиши: «гречка 200 г и курица 150»."
    return "Сколько граммов: %s? Напиши, например «%s 150 г»." % (
        ", ".join(missing[:3]),
        missing[0],
    )


def _day_label(day: str) -> str:
    """Слово дня для ответа: «сегодня», «вчера» или дата."""
    from datetime import date, timedelta

    try:
        value = date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        return str(day)
    today = date.today()
    if value == today:
        return "сегодня"
    if value == today - timedelta(days=1):
        return "вчера"
    if value == today + timedelta(days=1):
        return "завтра"
    return value.strftime("%d.%m")


def _meal_by_hour(hour: int) -> str:
    if hour < 11:
        return "breakfast"
    if hour < 16:
        return "lunch"
    if hour < 21:
        return "dinner"
    return "snack"


def _now(settings: Any = None) -> Any:
    from datetime import datetime

    try:
        return settings.now()
    except Exception:
        return datetime.now().astimezone()


def _iso(moment: Any) -> str:
    try:
        return moment.isoformat(timespec="seconds")
    except (AttributeError, ValueError):
        return str(moment)


def merge_extras(base: Mapping[str, Any], extra: Mapping[str, Any]) -> Dict[str, Any]:
    """Склеивает разбор LLM с разбором правил, не теряя позиции еды.

    Нужен страховкой: модель может назвать фразу мыслью или делом, но если
    правила видят «продукт + граммы», еда важнее — иначе владелец теряет
    запись, которую только что продиктовал.
    """
    merged = dict(base)
    items = extra.get("items")
    if items:
        merged["items"] = list(items)
        merged["meal"] = str(extra.get("meal") or merged.get("meal") or "")
        for macro in ("kcal", "protein", "fat", "carbs", "fiber", "sugar", "grams_total"):
            if extra.get(macro):
                merged[macro] = extra[macro]
    return merged
