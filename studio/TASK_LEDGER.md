# TASK_LEDGER — доска задач Life OS

**Дата среза:** 2026-11-16 16:58 MSK · **Основание:** `PLAN.md` (T1–T8), артефакты в проекте.
**Важно:** файла `team_task_list` в проекте и в workspace нет (проверено `find` по
`/Users/user/Documents/deepseek-harness/default-workspace`). Волна 1 (T1–T8) — из `PLAN.md`
дословно. Волна 2 (T9–T18) восстановлена по составу параллельных агентов и ожидаемых
отчётов; если ID разойдутся с доской Lead'а — правится только эта таблица.

**Текущее состояние (17:00):** идёт волна правок P1 — меняются `telegram.py`, `scheduler.py`,
`storage.py`, `dashboard_data.py`, plist и README. Suite: 16:58 — 149/150 (ролловер дважды),
17:00 — снова 150/150 OK; e2e 47/47. Реестр блокеров открыт до проверки фиксов.

**Статусы:** `готово` · `в работе` · `ожидается` · `блок` (внешняя зависимость).

## Волна 1 — контракты и сборка (PLAN.md)

| ID | Тема | Владелец | Статус | Результат | Отчёт / доказательство |
|---|---|---|---|---|---|
| T1 | Каркас бота: конфиг, Telegram-клиент, SQLite, планировщик, цикл | bot-core | готово | `src/config.py` 286, `src/telegram.py` 235, `src/storage.py` 397, `src/scheduler.py` 1320, `src/main.py` 466 строк. Long polling, 12 команд + русские алиасы, сессии в kv, TTL 12 ч, догон 180 мин, pid-лок, ротация лога 5 МБ × 3 | 84 теста (`test_config` 14, `test_storage` 13, `test_telegram` 18, `test_scheduler` 39) — `TEST_HISTORY.md` §2.1 |
| T2 | LLM-клиент, промпты, извлечение данных | brain | готово | `src/llm.py` 360, `src/prompts.py` 471, `src/parsing.py` 700 строк. SAMP `/v1/chat/completions`, модель `deepseek-v4.1-flash-1017`, явный `User-Agent: LifeOS/1.0`, fallback без LLM | 17 тестов `test_parsing`; фикс 403 Cloudflare — `DECISION_LOG.md` D-03 |
| T3 | Obsidian: дневник, цели, зеркало JSONL | vault | готово | `src/obsidian.py` 1048 строк. Daily из шаблона, маркеры `lifeos:entry:ID`, таблица целей между маркерами, `entries.jsonl` + `goals.json`, атомарная запись через `os.replace` | 19 тестов `test_obsidian` |
| T4 | Дашборд: данные + HTML/CSS/JS | dash | готово | `src/dashboard_data.py` 927, `dashboard/template.html`: первая версия 701 строка (Lead, 16:34), к 16:47 — 1 600+ строк (~77 КБ). 10 карточек, `window.LIFEOS_DATA`, `data.json` рядом, пустые состояния без NaN | 8 тестов `test_dashboard`; демо `demo/index.html` 41 785 Б + `demo/data.json` 7 084 Б |
| T5 | Установка, launchd, README, пример конфига | ops | готово | `install.sh` 739, `uninstall.sh` 229, `ops/com.lifeos.agent.plist` 37, `README.md` 324, `config/config.example.json`. Права 600, подхват `DDD_API_KEY`, снятие карантина, `launchctl bootstrap` + fallback `load -w` | 12 тестов `test_install`; `install.sh --dry-run` rc=0 |
| T6 | Мок-сервер + unit/e2e тесты | qa | готово | Мок `src/mock_telegram.py` 495 строк (429/409/401/500, long poll), 9 модулей unit-тестов (150 тестов / 551 проверка) + `tests/e2e_mock.py` 616 строк (47 проверок) | Прогоны 2026-11-16 16:47 (unit) и 16:42–16:48 (e2e) — `TEST_HISTORY.md` §1, §2.6 |
| T7 | Интеграция модулей, e2e, демо-дашборд | Lead | готово | Склейка `main.py`, демо `demo/` (16:34), `README.md`, `PROJECT_CONTEXT.md`; smoke `python3 src/main.py --dry-run --once` rc=0 (создаёт БД, `dashboard/index.html` + `data.json`) | `TEST_HISTORY.md` §2.5 |
| T8 | Реальная установка: launchd + живой бот | Lead | блок | Нет токена @BotFather и `chat_id` — живой запуск невозможен (ТЗ §9, риск 1). Всё остальное готово, `--dry-run` проходит | `TEST_HISTORY.md` §2.4 |

## Волна 2 — ревью, аудиты, релиз, память (текущая)

