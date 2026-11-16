# ARCHITECTURE_MAP — Life OS

**Дата:** 2026-11-16 · **Стек:** Python 3.9+ stdlib, SQLite, Telegram Bot API, Obsidian Markdown,
self-contained HTML. **Один процесс, один поток, один цикл.**

---

## 1. Контекст системы

```mermaid
flowchart LR
    U["Пользователь<br/>Telegram на iPhone/Mac"] -->|"сообщения"| TG["Telegram Bot API<br/>api.telegram.org"]
    TG -->|"getUpdates (long poll 20 c)"| A["Агент Life OS<br/>python3 src/main.py"]
    A -->|"sendMessage"| TG
    A -->|"опционально"| LLM["SAMP gateway<br/>api.samp.team/v1"]
    A --> DB[("SQLite<br/>data/lifeos.db")]
    A --> V["Obsidian vault<br/>01 Daily, Проекты/Life OS"]
    A --> D["dashboard/index.html<br/>+ data.json"]
    U -->|"двойной клик"| D
    L["launchd<br/>com.lifeos.agent"] -->|"KeepAlive + RunAtLoad"| A
```

Внешние зависимости: Telegram (обязательно), SAMP (опционально). Всё остальное — локально.

## 2. Модули

| Файл | Строк | Владелец | Контракт | Роль |
|---|--:|---|---|---|
| `src/__init__.py` | 11 | Lead | — | пакет, `__version__ = "1.0.0"` (:11) — **расхождение** с `VERSION` = 0.1.0 (D-13) |
| `src/config.py` | 286 | bot-core | §1 | поиск/слияние/валидация конфига, `resolve_path`, `project_root` |
| `src/telegram.py` | 235 | bot-core | §2 | Bot API на urllib: `call/send/get_updates`, split 4000, `TelegramError` |
| `src/storage.py` | 397 | bot-core | §3 | SQLite: messages, entries, goals, habits, kv; WAL, `busy_timeout` |
| `src/scheduler.py` | 1320 | bot-core | §8 | слоты, сессии, команды, свободный чат, запись в vault/dashboard, `write_mirror` |
| `src/llm.py` | 360 | brain | §4 | OpenAI-совместимый клиент SAMP, `User-Agent`, 429-retry, «никогда не бросает» |
| `src/prompts.py` | 471 | brain | §5 | личность, приветствия, вопросы по слотам, follow-up, JSON-разбор, `weekly_prompt` |
| `src/parsing.py` | 700 | brain | §6 | ответ → entry: LLM-JSON или эвристика; `heuristic_metrics` |
| `src/obsidian.py` | 1048 | vault | §7 | daily из шаблона, маркеры, цели между маркерами, mirror, `sync_pending` |
| `src/dashboard_data.py` | 927 | dash | §9 | `collect_data` + `build_dashboard`, вставка JSON в шаблон |
| `src/main.py` | 466 | bot-core | §10 | CLI, цикл, pid-лок, логи, SIGTERM |
| `src/mock_telegram.py` | 495 | qa | §11 | мок Bot API для тестов и e2e |
| `dashboard/template.html` | 1 600+ | Lead, затем правка | — | дизайн, CSS-токены, SVG-графики, `window.LIFEOS_DATA` (:600); переписан с 16:44 |
| `install.sh` / `uninstall.sh` | 739 / 229 | ops | — | установка, launchd, права, подхват ключа |
| `ops/com.lifeos.agent.plist` | 37 | ops | — | LaunchAgent: RunAtLoad, KeepAlive, WorkingDirectory |

Запуск: `python3 src/main.py` из корня (main.py добавляет `project_root` в `sys.path`);
альтернативно `python3 -m src.main`. Импорты — `from src import …`.

## 3. Поток входящего сообщения

```mermaid
sequenceDiagram
    participant T as Telegram
    participant M as main.py
    participant S as scheduler.py
    participant P as parsing.py
    participant L as llm.py
    participant DB as storage.py
    participant V as obsidian.py
    participant D as dashboard_data.py

    M->>T: getUpdates(offset, timeout=20)
    T-->>M: [update]
    M->>S: on_message(chat_id, text, now)
    alt команда
        S->>DB: kv / entries / goals
        S-->>T: sendMessage(ответ)
    else активная сессия
        S->>P: parse_answer(llm, slot, question, answer, now)
        P->>L: json_chat (если доступен)
        L-->>P: JSON или None
        P-->>S: entry {category,title,text,metrics}
        S->>DB: add_entry(entry)
        S->>V: write_entry(entry) → written=1
        S->>D: build_dashboard(cfg, storage)
        S->>V: write_mirror(all_entries)
        S-->>T: следующий вопрос / уточнение / закрытие
    else свободный чат
        S->>L: chat(system_prompt + history)
        S->>P: parse_answer(...)
        S->>DB: add_entry
        S->>V: write_entry / write_mirror
        S-->>T: человеческий ответ + сохранение
    end
    M->>S: tick(now) после каждого getUpdates — простой ≈ 20 c, при трафике ≈ 1 c
```

