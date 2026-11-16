# INTERFACES — контракты модулей (обязательны для всей команды)

Проект: `telegram-life-agent/` · Python 3 only stdlib · время `Europe/Moscow`.
Все пути в конфиге — относительно `project_root`, кроме абсолютных.
Любая функция/класс из этого файла — публичный контракт: сигнатуры менять нельзя,
внутренности свободны. Если контракт мешает — писать Lead, не менять молча.

## 0. Структура проекта (целевая)

```
telegram-life-agent/
├── ТЗ.md  PLAN.md  INTERFACES.md  PROJECT_CONTEXT.md  README.md
├── install.sh  uninstall.sh
├── config/config.example.json         # в git; config/config.json — локальный, 600
├── src/
│   ├── main.py          # bot-core
│   ├── config.py        # bot-core
│   ├── telegram.py      # bot-core
│   ├── storage.py       # bot-core
│   ├── goals_math.py    # bot-core — единый прогресс целей (§15)
│   ├── scheduler.py     # bot-core
│   ├── llm.py           # brain
│   ├── prompts.py       # brain
│   ├── parsing.py       # brain
│   ├── obsidian.py      # vault
│   ├── dashboard_data.py# dash
│   └── mock_telegram.py # qa (тестовый сервер)
├── dashboard/
│   ├── template.html    # dash
│   └── (сборка: index.html, data.json — генерируются)
├── tests/               # qa
│   ├── test_config.py test_storage.py test_telegram.py test_scheduler.py
│   ├── test_obsidian.py test_parsing.py test_dashboard.py
│   ├── test_goals_math.py test_install.py
│   └── e2e_mock.py
├── ops/com.lifeos.agent.plist
└── data/ logs/ (генерируется)
```

## 1. config.py

```python
DEFAULTS: dict  # базовые значения, см. ТЗ §6

def load_config(path: str | None = None) -> dict:
    """Читает JSON (по умолчанию $LIFEOS_CONFIG или <root>/config/config.json,
    иначе config/config.example.json). Дополняет DEFAULTS рекурсивно.
    Поднимает ConfigError, если нет token/chat при require_live=True? — нет:
    только валидация типов; живой запуск проверяет main.py.
    Возвращает dict с ключами ТЗ §6 + computed:
      cfg["_root"] = абсолютный путь проекта (родитель src/)."""

class ConfigError(Exception): ...
```

## 2. telegram.py

```python
class TelegramError(Exception):
    def __init__(self, message: str, status: int | None = None,
                 retry_after: float | None = None): ...

class Telegram:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org"): ...
    def call(self, method: str, payload: dict | None = None,
             timeout: int = 60) -> dict:
        """POST JSON-телом (Bot API это принимает). Возвращает result. 429 → TelegramError
        c retry_after; 401/403 → TelegramError status; сеть → TelegramError.
        Лимит длины текста — 4000 UTF-16 units (не code points!)."""
    def send(self, chat_id: int, text: str) -> None
        # режет текст >4000 символов на части по 4000 (по границам строк)
    def get_updates(self, offset: int | None,
                    timeout: int = 20) -> list[dict]
        # POST getUpdates {offset, timeout, allowed_updates:["message"]};
        # возвращает result list (пустой при таймауте)
```

## 3. storage.py

```python
class Storage:
    def __init__(self, db_path: str): ...     # создаёт файл и схему, WAL
    def close(self) -> None: ...

    # сообщения
    def record_message(self, chat_id: int, direction: str, text: str,
                       slot: str | None = None) -> int
    def chat_history(self, limit: int = 20) -> list[dict]
        # [{"direction","text","ts","slot"}], старые→новые

    # записи
    def add_entry(self, entry: dict) -> int
        # entry: {"date","ts","slot","category","title","text","raw","metrics"}
        # metrics — dict; сохраняется как JSON-строка. Возвращает id.
    def entries(self, date_from: str, date_to: str) -> list[dict]  # date YYYY-MM-DD
    def entries_unwritten(self) -> list[dict]
    def mark_written(self, ids: list[int]) -> None
    def set_entry_written(self, entry_id: int, value: bool = True) -> None

    # kv (состояние)
    def kv_get(self, key: str, default: str | None = None) -> str | None
    def kv_set(self, key: str, value: str) -> None
    def kv_del(self, key: str) -> None

    # цели
    def goals(self) -> list[dict]
        # [{"id","title","target","current","start","direction","deadline",
        #   "status","note","created","updated"}] — все, новые сначала.
        # "start" — стартовая точка цели (текущее значение в момент создания),
        # "direction" — "up"/"down"; по ним :mod:`src.goals_math` считает прогресс
        # одинаково в боте, дашборде и «Цели.md» (§15).
    def get_goal(self, goal_id: int) -> dict | None
    def upsert_goal(self, goal: dict) -> int     # без id — вставка; с id — update
        # При update достаточно только меняющихся полей: upsert_goal({"id": 5,
        # "current": "7"}). "title" пустым не затирается.
    def delete_goal(self, goal_id: int) -> None

    # привычки (факультативно, дашборд)
    def habits(self) -> list[dict]
        # [{"name","streak","last_done","total"}], порядок добавления;
        # заполняется командой бота «/habit <название>» (§8)
    def touch_habit(self, name: str, date: str) -> None
        # отметить за date (YYYY-MM-DD); повторный вызов за ту же дату
        # ничего не меняет (идемпотентно); streak растёт только за подряд
        # идущие дни, иначе начинается с 1
```