| ID | Тема | Владелец | Статус | Результат | Отчёт |
|---|---|---|---|---|---|
| T9 | E2E на мок-сервере: слот → ответы → vault → `written=1` → дашборд, повтор без дублей | qa | готово | `tests/e2e_mock.py` 616 строк: 47 проверок, rc=0; повторный запуск не дублирует записи и маркеры (3 → 3), SIGTERM → код 0, 500 от мока не роняет процесс | `TEST_HISTORY.md` §2.6 |
| T10 | Тесты установки: `bash -n`, `plutil -lint`, README, пример конфига | qa | готово | `tests/test_install.py` 174 строки, 12 тестов (16:38) | 12/12 OK |
| T11 | Код-ревью проекта | review | готово | `REVIEW.md` 238 строк: **P0 нет**; 4 P1 (offset `getUpdates` не сохраняется → дубли после рестарта; `--dry-run` пишет в рабочую БД/vault; утечка токена при неверном `api_base`; пути логов расходятся с README); 30 P2. Независимо прогнаны `compileall` на 3.9.6 и 3.13, 150 тестов, 47/47 e2e, сборка дашборда + JS в DOM-заглушке | `REVIEW.md` |
| T12 | Аудит безопасности | security | готово | `SECURITY_AUDIT.md` 455 строк: **P0 нет, 3 P1, 12 P2**. Проверены права/секреты, SQL (везде `?`), XSS (payload'ы), TLS, path traversal, install с пробелами/кириллицей, pid-lock, read-only vault, таймаут LLM | `SECURITY_AUDIT.md` |
| T13 | UX-ревью дашборда и сценария | ux | готово | `UX_REVIEW.md` 645 строк, оценка **73/100**, вердикт «к пользователю не готово»: 2 P0 (первый запуск начинает опрос до знакомства + нет тихих часов; README врёт про приватность), 12 P1 (fallback без цифр, вопросы-анкета, follow-up только <30 символов, цели не обновляются, мобильная шапка). Проверено в Safari 1440/1024/390 + OCR, первый день 08:30→21:30 на temp-БД | `UX_REVIEW.md` |
| T14 | Состязательная проверка (adversarial) | adversarial | готово | `ADVERSARIAL_REPORT.md` 517 строк, 64 сценария: **P0 нет, 5 P1, 17 P2, 12 P3**. Найдено: pid-лок берут 7 из 12 процессов; LLM-ответ 250k → 63 сообщения; `split_message` считает символы вместо UTF-16 (7 800 units → 400); `<!--<script>` убивает дашборд навсегда; подделка маркера скрывает запись, ставя `written=1` | `ADVERSARIAL_REPORT.md` |
| T15 | Производительность | perf | ожидается | `PERF_REPORT.md` отсутствует | ожидается |
| T16 | Релиз: `CHANGELOG.md`, версия, `check.sh` | release | готово | `CHANGELOG.md` 107 строк (Keep a Changelog, `[0.1.0] — 2026-11-16`), `VERSION` = `0.1.0`, `RELEASE_NOTES.md` в корне 131 строка (для пользователя), `check.sh` 300 строк (11 проверок, PASS), `.gitignore` 36 строк. **Хвост:** `src/__init__.py:11` всё ещё `__version__ = "1.0.0"` — расхождение с `VERSION` 0.1.0; тег не ставился (git-репозитория в проекте нет) | `CHANGELOG.md`, `RELEASE_NOTES.md`, `TEST_HISTORY.md` §2.7 |
| T17 | Память команды: `studio/` (8 документов) | studio | готово | `studio/PROJECT_BIBLE.md`, `TASK_LEDGER.md`, `DECISION_LOG.md`, `TEST_HISTORY.md`, `QUALITY_BACKLOG.md`, `ARCHITECTURE_MAP.md`, `AGENT_PERFORMANCE.md`, `RELEASE_NOTES.md` | этот файл |
| T18 | Финальная приёмка и установка у пользователя | Lead | в работе | Блок T8 (токен); приёмка по 7 пунктам ТЗ §8 — 5 ✅, 1 ◐, 1 ❌ (`PROJECT_BIBLE.md` §2) | `PROJECT_BIBLE.md` §2 |

## Правила ведения

1. Одна задача — одна строка. Статус меняется только владельцем или Lead'ом.
2. В «Результат» — пути и цифры (строки, тесты), а не «сделано».
3. В «Отчёт» — файл, где лежит доказательство; переписка доказательством не считается.
4. Новая задача получает следующий свободный T-номер; закрытые не удаляются.
5. Блокер пишется в статус (`блок`) с внешней причиной: токен, доступ, чужой файл.
