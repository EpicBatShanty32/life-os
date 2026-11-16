# TEST_HISTORY — прогоны Life OS

**Окружение:** macOS 27.2.1, Mac 16 ГБ, Europe/Moscow (UTC+3).
`/usr/local/bin/python3` = 3.13.14 (основной), `/usr/bin/python3` = 3.9.6 (системный).
Тесты — `unittest`, только stdlib, живут в `tests/`, запускаются из корня проекта.

---

## 1. Сводный прогон (независимая проверка, 2026-11-16 16:39 MSK)

```
cd "/Users/user/Documents/deepseek-harness/default-workspace/telegram-life-agent"
python3 -m unittest discover tests
```

```
Ran 148 tests in 1.156s
OK
```

**Результат:** 148 тестов, 0 падений, 0 ошибок, 0 пропусков; 544 вызова `self.assert*`.
**Доказательство:** вывод выше; разбивка по модулям:

| Модуль | Тестов | Проверок (`self.assert*`) | Статус | Что покрыто |
|---|--:|--:|---|---|
| `tests/test_config.py` | 14 | 40 | OK | поиск конфига (path/env/local/example), deep-merge, типы, `chat_id` строкой/отрицательным, битый JSON |
| `tests/test_storage.py` | 12 | 61 | OK | схема + WAL, messages, entries, `written`, kv, goals CRUD, habits/streak, SQL-инъекция |
| `tests/test_telegram.py` | 18 | 42 | OK | 401/403/409/429/500, сеть, split 4000, offset, long poll, скрытие токена в repr |
| `tests/test_scheduler.py` | 39 | 115 | OK | слоты, догон, TTL, follow-up ≤2, команды + русские алиасы, свободный чат, чужой chat_id, ролловер, 429-retry |
| `tests/test_obsidian.py` | 18 | 75 | OK | daily из шаблона, идемпотентность маркера, сохранение текста, цели между маркерами, mirror, ошибки записи |
| `tests/test_parsing.py` | 17 | 56 | OK | эвристика (вода/сон/вес/км/мин/ккал), LLM-JSON, мусорный LLM → fallback, без исключений |
| `tests/test_dashboard.py` | 8 | 82 | OK | пустая БД, «странные» данные, heatmap, demo-БД, шаблон есть/нет/кастомный |
| `tests/test_mock_telegram.py` | 10 | 26 | OK | push/offset, long poll, sendMessage, режимы 429/409/500/401, stop идемпотентен |
| `tests/test_install.py` | 12 | 47 | OK | `bash -n`, `plutil -lint`, `--help`, README, пример конфига, права/структура |
| **Итого** | **148** | **544** | **OK** | |

`tests/_support.py` (189 строк) — общие фикстуры: temp-каталоги, temp-vault, конфиг.
**E2E-файла `tests/e2e_mock.py` нет** — см. §3.

---

## 2. Прогоны по агентам

Цифры «проверок» в отчётах агентов — самопроверки в их сессиях (скрипты/ассерты), протоколы
в проект не выложены. Ниже они отделены от того, что подтверждено unit-тестами в репозитории.

### 2.1 bot-core — смоуки (100+ проверок по отчёту; подтверждено 83 теста)

| Что | Команда | Результат |
|---|---|---|
| Конфиг | `python3 -m unittest discover -s tests -p test_config.py` | 14/14 OK |
| SQLite | `... -p test_storage.py` | 12/12 OK |
| Telegram-клиент | `... -p test_telegram.py` | 18/18 OK |
| Планировщик/диалог/команды | `... -p test_scheduler.py` | 39/39 OK |

Дополнительно: `python3 src/main.py --dry-run --once` (Lead, §2.5) — цикл стартует и
завершается штатно.

### 2.2 brain — 338 проверок по отчёту; подтверждено 17 тестов

`python3 -m unittest discover -s tests -p test_parsing.py` → 17/17 OK.

Покрыто: эвристические метрики (`water_ml`, `sleep_h`, `weight_kg`, `distance_km`,
`duration_min`, `calories`), различение «8 часов сна» и «8 часов тренировки», LLM-JSON
нормализация (категория из списка, `title` ≤80, только известные ключи), мусор от LLM →
fallback, «странные» входы не бросают.

### 2.3 vault — 136 проверок по отчёту; подтверждено 18 тестов

`python3 -m unittest discover -s tests -p test_obsidian.py` → 18/18 OK.

