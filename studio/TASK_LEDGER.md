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
| T15 | Производительность | perf | заменено | Отчёт perf-агента не пришёл; замеры сняты Lead'ом в `studio/TEST_HISTORY.md` §2.9 (unit 266 за 1,4 с / 1,6 с; e2e 47 за ~12 с; `check.sh` 11 проверок). Отдельный `PERF_REPORT.md` признан избыточным | `TEST_HISTORY.md` §2.9 |
| T16 | Релиз: `CHANGELOG.md`, версия, `check.sh` | release | готово | `CHANGELOG.md` 107 строк (Keep a Changelog, `[0.1.0] — 2026-11-16`), `VERSION` = `0.1.0`, `RELEASE_NOTES.md` в корне 131 строка (для пользователя), `check.sh` 300 строк (11 проверок, PASS), `.gitignore` 36 строк. **Закрыто задним числом:** `src/__init__.py:11` = `0.1.1` (совпадает с `VERSION`); git-репозиторий есть (ветка `main`, коммиты `b40194d` → `e363159` → `c87d5da`), тег `v0.1.1` | `CHANGELOG.md`, `RELEASE_NOTES.md`, `TEST_HISTORY.md` §2.7 |
| T17 | Память команды: `studio/` (8 документов) | studio | готово | `studio/PROJECT_BIBLE.md`, `TASK_LEDGER.md`, `DECISION_LOG.md`, `TEST_HISTORY.md`, `QUALITY_BACKLOG.md`, `ARCHITECTURE_MAP.md`, `AGENT_PERFORMANCE.md`, `RELEASE_NOTES.md` | этот файл |
| T18 | Финальная приёмка и установка у пользователя | Lead | в работе | Блок T8 (токен); приёмка по 7 пунктам ТЗ §8 — 5 ✅, 1 ◐, 1 ❌ (`PROJECT_BIBLE.md` §2) | `PROJECT_BIBLE.md` §2 |

## Волна 3 — онбординг, цели, привычки, деньги и обучение (2026-11-16, вечер)

| ID | Тема | Владелец | Статус | Результат | Отчёт |
|---|---|---|---|---|---|
| T19 | Онбординг при первом запуске + тихие часы | bot-core | готово | Знакомство до первого вопроса; окно тихих часов в конфиге и планировщике | `CHANGELOG.md` [0.1.1], коммит `c87d5da` |
| T20 | Единый прогресс целей + привычки | brain / vault | готово | Общая формула прогресса (`INTERFACES.md` §15), срез привычек в дневнике и дашборде | коммит `c87d5da` |
| T21 | P1 из ревью/аудитов: offset, dry-run, токен, логи | bot-core | готово | `getUpdates` offset в БД, `--dry-run` на temp-путях, токен не попадает в лог при чужом `api_base`, пути логов = README | коммит `c87d5da` |
| T22 | Контраст дашборда и мобильная шапка | dash | готово | Правки контраста по WCAG, шапка на 390 px | коммит `c87d5da` |
| T23 | Финансы и обучение (v0.2) | core-dev | готово | `src/storage.py`: таблицы `money`/`learning` + индексы, `add_money`/`add_learning` с валидацией, отчёты за день/неделю/месяц, парсер сумм; команды в боте | 266 тестов (`TEST_HISTORY.md` §2.12), доска: `t2` |
| T24 | Устойчивость бота: backoff, healthcheck, ротация логов | core-dev | готово | Backoff 2→60 с + retry_after ≤900 с и reset после успеха; пульс `tick:last_ok` в kv; `tools/health.py` (6 проверок, `--rotate`); long-poll 10 с; один архив лога; `ExitTimeOut 30` в plist; 65 тестов в `tests/test_resilience.py` | 335/335 на 3.13 и 3.9, check.sh PASS — §2.12–2.13, коммит `b4429ab` |

## Волна 4 — Life OS v3 (`app/`, SPA, медиа) — идёт

Полный замысел — `MASTER_SPEC.md`; нарезка на доске команды (`t5`–`t10`).