**Схема таблицы `goals`** (SQLite, актуальная):

```sql
CREATE TABLE goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    target TEXT,
    current TEXT,
    start TEXT,          -- стартовая точка: текущее значение в момент создания
    direction TEXT,      -- "up" | "down"; пусто — определить по названию (§15)
    deadline TEXT,
    status TEXT DEFAULT 'active',
    note TEXT,
    created TEXT,
    updated TEXT
);
```

**Миграция старых БД.** Таблица `goals` в текущей схеме содержит `start TEXT`
и `direction TEXT`. Обе колонки появились позже первой версии схемы, поэтому
`Storage.__init__` после `CREATE TABLE IF NOT EXISTS` смотрит
`PRAGMA table_info(goals)` и добавляет недостающие через
`ALTER TABLE goals ADD COLUMN …` (`_GOAL_MIGRATIONS`, идемпотентно). Старая БД
открывается без ручных действий; у целей, созданных до миграции, `start` и
`direction` пустые — прогресс для них считается по формуле без старта (§15).

## 4. llm.py

```python
class LLM:
    def __init__(self, cfg: dict): ...   # cfg — весь конфиг; берёт cfg["llm"]
    def available(self) -> bool:         # enabled + непустой api_key
    def chat(self, messages: list[dict], temperature: float = 0.7,
             max_tokens: int = 900) -> str | None:
        """POST {base_url}/chat/completions. None при любой ошибке/таймауте/
        пустом ответе; никогда не бросает. Логирование — только код ошибки."""
    def json_chat(self, messages: list[dict], max_tokens: int = 900) -> dict | None:
        """chat + вытащить первый {...} из ответа и json.loads; None при ошибке."""
```

## 5. prompts.py (brain)

```python
SLOT_LABELS = {"morning": "утро", "midday": "день", "evening": "вечер",
               "weekly": "неделя"}

def system_prompt() -> str:
    """Личность агента: тёплый, конкретный, ведёт подробный разговор,
    русский язык, не льстит, не читает мораль, 1–2 предложения в ответе."""

def greeting(slot: str, name: str = "") -> str
def scripted_questions(slot: str) -> list[str]
    # morning: план/3 задачи/цели; midday: еда/вода/энергия; evening:
    # тренировка/задачи/еда итог/настроение/мысли; weekly: итоги недели

def followup_messages(slot: str, question: str, answer: str,
                      history: list[dict]) -> list[dict]
    # messages для LLM: решает, нужен ли уточняющий вопрос; ответ — текст
    # вопроса или "OK" (без вопроса)

def parse_messages(slot: str, question: str, answer: str) -> list[dict]
    # messages для LLM: вернуть строго JSON: {"category","title","text","metrics"}

def weekly_prompt(stats: dict) -> str
```

## 6. parsing.py (brain)

```python
CATEGORIES = {"tasks","food","training","goals","mood","journal",
              "sleep","water","other"}

def parse_answer(llm: LLM, slot: str, question: str, answer: str,
                 now_iso: str) -> dict:
    """Возвращает entry-подобный dict:
    {"date":"YYYY-MM-DD","ts":now_iso,"slot":slot,"category":...,
     "title":"коротко (≤80)","text":"человекочитаемая запись",
     "raw":answer,"metrics":{...}}
    LLM недоступен/ошибка → эвристика: категория по тексту вопроса/ключевым словам,
    title из первой строки, metrics — числа с единицами, вытащенные regex-эвристикой
    («2 000 мл» → 2000; «встреча 30 минут» не тренировка; «час за компом» не сон).
    Не бросает."""

def heuristic_metrics(text: str) -> dict:
    """достаёт числа: 2000 мл, 8 часов, 80 кг, 3 км, 45 мин → известные ключи"""
```

## 7. obsidian.py (vault)