Покрыто: создание daily из шаблона с подстановкой даты, каркас без шаблона, существующий
файл пользователя не меняется, все категории → свои разделы, идемпотентность по маркеру
`<!-- lifeos:entry:ID -->`, цели между маркерами с сохранением прочего текста, mirror
вкл/выкл, `sync_pending` и `written=1`, недоступный vault → флаги не сбрасываются.

### 2.4 ops — симуляция установки

| Проверка | Команда | Результат |
|---|---|---|
| Синтаксис скриптов | `bash -n install.sh && bash -n uninstall.sh` | rc=0 |
| Плист валиден | `plutil -lint ops/com.lifeos.agent.plist` | OK (покрыто `test_install`) |
| План установки | `LIFEOS_APP_DIR=$T/App LIFEOS_LOG_DIR=$T/Logs LIFEOS_PLIST_DIR=$T/LaunchAgents bash install.sh --dry-run` | rc=0; в `$T` **не создано ни одного файла**; напечатан план: копирование, конфиг 600, plist, `launchctl bootstrap`, статус |
| Проверка Python | внутри `install.sh:162` | 3.13.14 принят, 3.9.6 тоже (порог 3.9) |
| Тесты установки | `... -p test_install.py` | 12/12 OK |

Реальная установка (`launchctl`) **не выполнялась** — нет токена, и это изменило бы систему
пользователя (§4).

### 2.5 Lead — import / data / vault

| Проверка | Команда | Результат |
|---|---|---|
| Импорт всех модулей | `python3 -c "from src import config, telegram, storage, scheduler, llm, prompts, parsing, obsidian, dashboard_data, mock_telegram, main"` | OK, версия `1.0.0` |
| Пустая БД → данные дашборда | `collect_data(cfg, Storage(tmp))` | словарь по схеме §9, без NaN |
| Сборка дашборда | `main.py --dry-run --once` с `paths.*` в temp | созданы `lifeos.db`, `dashboard/index.html`, `data.json`; rc=0 |
| Цели (дефект D-10) | `upsert_goal` + `collect_data` | «Похудеть до 78 кг» → **1.0**, «Пробежать 10 км» (3/10) → 0.3 |
| Живой vault | — | не писался: тесты и проверки только в temp (`tests/tmp`, `mktemp`) |

### 2.6 QA / e2e — по факту

| Проверка | Статус |
|---|---|
| Мок-сервер Telegram (`src/mock_telegram.py`, 495 строк) | готов, 10 тестов OK |
| Unit-набор 148 тестов | готов, OK |
| E2E-сценарий ТЗ §8.2 (`tests/e2e_mock.py`) | **не сдан**, файла нет |
| Повторный прогон без дублей записей | не проверялся e2e (идемпотентность покрыта unit-тестом vault) |

---

## 3. Что не проверено (честные пробелы)

1. **Живой Telegram**: токена нет → ни одного реального `getUpdates`/`sendMessage`.
2. **Живой LLM**: ключ SAMP в CI-прогоне не использовался; проверен только fallback.
3. **Запись в боевую Obsidian-базу**: запрещено в тестах; проверено на temp-vault.
4. **Браузер**: `demo/index.html` собран, но консольные ошибки и вид глазами не проверены
   (нужен `UX_REVIEW.md` / проверка пользователя).
5. **24/7**: ротация `RotatingFileHandler` 5 МБ × 3 покрыта кодом, но не наблюдалась на
   длинном прогоне; stdout launchd пишется в отдельный `agent.log` без ротации (backlog #4).
6. **Установка на чистую систему**: только `--dry-run` и `bash -n`.

## 4. Как воспроизвести

```bash
cd "/Users/user/Documents/deepseek-harness/default-workspace/telegram-life-agent"
python3 -m unittest discover tests -v                 # 148 тестов
python3 -m unittest discover -s tests -p test_obsidian.py
python3 src/main.py --dry-run --once                  # smoke, пишет в data/
bash install.sh --dry-run                             # план установки, ничего не меняет
```

Для прогона без следов в проекте — временный конфиг с абсолютными `paths.*` в `mktemp -d`
(так делалась проверка §2.5).

## 5. Правила ведения

- Каждый прогон — строка с датой, командой, результатом и ссылкой на доказательство.
- «Зелёный» без числа тестов не считается доказательством.
- Падения не удаляются: строка остаётся, рядом — фикс и повторный прогон.
- Цифры агентских самопроверок помечаются «по отчёту», пока нет протокола в репозитории.