## 4. Поток слота (утро/день/вечер)

```mermaid
flowchart TD
    TICK["tick(now)"] --> PAUSE{"пауза?"}
    PAUSE -->|"да"| END["выход"]
    PAUSE -->|"нет"| DUE{"время слота пришло<br/>и сегодня не запускали?"}
    DUE -->|"нет"| LATE{"опоздали ≤ 180 мин?<br/>scheduler.py:352"}
    LATE -->|"нет"| END
    LATE -->|"да"| START
    DUE -->|"да"| START["старт сессии:<br/>kv['session'] = {slot, idx, step, ...}"]
    START --> Q["вопрос N: prompts.scripted_questions"]
    Q --> ANS{"ответ пользователя"}
    ANS -->|"короткий + требует деталей"| FU["follow-up через LLM<br/>≤ max_followups (2)"]
    FU --> Q
    ANS -->|"-/пропустить"| NEXT["idx += 1"]
    ANS -->|"стоп"| CLOSE["закрыть сессию"]
    ANS -->|"обычный"| SAVE["parse → add_entry → vault → dashboard"]
    SAVE --> NEXT
    NEXT -->|"есть ещё"| Q
    NEXT -->|"все"| DONE["закрытие + лог last_slot:<slot>=date"]
```

Состояние сессии переживает рестарт: лежит в `kv["session"]`, TTL 12 ч
(`src/scheduler.py:510`), просроченная сессия снимается на следующем тике.

## 5. Данные

| Хранилище | Путь | Содержимое | Права |
|---|---|---|---|
| SQLite | `data/lifeos.db` (прод: `~/Library/Application Support/LifeOS/data/lifeos.db`) | `messages`, `entries` (с `metrics` JSON и `written`), `goals`, `habits`, `kv` | 600 (каталог пользователя) |
| Дневник | `<vault>/01 Daily/YYYY-MM-DD.md` | разделы `## Еда (агент)`, `## Тренировка (агент)`, `## Задачи (агент)`, `## Цели (агент)`, `## Мысли (агент)`, `## Тело (агент)`; маркеры `<!-- lifeos:entry:ID -->` | наследует vault |
| Цели | `<vault>/Проекты/Life OS/Цели.md` | таблица между `<!-- lifeos:goals:start -->` / `end` | наследует vault |
| Зеркало | `<vault>/Life OS/data/entries.jsonl`, `goals.json` | все записи и цели в машинном виде (для Dataview/скриптов) | наследует vault |
| Дашборд | `dashboard/index.html` + `data.json` | HTML с встроенным `window.LIFEOS_DATA` и копия данных | 644 |
| Логи | `data/logs/agent.log` (handler `lifeos`, 5 МБ × 3) + `~/Library/Logs/LifeOS/agent.log` (stdout launchd) | события `lifeos`; логи `src.*` (llm/parsing/dashboard) в файл не попадают — дефект P1-4 | 600/644 |
| PID | `data/lifeos.pid` | pid живого процесса | 600 |

Схема SQLite — `ТЗ.md` §5. Время — ISO с оффсетом `+03:00`; сравнение — по локальной дате.

## 6. Секреты и доверие

| Секрет | Где лежит | Кто читает | Защита |
|---|---|---|---|
| `telegram.token` | `config/config.json` (прод — `~/Library/Application Support/LifeOS/config.json`) | `telegram.py` | chmod 600 (`install.sh:398,476`), не логируется, скрыт в `repr` (`tests/test_telegram.py`) |
| `llm.api_key` | там же; источник — `DDD_API_KEY` из `~/.dsh/.credentials.yaml` | `llm.py` | 600; в лог только код ошибки; `install.sh` не печатает ключ |
| `chat_id` | конфиг | `scheduler.py` | не секрет, но ограничивает доступ: чужие сообщения игнорируются |

Источник `DDD_API_KEY` — `~/.dsh/.credentials.yaml` (`refs: { DDD_API_KEY: sk-... }`),
подхватывается установщиком, в репозиторий не попадает.

## 7. Развёртывание

