# TASK_LEDGER — доска задач Life OS

**Дата среза:** 2026-11-16 16:40 MSK · **Основание:** `PLAN.md` (T1–T8), артефакты в проекте.
**Важно:** файла `team_task_list` в проекте и в workspace нет (проверено `find` по
`/Users/user/Documents/deepseek-harness/default-workspace`). Волна 1 (T1–T8) — из `PLAN.md`
дословно. Волна 2 (T9–T18) восстановлена по составу параллельных агентов и ожидаемых
отчётов; если ID разойдутся с доской Lead'а — правится только эта таблица.

**Статусы:** `готово` · `в работе` · `ожидается` · `блок` (внешняя зависимость).

## Волна 1 — контракты и сборка (PLAN.md)

| ID | Тема | Владелец | Статус | Результат | Отчёт / доказательство |
|---|---|---|---|---|---|
| T1 | Каркас бота: конфиг, Telegram-клиент, SQLite, планировщик, цикл | bot-core | готово | `src/config.py` 286, `src/telegram.py` 235, `src/storage.py` 397, `src/scheduler.py` 1320, `src/main.py` 466 строк. Long polling, 12 команд + русские алиасы, сессии в kv, TTL 12 ч, догон 180 мин, pid-лок, ротация лога 5 МБ × 3 | 83 теста (`test_config` 14, `test_storage` 12, `test_telegram` 18, `test_scheduler` 39) — `TEST_HISTORY.md` §2.1 |
| T2 | LLM-клиент, промпты, извлечение данных | brain | готово | `src/llm.py` 360, `src/prompts.py` 471, `src/parsing.py` 700 строк. SAMP `/v1/chat/completions`, модель `deepseek-v4.1-flash-1017`, явный `User-Agent: LifeOS/1.0`, fallback без LLM | 17 тестов `test_parsing`; фикс 403 Cloudflare — `DECISION_LOG.md` D-03 |
| T3 | Obsidian: дневник, цели, зеркало JSONL | vault | готово | `src/obsidian.py` 1048 строк. Daily из шаблона, маркеры `lifeos:entry:ID`, таблица целей между маркерами, `entries.jsonl` + `goals.json`, атомарная запись через `os.replace` | 18 тестов `test_obsidian` |
| T4 | Дашборд: данные + HTML/CSS/JS | dash | готово | `src/dashboard_data.py` 927, `dashboard/template.html` 701 строка (37 523 Б). 10 карточек, `window.LIFEOS_DATA`, `data.json` рядом, пустые состояния без NaN | 8 тестов `test_dashboard`; демо `demo/index.html` 41 785 Б + `demo/data.json` 7 084 Б |
| T5 | Установка, launchd, README, пример конфига | ops | готово | `install.sh` 739, `uninstall.sh` 229, `ops/com.lifeos.agent.plist` 37, `README.md` 324, `config/config.example.json`. Права 600, подхват `DDD_API_KEY`, снятие карантина, `launchctl bootstrap` + fallback `load -w` | 12 тестов `test_install`; `install.sh --dry-run` rc=0 |
| T6 | Мок-сервер + unit/e2e тесты | qa | в работе | Мок `src/mock_telegram.py` 495 строк (429/409/401/500, long poll), 8 модулей тестов, 148 тестов / 544 проверки. **Нет `tests/e2e_mock.py`** (требуется INTERFACES §0, ТЗ §8.2) | Прогон 2026-11-16 16:39 — `TEST_HISTORY.md` §1 |
| T7 | Интеграция модулей, e2e, демо-дашборд | Lead | готово | Склейка `main.py`, демо `demo/` (16:34), `README.md`, `PROJECT_CONTEXT.md`; smoke `python3 src/main.py --dry-run --once` rc=0 (создаёт БД, `dashboard/index.html` + `data.json`) | `TEST_HISTORY.md` §2.5 |
| T8 | Реальная установка: launchd + живой бот | Lead | блок | Нет токена @BotFather и `chat_id` — живой запуск невозможен (ТЗ §9, риск 1). Всё остальное готово, `--dry-run` проходит | `TEST_HISTORY.md` §2.4 |

## Волна 2 — ревью, аудиты, релиз, память (текущая)

| ID | Тема | Владелец | Статус | Результат | Отчёт |
|---|---|---|---|---|---|
| T9 | E2E на мок-сервере: слот → ответы → vault → `written=1` → дашборд, повтор без дублей | qa | в работе | Файла `tests/e2e_mock.py` нет; мок и unit-тесты готовы | ожидается |
| T10 | Тесты установки: `bash -n`, `plutil -lint`, README, пример конфига | ops | готово | `tests/test_install.py` 174 строки, 12 тестов (16:38) | 12/12 OK |
| T11 | Код-ревью проекта | review | ожидается | `REVIEW.md` в корне отсутствует | ожидается |
| T12 | Аудит безопасности | security | ожидается | `SECURITY_AUDIT.md` отсутствует | ожидается |
| T13 | UX-ревью дашборда | ux | ожидается | `UX_REVIEW.md` отсутствует | ожидается |
| T14 | Состязательная проверка (adversarial) | adversarial | ожидается | `ADVERSARIAL_REPORT.md` отсутствует | ожидается |
| T15 | Производительность | perf | ожидается | `PERF_REPORT.md` отсутствует | ожидается |
| T16 | Релиз: `CHANGELOG.md`, версия, тег | release | ожидается | `CHANGELOG.md` отсутствует; `src/__init__.py:11` уже объявляет `__version__ = "1.0.0"` — расхождение с планом v0.1.0, см. `RELEASE_NOTES.md` §5 | ожидается |
| T17 | Память команды: `studio/` (8 документов) | studio | готово | `studio/PROJECT_BIBLE.md`, `TASK_LEDGER.md`, `DECISION_LOG.md`, `TEST_HISTORY.md`, `QUALITY_BACKLOG.md`, `ARCHITECTURE_MAP.md`, `AGENT_PERFORMANCE.md`, `RELEASE_NOTES.md` | этот файл |
| T18 | Финальная приёмка и установка у пользователя | Lead | в работе | Блокировано T8 (токен); приёмка по 7 пунктам ТЗ §8 — 4 ✅, 1 ◐, 2 ❌ (`PROJECT_BIBLE.md` §2) | `PROJECT_BIBLE.md` §2 |

## Правила ведения

1. Одна задача — одна строка. Статус меняется только владельцем или Lead'ом.
2. В «Результат» — пути и цифры (строки, тесты), а не «сделано».
3. В «Отчёт» — файл, где лежит доказательство; переписка доказательством не считается.
4. Новая задача получает следующий свободный T-номер; закрытые не удаляются.
5. Блокер пишется в статус (`блок`) с внешней причиной: токен, доступ, чужой файл.