| ID | Тема | Владелец | Статус | Результат | Отчёт |
|---|---|---|---|---|---|
| T25 | Ф1: фундамент `app/` — настройки, схема БД, FTS5, модели | api-dev | готово | `app/server/settings.py` (env `LIFEOS_*` + config v0.x, секреты вне repr), `db.py` (13 таблиц, FTS5 `entries_fts`/`segments_fts` с триггерами, WAL, `LegacySchemaError`), `models.py` (Overview под контракт `app/web/src/api/types.ts`) | 49 тестов + компиле; прогон `pytest app/tests` 83 passed — доска: `t5` |
| T26 | Ф5: React SPA — каркас, Sidebar 3.0, Обзор, ⌘K | web-dev | готово | 13 маршрутов, сайдбар 260↔68, ⌘K, мобильная навигация, темы, reduced-motion; Overview 523 строки (6 KPI + виджеты), Settings с токеном владельца | `npm run build` exit 0 (~112,8 кБ gzip); DOM-проверка 4 страниц — `REPORT-v3-phase5.md` |
| T27 | Ф4: FastAPI — API, WebSocket, раздача медиа с Range | api-dev | готово | 8 роутеров (overview, journal, health, courses, sections, quick, settings, analytics), `/media` Range `206`, WS-хаб, auth-middleware, кеш-политика | `test_e2e_api` 15, `test_sections` 27, `test_auth_pwa` 17 — `REPORT-v3-phase4.md` |
| T28 | Ф2–Ф3: aiogram-бот, приём медиа, очередь задач, адаптер транскрибации | media-dev | готово (без живого токена) | `handlers.py` 687, `intent.py` 602, `media.py` 285, `media_store.py` 668, воркер 613, адаптер 495; whitelist, команды, `pending_toolchain` | 50 тестов бота/медиа; живой запуск ждёт токен — `REPORT-v3-phase2.md` |
| T29 | Ф1: движок Obsidian — дневник, курсы, здоровье (идемпотентно) | dash-v2 | готово | `vault.py` 1 256: маркеры `lifeos:entry:ID`, шаблон дня, слияние frontmatter, курсы/здоровье, защита путей, атомарная запись | 60 тестов `test_vault` — `REPORT-v3-phase3.md` |
| T30 | Ф1: сидер — 120 дней демо-данных и 2 курса с таймкодами | core-dev | готово | `seeder.py` 827: 824 метрики, 382 отметки, 3 цели, 2 курса, 12 уроков, 162 сегмента; `--seed 42`, идемпотентен; демо-медиа — `tools/make_demo_media.py` | 30 тестов `test_seeder` — `REPORT-v3-phase3.md` |
| T31 | Сайдбар дашборда 0.1.2: навигация, поиск, тёмная тема, мобильная версия | sidebar-dev | готово | Навигация по 10 разделам (scrollspy, свёрнутый 76px), поиск по журналу, тёмная тема AA, drawer ≤900px, `tools/contrast_check.js`; `check.sh` не падает на node_modules; VERSION/CHANGELOG/RELEASE_NOTES/llms.txt → 0.1.2 | 335/335 (оба питона), e2e 47/47, check.sh 11/11, тег `v0.1.2` — §2.13, коммит `ae29227` |
| T33 | Deploy-артефакты VPS/мини-сервера | deploy | готово | `deploy/` 10 файлов (compose 6 сервисов, 3 Dockerfile, Caddyfile, nginx, 2 systemd, `.env.example`, `check-deploy.sh` 630) + `DEPLOY_GUIDE.md` 690; переменные bridged (D-16) | `check-deploy.sh` 119✔ 0✖; docker/systemd в среде нет — «не проверено» в §10; коммит `d0a6c9e` |
| T34 | SPA-страницы «Журнал», «Аналитика», «Сон», «Настроение» (v3) | sidebar-dev | готово | «Аналитика» — отдельная страница с корреляциями, периодами 30/90/180 и 4 статусами; «Журнал», «Сон», «Настроение» — общий `SectionPage` на живых данных | `REPORT-v3-phase5.md` |
| T32 | Дашборд 0.2: разделы «Курсы» и «Тело» | dash-v2 | готово | «Курсы» 403 строки (витрина, плеер, таймкоды, поиск по субтитрам, прогресс), «Тело и весы» 725 строк (5 KPI, SVG EMA, прогноз цели, состав 7/30/90, записи) | tsc/build pass, живой DOM на 8787 — `REPORT-v3-phase5.md` |
| T35 | Аналитика v3: прогноз даты цели, дельты состава тела, корреляции с честными статусами | Lead | готово | `linear_trend`, `weight_forecast` (ok/flat/insufficient_data/no_target/away_from_target/reached), `composition_deltas` 7/30/90; `/api/health/weight` + `/api/health/composition`; инсайт Обзора различает «мало данных» и «слабая связь» (D-18) | 15 тестов `test_analytics.py` — `REPORT-v3-phase4.md` |
| T36 | PWA и auth: манифест, service worker, токен владельца | Lead | готово | manifest/SW/offline/иконки; `X-LifeOS-Token` + cookie, 401 без токена, `/api/health` публичный, `no-cache`/`immutable`; в SPA — поле токена в «Настройках» и cookie при старте | 17 тестов `test_auth_pwa.py` — `REPORT-v3-phase6.md` |
| T37 | Независимая проверка v3 (QA) | qa-ui | готово | сборка, 272 теста, check.sh 11, deploy 121, живой smoke на :8789, DOM 4 страниц, скриншоты; P0/P1 нет, 3 P2 | `studio/REPORT-v3-qa.md` |
| T38 | Отчёты фаз v3 и приёмка | Lead | готово | `REPORT-v3-phase1..7.md`, `TASK_LEDGER`, `DECISION_LOG` (D-18, D-19), `RELEASE_NOTES.md` (3.0.0-alpha) | `REPORT-v3-phase7.md` |

## Правила ведения

1. Одна задача — одна строка. Статус меняется только владельцем или Lead'ом.
2. В «Результат» — пути и цифры (строки, тесты), а не «сделано».
3. В «Отчёт» — файл, где лежит доказательство; переписка доказательством не считается.
4. Новая задача получает следующий свободный T-номер; закрытые не удаляются.
5. Блокер пишется в статус (`блок`) с внешней причиной: токен, доступ, чужой файл.