```mermaid
flowchart TD
    REPO["Проект<br/>~/Documents/…/telegram-life-agent"] -->|"install.sh"| APP["~/Library/Application Support/LifeOS<br/>(src, dashboard, config, README)"]
    APP --> CFG["config/config.json (600)"]
    APP --> PL["~/Library/LaunchAgents/com.lifeos.agent.plist"]
    PL -->|"launchctl bootstrap gui/$UID"| PROC["python3 src/main.py<br/>cwd = APP"]
    PROC --> LOGS["~/Library/Logs/LifeOS/agent.log"]
    PROC --> DBD["APP/data/lifeos.db"]
    PROC --> DASH["APP/dashboard/index.html"]
    PROC --> VAULT["Obsidian vault<br/>(вне APP, доступ по TCC)"]
```

`uninstall.sh` снимает LaunchAgent и сохраняет данные; `--purge` удаляет APP и логи.

## 8. Точки отказа и поведение

| Отказ | Проявление | Что делает система | Где |
|---|---|---|---|
| Telegram 401 | неверный токен | `TelegramError(401)`, сообщение в лог, бот не падает | `telegram.py` |
| Telegram 409 | два процесса | понятная ошибка + pid-лок не даёт второму стартовать | `main.py:65-97` |
| Telegram 429 | флуд | `retry_after`, один повтор в `scheduler._send` | `scheduler.py` |
| LLM 403/429/5xx | нет уточнений | `None` → скриптованные вопросы, данные пишутся | `llm.py`, `parsing.py` |
| Vault недоступен (TCC) | «Operation not permitted» | `write_entry` → False, `written=0`, повтор при следующем синке | `obsidian.py` |
| Нет шаблона дашборда | нет HTML | `build_dashboard` → `None`, лог; демо не страдает | `dashboard_data.py` |
| Битая строка `metrics` | мусор в JSON | нормализация в `None`/0, без NaN | `dashboard_data.py` |
| Диск/права на лог | нет файла лога | fallback в stderr, процесс живёт | `main.py:130` |

### 8.1 Известные дефекты (из `REVIEW.md`, снимок 16:50)

| ID | Дефект | Где | Эффект |
|---|---|---|---|
| P1-1 | offset `getUpdates` живёт только в памяти | `main.py:399,440` | после рестарта пачка переотдаётся: дубли, сдвиг сессии |
| P1-2 | `--dry-run` подменяет только Telegram | `main.py:315-333,443-446` | пишет в рабочую БД/kv и vault, «съедает» слот дня |
| P1-3 | `Request` строится до `try` | `telegram.py:110-119` | URL с токеном уходит в лог |
| P1-4 | логи `src.*` мимо файлового handler | `main.py:109-118`, `llm.py:25`, `parsing.py:31` | файл лога не содержит ошибок LLM/parsing/dashboard |
| P2-10 | `_json_safe` вызывается без присваивания | `dashboard_data.py:212` | NaN может уронить сборку в `None` |
| P2-12 | `touch_habit` никто не вызывает | `storage.py:367` | карточка привычек всегда пустая |
| SEC P1-2 | KeepAlive + фатальная ошибка конфига | `ops/*.plist:22-24`, `main.py:273` | рестарт каждые ~10 с, ~26 тыс. строк лога в сутки |
| ADV D-02 | pid-лок TOCTOU | `main.py:73-94` | 7 из 12 процессов стартуют; дубли сессий 27/30 |
| ADV D-03 | LLM 2×timeout без circuit breaker | `scheduler.py:894-906` | до ~120 с на сообщение при timeout=60 |
| ADV D-04 | нет лимитов ответа/истории LLM | `scheduler.py:245-272` | 250k → 63 сообщения, 249 КБ в daily |
| ADV D-05 | UTF-16: символы вместо code units | `telegram.py:21,44-73` | 400 от Bot API, ответ не доставлен |
| ADV D-06 | `<` не экранируется в JSON | `dashboard_data.py:577-580` | страница теряет прикладной JS |
| ADV D-07 | служебные маркеры в тексте пользователя | `obsidian.py` | запись скрыта из vault, но `written=1` |

Карта актуальна по структуре; перечисленные дефекты — в коде, не в схеме. На 16:57–16:58
часть P1 уже правится (`telegram.py`, plist, README); до зелёного прогона таблицу считать
снимком 16:50 (`TEST_HISTORY.md` §1.1).

## 9. Границы модулей

- `scheduler` — единственный, кто знает про слоты и сессии; зависит от `parsing`, `prompts`,
  `storage`, `Vault`, `dash_build`, `LLM` (через интерфейсы, не напрямую от их внутренностей).
- `parsing` не пишет в БД и не знает про Telegram.
- `obsidian` не знает про Telegram и LLM.
- `dashboard_data` только читает `Storage`.
- `main` — композиционный корень: собирает объекты и крутит цикл; бизнес-логики не содержит.
- Циклов импортов нет: `config → (все)`, `parsing → prompts/llm`, `scheduler → parsing/prompts`,
  `main → все`; `prompts` не зависит ни от чего, кроме stdlib.