```python
class Vault:
    def __init__(self, cfg: dict, storage=None): ...
        # cfg["obsidian"]; storage — Storage для goals/mirror (не обязателен)

    def ensure_daily(self, date: str) -> Path
        # создаёт daily-заметку из template_path (или каркас, если шаблона нет);
        # заменяет {{date:DD.MM.YYYY}}, {{date}}, {{title}}
    def write_entry(self, entry: dict) -> bool
        # entry с "id" (из БД): блок в раздел по category в daily note.
        # Маркер <!-- lifeos:entry:ID -->; если маркер уже есть — False.
        # Разделы: food→"Еда (агент)", training→"Тренировка (агент)",
        # tasks|goals→"Задачи (агент)"/"Цели (агент)", mood|journal|other→
        # "Мысли (агент)", sleep|water→"Тело (агент)". Раздел создаётся один раз,
        # новые пункты дописываются в его конец (перед следующим "## ").
    def rewrite_goals(self) -> bool
        # цели из storage → "Проекты/Life OS/Цели.md" между маркерами
        # <!-- lifeos:goals:start --> / <!-- lifeos:goals:end -->;
        # таблица: Цель | Прогресс | Срок | Статус. Сохраняет прочий текст файла.
    def write_mirror(self, entries: list[dict]) -> None
        # "Life OS/data/entries.jsonl" — перезапись целиком (по всем entry),
        # "Life OS/data/goals.json"; off при write_mirror=false.
    def sync_pending(self) -> int
        # entries_unwritten → write_entry → mark_written; возвращает число.
        # Ошибки → лог, не бросать. Всё под try/except.
```

## 8. scheduler.py (bot-core)

```python
class Scheduler:
    def __init__(self, cfg: dict, storage: Storage, tg, llm, vault, dash_build,
                 logger): ...
        # dash_build: callable(cfg, storage) -> str | None
    def tick(self, now: datetime) -> None
        # 1) если не пауза и время слота пришло (и ещё не запускали сегодня) —
        #    начать сессию; 2) завершить/напомнить просроченные;
        # 3) раз в день — rollover (dashboard refresh, cleanup)
    def on_message(self, chat_id: int, text: str, now: datetime) -> None
        # команды → ответ; иначе — диалог: сессия (ответ на текущий вопрос,
        # follow-up, следующий вопрос) или свободный чат (LLM-ответ + парсинг
        # и сохранение entry). Всегда отвечает пользователю.
        # Команды: /start /status /today /report /goals /add_goal /habit
        # /habits /skip /pause /resume /дизайн /stop /help (+ русские алиасы
        # в _COMMAND_ALIASES; help_text() — источник правды для README).
        #   /habit <название> — storage.touch_habit(название, сегодня), ответ
        #       с серией; сразу пересобирает дашборд (привычка видна там же).
        #   /habits — storage.habits() списком: серия, «сегодня ✅»,
        #       последняя дата.
    def status_text(self) -> str
    def today_text(self) -> str
    def report_text(self) -> str
    def goals_text(self) -> str
    def habits_text(self) -> str

# состояние сессии в kv: ключ "session" → JSON
# {"slot","idx","step":"question|followup|done","followups":n,
#  "question","started_at","date","answered":[...]}
```

## 9. dashboard_data.py (dash)

```python
def build_dashboard(cfg: dict, storage: Storage) -> str | None:
    """Собирает данные → рендерит self-contained dashboard/index.html
    (данные встроены как JSON), пишет рядом data.json, возвращает путь.
    Ошибки → лог и None (никогда не бросает)."""

def collect_data(cfg: dict, storage: Storage, now: datetime | None = None) -> dict:
    """Схема данных (используется тестами):
    {"generated_at": ISO,
     "today": {"date","checkins":{"morning":bool,"midday":bool,"evening":bool},
               "entries": int},
     "kpis": {"streak_days","checkin_rate_week","training_days_week",
              "avg_sleep_h","mood_avg","tasks_done_week","tasks_planned_week",
              "water_avg_ml"},
     "series": {"dates":[14],"sleep_h":[...],"mood":[...],"energy":[...],
                "water_ml":[...],"calories":[...],"training":[0|1...]},
     "goals": [{"title","progress"(0..1),"deadline","status","link"}],
     "habits": [{"name","streak","last_done"}],
     "feed": [{"date","time","category","text"}],
     "heatmap": {"start","weeks":8,"cells":[{"date","count"}]}}
    """
    # "progress" цели — goals_math.goal_fraction(), тот же процент, что в боте
    # и в «Цели.md» (§15). "habits" — из storage.habits() (серия и последняя
    # отметка), карточка «Привычки» рисует серию и недельную полосу отметок.
```

## 10. main.py (bot-core)

```python
def main(argv: list[str] | None = None) -> int:
    """Аргументы: --dry-run (без Telegram, лог), --once (один тик и выход),
    --config PATH, --mock-url URL (подменить api_base для тестов).
    Цикл: get_updates(offset, 20) → on_message; tick(); sleep(1).
    Ловит всё; pid-лок data/lifeos.pid; SIGTERM → аккуратный выход.
    Возвращает 0."""
```

## 11. mock_telegram.py (qa)

```python
class MockTelegramServer:
    """HTTP-сервер на localhost: хранит updates; /bot<TOKEN>/getUpdates отдаёт
    очередь; sendMessage складывает в .sent; любые ошибки эмулируются флагом.
    API: start() -> port; stop(); push_update(text, chat_id); sent() -> list[str];
    wait_sent(timeout=5.0, min_count=1) -> list[str]"""
```

## 12. Формат записи (entry) — общий для всех модулей

```python
{
  "id": 12,                     # только для записей из БД
  "date": "2026-11-16",
  "ts": "2026-11-16T14:02:11+03:00",
  "slot": "midday",             # или "free"
  "category": "food",           # из CATEGORIES
  "title": "обед: рис с курицей",
  "text": "Обед — рис с курицей и салатом, ~600 ккал. Выпил 500 мл воды.",
  "raw": "рис с курицей и салат, 600 ккал, воды 500",
  "metrics": {"calories": 600, "water_ml": 500}
}
```

## 13. Obsidian: формат блоков

Daily note (дописывается):
```md
## Еда (агент)
- 14:02 — обед: рис с курицей; ~600 ккал <!-- lifeos:entry:12 -->
```

Цели:
```md
<!-- lifeos:goals:start -->
| Цель | Прогресс | Срок | Статус |
|---|--:|---|---|
| [[Цель 200к (БТ)]] | 60% | 2027-06-01 | активна |
<!-- lifeos:goals:end -->
```
Прогресс в этой таблице — `goals_math.goal_percent()`, ровно тот же процент,
что показывает бот (`/goals`) и полоса на дашборде (§15). Если у цели нет
чисел и процентов вовсе, вместо процента печатается текущее значение или «—».

## 14. Правила для команды

1. Пиши только файлы своего scope (см. задачи на доске), чужие не трогай.
2. Никаких внешних зависимостей (pip install запрещён).
3. Все публичные функции — по контракту выше; тесты гоняй локально.
4. Секреты (токен, LLM-ключ) в коде и логах не появляются.
5. Тесты/демо — в temp-каталогах, не в живой Obsidian-базе.
6. Готово → отчёт Lead: что сделано, как проверял, что осталось.

## 15. goals_math.py (bot-core) — единый прогресс целей

Модуль выделен позже: раньше формула прогресса была своя в боте, в дашборде
и в «Цели.md», и они расходились — цель «вес 92 → 78» показывала на дашборде
0%, а в боте 94,7%. Теперь расчёт один, а витрины только оборачивают его:

| Где | Обёртка | Что отдаёт |
|---|---|---|
| бот (`/goals`) | `Scheduler._goal_progress` | целые проценты 0…100 или `None` |
| дашборд (полоса) | `dashboard_data._goal_progress` | доля 0…1 |
| Obsidian (`Цели.md`) | `Vault._goal_progress` | строка `0%` / `50%` / `100%` |

```python
def goal_direction(goal: dict) -> str
    # "up" (больше — лучше, по умолчанию) или "down".
    # Поле goal["direction"] главнее названия: "down"/"вниз"/"снижение"/
    # "уменьшение"/"меньше"/"похудение"/"сбросить" → "down",
    # "up"/"вверх"/"рост"/"увеличение"/"больше" → "up".
    # Без поля — по названию: «вес», «похуд», «скинуть», «сбросить» → "down";
    # исключение «набрать вес»/«набор веса»/«поправиться» → "up".

def goal_start(goal: dict) -> float | None
    # Стартовая точка: goal["start"] (синонимы: start_value, initial,
    # baseline). None — цель создана до появления колонки start.

def goal_fraction(goal: dict) -> float
    # Прогресс 0..1, округлён до тысячных; не бросает никогда.
    # Приоритет источников: status «выполнена» → 1.0; goal["progress"]
    # (доля 0..1 или проценты «60%»/60) → как записано; иначе числа
    # current/target/start:
    #   рост:     (current - start) / (target - start)
    #   снижение: (start - current) / (start - target)
    # Без start: рост — current / target; снижение — 0%, пока current > target
    # (100% при current <= target): базовая точка неизвестна, и «почти 100%»
    # только из-за близости к цели было бы выдумкой.
    # Degenerate-случаи: start == target → формула без старта; target == 0 →
    # 100% только по нужную сторону нуля; bool/NaN/Infinity/мусор → 0.0.

def goal_percent(goal: dict) -> int | None
    # round(goal_fraction(goal) * 100) — целые проценты; None только когда
    # у цели нет ни чисел, ни процентов (тогда витрины не печатают «%»).
```

Инварианты (закреплены `tests/test_goals_math.py`): «start=92, current=85,
target=78, down» → 0.5 (50%); current == start → 0%; current == target → 1.0
(100%); цель с одним `target` → 0%, как и полоса на дашборде.
